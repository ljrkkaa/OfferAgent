from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offeragent_harness.agent import BudgetLedger, RunBudget
from offeragent_harness.agent.context_manager import ContextFragment, ContextLayer
from offeragent_harness.agent.loop import AgentLoopFailure, ToolExecution
from offeragent_harness.agent.planner import Planner, PlanningAttempt, PlanningAttemptOutcome, PlanningStep
from offeragent_harness.agent.preparation import RunPreparationFailure
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.config import HarnessConfig, MemorySettings
from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.models import ModelUsage
from offeragent_harness.ports import CancellationToken, Sensitivity, StoredEvent, ToolLifecycleObserver
from offeragent_harness.protocol.events import TurnFailedPayload, parse_event, stored_event_to_envelope
from offeragent_harness.runtime import CancellationReason, RunCancelled, TurnManager
from offeragent_harness.runtime.cancellation import CancellationCode
from offeragent_harness.runtime.harness_service import (
    CreateSessionCommand,
    HarnessService,
    IdempotencyKeyConflict,
    PreparedRunComponents,
    RetryTurnCommand,
    RunComponents,
    SessionReceipt,
    SessionRunConflict,
    StartTurnCommand,
)
from offeragent_harness.runtime.run_preparation import RunPreparationRequest
from offeragent_harness.sessions import RunStatus, TerminationReason, TurnStatus
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualClock,
    RecordingEventSink,
)
from offeragent_harness.tools import ToolCall, canonical_json_sha256


class StopPlanner:
    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        return PlanningStep(
            (),
            False,
            "done",
            attempts=(
                PlanningAttempt(
                    request_id=f"test-stop-{state.model_rounds + 1}",
                    repair_index=0,
                    outcome=PlanningAttemptOutcome.SUCCEEDED,
                    usage=ModelUsage(0, 0, 0, 0),
                ),
            ),
        )


class WaitingPlanner:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        self.entered.set()
        await cancellation.wait()
        cancellation.checkpoint()
        raise AssertionError("checkpoint must raise")


class StubbornPlanner:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        del state, cancellation
        self.entered.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


class NoToolKernel:
    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        del observer
        raise AssertionError("no tool call expected")


class Components:
    def __init__(self, planner: Planner) -> None:
        self.planner = planner
        self.planner_budgets: list[BudgetLedger] = []
        self.tool_kernel_budgets: list[BudgetLedger] = []

    def _planner(self, budget: BudgetLedger) -> Planner:
        self.planner_budgets.append(budget)
        return self.planner

    def _tool_kernel(self, budget: BudgetLedger) -> NoToolKernel:
        self.tool_kernel_budgets.append(budget)
        return NoToolKernel()

    def build(self, command: StartTurnCommand, state: RunState) -> RunComponents:
        return RunComponents(
            planner_factory=self._planner,
            tool_kernel_factory=self._tool_kernel,
            budget=RunBudget(
                max_model_rounds=4,
                max_tool_calls=4,
                max_parallel_reads=2,
                max_wall_seconds=60,
                max_input_tokens=100,
                max_output_tokens=100,
                max_cost=Decimal("1"),
                max_artifact_bytes=10_000,
                max_subagents=2,
            ),
        )


