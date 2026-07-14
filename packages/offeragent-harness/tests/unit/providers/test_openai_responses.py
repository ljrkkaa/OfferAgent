from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import TypeVar

import httpx
import pytest

from offeragent_harness.config import ModelProvider, ModelSettings
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
    CodexResponsesProvider,
    LocalModelProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    OpenAIResponsesConfig,
    OpenAIResponsesGateway,
    ResponsesProviderKind,
    ResponsesProviderSelection,
    StaticModelEndpointPolicy,
    build_responses_provider,
    compose_model_gateway,
    model_secret_provider_id,
)
from offeragent_harness.testing.cancellation import ManualCancellationToken
from offeragent_harness.testing.errors import FakeRunCancelled

T = TypeVar("T")


class _SecretResolver:
    def __init__(self, secret: bytes = b"test-provider-secret") -> None:
        self.buffer = bytearray(secret)
        self.calls = 0

    def consume(
        self,
        handle: SecretHandle,
        *,
        scope_id: str,
        expected_kind: SecretKind,
        expected_provider_id: str,
        consumer: Callable[[memoryview], T],
    ) -> T:
        assert handle == SecretHandle("secret:v1:0123456789abcdef0123456789abcdef")
        assert scope_id == "workspace:wsi_test"
        assert expected_kind is SecretKind.MODEL_PROVIDER
        assert expected_provider_id == "openai"
        self.calls += 1
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
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
    return ModelRequest(
        request_id="req_test_1",
        model="gpt-test",
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
        temperature=0.0,
        seed=None,
        trace_context=TraceContext("trace_test"),
        metadata={"workspaceId": "must-not-leave-process"},
    )


def _config() -> OpenAIResponsesConfig:
    return OpenAIResponsesConfig(
        provider_id="openai",
        base_url="https://api.openai.example/v1",
        secret_scope_id="workspace:wsi_test",
        credential_handle=SecretHandle("secret:v1:0123456789abcdef0123456789abcdef"),
    )


def _gateway(
    handler: httpx.BaseTransport,
    secrets: _SecretResolver | None = None,
    config: OpenAIResponsesConfig | None = None,
) -> OpenAIResponsesGateway:
    config = config or _config()
    return OpenAIResponsesGateway(
        config=config,
        secrets=secrets or _SecretResolver(),
        endpoint_policy=StaticModelEndpointPolicy(frozenset({config.endpoint})),
        transport=handler,
    )


def _sse(*events: dict[str, object]) -> bytes:
    parts: list[bytes] = []
    for event in events:
        name = str(event["type"])
        parts.append(f"event: {name}\n".encode())
        parts.append(b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n")
    return b"".join(parts)


def _completed(text: str, *, sequence: int = 3) -> dict[str, object]:
    return {
        "type": "response.completed",
        "sequence_number": sequence,
        "response": {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": {
                "input_tokens": 9,
                "output_tokens": 4,
                "input_tokens_details": {"cached_tokens": 2},
                "output_tokens_details": {"reasoning_tokens": 1},
            },
        },
    }


async def _collect(gateway: OpenAIResponsesGateway, request: ModelRequest) -> tuple[ModelEvent, ...]:
    return tuple([item async for item in gateway.stream(request, ManualCancellationToken())])


@pytest.mark.asyncio
async def test_text_stream_is_real_typed_sse_and_request_exposes_no_runtime_authority() -> None:
    captured: dict[str, object] = {}
    secrets = _SecretResolver()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        body = json.loads(request.content)
        captured["body"] = body
        content = _sse(
            {"type": "response.created", "sequence_number": 0, "response": {"id": "resp_1"}},
            {
                "type": "response.output_text.delta",
                "sequence_number": 1,
                "output_index": 0,
                "content_index": 0,
                "delta": "hello ",
            },
            {
                "type": "response.output_text.delta",
                "sequence_number": 2,
                "output_index": 0,
                "content_index": 0,
                "delta": "world",
            },
            {
                "type": "response.output_text.done",
                "sequence_number": 3,
                "output_index": 0,
                "content_index": 0,
                "text": "hello world",
            },
            _completed("hello world", sequence=4),
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    events = await _collect(_gateway(httpx.MockTransport(handler), secrets), _request())

    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]
    assert "".join(event.text or "" for event in events) == "hello world"
    assert events[-2].usage is not None and events[-2].usage.cached_input_tokens == 2
    assert events[-1].finish_reason is ModelFinishReason.STOP
    assert captured["authorization"] == "Bearer test-provider-secret"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["store"] is False
    assert body["tools"] == []
    assert body["parallel_tool_calls"] is False
    encoded = json.dumps(body)
    assert "must-not-leave-process" not in encoded
    assert "test-provider-secret" not in encoded
    assert "Local tool result workspace.read" in encoded
    assert secrets.calls == 1 and set(secrets.buffer) == {0}


