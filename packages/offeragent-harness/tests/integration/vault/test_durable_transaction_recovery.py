from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.adapters.sqlite_stores import SqliteInvocationJournal
from offeragent_harness.agent import BudgetLedger, RunBudget
from offeragent_harness.ports import CancellationToken, InvocationRecord, JournalState
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import FakeRunCancelled, ManualCancellationToken, ManualClock
from offeragent_harness.tools import (
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
    invocation_journal_scope,
    invocation_request_fingerprint,
)
from offeragent_harness.tools.preflight import PreflightConflict
from offeragent_harness.tools.scheduler import ScheduledInvocation, ToolScheduler
from offeragent_harness.vault import (
    VaultTransactionCoordinator,
    content_hash,
    vault_transaction_definition,
)
from offeragent_harness.vault import durable_manifest as durable_manifest_module
from offeragent_harness.vault.durable_manifest import (
    DurableManifestError,
    DurableManifestState,
    DurableVaultManifestStore,
    DurableVaultTransactionManifest,
    durable_manifest_id,
    manifest_identity_token,
)

NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)
CRASH_EXIT = 73
_SCOPE = "ws_durable:run_durable:run_durable:vault.transaction:1"
_HELPER = Path(__file__).with_name("durable_crash_worker.py")
_BEFORE_CONTENT = b"BEFORE_PAYLOAD\n"
_AFTER_CONTENT = {
    "append": b"BEFORE_PAYLOAD\nAFTER_PAYLOAD\n",
    "replace": b"REPLACED_PAYLOAD\n",
    "patch": b"PATCHED_PAYLOAD\n",
}


def _fixture(tmp_path: Path) -> Path:
    (tmp_path / "vault").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "vault" / "note.md").write_bytes(_BEFORE_CONTENT)
    return tmp_path


def _run(root: Path, action: str, stage: str, *, mode: str = "append") -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source = Path(__file__).parents[3] / "src"
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source), environment.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        [sys.executable, str(_HELPER), action, str(root), stage, "--mode", mode],
        cwd=Path(__file__).parents[3],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


async def _journal_record(root: Path) -> InvocationRecord | None:
    return await SqliteInvocationJournal(root / "state" / "state.sqlite").get(_SCOPE, "idem_durable")


def _manifest_files(root: Path) -> tuple[Path, ...]:
    return tuple((root / "state" / "vault-transactions").glob("*.json"))


def _store_manifest(root: Path, index: int) -> DurableVaultTransactionManifest:
    token = content_hash(f"plan-{index}".encode())
    path = f"note-{index}.md"
    suffix = token.removeprefix("sha256:")[:20]
    return DurableVaultTransactionManifest(
        manifest_id=durable_manifest_id("ws_durable", token),
        workspace_id="ws_durable",
        root_identity=manifest_identity_token(((root / "vault").stat().st_dev, (root / "vault").stat().st_ino)),
        journal_scope=f"ws_durable:run-{index}:run-{index}:vault.transaction:1",
        idempotency_key=f"idem-{index}",
        request_hash=content_hash(f"request-{index}".encode()),
        tool_call_id=f"call-{index}",
        run_id=f"run-{index}",
        root_run_id=f"run-{index}",
        definition_fingerprint=content_hash(f"definition-{index}".encode()),
        plan_token=token,
        state_hash=content_hash(f"state-{index}".encode()),
        operation="create",
        path=path,
        before_hash="absent",
        after_hash=content_hash(f"after-{index}".encode()),
        original_identity=None,
        approved_parents={},
        active_parents={},
        backup_path=None,
        temporary_path=f".offeragent-tx-{suffix}-{path}.tmp",
        rollback_path=f".offeragent-rollback-{suffix}-{path}",
        temporary_identity=None,
        state=DurableManifestState.PREPARED,
    )


