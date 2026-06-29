import json
import os
from copy import deepcopy
from typing import Any

from langchain_core.messages.chat import ChatMessage

from khoj.processor.conversation.codex.auth import get_codex_base_url, get_codex_service_tier
from khoj.processor.conversation.openai.utils import (
    _extract_text_for_instructions,
    clean_response_schema,
    format_message_for_api,
    to_openai_tools,
)
from khoj.processor.conversation.utils import ResponseWithThought, ToolCall
from khoj.utils.helpers import ToolDefinition, is_none_or_empty

RESPONSES_FORMAT_BASE_URL = "https://api.openai.com/v1"


def use_codex_runtime() -> bool:
    return os.getenv("KHOJ_CONVERSATION_RUNTIME", "codex").lower() == "codex"


def build_codex_response_kwargs(
    messages: list[ChatMessage],
    model: str,
    response_type: str = "text",
    response_schema: Any = None,
    tools: list[ToolDefinition] | None = None,
    deepthought: bool = False,
) -> dict[str, Any]:
    formatted_messages = format_message_for_api(messages, model, RESPONSES_FORMAT_BASE_URL)
    instructions = None
    if formatted_messages and formatted_messages[0].get("role") == "system":
        instructions = _extract_text_for_instructions(formatted_messages[0].get("content")) or None
        formatted_messages = formatted_messages[1:]

    kwargs: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": formatted_messages,
        "store": False,
        "reasoning": {"effort": "medium" if deepthought else "low", "summary": "auto"},
    }
    service_tier = get_codex_service_tier()
    if service_tier:
        kwargs["service_tier"] = service_tier

    codex_tools = to_openai_tools(tools or [], model=model, api_base_url=RESPONSES_FORMAT_BASE_URL)
    if codex_tools:
        kwargs["tools"] = codex_tools
        kwargs["tool_choice"] = "auto"
        kwargs["parallel_tool_calls"] = True

    if response_schema:
        kwargs["text"] = {
            "format": {
                "type": "json_schema",
                "strict": True,
                "name": response_schema.__name__,
                "schema": clean_response_schema(response_schema),
            }
        }
    elif response_type == "json_object":
        kwargs["text"] = {"format": {"type": "json_object"}}

    return kwargs


def drop_reasoning_summary(kwargs: dict[str, Any]) -> dict[str, Any]:
    retry_kwargs = deepcopy(kwargs)
    reasoning = retry_kwargs.get("reasoning")
    if isinstance(reasoning, dict):
        reasoning.pop("summary", None)
    return retry_kwargs


def raw_item_dump(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if isinstance(item, dict):
        return item
    return dict(getattr(item, "__dict__", {}))


def _item_value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _summary_text(summary: Any) -> str:
    if not isinstance(summary, list):
        return ""
    parts = []
    for item in summary:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("text"):
            parts.append(str(item["text"]))
        elif getattr(item, "text", None):
            parts.append(str(item.text))
    return "\n\n".join(parts)


def normalize_codex_response(response: Any) -> ResponseWithThought:
    output = getattr(response, "output", None)
    if not isinstance(output, list):
        output = []

    raw_content = [raw_item_dump(item) for item in output]
    aggregated_text = getattr(response, "output_text", "") or ""
    thoughts = ""
    tool_calls: list[ToolCall] = []

    for item in output:
        item_type = _item_value(item, "type", "")
        if item_type == "function_call" or all(_item_value(item, key) is not None for key in ("name", "arguments")):
            arguments = _item_value(item, "arguments", "{}")
            try:
                args = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid Codex tool call arguments for {_item_value(item, 'name')}: {arguments}"
                ) from exc
            tool_calls.append(ToolCall(name=_item_value(item, "name"), args=args, id=_item_value(item, "call_id")))
        elif item_type == "reasoning":
            item_thoughts = _summary_text(_item_value(item, "summary", []))
            if item_thoughts:
                thoughts = "\n\n".join(part for part in [thoughts, item_thoughts] if part)

    if tool_calls:
        if thoughts and aggregated_text:
            thoughts = "\n".join([f"*{line.strip()}*" for line in thoughts.splitlines() if line.strip()])
            thoughts = f"{thoughts}\n\n{aggregated_text}"
        else:
            thoughts = thoughts or aggregated_text
        aggregated_text = json.dumps([tool_call.__dict__ for tool_call in tool_calls])

    if is_none_or_empty(aggregated_text):
        raise ValueError("Empty response returned by Codex backend")

    return ResponseWithThought(text=aggregated_text, thought=thoughts, raw_content=raw_content)


def is_reasoning_summary_rejected(exc: Exception) -> bool:
    message = str(exc).lower()
    return "reasoning" in message and "summary" in message


def codex_chat_model_label(model: str) -> str:
    return f"codex:{model}"


__all__ = [
    "build_codex_response_kwargs",
    "codex_chat_model_label",
    "drop_reasoning_summary",
    "get_codex_base_url",
    "is_reasoning_summary_rejected",
    "normalize_codex_response",
    "use_codex_runtime",
]
