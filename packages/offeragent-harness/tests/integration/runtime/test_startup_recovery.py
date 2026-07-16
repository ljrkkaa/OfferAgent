from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.agent import BudgetCheckpoint, BudgetDelta, BudgetLedger, RunBudget
from offeragent_harness.agent.loop import AgentLoopFailure, ToolExecution
from offeragent_harness.agent.planner import Planner, PlanningAttempt, PlanningAttemptOutcome, PlanningStep
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.app import ApplicationNotReady, create_application, start_application
from offeragent_harness.models import ModelUsage, thaw_json
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import CancellationToken, EntityRecord, NewEvent, ToolLifecycleObserver
from offeragent_harness.protocol.events import (
    EventType,
    ToolCompletedPayload,
    make_domain_event_record,
    parse_persisted_domain_event,
)
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.runtime.cancellation import CancellationScope
from offeragent_harness.runtime.harness_service import (
    CreateSessionCommand,
    HarnessService,
    RecoveryResumeRejected,
    RunComponents,
    StartTurnCommand,
)
from offeragent_harness.runtime.recovery import RecoveryCoordinator, RecoveryDisposition
from offeragent_harness.runtime.recovery_apply import RecoveryApplyResult, RecoveryPlanApplier
from offeragent_harness.runtime.startup import (
    RuntimeStartupBlocked,
    RuntimeStartupCoordinator,
    StartupFailurePhase,
)
from offeragent_harness.runtime.turn_manager import ActiveRun, RunControlInbox, RunFactory, TurnManager
from offeragent_harness.sessions import (
    AgentLineage,
    Run,
    RunKind,
    RunStatus,
    Session,
    SessionStatus,
    TerminationReason,
    Turn,
    TurnStatus,
)
from offeragent_harness.testing import DeterministicIdGenerator, ManualClock, RecordingEventSink
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
)
from offeragent_harness.tools.registry import ToolRegistry

STARTED = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
RECOVERY_NOW = STARTED + timedelta(seconds=30)
WORKSPACE_ID = "ws_startup_recovery"
BUDGET = RunBudget(
    max_model_rounds=8,
    max_tool_calls=20,
    max_parallel_reads=4,
    max_wall_seconds=300,
    max_input_tokens=20_000,
    max_output_tokens=8_000,
    max_cost=Decimal("10"),
    max_artifact_bytes=1_000_000,
    max_subagents=4,
)


@dataclass(frozen=True, slots=True)
class _CrashFixture:
    session: Session
    run: Run
    state: RunState
    turn: Turn
    write: ToolDefinition
    read: ToolDefinition
    completed_call: ToolCall
    replay_call: ToolCall
    completed_result: ToolResult
    replay_result: ToolResult


class _GateStopPlanner:
    def __init__(self, *, blocked: bool = True) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.calls = 0
        self.states: list[RunState] = []

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        self.calls += 1
        self.states.append(state)
        self.entered.set()
        await self.release.wait()
        cancellation.checkpoint()
        return PlanningStep(
            (),
            False,
            "done",
            attempts=(
                PlanningAttempt(
                    request_id=f"test-gate-{state.model_rounds + 1}",
                    repair_index=0,
                    outcome=PlanningAttemptOutcome.SUCCEEDED,
                    usage=ModelUsage(0, 0, 0, 0),
                ),
            ),
        )


class _PhantomTerminalPlanner:
    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        phantom = replace(state, phase=RunPhase.FAILED, revision=state.revision + 1)
        raise AgentLoopFailure(phantom, OSError("terminal recorder commit was not confirmed"))