class _ThreadBarrier:
    """Block a filesystem worker thread without blocking the asyncio loop."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.reached = threading.Event()
        self.release = threading.Event()

    def __call__(self, stage: str, _relative_path: str) -> None:
        if stage != self.stage:
            return
        self.reached.set()
        if not self.release.wait(10):
            raise RuntimeError(f"test barrier timed out: {stage}")


def _live_call(root: Path, *, deadline: datetime | None) -> tuple[ToolDefinition, ToolCall]:
    definition = vault_transaction_definition()
    arguments: dict[str, object] = {
        "operations": [
            {
                "op": "append",
                "path": "note.md",
                "content": "AFTER_PAYLOAD\n",
                "expectedHash": content_hash((root / "vault" / "note.md").read_bytes()),
            }
        ]
    }
    return definition, ToolCall(
        tool_call_id="call_durable",
        run_id="run_durable",
        workspace_id="ws_durable",
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="idem_durable",
        deadline=deadline,
        lineage=AgentLineage.root("run_durable"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


def _live_coordinator(root: Path, barrier: _ThreadBarrier | None) -> VaultTransactionCoordinator:
    return VaultTransactionCoordinator(
        workspace_id="ws_durable",
        vault_root=root / "vault",
        artifacts=LocalArtifactStore(root / "state" / "artifacts", workspace_id="ws_durable"),
        artifact_budget=BudgetLedger(
            RunBudget(8, 8, 2, 60, 10_000, 10_000, Decimal("1"), 20 * 1024 * 1024, 2, 1),
            started_at=NOW,
        ),
        clock=ManualClock(NOW),
        manifest_directory=root / "state" / "vault-transactions",
        journal=SqliteInvocationJournal(root / "state" / "state.sqlite"),
        cas_barrier=barrier,
    )


@asynccontextmanager
async def _live_guard(_cancellation: CancellationToken) -> AsyncIterator[None]:
    yield


async def _live_invocation(
    root: Path,
    coordinator: VaultTransactionCoordinator,
    definition: ToolDefinition,
    call: ToolCall,
    cancellation: ManualCancellationToken,
) -> ScheduledInvocation:
    # Keep the test's state transitions equivalent to the production Kernel:
    # preflight/revalidation precede scheduling, prepare starts the journal,
    # and finalize durably records the result before retiring the manifest.
    evidence = await coordinator.prepare(definition, call, cancellation)
    await coordinator.revalidate(definition, call, evidence, cancellation)
    journal = SqliteInvocationJournal(root / "state" / "state.sqlite")
    scope = invocation_journal_scope(call, definition)
    request_hash = invocation_request_fingerprint(call)

    async def prepare() -> ToolResult | None:
        await journal.start(scope, call.idempotency_key, request_hash, NOW)
        return None

    async def execute(token: CancellationToken) -> ToolResult:
        return await coordinator.execute(call, token)

    async def finalize(result: ToolResult) -> ToolResult:
        await journal.complete(scope, call.idempotency_key, request_hash, result, NOW)
        await coordinator.complete(definition, call, evidence, result)
        return result

    async def abort() -> None:
        result = ToolResult(
            call.tool_call_id,
            ToolResultStatus.FAILED,
            None,
            "scheduler aborted the prepared Vault transaction",
            (),
            (),
            (),
            False,
            None,
            None,
            ToolError("scheduler_aborted", "scheduler aborted the prepared Vault transaction", False, False),
        )
        await coordinator.complete(definition, call, evidence, result)

    return ScheduledInvocation(
        call,
        definition,
        _live_guard,
        prepare,
        execute,
        finalize,
        abort=abort,
    )


def _assert_live_vault_clean(root: Path, expected: bytes) -> None:
    target = root / "vault" / "note.md"
    assert target.read_bytes() == expected
    # These operations run before the Worker/test process exits.  On Windows
    # they fail immediately if AtomicVaultChange leaked any no-share HANDLE.
    target.write_bytes(expected + b"HANDLE_PROBE\n")
    assert target.read_bytes() == expected + b"HANDLE_PROBE\n"
    target.write_bytes(expected)
    assert not tuple((root / "vault").rglob(".offeragent-*"))
    assert not _manifest_files(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows HANDLE ownership regression")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "trigger", "expected_result", "expected_journal", "committed"),
    (
        (
            "manifest_prepared_written",
            "cancel",
            None,
            ToolResultStatus.FAILED,
            False,
        ),
        ("published", "cancel", None, ToolResultStatus.FAILED, False),
        (
            "published",
            "timeout",
            ToolResultStatus.UNKNOWN_OUTCOME,
            ToolResultStatus.UNKNOWN_OUTCOME,
            False,
        ),
        (
            "manifest_committed_written",
            "cancel",
            ToolResultStatus.SUCCEEDED,
            ToolResultStatus.SUCCEEDED,
            True,
        ),
        (
            "manifest_committed_written",
            "timeout",
            ToolResultStatus.SUCCEEDED,
            ToolResultStatus.SUCCEEDED,
            True,
        ),
    ),
)
async def test_live_scheduler_cancellation_transfers_cas_ownership_and_honors_commit_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    trigger: str,
    expected_result: ToolResultStatus | None,
    expected_journal: ToolResultStatus,
    committed: bool,
) -> None:
    root = _fixture(tmp_path)
    before_content = (root / "vault" / "note.md").read_bytes()
    barrier = _ThreadBarrier(stage)
    coordinator = _live_coordinator(root, barrier if stage == "published" else None)
    store = coordinator._manifests
    assert store is not None
    if stage == "manifest_prepared_written":
        original_create = store.create

        def create_then_block(manifest: DurableVaultTransactionManifest) -> None:
            original_create(manifest)
            barrier(stage, manifest.path)

        monkeypatch.setattr(store, "create", create_then_block)
    elif stage == "manifest_committed_written":
        original_save = store.save

        def save_then_block(manifest: DurableVaultTransactionManifest) -> None:
            original_save(manifest)
            if manifest.state is DurableManifestState.COMMITTED:
                barrier(stage, manifest.path)

        monkeypatch.setattr(store, "save", save_then_block)

    timeout_seconds = 1.0
    deadline = NOW + timedelta(seconds=timeout_seconds) if trigger == "timeout" else None
    definition, call = _live_call(root, deadline=deadline)
    cancellation = ManualCancellationToken()
    invocation = await _live_invocation(root, coordinator, definition, call, cancellation)
    scheduler = ToolScheduler(
        clock=ManualClock(NOW),
        max_parallel_reads=1,
        termination_grace_seconds=2.0,
    )
    run = asyncio.create_task(scheduler.execute_batch((invocation,), cancellation))
    try:
        assert await asyncio.to_thread(barrier.reached.wait, 5), f"barrier not reached: {stage}"
        if trigger == "cancel":
            assert cancellation.cancel()
            # Keep the worker thread blocked until the scheduler has injected
            # Task cancellation into the coordinator's shielded I/O wait.
            await asyncio.sleep(0.05)
        else:
            await asyncio.sleep(timeout_seconds + 0.05)
    finally:
        barrier.release.set()

    if expected_result is None:
        with pytest.raises(FakeRunCancelled):
            await run
    else:
        results = await run
        assert len(results) == 1
        assert results[0].status is expected_result

    expected_content = before_content + b"AFTER_PAYLOAD\n" if committed else before_content
    _assert_live_vault_clean(root, expected_content)
    record = await _journal_record(root)
    assert record is not None
    assert record.state is JournalState.COMPLETED
    assert record.result is not None and record.result.status is expected_journal


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", tuple(_AFTER_CONTENT))
@pytest.mark.parametrize(
    ("stage", "committed"),
    (
        ("manifest_prepared", False),
        ("manifest_staged", False),
        ("original_claimed", False),
        ("published", False),
        ("manifest_committed", True),
    ),
)
async def test_process_exit_at_every_live_manifest_stage_recovers_exactly_once(
    tmp_path: Path,
    mode: str,
    stage: str,
    committed: bool,
) -> None:
    root = _fixture(tmp_path)
    crashed = _run(root, "execute", stage, mode=mode)
    assert crashed.returncode == CRASH_EXIT, crashed.stderr
    manifests = _manifest_files(root)
    assert len(manifests) == 1
    manifest_bytes = manifests[0].read_bytes()
    assert b"BEFORE_PAYLOAD" not in manifest_bytes
    assert b"AFTER_PAYLOAD" not in manifest_bytes
    assert b"REPLACED_PAYLOAD" not in manifest_bytes
    assert b"PATCHED_PAYLOAD" not in manifest_bytes
    manifest = json.loads(manifest_bytes)
    assert manifest["operation"] == mode
    assert manifest["beforeHash"] == content_hash(_BEFORE_CONTENT)
    assert manifest["afterHash"] == content_hash(_AFTER_CONTENT[mode])

    recovered = _run(root, "recover", "none", mode=mode)
    assert recovered.returncode == 0, recovered.stderr
    expected = _AFTER_CONTENT[mode] if committed else _BEFORE_CONTENT
    target = root / "vault" / "note.md"
    assert target.read_bytes() == expected
    assert content_hash(target.read_bytes()) == content_hash(expected)
    assert not _manifest_files(root)
    assert not tuple((root / "vault").glob(".offeragent-*"))
    record = await _journal_record(root)
    assert record is not None and record.state is JournalState.COMPLETED and record.result is not None
    assert record.result.status is (ToolResultStatus.SUCCEEDED if committed else ToolResultStatus.FAILED)

    # A second startup recovery must be a true no-op: it cannot replay the
    # operation, rewrite the terminal journal row, or recreate CAS residue.
    recovered_again = _run(root, "recover", "none", mode=mode)
    assert recovered_again.returncode == 0, recovered_again.stderr
    assert target.read_bytes() == expected
    assert content_hash(target.read_bytes()) == content_hash(expected)
    assert await _journal_record(root) == record
    assert not _manifest_files(root)
    assert not tuple((root / "vault").glob(".offeragent-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "committed"),
    (
        ("manifest_prepared", False),
        ("manifest_staged", False),
        ("published", False),
        ("manifest_committed", True),
    ),
)
async def test_process_exit_at_every_create_manifest_stage_recovers_exactly_once(
    tmp_path: Path,
    stage: str,
    committed: bool,
) -> None:
    root = _fixture(tmp_path)
    crashed = _run(root, "execute", stage, mode="create")
    assert crashed.returncode == CRASH_EXIT, crashed.stderr
    target = root / "vault" / "created.md"
    if stage in {"published", "manifest_committed"}:
        assert target.read_text(encoding="utf-8") == "CREATED_PAYLOAD\n"
    else:
        assert not target.exists()

    recovered = _run(root, "recover", "none", mode="create")
    assert recovered.returncode == 0, recovered.stderr
    if committed:
        assert target.read_text(encoding="utf-8") == "CREATED_PAYLOAD\n"
    else:
        assert not target.exists()
    assert not _manifest_files(root)
    assert not tuple((root / "vault").glob(".offeragent-*"))
    record = await _journal_record(root)
    assert record is not None and record.result is not None
    assert record.result.status is (ToolResultStatus.SUCCEEDED if committed else ToolResultStatus.FAILED)

    recovered_again = _run(root, "recover", "none", mode="create")
    assert recovered_again.returncode == 0, recovered_again.stderr
    if committed:
        assert target.read_text(encoding="utf-8") == "CREATED_PAYLOAD\n"
    else:
        assert not target.exists()
    assert await _journal_record(root) == record
    assert not _manifest_files(root)
    assert not tuple((root / "vault").glob(".offeragent-*"))


@pytest.mark.parametrize("recovery_stage", ("recovery_final_claimed", "recovery_original_restored"))
def test_process_exit_during_recovery_is_rerunnable(tmp_path: Path, recovery_stage: str) -> None:
    root = _fixture(tmp_path)
    assert _run(root, "execute", "published").returncode == CRASH_EXIT
    interrupted = _run(root, "recover", recovery_stage)
    assert interrupted.returncode == CRASH_EXIT, interrupted.stderr

    recovered = _run(root, "recover", "none")
    assert recovered.returncode == 0, recovered.stderr
    assert (root / "vault" / "note.md").read_text(encoding="utf-8") == "BEFORE_PAYLOAD\n"
    assert not _manifest_files(root)
    assert not tuple((root / "vault").glob(".offeragent-*"))


@pytest.mark.asyncio
async def test_external_target_is_never_overwritten_and_manifest_blocks_the_path(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    crashed = _run(root, "execute", "original_claimed")
    assert crashed.returncode == CRASH_EXIT, crashed.stderr
    target = root / "vault" / "note.md"
    assert not target.exists()
    target.write_text("EXTERNAL_OWNER\n", encoding="utf-8")

    recovered = _run(root, "recover", "none")
    assert recovered.returncode == 0, recovered.stderr
    assert target.read_text(encoding="utf-8") == "EXTERNAL_OWNER\n"
    manifests = _manifest_files(root)
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["state"] == "manual_review"

    definition = vault_transaction_definition()
    arguments: dict[str, object] = {
        "operations": [
            {
                "op": "append",
                "path": "note.md",
                "content": "blocked",
                "expectedHash": content_hash(target.read_bytes()),
            }
        ]
    }
    call = ToolCall(
        "call_blocked",
        "run_blocked",
        "ws_durable",
        definition.name,
        definition.version,
        arguments,
        canonical_json_sha256(arguments),
        "idem_blocked",
        None,
        AgentLineage.root("run_blocked"),
        definition.fingerprint,
        definition.result_sensitivity,
    )
    coordinator = VaultTransactionCoordinator(
        workspace_id="ws_durable",
        vault_root=root / "vault",
        artifacts=LocalArtifactStore(root / "state" / "artifacts", workspace_id="ws_durable"),
        artifact_budget=BudgetLedger(
            RunBudget(8, 8, 2, 60, 10_000, 10_000, Decimal("1"), 20 * 1024 * 1024, 2, 1),
            started_at=NOW,
        ),
        clock=ManualClock(NOW),
        manifest_directory=root / "state" / "vault-transactions",
        journal=SqliteInvocationJournal(root / "state" / "state.sqlite"),
    )
    with pytest.raises(PreflightConflict, match="blocked by unresolved transaction"):
        await coordinator.prepare(definition, call, ManualCancellationToken())
    assert target.read_text(encoding="utf-8") == "EXTERNAL_OWNER\n"


def test_same_hash_replacement_identity_is_manual_and_preserved(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    crashed = _run(root, "execute", "published")
    assert crashed.returncode == CRASH_EXIT, crashed.stderr
    target = root / "vault" / "note.md"
    published = target.read_bytes()
    target.unlink()
    target.write_bytes(published)

    recovered = _run(root, "recover", "none")
    assert recovered.returncode == 0, recovered.stderr
    assert target.read_bytes() == published
    manifests = _manifest_files(root)
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["state"] == "manual_review"
    assert "identity/hash drifted" in manifest["manualReason"]


def test_manifest_store_refuses_the_entry_beyond_its_hard_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _fixture(tmp_path)
    monkeypatch.setattr(durable_manifest_module, "_MAX_MANIFESTS", 1)
    store = DurableVaultManifestStore(
        root / "state" / "vault-transactions",
        vault_root=root / "vault",
        workspace_id="ws_durable",
        trusted_state_root=root / "state",
    )
    store.create(_store_manifest(root, 1))
    with pytest.raises(DurableManifestError, match="too many unresolved"):
        store.create(_store_manifest(root, 2))
    assert len(store.list()) == 1
    assert not (store.directory / f"{_store_manifest(root, 2).manifest_id}.pending").exists()
    assert not (store.directory / f"{_store_manifest(root, 2).manifest_id}.json").exists()


@pytest.mark.parametrize("replace_root", (False, True))
def test_manifest_store_pins_trusted_state_ancestors_by_identity(tmp_path: Path, replace_root: bool) -> None:
    root = _fixture(tmp_path)
    store = DurableVaultManifestStore(
        root / "state" / "vault-transactions",
        vault_root=root / "vault",
        workspace_id="ws_durable",
        trusted_state_root=root / "state",
    )
    if replace_root:
        (root / "state").rename(root / "state-old")
        (root / "state").mkdir()
        (root / "state" / "vault-transactions").mkdir()
    else:
        store.directory.rename(root / "state" / "vault-transactions-old")
        store.directory.mkdir()
    with pytest.raises(DurableManifestError, match="identity changed"):
        store.list()
