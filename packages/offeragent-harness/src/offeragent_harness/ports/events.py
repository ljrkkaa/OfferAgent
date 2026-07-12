"""Append-only event store and lossy-delivery-safe event sink contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json


class EventStoreError(RuntimeError):
    pass


class SequenceConflict(EventStoreError):
    def __init__(self, stream_id: str, expected: int, actual: int) -> None:
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"stream {stream_id!r} expected sequence {expected}, actual {actual}")


class TerminalEventConflict(EventStoreError):
    pass


class EventIdConflict(EventStoreError):
    pass


class EventIdempotencyConflict(EventStoreError):
    pass


@dataclass(frozen=True)
class NewEvent:
    event_id: str
    event_type: str
    payload: Mapping[str, Any]
    occurred_at: datetime
    terminal: bool
    idempotency_key: str

    def __post_init__(self) -> None:
        if not self.event_id or not self.event_type or not self.idempotency_key:
            raise ValueError("event identity fields must not be empty")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("event timestamps must be timezone-aware")
        payload = freeze_json(self.payload)
        if not isinstance(payload, FrozenJsonObject):
            raise TypeError("event payload must be a JSON object")
        object.__setattr__(self, "payload", payload)


@dataclass(frozen=True)
class StoredEvent:
    stream_id: str
    sequence: int
    event_id: str
    event_type: str
    payload: Mapping[str, Any]
    occurred_at: datetime
    terminal: bool
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("stored event sequence starts at 1")
        payload = freeze_json(self.payload)
        if not isinstance(payload, FrozenJsonObject):
            raise TypeError("event payload must be a JSON object")
        object.__setattr__(self, "payload", payload)

    @classmethod
    def from_new(cls, stream_id: str, sequence: int, event: NewEvent) -> StoredEvent:
        return cls(
            stream_id=stream_id,
            sequence=sequence,
            event_id=event.event_id,
            event_type=event.event_type,
            payload=event.payload,
            occurred_at=event.occurred_at,
            terminal=event.terminal,
            idempotency_key=event.idempotency_key,
        )


@runtime_checkable
class EventStore(Protocol):
    async def append(
        self,
        stream_id: str,
        expected_sequence: int,
        events: Sequence[NewEvent],
    ) -> tuple[StoredEvent, ...]: ...

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]: ...

    async def latest_sequence(self, stream_id: str) -> int: ...

    async def terminal_event(self, stream_id: str) -> StoredEvent | None: ...


@runtime_checkable
class EventSink(Protocol):
    async def publish(self, events: Sequence[StoredEvent]) -> None: ...


__all__ = [
    "EventIdConflict",
    "EventIdempotencyConflict",
    "EventSink",
    "EventStore",
    "EventStoreError",
    "NewEvent",
    "SequenceConflict",
    "StoredEvent",
    "TerminalEventConflict",
]
