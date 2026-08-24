"""DeepSeek V4 Chat Completions adapter behind the sole ModelGateway port.

The shared HTTP/secret/cancellation machinery lives in ``openai_responses``;
this module supplies only the DeepSeek request dialect and strict data-only SSE
decoder.  Raw ``reasoning_content`` is deliberately discarded at the provider
boundary and can never become a Harness event.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from offeragent_harness.models import (
    ModelError,
    ModelEventKind,
    ModelFinishReason,
    ModelOutputMode,
    ModelRequest,
    ModelUsage,
    thaw_json,
)
from offeragent_harness.ports.network_audit import NetworkAuditSink
from offeragent_harness.ports.secrets import SecretHandle, SecretResolver
from offeragent_harness.ports.system import Clock

from .network_audit import ModelNetworkAuditor
from .openai_responses import (
    ModelEndpointPolicy,
    ModelProviderConfigurationError,
    ModelProviderProtocolError,
    OpenAIResponsesConfig,
    OpenAIResponsesGateway,
    _encode_message,
    _SemanticEvent,
)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_PROVIDER_ID = "deepseek"
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
_FINISH_REASONS = frozenset({"stop", "length", "content_filter", "tool_calls", "insufficient_system_resource"})


class DeepSeekChatGateway(OpenAIResponsesGateway):
    """DeepSeek Chat Completions using the Harness-owned streaming kernel."""

    def _encode_payload(self, request: ModelRequest) -> bytes:
        return _encode_deepseek_request(request, self._config)

    def _make_accumulator(self, request: ModelRequest) -> _DeepSeekAccumulator:
        return _DeepSeekAccumulator(request, self._config)


def build_deepseek_gateway(
    *,
    secret_scope_id: str,
    credential_handle: SecretHandle | None,
    secrets: SecretResolver,
    endpoint_policy: ModelEndpointPolicy,
    transport: httpx.BaseTransport | None = None,
    network_audit: NetworkAuditSink | None = None,
    clock: Clock | None = None,
) -> DeepSeekChatGateway:
    """Build the fixed official DeepSeek endpoint with an opaque SecretHandle."""

    if (network_audit is None) != (clock is None):
        raise ModelProviderConfigurationError("model network audit requires both a sink and clock")
    config = OpenAIResponsesConfig(
        provider_id=DEEPSEEK_PROVIDER_ID,
        base_url=DEEPSEEK_BASE_URL,
        endpoint_path="chat/completions",
        secret_scope_id=secret_scope_id,
        credential_handle=credential_handle,
        require_credential=True,
        organization_id=None,
        project_id=None,
        service_tier=None,
    )
    auditor = (
        None
        if network_audit is None or clock is None
        else ModelNetworkAuditor(
            workspace_id=secret_scope_id,
            provider_id=DEEPSEEK_PROVIDER_ID,
            endpoint=config.endpoint,
            sink=network_audit,
            clock=clock,
        )
    )
    return DeepSeekChatGateway(
        config=config,
        secrets=secrets,
        endpoint_policy=endpoint_policy,
        transport=transport,
        network_auditor=auditor,
    )


class _DeepSeekAccumulator:
    def __init__(self, request: ModelRequest, config: OpenAIResponsesConfig) -> None:
        self.request = request
        self.config = config
        self.terminal = False
        self._response_id: str | None = None
        self._response_model: str | None = None
        self._finish_reason: str | None = None
        self._usage: ModelUsage | None = None
        self._output: list[str] = []
        self._output_bytes = 0

    def accept(self, event_name: str | None, data: bytes) -> tuple[_SemanticEvent, ...]:
        if self.terminal:
            raise ModelProviderProtocolError("DeepSeek emitted data after stream completion")
        if event_name is not None:
            raise ModelProviderProtocolError("DeepSeek stream must use data-only SSE events")
        if data == b"[DONE]":
            return self._complete()
        value = _strict_object(data, "DeepSeek SSE data")
        if value.get("object") != "chat.completion.chunk":
            raise ModelProviderProtocolError("DeepSeek stream emitted an unsupported object type")
        self._accept_identity(value)
        self._accept_usage(value.get("usage"))

        choices = value.get("choices")
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
            raise ModelProviderProtocolError("DeepSeek choices must be an array")
        if len(choices) > 1:
            raise ModelProviderProtocolError("DeepSeek returned multiple choices")
        if not choices:
            if value.get("usage") is None:
                raise ModelProviderProtocolError("DeepSeek emitted an empty non-usage chunk")
            return ()

        choice = choices[0]
        if not isinstance(choice, Mapping) or any(not isinstance(key, str) for key in choice):
            raise ModelProviderProtocolError("DeepSeek choice must be an object")
        if type(choice.get("index")) is not int or choice.get("index") != 0:
            raise ModelProviderProtocolError("DeepSeek choice index must be zero")
        delta = choice.get("delta")
        if not isinstance(delta, Mapping) or any(not isinstance(key, str) for key in delta):
            raise ModelProviderProtocolError("DeepSeek choice delta must be an object")
        if delta.get("tool_calls") not in (None, []) or delta.get("function_call") is not None:
            raise ModelProviderProtocolError(
                "DeepSeek attempted an unrequested remote tool call",
                reason="unrequested_remote_tool_call",
            )
        role = delta.get("role")
        if role not in {None, "assistant"}:
            raise ModelProviderProtocolError("DeepSeek delta role is invalid")
        reasoning = delta.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ModelProviderProtocolError("DeepSeek reasoning content must be text")

        finish = choice.get("finish_reason")
        if finish is not None:
            if not isinstance(finish, str) or finish not in _FINISH_REASONS:
                raise ModelProviderProtocolError("DeepSeek finish reason is unsupported")
            if self._finish_reason is not None:
                raise ModelProviderProtocolError("DeepSeek emitted duplicate finish reasons")
            if finish == "tool_calls":
                raise ModelProviderProtocolError(
                    "DeepSeek attempted an unrequested remote tool call",
                    reason="unrequested_remote_tool_call",
                )
            self._finish_reason = finish

        content = delta.get("content")
        if content is None or content == "":
            return ()
        if not isinstance(content, str):
            raise ModelProviderProtocolError("DeepSeek content delta must be text")
        self._output_bytes += len(content.encode("utf-8"))
        if self._output_bytes > self.config.max_output_bytes:
            raise ModelProviderProtocolError("DeepSeek output exceeds its byte limit")
        self._output.append(content)
        if self.request.output_mode is ModelOutputMode.TEXT:
            return (_SemanticEvent(ModelEventKind.TEXT_DELTA, text=content),)
        return ()

    def _accept_identity(self, value: Mapping[str, Any]) -> None:
        response_id = value.get("id")
        response_model = value.get("model")
        if (
            not isinstance(response_id, str)
            or not response_id
            or len(response_id) > 512
            or not isinstance(response_model, str)
            or not response_model
            or len(response_model) > 256
        ):
            raise ModelProviderProtocolError("DeepSeek stream identity is invalid")
        if self._response_id is None:
            self._response_id = response_id
            self._response_model = response_model
        elif response_id != self._response_id or response_model != self._response_model:
            raise ModelProviderProtocolError("DeepSeek stream identity changed mid-response")

    def _accept_usage(self, raw: object) -> None:
        if raw is None:
            return
        if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
            raise ModelProviderProtocolError("DeepSeek usage must be an object")
        prompt_tokens = _nonnegative_int(raw, "prompt_tokens")
        completion_tokens = _nonnegative_int(raw, "completion_tokens")
        cached = _optional_nonnegative_int(raw, "prompt_cache_hit_tokens")
        details = raw.get("completion_tokens_details")
        if details is None:
            reasoning = 0
        elif isinstance(details, Mapping) and all(isinstance(key, str) for key in details):
            reasoning = _optional_nonnegative_int(details, "reasoning_tokens")
        else:
            raise ModelProviderProtocolError("DeepSeek completion token details must be an object")
        total = raw.get("total_tokens")
        if total is not None and (type(total) is not int or total != prompt_tokens + completion_tokens):
            raise ModelProviderProtocolError("DeepSeek total token usage is inconsistent")
        try:
            usage = ModelUsage(prompt_tokens, completion_tokens, cached, reasoning, cost=None, currency=None)
        except ValueError as error:
            raise ModelProviderProtocolError("DeepSeek usage is internally inconsistent") from error
        if self._usage is not None and usage != self._usage:
            raise ModelProviderProtocolError("DeepSeek emitted inconsistent duplicate usage")
        self._usage = usage

    def _complete(self) -> tuple[_SemanticEvent, ...]:
        if self._response_id is None or self._finish_reason is None or self._usage is None:
            raise ModelProviderProtocolError(
                "DeepSeek stream ended without identity, finish reason, or usage",
                reason="terminal_metadata_missing",
            )
        self.terminal = True
        finish = self._finish_reason
        events: list[_SemanticEvent] = []
        if finish == "insufficient_system_resource":
            events.append(_SemanticEvent(ModelEventKind.USAGE, usage=self._usage))
            events.append(
                _SemanticEvent(
                    ModelEventKind.ERROR,
                    error=ModelError(
                        "provider_unavailable",
                        "DeepSeek reported insufficient system resources",
                        True,
                        False,
                        {"providerId": DEEPSEEK_PROVIDER_ID},
                    ),
                )
            )
            return tuple(events)
        if finish == "stop" and self.request.output_mode is ModelOutputMode.JSON:
            events.append(self._structured_output())
        events.append(_SemanticEvent(ModelEventKind.USAGE, usage=self._usage))
        finish_reason = {
            "stop": ModelFinishReason.STOP,
            "length": ModelFinishReason.LENGTH,
            "content_filter": ModelFinishReason.CONTENT_FILTER,
        }.get(finish)
        if finish_reason is None:
            raise ModelProviderProtocolError("DeepSeek stream ended with an invalid finish reason")
        events.append(_SemanticEvent(ModelEventKind.COMPLETED, finish_reason=finish_reason))
        return tuple(events)

    def _structured_output(self) -> _SemanticEvent:
        text = "".join(self._output)
        if not text:
            raise ModelProviderProtocolError(
                "DeepSeek returned empty structured output",
                reason="structured_output_empty",
            )
        try:
            value = json.loads(text, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as error:
            raise ModelProviderProtocolError(
                "DeepSeek structured output is not strict JSON",
                reason="structured_output_invalid_json",
            ) from error
        if not isinstance(value, dict):
            raise ModelProviderProtocolError(
                "DeepSeek structured output must be a JSON object",
                reason="structured_output_not_object",
            )
        return _SemanticEvent(ModelEventKind.STRUCTURED_OUTPUT, data=value)


def _encode_deepseek_request(request: ModelRequest, config: OpenAIResponsesConfig) -> bytes:
    if _MODEL_ID.fullmatch(request.model) is None:
        raise ModelProviderConfigurationError("model ID contains unsupported characters")
    if request.seed is not None:
        raise ModelProviderConfigurationError("DeepSeek provider does not support deterministic seed")
    effort = request.reasoning_effort
    if effort is not None and effort not in _REASONING_EFFORTS:
        raise ModelProviderConfigurationError("reasoning effort is unsupported")
    thinking_enabled = effort not in {None, "none"}

    messages = [_encode_chat_message(message) for message in request.messages]
    if request.output_mode is ModelOutputMode.JSON:
        assert request.output_schema is not None
        schema = json.dumps(
            thaw_json(request.output_schema),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        messages.insert(
            0,
            {
                "role": "system",
                "content": (
                    "Return JSON only: exactly one JSON object, with no Markdown or commentary. "
                    "The JSON object must satisfy this canonical JSON Schema:\n" + schema
                ),
            },
        )

    body: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "thinking": {"type": "enabled" if thinking_enabled else "disabled"},
    }
    if request.max_output_tokens is not None:
        body["max_tokens"] = request.max_output_tokens
    if thinking_enabled:
        body["reasoning_effort"] = "max" if effort in {"xhigh", "max"} else "high"
    elif request.temperature is not None:
        body["temperature"] = request.temperature
    if request.output_mode is ModelOutputMode.JSON:
        body["response_format"] = {"type": "json_object"}

    encoded = json.dumps(
        body,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > config.max_request_bytes:
        raise ModelProviderConfigurationError("model request exceeds its byte limit")
    return encoded


def _encode_chat_message(message: Any) -> dict[str, str]:
    encoded = _encode_message(message)
    role = encoded.get("role")
    blocks = encoded.get("content")
    if not isinstance(role, str) or not isinstance(blocks, Sequence):
        raise ModelProviderConfigurationError("model message could not be encoded")
    texts: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping) or not isinstance(block.get("text"), str):
            raise ModelProviderConfigurationError("model message content could not be encoded")
        texts.append(str(block["text"]))
    return {"role": role, "content": "\n\n".join(texts)}


def _strict_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8", errors="strict"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ModelProviderProtocolError(f"{label} is not strict JSON") from error
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ModelProviderProtocolError(f"{label} must be a JSON object")
    return value


def _nonnegative_int(value: Mapping[str, Any], field: str) -> int:
    result = value.get(field)
    if type(result) is not int or result < 0:
        raise ModelProviderProtocolError(f"DeepSeek usage field {field!r} is invalid")
    return result


def _optional_nonnegative_int(value: Mapping[str, Any], field: str) -> int:
    result = value.get(field, 0)
    if type(result) is not int or result < 0:
        raise ModelProviderProtocolError(f"DeepSeek usage field {field!r} is invalid")
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


__all__ = [
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_PROVIDER_ID",
    "DeepSeekChatGateway",
    "build_deepseek_gateway",
]
