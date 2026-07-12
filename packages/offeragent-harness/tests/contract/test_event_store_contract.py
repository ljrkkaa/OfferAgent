from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from offeragent_harness.ports import (
    EventIdempotencyConflict,
    NewEvent,
    SequenceConflict,
    TerminalEventConflict,
)
from offeragent_harness.testing import InMemoryEventStore

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
async def test_expected_sequence_is_a_real_compare_and_swap_under_concurrency() -> None:
    store = InMemoryEventStore()
    first = asyncio.create_task(store.append("run_1", 0, (event(1),)))
    second = asyncio.create_task(store.append("run_1", 0, (event(2),)))
    results = await asyncio.gather(first, second, return_exceptions=True)

    successes = [result for result in results if isinstance(result, tuple)]
    conflicts = [result for result in results if isinstance(result, SequenceConflict)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    persisted = await store.read("run_1")
    assert len(persisted) == 1
    assert persisted[0].sequence == 1


@pytest.mark.asyncio
async def test_lost_append_ack_can_replay_the_same_idempotent_event_with_stale_cas() -> None:
    store = InMemoryEventStore()
    candidate = event(1)
    committed = await store.append("run_1", 0, (candidate,))
    replayed = await store.append("run_1", 0, (candidate,))
    assert replayed == committed
    assert await store.latest_sequence("run_1") == 1


@pytest.mark.asyncio
async def test_idempotency_key_is_content_bound() -> None:
    store = InMemoryEventStore()
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


@pytest.mark.asyncio
async def test_terminal_event_is_exactly_once_and_closes_the_stream() -> None:
    store = InMemoryEventStore()
    await store.append("run_1", 0, (event(1),))
    terminal = event(2, terminal=True)
    committed = await store.append("run_1", 1, (terminal,))

    assert await store.append("run_1", 1, (terminal,)) == committed
    assert await store.terminal_event("run_1") == committed[0]
    with pytest.raises(TerminalEventConflict):
        await store.append("run_1", 2, (event(3, terminal=True),))
    with pytest.raises(TerminalEventConflict):
        await store.append("run_1", 2, (event(4),))
    assert len(await store.read("run_1")) == 2


@pytest.mark.asyncio
async def test_terminal_must_be_last_and_batch_is_atomic() -> None:
    store = InMemoryEventStore()
    with pytest.raises(TerminalEventConflict):
        await store.append("run_1", 0, (event(1, terminal=True), event(2)))
    assert await store.read("run_1") == ()
