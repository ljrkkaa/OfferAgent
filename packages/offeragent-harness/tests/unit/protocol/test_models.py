from __future__ import annotations

import copy
import json
from types import MappingProxyType
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.capabilities import (
    CapabilityName,
    CapabilitySet,
    ProtocolRange,
    negotiate_protocol,
)
from offeragent_harness.protocol.content import (
    ContentBlock,
    DocumentContentBlock,
    DocumentMediaType,
    DocumentPageLocator,
    FileRef,
    RelativeVaultPath,
    SourceRef,
    TextContentBlock,
    VaultSourceRef,
)
from offeragent_harness.protocol.documents import (
    DocumentExtractionCompletedPayload,
    DocumentExtractionFailedPayload,
    DocumentExtractionFailureCode,
    DocumentExtractionStartedPayload,
)
from offeragent_harness.protocol.errors import ErrorCode, ProtocolViolation
from offeragent_harness.protocol.events import EVENT_REGISTRY, EventEnvelope, EventType, ToolCompletedPayload
from offeragent_harness.protocol.ids import Rfc3339DateTime, Sha256Digest, WorkspaceId
from offeragent_harness.protocol.jsonrpc import (
    BidirectionalRequestIds,
    JsonRpcRequest,
    RequestDirection,
    decode_json_document,
    parse_jsonrpc_message,
    validate_request,
)
from offeragent_harness.protocol.messages import (
    ALL_METHOD_REGISTRY,
    COMMAND_REGISTRY,
    EventsReplayParams,
    EventsReplayResult,
    InitializeParams,
    TurnStartParams,
    validate_command_params,
)
from offeragent_harness.protocol.schemas import build_examples

EXPECTED_COMMANDS = {
    "initialize",
    "runtime/ping",
    "runtime/status",
    "secrets/list",
    "secrets/put",
    "secrets/delete",
    "config/get",
    "config/update",
    "skills/list",
    "skills/status",
    "shell/list",
    "shell/install",
    "shell/confirm",
    "shell/set-enabled",
    "process/registrations/list",
    "process/registrations/probe",
    "process/registrations/confirm",
    "process/registrations/delete",
    "hooks/list",
    "hooks/install",
    "hooks/confirm-layer",
    "hooks/confirm-workspace-command",
    "models/list",
    "models/health",
    "session/create",
    "session/list",
    "session/get",
    "session/rename",
    "session/delete",
    "session/fork",
    "session/compact",
    "turn/start",
    "turn/get",
    "turn/cancel",
    "turn/retry",
    "turn/steer",
    "approval/resolve",
    "agent/status",
    "agent/result",
    "agent/cancel",
    "events/replay",
    "artifact/read",
    "diagnostics/get",
    "diagnostics/snapshot",
    "diagnostics/export-preview",
    "diagnostics/export",
    "shutdown",
}


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast(dict[str, object], value)


def test_command_registry_is_complete_and_immutable() -> None:
    assert isinstance(COMMAND_REGISTRY, MappingProxyType)
    assert set(COMMAND_REGISTRY) == EXPECTED_COMMANDS
    assert ALL_METHOD_REGISTRY is COMMAND_REGISTRY
    with pytest.raises(TypeError):
        COMMAND_REGISTRY["not/allowed"] = COMMAND_REGISTRY["initialize"]  # type: ignore[index]


def test_every_registered_dto_uses_the_closed_frozen_strict_base() -> None:
    for spec in ALL_METHOD_REGISTRY.values():
        for model in (spec.params_model, spec.result_model):
            assert issubclass(model, WireModel)
            assert model.model_config["extra"] == "forbid"
            assert model.model_config["frozen"] is True
            assert model.model_config["strict"] is True
            assert model.model_config["alias_generator"] is not None


def test_unknown_command_field_is_rejected_before_dispatch() -> None:
    example = build_examples()["turn-start.request.json"]
    params = dict(_object(example["params"]))
    params["futureUnsafeFlag"] = True
    with pytest.raises(ProtocolViolation) as caught:
        validate_command_params("turn/start", params)
    assert caught.value.error.code == ErrorCode.PROTOCOL_INVALID_PARAMS
    violations = caught.value.error.details.get("violations")
    assert isinstance(violations, list)
    first_violation = violations[0]
    assert isinstance(first_violation, dict)
    assert first_violation.get("type") == "extra_forbidden"


