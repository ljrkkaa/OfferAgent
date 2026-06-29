import asyncio
import logging
from types import SimpleNamespace
from typing import Any, AsyncGenerator

import httpx
import openai
from langchain_core.messages.chat import ChatMessage

from khoj.processor.conversation.codex.auth import (
    CodexAuthError,
    CodexAuthResolver,
    get_codex_base_url,
    get_codex_model,
)
from khoj.processor.conversation.codex.utils import (
    build_codex_response_kwargs,
    codex_chat_model_label,
    drop_reasoning_summary,
    is_reasoning_summary_rejected,
    normalize_codex_response,
)
from khoj.processor.conversation.utils import ResponseWithThought, commit_conversation_trace
from khoj.utils.helpers import ToolDefinition, is_promptrace_enabled

logger = logging.getLogger(__name__)


def _codex_client(resolver: CodexAuthResolver | None = None) -> openai.OpenAI:
    resolver = resolver or CodexAuthResolver()
    headers = resolver.headers()
    token = headers["Authorization"].removeprefix("Bearer ").strip()
    return openai.OpenAI(api_key=token, base_url=get_codex_base_url(), default_headers=headers)


def _raise_clear_codex_error(exc: Exception) -> None:
    response = getattr(exc, "response", None)
    status_code = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    headers = getattr(response, "headers", {}) or {}
    if status_code == 403 and str(headers.get("cf-mitigated", "")).lower() == "challenge":
        raise CodexAuthError("codex_cloudflare_challenge", "Codex backend returned Cloudflare challenge") from exc


def _requires_stream(exc: Exception) -> bool:
    return "stream must be set to true" in str(exc).lower()


def _response_from_stream(stream) -> Any:
    aggregated_text = ""
    for event in stream:
        if getattr(event, "type", "") == "response.output_text.delta":
            aggregated_text += getattr(event, "delta", "") or getattr(event, "output_text", "")
    response = stream.get_final_response()
    if aggregated_text and not getattr(response, "output_text", ""):
        return SimpleNamespace(
            output_text=aggregated_text,
            output=getattr(response, "output", []) or [],
            usage=getattr(response, "usage", None),
        )
    return response


def _stream_response(client: openai.OpenAI, kwargs: dict[str, Any]):
    try:
        with client.responses.stream(timeout=httpx.Timeout(30, read=300), **kwargs) as stream:
            return _response_from_stream(stream)
    except openai.BadRequestError as exc:
        if is_reasoning_summary_rejected(exc):
            with client.responses.stream(
                timeout=httpx.Timeout(30, read=300), **drop_reasoning_summary(kwargs)
            ) as stream:
                return _response_from_stream(stream)
        raise
    except Exception as exc:
        _raise_clear_codex_error(exc)
        raise


def _create_response(client: openai.OpenAI, kwargs: dict[str, Any]):
    try:
        return client.responses.create(timeout=httpx.Timeout(30, read=300), **kwargs)
    except openai.BadRequestError as exc:
        if is_reasoning_summary_rejected(exc):
            return client.responses.create(timeout=httpx.Timeout(30, read=300), **drop_reasoning_summary(kwargs))
        if _requires_stream(exc):
            return _stream_response(client, kwargs)
        raise
    except Exception as exc:
        _raise_clear_codex_error(exc)
        raise


def codex_send_message_to_model(
    messages,
    model: str | None = None,
    response_type: str = "text",
    response_schema=None,
    tools: list[ToolDefinition] | None = None,
    deepthought: bool = False,
    tracer: dict | None = None,
    resolver: CodexAuthResolver | None = None,
    client: openai.OpenAI | None = None,
) -> ResponseWithThought:
    model = model or get_codex_model()
    tracer = tracer if tracer is not None else {}
    kwargs = build_codex_response_kwargs(
        messages=messages,
        model=model,
        response_type=response_type,
        response_schema=response_schema,
        tools=tools,
        deepthought=deepthought,
    )
    client = client or _codex_client(resolver)
    response = _create_response(client, kwargs)
    result = normalize_codex_response(response)
    tracer["chat_model"] = codex_chat_model_label(model)
    if is_promptrace_enabled():
        commit_conversation_trace(messages, result.text, tracer)
    return result


async def converse_codex(
    messages: list[ChatMessage],
    model: str | None = None,
    deepthought: bool = False,
    tracer: dict | None = None,
) -> AsyncGenerator[ResponseWithThought, None]:
    result = await asyncio.to_thread(
        codex_send_message_to_model,
        messages=messages,
        model=model,
        deepthought=deepthought,
        tracer=tracer,
    )
    yield result
