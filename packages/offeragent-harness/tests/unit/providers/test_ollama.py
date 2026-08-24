from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import TypeVar

import httpx
import pytest

from offeragent_harness.config import ModelProvider, ModelSettings, ModelWireApi
from offeragent_harness.models import (
    ModelContentBlock,
    ModelEvent,
    ModelEventKind,
    ModelMessage,
    ModelOutputMode,
    ModelPurpose,
    ModelRequest,
    ModelRole,
    TraceContext,
)
from offeragent_harness.ports import SecretHandle, SecretKind
from offeragent_harness.providers import (
    OllamaConfig,
    OllamaLocalProvider,
    StaticModelEndpointPolicy,
    compose_model_gateway,
)
from offeragent_harness.testing.cancellation import ManualCancellationToken
from offeragent_harness.testing.errors import FakeRunCancelled

T = TypeVar("T")


def _request(mode: ModelOutputMode = ModelOutputMode.TEXT) -> ModelRequest:
    schema = None
    if mode is ModelOutputMode.JSON:
        schema = {
            "type": "object",
            "properties": {"decision": {"type": "string"}},
            "required": ["decision"],
            "additionalProperties": False,
        }
    return ModelRequest(
        request_id="req_ollama",
        model="qwen-test:latest",
        purpose=ModelPurpose.PLANNING if mode is ModelOutputMode.JSON else ModelPurpose.RESPONDING,
        messages=(
            ModelMessage(ModelRole.SYSTEM, (ModelContentBlock.text("system"),)),
            ModelMessage(ModelRole.USER, (ModelContentBlock.text("user"),)),
            ModelMessage(ModelRole.TOOL, (ModelContentBlock.text("evidence"),), name="workspace.read"),
        ),
        output_mode=mode,
        output_schema=schema,
        max_output_tokens=64,
        reasoning_effort="high",
        temperature=0.2,
        seed=42,
        trace_context=TraceContext("trace_ollama"),
        metadata={"workspaceId": "must-stay-local"},
    )


def _config(**overrides: object) -> OllamaConfig:
    values: dict[str, object] = {
        "base_url": "http://127.0.0.1:11434/api",
        "retry_base_seconds": 0.0,
        "retry_max_seconds": 0.0,
        "retry_jitter_ratio": 0.0,
    }
    values.update(overrides)
    return OllamaConfig(**values)  # type: ignore[arg-type]


def _gateway(transport: httpx.AsyncBaseTransport, config: OllamaConfig | None = None) -> OllamaLocalProvider:
    selected = config or _config()
    return OllamaLocalProvider(
        config=selected,
        endpoint_policy=StaticModelEndpointPolicy(frozenset({selected.endpoint})),
        transport=transport,
    )


def _line(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


async def _collect(provider: OllamaLocalProvider, request: ModelRequest) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in provider.stream(request, ManualCancellationToken())])


@pytest.mark.asyncio
async def test_native_ollama_streams_text_and_never_exposes_tools_or_runtime_metadata() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.update(body)
        content = b"".join(
            (
                _line(
                    {
                        "model": "qwen-test:latest",
                        "message": {"role": "assistant", "content": "hello ", "thinking": "raw hidden"},
                        "done": False,
                    }
                ),
                _line(
                    {
                        "model": "qwen-test:latest",
                        "message": {"role": "assistant", "content": "local"},
                        "done": False,
                    }
                ),
                _line(
                    {
                        "model": "qwen-test:latest",
                        "message": {"role": "assistant", "content": ""},
                        "done": True,
                        "done_reason": "stop",
                        "prompt_eval_count": 12,
                        "eval_count": 3,
                    }
                ),
            )
        )
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, content=content)

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]
    assert "".join(event.text or "" for event in events) == "hello local"
    assert all(event.kind is not ModelEventKind.REASONING_SUMMARY for event in events)
    assert events[-2].usage is not None and events[-2].usage.input_tokens == 12
    assert captured["stream"] is True
    assert "tools" not in captured
    assert "workspaceId" not in json.dumps(captured)
    assert captured["options"] == {"num_predict": 64, "seed": 42, "temperature": 0.2}
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[-1]["role"] == "user"
    assert "Local tool result workspace.read" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_native_ollama_structured_output_is_streamed_then_strictly_validated() -> None:
    structured = '{"decision":"continue"}'

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["format"] == {
            "type": "object",
            "properties": {"decision": {"type": "string"}},
            "required": ["decision"],
            "additionalProperties": False,
        }
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=b"".join(
                (
                    _line(
                        {
                            "model": "qwen-test:latest",
                            "message": {"role": "assistant", "content": structured[:10]},
                            "done": False,
                        }
                    ),
                    _line(
                        {
                            "model": "qwen-test:latest",
                            "message": {"role": "assistant", "content": structured[10:]},
                            "done": False,
                        }
                    ),
                    _line(
                        {
                            "model": "qwen-test:latest",
                            "message": {"role": "assistant", "content": ""},
                            "done": True,
                            "done_reason": "stop",
                            "prompt_eval_count": 20,
                            "eval_count": 6,
                        }
                    ),
                )
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request(ModelOutputMode.JSON))

    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.STRUCTURED_OUTPUT,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]
    assert events[1].data == {"decision": "continue"}