class DeferredComponents(Components):
    def __init__(
        self,
        planner: Planner,
        uow: InMemoryUnitOfWorkFactory,
        *,
        failure: BaseException | None = None,
    ) -> None:
        super().__init__(planner)
        self.uow = uow
        self.failure = failure
        self.token = object()
        self.prepared_after_durable_start = False
        self.released: list[str] = []

    def build(self, command: StartTurnCommand, state: RunState) -> RunComponents:
        raise AssertionError("synchronous factory must not run when async_components is configured")

    def budget_root(self, command: StartTurnCommand, state: RunState) -> RunBudget:
        del command, state
        return _test_budget()

    async def prepare_root(
        self,
        command: StartTurnCommand,
        state: RunState,
        cancellation: CancellationToken,
        durable_snapshot: Mapping[str, object] | None,
    ) -> PreparedRunComponents:
        del command
        cancellation.checkpoint()
        assert durable_snapshot is None
        run = await self.uow.get_entity("runs", state.run_id)
        events = await self.uow.event_store.read(state.run_id)
        self.prepared_after_durable_start = run is not None and [item.event_type for item in events] == ["turn.started"]
        if self.failure is not None:
            raise self.failure
        return PreparedRunComponents(
            token=self.token,
            durable_snapshot={"kind": "unit-test", "definitionsHash": "sha256:" + "a" * 64},
        )

    def build_prepared_root(
        self,
        command: StartTurnCommand,
        state: RunState,
        prepared: PreparedRunComponents,
    ) -> RunComponents:
        assert prepared.token is self.token
        return RunComponents(self._planner, self._tool_kernel, self.budget_root(command, state))

    def budget_child(self, execution: object, state: RunState) -> RunBudget:
        del execution, state
        raise AssertionError("child preparation is outside this test")

    async def prepare_child(self, *args: object, **kwargs: object) -> PreparedRunComponents:
        del args, kwargs
        raise AssertionError("child preparation is outside this test")

    def build_prepared_child(self, *args: object, **kwargs: object) -> RunComponents:
        del args, kwargs
        raise AssertionError("child preparation is outside this test")

    def release(self, run_id: str) -> None:
        self.released.append(run_id)


def _test_budget() -> RunBudget:
    return RunBudget(
        max_model_rounds=4,
        max_tool_calls=4,
        max_parallel_reads=2,
        max_wall_seconds=60,
        max_input_tokens=100,
        max_output_tokens=100,
        max_cost=Decimal("1"),
        max_artifact_bytes=10_000,
        max_subagents=2,
    )


def service(planner: Planner) -> tuple[HarnessService, TurnManager]:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    uow = InMemoryUnitOfWorkFactory()
    manager = TurnManager()
    harness = HarnessService(
        unit_of_work=uow,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=Components(planner),
        turn_manager=manager,
    )
    return harness, manager


class BlockingEventSink:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def publish(self, events: Sequence[StoredEvent]) -> None:
        # Session creation now has its own authoritative event.  These tests
        # deliberately gate the subsequent turn-start delivery only.
        if events and all(event.event_type == "session.updated" for event in events):
            return
        self.entered.set()
        await self.release.wait()


async def create_session(harness: HarnessService) -> SessionReceipt:
    return await harness.create_session(CreateSessionCommand("ws_main", "profile_main", "Session", "create-session"))


def turn_command(session_id: str, *, config: dict[str, object] | None = None) -> StartTurnCommand:
    return StartTurnCommand(
        workspace_id="ws_main",
        session_id=session_id,
        turn_id="turn_client_1",
        idempotency_key="turn-idem-1",
        input_blocks=({"type": "text", "text": "hello"},),
        run_config=config or {"model": "fake"},
    )


def test_command_boundary_rejects_unbounded_or_noncanonical_input_before_starting_a_run() -> None:
    with pytest.raises(ValueError, match="idempotency_key"):
        CreateSessionCommand("ws_main", "profile_main", "Session", "contains whitespace")
    with pytest.raises(ValueError, match="title"):
        CreateSessionCommand("ws_main", "profile_main", "", "create-session")
    with pytest.raises(ValueError, match="256 content blocks"):
        StartTurnCommand(
            workspace_id="ws_main",
            session_id="ses_main",
            turn_id="turn_main",
            idempotency_key="turn-main",
            input_blocks=tuple({"type": "text", "text": "x"} for _ in range(257)),
            run_config={"model": "fake"},
        )


