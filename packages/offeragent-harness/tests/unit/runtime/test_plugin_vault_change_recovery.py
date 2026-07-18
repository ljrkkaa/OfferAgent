from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.runtime.plugin_tools import plugin_tool_definitions
from offeragent_harness.runtime.plugin_vault_change_recovery import (
    PluginVaultChangeRecoveryError,
    PluginVaultChangeRecoveryLookup,
)
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools import (
    SideEffectState,
    ToolCall,
    ToolDefinition,
    ToolResultStatus,
    canonical_json_sha256,
)

TOKEN = "a" * 64
WORKSPACE_ID = "ws_vault"
RUN_ID = "run_interview"
ROOT_RUN_ID = "run_interview"
BATCH_ID = "batch_interview_recovery"
NEW_CONTENT_HASH = "sha256:" + hashlib.sha256(b"new\n").hexdigest()


def _definition() -> ToolDefinition:
    return next(item for item in plugin_tool_definitions() if item.name == "vault.changes.apply")


def _arguments() -> dict[str, Any]:
    return {
        "batchId": BATCH_ID,
        "task": "Reconcile one Interview Submission",
        "changeKind": "interview_submission",
        "sourceBindings": [],
        "interviewSubmission": {
            "sourceKind": "public_url",
            "capturedOn": "2026-07-18",
            "canonicalUrls": ["https://example.com/interview/42"],
            "orderedImageContentHashes": [],
            "sourceFingerprint": None,
            "reviewItems": [
                {
                    "kind": "experience",
                    "path": "experiences/acme.md",
                    "identity": "new",
                    "mutation": "create",
                }
            ],
        },
        "operations": [
            {
                "op": "create",
                "path": "experiences/acme.md",
                "content": "new\n",
                "expectedContentHash": "absent",
                "expectedModifiedVersion": "missing",
            }
        ],
    }


