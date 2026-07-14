from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from offeragent_harness.ports import EventSink, StoredEvent


@dataclass(frozen=True, slots=True)
class SinkDeliveryError:
    event_ids: tuple[str, ...]
    error_type: str
    message: str


class BufferedEventSink:
    """Bounded live-delivery queue; EventStore replay remains authoritative.

    A full queue drops only the live notification, never the already committed
    event. This prevents a stalled UI from applying unbounded memory pressure or
    blocking the Agent Loop.
    """

    def __init__(
        self,
        downstream: EventSink,
        *,
        capacity: int = 256,
        queue_length_observer: Callable[[int], None] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("event sink capacity must be positive")
        self._downstream = downstream
        self._queue: asyncio.Queue[tuple[StoredEvent, ...]] = asyncio.Queue(maxsize=capacity)
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self._queue_length_observer = queue_length_observer
        self.delivery_errors: list[SinkDeliveryError] = []
        self.dropped_event_ids: list[str] = []
        self._report_queue_length()

    async def publish(self, events: Sequence[StoredEvent]) -> None:
        if self._closed:
            raise RuntimeError("event sink is closed")
        batch = tuple(events)
        if not batch:
            return
        self._ensure_worker()
        try:
            self._queue.put_nowait(batch)
        except asyncio.QueueFull:
            self.dropped_event_ids.extend(event.event_id for event in batch)
        finally:
            self._report_queue_length()

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._deliver(), name="offeragent-event-delivery")

    async def _deliver(self) -> None:
        while True:
            batch = await self._queue.get()
            try:
                await self._downstream.publish(batch)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.delivery_errors.append(
                    SinkDeliveryError(
                        event_ids=tuple(event.event_id for event in batch),
                        error_type=type(error).__name__,
                        message=str(error),
                    )
                )
            finally:
                self._queue.task_done()
                self._report_queue_length()

    async def flush(self, *, timeout_seconds: float = 5.0) -> bool:
        if timeout_seconds <= 0:
            raise ValueError("flush timeout must be positive")
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout_seconds)
        except TimeoutError:
            return False
        return True

    async def close(self, *, timeout_seconds: float = 5.0) -> bool:
        if self._closed:
            return True
        flushed = await self.flush(timeout_seconds=timeout_seconds)
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._report_queue_length()
        return flushed

    def _report_queue_length(self) -> None:
        if self._queue_length_observer is None:
            return
        try:
            self._queue_length_observer(self._queue.qsize())
        except Exception:
            # Live telemetry cannot change durable Event Store semantics.
            return


__all__ = ["BufferedEventSink", "SinkDeliveryError"]
