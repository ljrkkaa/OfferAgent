from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import TypeVar

import httpx
import pytest

from offeragent_harness.config import ModelProvider, ModelSettings, ModelWireApi
from offeragent_harness.foundation import NetworkAuditRecord, NetworkCategory, NetworkOperationPurpose
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
from offeragent_harness.ports.model import ModelGateway
from offeragent_harness.providers import (
    ModelNetworkAuditError,
    ModelNetworkAuditor,
    OllamaConfig,
    OllamaLocalProvider,
    OpenAIResponsesConfig,
    OpenAIResponsesGateway,
    StaticModelEndpointPolicy,
    compose_model_gateway,
)
from offeragent_harness.testing import ManualCancellationToken, ManualClock, RecordingNetworkAuditSink
from offeragent_harness.testing.errors import FakeRunCancelled

T = TypeVar("T")
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
WORKSPACE_ID = "wsi_test"
RUN_ID = "run_test_1"


class _SecretResolver:
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
        assert scope_id == WORKSPACE_ID
        assert expected_kind is SecretKind.MODEL_PROVIDER
        assert expected_provider_id == "openai"
        material = bytearray(b"provider-secret")
        view = memoryview(material)
        try:
            return consumer(view)
        finally:
            view.release()
            for index in range(len(material)):
                material[index] = 0


class _FailingAuditSink:
    def __init__(self, fail_on_call: int) -> None:
        self._fail_on_call = fail_on_call
        self.calls = 0
        self.records: list[NetworkAuditRecord] = []
        self.attempted: list[NetworkAuditRecord] = []

    async def record(self, audit: NetworkAuditRecord) -> None:
        self.calls += 1
        self.attempted.append(audit)
        if self.calls == self._fail_on_call:
            raise OSError("durable audit unavailable")
        self.records.append(audit)


class _CancelOnIntentSink:
    def __init__(self, cancellation: ManualCancellationToken) -> None:
        self._cancellation = cancellation
        self.records: list[NetworkAuditRecord] = []

    async def record(self, audit: NetworkAuditRecord) -> None:
        self.records.append(audit)
        if audit.stage == "intent":
            self._cancellation.cancel()


def _request(*, health: bool = False, with_run_identity: bool = True) -> ModelRequest:
    metadata: dict[str, object]
    request_id: str
    if health:
        request_id = "req_model_health_1"
        metadata = {
            "operation": "model_health",
            "clientRequestId": "req_health_client_1",
            "contentSource": "fixed_runtime_probe",
        }
    else:
        request_id = "req_model_inference_1"
        metadata = {"workspaceId": WORKSPACE_ID}
        if with_run_identity:
            metadata["runId"] = RUN_ID
    return ModelRequest(
        request_id=request_id,
        model="test-model",
        purpose=ModelPurpose.GROUNDING if health else ModelPurpose.COMPOSING,
        messages=(ModelMessage(ModelRole.SYSTEM, (ModelContentBlock.text("fixed"),)),),
        output_mode=ModelOutputMode.TEXT,
        output_schema=None,
        max_output_tokens=8,
        reasoning_effort=None,
        temperature=None,
        seed=None,
        trace_context=TraceContext("trace_model_audit"),
        metadata=metadata,
    )


def _openai_gateway(
    transport: httpx.BaseTransport,
    sink: RecordingNetworkAuditSink | _FailingAuditSink | _CancelOnIntentSink,
) -> OpenAIResponsesGateway:
    config = OpenAIResponsesConfig(
        provider_id="openai",
        base_url="https://api.openai.example/v1",
        secret_scope_id=WORKSPACE_ID,
        credential_handle=SecretHandle("secret:v1:0123456789abcdef0123456789abcdef"),
        retry_base_seconds=0.0,
        retry_max_seconds=0.0,
        retry_jitter_ratio=0.0,
    )
    return OpenAIResponsesGateway(
        config=config,
        secrets=_SecretResolver(),
        endpoint_policy=StaticModelEndpointPolicy(frozenset({config.endpoint})),
        transport=transport,
        network_auditor=ModelNetworkAuditor(
            workspace_id=WORKSPACE_ID,
            provider_id="openai",
            endpoint=config.endpoint,
            sink=sink,
            clock=ManualClock(NOW),
        ),
    )


