from __future__ import annotations

import base64
import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from types import MappingProxyType
from typing import Any, cast

import httpx
import pytest

from offeragent_harness.models import (
    ModelCitation,
    ModelContentBlock,
    ModelContinuation,
    ModelEvent,
    ModelEventKind,
    ModelFinishReason,
    ModelHostedSearch,
    ModelHostedSearchPhase,
    ModelHostedTool,
    ModelMessage,
    ModelOutputMode,
    ModelPurpose,
    ModelRequest,
    ModelRole,
    TraceContext,
    thaw_json,
)
from offeragent_harness.providers import (
    ModelCredentialLease,
    ModelProviderConfigurationError,
    OpenAIResponsesConfig,
    OpenAIResponsesGateway,
    StaticModelEndpointPolicy,
)
from offeragent_harness.runtime.conversation_attachments import AttachmentLimits
from offeragent_harness.testing.cancellation import ManualCancellationToken
from offeragent_harness.testing.errors import FakeRunCancelled


class _CredentialSource:
    def __init__(self, secret: bytes = b"test-provider-secret") -> None:
        self.buffer = bytearray(secret)
        self.calls = 0

    @contextmanager
    def lease(self) -> Iterator[ModelCredentialLease]:
        self.calls += 1
        view = memoryview(self.buffer)
        try:
            yield ModelCredentialLease(
                material=view,
                headers=MappingProxyType({}),
                credential_fingerprint="credential-test",
                account_fingerprint="account-test",
            )
        finally:
            view.release()
            for index in range(len(self.buffer)):
                self.buffer[index] = 0


def _request(
    *,
    output_mode: ModelOutputMode = ModelOutputMode.TEXT,
    hosted_search: bool = False,
    responses_lite: bool = False,
) -> ModelRequest:
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
        model_instructions="catalog-owned model baseline",
        use_responses_lite=responses_lite,
        purpose=ModelPurpose.PLANNING if output_mode is ModelOutputMode.JSON else ModelPurpose.RESPONDING,
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
        hosted_tools=(ModelHostedTool.WEB_SEARCH,) if hosted_search else (),
    )


def _config() -> OpenAIResponsesConfig:
    return OpenAIResponsesConfig()


def _gateway(
    handler: httpx.BaseTransport,
    credentials: _CredentialSource | None = None,
    config: OpenAIResponsesConfig | None = None,
) -> OpenAIResponsesGateway:
    config = config or _config()
    return OpenAIResponsesGateway(
        config=config,
        endpoint_policy=StaticModelEndpointPolicy(frozenset({config.endpoint})),
        transport=handler,
        credential_source=credentials or _CredentialSource(),
    )


def test_default_request_ceiling_covers_one_maximum_attachment_batch_as_data_urls() -> None:
    raw_image_bytes = AttachmentLimits().max_submission_bytes
    base64_bytes = 4 * ((raw_image_bytes + 2) // 3)

    assert _config().max_request_bytes >= base64_bytes + 8 * 1024 * 1024


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_hosted_search_calls", 17),
        ("max_hosted_search_sources", 65),
        ("max_hosted_search_citations", 257),
    ),
)
def test_hosted_search_config_cannot_raise_fixed_safety_ceilings(field: str, value: int) -> None:
    with pytest.raises(ModelProviderConfigurationError, match="hosted search count limits"):
        replace(_config(), **cast(Any, {field: value}))


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
    secrets = _CredentialSource()

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
    assert body["instructions"] == "catalog-owned model baseline"
    assert body["tools"] == []
    assert body["parallel_tool_calls"] is False
    assert body["input"][0]["role"] == "developer"
    encoded = json.dumps(body)
    assert "must-not-leave-process" not in encoded
    assert "test-provider-secret" not in encoded
    assert "Local tool result workspace.read" in encoded
    assert secrets.calls == 1 and set(secrets.buffer) == {0}


