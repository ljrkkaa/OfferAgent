from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.agent import BudgetCheckpoint, BudgetDelta, RunBudget
from offeragent_harness.agent.state import PendingWork, RunPhase, RunState
from offeragent_harness.permissions import (
    ApprovalBinding,
    ApprovalRequest,
    ApprovalState,
    RiskClass,
    approval_id_for,
)
from offeragent_harness.ports import JournalState, UnitOfWork
from offeragent_harness.runtime.approval_manager import ApprovalRecord
from offeragent_harness.runtime.recovery import (
    RecoveryActionKind,
    RecoveryCoordinator,
    RecoveryDisposition,
)
from offeragent_harness.sessions import (
    AgentLineage,
    Run,
    RunKind,
    RunStatus,
    TerminationReason,
    Turn,
    TurnStatus,
)
from offeragent_harness.testing import ManualClock
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffect,
    SideEffectClass,
    SideEffectKind,
    SideEffectState,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
    invocation_journal_scope,
    invocation_request_fingerprint,
    is_safe_crash_replay,
    is_side_effect_free,
)
from offeragent_harness.tools.registry import ToolRegistry

NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _RunBundle:
    run: Run
    state: RunState
    turn: Turn


class _FakeRecoveryLookup:
    def __init__(self, responses: dict[str, ToolResult | Exception | None] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[str] = []

    async def lookup_result(self, definition: ToolDefinition, call: ToolCall) -> ToolResult | None:
        del definition
        self.calls.append(call.tool_call_id)
        response = self._responses.get(call.tool_call_id)
        if isinstance(response, Exception):
            raise response
        return response


def _definition(
    name: str,
    side_effect_class: SideEffectClass,
    *,
    description: str | None = None,
    idempotent: bool = True,
    retryable: bool | None = None,
) -> ToolDefinition:
    side_effect_free = side_effect_class in {SideEffectClass.NONE, SideEffectClass.READ}
    risk = RiskClass.READ if side_effect_free else RiskClass.WRITE
    return ToolDefinition(
        name=name,
        version="1",
        description=description or f"{name} test definition",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        executor_location=ExecutorLocation.LOCAL,
        risk=risk,
        side_effect_class=side_effect_class,
        required_capabilities=frozenset({f"test.{name}"}),
        concurrency_safe=side_effect_free,
        idempotent=idempotent,
        retryable=side_effect_free if retryable is None else retryable,
        timeout_ms=5_000,
        output_limit_bytes=64 * 1024,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def _call(definition: ToolDefinition, run_id: str, call_id: str) -> ToolCall:
    arguments = {"path": f"notes/{call_id}.md"}
    return ToolCall(
        tool_call_id=call_id,
        run_id=run_id,
        workspace_id="workspace_1",
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=f"idem-{call_id}",
        deadline=None,
        lineage=AgentLineage.root(run_id),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


def _result(call: ToolCall, definition: ToolDefinition) -> ToolResult:
    if definition.side_effect_class is SideEffectClass.READ:
        effects = (
            SideEffect(
                SideEffectKind.READ,
                SideEffectState.OBSERVED,
                f"vault:{call.arguments['path']}",
                None,
                None,
            ),
        )
    else:
        effects = (
            SideEffect(
                SideEffectKind.FILE_WRITE,
                SideEffectState.COMMITTED,
                f"vault:{call.arguments['path']}",
                {"hash": "before"},
                {"hash": "after"},
            ),
        )
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data={"path": call.arguments["path"]},
        user_visible_summary="工具已完成",
        artifact_ids=(),
        source_refs=(),
        side_effects=effects,
        retryable=False,
        before_state=None,
        after_state=None,
        error=None,
    )


def _bundle(run_id: str, calls: tuple[ToolCall, ...]) -> _RunBundle:
    lineage = AgentLineage.root(run_id)
    turn_id = f"turn-{run_id}"
    run = Run(
        run_id=run_id,
        session_id=f"session-{run_id}",
        turn_id=turn_id,
        workspace_id="workspace_1",
        lineage=lineage,
        kind=RunKind.ROOT,
        status=RunStatus.EXECUTING_TOOLS,
        attempt=1,
        event_sequence=4,
        config_snapshot={"model": "fake"},
        created_at=NOW,
        updated_at=NOW,
        deadline_at=NOW + timedelta(seconds=300),
    )
    state = RunState(
        workspace_id=run.workspace_id,
        session_id=run.session_id,
        turn_id=turn_id,
        run_id=run_id,
        lineage=lineage,
        phase=RunPhase.EXECUTING_TOOLS,
        revision=7 if calls else 8,
        model_rounds=1,
        budget_checkpoint=_budget_checkpoint(len(calls)),
    )
    if calls:
        state = state.accept_tool_calls(calls)
    turn = Turn(
        turn_id=turn_id,
        session_id=run.session_id,
        ordinal=1,
        status=TurnStatus.RUNNING,
        input_blocks=({"type": "text", "text": "recover"},),
        created_at=NOW,
        updated_at=NOW,
    )
    return _RunBundle(run, state, turn)


def _budget_checkpoint(tool_calls: int) -> BudgetCheckpoint:
    return BudgetCheckpoint(
        budget=RunBudget(8, 20, 4, 300, 20_000, 8_000, Decimal("10"), 1_000_000, 4),
        started_at=NOW,
        used=BudgetDelta(model_rounds=1, tool_calls=tool_calls),
        reserved=BudgetDelta(),
        captured_at=NOW,
        elapsed_seconds=0,
    )


def _completed_bundle(run_id: str) -> _RunBundle:
    lineage = AgentLineage.root(run_id)
    turn_id = f"turn-{run_id}"
    return _RunBundle(
        Run(
            run_id=run_id,
            session_id=f"session-{run_id}",
            turn_id=turn_id,
            workspace_id="workspace_1",
            lineage=lineage,
            kind=RunKind.ROOT,
            status=RunStatus.COMPLETED,
            attempt=1,
            event_sequence=9,
            config_snapshot={"model": "fake"},
            created_at=NOW,
            updated_at=NOW,
            deadline_at=None,
            termination_reason=TerminationReason.COMPLETED,
        ),
        RunState(
            workspace_id="workspace_1",
            session_id=f"session-{run_id}",
            turn_id=turn_id,
            run_id=run_id,
            lineage=lineage,
            phase=RunPhase.COMPLETED,
            revision=12,
        ),
        Turn(
            turn_id=turn_id,
            session_id=f"session-{run_id}",
            ordinal=1,
            status=TurnStatus.COMPLETED,
            input_blocks=({"type": "text", "text": "done"},),
            created_at=NOW,
            updated_at=NOW,
        ),
    )


async def _persist_bundle(unit_of_work: UnitOfWork, bundle: _RunBundle, *, include_state: bool = True) -> None:
    await unit_of_work.entities.put("runs", bundle.run.run_id, bundle.run, expected_revision=0)
    if include_state:
        await unit_of_work.entities.put("run_states", bundle.run.run_id, bundle.state, expected_revision=0)
    await unit_of_work.entities.put("turns", bundle.turn.turn_id, bundle.turn, expected_revision=0)


def _scope(call: ToolCall, definition: ToolDefinition) -> str:
    return invocation_journal_scope(call, definition)


def test_shared_crash_replay_contract_requires_all_three_safety_properties() -> None:
    safe = _definition("workspace.read", SideEffectClass.READ)
    not_idempotent = _definition(
        "workspace.read.nonidempotent",
        SideEffectClass.READ,
        idempotent=False,
        retryable=False,
    )
    not_retryable = _definition("workspace.read.noretry", SideEffectClass.READ, retryable=False)
    effectful = _definition("vault.write", SideEffectClass.WRITE)

    assert is_side_effect_free(safe)
    assert is_safe_crash_replay(safe)
    assert not is_safe_crash_replay(not_idempotent)
    assert not is_safe_crash_replay(not_retryable)
    assert not is_side_effect_free(effectful)
    assert not is_safe_crash_replay(effectful)


@pytest.mark.asyncio
async def test_batch_crash_reuses_first_completed_write_and_replays_only_unstarted_call(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    write = _definition("vault.write", SideEffectClass.WRITE)
    first = _call(write, "run-batch", "call-first")
    second = _call(write, "run-batch", "call-second")
    bundle = _bundle("run-batch", (first, second))
    factory = SqliteUnitOfWorkFactory(database_path)
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, bundle)
        await unit_of_work.journal.start(
            _scope(first, write),
            first.idempotency_key,
            invocation_request_fingerprint(first),
            NOW,
        )
        await unit_of_work.journal.complete(
            _scope(first, write),
            first.idempotency_key,
            invocation_request_fingerprint(first),
            _result(first, write),
            NOW,
        )
        await unit_of_work.commit()

    reopened = SqliteUnitOfWorkFactory(database_path)
    plans = await RecoveryCoordinator(
        unit_of_work=reopened,
        registry=ToolRegistry("restart", (write,)),
        clock=ManualClock(NOW),
    ).scan()

    assert len(plans) == 1
    plan = plans[0]
    assert plan.disposition is RecoveryDisposition.RESUME
    assert [action.kind for action in plan.actions] == [
        RecoveryActionKind.APPLY_JOURNAL_RESULT,
        RecoveryActionKind.REPLAY_ORIGINAL_CALL,
    ]
    assert plan.actions[0].result == _result(first, write)
    assert plan.actions[1].call == second
    assert plan.actions[1].journal_state is None
    assert plan.original_calls_to_replay == (second,)
    assert await reopened.get_journal(_scope(second, write), second.idempotency_key) is None


@pytest.mark.asyncio
async def test_unknown_write_requires_manual_review_and_never_calls_lookup(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    write = _definition("vault.write", SideEffectClass.WRITE)
    call = _call(write, "run-unknown", "call-unknown")
    factory = SqliteUnitOfWorkFactory(database_path)
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, _bundle("run-unknown", (call,)))
        await unit_of_work.journal.start(
            _scope(call, write),
            call.idempotency_key,
            invocation_request_fingerprint(call),
            NOW,
        )
        await unit_of_work.journal.mark_unknown(
            _scope(call, write),
            call.idempotency_key,
            invocation_request_fingerprint(call),
            NOW,
        )
        await unit_of_work.commit()

    lookup = _FakeRecoveryLookup({call.tool_call_id: _result(call, write)})
    plans = await RecoveryCoordinator(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        registry=ToolRegistry("restart", (write,)),
        lookup=lookup,
        clock=ManualClock(NOW),
    ).scan()

    assert plans[0].disposition is RecoveryDisposition.MANUAL_REVIEW
    assert plans[0].requires_interruption
    assert plans[0].actions == ()
    assert [issue.code for issue in plans[0].issues] == ["journal_unknown_effectful"]
    assert lookup.calls == []


@pytest.mark.asyncio
async def test_started_safe_read_replays_original_call_and_keeps_original_journal_binding(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    read = _definition("workspace.read", SideEffectClass.READ)
    call = _call(read, "run-read", "call-read")
    factory = SqliteUnitOfWorkFactory(database_path)
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, _bundle("run-read", (call,)))
        await unit_of_work.journal.start(
            _scope(call, read),
            call.idempotency_key,
            invocation_request_fingerprint(call),
            NOW,
        )
        await unit_of_work.commit()

    reopened = SqliteUnitOfWorkFactory(database_path)
    plans = await RecoveryCoordinator(
        unit_of_work=reopened,
        registry=ToolRegistry("restart", (read,)),
        lookup=_FakeRecoveryLookup(),
        clock=ManualClock(NOW),
    ).scan()

    action = plans[0].actions[0]
    assert plans[0].disposition is RecoveryDisposition.RESUME
    assert action.kind is RecoveryActionKind.REPLAY_ORIGINAL_CALL
    assert action.call == call
    assert action.journal_state is JournalState.STARTED
    record = await reopened.get_journal(_scope(call, read), call.idempotency_key)
    assert record is not None
    assert record.state is JournalState.STARTED
    assert record.request_hash == invocation_request_fingerprint(call)


@pytest.mark.asyncio
async def test_started_write_uses_lookup_result_or_requires_manual_review(tmp_path: Path) -> None:
    write = _definition("vault.write", SideEffectClass.WRITE)
    for suffix, response, expected in (
        ("recovered", "result", RecoveryDisposition.RESUME),
        ("unconfirmed", None, RecoveryDisposition.MANUAL_REVIEW),
    ):
        database_path = tmp_path / f"{suffix}.sqlite"
        call = _call(write, f"run-{suffix}", f"call-{suffix}")
        factory = SqliteUnitOfWorkFactory(database_path)
        async with factory.begin() as unit_of_work:
            await _persist_bundle(unit_of_work, _bundle(f"run-{suffix}", (call,)))
            await unit_of_work.journal.start(
                _scope(call, write),
                call.idempotency_key,
                invocation_request_fingerprint(call),
                NOW,
            )
            await unit_of_work.commit()
        recovered = _result(call, write) if response == "result" else None
        plans = await RecoveryCoordinator(
            unit_of_work=SqliteUnitOfWorkFactory(database_path),
            registry=ToolRegistry("restart", (write,)),
            lookup=_FakeRecoveryLookup({call.tool_call_id: recovered}),
            clock=ManualClock(NOW),
        ).scan()

        assert plans[0].disposition is expected
        if recovered is not None:
            assert plans[0].actions[0].kind is RecoveryActionKind.APPLY_LOOKUP_RESULT
            assert plans[0].actions[0].result == recovered
        else:
            assert plans[0].actions == ()
            assert plans[0].issues[0].code == "started_effectful_invocation_unconfirmed"


@pytest.mark.asyncio
async def test_definition_drift_interrupts_even_when_old_journal_is_completed(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    original = _definition("vault.write", SideEffectClass.WRITE)
    current = _definition(
        "vault.write",
        SideEffectClass.WRITE,
        description="definition changed after restart",
    )
    call = _call(original, "run-drift", "call-drift")
    factory = SqliteUnitOfWorkFactory(database_path)
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, _bundle("run-drift", (call,)))
        await unit_of_work.journal.start(
            _scope(call, original),
            call.idempotency_key,
            invocation_request_fingerprint(call),
            NOW,
        )
        await unit_of_work.journal.complete(
            _scope(call, original),
            call.idempotency_key,
            invocation_request_fingerprint(call),
            _result(call, original),
            NOW,
        )
        await unit_of_work.commit()

    plans = await RecoveryCoordinator(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        registry=ToolRegistry("changed", (current,)),
        clock=ManualClock(NOW),
    ).scan()

    assert plans[0].disposition is RecoveryDisposition.INTERRUPT
    assert plans[0].actions == ()
    assert [issue.code for issue in plans[0].issues] == ["definition_fingerprint_mismatch"]


@pytest.mark.asyncio
async def test_active_tool_result_sensitivity_corruption_interrupts_before_model_resume(tmp_path: Path) -> None:
    database_path = tmp_path / "result-sensitivity-corruption.sqlite"
    read = _definition("workspace.read", SideEffectClass.READ)
    expected_codes = {
        "run-binding-missing": "tool_result_sensitivity_binding_missing",
        "run-binding-unknown": "tool_result_sensitivity_unknown",
        "run-binding-orphan": "tool_result_sensitivity_binding_orphan",
        "run-result-duplicate": "duplicate_tool_result",
    }
    bundles: list[_RunBundle] = []
    for run_id in expected_codes:
        call = _call(read, run_id, f"call-{run_id}")
        bundle = _bundle(run_id, (call,))
        tool_result = _result(call, read)
        completed_state = bundle.state.record_tool_result(read, tool_result)
        if run_id == "run-binding-missing":
            corrupted = replace(completed_state, tool_result_sensitivities={})
        elif run_id == "run-binding-unknown":
            corrupted = replace(
                completed_state,
                tool_result_sensitivities={call.tool_call_id: ResultSensitivity.UNKNOWN},
            )
        elif run_id == "run-binding-orphan":
            corrupted = replace(
                completed_state,
                tool_result_sensitivities={
                    call.tool_call_id: ResultSensitivity.WORKSPACE,
                    "orphan-call": ResultSensitivity.WORKSPACE,
                },
            )
        else:
            corrupted = replace(completed_state, tool_results=(tool_result, tool_result))
        bundles.append(replace(bundle, state=corrupted))

    factory = SqliteUnitOfWorkFactory(database_path)
    async with factory.begin() as unit_of_work:
        for bundle in bundles:
            await _persist_bundle(unit_of_work, bundle)
        await unit_of_work.commit()

    plans = await RecoveryCoordinator(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        registry=ToolRegistry("restart", (read,)),
        clock=ManualClock(NOW),
    ).scan(page_size=1)

    by_run_id = {plan.run_id: plan for plan in plans}
    assert set(by_run_id) == set(expected_codes)
    for run_id, expected_code in expected_codes.items():
        plan = by_run_id[run_id]
        assert plan.disposition is RecoveryDisposition.INTERRUPT
        assert expected_code in {issue.code for issue in plan.issues}
        assert plan.actions == ()


@pytest.mark.asyncio
async def test_missing_state_and_incomplete_pending_call_fail_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    read = _definition("workspace.read", SideEffectClass.READ)
    missing_state_call = _call(read, "run-missing-state", "call-missing-state")
    incomplete_call = _call(read, "run-incomplete", "call-incomplete")
    missing_state = _bundle("run-missing-state", (missing_state_call,))
    incomplete = _bundle("run-incomplete", (incomplete_call,))
    incomplete = replace(
        incomplete,
        state=replace(
            incomplete.state,
            pending=PendingWork(tool_call_ids=frozenset({incomplete_call.tool_call_id})),
        ),
    )
    no_checkpoint = _bundle("run-no-checkpoint", ())
    missing_budget_call = _call(read, "run-missing-budget", "call-missing-budget")
    missing_budget = _bundle("run-missing-budget", (missing_budget_call,))
    missing_budget = replace(missing_budget, state=replace(missing_budget.state, budget_checkpoint=None))
    factory = SqliteUnitOfWorkFactory(database_path)
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, missing_state, include_state=False)
        await _persist_bundle(unit_of_work, incomplete)
        await _persist_bundle(unit_of_work, no_checkpoint)
        await _persist_bundle(unit_of_work, missing_budget)
        await unit_of_work.commit()

    plans = await RecoveryCoordinator(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        registry=ToolRegistry("restart", (read,)),
        clock=ManualClock(NOW),
    ).scan(page_size=1)
    by_id = {plan.run_id: plan for plan in plans}

    assert by_id["run-missing-state"].disposition is RecoveryDisposition.INTERRUPT
    assert "run_state_missing" in {issue.code for issue in by_id["run-missing-state"].issues}
    assert by_id["run-incomplete"].disposition is RecoveryDisposition.INTERRUPT
    assert "pending_tool_call_payload_missing" in {issue.code for issue in by_id["run-incomplete"].issues}
    assert by_id["run-no-checkpoint"].disposition is RecoveryDisposition.INTERRUPT
    assert "no_exact_recovery_checkpoint" in {issue.code for issue in by_id["run-no-checkpoint"].issues}
    assert by_id["run-missing-budget"].disposition is RecoveryDisposition.INTERRUPT
    assert "budget_checkpoint_missing" in {issue.code for issue in by_id["run-missing-budget"].issues}


@pytest.mark.asyncio
async def test_scan_pages_multiple_runs_in_stable_order_and_skips_terminal_runs(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    read = _definition("workspace.read", SideEffectClass.READ)
    run_ids = ("run-d", "run-a", "run-e", "run-b")
    bundles = tuple(_bundle(run_id, (_call(read, run_id, f"call-{run_id}"),)) for run_id in run_ids)
    factory = SqliteUnitOfWorkFactory(database_path)
    async with factory.begin() as unit_of_work:
        for bundle in bundles:
            await _persist_bundle(unit_of_work, bundle)
        await _persist_bundle(unit_of_work, _completed_bundle("run-c"))
        await unit_of_work.commit()

    plans = await RecoveryCoordinator(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        registry=ToolRegistry("restart", (read,)),
        clock=ManualClock(NOW),
    ).scan(page_size=1)

    assert [plan.run_id for plan in plans] == sorted(run_ids)
    assert all(plan.disposition is RecoveryDisposition.RESUME for plan in plans)
    assert all(plan.actions[0].call.idempotency_key.startswith("idem-call-run-") for plan in plans)


@pytest.mark.asyncio
async def test_valid_pending_approval_is_a_recoverable_checkpoint_but_expiry_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "pending-approval.sqlite"
    write = _definition("vault.write", SideEffectClass.WRITE)
    call = _call(write, "run-approval", "call-approval")
    bundle = _bundle("run-approval", (call,))
    bundle = replace(
        bundle,
        run=replace(bundle.run, status=RunStatus.AWAITING_APPROVAL),
        state=replace(
            bundle.state,
            phase=RunPhase.AWAITING_APPROVAL,
        ),
    )
    binding = ApprovalBinding(
        tool_name=call.name,
        tool_version=call.version,
        definition_fingerprint=call.definition_fingerprint,
        args_hash=call.args_hash,
        workspace_id=call.workspace_id,
        session_id=bundle.run.session_id,
        principal_id="principal_1",
        root_run_id=call.lineage.root_run_id,
        run_id=call.run_id,
        agent_name=call.lineage.agent_name,
        ancestor_run_ids=call.lineage.ancestor_run_ids,
        expected_state_hash=None,
        expires_at=NOW.replace(hour=10),
    )
    approval_id = approval_id_for(call.tool_call_id, binding)
    approval = ApprovalRequest(
        approval_id=approval_id,
        tool_call_id=call.tool_call_id,
        binding=binding,
        risk=RiskClass.WRITE,
        summary="approve recovered write",
        diff_artifact_ids=(),
    )
    bundle = replace(
        bundle,
        state=replace(
            bundle.state,
            pending=replace(bundle.state.pending, approval_ids=frozenset({approval_id})),
        ),
    )
    factory = SqliteUnitOfWorkFactory(database_path)
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, bundle)
        await unit_of_work.entities.put(
            "approvals",
            approval_id,
            ApprovalRecord(approval, ApprovalState.PENDING, 1),
            expected_revision=0,
        )
        await unit_of_work.commit()

    valid = await RecoveryCoordinator(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        registry=ToolRegistry("restart", (write,)),
        clock=ManualClock(NOW),
    ).scan()
    assert valid[0].disposition is RecoveryDisposition.RESUME
    assert valid[0].actions[0].kind is RecoveryActionKind.REPLAY_ORIGINAL_CALL

    expired = await RecoveryCoordinator(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        registry=ToolRegistry("restart", (write,)),
        clock=ManualClock(NOW.replace(hour=11)),
    ).scan()
    assert expired[0].disposition is RecoveryDisposition.INTERRUPT
    assert "pending_approval_expired" in {issue.code for issue in expired[0].issues}


@pytest.mark.asyncio
async def test_budget_counters_reservations_and_deadline_are_strict_recovery_invariants(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "budget-invariants.sqlite"
    read = _definition("workspace.read", SideEffectClass.READ)

    def bundle_for(suffix: str) -> _RunBundle:
        return _bundle(suffix, (_call(read, suffix, f"call-{suffix}"),))

    model_mismatch = bundle_for("run-budget-model")
    assert model_mismatch.state.budget_checkpoint is not None
    model_mismatch = replace(
        model_mismatch,
        state=replace(
            model_mismatch.state,
            budget_checkpoint=replace(
                model_mismatch.state.budget_checkpoint,
                used=BudgetDelta(model_rounds=0, tool_calls=1),
            ),
        ),
    )
    tool_mismatch = bundle_for("run-budget-tool")
    assert tool_mismatch.state.budget_checkpoint is not None
    tool_mismatch = replace(
        tool_mismatch,
        state=replace(
            tool_mismatch.state,
            budget_checkpoint=replace(
                tool_mismatch.state.budget_checkpoint,
                used=BudgetDelta(model_rounds=1, tool_calls=0),
            ),
        ),
    )
    unsettled_reservation = bundle_for("run-budget-reservation")
    assert unsettled_reservation.state.budget_checkpoint is not None
    unsettled_reservation = replace(
        unsettled_reservation,
        state=replace(
            unsettled_reservation.state,
            budget_checkpoint=replace(
                unsettled_reservation.state.budget_checkpoint,
                reserved=BudgetDelta(model_rounds=1),
            ),
        ),
    )
    stray_reservation = bundle_for("run-budget-stray-reservation")
    assert stray_reservation.state.budget_checkpoint is not None
    stray_reservation = replace(
        stray_reservation,
        state=replace(
            stray_reservation.state,
            budget_checkpoint=replace(
                stray_reservation.state.budget_checkpoint,
                reserved=BudgetDelta(model_rounds=1, artifact_bytes=1),
            ),
        ),
    )
    deadline_missing = bundle_for("run-deadline-missing")
    deadline_missing = replace(deadline_missing, run=replace(deadline_missing.run, deadline_at=None))
    deadline_mismatch = bundle_for("run-deadline-mismatch")
    assert deadline_mismatch.run.deadline_at is not None
    deadline_mismatch = replace(
        deadline_mismatch,
        run=replace(deadline_mismatch.run, deadline_at=deadline_mismatch.run.deadline_at + timedelta(seconds=1)),
    )
    start_mismatch = bundle_for("run-budget-start-mismatch")
    start_mismatch = replace(
        start_mismatch,
        run=replace(start_mismatch.run, created_at=NOW - timedelta(seconds=1)),
    )
    future_checkpoint = bundle_for("run-budget-future")
    assert future_checkpoint.state.budget_checkpoint is not None
    future_checkpoint = replace(
        future_checkpoint,
        state=replace(
            future_checkpoint.state,
            budget_checkpoint=replace(
                future_checkpoint.state.budget_checkpoint,
                captured_at=NOW + timedelta(seconds=1),
                elapsed_seconds=1,
            ),
        ),
    )
    factory = SqliteUnitOfWorkFactory(database_path)
    async with factory.begin() as unit_of_work:
        for bundle in (
            model_mismatch,
            tool_mismatch,
            unsettled_reservation,
            stray_reservation,
            deadline_missing,
            deadline_mismatch,
            start_mismatch,
            future_checkpoint,
        ):
            await _persist_bundle(unit_of_work, bundle)
        await unit_of_work.commit()

    plans = await RecoveryCoordinator(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        registry=ToolRegistry("restart", (read,)),
        clock=ManualClock(NOW),
    ).scan()
    issues_by_run = {plan.run_id: {issue.code for issue in plan.issues} for plan in plans}
    assert "budget_checkpoint_counter_mismatch" in issues_by_run["run-budget-model"]
    assert "budget_checkpoint_counter_mismatch" in issues_by_run["run-budget-tool"]
    assert "budget_checkpoint_reservation_present" in issues_by_run["run-budget-reservation"]
    assert "budget_checkpoint_reservation_present" in issues_by_run["run-budget-stray-reservation"]
    assert "run_deadline_missing" in issues_by_run["run-deadline-missing"]
    assert "run_deadline_budget_mismatch" in issues_by_run["run-deadline-mismatch"]
    assert "run_budget_start_mismatch" in issues_by_run["run-budget-start-mismatch"]
    assert "budget_checkpoint_from_future" in issues_by_run["run-budget-future"]
    assert all(plan.disposition is RecoveryDisposition.INTERRUPT for plan in plans)


@pytest.mark.asyncio
async def test_expired_absolute_run_deadline_interrupts_before_recovery_apply(tmp_path: Path) -> None:
    database_path = tmp_path / "expired-deadline.sqlite"
    read = _definition("workspace.read", SideEffectClass.READ)
    bundle = _bundle("run-expired-deadline", (_call(read, "run-expired-deadline", "call-expired"),))
    factory = SqliteUnitOfWorkFactory(database_path)
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, bundle)
        await unit_of_work.commit()

    plans = await RecoveryCoordinator(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        registry=ToolRegistry("restart", (read,)),
        clock=ManualClock(NOW + timedelta(seconds=300)),
    ).scan()
    assert plans[0].disposition is RecoveryDisposition.INTERRUPT
    assert "run_deadline_expired" in {issue.code for issue in plans[0].issues}