class _ReplayKernel:
    def __init__(
        self,
        *,
        factory: SqliteUnitOfWorkFactory,
        clock: ManualClock,
        definitions: Mapping[str, ToolDefinition],
        results: Mapping[str, ToolResult],
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._definitions = definitions
        self._results = results
        self.batches: list[tuple[ToolCall, ...]] = []

    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        cancellation.checkpoint()
        assert observer is not None
        batch = tuple(calls)
        self.batches.append(batch)
        for call in batch:
            await observer.execution_started(call, self._definitions[call.name])
        executions: list[ToolExecution] = []
        for call in batch:
            definition = self._definitions[call.name]
            result = self._results[call.tool_call_id]
            async with self._factory.begin() as unit_of_work:
                await unit_of_work.journal.complete(
                    invocation_journal_scope(call, definition),
                    call.idempotency_key,
                    invocation_request_fingerprint(call),
                    result,
                    self._clock.utcnow(),
                )
                await unit_of_work.commit()
            await observer.result_available(call, definition, result)
            executions.append(ToolExecution(call, definition, result))
        return tuple(executions)


class _Components:
    def __init__(
        self,
        *,
        planner: Planner,
        kernel: _ReplayKernel,
        budget: RunBudget = BUDGET,
    ) -> None:
        self.planner = planner
        self.kernel = kernel
        self.budget = budget
        self.build_count = 0
        self.planner_budgets: list[BudgetLedger] = []
        self.kernel_budgets: list[BudgetLedger] = []

    def _planner(self, budget: BudgetLedger) -> Planner:
        self.planner_budgets.append(budget)
        return self.planner

    def _kernel(self, budget: BudgetLedger) -> _ReplayKernel:
        self.kernel_budgets.append(budget)
        return self.kernel

    def build(self, command: StartTurnCommand, state: RunState) -> RunComponents:
        del command, state
        self.build_count += 1
        return RunComponents(
            planner_factory=self._planner,
            tool_kernel_factory=self._kernel,
            budget=self.budget,
        )


class _BlockingSecondRegistrationTurnManager(TurnManager):
    def __init__(self) -> None:
        super().__init__(max_active_runs=4)
        self.calls = 0
        self.second_entered = asyncio.Event()

    async def start(
        self,
        *,
        session_id: str,
        run_id: str,
        factory: RunFactory,
        controls: RunControlInbox | None = None,
    ) -> ActiveRun:
        self.calls += 1
        if self.calls == 2:
            self.second_entered.set()
            await asyncio.Future()
        return await super().start(session_id=session_id, run_id=run_id, factory=factory, controls=controls)


class _RecordingApprovalManager(ApprovalManager):
    def __init__(self, *, factory: SqliteUnitOfWorkFactory, clock: ManualClock) -> None:
        super().__init__(unit_of_work=factory, clock=clock)
        self.revoked: list[str] = []

    async def revoke_run_grants(self, root_run_id: str, *, reason: str) -> None:
        del reason
        self.revoked.append(root_run_id)


class _RecordingSubagentRecovery:
    def __init__(self, recovered: tuple[str, ...] = ("run_child",)) -> None:
        self.recovered = recovered
        self.calls = 0

    async def recover(self) -> tuple[str, ...]:
        self.calls += 1
        return self.recovered


def _definition(name: str, side_effect_class: SideEffectClass) -> ToolDefinition:
    side_effect_free = side_effect_class in {SideEffectClass.NONE, SideEffectClass.READ}
    return ToolDefinition(
        name=name,
        version="1",
        description=f"{name} startup recovery definition",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.READ if side_effect_free else RiskClass.WRITE,
        side_effect_class=side_effect_class,
        required_capabilities=frozenset({f"test.{name}"}),
        concurrency_safe=side_effect_free,
        idempotent=True,
        retryable=side_effect_free,
        timeout_ms=5_000,
        output_limit_bytes=64 * 1024,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def _call(definition: ToolDefinition, run_id: str, call_id: str) -> ToolCall:
    arguments = {"path": f"tests/{call_id}.md"}
    return ToolCall(
        tool_call_id=call_id,
        run_id=run_id,
        workspace_id=WORKSPACE_ID,
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
    write = definition.side_effect_class is SideEffectClass.WRITE
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data={"path": call.arguments["path"]},
        user_visible_summary="工具已完成",
        artifact_ids=(),
        source_refs=(),
        side_effects=(
            SideEffect(
                SideEffectKind.FILE_WRITE if write else SideEffectKind.READ,
                SideEffectState.COMMITTED if write else SideEffectState.OBSERVED,
                f"vault:{call.arguments['path']}",
                {"hash": "before"} if write else None,
                {"hash": "after"} if write else None,
            ),
        ),
        retryable=False,
        before_state=None,
        after_state=None,
        error=None,
    )


def _fixture(suffix: str) -> _CrashFixture:
    run_id = f"run_{suffix}"
    session_id = f"ses_{suffix}"
    turn_id = f"turn_{suffix}"
    lineage = AgentLineage.root(run_id)
    write = _definition("vault.write.startup", SideEffectClass.WRITE)
    read = _definition("workspace.read.startup", SideEffectClass.READ)
    completed_call = _call(write, run_id, f"call_{suffix}_write")
    replay_call = _call(read, run_id, f"call_{suffix}_read")
    checkpoint = BudgetCheckpoint(
        budget=BUDGET,
        started_at=STARTED,
        used=BudgetDelta(model_rounds=1, tool_calls=2),
        reserved=BudgetDelta(),
        captured_at=STARTED + timedelta(seconds=10),
        elapsed_seconds=10,
    )
    state = RunState(
        workspace_id=WORKSPACE_ID,
        session_id=session_id,
        turn_id=turn_id,
        run_id=run_id,
        lineage=lineage,
        phase=RunPhase.EXECUTING_TOOLS,
        revision=7,
        model_rounds=1,
        budget_checkpoint=checkpoint,
    ).accept_tool_calls((completed_call, replay_call))
    return _CrashFixture(
        session=Session(
            session_id=session_id,
            workspace_id=WORKSPACE_ID,
            profile_id="profile_startup_recovery",
            title=f"Recovery {suffix}",
            status=SessionStatus.ACTIVE,
            created_at=STARTED,
            updated_at=STARTED,
            revision=2,
        ),
        run=Run(
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
            workspace_id=WORKSPACE_ID,
            lineage=lineage,
            kind=RunKind.ROOT,
            status=RunStatus.EXECUTING_TOOLS,
            attempt=1,
            event_sequence=1,
            config_snapshot={"model": "fake"},
            created_at=STARTED,
            updated_at=checkpoint.captured_at,
            deadline_at=STARTED + timedelta(seconds=BUDGET.max_wall_seconds),
        ),
        state=state,
        turn=Turn(
            turn_id=turn_id,
            session_id=session_id,
            ordinal=1,
            status=TurnStatus.RUNNING,
            input_blocks=({"type": "text", "text": "recover"},),
            created_at=STARTED,
            updated_at=STARTED,
        ),
        write=write,
        read=read,
        completed_call=completed_call,
        replay_call=replay_call,
        completed_result=_result(completed_call, write),
        replay_result=_result(replay_call, read),
    )


async def _persist_fixture(
    factory: SqliteUnitOfWorkFactory,
    fixture: _CrashFixture,
    *,
    lease_run_id: str | None = None,
) -> None:
    record = make_domain_event_record(
        event_type=EventType.TURN_STARTED,
        payload={
            "input": thaw_json(fixture.turn.input_blocks),
            "runConfig": thaw_json(fixture.run.config_snapshot),
            "attempt": 1,
        },
        trace_id=f"trace_{fixture.run.run_id.removeprefix('run_')}",
        workspace_id=fixture.run.workspace_id,
        session_id=fixture.run.session_id,
        turn_id=fixture.run.turn_id,
        run_id=fixture.run.run_id,
        root_run_id=fixture.run.run_id,
        parent_run_id=None,
        state_revision=0,
    )
    event = NewEvent(
        event_id=f"evt_{fixture.run.run_id.removeprefix('run_')}_started",
        event_type=EventType.TURN_STARTED.value,
        payload=record.to_wire(),
        occurred_at=STARTED,
        terminal=False,
        idempotency_key=f"{fixture.run.run_id}:1:turn.started",
    )
    async with factory.begin() as unit_of_work:
        await unit_of_work.entities.put("sessions", fixture.session.session_id, fixture.session, expected_revision=0)
        await unit_of_work.entities.put("turns", fixture.turn.turn_id, fixture.turn, expected_revision=0)
        await unit_of_work.entities.put("runs", fixture.run.run_id, fixture.run, expected_revision=0)
        await unit_of_work.entities.put("run_states", fixture.run.run_id, fixture.state, expected_revision=0)
        await unit_of_work.entities.put(
            "active_root_runs",
            fixture.run.session_id,
            {
                "schemaVersion": 1,
                "workspaceId": fixture.run.workspace_id,
                "sessionId": fixture.run.session_id,
                "runId": lease_run_id or fixture.run.run_id,
                "acquiredAt": STARTED.isoformat(),
            },
            expected_revision=0,
        )
        await unit_of_work.events.append(fixture.run.run_id, 0, (event,))
        await unit_of_work.journal.start(
            invocation_journal_scope(fixture.completed_call, fixture.write),
            fixture.completed_call.idempotency_key,
            invocation_request_fingerprint(fixture.completed_call),
            STARTED,
        )
        await unit_of_work.journal.complete(
            invocation_journal_scope(fixture.completed_call, fixture.write),
            fixture.completed_call.idempotency_key,
            invocation_request_fingerprint(fixture.completed_call),
            fixture.completed_result,
            STARTED + timedelta(seconds=5),
        )
        await unit_of_work.journal.start(
            invocation_journal_scope(fixture.replay_call, fixture.read),
            fixture.replay_call.idempotency_key,
            invocation_request_fingerprint(fixture.replay_call),
            STARTED + timedelta(seconds=6),
        )
        await unit_of_work.commit()


def _registry(*fixtures: _CrashFixture) -> ToolRegistry:
    definitions: dict[tuple[str, str], ToolDefinition] = {}
    for fixture in fixtures:
        for definition in (fixture.write, fixture.read):
            definitions[(definition.name, definition.version)] = definition
    return ToolRegistry("startup", tuple(definitions.values()))


def _harness(
    *,
    factory: SqliteUnitOfWorkFactory,
    clock: ManualClock,
    sink: RecordingEventSink,
    components: _Components,
    manager: TurnManager | None = None,
) -> tuple[HarnessService, TurnManager]:
    resolved_manager = manager or TurnManager(max_active_runs=4)
    return (
        HarnessService(
            unit_of_work=factory,
            event_sink=sink,
            clock=clock,
            ids=DeterministicIdGenerator(),
            components=components,
            turn_manager=resolved_manager,
        ),
        resolved_manager,
    )


def _startup(
    *,
    factory: SqliteUnitOfWorkFactory,
    clock: ManualClock,
    registry: ToolRegistry,
    harness: HarnessService,
    subagent_recovery: _RecordingSubagentRecovery | None = None,
) -> RuntimeStartupCoordinator:
    return RuntimeStartupCoordinator(
        recovery=RecoveryCoordinator(
            unit_of_work=factory,
            registry=registry,
            clock=clock,
        ),
        applier=RecoveryPlanApplier(
            unit_of_work=factory,
            clock=clock,
            ids=DeterministicIdGenerator(),
        ),
        harness=harness,
        subagent_recovery=subagent_recovery,
    )


async def _apply_results(
    factory: SqliteUnitOfWorkFactory,
    clock: ManualClock,
    registry: ToolRegistry,
) -> tuple[RecoveryApplyResult, ...]:
    plans = await RecoveryCoordinator(unit_of_work=factory, registry=registry, clock=clock).scan()
    applier = RecoveryPlanApplier(
        unit_of_work=factory,
        clock=clock,
        ids=DeterministicIdGenerator(),
    )
    return tuple([await applier.apply(plan) for plan in plans])


async def _lease_record(factory: SqliteUnitOfWorkFactory, session_id: str) -> EntityRecord | None:
    records = await factory.list_entities("active_root_runs")
    return next((record for record in records if record.entity_id == session_id), None)


@pytest.mark.asyncio
async def test_startup_reconciles_subagent_reservations_before_reporting_ready(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "startup-empty.sqlite")
    clock = ManualClock(RECOVERY_NOW)
    planner = _GateStopPlanner(blocked=False)
    kernel = _ReplayKernel(factory=factory, clock=clock, definitions={}, results={})
    components = _Components(planner=planner, kernel=kernel)
    sink = RecordingEventSink()
    harness, manager = _harness(factory=factory, clock=clock, sink=sink, components=components)
    child_recovery = _RecordingSubagentRecovery()

    report = await _startup(
        factory=factory,
        clock=clock,
        registry=ToolRegistry("empty", ()),
        harness=harness,
        subagent_recovery=child_recovery,
    ).bootstrap()

    assert child_recovery.calls == 1
    assert report.recovered_subagent_run_ids == ("run_child",)
    await manager.shutdown(grace_seconds=0)


@pytest.mark.asyncio
async def test_sqlite_bootstrap_persists_recovered_result_then_replays_original_call_before_planner(
    tmp_path: Path,
) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "startup.sqlite")
    fixture = _fixture("main")
    await _persist_fixture(factory, fixture)
    clock = ManualClock(RECOVERY_NOW)
    planner = _GateStopPlanner()
    kernel = _ReplayKernel(
        factory=factory,
        clock=clock,
        definitions={fixture.read.name: fixture.read},
        results={fixture.replay_call.tool_call_id: fixture.replay_result},
    )
    components = _Components(planner=planner, kernel=kernel)
    sink = RecordingEventSink(acknowledgement_loss_calls=frozenset({1}))
    harness, manager = _harness(
        factory=factory,
        clock=clock,
        sink=sink,
        components=components,
    )
    startup = _startup(
        factory=factory,
        clock=clock,
        registry=_registry(fixture),
        harness=harness,
    )

    report = await startup.bootstrap()
    await planner.entered.wait()
    repeated = await startup.bootstrap()

    assert repeated is report
    assert report.plans_scanned == 1
    assert report.resumed_run_ids == (fixture.run.run_id,)
    assert report.recovery_delivery_failures == 1
    assert report.delivery_degraded
    assert len(report.active_runs) == 1
    assert kernel.batches == [(fixture.replay_call,)]
    assert kernel.batches[0][0].idempotency_key == fixture.replay_call.idempotency_key
    assert planner.calls == 1
    assert [result.tool_call_id for result in planner.states[0].tool_results] == [
        fixture.completed_call.tool_call_id,
        fixture.replay_call.tool_call_id,
    ]
    assert components.planner_budgets[0] is components.kernel_budgets[0]
    assert components.planner_budgets[0].started_at == STARTED
    lease = await _lease_record(factory, fixture.run.session_id)
    assert lease is not None and lease.revision == 1
    assert len(await manager.active_runs()) == 1

    recovery_events = await factory.event_store.read(fixture.run.run_id)
    assert [event.event_type for event in recovery_events[:3]] == [
        EventType.TURN_STARTED.value,
        EventType.TOOL_COMPLETED.value,
        EventType.RUNTIME_WARNING.value,
    ]
    parsed_recovered = parse_persisted_domain_event(recovery_events[1].payload)
    assert isinstance(parsed_recovered.payload, ToolCompletedPayload)
    assert parsed_recovered.payload.result.tool_call_id == fixture.completed_call.tool_call_id

    planner.release.set()
    terminal_state = await report.active_runs[0].task
    assert terminal_state.phase is RunPhase.COMPLETED
    assert [result.tool_call_id for result in terminal_state.tool_results] == [
        fixture.completed_call.tool_call_id,
        fixture.replay_call.tool_call_id,
    ]
    events = await factory.event_store.read(fixture.run.run_id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[-1].event_type == EventType.TURN_COMPLETED.value
    completed_ids = []
    for event in events:
        parsed = parse_persisted_domain_event(event.payload)
        if isinstance(parsed.payload, ToolCompletedPayload):
            completed_ids.append(parsed.payload.result.tool_call_id)
    assert completed_ids == [fixture.completed_call.tool_call_id, fixture.replay_call.tool_call_id]
    stored_run = await factory.get_entity("runs", fixture.run.run_id)
    assert isinstance(stored_run, Run)
    assert stored_run.status is RunStatus.COMPLETED
    assert stored_run.event_sequence == events[-1].sequence
    assert stored_run.termination_reason is TerminationReason.COMPLETED
    assert await factory.get_entity("active_root_runs", fixture.run.session_id) is None
    replay_journal = await factory.get_journal(
        invocation_journal_scope(fixture.replay_call, fixture.read),
        fixture.replay_call.idempotency_key,
    )
    assert replay_journal is not None and replay_journal.result == fixture.replay_result

    await asyncio.sleep(0)
    second = _startup(
        factory=factory,
        clock=clock,
        registry=_registry(fixture),
        harness=harness,
    )
    second_report = await second.bootstrap()
    assert second_report.plans_scanned == 0
    assert second_report.active_runs == ()


@pytest.mark.asyncio
async def test_apply_block_in_batch_starts_zero_loops_and_preserves_blocked_lease(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "blocked.sqlite")
    valid = _fixture("a_valid")
    blocked = _fixture("z_blocked")
    await _persist_fixture(factory, valid)
    await _persist_fixture(factory, blocked, lease_run_id="run_other_owner")
    clock = ManualClock(RECOVERY_NOW)
    planner = _GateStopPlanner(blocked=False)
    kernel = _ReplayKernel(factory=factory, clock=clock, definitions={}, results={})
    components = _Components(planner=planner, kernel=kernel)
    sink = RecordingEventSink()
    harness, manager = _harness(factory=factory, clock=clock, sink=sink, components=components)
    startup = _startup(
        factory=factory,
        clock=clock,
        registry=_registry(valid, blocked),
        harness=harness,
    )

    with pytest.raises(RuntimeStartupBlocked) as captured:
        await startup.bootstrap()

    assert captured.value.phase is StartupFailurePhase.APPLY
    assert captured.value.run_id == blocked.run.run_id
    assert [result.run.run_id for result in captured.value.applied_results] == [valid.run.run_id]
    assert components.build_count == 0
    assert planner.calls == 0
    assert kernel.batches == []
    assert await manager.active_runs() == ()
    assert sink.publish_calls == 0
    blocked_lease = await _lease_record(factory, blocked.run.session_id)
    assert blocked_lease is not None and blocked_lease.value["runId"] == "run_other_owner"
    blocked_events = await factory.event_store.read(blocked.run.run_id)
    assert len(blocked_events) == 1
    assert blocked_events[0].event_type == EventType.TURN_STARTED.value
    assert not blocked_events[0].terminal


@pytest.mark.asyncio
async def test_expired_deadline_is_terminally_applied_before_any_resume(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "expired.sqlite")
    fixture = _fixture("expired")
    await _persist_fixture(factory, fixture)
    clock = ManualClock(STARTED + timedelta(seconds=BUDGET.max_wall_seconds + 1))
    planner = _GateStopPlanner(blocked=False)
    kernel = _ReplayKernel(factory=factory, clock=clock, definitions={}, results={})
    components = _Components(planner=planner, kernel=kernel)
    harness, manager = _harness(
        factory=factory,
        clock=clock,
        sink=RecordingEventSink(),
        components=components,
    )

    report = await _startup(
        factory=factory,
        clock=clock,
        registry=_registry(fixture),
        harness=harness,
    ).bootstrap()

    assert report.resumed_run_ids == ()
    assert report.terminalized_run_ids == (fixture.run.run_id,)
    assert report.applied_results[0].disposition is RecoveryDisposition.INTERRUPT
    assert report.applied_results[0].event.event_type == EventType.TURN_INTERRUPTED.value
    assert components.build_count == 0
    assert await manager.active_runs() == ()
    assert await factory.get_entity("active_root_runs", fixture.run.session_id) is None
    run = await factory.get_entity("runs", fixture.run.run_id)
    assert isinstance(run, Run) and run.status is RunStatus.INTERRUPTED
    with pytest.raises(RecoveryResumeRejected) as rejected:
        await harness.resume_recovered_run(report.applied_results[0])
    assert rejected.value.code == "recovery_disposition_not_resumable"


@pytest.mark.asyncio
async def test_budget_or_authoritative_config_drift_fails_closed_before_loop(tmp_path: Path) -> None:
    budget_factory = SqliteUnitOfWorkFactory(tmp_path / "budget-drift.sqlite")
    budget_fixture = _fixture("budget_drift")
    await _persist_fixture(budget_factory, budget_fixture)
    clock = ManualClock(RECOVERY_NOW)
    planner = _GateStopPlanner(blocked=False)
    kernel = _ReplayKernel(factory=budget_factory, clock=clock, definitions={}, results={})
    components = _Components(
        planner=planner,
        kernel=kernel,
        budget=replace(BUDGET, max_tool_calls=BUDGET.max_tool_calls + 1),
    )
    harness, manager = _harness(
        factory=budget_factory,
        clock=clock,
        sink=RecordingEventSink(),
        components=components,
    )
    startup = _startup(
        factory=budget_factory,
        clock=clock,
        registry=_registry(budget_fixture),
        harness=harness,
    )

    with pytest.raises(RuntimeStartupBlocked) as budget_error:
        await startup.bootstrap()
    assert budget_error.value.phase is StartupFailurePhase.RESUME
    assert isinstance(budget_error.value.cause, RecoveryResumeRejected)
    assert budget_error.value.cause.code == "budget_config_drift"
    assert await manager.active_runs() == ()
    assert planner.calls == 0

    config_factory = SqliteUnitOfWorkFactory(tmp_path / "config-drift.sqlite")
    config_fixture = _fixture("config_drift")
    await _persist_fixture(config_factory, config_fixture)
    config_clock = ManualClock(RECOVERY_NOW)
    results = await _apply_results(config_factory, config_clock, _registry(config_fixture))
    result = results[0]
    async with config_factory.begin() as unit_of_work:
        await unit_of_work.entities.put(
            "runs",
            result.run.run_id,
            replace(result.run, config_snapshot={"model": "changed"}),
            expected_revision=result.run_entity_revision,
        )
        await unit_of_work.commit()
    config_planner = _GateStopPlanner(blocked=False)
    config_kernel = _ReplayKernel(factory=config_factory, clock=config_clock, definitions={}, results={})
    config_components = _Components(planner=config_planner, kernel=config_kernel)
    config_harness, config_manager = _harness(
        factory=config_factory,
        clock=config_clock,
        sink=RecordingEventSink(),
        components=config_components,
    )

    with pytest.raises(RecoveryResumeRejected) as config_error:
        await config_harness.resume_recovered_run(result)
    assert config_error.value.code == "recovery_authority_changed"
    assert config_components.build_count == 0
    assert await config_manager.active_runs() == ()


@pytest.mark.asyncio
async def test_caller_cancel_mid_registration_cleans_every_turn_manager_placeholder(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "registration-cancel.sqlite")
    first = _fixture("cancel_first")
    second = _fixture("cancel_second")
    await _persist_fixture(factory, first)
    await _persist_fixture(factory, second)
    clock = ManualClock(RECOVERY_NOW)
    results = await _apply_results(factory, clock, _registry(first, second))
    planner = _GateStopPlanner(blocked=False)
    kernel = _ReplayKernel(factory=factory, clock=clock, definitions={}, results={})
    components = _Components(planner=planner, kernel=kernel)
    manager = _BlockingSecondRegistrationTurnManager()
    harness, _ = _harness(
        factory=factory,
        clock=clock,
        sink=RecordingEventSink(),
        components=components,
        manager=manager,
    )

    resume = asyncio.create_task(harness.resume_recovered_runs(results))
    await manager.second_entered.wait()
    resume.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resume
    for _ in range(3):
        await asyncio.sleep(0)

    assert await manager.active_runs() == ()
    assert planner.calls == 0
    assert kernel.batches == []


@pytest.mark.asyncio
async def test_recovered_run_crossing_deadline_in_turn_manager_queue_terminally_cancels_without_replay(
    tmp_path: Path,
) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "queue-deadline.sqlite")
    fixture = _fixture("queue_deadline")
    await _persist_fixture(factory, fixture)
    clock = ManualClock(RECOVERY_NOW)
    result = (await _apply_results(factory, clock, _registry(fixture)))[0]
    planner = _GateStopPlanner(blocked=False)
    kernel = _ReplayKernel(factory=factory, clock=clock, definitions={}, results={})
    components = _Components(planner=planner, kernel=kernel)
    manager = TurnManager(max_active_runs=1)
    harness, _ = _harness(
        factory=factory,
        clock=clock,
        sink=RecordingEventSink(),
        components=components,
        manager=manager,
    )
    blocker_release = asyncio.Event()

    async def block_slot(_cancellation: CancellationScope) -> None:
        await blocker_release.wait()

    blocker = await manager.start(session_id="ses_queue_blocker", run_id="run_queue_blocker", factory=block_slot)
    active = await harness.resume_recovered_run(result)
    clock.advance(timedelta(seconds=BUDGET.max_wall_seconds + 1))
    blocker_release.set()
    await blocker.task
    terminal = await active.task

    assert terminal.phase is RunPhase.FAILED
    assert planner.calls == 0
    assert kernel.batches == []
    run = await factory.get_entity("runs", fixture.run.run_id)
    assert isinstance(run, Run)
    assert run.status is RunStatus.FAILED
    assert run.termination_reason is TerminationReason.BUDGET_EXHAUSTED
    assert await factory.get_entity("active_root_runs", fixture.run.session_id) is None


@pytest.mark.asyncio
async def test_fresh_run_crossing_deadline_in_turn_manager_queue_enters_loop_only_to_persist_budget_terminal(
    tmp_path: Path,
) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "fresh-queue-deadline.sqlite")
    clock = ManualClock(STARTED)
    planner = _GateStopPlanner(blocked=False)
    kernel = _ReplayKernel(factory=factory, clock=clock, definitions={}, results={})
    components = _Components(planner=planner, kernel=kernel)
    manager = TurnManager(max_active_runs=1)
    harness, _ = _harness(
        factory=factory,
        clock=clock,
        sink=RecordingEventSink(),
        components=components,
        manager=manager,
    )
    blocker_release = asyncio.Event()

    async def block_slot(_cancellation: CancellationScope) -> None:
        await blocker_release.wait()

    blocker = await manager.start(
        session_id="ses_fresh_queue_blocker",
        run_id="run_fresh_queue_blocker",
        factory=block_slot,
    )
    session = await harness.create_session(
        CreateSessionCommand(WORKSPACE_ID, "profile_fresh_queue", "Fresh queue", "fresh-queue-session")
    )
    receipt = await harness.start_turn(
        StartTurnCommand(
            workspace_id=WORKSPACE_ID,
            session_id=session.session_id,
            turn_id="turn_fresh_queue",
            idempotency_key="fresh-queue-turn",
            input_blocks=({"type": "text", "text": "queued"},),
            run_config={"model": "fake"},
        )
    )
    active = await manager.get(receipt.run_id)
    assert active is not None
    clock.advance(timedelta(seconds=BUDGET.max_wall_seconds + 1))
    blocker_release.set()
    await blocker.task
    terminal = await active.task

    assert terminal.phase is RunPhase.FAILED
    assert planner.calls == 0
    assert kernel.batches == []
    run = await harness.get_run(receipt.run_id)
    assert run.status is RunStatus.FAILED
    assert run.termination_reason is TerminationReason.BUDGET_EXHAUSTED
    assert await factory.get_entity("active_root_runs", session.session_id) is None