@pytest.mark.asyncio
async def test_codex_request_without_catalog_model_instructions_fails_before_http() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_sse(_completed("unreachable")), request=request)

    events = await _collect(
        _gateway(httpx.MockTransport(handler)),
        replace(_request(), model_instructions=None),
    )

    assert calls == 0
    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None
    assert events[-1].error.code == "provider_configuration"


@pytest.mark.asyncio
async def test_responses_lite_moves_catalog_baseline_and_tools_into_input_and_sets_header() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["header"] = request.headers.get("x-openai-internal-codex-responses-lite")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                _completed("ok"),
            ),
            request=request,
        )

    await _collect(
        _gateway(httpx.MockTransport(handler)),
        _request(hosted_search=True, responses_lite=True),
    )

    body = captured["body"]
    assert isinstance(body, dict)
    assert captured["header"] == "true"
    assert "instructions" not in body
    assert "tools" not in body
    assert body["reasoning"]["context"] == "all_turns"
    assert body["input"][:3] == [
        {"type": "additional_tools", "role": "developer", "tools": [{"type": "web_search"}]},
        {
            "role": "developer",
            "content": [{"type": "input_text", "text": "catalog-owned model baseline"}],
        },
        {
            "role": "developer",
            "content": [{"type": "input_text", "text": "system boundary"}],
        },
    ]


@pytest.mark.asyncio
async def test_stateless_continuation_replays_exact_output_before_named_local_result() -> None:
    first_output: list[dict[str, Any]] = [
        {
            "id": "rs_private",
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "opaque-encrypted-reasoning",
        },
        {
            "id": "msg_private",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": '{"calls":[]}'}],
        },
    ]
    fallback_response = cast(dict[str, Any], _completed("ok")["response"])
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, dict)
        bodies.append(body)
        output = first_output if len(bodies) == 1 else fallback_response["output"]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "output_index": 1,
                    "content_index": 0,
                    "delta": '{"calls":[]}' if len(bodies) == 1 else "ok",
                },
                {
                    "type": "response.completed",
                    "sequence_number": 2,
                    "response": {
                        "status": "completed",
                        "output": output,
                        "usage": fallback_response["usage"],
                    },
                },
            ),
            request=request,
        )

    gateway = _gateway(httpx.MockTransport(handler))
    first_events = await _collect(gateway, _request())
    completed_events = [event for event in first_events if event.kind is ModelEventKind.COMPLETED]
    assert len(completed_events) == 1
    continuation = completed_events[0].continuation
    assert isinstance(continuation, ModelContinuation)
    assert thaw_json(continuation.output_items) == first_output

    second = replace(
        _request(),
        request_id="req_test_2",
        messages=(
            ModelMessage(ModelRole.SYSTEM, (ModelContentBlock.text("system boundary"),)),
            ModelMessage(ModelRole.USER, (ModelContentBlock.text("user prompt"),)),
            ModelMessage(ModelRole.ASSISTANT, (continuation.as_content_block(),)),
            ModelMessage(
                ModelRole.TOOL,
                (ModelContentBlock.text('{"agentStepId":"step_1","status":"succeeded"}'),),
                name="planning_memory.list@1",
            ),
        ),
    )
    second_events = await _collect(_gateway(httpx.MockTransport(handler)), second)
    assert second_events[-1].kind is ModelEventKind.COMPLETED, second_events[-1]

    assert bodies[0]["include"] == ["reasoning.encrypted_content"]
    replayed = bodies[1]["input"][-3:]
    assert replayed[0] == {key: value for key, value in first_output[0].items() if key != "id"}
    assert replayed[1] == {key: value for key, value in first_output[1].items() if key != "id"}
    assert replayed[1]["phase"] == "final_answer"
    assert replayed[2]["role"] == "user"
    assert "Local tool result planning_memory.list@1" in replayed[2]["content"][0]["text"]
    assert "function_call_output" not in json.dumps(bodies[1])


