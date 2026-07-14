from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.ports import EntityStore, EventStore, InvocationJournal, TerminalEventConflict, UnitOfWork
from offeragent_harness.runtime.event_bus import UowRunRecorder
from offeragent_harness.sessions import AgentLineage, Turn, TurnStatus
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualClock,
    RecordingEventSink,
)


def state() -> RunState:
    return RunState("ws_main", "ses_1", "turn_1", "run_1", AgentLineage.root("run_1"))


def phase_payload(previous: RunPhase, current: RunPhase) -> dict[str, object]:
    return {"previousPhase": previous.value, "phase": current.value, "reason": None}


def failed_payload() -> dict[str, object]:
    return {
        "error": {
            "code": "internal.error",
            "retryable": False,
            "cancelled": False,
            "userVisibleMessage": "failed",
            "details": {"errorType": "RuntimeError", "failureCategory": "runtime"},
            "retryAfterMs": None,
            "traceId": None,
        },
        "usage": {
            "inputTokens": 0,
            "outputTokens": 0,
            "cachedInputTokens": 0,
            "reasoningTokens": 0,
            "modelCalls": 0,
            "toolCalls": 0,
            "costMicros": 0,
            "wallTimeMs": 0,
        },
        "partialContent": [],
    }


class _CommitAckLossUnitOfWork:
    def __init__(self, inner: UnitOfWork, owner: _CommitAckLossFactory) -> None:
        self._inner = inner
        self._owner = owner

    @property
    def entities(self) -> EntityStore:
        return self._inner.entities

    @property
    def events(self) -> EventStore:
        return self._inner.events

    @property
    def journal(self) -> InvocationJournal:
        return self._inner.journal

    async def __aenter__(self) -> _CommitAckLossUnitOfWork:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self._inner.__aexit__(exc_type, exc, traceback)

    async def commit(self) -> None:
        await self._inner.commit()
        if self._owner.commit_error is not None:
            error = self._owner.commit_error
            self._owner.commit_error = None
            raise error

    async def rollback(self) -> None:
        await self._inner.rollback()


class _CommitAckLossFactory:
    def __init__(self, error: BaseException | None = None) -> None:
        self.inner = InMemoryUnitOfWorkFactory()
        self.commit_error: BaseException | None = error or ConnectionError("commit acknowledgement lost")

    def begin(self) -> UnitOfWork:
        return _CommitAckLossUnitOfWork(self.inner.begin(), self)


@pytest.mark.asyncio
async def test_state_and_event_commit_atomically_with_monotonic_sequences() -> None:
    uow = InMemoryUnitOfWorkFactory()
    sink = RecordingEventSink()
    recorder = UowRunRecorder(
        unit_of_work=uow,
        event_sink=sink,
        clock=ManualClock(),
        ids=DeterministicIdGenerator(),
        run_id="run_1",
        trace_id="trace_1",
        expected_entity_revision=0,
        expected_event_sequence=0,
    )
    current = state().transition(RunPhase.LOADING_CONTEXT)
    await recorder.commit(
        current,
        event_type="phase.changed",
        payload=phase_payload(RunPhase.CREATED, current.phase),
    )

    assert recorder.entity_revision == 1
    assert recorder.event_sequence == 1
    assert (await uow.get_entity("run_states", "run_1")) == current
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
        run_id="run_1",
        trace_id="trace_1",
        expected_entity_revision=0,
        expected_event_sequence=0,
    )
    current = state().transition(RunPhase.LOADING_CONTEXT)
    await recorder.commit(
        current,
        event_type="phase.changed",
        payload=phase_payload(RunPhase.CREATED, current.phase),
    )

    assert recorder.event_sequence == 1
    assert len(await uow.event_store.read("run_1")) == 1
    assert recorder.delivery_failures[0].error_type == "AcknowledgementLost"


@pytest.mark.asyncio
async def test_database_commit_ack_loss_resynchronizes_cursor_without_duplicate_event() -> None:
    factory = _CommitAckLossFactory()
    sink = RecordingEventSink()
    recorder = UowRunRecorder(
        unit_of_work=factory,
        event_sink=sink,
        clock=ManualClock(),
        ids=DeterministicIdGenerator(),
        run_id="run_1",
        trace_id="trace_1",
        expected_entity_revision=0,
        expected_event_sequence=0,
    )
    first = state().transition(RunPhase.LOADING_CONTEXT)

    await recorder.commit(
        first,
        event_type="phase.changed",
        payload=phase_payload(RunPhase.CREATED, first.phase),
    )
    second = first.transition(RunPhase.SELECTING_MEMORY)
    await recorder.commit(
        second,
        event_type="phase.changed",
        payload=phase_payload(first.phase, second.phase),
    )

    stored = await factory.inner.event_store.read("run_1")
    assert [event.sequence for event in stored] == [1, 2]
    assert len({event.event_id for event in stored}) == 2
    assert [event.event_id for event in sink.events] == [event.event_id for event in stored]
    assert recorder.entity_revision == 2
    assert recorder.event_sequence == 2