@pytest.mark.asyncio
async def test_ollama_retries_503_before_output_but_not_after_partial_output() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=_line(
                {
                    "model": "qwen-test:latest",
                    "message": {"role": "assistant", "content": "ok"},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                }
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())
    assert calls == 2
    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_fields",
    (
        {"code": "context_window_exceeded"},
        {"reason": "input_too_long"},
    ),
)
async def test_ollama_http_context_overflow_uses_bounded_structured_discriminators(
    error_fields: dict[str, str],
) -> None:
    secret = "private local prompt must not be reflected"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {**error_fields, "message": secret}})

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]
    assert events[-1].error is not None and events[-1].error.code == "context_overflow"
    assert events[-1].error.retryable is False
    assert secret not in repr(events[-1])


@pytest.mark.asyncio
async def test_ollama_ndjson_context_overflow_is_typed_without_echoing_body() -> None:
    secret = "vault body must not be reflected"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=_line(
                {
                    "error": {
                        "code": "max_context_length_exceeded",
                        "message": secret,
                    }
                }
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]
    assert events[-1].error is not None and events[-1].error.code == "context_overflow"
    assert secret not in repr(events[-1])


@pytest.mark.asyncio
async def test_ollama_unstructured_overflow_words_do_not_trigger_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=_line({"error": "context_length_exceeded private prompt"}),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert events[-1].error is not None and events[-1].error.code == "provider_response_failed"
    assert "private prompt" not in repr(events[-1])


class _FailAfterPartial(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield _line(
            {
                "model": "qwen-test:latest",
                "message": {"role": "assistant", "content": "partial"},
                "done": False,
            }
        )
        raise httpx.ReadError("lost")


@pytest.mark.asyncio
async def test_ollama_does_not_retry_or_duplicate_after_partial_output() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            stream=_FailAfterPartial(),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert calls == 1
    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.ERROR,
    ]


@pytest.mark.asyncio
async def test_ollama_native_tool_call_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=_line(
                {
                    "model": "qwen-test:latest",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": "shell", "arguments": {}}}],
                    },
                    "done": False,
                }
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())
    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]


class _BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        self.started.set()
        await self.closed.wait()
        return
        yield b""  # pragma: no cover

    async def aclose(self) -> None:
        self.closed.set()


@pytest.mark.asyncio
async def test_ollama_cancellation_closes_slow_stream_immediately() -> None:
    blocking = _BlockingStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            stream=blocking,
        )

    token = ManualCancellationToken()
    stream = _gateway(httpx.MockTransport(handler)).stream(_request(), token)
    assert (await anext(stream)).kind is ModelEventKind.STARTED
    pending: asyncio.Future[ModelEvent] = asyncio.ensure_future(anext(stream))
    await asyncio.wait_for(blocking.started.wait(), timeout=2)
    token.cancel()

    with pytest.raises(FakeRunCancelled):
        await pending
    assert blocking.closed.is_set()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/api",
        "http://192.168.1.2:11434/api",
        "https://127.0.0.1:11434/api",
        "http://user:pass@127.0.0.1:11434/api",
        "http://127.0.0.1:11434/not-api",
        "http://127.0.0.1:11434/api?secret=x",
    ],
)
def test_native_ollama_endpoint_is_literal_loopback_only(url: str) -> None:
    with pytest.raises(ValueError):
        OllamaConfig(base_url=url)


def test_run_snapshot_composition_selects_native_ollama() -> None:
    class NoSecrets:
        def consume(
            self,
            handle: SecretHandle,
            *,
            scope_id: str,
            expected_kind: SecretKind,
            expected_provider_id: str,
            consumer: Callable[[memoryview], T],
        ) -> T:
            raise AssertionError("native local Ollama must not request a secret")

    provider = compose_model_gateway(
        ModelSettings(
            provider=ModelProvider.LOCAL,
            wire_api=ModelWireApi.OLLAMA_CHAT,
            model="qwen-test:latest",
            base_url="http://127.0.0.1:11434/api",
        ),
        secret_scope_id="workspace:wsi_test",
        secrets=NoSecrets(),
        network_enabled=True,
    )
    assert isinstance(provider, OllamaLocalProvider)