def test_model_continuation_rejects_coerced_identity_fields() -> None:
    block = ModelContentBlock(
        "model_continuation",
        {
            "providerId": 1,
            "model": "gpt-test",
            "requestId": "request-1",
            "outputItems": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "contentHash": "sha256:" + "0" * 64,
        },
    )

    with pytest.raises(TypeError, match="identity fields"):
        ModelContinuation.from_content_block(block)


@pytest.mark.asyncio
async def test_empty_terminal_output_reports_a_specific_continuation_reason() -> None:
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
                    "delta": "ok",
                },
                {
                    "type": "response.output_text.done",
                    "sequence_number": 2,
                    "output_index": 0,
                    "content_index": 0,
                    "text": "ok",
                },
                {
                    "type": "response.completed",
                    "sequence_number": 3,
                    "response": {
                        "status": "completed",
                        "output": [],
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens_details": {"reasoning_tokens": 0},
                        },
                    },
                },
            ),
            request=request,
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None
    assert events[-1].error.details["protocolReason"] == "invalid_continuation_item_count"


@pytest.mark.asyncio
async def test_hosted_search_request_and_stream_are_typed_bounded_and_citation_preserving() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        content = _sse(
            {"type": "response.created", "sequence_number": 0, "response": {"id": "resp_search"}},
            {
                "type": "response.output_item.added",
                "sequence_number": 1,
                "output_index": 0,
                "item": {"id": "ws_1", "type": "web_search_call", "status": "in_progress"},
            },
            {
                "type": "response.web_search_call.in_progress",
                "sequence_number": 2,
                "output_index": 0,
                "item_id": "ws_1",
            },
            {
                "type": "response.web_search_call.searching",
                "sequence_number": 3,
                "output_index": 0,
                "item_id": "ws_1",
            },
            {
                "type": "response.web_search_call.completed",
                "sequence_number": 4,
                "output_index": 0,
                "item_id": "ws_1",
            },
            {
                "type": "response.output_item.done",
                "sequence_number": 5,
                "output_index": 0,
                "item": {
                    "id": "ws_1",
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {
                        "type": "search",
                        "queries": ["OfferAgent interview research"],
                        "sources": [{"type": "url", "url": "https://example.com/interview"}],
                    },
                },
            },
            {
                "type": "response.reasoning_summary_text.delta",
                "sequence_number": 6,
                "output_index": 1,
                "summary_index": 0,
                "delta": "Checked a public source.",
            },
            {
                "type": "response.output_text.delta",
                "sequence_number": 7,
                "item_id": "msg_1",
                "output_index": 1,
                "content_index": 0,
                "delta": "Cited answer",
            },
            {
                "type": "response.output_text.annotation.added",
                "sequence_number": 8,
                "item_id": "msg_1",
                "output_index": 1,
                "content_index": 0,
                "annotation_index": 0,
                "annotation": {
                    "type": "url_citation",
                    "start_index": 0,
                    "end_index": 5,
                    "url": "https://example.com/interview",
                    "title": "Example interview",
                },
            },
            {
                "type": "response.output_text.done",
                "sequence_number": 9,
                "item_id": "msg_1",
                "output_index": 1,
                "content_index": 0,
                "text": "Cited answer",
            },
            {
                "type": "response.output_item.done",
                "sequence_number": 10,
                "output_index": 1,
                "item": {
                    "id": "msg_1",
                    "type": "message",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Cited answer",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "start_index": 0,
                                    "end_index": 5,
                                    "url": "https://example.com/interview",
                                    "title": "Example interview",
                                }
                            ],
                        }
                    ],
                },
            },
            {
                "type": "response.completed",
                "sequence_number": 11,
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "id": "ws_1",
                            "type": "web_search_call",
                            "status": "completed",
                            "action": {
                                "type": "search",
                                "queries": ["OfferAgent interview research"],
                                "sources": [{"type": "url", "url": "https://example.com/interview"}],
                            },
                        },
                        {
                            "id": "msg_1",
                            "type": "message",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Cited answer",
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "start_index": 0,
                                            "end_index": 5,
                                            "url": "https://example.com/interview",
                                            "title": "Example interview",
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 8,
                        "input_tokens_details": {"cached_tokens": 2},
                        "output_tokens_details": {"reasoning_tokens": 1},
                    },
                },
            },
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request(hosted_search=True))

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["tools"] == [{"type": "web_search"}]
    assert body["tool_choice"] == "auto"
    assert body["include"] == ["reasoning.encrypted_content", "web_search_call.action.sources"]
    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.HOSTED_SEARCH,
        ModelEventKind.HOSTED_SEARCH,
        ModelEventKind.HOSTED_SEARCH,
        ModelEventKind.HOSTED_SEARCH,
        ModelEventKind.REASONING_SUMMARY,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.CITATION,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]
    searches = [event.hosted_search for event in events if event.hosted_search is not None]
    assert searches == [
        ModelHostedSearch("ws_1", ModelHostedSearchPhase.STARTED),
        ModelHostedSearch("ws_1", ModelHostedSearchPhase.IN_PROGRESS),
        ModelHostedSearch("ws_1", ModelHostedSearchPhase.SEARCHING),
        ModelHostedSearch("ws_1", ModelHostedSearchPhase.COMPLETED),
    ]
    citations = [event.citation for event in events if event.citation is not None]
    assert citations == [
        ModelCitation(
            provider_id="codex-subscription",
            model="gpt-test",
            request_id="req_test_1",
            url="https://example.com/interview",
            title="Example interview",
            start_index=0,
            end_index=5,
        )
    ]


