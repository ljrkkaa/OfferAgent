from __future__ import annotations

import ctypes
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.agent import BudgetLedger, RunBudget
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import ManualCancellationToken, ManualClock
from offeragent_harness.tools import (
    PreflightEvidence,
    ToolCall,
    ToolResult,
    ToolResultStatus,
    ToolValidationError,
    ToolValidator,
    canonical_json_sha256,
)
from offeragent_harness.vault import (
    ABSENT_HASH,
    INTERNAL_VAULT_TRANSACTION_SCHEMA,
    VAULT_TRANSACTION_SCHEMA,
    VaultCasBarrier,
    VaultFaultInjector,
    VaultTransactionCoordinator,
    VaultTransactionError,
    content_hash,
    vault_transaction_definition,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _budget() -> BudgetLedger:
    return BudgetLedger(
        RunBudget(8, 8, 2, 60, 10_000, 10_000, Decimal("1"), 20 * 1024 * 1024, 2),
        started_at=NOW,
    )


def _call(arguments: dict[str, object], *, call_id: str = "call_1", idem: str = "vault-idem") -> ToolCall:
    definition = vault_transaction_definition()
    return ToolCall(
        tool_call_id=call_id,
        run_id="run_1",
        workspace_id="ws_test",
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        definition_fingerprint=definition.fingerprint,
        idempotency_key=idem,
        deadline=None,
        lineage=AgentLineage.root("run_1"),
        result_sensitivity=definition.result_sensitivity,
    )


def _coordinator(
    tmp_path: Path,
    *,
    faults: VaultFaultInjector | None = None,
    cas_barrier: VaultCasBarrier | None = None,
) -> tuple[VaultTransactionCoordinator, LocalArtifactStore, Path]:
    vault = tmp_path / "vault"
    vault.mkdir()
    store = LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_test")
    coordinator = VaultTransactionCoordinator(
        workspace_id="ws_test",
        vault_root=vault,
        artifacts=store,
        artifact_budget=_budget(),
        clock=ManualClock(NOW),
        faults=faults,
        cas_barrier=cas_barrier,
    )
    return coordinator, store, vault


def _sha(text: str) -> str:
    return content_hash(text.encode())


async def _prepare_and_execute(
    coordinator: VaultTransactionCoordinator,
    call: ToolCall,
    token: ManualCancellationToken | None = None,
) -> tuple[PreflightEvidence, ToolResult]:
    cancellation = token or ManualCancellationToken()
    definition = vault_transaction_definition()
    evidence = await coordinator.prepare(definition, call, cancellation)
    await coordinator.revalidate(definition, call, evidence, cancellation)
    return evidence, await coordinator.execute(call, cancellation)


def test_public_schema_is_single_path_and_internal_schema_keeps_migration_operations() -> None:
    definition = vault_transaction_definition()
    validator = ToolValidator()
    digest = "sha256:" + "a" * 64
    valid = {
        "operations": [
            {"op": "create", "path": "new.md", "content": "new", "expectedHash": ABSENT_HASH},
            {"op": "append", "path": "a.md", "content": "x", "expectedHash": digest},
            {"op": "replace", "path": "b.md", "find": "x", "replace": "y", "expectedHash": digest},
            {
                "op": "patch",
                "path": "c.md",
                "edits": [{"startLine": 1, "endLine": 1, "replacement": "z\n"}],
                "expectedHash": digest,
            },
            {
                "op": "rename",
                "path": "d.md",
                "destination": "e.md",
                "expectedHash": digest,
                "expectedDestinationHash": ABSENT_HASH,
            },
            {"op": "trash", "path": "f.md", "expectedHash": digest},
        ]
    }
    for operation in valid["operations"][:4]:
        assert validator.validate_arguments(definition, {"operations": [operation]}).arguments["operations"]
    with pytest.raises(ToolValidationError):
        validator.validate_arguments(definition, valid)
    with pytest.raises(ToolValidationError):
        validator.validate_arguments(definition, {"operations": [valid["operations"][4]]})
    Draft202012Validator(INTERNAL_VAULT_TRANSACTION_SCHEMA).validate(valid)
    with pytest.raises(ToolValidationError):
        validator.validate_arguments(definition, {**valid, "unexpected": True})
    with pytest.raises(ToolValidationError):
        validator.validate_arguments(
            definition,
            {"operations": [{"op": "create", "path": "x.md", "content": "x"}]},
        )
    assert VAULT_TRANSACTION_SCHEMA["additionalProperties"] is False
    assert VAULT_TRANSACTION_SCHEMA["properties"]["operations"]["maxItems"] == 1


@pytest.mark.asyncio
async def test_all_operations_execute_from_hash_bound_plan_and_emit_diff_artifact(tmp_path: Path) -> None:
    coordinator, store, vault = _coordinator(tmp_path)
    originals = {
        "append.md": "A\n",
        "replace.md": "before\n",
        "patch.md": "one\ntwo\nthree\n",
        "rename.md": "move me\n",
        "trash.md": "trash me\n",
    }
    for path, text in originals.items():
        (vault / path).write_bytes(text.encode("utf-8"))
    arguments: dict[str, object] = {
        "operations": [
            {"op": "create", "path": "created.md", "content": "created\n", "expectedHash": ABSENT_HASH},
            {
                "op": "append",
                "path": "append.md",
                "content": "B\n",
                "expectedHash": _sha(originals["append.md"]),
            },
            {
                "op": "replace",
                "path": "replace.md",
                "find": "before",
                "replace": "after",
                "expectedHash": _sha(originals["replace.md"]),
            },
            {
                "op": "patch",
                "path": "patch.md",
                "edits": [{"startLine": 2, "endLine": 2, "replacement": "TWO\n"}],
                "expectedHash": _sha(originals["patch.md"]),
            },
            {
                "op": "rename",
                "path": "rename.md",
                "destination": "renamed.md",
                "expectedHash": _sha(originals["rename.md"]),
                "expectedDestinationHash": ABSENT_HASH,
            },
            {"op": "trash", "path": "trash.md", "expectedHash": _sha(originals["trash.md"])},
        ]
    }

    evidence, result = await _prepare_and_execute(coordinator, _call(arguments))

    assert result.status is ToolResultStatus.SUCCEEDED
    before_hashes = evidence.facts["beforeHashes"]
    after_hashes = evidence.facts["afterHashes"]
    assert isinstance(before_hashes, Mapping) and isinstance(after_hashes, Mapping)
    assert tuple(before_hashes) == tuple(sorted(before_hashes, key=str.casefold))
    assert tuple(after_hashes) == tuple(before_hashes)
    assert before_hashes["append.md"] == _sha(originals["append.md"])
    assert after_hashes["append.md"] == _sha("A\nB\n")
    assert before_hashes["created.md"] == ABSENT_HASH
    assert after_hashes["created.md"] == _sha("created\n")
    assert after_hashes["rename.md"] == ABSENT_HASH
    assert after_hashes["renamed.md"] == _sha(originals["rename.md"])
    assert after_hashes["trash.md"] == ABSENT_HASH
    trash_internal = next(path for path in before_hashes if path.startswith(".trash/offeragent/"))
    assert before_hashes[trash_internal] == ABSENT_HASH
    assert after_hashes[trash_internal] == _sha(originals["trash.md"])
    assert (vault / "created.md").read_bytes() == b"created\n"
    assert (vault / "append.md").read_bytes() == b"A\nB\n"
    assert (vault / "replace.md").read_bytes() == b"after\n"
    assert (vault / "patch.md").read_bytes() == b"one\nTWO\nthree\n"
    assert not (vault / "rename.md").exists()
    assert (vault / "renamed.md").read_bytes() == b"move me\n"
    assert not (vault / "trash.md").exists()
    trashed = list((vault / ".trash" / "offeragent").glob("*-trash.md"))
    assert len(trashed) == 1 and trashed[0].read_bytes() == b"trash me\n"
    chunks = [chunk async for chunk in store.read(evidence.artifact_ids[0])]
    diff = b"".join(chunks).decode("utf-8")
    assert "created.md" in diff and "-before" in diff and "+after" in diff


@pytest.mark.asyncio
async def test_expected_hash_conflict_and_path_escape_fail_before_artifact_or_write(tmp_path: Path) -> None:
    coordinator, _store, vault = _coordinator(tmp_path)
    (vault / "note.md").write_text("current", encoding="utf-8")
    definition = vault_transaction_definition()
    token = ManualCancellationToken()
    with pytest.raises(Exception, match="expectedHash conflict"):
        await coordinator.prepare(
            definition,
            _call(
                {
                    "operations": [
                        {
                            "op": "append",
                            "path": "note.md",
                            "content": "x",
                            "expectedHash": "sha256:" + "0" * 64,
                        }
                    ]
                }
            ),
            token,
        )
    with pytest.raises(VaultTransactionError, match="path_escape"):
        await coordinator.prepare(
            definition,
            _call(
                {"operations": [{"op": "create", "path": "../escape.md", "content": "x", "expectedHash": ABSENT_HASH}]},
                call_id="call_2",
            ),
            token,
        )
    assert not (tmp_path / "escape.md").exists()


@pytest.mark.asyncio
async def test_symlink_and_hardlink_targets_are_rejected(tmp_path: Path) -> None:
    coordinator, _store, vault = _coordinator(tmp_path)
    source = vault / "source.md"
    source.write_text("source", encoding="utf-8")
    hardlink = vault / "hard.md"
    hardlink.hardlink_to(source)
    definition = vault_transaction_definition()
    with pytest.raises(VaultTransactionError, match="hard-linked"):
        await coordinator.prepare(
            definition,
            _call(
                {"operations": [{"op": "append", "path": "hard.md", "content": "x", "expectedHash": _sha("source")}]}
            ),
            ManualCancellationToken(),
        )

    external = tmp_path / "external.md"
    external.write_text("external", encoding="utf-8")
    symlink = vault / "link.md"
    try:
        symlink.symlink_to(external)
    except OSError:
        pytest.skip("current Windows account cannot create symlinks")
    with pytest.raises(VaultTransactionError, match="reparse_point"):
        await coordinator.prepare(
            definition,
            _call(
                {"operations": [{"op": "append", "path": "link.md", "content": "x", "expectedHash": _sha("external")}]},
                call_id="call_2",
            ),
            ManualCancellationToken(),
        )


class _FailApply:
    def before_apply(self, index: int, relative_path: str) -> None:
        del relative_path
        if index == 1:
            raise OSError("injected apply failure")

    def before_rollback(self, index: int, relative_path: str) -> None:
        del index, relative_path

    def before_cleanup(self, index: int, relative_path: str) -> None:
        del index, relative_path


class _FailApplyAndRollback(_FailApply):
    def before_rollback(self, index: int, relative_path: str) -> None:
        del index, relative_path
        raise OSError("injected rollback failure")


class _CancelBeforeSecond:
    def __init__(self, token: ManualCancellationToken) -> None:
        self._token = token

    def before_apply(self, index: int, relative_path: str) -> None:
        del relative_path
        if index == 1:
            self._token.cancel()

    def before_rollback(self, index: int, relative_path: str) -> None:
        del index, relative_path

    def before_cleanup(self, index: int, relative_path: str) -> None:
        del index, relative_path


class _MutateBeforeApply:
    def __init__(self, target: Path) -> None:
        self._target = target

    def before_apply(self, index: int, relative_path: str) -> None:
        del index, relative_path
        self._target.write_bytes(b"raced")

    def before_rollback(self, index: int, relative_path: str) -> None:
        del index, relative_path

    def before_cleanup(self, index: int, relative_path: str) -> None:
        del index, relative_path


class _ReplaceParentBeforeApply:
    def __init__(self, parent: Path) -> None:
        self._parent = parent

    def before_apply(self, index: int, relative_path: str) -> None:
        del index, relative_path
        moved = self._parent.with_name(f"{self._parent.name}-moved")
        self._parent.rename(moved)
        self._parent.mkdir()

    def before_rollback(self, index: int, relative_path: str) -> None:
        del index, relative_path

    def before_cleanup(self, index: int, relative_path: str) -> None:
        del index, relative_path


class _FailCleanup:
    def before_apply(self, index: int, relative_path: str) -> None:
        del index, relative_path

    def before_rollback(self, index: int, relative_path: str) -> None:
        del index, relative_path

    def before_cleanup(self, index: int, relative_path: str) -> None:
        del index, relative_path
        raise OSError("injected cleanup failure")


def _two_file_arguments() -> dict[str, object]:
    return {
        "operations": [
            {"op": "append", "path": "a.md", "content": "A2", "expectedHash": _sha("A1")},
            {"op": "append", "path": "b.md", "content": "B2", "expectedHash": _sha("B1")},
        ]
    }


@pytest.mark.asyncio
async def test_two_file_failure_rolls_back_first_file(tmp_path: Path) -> None:
    coordinator, _store, vault = _coordinator(tmp_path, faults=_FailApply())
    (vault / "a.md").write_text("A1", encoding="utf-8")
    (vault / "b.md").write_text("B1", encoding="utf-8")

    _evidence, result = await _prepare_and_execute(coordinator, _call(_two_file_arguments()))

    assert result.status is ToolResultStatus.FAILED
    assert (vault / "a.md").read_text(encoding="utf-8") == "A1"
    assert (vault / "b.md").read_text(encoding="utf-8") == "B1"


@pytest.mark.asyncio
async def test_unconfirmed_rollback_returns_unknown_with_recovery_artifact(tmp_path: Path) -> None:
    coordinator, store, vault = _coordinator(tmp_path, faults=_FailApplyAndRollback())
    (vault / "a.md").write_text("A1", encoding="utf-8")
    (vault / "b.md").write_text("B1", encoding="utf-8")

    _evidence, result = await _prepare_and_execute(coordinator, _call(_two_file_arguments()))

    assert result.status is ToolResultStatus.UNKNOWN_OUTCOME
    assert result.artifact_ids
    recovery = b"".join([chunk async for chunk in store.read(result.artifact_ids[0])])
    assert b"rollbackErrors" in recovery and b"original" in recovery


@pytest.mark.asyncio
async def test_cancellation_between_files_rolls_back_completed_change(tmp_path: Path) -> None:
    token = ManualCancellationToken()
    coordinator, _store, vault = _coordinator(tmp_path, faults=_CancelBeforeSecond(token))
    (vault / "a.md").write_text("A1", encoding="utf-8")
    (vault / "b.md").write_text("B1", encoding="utf-8")

    _evidence, result = await _prepare_and_execute(coordinator, _call(_two_file_arguments()), token)

    assert result.status is ToolResultStatus.CANCELLED
    assert (vault / "a.md").read_text(encoding="utf-8") == "A1"
    assert (vault / "b.md").read_text(encoding="utf-8") == "B1"


@pytest.mark.asyncio
async def test_execution_window_hash_race_returns_conflicted_without_overwrite(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_bytes(b"before")
    store = LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_test")
    coordinator = VaultTransactionCoordinator(
        workspace_id="ws_test",
        vault_root=vault,
        artifacts=store,
        artifact_budget=_budget(),
        clock=ManualClock(NOW),
        faults=_MutateBeforeApply(target),
    )
    arguments: dict[str, object] = {
        "operations": [{"op": "append", "path": "note.md", "content": "after", "expectedHash": _sha("before")}]
    }

    _evidence, result = await _prepare_and_execute(coordinator, _call(arguments))

    assert result.status is ToolResultStatus.CONFLICTED
    assert target.read_bytes() == b"raced"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-mode guarantee")
@pytest.mark.asyncio
async def test_locked_target_cannot_be_replaced_at_final_cas_barrier(tmp_path: Path) -> None:
    target_holder: list[Path] = []
    attacker = tmp_path / "attacker.md"
    blocked: list[OSError] = []

    def barrier(stage: str, relative_path: str) -> None:
        if stage != "target_locked" or relative_path != "note.md":
            return
        attacker.write_bytes(b"external")
        try:
            os.replace(attacker, target_holder[0])
        except OSError as error:
            blocked.append(error)

    coordinator, _store, vault = _coordinator(tmp_path, cas_barrier=barrier)
    target = vault / "note.md"
    target_holder.append(target)
    target.write_bytes(b"before")
    call = _call(
        {"operations": [{"op": "append", "path": "note.md", "content": "after", "expectedHash": _sha("before")}]}
    )

    _evidence, result = await _prepare_and_execute(coordinator, call)

    assert blocked and blocked[0].winerror in {5, 32}
    assert result.status is ToolResultStatus.SUCCEEDED
    assert target.read_bytes() == b"beforeafter"
    assert attacker.read_bytes() == b"external"


@pytest.mark.asyncio
async def test_external_target_created_after_claim_is_never_overwritten(tmp_path: Path) -> None:
    target_holder: list[Path] = []
    injected = [False]

    def barrier(stage: str, relative_path: str) -> None:
        if stage == "original_claimed" and relative_path == "note.md" and not injected[0]:
            injected[0] = True
            target_holder[0].write_bytes(b"external-after-claim")

    coordinator, _store, vault = _coordinator(tmp_path, cas_barrier=barrier)
    target = vault / "note.md"
    target_holder.append(target)
    target.write_bytes(b"before")
    call = _call(
        {"operations": [{"op": "append", "path": "note.md", "content": "after", "expectedHash": _sha("before")}]}
    )

    _evidence, result = await _prepare_and_execute(coordinator, call)

    assert injected[0]
    assert result.status is ToolResultStatus.UNKNOWN_OUTCOME
    assert target.read_bytes() == b"external-after-claim"
    backups = list(vault.glob(".offeragent-tx-*-note.md.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"before"


@pytest.mark.asyncio
async def test_create_publish_uses_atomic_no_replace_and_preserves_racing_file(tmp_path: Path) -> None:
    target_holder: list[Path] = []
    injected = [False]

    def barrier(stage: str, relative_path: str) -> None:
        if stage == "before_publish" and relative_path == "new.md" and not injected[0]:
            injected[0] = True
            target_holder[0].write_bytes(b"external-create")

    coordinator, _store, vault = _coordinator(tmp_path, cas_barrier=barrier)
    target = vault / "new.md"
    target_holder.append(target)
    call = _call(
        {"operations": [{"op": "create", "path": "new.md", "content": "planned", "expectedHash": ABSENT_HASH}]}
    )

    _evidence, result = await _prepare_and_execute(coordinator, call)

    assert injected[0]
    assert result.status is ToolResultStatus.CONFLICTED
    assert target.read_bytes() == b"external-create"
    assert not list(vault.glob(".offeragent-tx-*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows directory-handle guarantee")
@pytest.mark.asyncio
async def test_locked_parent_chain_blocks_directory_replacement(tmp_path: Path) -> None:
    parent_holder: list[Path] = []
    blocked: list[OSError] = []

    def barrier(stage: str, relative_path: str) -> None:
        if stage != "ancestry_locked" or relative_path != "notes/new.md":
            return
        parent = parent_holder[0]
        try:
            parent.rename(parent.with_name("notes-attacker-moved"))
        except OSError as error:
            blocked.append(error)

    coordinator, _store, vault = _coordinator(tmp_path, cas_barrier=barrier)
    parent = vault / "notes"
    parent.mkdir()
    parent_holder.append(parent)
    call = _call(
        {"operations": [{"op": "create", "path": "notes/new.md", "content": "safe", "expectedHash": ABSENT_HASH}]}
    )

    _evidence, result = await _prepare_and_execute(coordinator, call)

    assert blocked and blocked[0].winerror in {5, 32}
    assert result.status is ToolResultStatus.SUCCEEDED
    assert (parent / "new.md").read_bytes() == b"safe"
    assert not (vault / "notes-attacker-moved").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows hardlink race guarantee")
@pytest.mark.asyncio
async def test_hardlink_added_after_publish_is_detected_without_overwrite(tmp_path: Path) -> None:
    target_holder: list[Path] = []
    alias_holder: list[Path] = []

    def barrier(stage: str, relative_path: str) -> None:
        if stage == "published" and relative_path == "note.md":
            os.link(target_holder[0], alias_holder[0])

    coordinator, _store, vault = _coordinator(tmp_path, cas_barrier=barrier)
    target = vault / "note.md"
    alias = vault / "external-alias.md"
    target_holder.append(target)
    alias_holder.append(alias)
    target.write_bytes(b"before")
    call = _call(
        {"operations": [{"op": "append", "path": "note.md", "content": "after", "expectedHash": _sha("before")}]}
    )

    _evidence, result = await _prepare_and_execute(coordinator, call)

    assert result.status is ToolResultStatus.UNKNOWN_OUTCOME
    assert target.read_bytes() == b"beforeafter"
    assert alias.read_bytes() == b"beforeafter"
    backups = list(vault.glob(".offeragent-tx-*-note.md.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"before"


@pytest.mark.asyncio
async def test_second_path_claim_collision_rolls_back_first_and_preserves_external_file(tmp_path: Path) -> None:
    vault_holder: list[Path] = []
    injected = [False]

    def barrier(stage: str, relative_path: str) -> None:
        if stage == "original_claimed" and relative_path == "b.md" and not injected[0]:
            injected[0] = True
            (vault_holder[0] / "b.md").write_bytes(b"external-b")

    coordinator, _store, vault = _coordinator(tmp_path, cas_barrier=barrier)
    vault_holder.append(vault)
    (vault / "a.md").write_bytes(b"A1")
    (vault / "b.md").write_bytes(b"B1")

    _evidence, result = await _prepare_and_execute(coordinator, _call(_two_file_arguments()))

    assert result.status is ToolResultStatus.UNKNOWN_OUTCOME
    assert (vault / "a.md").read_bytes() == b"A1"
    assert (vault / "b.md").read_bytes() == b"external-b"
    backups = list(vault.glob(".offeragent-tx-*-b.md.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"B1"


@pytest.mark.asyncio
async def test_rollback_restore_collision_never_overwrites_external_file(tmp_path: Path) -> None:
    vault_holder: list[Path] = []
    injected = [False]

    def barrier(stage: str, relative_path: str) -> None:
        if stage == "rollback_final_claimed" and relative_path == "a.md" and not injected[0]:
            injected[0] = True
            (vault_holder[0] / "a.md").write_bytes(b"external-during-rollback")

    coordinator, _store, vault = _coordinator(
        tmp_path,
        faults=_FailApply(),
        cas_barrier=barrier,
    )
    vault_holder.append(vault)
    (vault / "a.md").write_bytes(b"A1")
    (vault / "b.md").write_bytes(b"B1")

    _evidence, result = await _prepare_and_execute(coordinator, _call(_two_file_arguments()))

    assert result.status is ToolResultStatus.UNKNOWN_OUTCOME
    assert (vault / "a.md").read_bytes() == b"external-during-rollback"
    assert (vault / "b.md").read_bytes() == b"B1"
    originals = list(vault.glob(".offeragent-tx-*-a.md.bak"))
    planned = list(vault.glob(".offeragent-rollback-*-a.md"))
    assert len(originals) == 1 and originals[0].read_bytes() == b"A1"
    assert len(planned) == 1 and planned[0].read_bytes() == b"A1A2"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-rename acknowledgement semantics")
@pytest.mark.asyncio
async def test_lost_native_rename_ack_is_confirmed_from_same_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _store, vault = _coordinator(tmp_path)
    target = vault / "note.md"
    target.write_bytes(b"before")
    backend = coordinator._cas._backend
    kernel32 = backend._kernel32  # type: ignore[union-attr]
    native = kernel32.SetFileInformationByHandle
    acknowledgement_lost = [False]

    def lose_first_successful_rename_ack(
        handle: object,
        information_class: int,
        information: object,
        size: int,
    ) -> int:
        result = int(native(handle, information_class, information, size))
        if information_class == 3 and result and not acknowledgement_lost[0]:
            acknowledgement_lost[0] = True
            ctypes.set_last_error(64)
            return 0
        return result

    monkeypatch.setattr(kernel32, "SetFileInformationByHandle", lose_first_successful_rename_ack)
    call = _call(
        {"operations": [{"op": "append", "path": "note.md", "content": "after", "expectedHash": _sha("before")}]}
    )

    _evidence, result = await _prepare_and_execute(coordinator, call)

    assert acknowledgement_lost[0]
    assert result.status is ToolResultStatus.SUCCEEDED
    assert target.read_bytes() == b"beforeafter"
    assert not list(vault.glob(".offeragent-*"))


@pytest.mark.asyncio
async def test_parent_directory_identity_replacement_fails_closed_before_create(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    parent = vault / "notes"
    parent.mkdir()
    store = LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_test")
    coordinator = VaultTransactionCoordinator(
        workspace_id="ws_test",
        vault_root=vault,
        artifacts=store,
        artifact_budget=_budget(),
        clock=ManualClock(NOW),
        faults=_ReplaceParentBeforeApply(parent),
    )
    call = _call(
        {"operations": [{"op": "create", "path": "notes/new.md", "content": "safe", "expectedHash": ABSENT_HASH}]}
    )

    _evidence, result = await _prepare_and_execute(coordinator, call)

    assert result.status is ToolResultStatus.CONFLICTED
    assert not (parent / "new.md").exists()
    assert not (vault / "notes-moved" / "new.md").exists()


@pytest.mark.asyncio
async def test_batch_can_create_multiple_files_under_one_preflight_absent_parent(tmp_path: Path) -> None:
    coordinator, _store, vault = _coordinator(tmp_path)
    call = _call(
        {
            "operations": [
                {"op": "create", "path": "new/a.md", "content": "a", "expectedHash": ABSENT_HASH},
                {"op": "create", "path": "new/b.md", "content": "b", "expectedHash": ABSENT_HASH},
            ]
        }
    )

    _evidence, result = await _prepare_and_execute(coordinator, call)

    assert result.status is ToolResultStatus.SUCCEEDED
    assert (vault / "new" / "a.md").read_bytes() == b"a"
    assert (vault / "new" / "b.md").read_bytes() == b"b"


@pytest.mark.asyncio
async def test_committed_cleanup_failure_is_partial_with_committed_effect_and_recovery_artifact(
    tmp_path: Path,
) -> None:
    coordinator, store, vault = _coordinator(tmp_path, faults=_FailCleanup())
    (vault / "note.md").write_bytes(b"before")
    call = _call(
        {"operations": [{"op": "append", "path": "note.md", "content": "after", "expectedHash": _sha("before")}]}
    )

    _evidence, result = await _prepare_and_execute(coordinator, call)

    assert result.status is ToolResultStatus.PARTIAL
    assert (vault / "note.md").read_bytes() == b"beforeafter"
    assert result.side_effects and all(effect.state.value == "committed" for effect in result.side_effects)
    assert result.artifact_ids
    recovery = b"".join([chunk async for chunk in store.read(result.artifact_ids[0])])
    assert b"cleanupErrors" in recovery
