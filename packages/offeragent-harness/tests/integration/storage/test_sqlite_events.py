from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteEventStore
from offeragent_harness.ports import (
    EventIdConflict,
    EventIdempotencyConflict,
    NewEvent,
    SequenceConflict,
    TerminalEventConflict,
)

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def event(number: int, *, terminal: bool = False, payload: dict[str, object] | None = None) -> NewEvent:
    return NewEvent(
        event_id=f"evt_{number}",
        event_type="turn.completed" if terminal else "phase.changed",
        payload=payload or {"number": number},
        occurred_at=NOW,
        terminal=terminal,
        idempotency_key=f"event-key-{number}",
    )


@pytest.mark.asyncio
async def test_events_replay_after_database_reopen_with_cursor_and_limit(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    first_store = SqliteEventStore(database_path)
    committed = await first_store.append("run_1", 0, (event(1), event(2), event(3)))

    reopened = SqliteEventStore(database_path)
    assert await reopened.read("run_1") == committed
    assert await reopened.read("run_1", after_sequence=1, limit=1) == (committed[1],)
    assert await reopened.read("run_1", after_sequence=3) == ()
    assert await reopened.read("run_1", limit=0) == ()
    assert await reopened.latest_sequence("run_1") == 3


@pytest.mark.asyncio
async def test_expected_sequence_is_database_cas_across_independent_connections(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    first_store = SqliteEventStore(database_path)
    second_store = SqliteEventStore(database_path)

    results = await asyncio.gather(
        first_store.append("run_1", 0, (event(1),)),
        second_store.append("run_1", 0, (event(2),)),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, tuple)]
    conflicts = [result for result in results if isinstance(result, SequenceConflict)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert len(await first_store.read("run_1")) == 1


@pytest.mark.asyncio
async def test_lost_ack_replays_same_event_after_reopen_without_second_append(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    candidate = event(1)
    committed = await SqliteEventStore(database_path).append("run_1", 0, (candidate,))

    # Simulate process loss after COMMIT but before the caller receives/records the ACK.
    replayed = await SqliteEventStore(database_path).append("run_1", 0, (candidate,))

    assert replayed == committed
    assert await SqliteEventStore(database_path).latest_sequence("run_1") == 1


@pytest.mark.asyncio
async def test_event_id_and_idempotency_are_content_bound_and_batch_replay_is_all_or_nothing(
    tmp_path: Path,
) -> None:
    store = SqliteEventStore(tmp_path / "state.sqlite")
    original = event(1)
    await store.append("run_1", 0, (original,))

    changed = NewEvent(
        event_id=original.event_id,
        event_type=original.event_type,
        payload={"changed": True},
        occurred_at=original.occurred_at,
        terminal=False,
        idempotency_key=original.idempotency_key,
    )
    with pytest.raises(EventIdempotencyConflict):
        await store.append("run_1", 0, (changed,))

    with pytest.raises(EventIdConflict):
        await store.append(
            "run_2",
            0,
            (
                NewEvent(
                    event_id=original.event_id,
                    event_type="phase.changed",
                    payload={},
                    occurred_at=NOW,
                    terminal=False,
                    idempotency_key="different-key",
                ),
            ),
        )

    with pytest.raises(EventIdempotencyConflict, match="partial batch replay"):
        await store.append("run_1", 1, (original, event(2)))
    assert await store.latest_sequence("run_1") == 1


@pytest.mark.asyncio
async def test_terminal_event_is_exactly_once_and_closes_stream_across_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    store = SqliteEventStore(database_path)
    await store.append("run_1", 0, (event(1),))
    terminal = event(2, terminal=True)
    committed = await store.append("run_1", 1, (terminal,))

    reopened = SqliteEventStore(database_path)
    assert await reopened.append("run_1", 1, (terminal,)) == committed
    assert await reopened.terminal_event("run_1") == committed[0]
    with pytest.raises(TerminalEventConflict):
        await reopened.append("run_1", 2, (event(3),))
    with pytest.raises(TerminalEventConflict):
        await reopened.append("run_1", 2, (event(4, terminal=True),))
    assert len(await reopened.read("run_1")) == 2


@pytest.mark.asyncio
async def test_terminal_must_be_last_and_validation_failure_is_atomic(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "state.sqlite")
    with pytest.raises(TerminalEventConflict):
        await store.append("run_1", 0, (event(1, terminal=True), event(2)))
    assert await store.read("run_1") == ()