def _ollama_gateway(
    transport: httpx.AsyncBaseTransport,
    sink: RecordingNetworkAuditSink | _FailingAuditSink | _CancelOnIntentSink,
) -> OllamaLocalProvider:
    config = OllamaConfig(
        base_url="http://127.0.0.1:11434/api",
        retry_base_seconds=0.0,
        retry_max_seconds=0.0,
        retry_jitter_ratio=0.0,
    )
    return OllamaLocalProvider(
        config=config,
        endpoint_policy=StaticModelEndpointPolicy(frozenset({config.endpoint})),
        transport=transport,
        network_auditor=ModelNetworkAuditor(
            workspace_id=WORKSPACE_ID,
            provider_id="ollama",
            endpoint=config.endpoint,
            sink=sink,
            clock=ManualClock(NOW),
        ),
    )


async def _collect(provider: ModelGateway, request: ModelRequest) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in provider.stream(request, ManualCancellationToken())])


async def _next_event(stream: AsyncIterator[ModelEvent]) -> ModelEvent:
    return await anext(stream)


def _openai_sse(text: str = "ok") -> bytes:
    events = (
        {"type": "response.created", "sequence_number": 0, "response": {"id": "resp_1"}},
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.completed",
            "sequence_number": 2,
            "response": {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        },
    )
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode() for event in events
    )


def _ollama_ndjson(text: str = "ok") -> bytes:
    return (
        json.dumps(
            {
                "model": "test-model",
                "message": {"role": "assistant", "content": text},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 1,
                "eval_count": 1,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


class _BlockingOpenAIStream(httpx.SyncByteStream):
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


class _BlockingOllamaStream(httpx.AsyncByteStream):
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
async def test_model_audit_has_real_run_or_admin_identity_and_no_request_content() -> None:
    sink = RecordingNetworkAuditSink()
    auditor = ModelNetworkAuditor(
        workspace_id=WORKSPACE_ID,
        provider_id="openai",
        endpoint="https://api.openai.example/v1/responses?must_not_be_recorded=1",
        sink=sink,
        clock=ManualClock(NOW),
    )

    await auditor.record_intent(_request(), 1, sent_bytes=0)
    await auditor.record_result(
        _request(),
        1,
        outcome="completed",
        status_code=200,
        sent_bytes=321,
        received_bytes=654,
    )
    await auditor.record_intent(_request(health=True), 1, sent_bytes=0)

    run_intent, run_result, health_intent = sink.records
    assert run_intent.category is NetworkCategory.MODEL
    assert run_intent.operation_purpose is NetworkOperationPurpose.MODEL_INFERENCE
    assert run_intent.run_id == RUN_ID and run_intent.client_request_id is None
    assert run_result.event_id != run_intent.event_id
    assert run_result.sent_bytes == 321 and run_result.received_bytes == 654
    assert health_intent.operation_purpose is NetworkOperationPurpose.MODEL_HEALTH
    assert health_intent.run_id is None and health_intent.client_request_id == "req_health_client_1"
    serialized = json.dumps([asdict(record) for record in sink.records], default=str)
    for forbidden in ("fixed", "must_not_be_recorded", "/v1/responses", "provider-secret"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_model_audit_rejects_missing_identity_and_workspace_mismatch() -> None:
    auditor = ModelNetworkAuditor(
        workspace_id=WORKSPACE_ID,
        provider_id="openai",
        endpoint="https://api.openai.example/v1/responses",
        sink=RecordingNetworkAuditSink(),
        clock=ManualClock(NOW),
    )

    with pytest.raises(ModelNetworkAuditError):
        await auditor.record_intent(_request(with_run_identity=False), 1, sent_bytes=1)
    wrong = replace(_request(), metadata={"workspaceId": "wsi_other", "runId": RUN_ID})
    with pytest.raises(ModelNetworkAuditError):
        await auditor.record_intent(wrong, 1, sent_bytes=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "ollama"])
async def test_audit_intent_failure_prevents_provider_network(provider: str) -> None:
    calls = 0
    sink = _FailingAuditSink(1)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if provider == "openai":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_openai_sse())
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, content=_ollama_ndjson())

    gateway = (
        _openai_gateway(httpx.MockTransport(handler), sink)
        if provider == "openai"
        else _ollama_gateway(httpx.MockTransport(handler), sink)
    )
    events = await _collect(gateway, _request())

    assert calls == 0
    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]
    assert events[-1].error is not None and events[-1].error.code == "provider_audit_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "ollama"])