@pytest.mark.asyncio
async def test_session_and_turn_idempotency_share_one_authoritative_run() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    manager = TurnManager()
    components = Components(StopPlanner())
    harness = HarnessService(
        unit_of_work=InMemoryUnitOfWorkFactory(),
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=components,
        turn_manager=manager,
    )
    session = await create_session(harness)
    duplicate_session = await create_session(harness)
    assert duplicate_session.session_id == session.session_id
    assert not duplicate_session.created

    command = turn_command(session.session_id)
    receipt = await harness.start_turn(command)
    active = await manager.get(receipt.run_id)
    assert active is not None
    result = await active.task
    assert result.phase is RunPhase.COMPLETED
    assert (await harness.get_turn(command.turn_id)).status is TurnStatus.COMPLETED
    stored_run = await harness.get_run(receipt.run_id)
    assert stored_run.status is RunStatus.COMPLETED
    assert stored_run.termination_reason is TerminationReason.COMPLETED
    stored_state = await harness.get_run_state(receipt.run_id)
    assert stored_state.budget_checkpoint is not None
    assert stored_state.budget_checkpoint.used.model_rounds == 1
    assert stored_state.budget_checkpoint.reserved.model_rounds == 0
    assert stored_state.budget_checkpoint.started_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert components.planner_budgets == components.tool_kernel_budgets
    assert len(components.planner_budgets) == 1

    duplicate = await harness.start_turn(command)
    assert duplicate == replace(receipt, duplicate=True)
    events = await harness.replay_events(receipt.run_id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[0].event_type == "turn.started"
    assert events[-1].event_type == "turn.completed"
    assert len([event for event in events if event.terminal]) == 1
    for event in events:
        envelope = stored_event_to_envelope(event)
        assert parse_event(envelope.to_wire()) == envelope
    assert stored_run.deadline_at == datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_async_components_prepare_only_after_durable_start_and_snapshot_is_atomic() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    uow = InMemoryUnitOfWorkFactory()
    manager = TurnManager()
    deferred = DeferredComponents(StopPlanner(), uow)
    harness = HarnessService(
        unit_of_work=uow,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=deferred,
        async_components=deferred,
        turn_manager=manager,
    )
    session = await create_session(harness)

    receipt = await harness.start_turn(turn_command(session.session_id))
    active = await manager.get(receipt.run_id)
    assert active is not None
    assert (await active.task).phase is RunPhase.COMPLETED

    snapshot = await uow.get_entity("run_capability_snapshots", receipt.run_id)
    assert deferred.prepared_after_durable_start
    assert isinstance(snapshot, Mapping)
    assert snapshot["snapshot"] == {
        "kind": "unit-test",
        "definitionsHash": "sha256:" + "a" * 64,
    }
    assert "object at" not in repr(snapshot)
    events = await harness.replay_events(receipt.run_id)
    assert [item.event_type for item in events[:2]] == ["turn.started", "phase.changed"]
    assert deferred.planner_budgets == deferred.tool_kernel_budgets
    assert deferred.released == [receipt.run_id]


@pytest.mark.asyncio
async def test_async_components_failure_uses_one_durable_terminal_commit() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    uow = InMemoryUnitOfWorkFactory()
    manager = TurnManager()
    deferred = DeferredComponents(StopPlanner(), uow, failure=RuntimeError("prepare exploded"))
    harness = HarnessService(
        unit_of_work=uow,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=deferred,
        async_components=deferred,
        turn_manager=manager,
    )
    session = await create_session(harness)

    receipt = await harness.start_turn(turn_command(session.session_id))
    active = await manager.get(receipt.run_id)
    assert active is not None
    with pytest.raises(AgentLoopFailure):
        await active.task

    state = await harness.get_run_state(receipt.run_id)
    run = await harness.get_run(receipt.run_id)
    turn = await harness.get_turn(receipt.turn_id)
    events = await harness.replay_events(receipt.run_id)
    assert deferred.prepared_after_durable_start
    assert state.phase is RunPhase.FAILED and run.status is RunStatus.FAILED
    assert turn.status is TurnStatus.FAILED
    assert [(item.event_type, item.terminal) for item in events] == [
        ("turn.started", False),
        ("turn.failed", True),
    ]
    assert await uow.get_entity("run_capability_snapshots", receipt.run_id) is None
    assert await uow.get_entity("active_root_runs", session.session_id) is None
    assert deferred.released == [receipt.run_id]


@pytest.mark.asyncio
async def test_async_component_preparation_cancellation_uses_the_normal_cancelled_terminal() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    uow = InMemoryUnitOfWorkFactory()
    manager = TurnManager()
    reason = CancellationReason(CancellationCode.USER, "cancel during history preparation", clock.utcnow())
    deferred = DeferredComponents(StopPlanner(), uow, failure=RunCancelled(reason))
    harness = HarnessService(
        unit_of_work=uow,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=deferred,
        async_components=deferred,
        turn_manager=manager,
    )
    session = await create_session(harness)

    receipt = await harness.start_turn(turn_command(session.session_id))
    active = await manager.get(receipt.run_id)
    assert active is not None
    result = await active.task

    assert result.phase is RunPhase.CANCELLED
    assert (await harness.get_run(receipt.run_id)).status is RunStatus.CANCELLED
    assert (await harness.get_turn(receipt.turn_id)).status is TurnStatus.CANCELLED
    events = await harness.replay_events(receipt.run_id)
    assert [(item.event_type, item.terminal) for item in events] == [
        ("turn.started", False),
        ("phase.changed", False),
        ("turn.cancelled", True),
    ]
    assert deferred.released == [receipt.run_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "error_code", "retryable"),
    [
        ("auth_account_changed", ErrorCode.AUTH_REQUIRED, False),
        ("catalog_unreachable", ErrorCode.PROVIDER_UNREACHABLE, True),
        ("model_unavailable", ErrorCode.PROVIDER_UNSUPPORTED, False),
    ],
)
async def test_typed_model_binding_preparation_failure_preserves_actionable_wire_error(
    reason: str,
    error_code: ErrorCode,
    retryable: bool,
) -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    uow = InMemoryUnitOfWorkFactory()
    manager = TurnManager()
    failure = RunPreparationFailure(
        reason,
        "Codex model selection could not be verified; refresh the catalog and try again.",
        retryable=retryable,
        error_code=error_code,
        failure_category="model",
        details={"runBindingCode": reason},
    )
    deferred = DeferredComponents(StopPlanner(), uow, failure=failure)
    harness = HarnessService(
        unit_of_work=uow,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=deferred,
        async_components=deferred,
        turn_manager=manager,
    )
    session = await create_session(harness)

    receipt = await harness.start_turn(turn_command(session.session_id))
    active = await manager.get(receipt.run_id)
    assert active is not None
    with pytest.raises(AgentLoopFailure):
        await active.task

    terminal = stored_event_to_envelope((await harness.replay_events(receipt.run_id))[-1])
    assert isinstance(terminal.payload, TurnFailedPayload)
    assert terminal.payload.error.code is error_code
    assert terminal.payload.error.retryable is retryable
    assert terminal.payload.error.details["runBindingCode"] == reason
    assert terminal.payload.error.details["failureCategory"] == "model"