def _call() -> ToolCall:
    definition = _definition()
    arguments = _arguments()
    return ToolCall(
        tool_call_id="call_interview_apply",
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="interview-apply-once",
        deadline=datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
        lineage=AgentLineage.root(ROOT_RUN_ID),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


def _call_with_arguments(arguments: dict[str, Any]) -> ToolCall:
    return replace(_call(), arguments=arguments, args_hash=canonical_json_sha256(arguments))


def _target() -> dict[str, Any]:
    return {
        "operation": "create",
        "path": "experiences/acme.md",
        "beforeHash": "absent",
        "afterHash": NEW_CONTENT_HASH,
        "beforeModifiedVersion": "missing",
        "afterModifiedVersion": "mtime:1:size:4",
    }


def _record(call: ToolCall, state: str = "applied") -> dict[str, Any]:
    return {
        "version": 2,
        "batchId": BATCH_ID,
        "toolCallId": call.tool_call_id,
        "workspaceId": call.workspace_id,
        "runId": call.run_id,
        "rootRunId": call.lineage.root_run_id,
        "changeKind": "interview_submission",
        "reviewHash": f"sha256:{'c' * 64}",
        "argsHash": call.args_hash,
        "idempotencyKey": call.idempotency_key,
        "state": state,
        "checkpointRef": f"refs/offeragent/checkpoints/{BATCH_ID}" if state == "applied" else None,
        "targets": [_target()],
        "appliedPaths": ["experiences/acme.md"] if state == "applied" else [],
        "manualReviewPaths": [],
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")


def _lookup(tmp_path: Path) -> tuple[PluginVaultChangeRecoveryLookup, Path]:
    vault = tmp_path / "Vault"
    journal = vault / ".obsidian" / "offeragent" / "vault-change-journal"
    journal.mkdir(parents=True)
    return PluginVaultChangeRecoveryLookup(vault, journal, TOKEN), journal


def _seal(journal: Path, *batch_ids: str, token: str = TOKEN) -> None:
    seal_directory = journal / ".recovery-seals" / "current"
    seal_directory.mkdir(parents=True)
    for batch_id in sorted(batch_ids):
        raw = (journal / f"{batch_id}.json").read_bytes()
        _write_json(
            seal_directory / _seal_name(batch_id),
            {
                "schemaVersion": 1,
                "recoveryToken": token,
                "batchId": batch_id,
                "contentHash": f"sha256:{hashlib.sha256(raw).hexdigest()}",
                "byteLength": len(raw),
            },
        )
    _write_json(
        journal / ".recovery-ready.json",
        {"schemaVersion": 2, "recoveryToken": token},
    )


def _seal_name(batch_id: str) -> str:
    return f"{hashlib.sha256(batch_id.encode()).hexdigest()}.json"


@pytest.mark.asyncio
async def test_exact_applied_interview_batch_is_reconstructed_without_execution(tmp_path: Path) -> None:
    lookup, journal = _lookup(tmp_path)
    call = _call()
    _write_json(journal / f"{BATCH_ID}.json", _record(call))
    _seal(journal, BATCH_ID)

    result = await lookup.lookup_result(_definition(), call)

    assert result is not None and result.status is ToolResultStatus.SUCCEEDED
    assert result.tool_call_id == call.tool_call_id
    assert isinstance(result.data, Mapping)
    assert result.data["batchId"] == BATCH_ID
    assert result.data["state"] == "applied"
    assert result.data["paths"] == ("experiences/acme.md",)
    assert len(result.side_effects) == 1
    assert result.side_effects[0].state is SideEffectState.COMMITTED
    assert result.side_effects[0].resource_id == "experiences/acme.md"
    assert dict(result.side_effects[0].metadata) == {
        "protocolKind": "file_created",
        "reconciledFrom": "plugin_vault_change_journal",
    }


@pytest.mark.asyncio
async def test_create_after_hash_must_match_the_original_content(tmp_path: Path) -> None:
    lookup, journal = _lookup(tmp_path)
    call = _call()
    record = _record(call)
    record["targets"][0]["afterHash"] = f"sha256:{'d' * 64}"
    _write_json(journal / f"{BATCH_ID}.json", record)
    _seal(journal, BATCH_ID)

    with pytest.raises(PluginVaultChangeRecoveryError, match="target binding"):
        await lookup.lookup_result(_definition(), call)


@pytest.mark.asyncio
async def test_recovered_side_effects_preserve_protocol_mutation_kinds(tmp_path: Path) -> None:
    lookup, journal = _lookup(tmp_path)
    arguments = _arguments()
    arguments["operations"] = [
        {
            "op": "create",
            "path": "notes/new.md",
            "content": "new\n",
            "expectedContentHash": "absent",
            "expectedModifiedVersion": "missing",
        },
        {
            "op": "append",
            "path": "notes/current.md",
            "content": "more\n",
            "expectedContentHash": f"sha256:{'1' * 64}",
            "expectedModifiedVersion": "mtime:1:size:4",
        },
        {
            "op": "delete",
            "path": "notes/old.md",
            "expectedContentHash": f"sha256:{'2' * 64}",
            "expectedModifiedVersion": "mtime:2:size:4",
        },
    ]
    call = _call_with_arguments(arguments)
    record = _record(call)
    record["targets"] = [
        {
            "operation": "create",
            "path": "notes/new.md",
            "beforeHash": "absent",
            "afterHash": NEW_CONTENT_HASH,
            "beforeModifiedVersion": "missing",
            "afterModifiedVersion": "mtime:3:size:4",
        },
        {
            "operation": "append",
            "path": "notes/current.md",
            "beforeHash": f"sha256:{'1' * 64}",
            "afterHash": f"sha256:{'3' * 64}",
            "beforeModifiedVersion": "mtime:1:size:4",
            "afterModifiedVersion": "mtime:4:size:9",
        },
        {
            "operation": "delete",
            "path": "notes/old.md",
            "beforeHash": f"sha256:{'2' * 64}",
            "afterHash": "absent",
            "beforeModifiedVersion": "mtime:2:size:4",
            "afterModifiedVersion": "missing",
        },
    ]
    record["appliedPaths"] = ["notes/new.md", "notes/current.md", "notes/old.md"]
    _write_json(journal / f"{BATCH_ID}.json", record)
    _seal(journal, BATCH_ID)

    result = await lookup.lookup_result(_definition(), call)

    assert result is not None
    assert [item.metadata["protocolKind"] for item in result.side_effects] == [
        "file_created",
        "file_modified",
        "file_trashed",
    ]


@pytest.mark.asyncio
async def test_state_hash_uses_unicode_code_point_path_order(tmp_path: Path) -> None:
    lookup, journal = _lookup(tmp_path)
    arguments = _arguments()
    operations = [
        {
            "op": "create",
            "path": path,
            "content": content,
            "expectedContentHash": "absent",
            "expectedModifiedVersion": "missing",
        }
        for path, content in (("notes/ä.md", "umlaut\n"), ("notes/z.md", "zed\n"))
    ]
    arguments["operations"] = operations
    call = _call_with_arguments(arguments)
    record = _record(call)
    record["targets"] = [
        {
            "operation": "create",
            "path": operation["path"],
            "beforeHash": "absent",
            "afterHash": f"sha256:{hashlib.sha256(str(operation['content']).encode()).hexdigest()}",
            "beforeModifiedVersion": "missing",
            "afterModifiedVersion": f"mtime:{index}:size:1",
        }
        for index, operation in enumerate(operations, start=1)
    ]
    record["appliedPaths"] = ["notes/ä.md", "notes/z.md"]
    _write_json(journal / f"{BATCH_ID}.json", record)
    _seal(journal, BATCH_ID)

    result = await lookup.lookup_result(_definition(), call)

    expected = "notes/z.md\0absent\nnotes/ä.md\0absent"
    assert result is not None
    assert isinstance(result.data, Mapping)
    assert result.data["beforeStateHash"] == f"sha256:{hashlib.sha256(expected.encode()).hexdigest()}"


@pytest.mark.asyncio
async def test_recovery_rejects_paths_forbidden_by_the_plugin_contract(tmp_path: Path) -> None:
    lookup, journal = _lookup(tmp_path)
    arguments = _arguments()
    arguments["operations"][0]["path"] = "experiences/C:.md"
    call = _call_with_arguments(arguments)
    record = _record(call)
    record["targets"][0]["path"] = "experiences/C:.md"
    record["appliedPaths"] = ["experiences/C:.md"]
    _write_json(journal / f"{BATCH_ID}.json", record)
    _seal(journal, BATCH_ID)

    with pytest.raises(PluginVaultChangeRecoveryError, match="target is malformed"):
        await lookup.lookup_result(_definition(), call)


@pytest.mark.asyncio
async def test_record_deleted_after_plugin_recovery_is_not_reinterpreted_as_not_applied(tmp_path: Path) -> None:
    lookup, journal = _lookup(tmp_path)
    call = _call()
    path = journal / f"{BATCH_ID}.json"
    _write_json(path, _record(call))
    _seal(journal, BATCH_ID)
    path.unlink()

    with pytest.raises(PluginVaultChangeRecoveryError, match="sealed record is unavailable"):
        await lookup.lookup_result(_definition(), call)


@pytest.mark.asyncio
async def test_record_replaced_after_plugin_recovery_is_not_adopted(tmp_path: Path) -> None:
    lookup, journal = _lookup(tmp_path)
    call = _call()
    path = journal / f"{BATCH_ID}.json"
    _write_json(path, _record(call))
    _seal(journal, BATCH_ID)
    changed = _record(call)
    changed["targets"][0]["afterHash"] = f"sha256:{'d' * 64}"
    _write_json(path, changed)

    with pytest.raises(PluginVaultChangeRecoveryError, match="changed after plugin recovery"):
        await lookup.lookup_result(_definition(), call)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        (None, ToolResultStatus.CONFLICTED),
        ("rejected", ToolResultStatus.DENIED),
        ("rolled_back", ToolResultStatus.CONFLICTED),
    ],
)
async def test_definite_not_applied_states_resume_with_no_side_effects(
    tmp_path: Path,
    state: str | None,
    expected_status: ToolResultStatus,
) -> None:
    lookup, journal = _lookup(tmp_path)
    call = _call()
    if state is not None:
        _write_json(journal / f"{BATCH_ID}.json", _record(call, state))
        _seal(journal, BATCH_ID)
    else:
        _seal(journal)

    result = await lookup.lookup_result(_definition(), call)

    assert result is not None and result.status is expected_status
    assert result.side_effects == ()
    assert result.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["prepared", "applying", "undoing", "undone", "recovery_failed"])
