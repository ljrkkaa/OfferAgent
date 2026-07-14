from __future__ import annotations

import json
from collections.abc import Callable
from typing import TypeVar

import httpx
import pytest
from pydantic import ValidationError

from offeragent_harness.config import ModelProvider, ModelSettings, ModelWireApi
from offeragent_harness.models import (
    ModelContentBlock,
    ModelEvent,
    ModelEventKind,
    ModelFinishReason,
    ModelMessage,
    ModelOutputMode,
    ModelPurpose,
    ModelRequest,
    ModelRole,
    TraceContext,
)
from offeragent_harness.ports import SecretHandle, SecretKind
from offeragent_harness.providers import (
    DEEPSEEK_BASE_URL,
    DeepSeekChatGateway,
    OpenAIResponsesConfig,
    StaticModelEndpointPolicy,
    build_deepseek_gateway,
    compose_model_gateway,
)
from offeragent_harness.testing.cancellation import ManualCancellationToken

T = TypeVar("T")
HANDLE = SecretHandle("secret:v1:0123456789abcdef0123456789abcdef")
SCOPE = "workspace:wsi_test"
FIXTURE_SECRET = b"fixture-deepseek-secret"
ENDPOINT = f"{DEEPSEEK_BASE_URL}/chat/completions"


class _SecretResolver:
    def __init__(self, secret: bytes = FIXTURE_SECRET) -> None:
        self.buffer = bytearray(secret)
        self.calls: list[tuple[SecretHandle, str, SecretKind, str]] = []

    def consume(
        self,
        handle: SecretHandle,
        *,
        scope_id: str,
        expected_kind: SecretKind,
        expected_provider_id: str,
        consumer: Callable[[memoryview], T],
    ) -> T:
        self.calls.append((handle, scope_id, expected_kind, expected_provider_id))
        view = memoryview(self.buffer)
        try:
            return consumer(view)
        finally:
            view.release()
            for index in range(len(self.buffer)):
                self.buffer[index] = 0


def _request(*, output_mode: ModelOutputMode = ModelOutputMode.TEXT) -> ModelRequest:
    schema = None
    if output_mode is ModelOutputMode.JSON:
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string", "minLength": 1}},
            "required": ["answer"],
            "additionalProperties": False,
        }
    return ModelRequest(
        request_id="req_deepseek_test",
        model="deepseek-v4-flash",
        purpose=ModelPurpose.PLANNING if output_mode is ModelOutputMode.JSON else ModelPurpose.COMPOSING,
        messages=(
            ModelMessage(ModelRole.SYSTEM, (ModelContentBlock.text("system boundary"),)),
            ModelMessage(ModelRole.USER, (ModelContentBlock.text("user prompt"),)),
            ModelMessage(
                ModelRole.TOOL,
                (ModelContentBlock.text("tool output"),),
                name="workspace.read",
            ),
        ),
        output_mode=output_mode,
        output_schema=schema,
        max_output_tokens=128,
        reasoning_effort="medium",
        temperature=0.7,
        seed=None,
        trace_context=TraceContext("trace_deepseek_test"),
        metadata={"workspaceId": "must-not-leave-process"},
    )


def _gateway(
    transport: httpx.BaseTransport,
    *,
    secrets: _SecretResolver | None = None,
) -> DeepSeekChatGateway:
    config = OpenAIResponsesConfig(
        provider_id="deepseek",
        base_url=DEEPSEEK_BASE_URL,
        endpoint_path="chat/completions",
        secret_scope_id=SCOPE,
        credential_handle=HANDLE,
        service_tier=None,
        max_retries=0,
    )
    return DeepSeekChatGateway(
        config=config,
        secrets=secrets or _SecretResolver(),
        endpoint_policy=StaticModelEndpointPolicy(frozenset({ENDPOINT})),
        transport=transport,
    )


async def _collect(gateway: DeepSeekChatGateway, request: ModelRequest) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in gateway.stream(request, ManualCancellationToken())])