def test_model_health_requires_an_explicit_admin_request_identity() -> None:
    with pytest.raises(ProtocolViolation) as missing:
        validate_command_params("models/health", {"provider": "openai"})
    assert missing.value.error.code == ErrorCode.PROTOCOL_INVALID_PARAMS

    params = validate_command_params(
        "models/health",
        {"provider": "openai", "clientRequestId": "req_model_health_1"},
    )
    assert params.to_wire()["clientRequestId"] == "req_model_health_1"


def test_models_are_frozen_and_emit_camel_case_wire_names() -> None:
    params = validate_command_params(
        "initialize",
        build_examples()["initialize.request.json"]["params"],
    )
    assert isinstance(params, InitializeParams)
    assert "protocolVersion" in params.to_wire()
    assert "protocol_version" not in params.to_wire()
    with pytest.raises(ValidationError):
        params.protocol_version = "1.1"


@pytest.mark.parametrize(
    ("adapter", "valid", "invalid"),
    [
        (TypeAdapter(WorkspaceId), "ws_alpha-01", "session_alpha"),
        (TypeAdapter(Sha256Digest), "sha256:" + "0" * 64, "sha256:ABC"),
        (TypeAdapter(Rfc3339DateTime), "2026-07-12T10:00:00+08:00", "2026-07-12 10:00:00"),
    ],
)
def test_named_wire_primitives_validate_without_coercion(
    adapter: TypeAdapter[object], valid: str, invalid: str
) -> None:
    assert adapter.validate_json(f'"{valid}"') == valid
    with pytest.raises(ValidationError):
        adapter.validate_json(f'"{invalid}"')
    with pytest.raises(ValidationError):
        adapter.validate_python(123, strict=True)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-02-30T10:00:00Z",
        "2026-07-12T25:00:00Z",
        "2026-07-12T10:00:00",
        "2026-07-12t10:00:00z",
    ],
)
def test_rfc3339_timestamp_rejects_invalid_calendar_or_offset(timestamp: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Rfc3339DateTime).validate_json(f'"{timestamp}"')


@pytest.mark.parametrize(
    "path",
    [
        "../secret.md",
        "raw/../secret.md",
        "/absolute.md",
        "C:/vault/a.md",
        "note.md:secret",
        "a\\b.md",
        "notes/CON.md",
        "notes/trailing./file.md",
        "notes/trailing /file.md",
        "notes/question?.md",
    ],
)
def test_relative_vault_path_rejects_escape_and_windows_special_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(RelativeVaultPath).validate_json(f'"{path}"')


def test_content_blocks_and_source_refs_are_discriminated_and_closed() -> None:
    block: ContentBlock = TypeAdapter(ContentBlock).validate_json(
        """
        {
          "type": "text",
          "text": "有来源的答案",
          "references": [{
            "type": "vault",
            "file": {"workspaceId": "ws_main", "path": "notes/Agent.md"},
            "freshness": "fresh"
          }]
        }
        """
    )
    assert isinstance(block, TextContentBlock)
    source = block.references[0]
    assert isinstance(source, VaultSourceRef)
    assert source.file.path == "notes/Agent.md"
    with pytest.raises(ValidationError):
        TypeAdapter(ContentBlock).validate_json('{"type":"text","text":"x","unexpected":true}')


def test_document_content_block_requires_an_explicit_supported_media_type_and_hash() -> None:
    block: ContentBlock = TypeAdapter(ContentBlock).validate_json(
        """
        {
          "type": "document",
          "file": {
            "workspaceId": "ws_main",
            "path": "OfferAgent Sources/interview.pdf",
            "contentHash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
          },
          "mediaType": "application/pdf"
        }
        """
    )
    assert isinstance(block, DocumentContentBlock)
    assert block.media_type is DocumentMediaType.PDF

    with pytest.raises(ValidationError, match="contentHash"):
        TypeAdapter(ContentBlock).validate_json(
            '{"type":"document","file":{"workspaceId":"ws_main","path":"source.pdf"},"mediaType":"application/pdf"}'
        )
    with pytest.raises(ValidationError):
        TypeAdapter(ContentBlock).validate_json(
            '{"type":"document","file":{"workspaceId":"ws_main","path":"source.gif",'
            '"contentHash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},'
            '"mediaType":"image/gif"}'
        )
    with pytest.raises(ValidationError):
        TypeAdapter(ContentBlock).validate_json(
            '{"type":"document","file":{"workspaceId":"ws_main","path":"source.pdf",'
            '"contentHash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            '"lineStart":1},"mediaType":"application/pdf"}'
        )