@pytest.mark.asyncio
async def test_commit_time_cancellation_resynchronizes_cursor_before_propagation() -> None:
    factory = _CommitAckLossFactory(asyncio.CancelledError())
    recorder = UowRunRecorder(
        unit_of_work=factory,
        event_sink=RecordingEventSink(),
        clock=ManualClock(),
        ids=DeterministicIdGenerator(),
        run_id="run_1",
        trace_id="trace_1",
        expected_entity_revision=0,
        expected_event_sequence=0,
    )
    first = state().transition(RunPhase.LOADING_CONTEXT)

    with pytest.raises(asyncio.CancelledError):
        await recorder.commit(
            first,
            event_type="phase.changed",
            payload=phase_payload(RunPhase.CREATED, first.phase),
        )

    assert recorder.entity_revision == 1
    assert recorder.event_sequence == 1
    second = first.transition(RunPhase.SELECTING_MEMORY)
    await recorder.commit(
        second,
        event_type="phase.changed",
        payload=phase_payload(first.phase, second.phase),
    )
    assert [event.sequence for event in await factory.inner.event_store.read("run_1")] == [1, 2]


@pytest.mark.asyncio
async def test_terminal_conflict_rolls_back_entity_update() -> None:
    uow = InMemoryUnitOfWorkFactory()
    recorder = UowRunRecorder(
        unit_of_work=uow,
        event_sink=RecordingEventSink(),
        clock=ManualClock(),
        ids=DeterministicIdGenerator(),
        run_id="run_1",
        trace_id="trace_1",
        expected_entity_revision=0,
        expected_event_sequence=0,
    )
    async with uow.begin() as transaction:
        now = ManualClock().utcnow()
        await transaction.entities.put(
            "turns",
            "turn_1",
            Turn(
                turn_id="turn_1",
                session_id="ses_1",
                ordinal=1,
                status=TurnStatus.RUNNING,
                input_blocks=({"type": "text", "text": "test"},),
                created_at=now,
                updated_at=now,
            ),
            expected_revision=0,
        )
        await transaction.entities.put(
            "active_root_runs",
            "ses_1",
            {"schemaVersion": 1, "sessionId": "ses_1", "runId": "run_1"},
            expected_revision=0,
        )
        await transaction.commit()
    current = replace(state(), phase=RunPhase.FAILED, revision=1)
    await recorder.commit(current, event_type="turn.failed", payload=failed_payload(), terminal=True)
    committed_revision = recorder.entity_revision

    changed = replace(current, assistant_text="must not persist", revision=2)
    with pytest.raises(TerminalEventConflict, match="already terminated"):
        await recorder.commit(changed, event_type="turn.failed", payload=failed_payload(), terminal=True)

    assert recorder.entity_revision == committed_revision
    assert (await uow.get_entity("run_states", "run_1")) == current


@pytest.mark.asyncio
async def test_missing_turn_fails_closed_and_rolls_back_terminal_uow() -> None:
    uow = InMemoryUnitOfWorkFactory()
    recorder = UowRunRecorder(
        unit_of_work=uow,
        event_sink=RecordingEventSink(),
        clock=ManualClock(),
        ids=DeterministicIdGenerator(),
        run_id="run_1",
        trace_id="trace_1",
        expected_entity_revision=0,
        expected_event_sequence=0,
    )
    async with uow.begin() as transaction:
        await transaction.entities.put(
            "active_root_runs",
            "ses_1",
            {"schemaVersion": 1, "sessionId": "ses_1", "runId": "run_1"},
            expected_revision=0,
        )
        await transaction.commit()
    terminal = replace(state(), phase=RunPhase.FAILED, revision=1)

    with pytest.raises(RuntimeError, match=r"Turn record .* missing or corrupt"):
        await recorder.commit(terminal, event_type="turn.failed", payload=failed_payload(), terminal=True)

    assert await uow.get_entity("run_states", "run_1") is None
    assert await uow.event_store.read("run_1") == ()
    assert await uow.get_entity("active_root_runs", "ses_1") is not None
