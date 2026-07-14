from __future__ import annotations

import copy
import json
from types import MappingProxyType
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from offeragent_harness.foundation import canonical_json_sha256, vault_write_intent_hash
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.capabilities import (
    CapabilityName,
    CapabilitySet,
    ProtocolRange,
    negotiate_protocol,
)
from offeragent_harness.protocol.content import (
    ContentBlock,
    FileRef,
    RelativeVaultPath,
    TextContentBlock,
    VaultSourceRef,
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
    REVERSE_REQUEST_REGISTRY,
    EventsReplayParams,
    EventsReplayResult,
    InitializeParams,
    TurnStartParams,
    validate_command_params,
    validate_command_result,
)
from offeragent_harness.protocol.schemas import build_examples

EXPECTED_COMMANDS = {
    "initialize",
    "runtime/ping",
    "runtime/status",
    "web/launch",
    "secrets/list",
    "secrets/put",
    "secrets/delete",
    "config/get",
    "config/update",
    "skills/list",
    "skills/status",
    "skills/rescan",
    "skills/confirm-trust",
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
    "vault/headless/status",
    "vault/headless/request",
    "vault/headless/activate",
    "vault/headless/revoke",
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

EXPECTED_REVERSE_REQUESTS = {
    "client/context/get",
    "client/tool/commit-observe",
    "client/tool/preview",
    "client/tool/invoke",
    "client/tool/lookup",
    "client/tool/cancel",
    "client/approval/present",
}


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast(dict[str, object], value)


def test_command_registry_is_complete_and_immutable() -> None:
    assert isinstance(COMMAND_REGISTRY, MappingProxyType)
    assert isinstance(REVERSE_REQUEST_REGISTRY, MappingProxyType)
    assert set(COMMAND_REGISTRY) == EXPECTED_COMMANDS
    assert set(REVERSE_REQUEST_REGISTRY) == EXPECTED_REVERSE_REQUESTS
    assert set(ALL_METHOD_REGISTRY) == EXPECTED_COMMANDS | EXPECTED_REVERSE_REQUESTS
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


def test_turn_start_write_intent_is_explicit_closed_and_hash_bound() -> None:
    example = build_examples()["turn-start.request.json"]
    params = dict(_object(example["params"]))
    params["writeIntent"] = {"kind": "none"}
    assert validate_command_params("turn/start", params).to_wire()["writeIntent"] == {"kind": "none"}

    missing = dict(params)
    del missing["writeIntent"]
    with pytest.raises(ProtocolViolation) as absent:
        validate_command_params("turn/start", missing)
    assert absent.value.error.code == ErrorCode.PROTOCOL_INVALID_PARAMS

    binding = {
        "kind": "vault_write_required",
        "targetPaths": ["notes/offer.md", "notes/summary.md"],
    }
    required = dict(params)
    required["writeIntent"] = {
        **binding,
        "intentHash": canonical_json_sha256(binding),
    }
    validated = validate_command_params("turn/start", required)
    assert validated.to_wire()["writeIntent"] == required["writeIntent"]

    wrong_hash = copy.deepcopy(required)
    _object(wrong_hash["writeIntent"])["intentHash"] = "sha256:" + ("0" * 64)
    with pytest.raises(ProtocolViolation) as mismatched:
        validate_command_params("turn/start", wrong_hash)
    assert mismatched.value.error.code == ErrorCode.PROTOCOL_INVALID_PARAMS

    unknown = copy.deepcopy(required)
    _object(unknown["writeIntent"])["allowAnyVaultWrite"] = True
    with pytest.raises(ProtocolViolation) as extra:
        validate_command_params("turn/start", unknown)
    assert extra.value.error.code == ErrorCode.PROTOCOL_INVALID_PARAMS

    unsorted = copy.deepcopy(required)
    _object(unsorted["writeIntent"])["targetPaths"] = ["notes/summary.md", "notes/offer.md"]
    with pytest.raises(ProtocolViolation):
        validate_command_params("turn/start", unsorted)

    unicode_paths = ["notes/\ue000.md", "notes/😀.md"]
    assert vault_write_intent_hash(unicode_paths) == (
        "sha256:30f42b95f8a9f84f5794d603e05de23bb6260532190ac6826bae7c2958f17bbb"
    )
    unicode_required = dict(params)
    unicode_required["writeIntent"] = {
        "kind": "vault_write_required",
        "targetPaths": unicode_paths,
        "intentHash": vault_write_intent_hash(unicode_paths),
    }
    assert (
        validate_command_params("turn/start", unicode_required).to_wire()["writeIntent"]
        == unicode_required["writeIntent"]
    )


def test_model_health_requires_an_explicit_admin_request_identity() -> None:
    with pytest.raises(ProtocolViolation) as missing:
        validate_command_params("models/health", {"provider": "openai"})
    assert missing.value.error.code == ErrorCode.PROTOCOL_INVALID_PARAMS

    params = validate_command_params(
        "models/health",
        {"provider": "openai", "clientRequestId": "req_model_health_1"},
    )
    assert params.to_wire()["clientRequestId"] == "req_model_health_1"


def test_headless_vault_write_protocol_is_closed_and_capability_gated() -> None:
    methods = {
        "vault/headless/status",
        "vault/headless/request",
        "vault/headless/activate",
        "vault/headless/revoke",
    }
    assert all(
        COMMAND_REGISTRY[method].required_capability is CapabilityName.HEADLESS_VAULT_WRITE for method in methods
    )
    assert CapabilitySet(headless_vault_write=True).model_dump(mode="json", by_alias=True)["headlessVaultWrite"] is True

    assert validate_command_params("vault/headless/status", {}).to_wire() == {}
    requested = validate_command_params(
        "vault/headless/request",
        {
            "clientRequestId": "req_headless_protocol_1",
            "confirmation": "obsidian_closed_disk_authoritative",
            "ttlSeconds": 300,
            "expectedBaselineFingerprint": "sha256:" + "a" * 64,
        },
    )
    assert requested.to_wire()["confirmation"] == "obsidian_closed_disk_authoritative"

    with pytest.raises(ProtocolViolation) as wrong_confirmation:
        validate_command_params(
            "vault/headless/request",
            {
                **requested.to_wire(),
                "confirmation": "yes",
            },
        )
    assert wrong_confirmation.value.error.code == ErrorCode.PROTOCOL_INVALID_PARAMS

    with pytest.raises(ProtocolViolation):
        validate_command_params("vault/headless/status", {"assumeObsidianClosed": True})


def test_headless_status_identity_and_administrative_approval_target_are_exact() -> None:
    status = validate_command_result(
        "vault/headless/status",
        {
            "state": "approved",
            "pipeConnectionCount": 0,
            "baselineReliable": True,
            "baselineFingerprint": "sha256:" + "2" * 64,
            "approvalId": "apr_" + "1" * 64,
            "operationId": "op_headless_" + "2" * 32,
            "argsHash": "sha256:" + "3" * 64,
            "revision": 2,
            "expiresAt": "2026-07-13T08:05:00+00:00",
            "reasonCode": "authorization_approved",
            "userMessage": "一次性审批已通过。",
            "canRequest": False,
            "canActivate": True,
            "canRevoke": True,
        },
    )
    assert status.to_wire()["operationId"] == "op_headless_" + "2" * 32

    partial_identity = status.to_wire()
    partial_identity["argsHash"] = None
    with pytest.raises(ProtocolViolation) as partial:
        validate_command_result("vault/headless/status", partial_identity)
    assert partial.value.error.code == ErrorCode.PROTOCOL_SCHEMA_MISMATCH

    administrative = validate_command_result(
        "approval/resolve",
        {
            "approvalId": "apr_" + "4" * 64,
            "status": "approved",
            "runId": None,
            "operationId": "op_headless_" + "5" * 32,
            "resumed": False,
        },
    )
    assert administrative.to_wire()["runId"] is None

    invalid_target = administrative.to_wire()
    invalid_target["operationId"] = None
    with pytest.raises(ProtocolViolation):
        validate_command_result("approval/resolve", invalid_target)


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


def test_event_registry_is_complete_concrete_and_immutable() -> None:
    assert isinstance(EVENT_REGISTRY, MappingProxyType)
    assert set(EVENT_REGISTRY) == set(EventType)
    assert len(EVENT_REGISTRY) == 44
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
        client_capabilities=CapabilitySet(client_tools=True, event_replay=True, shell=True),
        client_required_capabilities=[CapabilityName.CLIENT_TOOLS],
        client_schema_hash="sha256:" + "a" * 64,
        server_preferred="1.3",
        server_range=ProtocolRange(minimum="1.0", maximum="1.3"),
        server_capabilities=CapabilitySet(client_tools=True, event_replay=True),
        server_schema_hash="sha256:" + "a" * 64,
    )
    assert result.protocol_version == "1.3"
    assert result.capabilities.client_tools is True
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
        "client_capabilities": CapabilitySet(client_tools=True, shell=True),
        "client_required_capabilities": [],
        "client_schema_hash": "sha256:" + "a" * 64,
        "server_preferred": "1.0",
        "server_range": ProtocolRange(minimum="1.0", maximum="1.2"),
        "server_capabilities": CapabilitySet(client_tools=True),
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