@pytest.mark.asyncio
async def test_structured_output_is_parsed_and_schema_validated_before_emission() -> None:
    structured = '{"answer":"local"}'

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["text"]["format"] == {
            "type": "json_schema",
            "name": "offeragent_planning",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": structured,
                },
                {
                    "type": "response.output_text.done",
                    "sequence_number": 2,
                    "output_index": 0,
                    "content_index": 0,
                    "text": structured,
                },
                _completed(structured),
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request(output_mode=ModelOutputMode.JSON))

    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.STRUCTURED_OUTPUT,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]
    assert events[1].data == {"answer": "local"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,retryable", [(401, False), (429, True), (503, True)])
async def test_http_errors_are_redacted_and_classified(status: int, retryable: bool) -> None:
    secret = "must-never-appear"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": f"echo {secret}"}})

    resolver = _SecretResolver(secret.encode())
    events = await _collect(_gateway(httpx.MockTransport(handler), resolver), _request())

    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]
    assert events[-1].error is not None and events[-1].error.retryable is retryable
    assert secret not in repr(events[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_fields",
    (
        {"code": "context_length_exceeded"},
        {"reason": "prompt_too_long"},
    ),
)
async def test_http_context_overflow_uses_only_bounded_structured_discriminators(
    error_fields: dict[str, str],
) -> None:
    secret = "private prompt must not be reflected"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {**error_fields, "message": secret}})

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]
    assert events[-1].error is not None and events[-1].error.code == "context_overflow"
    assert events[-1].error.retryable is False
    assert secret not in repr(events[-1])


@pytest.mark.asyncio
async def test_sse_context_overflow_is_typed_and_body_message_is_not_exposed() -> None:
    secret = "vault body must not be reflected"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.failed",
                    "sequence_number": 1,
                    "response": {
                        "status": "failed",
                        "error": {"code": "context_window_exceeded", "message": secret},
                    },
                },
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]
    assert events[-1].error is not None and events[-1].error.code == "context_overflow"
    assert secret not in repr(events[-1])


@pytest.mark.asyncio
async def test_unstructured_overflow_words_never_trigger_typed_context_overflow() -> None:
    secret = "context_length_exceeded plus private prompt"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {
                    "type": "error",
                    "sequence_number": 0,
                    "error": {"message": secret},
                }
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert events[-1].error is not None and events[-1].error.code == "provider_response_failed"
    assert secret not in repr(events[-1])


@pytest.mark.asyncio
async def test_invalid_structured_output_fails_closed_without_echoing_output() -> None:
    invalid = '{"unexpected":"private payload"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": invalid,
                },
                {
                    "type": "response.output_text.done",
                    "sequence_number": 2,
                    "output_index": 0,
                    "content_index": 0,
                    "text": invalid,
                },
                _completed(invalid),
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request(output_mode=ModelOutputMode.JSON))

    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]
    assert "private payload" not in repr(events[-1])


@pytest.mark.asyncio
async def test_redirect_is_never_followed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"location": "https://attacker.example/steal"})

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert calls == 1
    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]


@pytest.mark.asyncio
async def test_retryable_failure_retries_before_output_with_one_secret_consume() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": {"code": "server_error"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "recovered",
                },
                {
                    "type": "response.output_text.done",
                    "sequence_number": 2,
                    "output_index": 0,
                    "content_index": 0,
                    "text": "recovered",
                },
                _completed("recovered"),
            ),
        )

    resolver = _SecretResolver()
    base = _config()
    config = OpenAIResponsesConfig(
        provider_id=base.provider_id,
        base_url=base.base_url,
        secret_scope_id=base.secret_scope_id,
        credential_handle=base.credential_handle,
        max_retries=2,
        retry_base_seconds=0,
        retry_max_seconds=0,
        retry_jitter_ratio=0,
    )
    events = await _collect(_gateway(httpx.MockTransport(handler), resolver, config), _request())

    assert calls == 2
    assert resolver.calls == 1
    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]


class _PartialThenFailure(httpx.SyncByteStream):
    def __iter__(self):  # type: ignore[no-untyped-def]
        yield _sse(
            {"type": "response.created", "sequence_number": 0, "response": {}},
            {
                "type": "response.output_text.delta",
                "sequence_number": 1,
                "output_index": 0,
                "content_index": 0,
                "delta": "partial",
            },
        )
        raise httpx.ReadError("connection lost after output")


@pytest.mark.asyncio
async def test_transport_failure_after_output_is_never_retried_or_duplicated() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_PartialThenFailure())

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert calls == 1
    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.ERROR,
    ]
    assert events[-1].error is not None and events[-1].error.code == "provider_unreachable"


class _BlockingStream(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):  # type: ignore[no-untyped-def]
        self.started.set()
        self.closed.wait(timeout=10)
        return
        yield b""  # pragma: no cover

    def close(self) -> None:
        self.closed.set()