async def test_cancellation_committed_with_intent_prevents_send_and_pairs_zero_byte_result(provider: str) -> None:
    calls = 0
    token = ManualCancellationToken()
    sink = _CancelOnIntentSink(token)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if provider == "openai":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_openai_sse())
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, content=_ollama_ndjson())

    gateway = (
        _openai_gateway(httpx.MockTransport(handler), sink)
        if provider == "openai"
        else _ollama_gateway(httpx.MockTransport(handler), sink)
    )
    stream = gateway.stream(_request(), token)
    assert (await anext(stream)).kind is ModelEventKind.STARTED

    with pytest.raises(FakeRunCancelled):
        await anext(stream)
    assert calls == 0
    assert [(record.stage, record.outcome, record.sent_bytes) for record in sink.records] == [
        ("intent", "attempting", 0),
        ("result", "cancelled_before_send", 0),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "ollama"])
async def test_audit_result_failure_suppresses_completed_event(provider: str) -> None:
    calls = 0
    sink = _FailingAuditSink(2)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if provider == "openai":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_openai_sse())
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, content=_ollama_ndjson())

    gateway = (
        _openai_gateway(httpx.MockTransport(handler), sink)
        if provider == "openai"
        else _ollama_gateway(httpx.MockTransport(handler), sink)
    )
    events = await _collect(gateway, _request())

    assert calls == 1
    assert ModelEventKind.COMPLETED not in {event.kind for event in events}
    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None and events[-1].error.code == "provider_audit_unavailable"


@pytest.mark.asyncio
async def test_provider_terminal_failure_is_audited_as_provider_error() -> None:
    sink = RecordingNetworkAuditSink()
    response_events = (
        {"type": "response.created", "sequence_number": 0, "response": {"id": "resp_1"}},
        {"type": "response.failed", "sequence_number": 1, "error": {"code": "server_error"}},
    )
    content = b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
        for event in response_events
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    events = await _collect(_openai_gateway(httpx.MockTransport(handler), sink), _request())

    assert events[-1].kind is ModelEventKind.ERROR
    assert [record.outcome for record in sink.records] == ["attempting", "provider_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "ollama"])
async def test_provider_retry_attempts_are_durably_audited_in_order(provider: str) -> None:
    calls = 0
    sink = RecordingNetworkAuditSink()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, content=b"busy")
        if provider == "openai":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_openai_sse())
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, content=_ollama_ndjson())

    gateway = (
        _openai_gateway(httpx.MockTransport(handler), sink)
        if provider == "openai"
        else _ollama_gateway(httpx.MockTransport(handler), sink)
    )
    events = await _collect(gateway, _request())

    assert calls == 2 and events[-1].kind is ModelEventKind.COMPLETED
    assert [(record.attempt, record.stage) for record in sink.records] == [
        (1, "intent"),
        (1, "result"),
        (2, "intent"),
        (2, "result"),
    ]
    assert [record.outcome for record in sink.records] == [
        "attempting",
        "http_error",
        "attempting",
        "completed",
    ]
    assert sink.records[1].status_code == 503 and sink.records[3].status_code == 200
    assert sink.records[0].sent_bytes == sink.records[2].sent_bytes == 0
    assert sink.records[1].sent_bytes > 0
    assert sink.records[1].sent_bytes == sink.records[3].sent_bytes
    assert len({record.event_id for record in sink.records}) == 4