@pytest.mark.asyncio
async def test_undeclared_or_out_of_order_hosted_search_fails_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.web_search_call.searching",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item_id": "ws_missing",
                },
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert calls == 1
    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]
    assert events[-1].error is not None and events[-1].error.code == "provider_protocol_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_url",
    ["https://user:password@example.com/private", "file:///C:/private.txt"],
)
async def test_hosted_search_rejects_unsafe_included_source_urls(source_url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.output_item.added",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item": {"id": "ws_unsafe", "type": "web_search_call", "status": "in_progress"},
                },
                {
                    "type": "response.web_search_call.completed",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "ws_unsafe",
                },
                {
                    "type": "response.output_item.done",
                    "sequence_number": 3,
                    "output_index": 0,
                    "item": {
                        "id": "ws_unsafe",
                        "type": "web_search_call",
                        "status": "completed",
                        "action": {
                            "type": "search",
                            "sources": [{"type": "url", "url": source_url}],
                        },
                    },
                },
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request(hosted_search=True))

    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None and events[-1].error.code == "provider_protocol_error"


@pytest.mark.asyncio
async def test_hosted_search_source_count_is_locally_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.output_item.added",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item": {"id": "ws_bounded", "type": "web_search_call", "status": "in_progress"},
                },
                {
                    "type": "response.web_search_call.completed",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "ws_bounded",
                },
                {
                    "type": "response.output_item.done",
                    "sequence_number": 3,
                    "output_index": 0,
                    "item": {
                        "id": "ws_bounded",
                        "type": "web_search_call",
                        "status": "completed",
                        "action": {
                            "type": "search",
                            "sources": [
                                {"type": "url", "url": "https://example.com/one"},
                                {"type": "url", "url": "https://example.com/two"},
                            ],
                        },
                    },
                },
            ),
        )

    config = replace(_config(), max_hosted_search_sources=1)
    events = await _collect(_gateway(httpx.MockTransport(handler), config=config), _request(hosted_search=True))

    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None and events[-1].error.code == "provider_protocol_error"