@pytest.mark.asyncio
async def test_unconfirmed_terminal_agent_loop_failure_keeps_lease_and_run_grants_for_recovery(
    tmp_path: Path,
) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "terminal-unconfirmed.sqlite")
    clock = ManualClock(STARTED)
    planner = _PhantomTerminalPlanner()
    kernel = _ReplayKernel(factory=factory, clock=clock, definitions={}, results={})
    components = _Components(planner=planner, kernel=kernel)
    manager = TurnManager()
    approvals = _RecordingApprovalManager(factory=factory, clock=clock)
    harness = HarnessService(
        unit_of_work=factory,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=components,
        turn_manager=manager,
        approval_manager=approvals,
    )
    session = await harness.create_session(
        CreateSessionCommand(WORKSPACE_ID, "profile_terminal_failure", "Terminal failure", "terminal-session")
    )
    receipt = await harness.start_turn(
        StartTurnCommand(
            workspace_id=WORKSPACE_ID,
            session_id=session.session_id,
            turn_id="turn_terminal_unconfirmed",
            idempotency_key="terminal-unconfirmed",
            input_blocks=({"type": "text", "text": "fail terminal commit"},),
            run_config={"model": "fake"},
        )
    )
    active = await manager.get(receipt.run_id)
    assert active is not None

    with pytest.raises(AgentLoopFailure):
        await active.task

    persisted_state = await harness.get_run_state(receipt.run_id)
    persisted_run = await harness.get_run(receipt.run_id)
    assert not persisted_state.phase.terminal
    assert not persisted_run.status.is_terminal
    assert await factory.event_store.terminal_event(receipt.run_id) is None
    assert await factory.get_entity("active_root_runs", session.session_id) is not None
    assert approvals.revoked == []