@pytest.mark.asyncio
async def test_run_snapshot_composition_wires_the_shared_durable_model_audit() -> None:
    sink = RecordingNetworkAuditSink()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=_ollama_ndjson(),
        )

    gateway = compose_model_gateway(
        ModelSettings(
            provider=ModelProvider.LOCAL,
            wire_api=ModelWireApi.OLLAMA_CHAT,
            model="test-model",
            base_url="http://127.0.0.1:11434/api",
        ),
        secret_scope_id=WORKSPACE_ID,
        secrets=_SecretResolver(),
        network_enabled=True,
        ollama_transport=httpx.MockTransport(handler),
        network_audit=sink,
        clock=ManualClock(NOW),
    )

    events = await _collect(gateway, _request())

    assert events[-1].kind is ModelEventKind.COMPLETED
    assert [(record.stage, record.provider_id) for record in sink.records] == [
        ("intent", "ollama"),
        ("result", "ollama"),
    ]


@pytest.mark.asyncio
async def test_openai_cancellation_closes_stream_and_audits_unconfirmed_provider_stop() -> None:
    blocking = _BlockingOpenAIStream()
    sink = RecordingNetworkAuditSink()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=blocking)

    token = ManualCancellationToken()
    stream = _openai_gateway(httpx.MockTransport(handler), sink).stream(_request(), token)
    assert (await anext(stream)).kind is ModelEventKind.STARTED
    pending = asyncio.create_task(_next_event(stream))
    assert await asyncio.to_thread(blocking.started.wait, 2)
    token.cancel()

    with pytest.raises(FakeRunCancelled):
        await pending
    assert blocking.closed.is_set()
    assert [(record.stage, record.outcome) for record in sink.records] == [
        ("intent", "attempting"),
        ("result", "provider_cancel_unconfirmed"),
    ]


@pytest.mark.asyncio
async def test_ollama_cancellation_wins_even_if_result_audit_storage_fails() -> None:
    blocking = _BlockingOllamaStream()
    sink = _FailingAuditSink(2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, stream=blocking)

    token = ManualCancellationToken()
    stream = _ollama_gateway(httpx.MockTransport(handler), sink).stream(_request(), token)
    assert (await anext(stream)).kind is ModelEventKind.STARTED
    pending = asyncio.create_task(_next_event(stream))
    await asyncio.wait_for(blocking.started.wait(), timeout=2)
    token.cancel()

    with pytest.raises(FakeRunCancelled):
        await pending
    assert blocking.closed.is_set()
    assert sink.calls == 2
    assert sink.attempted[-1].outcome == "provider_cancel_unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "ollama"])
async def test_local_stream_task_close_after_send_is_audited_as_unconfirmed_provider_stop(provider: str) -> None:
    sink = RecordingNetworkAuditSink()
    gateway: ModelGateway
    if provider == "openai":
        blocking: _BlockingOpenAIStream | _BlockingOllamaStream = _BlockingOpenAIStream()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=blocking)

        gateway = _openai_gateway(httpx.MockTransport(handler), sink)
    else:
        blocking = _BlockingOllamaStream()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, stream=blocking)

        gateway = _ollama_gateway(httpx.MockTransport(handler), sink)

    stream = gateway.stream(_request(), ManualCancellationToken())
    assert (await anext(stream)).kind is ModelEventKind.STARTED
    pending = asyncio.create_task(_next_event(stream))
    if isinstance(blocking, _BlockingOpenAIStream):
        assert await asyncio.to_thread(blocking.started.wait, 2)
    else:
        await asyncio.wait_for(blocking.started.wait(), timeout=2)
    pending.cancel()

    with pytest.raises(asyncio.CancelledError):
        await pending
    assert [(record.stage, record.outcome) for record in sink.records] == [
        ("intent", "attempting"),
        ("result", "provider_cancel_unconfirmed"),
    ]


@pytest.mark.asyncio
async def test_external_ollama_stream_close_wins_if_result_audit_storage_fails() -> None:
    blocking = _BlockingOllamaStream()
    sink = _FailingAuditSink(2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, stream=blocking)

    stream = _ollama_gateway(httpx.MockTransport(handler), sink).stream(
        _request(),
        ManualCancellationToken(),
    )
    assert (await anext(stream)).kind is ModelEventKind.STARTED
    pending = asyncio.create_task(_next_event(stream))
    await asyncio.wait_for(blocking.started.wait(), timeout=2)
    pending.cancel()

    with pytest.raises(asyncio.CancelledError):
        await pending
    assert sink.calls == 2
    assert sink.attempted[-1].outcome == "provider_cancel_unconfirmed"