def test_file_ref_requires_an_ordered_line_range() -> None:
    with pytest.raises(ValidationError):
        FileRef.model_validate_json('{"workspaceId":"ws_main","path":"a.md","lineStart":9,"lineEnd":3}')


def test_page_source_locator_is_one_based_ordered_and_cannot_mix_with_text_locators() -> None:
    source: SourceRef = TypeAdapter(SourceRef).validate_json(
        """
        {
          "type": "vault",
          "file": {"workspaceId": "ws_main", "path": "source.pdf"},
          "locator": {"type": "page", "pageStart": 2, "pageEnd": 4}
        }
        """
    )
    assert isinstance(source, VaultSourceRef)
    assert isinstance(source.locator, DocumentPageLocator)
    assert source.locator.page_start == 2

    with pytest.raises(ValidationError, match="pageEnd"):
        DocumentPageLocator.model_validate_json('{"type":"page","pageStart":4,"pageEnd":2}')
    with pytest.raises(ValidationError, match="cannot be combined"):
        TypeAdapter(SourceRef).validate_json(
            '{"type":"vault","file":{"workspaceId":"ws_main","path":"source.pdf","lineStart":1},'
            '"locator":{"type":"page","pageStart":1,"pageEnd":1}}'
        )


def test_vault_source_ref_accepts_combined_stale_partial_freshness() -> None:
    source = VaultSourceRef.model_validate_json(
        '{"type":"vault","file":{"workspaceId":"ws_main","path":"notes/source.md"},"freshness":"stale_partial"}'
    )
    assert source.freshness.value == "stale_partial"


def test_event_registry_is_complete_concrete_and_immutable() -> None:
    assert isinstance(EVENT_REGISTRY, MappingProxyType)
    assert set(EVENT_REGISTRY) == set(EventType)
    assert all(issubclass(payload, WireModel) for payload in EVENT_REGISTRY.values())
    assert EVENT_REGISTRY[EventType.TOOL_COMPLETED] is ToolCompletedPayload
    with pytest.raises(TypeError):
        EVENT_REGISTRY[EventType.TOOL_COMPLETED] = WireModel  # type: ignore[index]


def test_event_type_and_payload_cannot_be_mismatched_or_extended() -> None:
    raw = _object(build_examples()["tool-completed.event.json"]["params"])
    event = EventEnvelope.model_validate_json(json.dumps(raw))
    assert isinstance(event.payload, ToolCompletedPayload)

    mismatched = dict(raw)
    mismatched["type"] = "assistant.delta"
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate_json(json.dumps(mismatched))

    extended = copy.deepcopy(raw)
    extended_payload = _object(extended["payload"])
    extended_payload["unknown"] = True
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate_json(json.dumps(extended))

    wrong_terminal_kind = copy.deepcopy(raw)
    wrong_terminal_kind["type"] = "tool.failed"
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate_json(json.dumps(wrong_terminal_kind))


def test_document_extraction_lifecycle_examples_are_strict_durable_events() -> None:
    examples = build_examples()
    started = EventEnvelope.model_validate_json(
        json.dumps(_object(examples["document-extraction-started.event.json"]["params"]))
    )
    completed = EventEnvelope.model_validate_json(
        json.dumps(_object(examples["document-extraction-completed.event.json"]["params"]))
    )
    failed = EventEnvelope.model_validate_json(
        json.dumps(_object(examples["document-extraction-failed.event.json"]["params"]))
    )
    assert isinstance(started.payload, DocumentExtractionStartedPayload)
    assert started.payload.input_block_index == 1
    assert isinstance(completed.payload, DocumentExtractionCompletedPayload)
    assert completed.payload.page_provenance[1].locator.page_start == 2
    assert isinstance(failed.payload, DocumentExtractionFailedPayload)
    assert failed.payload.failure.code is DocumentExtractionFailureCode.DOCUMENT_ENCRYPTED

    extended = copy.deepcopy(_object(examples["document-extraction-failed.event.json"]["params"]))
    failure = _object(_object(extended["payload"])["failure"])
    failure["unstableMessageKey"] = "not allowed"
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate_json(json.dumps(extended))