@pytest.mark.asyncio
async def test_application_readiness_gate_requires_successful_idempotent_startup(tmp_path: Path) -> None:
    clock = ManualClock(RECOVERY_NOW)
    registry = ToolRegistry("empty-startup", ())
    first_factory = SqliteUnitOfWorkFactory(tmp_path / "application-ready.sqlite")
    planner = _GateStopPlanner(blocked=False)
    kernel = _ReplayKernel(factory=first_factory, clock=clock, definitions={}, results={})
    application = create_application(
        unit_of_work=first_factory,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=_Components(planner=planner, kernel=kernel),
        recovery_registry=registry,
    )

    assert not application.ready
    with pytest.raises(ApplicationNotReady):
        application.require_ready()
    with pytest.raises(ApplicationNotReady):
        _ = application.harness
    report = await application.start()
    repeated = await application.start()
    assert repeated is report
    assert report.plans_scanned == 0
    assert application.ready
    assert application.require_ready() is application.harness
    await application.shutdown()
    assert not application.ready
    with pytest.raises(ApplicationNotReady):
        application.require_ready()
    with pytest.raises(ApplicationNotReady):
        _ = application.harness

    second_factory = SqliteUnitOfWorkFactory(tmp_path / "start-application.sqlite")
    started = await start_application(
        unit_of_work=second_factory,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=_Components(
            planner=_GateStopPlanner(blocked=False),
            kernel=_ReplayKernel(factory=second_factory, clock=clock, definitions={}, results={}),
        ),
        recovery_registry=registry,
    )
    assert started.ready
    await started.shutdown()