def _chunk(
    choices: list[dict[str, object]],
    *,
    usage: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": "chatcmpl_fixture",
        "object": "chat.completion.chunk",
        "model": "deepseek-v4-flash",
        "choices": choices,
        "usage": usage,
    }


def _sse(*chunks: dict[str, object], done: bool = True) -> bytes:
    content = b"".join(
        b"data: " + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n" for chunk in chunks
    )
    if done:
        content += b"data: [DONE]\n\n"
    return content


def _usage() -> dict[str, object]:
    return {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "prompt_cache_hit_tokens": 3,
        "completion_tokens_details": {"reasoning_tokens": 2},
    }


def _successful_sse(text: str, *, reasoning: str = "hidden private reasoning") -> bytes:
    return _sse(
        _chunk([{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]),
        _chunk(
            [
                {
                    "index": 0,
                    "delta": {"reasoning_content": reasoning, "content": text},
                    "finish_reason": None,
                }
            ]
        ),
        _chunk([{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        _chunk([], usage=_usage()),
    )


@pytest.mark.asyncio
async def test_fixed_chat_endpoint_secret_binding_and_v4_thinking_request() -> None:
    captured: dict[str, object] = {}
    secrets = _SecretResolver()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_successful_sse("hello"),
        )

    gateway = build_deepseek_gateway(
        secret_scope_id=SCOPE,
        credential_handle=HANDLE,
        secrets=secrets,
        endpoint_policy=StaticModelEndpointPolicy(frozenset({ENDPOINT})),
        transport=httpx.MockTransport(handler),
    )
    events = await _collect(gateway, _request())

    assert captured["method"] == "POST"
    assert captured["url"] == ENDPOINT
    assert captured["authorization"] == f"Bearer {FIXTURE_SECRET.decode()}"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "deepseek-v4-flash"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    assert body["max_tokens"] == 128
    assert "temperature" not in body
    assert "tools" not in body
    assert "must-not-leave-process" not in json.dumps(body)
    assert secrets.calls == [(HANDLE, SCOPE, SecretKind.MODEL_PROVIDER, "deepseek")]
    assert set(secrets.buffer) == {0}
    assert events[-1].kind is ModelEventKind.COMPLETED


@pytest.mark.asyncio
async def test_text_sse_discards_reasoning_and_requires_usage_finish_and_done() -> None:
    hidden = "never expose this chain of thought"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            content=_successful_sse("visible answer", reasoning=hidden),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]
    assert "".join(event.text or "" for event in events) == "visible answer"
    assert hidden not in repr(events)
    assert all(event.kind is not ModelEventKind.REASONING_SUMMARY for event in events)
    usage = events[-2].usage
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens) == (11, 7)
    assert (usage.cached_input_tokens, usage.reasoning_tokens) == (3, 2)
    assert events[-1].finish_reason is ModelFinishReason.STOP


@pytest.mark.asyncio
async def test_json_mode_injects_canonical_schema_prompt_and_validates_locally() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["body"] = body
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_successful_sse('{"answer":"local"}'),
        )

    events = await _collect(
        _gateway(httpx.MockTransport(handler)),
        _request(output_mode=ModelOutputMode.JSON),
    )

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["response_format"] == {"type": "json_object"}
    messages = body["messages"]
    assert isinstance(messages, list)
    schema_instruction = messages[0]
    assert schema_instruction["role"] == "system"
    instruction = schema_instruction["content"]
    assert isinstance(instruction, str)
    assert "Return JSON only" in instruction
    assert '"additionalProperties":false' in instruction
    assert '"minLength":1' in instruction
    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.STRUCTURED_OUTPUT,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]
    assert events[1].data == {"answer": "local"}


