from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone

import pytest

from offeragent_harness.ports import StoredEvent
from offeragent_harness.runtime.backpressure import BufferedEventSink


def event(sequence: int) -> StoredEvent:
    return StoredEvent(
        stream_id="run",
        sequence=sequence,
        event_id=f"event-{sequence}",
        event_type="assistant.delta",
        payload={"text": str(sequence)},
        occurred_at=datetime.now(timezone.utc),
        terminal=False,
        idempotency_key=f"run:{sequence}",
    )


class SlowSink:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.events: list[StoredEvent] = []

    async def publish(self, events: Sequence[StoredEvent]) -> None:
        self.started.set()
        await self.release.wait()
        self.events.extend(events)


@pytest.mark.asyncio
async def test_full_live_queue_drops_notification_without_blocking_producer() -> None:
    downstream = SlowSink()
    queue_lengths: list[int] = []
    sink = BufferedEventSink(downstream, capacity=1, queue_length_observer=queue_lengths.append)

    await sink.publish((event(1),))
    await downstream.started.wait()
    await sink.publish((event(2),))
    await sink.publish((event(3),))
    assert sink.dropped_event_ids == ["event-3"]

    downstream.release.set()
    assert await sink.flush()
    assert [item.event_id for item in downstream.events] == ["event-1", "event-2"]
    assert await sink.close()
    assert queue_lengths[0] == 0
    assert max(queue_lengths) == 1
    assert queue_lengths[-1] == 0


class FailingSink:
    async def publish(self, events: Sequence[StoredEvent]) -> None:
        raise ConnectionError("client disconnected")


@pytest.mark.asyncio
async def test_delivery_failure_is_diagnostic_not_producer_failure() -> None:
    sink = BufferedEventSink(FailingSink())
    await sink.publish((event(1),))
    assert await sink.flush()
    assert sink.delivery_errors[0].event_ids == ("event-1",)
    assert await sink.close()


@pytest.mark.asyncio
async def test_queue_metric_failure_cannot_break_durable_live_delivery() -> None:
    downstream = SlowSink()

    def unavailable_metric(_length: int) -> None:
        raise RuntimeError("metrics unavailable")

    sink = BufferedEventSink(downstream, queue_length_observer=unavailable_metric)
    await sink.publish((event(1),))
    await downstream.started.wait()
    downstream.release.set()

    assert await sink.close()
    assert [item.event_id for item in downstream.events] == ["event-1"]