@pytest.mark.asyncio
async def test_application_remains_cold_and_transport_gate_closed_when_startup_is_blocked(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "application-blocked.sqlite")
    fixture = _fixture("application_blocked")
    await _persist_fixture(factory, fixture, lease_run_id="run_other_application")
    clock = ManualClock(RECOVERY_NOW)
    planner = _GateStopPlanner(blocked=False)
    kernel = _ReplayKernel(factory=factory, clock=clock, definitions={}, results={})
    manager = TurnManager()
    application = create_application(
        unit_of_work=factory,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=_Components(planner=planner, kernel=kernel),
        turn_manager=manager,
        recovery_registry=_registry(fixture),
    )

    with pytest.raises(RuntimeStartupBlocked):
        await application.start()

    assert not application.ready
    assert application.startup_report is None
    with pytest.raises(ApplicationNotReady):
        application.require_ready()
    assert await manager.active_runs() == ()
    assert planner.calls == 0
    await application.shutdown()


@pytest.mark.asyncio
async def test_blocked_start_application_closes_internal_delivery_worker_before_reraising(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "start-application-blocked.sqlite")
    fixture = _fixture("start_application_budget_drift")
    await _persist_fixture(factory, fixture)
    clock = ManualClock(RECOVERY_NOW)
    planner = _GateStopPlanner(blocked=False)
    kernel = _ReplayKernel(factory=factory, clock=clock, definitions={}, results={})
    components = _Components(
        planner=planner,
        kernel=kernel,
        budget=replace(BUDGET, max_tool_calls=BUDGET.max_tool_calls + 1),
    )
    manager = TurnManager()
    downstream = RecordingEventSink()
    delivery_tasks_before = {task for task in asyncio.all_tasks() if task.get_name() == "offeragent-event-delivery"}

    with pytest.raises(RuntimeStartupBlocked):
        await start_application(
            unit_of_work=factory,
            event_sink=downstream,
            clock=clock,
            ids=DeterministicIdGenerator(),
            components=components,
            recovery_registry=_registry(fixture),
            turn_manager=manager,
        )
    await asyncio.sleep(0)
    leaked_delivery_tasks = {
        task for task in asyncio.all_tasks() if task.get_name() == "offeragent-event-delivery"
    } - delivery_tasks_before

    assert leaked_delivery_tasks == set()
    assert downstream.publish_calls >= 1
    assert await manager.active_runs() == ()
    assert planner.calls == 0