@pytest.mark.asyncio
async def test_successful_terminal_must_reconcile_every_hosted_search_output_item() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.output_item.added",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item": {"id": "ws_lost", "type": "web_search_call", "status": "in_progress"},
                },
                {
                    "type": "response.web_search_call.completed",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "ws_lost",
                },
                _completed("answer", sequence=3),
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request(hosted_search=True))

    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None and events[-1].error.code == "provider_protocol_error"


@pytest.mark.asyncio
async def test_terminal_hosted_search_snapshot_must_match_its_done_event() -> None:
    done_action = {
        "type": "search",
        "queries": ["OfferAgent"],
        "sources": [{"type": "url", "url": "https://example.com/one"}],
    }
    terminal_action = {
        "type": "search",
        "queries": ["OfferAgent"],
        "sources": [{"type": "url", "url": "https://example.com/two"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.output_item.added",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item": {"id": "ws_changed", "type": "web_search_call", "status": "in_progress"},
                },
                {
                    "type": "response.web_search_call.completed",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "ws_changed",
                },
                {
                    "type": "response.output_item.done",
                    "sequence_number": 3,
                    "output_index": 0,
                    "item": {
                        "id": "ws_changed",
                        "type": "web_search_call",
                        "status": "completed",
                        "action": done_action,
                    },
                },
                {
                    "type": "response.completed",
                    "sequence_number": 4,
                    "response": {
                        "status": "completed",
                        "output": [
                            {
                                "id": "ws_changed",
                                "type": "web_search_call",
                                "status": "completed",
                                "action": terminal_action,
                            }
                        ],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                },
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request(hosted_search=True))

    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None and events[-1].error.code == "provider_protocol_error"


@pytest.mark.asyncio
async def test_duplicate_hosted_search_done_event_is_rejected() -> None:
    search_item = {
        "id": "ws_duplicate_done",
        "type": "web_search_call",
        "status": "completed",
        "action": {"type": "search", "queries": ["OfferAgent"]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.output_item.added",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item": {"id": "ws_duplicate_done", "type": "web_search_call", "status": "in_progress"},
                },
                {
                    "type": "response.web_search_call.completed",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "ws_duplicate_done",
                },
                {
                    "type": "response.output_item.done",
                    "sequence_number": 3,
                    "output_index": 0,
                    "item": search_item,
                },
                {
                    "type": "response.output_item.done",
                    "sequence_number": 4,
                    "output_index": 0,
                    "item": search_item,
                },
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request(hosted_search=True))

    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None and events[-1].error.code == "provider_protocol_error"


@pytest.mark.asyncio
async def test_terminal_url_citation_without_a_hosted_search_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        terminal = _completed("Cited answer", sequence=1)
        response = terminal["response"]
        assert isinstance(response, dict)
        response["output"] = [
            {
                "id": "msg_unsupported_citation",
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Cited answer",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "start_index": 0,
                                "end_index": 5,
                                "url": "https://example.com/source",
                                "title": "Unsupported citation",
                            }
                        ],
                    }
                ],
            }
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse({"type": "response.created", "sequence_number": 0, "response": {}}, terminal),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request(hosted_search=True))

    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None and events[-1].error.code == "provider_protocol_error"


