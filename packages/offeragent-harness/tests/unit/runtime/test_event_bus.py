from __future__ import annotations

from dataclasses import replace

import pytest

from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.ports import TerminalEventConflict
from offeragent_harness.runtime.event_bus import UowRunRecorder
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualClock,
    RecordingEventSink,
)


def state() -> RunState:
    return RunState("ws", "session", "turn", "run", AgentLineage.root("run"))


@pytest.mark.asyncio
async def test_state_and_event_commit_atomically_with_monotonic_sequences() -> None:
    uow = InMemoryUnitOfWorkFactory()
    sink = RecordingEventSink()
    recorder = UowRunRecorder(
        unit_of_work=uow,
        event_sink=sink,
        clock=ManualClock(),
        ids=DeterministicIdGenerator(),
        run_id="run",
        expected_entity_revision=0,
        expected_event_sequence=0,
    )
    current = state().transition(RunPhase.LOADING_CONTEXT)
    await recorder.commit(current, event_type="phase.changed", payload={"phase": current.phase.value})

    assert recorder.entity_revision == 1
    assert recorder.event_sequence == 1
    assert (await uow.get_entity("run_states", "run")) == current
    assert [event.sequence for event in sink.events] == [1]


@pytest.mark.asyncio
async def test_sink_ack_loss_does_not_rollback_authoritative_event() -> None:
    uow = InMemoryUnitOfWorkFactory()
    sink = RecordingEventSink(acknowledgement_loss_calls=frozenset({1}))
    recorder = UowRunRecorder(
        unit_of_work=uow,
        event_sink=sink,
        clock=ManualClock(),
        ids=DeterministicIdGenerator(),
        run_id="run",
        expected_entity_revision=0,
        expected_event_sequence=0,
    )
    current = state().transition(RunPhase.LOADING_CONTEXT)
    await recorder.commit(current, event_type="phase.changed", payload={"phase": current.phase.value})

    assert recorder.event_sequence == 1
    assert len(await uow.event_store.read("run")) == 1
    assert recorder.delivery_failures[0].error_type == "AcknowledgementLost"


@pytest.mark.asyncio
async def test_terminal_conflict_rolls_back_entity_update() -> None:
    uow = InMemoryUnitOfWorkFactory()
    recorder = UowRunRecorder(
        unit_of_work=uow,
        event_sink=RecordingEventSink(),
        clock=ManualClock(),
        ids=DeterministicIdGenerator(),
        run_id="run",
        expected_entity_revision=0,
        expected_event_sequence=0,
    )
    current = replace(state(), phase=RunPhase.FAILED, revision=1)
    await recorder.commit(current, event_type="turn.failed", payload={}, terminal=True)
    committed_revision = recorder.entity_revision

    changed = replace(current, assistant_text="must not persist", revision=2)
    with pytest.raises(TerminalEventConflict, match="already terminated"):
        await recorder.commit(changed, event_type="turn.failed", payload={}, terminal=True)

    assert recorder.entity_revision == committed_revision
    assert (await uow.get_entity("run_states", "run")) == current
