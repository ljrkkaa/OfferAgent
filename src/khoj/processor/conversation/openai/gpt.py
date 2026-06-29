import logging
import os
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

from langchain_core.messages.chat import ChatMessage

from khoj.database.models import Agent, ChatMessageModel, ChatModel
from khoj.processor.conversation import prompts
from khoj.processor.conversation.openai.utils import (
    chat_completion_with_backoff,
    clean_response_schema,
    completion_with_backoff,
    get_effective_openai_api_base_url,
    get_structured_output_support,
    is_cerebras_api,
    responses_chat_completion_with_backoff,
    responses_completion_with_backoff,
    supports_responses_api,
    to_openai_tools,
)
from khoj.processor.conversation.utils import (
    ResponseWithThought,
    StructuredOutputSupport,
    generate_chatml_messages_with_context,
    messages_to_print,
)
from khoj.utils.helpers import ToolDefinition
from khoj.utils.yaml import yaml_dump

logger = logging.getLogger(__name__)


def openai_send_message_to_model(
    messages,
    api_key,
    model: str,
    response_type="text",
    response_schema=None,
    tools: list[ToolDefinition] = None,
    deepthought=False,
    api_base_url: str | None = None,
    tracer: dict = {},
):
    """
    Send message to model
    """

    api_base_url = get_effective_openai_api_base_url(api_base_url)
    model_kwargs: Dict[str, Any] = {}
    json_support = get_structured_output_support(model, api_base_url)
    strict = not is_cerebras_api(api_base_url)
    if tools and json_support == StructuredOutputSupport.TOOL:
        model_kwargs["tools"] = to_openai_tools(tools, model=model, api_base_url=api_base_url)
    elif response_schema and json_support >= StructuredOutputSupport.SCHEMA:
        # Drop unsupported fields from schema passed to OpenAI APi
        cleaned_response_schema = clean_response_schema(response_schema)
        if supports_responses_api(model, api_base_url):
            model_kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "strict": strict,
                    "name": response_schema.__name__,
                    "schema": cleaned_response_schema,
                }
            }
        else:
            model_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "schema": cleaned_response_schema,
                    "name": response_schema.__name__,
                    "strict": strict,
                },
            }
    elif response_type == "json_object" and json_support == StructuredOutputSupport.OBJECT:
        model_kwargs["response_format"] = {"type": response_type}

    # Get Response from GPT
    if supports_responses_api(model, api_base_url):
        return responses_completion_with_backoff(
            messages=messages,
            model_name=model,
            openai_api_key=api_key,
            api_base_url=api_base_url,
            deepthought=deepthought,
            model_kwargs=model_kwargs,
            tracer=tracer,
        )
    else:
        return completion_with_backoff(
            messages=messages,
            model_name=model,
            openai_api_key=api_key,
            api_base_url=api_base_url,
            deepthought=deepthought,
            model_kwargs=model_kwargs,
            tracer=tracer,
        )


async def converse_openai(
    # Query
    messages: Optional[List[ChatMessage]] = None,
    # Model
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    api_base_url: Optional[str] = None,
    temperature: float = 0.6,
    deepthought: Optional[bool] = False,
    tracer: dict = {},
    # Legacy test/direct-call interface
    references: Optional[List[Dict[str, Any]]] = None,
    user_query: Optional[str] = None,
    chat_history: Optional[List[ChatMessageModel]] = None,
    agent: Optional[Agent] = None,
) -> AsyncGenerator[ResponseWithThought, None]:
    """
    Converse with user using OpenAI's ChatGPT
    """
    model = model or os.getenv("KHOJ_DEFAULT_CHAT_MODEL", "gpt-4.1-mini")
    api_base_url = get_effective_openai_api_base_url(api_base_url)
    if messages is None:
        if user_query is None:
            raise TypeError("converse_openai requires messages or user_query")
        current_date = datetime.now()
        if agent and agent.personality:
            system_prompt = prompts.custom_personality.format(
                name=agent.name,
                bio=agent.personality,
                current_date=current_date.strftime("%Y-%m-%d"),
                day_of_week=current_date.strftime("%A"),
            )
        else:
            system_prompt = prompts.personality.format(
                current_date=current_date.strftime("%Y-%m-%d"),
                day_of_week=current_date.strftime("%A"),
            )
        context_message = ""
        if references:
            context_message = prompts.notes_conversation.format(references=yaml_dump(references))
        messages = generate_chatml_messages_with_context(
            user_message=user_query,
            context_message=context_message,
            chat_history=chat_history or [],
            system_message=system_prompt,
            model_name=model,
            model_type=ChatModel.ModelType.OPENAI,
        )

    logger.debug(f"Conversation Context for GPT: {messages_to_print(messages)}")

    # Get Response from GPT
    if supports_responses_api(model, api_base_url):
        async for chunk in responses_chat_completion_with_backoff(
            messages=messages,
            model_name=model,
            temperature=temperature,
            openai_api_key=api_key,
            api_base_url=api_base_url,
            deepthought=deepthought,
            tracer=tracer,
        ):
            yield chunk
    else:
        # For non-OpenAI APIs, use the chat completion method
        async for chunk in chat_completion_with_backoff(
            messages=messages,
            model_name=model,
            temperature=temperature,
            openai_api_key=api_key,
            api_base_url=api_base_url,
            deepthought=deepthought,
            tracer=tracer,
        ):
            yield chunk