@pytest.mark.asyncio
async def test_streamed_citation_must_resolve_to_streamed_output_text_coordinates() -> None:
    action = {"type": "search", "queries": ["OfferAgent"]}

    def handler(request: httpx.Request) -> httpx.Response:
        terminal = _completed("answer", sequence=5)
        response = terminal["response"]
        assert isinstance(response, dict)
        response["output"] = [
            {
                "id": "ws_coordinate",
                "type": "web_search_call",
                "status": "completed",
                "action": action,
            },
            {
                "id": "msg_coordinate",
                "type": "message",
                "content": [{"type": "output_text", "text": "answer", "annotations": []}],
            },
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.output_item.added",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item": {"id": "ws_coordinate", "type": "web_search_call", "status": "in_progress"},
                },
                {
                    "type": "response.web_search_call.completed",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "ws_coordinate",
                },
                {
                    "type": "response.output_item.done",
                    "sequence_number": 3,
                    "output_index": 0,
                    "item": {
                        "id": "ws_coordinate",
                        "type": "web_search_call",
                        "status": "completed",
                        "action": action,
                    },
                },
                {
                    "type": "response.output_text.annotation.added",
                    "sequence_number": 4,
                    "item_id": "msg_missing",
                    "output_index": 99,
                    "content_index": 0,
                    "annotation_index": 0,
                    "annotation": {
                        "type": "url_citation",
                        "start_index": 0,
                        "end_index": 1,
                        "url": "https://example.com/source",
                        "title": "Missing coordinates",
                    },
                },
                terminal,
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request(hosted_search=True))

    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None and events[-1].error.code == "provider_protocol_error"


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
async def test_false_array_items_are_projected_as_an_exact_empty_array_for_codex() -> None:
    structured = '{"calls":[]}'

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls = body["text"]["format"]["schema"]["properties"]["calls"]
        assert calls == {
            "type": "array",
            "maxItems": 0,
            "items": {"type": "string"},
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

    request = replace(
        _request(output_mode=ModelOutputMode.JSON),
        output_schema={
            "type": "object",
            "properties": {
                "calls": {
                    "type": "array",
                    "maxItems": 64,
                    "items": False,
                }
            },
            "required": ["calls"],
            "additionalProperties": False,
        },
    )

    events = await _collect(_gateway(httpx.MockTransport(handler)), request)

    assert events[1].data == {"calls": ()}


@pytest.mark.asyncio
async def test_implicit_object_refinements_are_omitted_from_the_codex_schema_projection() -> None:
    structured = '{"changeKind":"general","payload":null}'

    def handler(request: httpx.Request) -> httpx.Response:
        schema = json.loads(request.content)["text"]["format"]["schema"]
        assert "oneOf" not in schema
        assert "anyOf" not in schema
        assert schema["properties"]["changeKind"] == {"type": "string"}
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

    request = replace(
        _request(output_mode=ModelOutputMode.JSON),
        output_schema={
            "type": "object",
            "properties": {
                "changeKind": {"type": "string"},
                "payload": {"type": ["object", "null"]},
            },
            "required": ["changeKind", "payload"],
            "oneOf": [
                {"properties": {"changeKind": {"const": "general"}, "payload": {"type": "null"}}},
                {"properties": {"changeKind": {"const": "special"}, "payload": {"type": "object"}}},
            ],
            "additionalProperties": False,
        },
    )

    events = await _collect(_gateway(httpx.MockTransport(handler)), request)

    assert events[1].data == {"changeKind": "general", "payload": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code", "retryable"),
    [
        (401, "auth_required", False),
        (429, "provider_rate_limited", True),
        (503, "provider_unavailable", True),
    ],
)
async def test_http_errors_are_redacted_and_classified(
    status: int,
    expected_code: str,
    retryable: bool,
) -> None:
    secret = "must-never-appear"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": f"echo {secret}"}})

    resolver = _CredentialSource(secret.encode())
    events = await _collect(_gateway(httpx.MockTransport(handler), resolver), _request())

    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]
    assert events[-1].error is not None and events[-1].error.code == expected_code
    assert events[-1].error.retryable is retryable
    assert secret not in repr(events[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_code", "expected_code"),
    [
        ("invalid_image", "image_invalid"),
        ("invalid_image_format", "image_invalid"),
        ("unsupported_image_media_type", "image_unsupported"),
        ("image_not_supported", "image_unsupported"),
        ("unknown_bounded_failure", "provider_protocol_error"),
    ],
)
async def test_http_image_and_unknown_failures_use_bounded_typed_discriminators(
    provider_code: str,
    expected_code: str,
) -> None:
    secret = "must-not-cross-the-provider-boundary"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": provider_code, "message": secret}})

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert events[-1].error is not None and events[-1].error.code == expected_code
    assert events[-1].error.retryable is False
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
@pytest.mark.parametrize(
    ("provider_code", "expected_code", "retryable"),
    [
        ("authentication_error", "auth_required", False),
        ("model_not_found", "model_unsupported", False),
        ("invalid_image_url", "image_invalid", False),
        ("unsupported_image", "image_unsupported", False),
        ("rate_limit_exceeded", "provider_rate_limited", True),
        ("server_error", "provider_unavailable", True),
        ("unknown_bounded_failure", "provider_protocol_error", False),
    ],
)
async def test_sse_provider_failures_share_stable_redacted_classification(
    provider_code: str,
    expected_code: str,
    retryable: bool,
) -> None:
    secret = "private response body"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.failed",
                    "sequence_number": 1,
                    "response": {"status": "failed", "error": {"code": provider_code, "message": secret}},
                },
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert events[-1].error is not None and events[-1].error.code == expected_code
    assert events[-1].error.retryable is retryable
    assert secret not in repr(events[-1])