@pytest.mark.asyncio
async def test_turn_start_uses_the_shorter_absolute_deadline() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = ManualClock(now)
    manager = TurnManager()
    harness = HarnessService(
        unit_of_work=InMemoryUnitOfWorkFactory(),
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=Components(StopPlanner()),
        turn_manager=manager,
    )
    session = await create_session(harness)
    base = turn_command(session.session_id)
    command = StartTurnCommand(
        workspace_id=base.workspace_id,
        session_id=base.session_id,
        turn_id=base.turn_id,
        idempotency_key=base.idempotency_key,
        input_blocks=base.input_blocks,
        run_config=base.run_config,
        deadline_at=now + timedelta(seconds=30),
    )
    receipt = await harness.start_turn(command)
    active = await manager.get(receipt.run_id)
    assert active is not None
    await active.task
    run = await harness.get_run(receipt.run_id)
    assert run.deadline_at == now + timedelta(seconds=30)


@pytest.mark.asyncio
async def test_retry_creates_new_attempt_on_same_turn_with_durable_idempotency() -> None:
    harness, manager = service(StopPlanner())
    session = await create_session(harness)
    original = await harness.start_turn(turn_command(session.session_id))
    original_active = await manager.get(original.run_id)
    assert original_active is not None
    await original_active.task
    command = RetryTurnCommand(
        workspace_id="ws_main",
        session_id=session.session_id,
        turn_id="turn_client_1",
        source_run_id=original.run_id,
        idempotency_key="retry-idem-1",
    )
    retry = await harness.retry_turn(command)
    assert retry.run_id != original.run_id and retry.turn_id == original.turn_id
    active = await manager.get(retry.run_id)
    assert active is not None
    await active.task
    stored = await harness.get_run(retry.run_id)
    assert stored.attempt == 2 and stored.status is RunStatus.COMPLETED
    assert await harness.retry_turn(command) == replace(retry, duplicate=True)
    events = await harness.replay_events(retry.run_id)
    assert events[0].event_type == "turn.started" and events[-1].event_type == "turn.completed"


