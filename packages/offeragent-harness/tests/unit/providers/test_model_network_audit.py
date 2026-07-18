from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from types import MappingProxyType

import httpx
import pytest

from offeragent_harness.foundation import NetworkAuditRecord
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
from offeragent_harness.providers import (
    ModelCredentialLease,
    ModelNetworkAuditError,
    ModelNetworkAuditor,
    OpenAIResponsesConfig,
    OpenAIResponsesGateway,
    StaticModelEndpointPolicy,
)
from offeragent_harness.testing import ManualCancellationToken, ManualClock, RecordingNetworkAuditSink
from offeragent_harness.testing.errors import FakeRunCancelled

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
WORKSPACE_ID = "wsi_test"
RUN_ID = "run_test_1"


class _CredentialSource:
    @contextmanager
    def lease(self) -> Iterator[ModelCredentialLease]:
        material = bytearray(b"subscription-secret")
        view = memoryview(material)
        try:
            yield ModelCredentialLease(
                material=view,
                headers=MappingProxyType({}),
                credential_fingerprint="credential-test",
                account_fingerprint="account-test",
            )
        finally:
            view.release()
            for index in range(len(material)):
                material[index] = 0


class _FailingAuditSink:
    def __init__(self, fail_on_call: int) -> None:
        self.fail_on_call = fail_on_call
        self.calls = 0

    async def record(self, audit: NetworkAuditRecord) -> None:
        del audit
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise OSError("durable audit unavailable")


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


def _request(*, with_run_identity: bool = True) -> ModelRequest:
    metadata: dict[str, object] = {"workspaceId": WORKSPACE_ID}
    if with_run_identity:
        metadata["runId"] = RUN_ID
    return ModelRequest(
        request_id="req_model_inference_1",
        model="gpt-test",
        model_instructions="catalog-owned model baseline",
        purpose=ModelPurpose.RESPONDING,
        messages=(ModelMessage(ModelRole.USER, (ModelContentBlock.text("fixed"),)),),
        output_mode=ModelOutputMode.TEXT,
        output_schema=None,
        max_output_tokens=8,
        reasoning_effort=None,
        temperature=None,
        seed=None,
        trace_context=TraceContext("trace_model_audit"),
        metadata=metadata,
    )


def _sse(text: str = "ok") -> bytes:
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


def _gateway(transport: httpx.BaseTransport, sink: object) -> OpenAIResponsesGateway:
    config = OpenAIResponsesConfig(
        retry_base_seconds=0.0,
        retry_max_seconds=0.0,
        retry_jitter_ratio=0.0,
    )
    return OpenAIResponsesGateway(
        config=config,
        endpoint_policy=StaticModelEndpointPolicy(frozenset({config.endpoint})),
        credential_source=_CredentialSource(),
        transport=transport,
        network_auditor=ModelNetworkAuditor(
            workspace_id=WORKSPACE_ID,
            provider_id=config.provider_id,
            endpoint=config.endpoint,
            sink=sink,  # type: ignore[arg-type]
            clock=ManualClock(NOW),
        ),
    )


async def _collect(gateway: OpenAIResponsesGateway) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in gateway.stream(_request(), ManualCancellationToken())])


@pytest.mark.asyncio
async def test_model_audit_requires_run_identity_and_redacts_request_content() -> None:
    sink = RecordingNetworkAuditSink()
    auditor = ModelNetworkAuditor(
        workspace_id=WORKSPACE_ID,
        provider_id="codex-subscription",
        endpoint="https://chatgpt.com/backend-api/codex/responses?not-recorded=1",
        sink=sink,
        clock=ManualClock(NOW),
    )
    await auditor.record_intent(_request(), 1, sent_bytes=0)
    with pytest.raises(ModelNetworkAuditError):
        await auditor.record_intent(_request(with_run_identity=False), 1, sent_bytes=0)
    serialized = json.dumps([asdict(record) for record in sink.records], default=str)
    assert "fixed" not in serialized
    assert "not-recorded" not in serialized
    assert sink.records[0].run_id == RUN_ID


@pytest.mark.asyncio
async def test_audit_intent_failure_prevents_codex_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_sse(), request=request)

    events = await _collect(_gateway(httpx.MockTransport(handler), _FailingAuditSink(1)))
    assert calls == 0
    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None and events[-1].error.code == "provider_audit_unavailable"


@pytest.mark.asyncio
async def test_retry_attempts_are_durably_audited_in_order() -> None:
    calls = 0
    sink = RecordingNetworkAuditSink()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, content=b"busy", request=request)
        return httpx.Response(200, content=_sse(), request=request)

    events = await _collect(_gateway(httpx.MockTransport(handler), sink))
    assert calls == 2 and events[-1].kind is ModelEventKind.COMPLETED
    assert [(record.attempt, record.stage, record.outcome) for record in sink.records] == [
        (1, "intent", "attempting"),
        (1, "result", "http_error"),
        (2, "intent", "attempting"),
        (2, "result", "completed"),
    ]


@pytest.mark.asyncio
async def test_cancellation_closes_stream_and_audits_unconfirmed_stop() -> None:
    blocking = _BlockingStream()
    sink = RecordingNetworkAuditSink()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=blocking, request=request)

    token = ManualCancellationToken()
    stream = _gateway(httpx.MockTransport(handler), sink).stream(_request(), token)
    assert (await anext(stream)).kind is ModelEventKind.STARTED
    pending: asyncio.Future[ModelEvent] = asyncio.ensure_future(anext(stream))
    assert await asyncio.to_thread(blocking.started.wait, 2)
    token.cancel()
    with pytest.raises(FakeRunCancelled):
        await pending
    assert blocking.closed.is_set()
    assert [(record.stage, record.outcome) for record in sink.records] == [
        ("intent", "attempting"),
        ("result", "provider_cancel_unconfirmed"),
    ]