@pytest.mark.asyncio
async def test_sse_protocol_failure_names_the_closed_check_without_reflecting_content() -> None:
    secret = "private provider event content"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "response.output_item.added",
                    "sequence_number": 0,
                    "output_index": 0,
                    "item": {"type": "message", "content": secret},
                },
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert events[-1].error is not None
    assert events[-1].error.code == "provider_protocol_error"
    assert events[-1].error.details["protocolReason"] == "non_monotonic_provider_sequence"
    assert secret not in repr(events[-1])


@pytest.mark.asyncio
async def test_stream_error_followed_by_response_failed_converges_on_the_structured_failure() -> None:
    secret = "private provider failure body"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                {
                    "type": "error",
                    "sequence_number": 1,
                    "error": {"code": "server_error", "message": secret},
                },
                {
                    "type": "response.failed",
                    "sequence_number": 2,
                    "response": {
                        "status": "failed",
                        "error": {"code": "server_error", "message": secret},
                    },
                },
            ),
        )

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert [event.kind for event in events] == [ModelEventKind.STARTED, ModelEventKind.ERROR]
    assert events[-1].error is not None
    assert events[-1].error.code == "provider_unavailable"
    assert events[-1].error.retryable is True
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

    assert events[-1].error is not None and events[-1].error.code == "provider_protocol_error"
    assert secret not in repr(events[-1])


@pytest.mark.asyncio
async def test_schema_invalid_object_is_emitted_for_canonical_agent_step_validation() -> None:
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

    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.STRUCTURED_OUTPUT,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]
    assert events[1].data == {"unexpected": "private payload"}


@pytest.mark.asyncio
async def test_non_json_structured_output_finishes_without_exposing_untrusted_text() -> None:
    invalid = "not-json private model body"

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

    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]
    assert invalid not in repr(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", ["high", "original"])
async def test_image_block_is_encoded_as_an_ephemeral_responses_data_url(detail: str) -> None:
    captured: dict[str, object] = {}
    png = b"\x89PNG\r\n\x1a\n" + b"offeragent-image"

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created", "sequence_number": 0, "response": {}},
                _completed("ok"),
            ),
        )

    request = replace(
        _request(),
        messages=(
            ModelMessage(ModelRole.SYSTEM, (ModelContentBlock.text("system boundary"),)),
            ModelMessage(
                ModelRole.USER,
                (
                    ModelContentBlock.text("inspect this image"),
                    ModelContentBlock(
                        "image",
                        {
                            "artifactId": "art_one",
                            "mediaType": "image/png",
                            "contentHash": "sha256:" + hashlib.sha256(png).hexdigest(),
                            "detail": detail,
                        },
                        binary_data=png,
                    ),
                ),
            ),
        ),
    )
    await _collect(_gateway(httpx.MockTransport(handler)), request)

    inputs = captured["input"]
    assert isinstance(inputs, list)
    content = inputs[-1]["content"]
    assert content[1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "detail": detail,
    }
    assert "binary_data" not in repr(request.messages)


