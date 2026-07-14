from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.adapters.sqlite_stores import SqliteInvocationJournal
from offeragent_harness.agent import BudgetLedger, RunBudget
from offeragent_harness.permissions import (
    ApprovalDecisionReceipt,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalScope,
    ApprovalState,
    CapabilityScope,
    PermissionMode,
    PolicyContext,
    RiskClass,
)
from offeragent_harness.permissions.audit import NullPolicyAuditSink
from offeragent_harness.permissions.evaluator import RuleBasedPolicyEvaluator
from offeragent_harness.ports import ApplicationCommandContext, ApprovalObserver, CancellationToken
from offeragent_harness.protocol.common import ApprovalDecision
from offeragent_harness.protocol.common import ApprovalScope as WireApprovalScope
from offeragent_harness.protocol.messages import (
    ApprovalResolveParams,
    HeadlessVaultWriteActivateParams,
    HeadlessVaultWriteRequestParams,
)
from offeragent_harness.runtime.headless_vault_write import (
    HeadlessAuthorizedVaultTransaction,
    HeadlessVaultWriteAuthority,
)
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
)
from offeragent_harness.tools import (
    PreflightRegistry,
    ToolCall,
    ToolResultStatus,
    ToolValidator,
    canonical_json_sha256,
)
from offeragent_harness.tools.dispatcher import ToolDispatcher
from offeragent_harness.tools.kernel import UnifiedToolKernel
from offeragent_harness.tools.registry import ToolRegistry
from offeragent_harness.tools.scheduler import ToolScheduler
from offeragent_harness.vault import (
    VaultTransactionCoordinator,
    content_hash,
    vault_transaction_definition,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _required(value: str | None) -> str:
    assert value is not None
    return value


class RecordingApproval:
    def __init__(self, *, mutate: Callable[[], object] | None = None) -> None:
        self.requests: list[ApprovalRequest] = []
        self._mutate = mutate

    async def request(
        self,
        approval: ApprovalRequest,
        cancellation: CancellationToken,
        observer: ApprovalObserver | None = None,
    ) -> ApprovalDecisionReceipt:
        cancellation.checkpoint()
        self.requests.append(approval)
        if observer is not None:
            await observer.required(approval)
        if self._mutate is not None:
            self._mutate()
        resolution = ApprovalResolution(
            approval_id=approval.approval_id,
            state=ApprovalState.APPROVED,
            scope=ApprovalScope.ONCE,
            resolved_at=NOW,
            resolver_id="user_1",
            include_descendants=False,
        )
        if observer is not None:
            await observer.resolved(approval, resolution)
        return ApprovalDecisionReceipt(approval, resolution)

    async def cancel(self, approval_id: str, reason: str) -> None:
        del approval_id, reason

    async def pending(self, approval_id: str) -> ApprovalRequest | None:
        del approval_id
        return None


def _budget() -> BudgetLedger:
    return BudgetLedger(
        RunBudget(8, 8, 2, 60, 10_000, 10_000, Decimal("1"), 1_000_000, 2),
        started_at=NOW,
    )


def _call(
    expected_hash: str,
    *,
    call_id: str = "call_1",
    idempotency_key: str = "idem_vault_1",
) -> ToolCall:
    definition = vault_transaction_definition()
    arguments = {
        "operations": [
            {
                "op": "append",
                "path": "note.md",
                "content": "after\n",
                "expectedHash": expected_hash,
            }
        ]
    }
    return ToolCall(
        tool_call_id=call_id,
        run_id="run_1",
        workspace_id="ws_test",
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=idempotency_key,
        deadline=None,
        lineage=AgentLineage.root("run_1"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


def _kernel(
    tmp_path: Path,
    approval: RecordingApproval,
    *,
    transaction_wrapper: Callable[[VaultTransactionCoordinator], HeadlessAuthorizedVaultTransaction] | None = None,
) -> tuple[UnifiedToolKernel, LocalArtifactStore, Path, BudgetLedger]:
    vault = tmp_path / "vault"
    vault.mkdir()
    store = LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_test")
    budget = _budget()
    clock = ManualClock(NOW)
    coordinator = VaultTransactionCoordinator(
        workspace_id="ws_test",
        vault_root=vault,
        artifacts=store,
        artifact_budget=budget,
        clock=clock,
    )
    transaction = coordinator if transaction_wrapper is None else transaction_wrapper(coordinator)
    definition = vault_transaction_definition()
    preflights = PreflightRegistry((transaction,))
    registry = ToolRegistry(
        "snapshot_vault_1",
        (definition,),
        preflight_provider_ids=preflights.provider_ids,
    )
    context = PolicyContext(
        workspace_id="ws_test",
        session_id="session_1",
        principal_id="principal_1",
        run_id="run_1",
        permission_mode=PermissionMode.NORMAL,
        effective_scope=CapabilityScope(
            allowed_tools=frozenset({definition.name}),
            denied_tools=frozenset(),
            allowed_risks=frozenset(RiskClass),
            root_capabilities=definition.required_capabilities,
            allow_network=False,
            allow_secret_handles=False,
        ),
        workspace_trusted=True,
        now=NOW,
    )
    kernel = UnifiedToolKernel(
        registry=registry,
        validator=ToolValidator(),
        policy=RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink()),
        policy_context=lambda _: context,
        scheduler=ToolScheduler(clock=clock, max_parallel_reads=2),
        dispatcher=ToolDispatcher(clock=clock, local=transaction),
        journal=SqliteInvocationJournal(tmp_path / "state.sqlite"),
        clock=clock,
        ids=DeterministicIdGenerator(),
        approvals=approval,
        preflights=preflights,
    )
    return kernel, store, vault, budget


@pytest.mark.asyncio
async def test_kernel_approval_uses_real_diff_and_sqlite_journal_replays_exactly_once(tmp_path: Path) -> None:
    approval = RecordingApproval()
    kernel, store, vault, budget = _kernel(tmp_path, approval)
    before = b"before\n"
    (vault / "note.md").write_bytes(before)
    first_call = _call(content_hash(before))

    first = await kernel.execute_batch((first_call,), ManualCancellationToken())
    replay_call = _call(content_hash(before))
    replay = await kernel.execute_batch((replay_call,), ManualCancellationToken())

    assert first[0].result.status is ToolResultStatus.SUCCEEDED
    assert replay[0].result.status is ToolResultStatus.SUCCEEDED
    assert replay[0].result.tool_call_id == first_call.tool_call_id
    assert (vault / "note.md").read_bytes() == b"before\nafter\n"
    assert len(approval.requests) == 1
    request = approval.requests[0]
    assert request.diff_artifact_ids
    diff = b"".join([chunk async for chunk in store.read(request.diff_artifact_ids[0])])
    assert b"--- a/note.md" in diff and b"+after" in diff
    snapshot = await budget.snapshot(now=NOW)
    assert snapshot.used.artifact_bytes == len(diff)
    assert snapshot.reserved.artifact_bytes == 0


@pytest.mark.asyncio
async def test_approval_window_mutation_conflicts_before_dispatch_and_never_overwrites(tmp_path: Path) -> None:
    changed = b"changed outside approval\n"
    target: list[Path] = []
    approval = RecordingApproval(mutate=lambda: target[0].write_bytes(changed))
    kernel, _store, vault, _budget_ledger = _kernel(tmp_path, approval)
    target.append(vault / "note.md")
    before = b"before\n"
    target[0].write_bytes(before)

    result = await kernel.execute_batch((_call(content_hash(before)),), ManualCancellationToken())

    assert result[0].result.status is ToolResultStatus.CONFLICTED
    assert result[0].result.error is not None
    assert result[0].result.error.code == "preflight_state_changed"
    assert target[0].read_bytes() == changed
    assert len(approval.requests) == 1


@pytest.mark.asyncio
async def test_headless_grant_still_uses_real_kernel_diff_approval_journal_and_coordinator(
    tmp_path: Path,
) -> None:
    root_identity = "sha256:" + "1" * 64
    database_identity = "sha256:" + "2" * 64
    pipe_count = [0]
    authority_store = InMemoryUnitOfWorkFactory()
    authority_clock = ManualClock(NOW)

    authority = HeadlessVaultWriteAuthority(
        workspace_id="ws_test",
        workspace_instance_id="wsi_headless_kernel",
        root_identity=root_identity,
        database_identity=database_identity,
        unit_of_work=authority_store,
        clock=authority_clock,
        identity_probe=lambda: (root_identity, database_identity),
        pipe_connection_count=lambda: pipe_count[0],
    )
    await authority.recover_after_restart()
    web = ApplicationCommandContext(
        transport="loopback-http",
        client_id="web-headless-kernel",
        peer="127.0.0.1",
    )
    baseline = await authority.status(web, ManualCancellationToken())
    pending = await authority.request(
        HeadlessVaultWriteRequestParams(
            client_request_id="req_headless_kernel",
            confirmation="obsidian_closed_disk_authoritative",
            ttl_seconds=300,
            expected_baseline_fingerprint=baseline.baseline_fingerprint,
        ),
        web,
        ManualCancellationToken(),
    )
    resolved = await authority.try_resolve(
        ApprovalResolveParams(
            approval_id=_required(pending.approval_id),
            decision=ApprovalDecision.ALLOW_ONCE,
            scope=WireApprovalScope.ONCE,
            expected_args_hash=_required(pending.args_hash),
        ),
        web,
    )
    assert resolved is not None and resolved.run_id is None
    approved = await authority.status(web, ManualCancellationToken())
    active = await authority.activate(
        HeadlessVaultWriteActivateParams(
            client_request_id="req_activate_headless_kernel",
            approval_id=_required(approved.approval_id),
            expected_args_hash=_required(approved.args_hash),
            expected_revision=approved.revision,
        ),
        web,
        ManualCancellationToken(),
    )
    assert active.state == "active"
    grant_id = await authority.claim_for_turn(
        web.client_id,
        canonical_json_sha256({"turn": "turn_headless_kernel", "idempotencyKey": "headless-kernel"}),
    )
    assert grant_id is not None

    transaction_approval = RecordingApproval()
    kernel, artifacts, vault, _budget_ledger = _kernel(
        tmp_path,
        transaction_approval,
        transaction_wrapper=lambda coordinator: HeadlessAuthorizedVaultTransaction(
            authority=authority,
            grant_id=grant_id,
            transaction=coordinator,
        ),
    )
    before = b"before\n"
    (vault / "note.md").write_bytes(before)
    call = _call(content_hash(before), call_id="call_headless_1", idempotency_key="idem_headless_1")

    first = await kernel.execute_batch((call,), ManualCancellationToken())
    replay = await kernel.execute_batch((call,), ManualCancellationToken())

    assert first[0].result.status is ToolResultStatus.SUCCEEDED
    assert replay[0].result == first[0].result
    assert (vault / "note.md").read_bytes() == b"before\nafter\n"
    assert len(transaction_approval.requests) == 1
    diff_id = transaction_approval.requests[0].diff_artifact_ids[0]
    diff = b"".join([chunk async for chunk in artifacts.read(diff_id)])
    assert b"--- a/note.md" in diff and b"+after" in diff

    async with authority.pipe_registration():
        pipe_count[0] = 1
    after = (vault / "note.md").read_bytes()
    blocked = await kernel.execute_batch(
        (
            _call(
                content_hash(after),
                call_id="call_headless_after_pipe",
                idempotency_key="idem_headless_after_pipe",
            ),
        ),
        ManualCancellationToken(),
    )
    assert blocked[0].result.status is ToolResultStatus.CONFLICTED
    assert blocked[0].result.error is not None
    assert blocked[0].result.error.code == "preflight_conflict"
    assert blocked[0].result.error.details["reason"] == "headless_authority_invalid"
    assert (vault / "note.md").read_bytes() == after