@pytest.mark.asyncio
async def test_session_event_replay_uses_independent_run_cursors() -> None:
    harness, manager = service(StopPlanner())
    session = await create_session(harness)
    original = await harness.start_turn(turn_command(session.session_id))
    original_active = await manager.get(original.run_id)
    assert original_active is not None
    await original_active.task
    retry = await harness.retry_turn(
        RetryTurnCommand(
            workspace_id="ws_main",
            session_id=session.session_id,
            turn_id=original.turn_id,
            source_run_id=original.run_id,
            idempotency_key="retry-replay",
        )
    )
    retry_active = await manager.get(retry.run_id)
    assert retry_active is not None
    await retry_active.task

    page = await harness.replay_session_events(session.session_id, run_cursors={}, limit=10_000)
    by_run: dict[str, list[int]] = {}
    for event in page.events:
        by_run.setdefault(event.stream_id, []).append(event.sequence)
    assert set(by_run) == {original.run_id, retry.run_id}
    assert by_run[original.run_id][0] == by_run[retry.run_id][0] == 1
    assert by_run[original.run_id] == list(range(1, page.run_cursors[original.run_id] + 1))
    assert by_run[retry.run_id] == list(range(1, page.run_cursors[retry.run_id] + 1))
    assert not page.has_more

    replay = await harness.replay_session_events(
        session.session_id,
        run_cursors=page.run_cursors,
        limit=10_000,
    )
    assert replay.events == () and replay.run_cursors == page.run_cursors
    with pytest.raises(SessionRunConflict, match="outside"):
        await harness.replay_session_events(
            session.session_id,
            run_cursors={"run_foreign": 0},
        )


@pytest.mark.asyncio
async def test_idempotency_key_reuse_with_different_request_fails_closed() -> None:
    harness, manager = service(StopPlanner())
    session = await create_session(harness)
    original = await harness.start_turn(turn_command(session.session_id))
    active = await manager.get(original.run_id)
    assert active is not None
    await active.task

    with pytest.raises(IdempotencyKeyConflict):
        await harness.start_turn(turn_command(session.session_id, config={"model": "different"}))


@pytest.mark.asyncio
async def test_cancel_turn_reaches_waiting_planner_and_persists_terminal_state() -> None:
    harness, manager = service(WaitingPlanner())
    session = await create_session(harness)
    receipt = await harness.start_turn(turn_command(session.session_id))
    active = await manager.get(receipt.run_id)
    assert active is not None

    assert await harness.cancel_turn(receipt.run_id)
    result = await active.task
    assert result.phase is RunPhase.CANCELLED
    assert (await harness.get_run_state(receipt.run_id)).phase is RunPhase.CANCELLED
    assert (await harness.get_turn("turn_client_1")).status is TurnStatus.CANCELLED
    assert (await harness.get_run(receipt.run_id)).termination_reason is TerminationReason.CANCELLED_BY_USER
    events = await harness.replay_events(receipt.run_id)
    assert events[-1].event_type == "turn.cancelled"
    assert parse_event(stored_event_to_envelope(events[-1]).to_wire()).type.value == "turn.cancelled"


@pytest.mark.asyncio
async def test_caller_cancel_after_start_commit_keeps_run_recoverable_and_wakes_loop() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    uow = InMemoryUnitOfWorkFactory()
    manager = TurnManager()
    sink = BlockingEventSink()
    harness = HarnessService(
        unit_of_work=uow,
        event_sink=sink,
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=Components(StopPlanner()),
        turn_manager=manager,
    )
    session = await create_session(harness)
    command = turn_command(session.session_id)
    starting = asyncio.create_task(harness.start_turn(command))
    await sink.entered.wait()
    starting.cancel()
    sink.release.set()

    with pytest.raises(asyncio.CancelledError):
        await starting
    receipt = await harness.start_turn(command)
    active = await manager.get(receipt.run_id)
    if active is not None:
        await active.task
    assert (await harness.get_run(receipt.run_id)).status is RunStatus.COMPLETED
    assert (await harness.replay_events(receipt.run_id))[-1].event_type == "turn.completed"


