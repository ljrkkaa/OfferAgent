from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from offeragent_harness.agent.state import RunState
from offeragent_harness.ports import ArtifactMetadata, ArtifactState, NewEvent, Sensitivity
from offeragent_harness.runtime.conversation_controls import CompactionExecution, ConversationControlService
from offeragent_harness.runtime.turn_manager import TurnManager
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
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
    RecordingEventSink,
)


class Runner:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.calls = 0

    async def compact(self, **kwargs: Any) -> CompactionExecution:
        self.calls += 1
        metadata = ArtifactMetadata(
            artifact_id="art_summary",
            workspace_id="ws_test",
            owner_run_id="run_1",
            mime_type="application/json",
            byte_length=2,
            sha256="sha256:" + "a" * 64,
            sensitivity=Sensitivity.WORKSPACE,
            state=ArtifactState.COMPLETE,
            created_at=self.clock.utcnow(),
        )
        return CompactionExecution(metadata, 1, 1, 1, "fake-model")


async def _seed_graph(uow: InMemoryUnitOfWorkFactory, now: datetime, *, terminal: bool = True) -> None:
    session = Session(
        "ses_1",
        "ws_test",
        "profile_1",
        "Session",
        SessionStatus.ACTIVE,
        now,
        now,
        1,
    )
    turn = Turn("turn_1", "ses_1", 1, TurnStatus.COMPLETED, ({"type": "text", "text": "hello"},), now, now)
    run = Run(
        "run_1",
        "ses_1",
        "turn_1",
        "ws_test",
        AgentLineage.root("run_1"),
        RunKind.ROOT,
        RunStatus.COMPLETED if terminal else RunStatus.PLANNING,
        1,
        1,
        {"model": "fake"},
        now,
        now,
        None,
        TerminationReason.COMPLETED if terminal else None,
    )
    async with uow.begin() as work:
        await work.entities.put("sessions", session.session_id, session, expected_revision=0)
        await work.entities.put("turns", turn.turn_id, turn, expected_revision=0)
        await work.entities.put("runs", run.run_id, run, expected_revision=0)
        await work.entities.put(
            "run_states",
            run.run_id,
            RunState("ws_test", "ses_1", "turn_1", "run_1", AgentLineage.root("run_1")),
            expected_revision=0,
        )
        await work.events.append(
            "run_1",
            0,
            (
                NewEvent(
                    "evt_seed",
                    "turn.completed" if terminal else "phase.changed",
                    {"seed": True},
                    now,
                    terminal,
                    "seed-event",
                ),
            ),
        )
        await work.commit()


@pytest.mark.asyncio
async def test_compaction_persists_boundary_event_and_replays_receipt() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    uow = InMemoryUnitOfWorkFactory()
    await _seed_graph(uow, clock.utcnow())
    runner = Runner(clock)
    sink = RecordingEventSink()
    service = ConversationControlService(
        workspace_id="ws_test",
        unit_of_work=uow,
        event_sink=sink,
        clock=clock,
        ids=DeterministicIdGenerator(),
        turn_manager=TurnManager(),
        compaction_runner=runner,
    )
    result = await service.compact(
        session_id="ses_1",
        through_turn_id="turn_1",
        force=True,
        cancellation=ManualCancellationToken(),
    )
    replay = await service.compact(
        session_id="ses_1",
        through_turn_id="turn_1",
        force=True,
        cancellation=ManualCancellationToken(),
    )
    assert result == replay and runner.calls == 1
    assert result.boundary_artifact is not None and result.boundary_artifact.artifact_id == "art_summary"
    assert sink.events[-1].event_type == "context.compacted"


@pytest.mark.asyncio
async def test_steer_is_durable_and_queued_without_interrupting_active_run() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    uow = InMemoryUnitOfWorkFactory()
    await _seed_graph(uow, clock.utcnow(), terminal=False)
    manager = TurnManager()
    release = asyncio.Event()

    async def run(cancellation: Any) -> None:
        await release.wait()

    active = await manager.start(session_id="ses_1", run_id="run_1", factory=run)
    service = ConversationControlService(
        workspace_id="ws_test",
        unit_of_work=uow,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        turn_manager=manager,
        compaction_runner=Runner(clock),
    )
    result = await service.steer(
        run_id="run_1",
        message_id="msg_1",
        input_blocks=({"type": "text", "text": "change direction"},),
        mode="steer",
        cancellation=ManualCancellationToken(),
    )
    queued = await active.controls.drain()
    assert result.accepted and queued[0].message_id == "msg_1"
    assert not active.cancellation.cancelled
    replay = await service.steer(
        run_id="run_1",
        message_id="msg_1",
        input_blocks=({"type": "text", "text": "change direction"},),
        mode="steer",
        cancellation=ManualCancellationToken(),
    )
    assert replay == result
    release.set()
    await active.task