async def test_nonterminal_or_manually_changed_states_remain_unresolved(tmp_path: Path, state: str) -> None:
    lookup, journal = _lookup(tmp_path)
    call = _call()
    record = _record(call, state)
    if state in {"applying", "undoing", "undone"}:
        record["checkpointRef"] = f"refs/offeragent/checkpoints/{BATCH_ID}"
    if state == "recovery_failed":
        record["manualReviewPaths"] = ["experiences/acme.md"]
    _write_json(journal / f"{BATCH_ID}.json", record)
    _seal(journal, BATCH_ID)

    assert await lookup.lookup_result(_definition(), call) is None


@pytest.mark.asyncio
async def test_current_recovery_token_is_required_before_absence_is_authoritative(tmp_path: Path) -> None:
    lookup, journal = _lookup(tmp_path)
    _seal(journal, token="d" * 64)

    with pytest.raises(PluginVaultChangeRecoveryError, match="token"):
        await lookup.lookup_result(_definition(), _call())


@pytest.mark.asyncio
async def test_durable_binding_conflict_is_not_adopted(tmp_path: Path) -> None:
    lookup, journal = _lookup(tmp_path)
    call = _call()
    record = _record(call)
    record["argsHash"] = f"sha256:{'9' * 64}"
    _write_json(journal / f"{BATCH_ID}.json", record)
    _seal(journal, BATCH_ID)

    with pytest.raises(PluginVaultChangeRecoveryError, match="binding"):
        await lookup.lookup_result(_definition(), call)