@pytest.mark.parametrize("role", [ModelRole.SYSTEM, ModelRole.ASSISTANT])
@pytest.mark.asyncio
async def test_non_user_image_is_rejected_before_provider_http(role: ModelRole) -> None:
    calls = 0
    png = b"\x89PNG\r\n\x1a\n" + b"offeragent-image"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_sse(_completed("must-not-run")), request=request)

    request = replace(
        _request(),
        messages=(
            ModelMessage(
                role,
                (
                    ModelContentBlock(
                        "image",
                        {
                            "mediaType": "image/png",
                            "contentHash": "sha256:" + hashlib.sha256(png).hexdigest(),
                            "detail": "high",
                        },
                        binary_data=png,
                    ),
                ),
            ),
        ),
    )

    events = await _collect(_gateway(httpx.MockTransport(handler)), request)

    assert calls == 0
    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None and events[-1].error.code == "provider_protocol_error"


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

    resolver = _CredentialSource()
    config = OpenAIResponsesConfig(
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


@pytest.mark.asyncio
async def test_retryable_stream_failure_retries_before_output_with_one_secret_consume() -> None:
    calls = 0
    secret = "private transient stream failure"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {"type": "response.created", "sequence_number": 0, "response": {}},
                    {
                        "type": "error",
                        "sequence_number": 1,
                        "error": {"code": "server_error", "message": secret},
                    },
                    {
                        "type": "response.failed",
                        "sequence_number": 2,
                        "response": {
                            "status": "failed",
                            "error": {"code": "server_error", "message": secret},
                        },
                    },
                ),
            )
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

    resolver = _CredentialSource()
    config = replace(
        _config(),
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
    assert secret not in repr(events)


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


@pytest.mark.asyncio
async def test_http_conflict_remains_retryable_without_reflecting_response_body() -> None:
    calls = 0
    secret = "private conflict body"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                409,
                headers={"content-type": "application/json"},
                json={"error": {"code": "conflict", "message": secret}},
            )
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

    config = replace(
        _config(),
        max_retries=1,
        retry_base_seconds=0,
        retry_max_seconds=0,
        retry_jitter_ratio=0,
    )
    events = await _collect(_gateway(httpx.MockTransport(handler), config=config), _request())

    assert calls == 2
    assert events[-1].kind is ModelEventKind.COMPLETED
    assert secret not in repr(events)


@pytest.mark.asyncio
async def test_remote_protocol_failure_is_not_misclassified_as_network_unreachable() -> None:
    secret = "private remote protocol text"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError(secret, request=request)

    events = await _collect(_gateway(httpx.MockTransport(handler)), _request())

    assert events[-1].error is not None and events[-1].error.code == "provider_protocol_error"
    assert secret not in repr(events[-1])


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

    resolver = _CredentialSource()
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


def test_endpoint_policy_allows_only_the_fixed_codex_subscription_endpoint() -> None:
    config = _config()
    policy = StaticModelEndpointPolicy(frozenset({config.endpoint}))
    policy.authorize(provider_id=config.provider_id, endpoint=config.endpoint)
    with pytest.raises(ValueError):
        policy.authorize(provider_id=config.provider_id, endpoint="https://attacker.example/v1/responses")


def test_adapter_config_has_no_provider_endpoint_or_secret_decisions() -> None:
    assert (
        "provider_id" not in OpenAIResponsesConfig.__dataclass_fields__
        or not OpenAIResponsesConfig.__dataclass_fields__["provider_id"].init
    )
    assert not OpenAIResponsesConfig.__dataclass_fields__["base_url"].init
    for retired in ("secret_scope_id", "credential_handle", "require_credential", "organization_id", "project_id"):
        assert retired not in OpenAIResponsesConfig.__dataclass_fields__
