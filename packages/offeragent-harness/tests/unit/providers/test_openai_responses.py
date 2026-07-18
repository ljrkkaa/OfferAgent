from __future__ import annotations

import base64
import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import replace
from typing import TypeVar

import httpx
import pytest

from offeragent_harness.config import ModelProvider, ModelSettings
from offeragent_harness.models import (
    ModelCitation,
    ModelContentBlock,
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
)
from offeragent_harness.ports import SecretHandle, SecretKind
from offeragent_harness.providers import (
    CodexResponsesProvider,
    LocalModelProvider,
    ModelProviderConfigurationError,
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
from offeragent_harness.runtime.conversation_attachments import AttachmentLimits
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


def _request(
    *,
    output_mode: ModelOutputMode = ModelOutputMode.TEXT,
    hosted_search: bool = False,
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
        replace(_config(), **{field: value})


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
    assert body["include"] == ["web_search_call.action.sources"]
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
            provider_id="openai",
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

    resolver = _SecretResolver(secret.encode())
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
