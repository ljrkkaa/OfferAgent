"""Observable EventSink with explicit delivery gates and ACK-loss faults."""

from __future__ import annotations

from collections.abc import Sequence

from offeragent_harness.ports import StoredEvent

from .barrier import ControlledBarrier
from .cancellation import ManualCancellationToken
from .errors import AcknowledgementLost


class RecordingEventSink:
    def __init__(
        self,
        *,
        acknowledgement_loss_calls: frozenset[int] = frozenset(),
        barriers: dict[int, ControlledBarrier] | None = None,
    ) -> None:
        if any(call < 1 for call in acknowledgement_loss_calls):
            raise ValueError("publish call numbers start at 1")
        self._acknowledgement_loss_calls = acknowledgement_loss_calls
        self._barriers = barriers or {}
        self._by_id: dict[str, StoredEvent] = {}
        self.events: list[StoredEvent] = []
        self.attempts: list[tuple[StoredEvent, ...]] = []
        self.publish_calls = 0

    async def publish(self, events: Sequence[StoredEvent]) -> None:
        self.publish_calls += 1
        call_number = self.publish_calls
        batch = tuple(events)
        self.attempts.append(batch)
        barrier = self._barriers.get(call_number)
        if barrier is not None:
            await barrier.arrive_and_wait(ManualCancellationToken())
        for event in batch:
            existing = self._by_id.get(event.event_id)
            if existing is not None and existing != event:
                raise AssertionError(f"event_id {event.event_id!r} was published with different content")
            if existing is None:
                self._by_id[event.event_id] = event
                self.events.append(event)
        if call_number in self._acknowledgement_loss_calls:
            raise AcknowledgementLost(f"event batch delivered but publish ACK #{call_number} was lost")


__all__ = ["RecordingEventSink"]