@pytest.mark.parametrize(
    "payload",
    [
        {
            "documentId": "doc_1",
            "textArtifactId": "art_1",
            "pageCount": 1,
            "pageProvenance": [
                {
                    "locator": {"type": "page", "pageStart": 2, "pageEnd": 2},
                    "extractionMethod": "ocr",
                    "utf8StartByte": 0,
                    "utf8EndByte": 2,
                }
            ],
        },
        {
            "documentId": "doc_1",
            "textArtifactId": "art_1",
            "pageCount": 2,
            "pageProvenance": [
                {
                    "locator": {"type": "page", "pageStart": 1, "pageEnd": 1},
                    "extractionMethod": "ocr",
                    "utf8StartByte": 4,
                    "utf8EndByte": 8,
                },
                {
                    "locator": {"type": "page", "pageStart": 2, "pageEnd": 2},
                    "extractionMethod": "ocr",
                    "utf8StartByte": 7,
                    "utf8EndByte": 12,
                },
            ],
        },
        {
            "documentId": "doc_1",
            "textArtifactId": "art_1",
            "pageCount": 1,
            "pageProvenance": [
                {
                    "locator": {"type": "page", "pageStart": 1, "pageEnd": 1},
                    "extractionMethod": "embedded_text",
                    "utf8StartByte": 0,
                    "utf8EndByte": 2,
                    "confidence": 0.9,
                }
            ],
        },
    ],
)
def test_document_extraction_completion_rejects_invalid_provenance(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DocumentExtractionCompletedPayload.model_validate_json(json.dumps(payload))


def test_document_extraction_failure_codes_reject_unknown_values() -> None:
    with pytest.raises(ValidationError):
        DocumentExtractionFailedPayload.model_validate_json(
            '{"documentId":"doc_1","failure":{"code":"best_effort_fallback","retryable":false,'
            '"userVisibleMessage":"not stable"}}'
        )


def test_capability_negotiation_selects_highest_common_minor_and_intersection() -> None:
    result = negotiate_protocol(
        client_preferred="1.4",
        client_range=ProtocolRange(minimum="1.1", maximum="1.4"),
        client_capabilities=CapabilitySet(event_replay=True, shell=True),
        client_required_capabilities=[CapabilityName.EVENT_REPLAY],
        client_schema_hash="sha256:" + "a" * 64,
        server_preferred="1.3",
        server_range=ProtocolRange(minimum="1.0", maximum="1.3"),
        server_capabilities=CapabilitySet(event_replay=True),
        server_schema_hash="sha256:" + "a" * 64,
    )
    assert result.protocol_version == "1.3"
    assert result.capabilities.event_replay is True
    assert result.capabilities.shell is False
    assert result.disabled_optional_capabilities == [CapabilityName.SHELL]


def test_document_ingestion_is_an_explicit_negotiated_capability() -> None:
    advertised = CapabilitySet(document_ingestion=True)
    assert advertised.enabled() == {CapabilityName.DOCUMENT_INGESTION}
    assert advertised.to_wire()["documentIngestion"] is True
    assert advertised.intersection(CapabilitySet()).document_ingestion is False


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            {"server_preferred": "2.0", "server_range": ProtocolRange(minimum="2.0", maximum="2.1")},
            ErrorCode.PROTOCOL_INCOMPATIBLE_VERSION,
        ),
        ({"server_schema_hash": "sha256:" + "b" * 64}, ErrorCode.PROTOCOL_SCHEMA_MISMATCH),
        ({"client_required_capabilities": [CapabilityName.SHELL]}, ErrorCode.PROTOCOL_MISSING_CAPABILITY),
    ],
)
def test_capability_negotiation_fails_closed(change: dict[str, object], code: ErrorCode) -> None:
    arguments: dict[str, object] = {
        "client_preferred": "1.0",
        "client_range": None,
        "client_capabilities": CapabilitySet(event_replay=True, shell=True),
        "client_required_capabilities": [],
        "client_schema_hash": "sha256:" + "a" * 64,
        "server_preferred": "1.0",
        "server_range": ProtocolRange(minimum="1.0", maximum="1.2"),
        "server_capabilities": CapabilitySet(event_replay=True),
        "server_schema_hash": "sha256:" + "a" * 64,
    }
    arguments.update(change)
    with pytest.raises(ProtocolViolation) as caught:
        negotiate_protocol(**arguments)  # type: ignore[arg-type]
    assert caught.value.error.code == code