@pytest.mark.asyncio
async def test_cancellation_closes_blocked_response_and_joins_secret_consumer() -> None:
    blocking = _BlockingStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=blocking)

    resolver = _SecretResolver()
    gateway = _gateway(httpx.MockTransport(handler), resolver)
    token = ManualCancellationToken()
    stream = gateway.stream(_request(), token)
    first = await anext(stream)
    assert first.kind is ModelEventKind.STARTED
    pending = __import__("asyncio").create_task(anext(stream))
    await __import__("asyncio").to_thread(blocking.started.wait, 2)
    token.cancel()

    with pytest.raises(FakeRunCancelled):
        await pending
    assert blocking.closed.is_set()
    assert resolver.calls == 1 and set(resolver.buffer) == {0}


def test_endpoint_policy_allows_https_and_loopback_only() -> None:
    config = _config()
    policy = StaticModelEndpointPolicy(frozenset({config.endpoint}))
    policy.authorize(provider_id="openai", endpoint="https://api.openai.example/v1/responses")
    with pytest.raises(ValueError):
        policy.authorize(provider_id="openai", endpoint="https://attacker.example/v1/responses")
    with pytest.raises(ValueError):
        OpenAIResponsesConfig(
            provider_id="local",
            base_url="http://192.168.1.10:11434/v1",
            secret_scope_id="workspace:wsi_test",
            credential_handle=None,
            require_credential=False,
        )


def test_typed_factory_separates_official_codex_openai_and_loopback_local() -> None:
    resolver = _SecretResolver()
    handle = SecretHandle("secret:v1:0123456789abcdef0123456789abcdef")
    official_policy = StaticModelEndpointPolicy(frozenset({"https://api.openai.com/v1/responses"}))
    codex = build_responses_provider(
        ResponsesProviderSelection(ResponsesProviderKind.CODEX, "workspace:wsi_test", handle),
        secrets=resolver,
        endpoint_policy=official_policy,
    )
    openai = build_responses_provider(
        ResponsesProviderSelection(ResponsesProviderKind.OPENAI, "workspace:wsi_test", handle),
        secrets=resolver,
        endpoint_policy=official_policy,
    )
    local = build_responses_provider(
        ResponsesProviderSelection(
            ResponsesProviderKind.LOCAL,
            "workspace:wsi_test",
            None,
            base_url="http://127.0.0.1:11434/v1",
        ),
        secrets=resolver,
        endpoint_policy=StaticModelEndpointPolicy(frozenset({"http://127.0.0.1:11434/v1/responses"})),
    )

    assert isinstance(codex, CodexResponsesProvider)
    assert isinstance(openai, OpenAIProvider)
    assert isinstance(local, LocalModelProvider)
    with pytest.raises(ValueError, match="literal loopback"):
        build_responses_provider(
            ResponsesProviderSelection(
                ResponsesProviderKind.LOCAL,
                "workspace:wsi_test",
                None,
                base_url="https://remote.example/v1",
            ),
            secrets=resolver,
            endpoint_policy=StaticModelEndpointPolicy(frozenset({"https://remote.example/v1/responses"})),
        )
    with pytest.raises(ValueError, match="cannot be overridden"):
        build_responses_provider(
            ResponsesProviderSelection(
                ResponsesProviderKind.OPENAI,
                "workspace:wsi_test",
                handle,
                base_url="https://proxy.example/v1",
            ),
            secrets=resolver,
            endpoint_policy=official_policy,
        )


def test_run_snapshot_composition_selects_compatible_responses_without_runtime_authority() -> None:
    resolver = _SecretResolver()
    official = compose_model_gateway(
        ModelSettings(
            provider=ModelProvider.OPENAI,
            model="gpt-test",
            credential_handle="secret:v1:0123456789abcdef0123456789abcdef",
        ),
        secret_scope_id="workspace:wsi_test",
        secrets=resolver,
        network_enabled=True,
    )
    compatible = compose_model_gateway(
        ModelSettings(
            provider=ModelProvider.OPENAI_COMPATIBLE,
            model="custom",
            base_url="https://models.example/v1",
            allow_remote_https=True,
        ),
        secret_scope_id="workspace:wsi_test",
        secrets=resolver,
        network_enabled=True,
    )
    assert isinstance(official, OpenAIProvider)
    assert isinstance(compatible, OpenAICompatibleProvider)


def test_compatible_secret_provider_id_has_cross_language_endpoint_fixed_vector() -> None:
    assert (
        model_secret_provider_id("openai-compatible", "https://MODELS.example:443//v1//")
        == "openai-compatible.00a98afb5b4eaaf4f9a877f0f3683900"
    )
    assert model_secret_provider_id("openai-compatible", "https://other.example/v1") != model_secret_provider_id(
        "openai-compatible", "https://models.example/v1"
    )
    assert model_secret_provider_id("openai") == "openai"