@pytest.mark.asyncio
async def test_json_mode_rejects_schema_invalid_provider_output_without_echo() -> None:
    private_output = '{"answer":"","unexpected":"private payload"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_successful_sse(private_output),
        )

    events = await _collect(
        _gateway(httpx.MockTransport(handler)),
        _request(output_mode=ModelOutputMode.JSON),
    )

    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]
    assert events[-1].error is not None
    assert events[-1].error.code == "provider_protocol_error"
    assert "private payload" not in repr(events[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_usage", "tool_calls", "missing_done"])
async def test_incomplete_or_remote_tool_streams_fail_closed(failure: str) -> None:
    if failure == "tool_calls":
        content = _sse(
            _chunk(
                [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"id": "provider_tool"}]},
                        "finish_reason": None,
                    }
                ]
            ),
            _chunk([{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]),
            _chunk([], usage=_usage()),
        )
    elif failure == "missing_done":
        content = _sse(
            _chunk([{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]),
            _chunk([{"index": 0, "delta": {}, "finish_reason": "stop"}]),
            _chunk([], usage=_usage()),
            done=False,
        )
    else:
        content = _sse(
            _chunk([{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]),
            _chunk([{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=content,
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None
    assert events[-1].error.code == "provider_protocol_error"
    assert all(event.kind is not ModelEventKind.COMPLETED for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "auth_required", False),
        (402, "insufficient_balance", False),
        (429, "provider_rate_limited", True),
        (503, "provider_unavailable", True),
    ],
)
async def test_deepseek_http_errors_are_typed_and_redacted(
    status: int,
    code: str,
    retryable: bool,
) -> None:
    reflected = "fixture secret and private prompt"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": reflected}})

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]
    error = events[-1].error
    assert error is not None
    assert error.code == code
    assert error.retryable is retryable
    assert error.details == {"providerId": "deepseek", "httpStatus": status}
    assert reflected not in repr(events[-1])


def test_deepseek_model_settings_accept_only_fixed_chat_completions_boundary() -> None:
    settings = ModelSettings(
        provider=ModelProvider.DEEPSEEK,
        wire_api=ModelWireApi.CHAT_COMPLETIONS,
        model="deepseek-v4-flash",
        credential_handle=HANDLE.opaque_id,
    )

    assert settings.provider is ModelProvider.DEEPSEEK
    assert settings.wire_api is ModelWireApi.CHAT_COMPLETIONS
    assert settings.base_url == ""

    invalid_overrides: list[dict[str, object]] = [
        {"wire_api": ModelWireApi.RESPONSES},
        {"base_url": DEEPSEEK_BASE_URL, "allow_remote_https": True},
        {"organization_id": "org_override"},
        {"project_id": "project_override"},
        {"allow_remote_https": True},
        {"proxy_url": "http://127.0.0.1:8080"},
    ]
    for override in invalid_overrides:
        with pytest.raises(ValidationError):
            ModelSettings.model_validate(
                {
                    "provider": ModelProvider.DEEPSEEK,
                    "wire_api": ModelWireApi.CHAT_COMPLETIONS,
                    "model": "deepseek-v4-flash",
                    "credential_handle": HANDLE.opaque_id,
                    **override,
                }
            )

    with pytest.raises(ValidationError, match="only valid for the DeepSeek provider"):
        ModelSettings(
            provider=ModelProvider.OPENAI,
            wire_api=ModelWireApi.CHAT_COMPLETIONS,
            model="gpt-test",
            credential_handle=HANDLE.opaque_id,
        )


def test_composition_builds_deepseek_gateway_and_requires_secret_handle() -> None:
    gateway = compose_model_gateway(
        ModelSettings(
            provider=ModelProvider.DEEPSEEK,
            wire_api=ModelWireApi.CHAT_COMPLETIONS,
            model="deepseek-v4-flash",
            credential_handle=HANDLE.opaque_id,
        ),
        secret_scope_id=SCOPE,
        secrets=_SecretResolver(),
        network_enabled=True,
        deepseek_transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_successful_sse("unused"),
            )
        ),
    )

    assert isinstance(gateway, DeepSeekChatGateway)

    with pytest.raises(ValueError, match="opaque credential handle"):
        compose_model_gateway(
            ModelSettings(
                provider=ModelProvider.DEEPSEEK,
                wire_api=ModelWireApi.CHAT_COMPLETIONS,
                model="deepseek-v4-flash",
            ),
            secret_scope_id=SCOPE,
            secrets=_SecretResolver(),
            network_enabled=True,
        )