@pytest.mark.asyncio
async def test_journal_targets_must_match_the_original_apply_operations(tmp_path: Path) -> None:
    lookup, journal = _lookup(tmp_path)
    call = _call()
    record = _record(call)
    record["targets"][0]["path"] = "experiences/other.md"
    record["appliedPaths"] = ["experiences/other.md"]
    _write_json(journal / f"{BATCH_ID}.json", record)
    _seal(journal, BATCH_ID)

    with pytest.raises(PluginVaultChangeRecoveryError, match="target binding"):
        await lookup.lookup_result(_definition(), call)


@pytest.mark.asyncio
async def test_malformed_journal_is_not_treated_as_absent(tmp_path: Path) -> None:
    lookup, journal = _lookup(tmp_path)
    (journal / f"{BATCH_ID}.json").write_text('{"version":2,"version":2}\n', encoding="utf-8")
    _seal(journal, BATCH_ID)

    with pytest.raises(PluginVaultChangeRecoveryError, match="malformed"):
        await lookup.lookup_result(_definition(), _call())


@pytest.mark.asyncio
async def test_broken_journal_symlink_is_not_authoritative_absence(tmp_path: Path) -> None:
    lookup, journal = _lookup(tmp_path)
    record_path = journal / f"{BATCH_ID}.json"
    try:
        record_path.symlink_to(journal / "missing-record.json")
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")
    seal_directory = journal / ".recovery-seals" / "current"
    seal_directory.mkdir(parents=True)
    _write_json(seal_directory / _seal_name(BATCH_ID), {
        "schemaVersion": 1,
        "recoveryToken": TOKEN,
        "batchId": BATCH_ID,
        "contentHash": f"sha256:{'f' * 64}",
        "byteLength": 2,
    })
    _write_json(journal / ".recovery-ready.json", {"schemaVersion": 2, "recoveryToken": TOKEN})

    with pytest.raises(PluginVaultChangeRecoveryError, match="real file"):
        await lookup.lookup_result(_definition(), _call())
