from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from offeragent_harness.agent import RunBudget
from offeragent_harness.agent.composer import CompositionEvent
from offeragent_harness.agent.loop import ToolExecution
from offeragent_harness.agent.planner import Planner, PlanningStep
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.ports import CancellationToken
from offeragent_harness.runtime import TurnManager
from offeragent_harness.runtime.harness_service import (
    CreateSessionCommand,
    HarnessService,
    IdempotencyKeyConflict,
    RunComponents,
    SessionReceipt,
    StartTurnCommand,
)
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualClock,
    RecordingEventSink,
)
from offeragent_harness.tools import ToolCall


class StopPlanner:
    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        return PlanningStep((), False, "done")


class WaitingPlanner:
    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        await cancellation.wait()
        cancellation.checkpoint()
        raise AssertionError("checkpoint must raise")


class Composer:
    def stream(
        self,
        state: RunState,
        *,
        partial: bool,
        cancellation: CancellationToken,
    ) -> AsyncIterator[CompositionEvent]:
        async def generate() -> AsyncIterator[CompositionEvent]:
            cancellation.checkpoint()
            yield CompositionEvent(text_delta="answer")

        return generate()


class NoToolKernel:
    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
    ) -> tuple[ToolExecution, ...]:
        raise AssertionError("no tool call expected")


class Components:
    def __init__(self, planner: Planner) -> None:
        self.planner = planner

    def build(self, command: StartTurnCommand, state: RunState) -> RunComponents:
        return RunComponents(
            planner=self.planner,
            composer=Composer(),
            tool_kernel=NoToolKernel(),
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
                max_subagent_depth=1,
            ),
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


async def create_session(harness: HarnessService) -> SessionReceipt:
    return await harness.create_session(CreateSessionCommand("ws", "profile", "Session", "create-session"))


def turn_command(session_id: str, *, config: dict[str, object] | None = None) -> StartTurnCommand:
    return StartTurnCommand(
        workspace_id="ws",
        session_id=session_id,
        turn_id="turn-client-1",
        idempotency_key="turn-idem-1",
        input_blocks=({"type": "text", "text": "hello"},),
        run_config=config or {"model": "fake"},
    )


@pytest.mark.asyncio
async def test_session_and_turn_idempotency_share_one_authoritative_run() -> None:
    harness, manager = service(StopPlanner())
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

    duplicate = await harness.start_turn(command)
    assert duplicate == receipt
    events = await harness.replay_events(receipt.run_id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[0].event_type == "turn.started"
    assert events[-1].event_type == "turn.completed"
    assert len([event for event in events if event.terminal]) == 1


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
    assert (await harness.replay_events(receipt.run_id))[-1].event_type == "turn.cancelled"