def test_json_parser_rejects_duplicate_members_nonfinite_numbers_and_unknown_envelope_fields() -> None:
    with pytest.raises(ProtocolViolation) as duplicate:
        decode_json_document(b'{"jsonrpc":"2.0","jsonrpc":"2.0"}')
    assert duplicate.value.error.code == ErrorCode.PROTOCOL_INVALID_JSON

    with pytest.raises(ProtocolViolation) as nan:
        decode_json_document(b'{"jsonrpc":"2.0","id":NaN}')
    assert nan.value.error.code == ErrorCode.PROTOCOL_INVALID_JSON

    with pytest.raises(ProtocolViolation) as unknown:
        parse_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "runtime/status", "params": {}, "extra": 1})
    assert unknown.value.error.code == ErrorCode.PROTOCOL_INVALID_REQUEST

    with pytest.raises(ProtocolViolation) as missing_receipt:
        parse_jsonrpc_message({"jsonrpc": "2.0", "method": "runtime/status", "params": {}})
    assert missing_receipt.value.error.code == ErrorCode.PROTOCOL_INVALID_REQUEST


def test_request_validation_is_method_aware() -> None:
    raw = build_examples()["turn-start.request.json"]
    message = parse_jsonrpc_message(raw)
    assert isinstance(message, JsonRpcRequest)
    validated = validate_request(message)
    assert isinstance(validated.params, TurnStartParams)

    unknown_method = JsonRpcRequest.model_validate_json(
        '{"jsonrpc":"2.0","id":1,"method":"model/directCall","params":{}}'
    )
    with pytest.raises(ProtocolViolation) as caught:
        validate_request(unknown_method)
    assert caught.value.error.code == ErrorCode.PROTOCOL_METHOD_NOT_FOUND


def test_event_replay_scopes_have_unambiguous_cursor_shapes() -> None:
    run = EventsReplayParams(run_id="run_1", after_sequence=7)
    assert run.after_sequence == 7 and run.run_cursors == {}
    session = EventsReplayParams(session_id="ses_1", run_cursors={"run_1": 7, "run_2": 3})
    assert session.after_sequence == 0 and session.run_cursors["run_2"] == 3
    with pytest.raises(ValidationError, match="exactly one"):
        EventsReplayParams(session_id="ses_1", run_id="run_1")
    with pytest.raises(ValidationError, match="only for Session"):
        EventsReplayParams(run_id="run_1", run_cursors={"run_1": 1})
    with pytest.raises(ValidationError, match="only for one Run"):
        EventsReplayParams(session_id="ses_1", after_sequence=1)
    assert EventsReplayResult(events=[], last_sequence=7, has_more=False).run_cursors == {}
    assert EventsReplayResult(events=[], run_cursors={"run_1": 7}, has_more=False).last_sequence is None


def test_bidirectional_request_ids_are_independent_and_detect_duplicates() -> None:
    pending = BidirectionalRequestIds()
    pending.register(RequestDirection.LOCAL, "rpc_1")
    pending.register(RequestDirection.REMOTE, "rpc_1")
    with pytest.raises(ProtocolViolation) as caught:
        pending.register(RequestDirection.LOCAL, "rpc_1")
    assert caught.value.error.code == ErrorCode.PROTOCOL_DUPLICATE_REQUEST_ID
    assert pending.complete(RequestDirection.LOCAL, "rpc_1") is True
    assert pending.contains(RequestDirection.REMOTE, "rpc_1") is True
    assert pending.complete(RequestDirection.LOCAL, "missing") is False
