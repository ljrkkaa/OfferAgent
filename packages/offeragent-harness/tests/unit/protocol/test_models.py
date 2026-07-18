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
from offeragent_harness.protocol.common import RunConfigSnapshot
from offeragent_harness.protocol.content import (
    ContentBlock,
    FileRef,
    HostedWebSourceRef,
    PinnedContextContentBlock,
    PinnedSelectionContextReference,
    ProjectSourceRef,
    RelativeVaultPath,
    SourceRef,
    TextContentBlock,
    VaultSourceRef,
    WebSourceRef,
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
    AttachmentBeginParams,
    AttachmentChunkParams,
    AttachmentReadParams,
    EventsReplayParams,
    EventsReplayResult,
    InitializeParams,
    ModelDescriptor,
    ModelsListParams,
    PluginToolCompleteParams,
    TurnStartParams,
    validate_command_params,
)
from offeragent_harness.protocol.schemas import build_examples

EXPECTED_COMMANDS = {
    "initialize",
    "runtime/ping",
    "runtime/status",
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
    "plugin-tools/complete",
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
    "attachments/begin",
    "attachments/chunk",
    "attachments/commit",
    "attachments/abort",
    "attachments/read",
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


def test_plugin_tool_completion_command_carries_the_exact_execution_binding() -> None:
    digest = "sha256:" + "a" * 64
    params = validate_command_params(
        "plugin-tools/complete",
        {
            "workspaceId": "ws_vault",
            "runId": "run_contract",
            "definitionFingerprint": digest,
            "argsHash": digest,
            "idempotencyKey": "contract-read-1",
            "result": {
                "toolCallId": "call_contract",
                "status": "succeeded",
                "summary": "Read the Vault Agent Contract.",
                "data": {"content": "# OfferAgent"},
                "artifactRefs": [],
                "sourceRefs": [],
                "sideEffects": [],
                "retryable": False,
                "error": None,
            },
        },
    )

    assert isinstance(params, PluginToolCompleteParams)
    assert params.workspace_id == "ws_vault"
    assert params.result.tool_call_id == "call_contract"


def test_model_protocol_has_no_provider_choice_or_runtime_probe_state() -> None:
    assert set(RunConfigSnapshot.model_fields) == {"model", "reasoning_effort", "permission_mode", "budgets"}
    assert set(ModelsListParams.model_fields) == set()
    assert set(ModelDescriptor.model_fields) == {
        "model",
        "display_name",
        "input_modalities",
        "supports_image_detail_original",
        "supports_hosted_search",
        "web_search_tool_type",
        "context_window",
        "max_context_window",
        "effective_context_window_percent",
        "additional_speed_tiers",
        "service_tiers",
        "default_service_tier",
        "supports_fast_mode",
        "max_context_tokens",
    }
    for retired in ("models/health", "secrets/list", "secrets/put", "secrets/delete", "web/launch"):
        assert retired not in COMMAND_REGISTRY


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


def test_file_ref_requires_an_ordered_line_range() -> None:
    with pytest.raises(ValidationError):
        FileRef.model_validate_json('{"workspaceId":"ws_main","path":"a.md","lineStart":9,"lineEnd":3}')


def test_vault_source_ref_accepts_combined_stale_partial_freshness() -> None:
    source = VaultSourceRef.model_validate_json(
        '{"type":"vault","file":{"workspaceId":"ws_main","path":"notes/source.md"},"freshness":"stale_partial"}'
    )
    assert source.freshness.value == "stale_partial"


def test_project_source_ref_preserves_registry_identity_and_precise_lines() -> None:
    source: SourceRef = TypeAdapter(SourceRef).validate_json(
        """
        {
          "type": "project",
          "projectId": "offeragent",
          "path": "src/agent.py",
          "contentHash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "modifiedVersion": "mtime:17:size:41",
          "lineStart": 7,
          "lineEnd": 9,
          "freshness": "fresh"
        }
        """
    )
    assert isinstance(source, ProjectSourceRef)
    assert source.project_id == "offeragent"
    assert source.path == "src/agent.py"
    assert source.line_start == 7


def test_web_source_ref_preserves_clickable_research_provenance() -> None:
    source: SourceRef = TypeAdapter(SourceRef).validate_json(
        """
        {
          "type": "web",
          "url": "https://example.com/interview/42",
          "contentHash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "title": "Acme backend interview",
          "freshness": "fresh"
        }
        """
    )
    assert isinstance(source, WebSourceRef)
    assert str(source.url) == "https://example.com/interview/42"


def test_hosted_web_source_ref_is_provider_attested_without_fake_content_hash() -> None:
    source: SourceRef = TypeAdapter(SourceRef).validate_json(
        """
        {
          "type": "hostedWeb",
          "url": "https://example.com/interview/42",
          "title": "Acme backend interview",
          "providerId": "codex-subscription",
          "model": "gpt-catalog-model",
          "modelRequestId": "model-request-42",
          "freshness": "unknown"
        }
        """
    )
    assert isinstance(source, HostedWebSourceRef)
    assert "contentHash" not in source.to_wire()


@pytest.mark.parametrize(
    "url",
    (
        "https://user@example.com/interview/42",
        "https://user:secret@example.com/interview/42",
    ),
)
def test_hosted_web_source_ref_rejects_urls_with_credentials(url: str) -> None:
    with pytest.raises(ValidationError, match="credentials"):
        HostedWebSourceRef.model_validate(
            {
                "type": "hostedWeb",
                "url": url,
                "title": "Credential-bearing source",
                "provider_id": "codex-subscription",
                "model": "gpt-catalog-model",
                "model_request_id": "model-request-42",
            }
        )


def test_turn_start_accepts_at_most_eight_safe_pinned_source_locators() -> None:
    raw = copy.deepcopy(_object(build_examples()["turn-start.request.json"]["params"]))
    raw["pinnedContext"] = [
        {"kind": "document", "path": "notes/preferred.md"},
        {"kind": "selection", "path": "notes/range.md", "lineStart": 4, "lineEnd": 8},
    ]
    params = TurnStartParams.model_validate_json(json.dumps(raw))
    assert isinstance(params.pinned_context[1], PinnedSelectionContextReference)
    assert params.pinned_context[1].line_start == 4

    raw["pinnedContext"] = [{"kind": "document", "path": f"notes/{index}.md"} for index in range(9)]
    with pytest.raises(ValidationError):
        TurnStartParams.model_validate_json(json.dumps(raw))
    raw["pinnedContext"] = [{"kind": "document", "path": "../outside.md"}]
    with pytest.raises(ValidationError):
        TurnStartParams.model_validate_json(json.dumps(raw))


def test_pinned_context_is_a_distinct_replayable_content_block() -> None:
    value: ContentBlock = TypeAdapter(ContentBlock).validate_python(
        {
            "type": "pinnedContext",
            "references": [
                {"kind": "document", "path": "notes/preferred.md"},
                {"kind": "selection", "path": "notes/range.md", "lineStart": 4, "lineEnd": 8},
            ],
        }
    )

    assert isinstance(value, PinnedContextContentBlock)
    assert len(value.references) == 2


def test_attachment_commands_bound_transfer_without_exposing_paths() -> None:
    sha = "sha256:" + "a" * 64
    begun = AttachmentBeginParams.model_validate(
        {
            "sessionId": "ses_one",
            "clientRequestId": "req_upload",
            "fileName": "evidence.png",
            "mediaType": "image/png",
            "byteLength": 1024,
            "contentHash": sha,
        }
    )
    chunk = AttachmentChunkParams.model_validate(
        {
            "sessionId": "ses_one",
            "uploadId": "upload_one",
            "offset": 0,
            "contentBase64": "aGVsbG8=",
            "contentHash": "sha256:" + "2" * 64,
        }
    )
    read = AttachmentReadParams.model_validate(
        {"sessionId": "ses_one", "artifactId": "art_one", "offset": 0, "maxBytes": 65_536}
    )

    assert begun.file_name == "evidence.png"
    assert chunk.content_base64 == "aGVsbG8="
    assert read.max_bytes == 65_536
    with pytest.raises(ValidationError):
        AttachmentChunkParams.model_validate(
            {
                "sessionId": "ses_one",
                "uploadId": "upload_one",
                "offset": 0,
                "contentBase64": "A" * 100_000,
                "contentHash": sha,
            }
        )


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
