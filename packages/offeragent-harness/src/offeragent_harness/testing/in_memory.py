"""Transactionally correct in-memory EventStore, Journal and Unit of Work."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from offeragent_harness.ports import (
    EntityRevisionConflict,
    EventIdConflict,
    EventIdempotencyConflict,
    InvocationJournalConflict,
    InvocationRecord,
    JournalState,
    NewEvent,
    SequenceConflict,
    StoredEvent,
    TerminalEventConflict,
)
from offeragent_harness.tools import ToolResult


@dataclass
class _EntityRow:
    revision: int
    value: Any


@dataclass
class _State:
    streams: dict[str, list[StoredEvent]] = field(default_factory=dict)
    event_ids: dict[str, StoredEvent] = field(default_factory=dict)
    event_keys: dict[tuple[str, str], StoredEvent] = field(default_factory=dict)
    entities: dict[tuple[str, str], _EntityRow] = field(default_factory=dict)
    journal: dict[tuple[str, str], InvocationRecord] = field(default_factory=dict)


class _Database:
    def __init__(self) -> None:
        self.state = _State()
        self.lock = asyncio.Lock()
        self.generation = 0


def _same_event(stored: StoredEvent, candidate: NewEvent) -> bool:
    return (
        stored.event_id == candidate.event_id
        and stored.event_type == candidate.event_type
        and stored.payload == candidate.payload
        and stored.occurred_at == candidate.occurred_at
        and stored.terminal == candidate.terminal
        and stored.idempotency_key == candidate.idempotency_key
    )


def _append(
    state: _State, stream_id: str, expected_sequence: int, events: Sequence[NewEvent]
) -> tuple[StoredEvent, ...]:
    if expected_sequence < 0:
        raise ValueError("expected sequence cannot be negative")
    if not stream_id:
        raise ValueError("stream_id must not be empty")
    if not events:
        return ()

    keys = [(stream_id, event.idempotency_key) for event in events]
    event_ids = [event.event_id for event in events]
    if len(set(keys)) != len(keys):
        raise EventIdempotencyConflict("a batch cannot repeat an idempotency key")
    if len(set(event_ids)) != len(event_ids):
        raise EventIdConflict("a batch cannot repeat an event_id")

    replays = [state.event_keys.get(key) for key in keys]
    if any(item is not None for item in replays):
        if not all(item is not None for item in replays):
            raise EventIdempotencyConflict("partial batch replay is not allowed")
        stored_replays = tuple(item for item in replays if item is not None)
        for stored, candidate in zip(stored_replays, events, strict=True):
            if not _same_event(stored, candidate):
                raise EventIdempotencyConflict(
                    f"idempotency key {candidate.idempotency_key!r} is bound to a different event"
                )
        return stored_replays

    for candidate in events:
        existing_id = state.event_ids.get(candidate.event_id)
        if existing_id is not None:
            raise EventIdConflict(f"event_id {candidate.event_id!r} already exists in {existing_id.stream_id!r}")

    stream = state.streams.setdefault(stream_id, [])
    actual_sequence = len(stream)
    if actual_sequence != expected_sequence:
        raise SequenceConflict(stream_id, expected_sequence, actual_sequence)
    existing_terminal = next((event for event in stream if event.terminal), None)
    if existing_terminal is not None:
        raise TerminalEventConflict(f"stream {stream_id!r} already terminated at sequence {existing_terminal.sequence}")
    terminal_indexes = [index for index, event in enumerate(events) if event.terminal]
    if len(terminal_indexes) > 1:
        raise TerminalEventConflict("a batch cannot contain multiple terminal events")
    if terminal_indexes and terminal_indexes[0] != len(events) - 1:
        raise TerminalEventConflict("terminal event must be the final event in its batch")

    appended: list[StoredEvent] = []
    for offset, candidate in enumerate(events, start=1):
        stored = StoredEvent.from_new(stream_id, actual_sequence + offset, candidate)
        stream.append(stored)
        state.event_ids[stored.event_id] = stored
        state.event_keys[(stream_id, stored.idempotency_key)] = stored
        appended.append(stored)
    return tuple(appended)


class _EventView:
    def __init__(self, state: _State) -> None:
        self._state = state

    async def append(
        self,
        stream_id: str,
        expected_sequence: int,
        events: Sequence[NewEvent],
    ) -> tuple[StoredEvent, ...]:
        return _append(self._state, stream_id, expected_sequence, events)

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        if after_sequence < 0 or (limit is not None and limit < 0):
            raise ValueError("event cursor and limit cannot be negative")
        events = tuple(event for event in self._state.streams.get(stream_id, ()) if event.sequence > after_sequence)
        return events if limit is None else events[:limit]

    async def latest_sequence(self, stream_id: str) -> int:
        return len(self._state.streams.get(stream_id, ()))

    async def terminal_event(self, stream_id: str) -> StoredEvent | None:
        return next((event for event in self._state.streams.get(stream_id, ()) if event.terminal), None)


class InMemoryEventStore:
    """CAS event store whose retry path is safe after a lost append ACK."""

    def __init__(self, database: _Database | None = None) -> None:
        self._database = database or _Database()

    async def append(
        self,
        stream_id: str,
        expected_sequence: int,
        events: Sequence[NewEvent],
    ) -> tuple[StoredEvent, ...]:
        async with self._database.lock:
            return _append(self._database.state, stream_id, expected_sequence, events)

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        async with self._database.lock:
            return await _EventView(self._database.state).read(stream_id, after_sequence=after_sequence, limit=limit)

    async def latest_sequence(self, stream_id: str) -> int:
        async with self._database.lock:
            return len(self._database.state.streams.get(stream_id, ()))

    async def terminal_event(self, stream_id: str) -> StoredEvent | None:
        async with self._database.lock:
            return next((event for event in self._database.state.streams.get(stream_id, ()) if event.terminal), None)


class _EntityView:
    def __init__(self, state: _State) -> None:
        self._state = state

    async def get(self, collection: str, entity_id: str) -> Any | None:
        row = self._state.entities.get((collection, entity_id))
        return None if row is None else copy.deepcopy(row.value)

    async def put(self, collection: str, entity_id: str, value: Any, *, expected_revision: int | None) -> int:
        key = (collection, entity_id)
        row = self._state.entities.get(key)
        actual = 0 if row is None else row.revision
        if expected_revision is not None and expected_revision != actual:
            raise EntityRevisionConflict(collection, entity_id, expected_revision, actual)
        revision = actual + 1
        self._state.entities[key] = _EntityRow(revision, copy.deepcopy(value))
        return revision

    async def delete(self, collection: str, entity_id: str, *, expected_revision: int | None) -> None:
        key = (collection, entity_id)
        row = self._state.entities.get(key)
        actual = 0 if row is None else row.revision
        if expected_revision is not None and expected_revision != actual:
            raise EntityRevisionConflict(collection, entity_id, expected_revision, actual)
        self._state.entities.pop(key, None)


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("journal timestamps must be timezone-aware")


class _JournalView:
    def __init__(self, state: _State) -> None:
        self._state = state

    async def get(self, scope: str, idempotency_key: str) -> InvocationRecord | None:
        return self._state.journal.get((scope, idempotency_key))

    async def start(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        started_at: datetime,
    ) -> InvocationRecord:
        _aware(started_at)
        key = (scope, idempotency_key)
        existing = self._state.journal.get(key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise InvocationJournalConflict(f"journal key {key!r} is bound to different arguments")
            return existing
        record = InvocationRecord(scope, idempotency_key, request_hash, JournalState.STARTED, started_at, None, None)
        self._state.journal[key] = record
        return record

    async def complete(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        result: ToolResult,
        completed_at: datetime,
    ) -> InvocationRecord:
        _aware(completed_at)
        key = (scope, idempotency_key)
        existing = self._state.journal.get(key)
        if existing is None:
            raise InvocationJournalConflict(f"journal key {key!r} was not started")
        if existing.request_hash != request_hash:
            raise InvocationJournalConflict(f"journal key {key!r} is bound to different arguments")
        if existing.state is JournalState.COMPLETED:
            if existing.result != result:
                raise InvocationJournalConflict(f"journal key {key!r} completed with a different result")
            return existing
        record = InvocationRecord(
            scope,
            idempotency_key,
            request_hash,
            JournalState.COMPLETED,
            existing.started_at,
            completed_at,
            result,
        )
        self._state.journal[key] = record
        return record

    async def mark_unknown(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        completed_at: datetime,
    ) -> InvocationRecord:
        _aware(completed_at)
        key = (scope, idempotency_key)
        existing = self._state.journal.get(key)
        if existing is None or existing.request_hash != request_hash:
            raise InvocationJournalConflict(f"journal key {key!r} is missing or bound to different arguments")
        if existing.state is JournalState.COMPLETED:
            return existing
        record = InvocationRecord(
            scope,
            idempotency_key,
            request_hash,
            JournalState.UNKNOWN,
            existing.started_at,
            completed_at,
            None,
        )
        self._state.journal[key] = record
        return record


class InMemoryUnitOfWork:
    def __init__(self, database: _Database) -> None:
        self._database = database
        self._working: _State | None = None
        self._entered = False
        self._committed = False

    def _require_working(self) -> _State:
        if self._working is None:
            raise RuntimeError("unit of work is not active")
        if self._committed:
            raise RuntimeError("unit of work has already committed")
        return self._working

    @property
    def entities(self) -> _EntityView:
        return _EntityView(self._require_working())

    @property
    def events(self) -> _EventView:
        return _EventView(self._require_working())

    @property
    def journal(self) -> _JournalView:
        return _JournalView(self._require_working())

    async def __aenter__(self) -> InMemoryUnitOfWork:
        if self._entered:
            raise RuntimeError("unit of work cannot be entered twice")
        await self._database.lock.acquire()
        self._entered = True
        self._working = copy.deepcopy(self._database.state)
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if not self._committed:
                await self.rollback()
        finally:
            self._working = None
            if self._database.lock.locked():
                self._database.lock.release()

    async def commit(self) -> None:
        if self._committed:
            return
        working = self._require_working()
        self._database.state = working
        self._database.generation += 1
        self._committed = True

    async def rollback(self) -> None:
        if self._committed:
            raise RuntimeError("cannot roll back a committed unit of work")
        self._working = None


class InMemoryUnitOfWorkFactory:
    def __init__(self, event_store: InMemoryEventStore | None = None) -> None:
        self._database = event_store._database if event_store is not None else _Database()
        self.event_store = event_store or InMemoryEventStore(self._database)

    def begin(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self._database)

    async def get_entity(self, collection: str, entity_id: str) -> Any | None:
        async with self._database.lock:
            return await _EntityView(self._database.state).get(collection, entity_id)

    async def get_entity_revision(self, collection: str, entity_id: str) -> int:
        async with self._database.lock:
            row = self._database.state.entities.get((collection, entity_id))
            return 0 if row is None else row.revision

    async def get_journal(self, scope: str, idempotency_key: str) -> InvocationRecord | None:
        async with self._database.lock:
            return self._database.state.journal.get((scope, idempotency_key))


__all__ = ["InMemoryEventStore", "InMemoryUnitOfWork", "InMemoryUnitOfWorkFactory"]