@pytest.mark.asyncio
async def test_shutdown_hard_grace_persists_interrupted_terminal_fact() -> None:
    planner = StubbornPlanner()
    harness, manager = service(planner)
    session = await create_session(harness)
    receipt = await harness.start_turn(turn_command(session.session_id))
    active = await manager.get(receipt.run_id)
    assert active is not None
    await planner.entered.wait()

    await harness.shutdown(grace_seconds=0)
    result = await active.task

    assert result.phase is RunPhase.INTERRUPTED
    assert (await harness.get_run(receipt.run_id)).status is RunStatus.INTERRUPTED
    assert (await harness.get_run(receipt.run_id)).termination_reason is TerminationReason.RUNTIME_INTERRUPTED
    assert (await harness.get_turn("turn_client_1")).status is TurnStatus.INTERRUPTED
    terminal = (await harness.replay_events(receipt.run_id))[-1]
    assert terminal.event_type == "turn.interrupted"
    assert stored_event_to_envelope(terminal).type.value == "turn.interrupted"


@pytest.mark.asyncio
async def test_manual_clock_deadline_persists_failed_budget_terminal_state() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    planner = WaitingPlanner()
    manager = TurnManager()
    uow = InMemoryUnitOfWorkFactory()
    harness = HarnessService(
        unit_of_work=uow,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=Components(planner),
        turn_manager=manager,
    )
    session = await create_session(harness)
    receipt = await harness.start_turn(turn_command(session.session_id))
    active = await manager.get(receipt.run_id)
    assert active is not None
    await planner.entered.wait()

    clock.advance(timedelta(seconds=60))
    result = await active.task

    assert result.phase is RunPhase.FAILED
    assert (await harness.get_run(receipt.run_id)).status is RunStatus.FAILED
    assert (await harness.get_run(receipt.run_id)).termination_reason is TerminationReason.BUDGET_EXHAUSTED
    assert (await harness.get_turn("turn_client_1")).status is TurnStatus.FAILED
    terminal = (await harness.replay_events(receipt.run_id))[-1]
    assert terminal.event_type == "turn.failed"
    payload = stored_event_to_envelope(terminal).payload
    assert isinstance(payload, TurnFailedPayload)
    assert payload.error.code.value == "request.deadline_exceeded"


@pytest.mark.asyncio
async def test_persistent_session_lease_blocks_second_service_and_releases_at_terminal() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    uow = InMemoryUnitOfWorkFactory()
    first_manager = TurnManager()
    second_manager = TurnManager()
    first_planner = WaitingPlanner()
    sink = BlockingEventSink()
    first = HarnessService(
        unit_of_work=uow,
        event_sink=sink,
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=Components(first_planner),
        turn_manager=first_manager,
    )
    second = HarnessService(
        unit_of_work=uow,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(start=100),
        components=Components(WaitingPlanner()),
        turn_manager=second_manager,
    )
    session = await create_session(first)
    first_command = turn_command(session.session_id)
    starting = asyncio.create_task(first.start_turn(first_command))
    await sink.entered.wait()
    conflicting = StartTurnCommand(
        workspace_id="ws_main",
        session_id=session.session_id,
        turn_id="turn_2",
        idempotency_key="turn-idem-2",
        input_blocks=({"type": "text", "text": "second"},),
        run_config={"model": "fake"},
    )

    with pytest.raises(SessionRunConflict):
        await second.start_turn(conflicting)

    sink.release.set()
    receipt = await starting
    active = await first_manager.get(receipt.run_id)
    assert active is not None
    await first_planner.entered.wait()
    assert await first.cancel_turn(receipt.run_id)
    await active.task
    assert await uow.get_entity("active_root_runs", session.session_id) is None

    third_manager = TurnManager()
    third = HarnessService(
        unit_of_work=uow,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(start=200),
        components=Components(StopPlanner()),
        turn_manager=third_manager,
    )
    third_receipt = await third.start_turn(conflicting)
    third_active = await third_manager.get(third_receipt.run_id)
    assert third_active is not None
    assert (await third_active.task).phase is RunPhase.COMPLETED


class _VaultMemoryPlanner(StopPlanner):
    def __init__(self) -> None:
        self.memories: list[ContextFragment] = []

    def add_memory_context(self, fragments: Sequence[ContextFragment]) -> None:
        self.memories.extend(fragments)


class _VaultMemoryComponents(Components):
    def __init__(self, planner: _VaultMemoryPlanner) -> None:
        super().__init__(planner)


class _RunContextProvider:
    def __init__(self, *, failure: RunPreparationFailure | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[RunPreparationRequest, RunPhase, CancellationToken]] = []
        self.fragment = ContextFragment(
            "vault:ws_main:.offeragent/memory/MEMORY.md",
            ContextLayer.MEMORY,
            "Harness loaded Vault MEMORY.md",
            Sensitivity.WORKSPACE,
            source_refs=("vault:ws_main:.offeragent/memory/MEMORY.md",),
            content_hash="sha256:" + "b" * 64,
        )

    async def context_fragments(
        self,
        request: RunPreparationRequest,
        phase: RunPhase,
        cancellation: CancellationToken,
    ) -> tuple[ContextFragment, ...]:
        cancellation.checkpoint()
        self.calls.append((request, phase, cancellation))
        if phase is RunPhase.SELECTING_MEMORY and self.failure is not None:
            raise self.failure
        return (self.fragment,) if phase is RunPhase.SELECTING_MEMORY else ()


def _vault_memory_enabled_command(session_id: str) -> StartTurnCommand:
    config = HarnessConfig(memory=MemorySettings(memory_enabled=True))
    base = turn_command(session_id)
    return replace(
        base,
        effective_config=config,
        effective_config_fingerprint=canonical_json_sha256(config.model_dump(mode="json")),
    )


@pytest.mark.asyncio
async def test_vault_memory_preparation_enters_agent_context_without_touching_tool_kernel() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    manager = TurnManager()
    factory = InMemoryUnitOfWorkFactory()
    planner = _VaultMemoryPlanner()
    components = _VaultMemoryComponents(planner)
    provider = _RunContextProvider()
    harness = HarnessService(
        unit_of_work=factory,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=components,
        turn_manager=manager,
        run_context_provider=provider,
    )
    session = await create_session(harness)

    receipt = await harness.start_turn(_vault_memory_enabled_command(session.session_id))
    active = await manager.get(receipt.run_id)
    assert active is not None
    assert (await active.task).phase is RunPhase.COMPLETED

    assert planner.memories == [provider.fragment]
    assert [phase for _, phase, _ in provider.calls] == [RunPhase.LOADING_CONTEXT, RunPhase.SELECTING_MEMORY]
    request = provider.calls[0][0]
    assert request.workspace_id == "ws_main" and request.session_id == session.session_id
    assert request.run_id == receipt.run_id
    assert len(components.tool_kernel_budgets) == 1


@pytest.mark.asyncio
async def test_vault_memory_preparation_failure_is_a_typed_durable_terminal_failure() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    manager = TurnManager()
    factory = InMemoryUnitOfWorkFactory()
    provider = _RunContextProvider(
        failure=RunPreparationFailure("vault_memory_test_failure", "Vault Memory test failed closed", retryable=False)
    )
    harness = HarnessService(
        unit_of_work=factory,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=_VaultMemoryComponents(_VaultMemoryPlanner()),
        turn_manager=manager,
        run_context_provider=provider,
    )
    session = await create_session(harness)

    receipt = await harness.start_turn(_vault_memory_enabled_command(session.session_id))
    active = await manager.get(receipt.run_id)
    assert active is not None
    with pytest.raises(AgentLoopFailure):
        await active.task

    state = await harness.get_run_state(receipt.run_id)
    run = await harness.get_run(receipt.run_id)
    events = await harness.replay_events(receipt.run_id)
    assert state.phase is RunPhase.FAILED and run.status is RunStatus.FAILED
    assert events[-1].event_type == "turn.failed" and events[-1].terminal
    assert "vault_memory_test_failure" in repr(events[-1].payload)
    assert [phase for _, phase, _ in provider.calls] == [RunPhase.LOADING_CONTEXT, RunPhase.SELECTING_MEMORY]
