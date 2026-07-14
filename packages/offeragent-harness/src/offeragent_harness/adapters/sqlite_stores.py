"""Production stdlib-SQLite adapters for the Windows-local Harness.

Every standalone operation owns a short-lived connection and transaction.  A
``SqliteUnitOfWork`` owns one connection for its entire atomic boundary.  All
SQLite calls run off the asyncio event-loop and all writes use ``BEGIN
IMMEDIATE`` so revision/sequence checks are real database compare-and-swap
operations rather than process-local guesses.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from offeragent_harness.ports import (
    EntityRecord,
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
from offeragent_harness.storage.entity_codecs import (
    EntityCodecRegistry,
    approval_record_codec,
    core_entity_codec_registry,
)
from offeragent_harness.storage.serialization import (
    dump_datetime,
    dump_json,
    dump_tool_result,
    invocation_record_from_row,
    stored_event_from_row,
)
from offeragent_harness.storage.sqlite import SqliteDatabase, SqliteDatabaseInfo, run_in_worker
from offeragent_harness.tools import ToolResult

T = TypeVar("T")
DatabaseTarget = str | Path | SqliteDatabase


def _database(target: DatabaseTarget, busy_timeout_ms: int) -> SqliteDatabase:
    return target if isinstance(target, SqliteDatabase) else SqliteDatabase(target, busy_timeout_ms=busy_timeout_ms)


def default_entity_codecs() -> EntityCodecRegistry:
    """Build the explicit production codec set at the outer adapter boundary."""

    from offeragent_harness.runtime.approval_manager import ApprovalRecord

    return core_entity_codec_registry().with_codec(approval_record_codec(ApprovalRecord))


def _same_event(stored: StoredEvent, candidate: NewEvent) -> bool:
    return (
        stored.event_id == candidate.event_id
        and stored.event_type == candidate.event_type
        and stored.payload == candidate.payload
        and stored.occurred_at == candidate.occurred_at
        and stored.terminal == candidate.terminal
        and stored.idempotency_key == candidate.idempotency_key
    )


def _append_events(
    connection: sqlite3.Connection,
    stream_id: str,
    expected_sequence: int,
    events: Sequence[NewEvent],
) -> tuple[StoredEvent, ...]:
    if expected_sequence < 0:
        raise ValueError("expected sequence cannot be negative")
    if not stream_id:
        raise ValueError("stream_id must not be empty")
    if not events:
        return ()

    idempotency_keys = [event.idempotency_key for event in events]
    event_ids = [event.event_id for event in events]
    if len(set(idempotency_keys)) != len(idempotency_keys):
        raise EventIdempotencyConflict("a batch cannot repeat an idempotency key")
    if len(set(event_ids)) != len(event_ids):
        raise EventIdConflict("a batch cannot repeat an event_id")

    replay_rows = [
        connection.execute(
            "SELECT * FROM events WHERE stream_id = ? AND idempotency_key = ?",
            (stream_id, event.idempotency_key),
        ).fetchone()
        for event in events
    ]
    if any(row is not None for row in replay_rows):
        if not all(row is not None for row in replay_rows):
            raise EventIdempotencyConflict("partial batch replay is not allowed")
        replayed = tuple(stored_event_from_row(row) for row in replay_rows if row is not None)
        for stored, candidate in zip(replayed, events, strict=True):
            if not _same_event(stored, candidate):
                raise EventIdempotencyConflict(
                    f"idempotency key {candidate.idempotency_key!r} is bound to a different event"
                )
        return replayed

    for candidate in events:
        existing = connection.execute(
            "SELECT stream_id FROM events WHERE event_id = ?",
            (candidate.event_id,),
        ).fetchone()
        if existing is not None:
            raise EventIdConflict(f"event_id {candidate.event_id!r} already exists in {existing['stream_id']!r}")

    stream_row = connection.execute(
        "SELECT latest_sequence, terminal_sequence FROM event_streams WHERE stream_id = ?",
        (stream_id,),
    ).fetchone()
    actual_sequence = 0 if stream_row is None else int(stream_row["latest_sequence"])
    if actual_sequence != expected_sequence:
        raise SequenceConflict(stream_id, expected_sequence, actual_sequence)
    if stream_row is not None and stream_row["terminal_sequence"] is not None:
        raise TerminalEventConflict(
            f"stream {stream_id!r} already terminated at sequence {stream_row['terminal_sequence']}"
        )

    terminal_indexes = [index for index, event in enumerate(events) if event.terminal]
    if len(terminal_indexes) > 1:
        raise TerminalEventConflict("a batch cannot contain multiple terminal events")
    if terminal_indexes and terminal_indexes[0] != len(events) - 1:
        raise TerminalEventConflict("terminal event must be the final event in its batch")

    if stream_row is None:
        connection.execute(
            "INSERT INTO event_streams(stream_id, latest_sequence, terminal_sequence) VALUES (?, 0, NULL)",
            (stream_id,),
        )

    appended: list[StoredEvent] = []
    for offset, candidate in enumerate(events, start=1):
        stored = StoredEvent.from_new(stream_id, actual_sequence + offset, candidate)
        connection.execute(
            """
            INSERT INTO events(
                stream_id, sequence, event_id, event_type, payload_json,
                occurred_at, terminal, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored.stream_id,
                stored.sequence,
                stored.event_id,
                stored.event_type,
                dump_json(stored.payload),
                dump_datetime(stored.occurred_at),
                int(stored.terminal),
                stored.idempotency_key,
            ),
        )
        appended.append(stored)

    new_sequence = actual_sequence + len(appended)
    terminal_sequence = new_sequence if terminal_indexes else None
    updated = connection.execute(
        """
        UPDATE event_streams
        SET latest_sequence = ?, terminal_sequence = ?
        WHERE stream_id = ? AND latest_sequence = ? AND terminal_sequence IS NULL
        """,
        (new_sequence, terminal_sequence, stream_id, actual_sequence),
    )
    if updated.rowcount != 1:
        latest = connection.execute(
            "SELECT latest_sequence, terminal_sequence FROM event_streams WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()
        if latest is not None and latest["terminal_sequence"] is not None:
            raise TerminalEventConflict(
                f"stream {stream_id!r} already terminated at sequence {latest['terminal_sequence']}"
            )
        current = 0 if latest is None else int(latest["latest_sequence"])
        raise SequenceConflict(stream_id, expected_sequence, current)
    return tuple(appended)


def _read_events(
    connection: sqlite3.Connection,
    stream_id: str,
    after_sequence: int,
    limit: int | None,
) -> tuple[StoredEvent, ...]:
    if after_sequence < 0 or (limit is not None and limit < 0):
        raise ValueError("event cursor and limit cannot be negative")
    if limit == 0:
        return ()
    sql = "SELECT * FROM events WHERE stream_id = ? AND sequence > ? ORDER BY sequence"
    parameters: tuple[object, ...] = (stream_id, after_sequence)
    if limit is not None:
        sql += " LIMIT ?"
        parameters += (limit,)
    return tuple(stored_event_from_row(row) for row in connection.execute(sql, parameters).fetchall())


def _latest_sequence(connection: sqlite3.Connection, stream_id: str) -> int:
    row = connection.execute(
        "SELECT latest_sequence FROM event_streams WHERE stream_id = ?",
        (stream_id,),
    ).fetchone()
    return 0 if row is None else int(row["latest_sequence"])


def _terminal_event(connection: sqlite3.Connection, stream_id: str) -> StoredEvent | None:
    row = connection.execute(
        "SELECT * FROM events WHERE stream_id = ? AND terminal = 1",
        (stream_id,),
    ).fetchone()
    return None if row is None else stored_event_from_row(row)


def _get_entity(
    connection: sqlite3.Connection,
    codecs: EntityCodecRegistry,
    collection: str,
    entity_id: str,
) -> Any | None:
    row = connection.execute(
        "SELECT value_json FROM entities WHERE collection = ? AND entity_id = ?",
        (collection, entity_id),
    ).fetchone()
    return None if row is None else codecs.decode(collection, row["value_json"])


def _get_entity_revision(connection: sqlite3.Connection, collection: str, entity_id: str) -> int:
    row = connection.execute(
        "SELECT revision FROM entities WHERE collection = ? AND entity_id = ?",
        (collection, entity_id),
    ).fetchone()
    return 0 if row is None else int(row["revision"])


def _list_entities(
    connection: sqlite3.Connection,
    codecs: EntityCodecRegistry,
    collection: str,
    after_id: str | None,
    limit: int,
) -> tuple[EntityRecord, ...]:
    if limit < 1 or limit > 1_000:
        raise ValueError("entity page limit must be between 1 and 1000")
    if after_id is None:
        rows = connection.execute(
            """
            SELECT entity_id, revision, value_json
            FROM entities
            WHERE collection = ?
            ORDER BY entity_id COLLATE BINARY
            LIMIT ?
            """,
            (collection, limit),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT entity_id, revision, value_json
            FROM entities
            WHERE collection = ? AND entity_id > ? COLLATE BINARY
            ORDER BY entity_id COLLATE BINARY
            LIMIT ?
            """,
            (collection, after_id, limit),
        ).fetchall()
    return tuple(
        EntityRecord(
            entity_id=str(row["entity_id"]),
            revision=int(row["revision"]),
            value=codecs.decode(collection, row["value_json"]),
        )
        for row in rows
    )


def _put_entity(
    connection: sqlite3.Connection,
    codecs: EntityCodecRegistry,
    collection: str,
    entity_id: str,
    value: Any,
    expected_revision: int | None,
) -> int:
    value_json = codecs.encode(collection, value)
    actual = _get_entity_revision(connection, collection, entity_id)
    if expected_revision is not None and expected_revision != actual:
        raise EntityRevisionConflict(collection, entity_id, expected_revision, actual)
    revision = actual + 1
    if actual == 0:
        connection.execute(
            "INSERT INTO entities(collection, entity_id, revision, value_json) VALUES (?, ?, ?, ?)",
            (collection, entity_id, revision, value_json),
        )
    else:
        updated = connection.execute(
            """
            UPDATE entities SET revision = ?, value_json = ?
            WHERE collection = ? AND entity_id = ? AND revision = ?
            """,
            (revision, value_json, collection, entity_id, actual),
        )
        if updated.rowcount != 1:
            current = _get_entity_revision(connection, collection, entity_id)
            raise EntityRevisionConflict(collection, entity_id, actual, current)
    return revision


def _delete_entity(
    connection: sqlite3.Connection,
    collection: str,
    entity_id: str,
    expected_revision: int | None,
) -> None:
    actual = _get_entity_revision(connection, collection, entity_id)
    if expected_revision is not None and expected_revision != actual:
        raise EntityRevisionConflict(collection, entity_id, expected_revision, actual)
    if actual != 0:
        deleted = connection.execute(
            "DELETE FROM entities WHERE collection = ? AND entity_id = ? AND revision = ?",
            (collection, entity_id, actual),
        )
        if deleted.rowcount != 1:
            current = _get_entity_revision(connection, collection, entity_id)
            raise EntityRevisionConflict(collection, entity_id, actual, current)


def _get_journal(connection: sqlite3.Connection, scope: str, idempotency_key: str) -> InvocationRecord | None:
    row = connection.execute(
        "SELECT * FROM invocation_journal WHERE scope = ? AND idempotency_key = ?",
        (scope, idempotency_key),
    ).fetchone()
    return None if row is None else invocation_record_from_row(row)


def _start_journal(
    connection: sqlite3.Connection,
    scope: str,
    idempotency_key: str,
    request_hash: str,
    started_at: datetime,
) -> InvocationRecord:
    encoded_started_at = dump_datetime(started_at)
    existing = _get_journal(connection, scope, idempotency_key)
    if existing is not None:
        if existing.request_hash != request_hash:
            raise InvocationJournalConflict(f"journal key {(scope, idempotency_key)!r} is bound to different arguments")
        return existing
    connection.execute(
        """
        INSERT INTO invocation_journal(
            scope, idempotency_key, request_hash, state, started_at, completed_at, result_json
        ) VALUES (?, ?, ?, 'started', ?, NULL, NULL)
        """,
        (scope, idempotency_key, request_hash, encoded_started_at),
    )
    record = _get_journal(connection, scope, idempotency_key)
    if record is None:
        raise RuntimeError("journal insert did not produce a record")
    return record


def _complete_journal(
    connection: sqlite3.Connection,
    scope: str,
    idempotency_key: str,
    request_hash: str,
    result: ToolResult,
    completed_at: datetime,
) -> InvocationRecord:
    encoded_completed_at = dump_datetime(completed_at)
    encoded_result = dump_tool_result(result)
    existing = _get_journal(connection, scope, idempotency_key)
    key = (scope, idempotency_key)
    if existing is None:
        raise InvocationJournalConflict(f"journal key {key!r} was not started")
    if existing.request_hash != request_hash:
        raise InvocationJournalConflict(f"journal key {key!r} is bound to different arguments")
    if existing.state is JournalState.COMPLETED:
        if existing.result != result:
            raise InvocationJournalConflict(f"journal key {key!r} completed with a different result")
        return existing
    connection.execute(
        """
        UPDATE invocation_journal
        SET state = 'completed', completed_at = ?, result_json = ?
        WHERE scope = ? AND idempotency_key = ? AND request_hash = ?
        """,
        (encoded_completed_at, encoded_result, scope, idempotency_key, request_hash),
    )
    record = _get_journal(connection, scope, idempotency_key)
    if record is None:
        raise RuntimeError("journal completion lost its record")
    return record


def _mark_journal_unknown(
    connection: sqlite3.Connection,
    scope: str,
    idempotency_key: str,
    request_hash: str,
    completed_at: datetime,
) -> InvocationRecord:
    encoded_completed_at = dump_datetime(completed_at)
    existing = _get_journal(connection, scope, idempotency_key)
    key = (scope, idempotency_key)
    if existing is None or existing.request_hash != request_hash:
        raise InvocationJournalConflict(f"journal key {key!r} is missing or bound to different arguments")
    if existing.state in {JournalState.COMPLETED, JournalState.UNKNOWN}:
        return existing
    connection.execute(
        """
        UPDATE invocation_journal
        SET state = 'unknown', completed_at = ?, result_json = NULL
        WHERE scope = ? AND idempotency_key = ? AND request_hash = ?
        """,
        (encoded_completed_at, scope, idempotency_key, request_hash),
    )
    record = _get_journal(connection, scope, idempotency_key)
    if record is None:
        raise RuntimeError("journal unknown transition lost its record")
    return record


class SqliteEventStore:
    def __init__(self, target: DatabaseTarget, *, busy_timeout_ms: int = 5_000) -> None:
        self._database = _database(target, busy_timeout_ms)

    async def append(
        self,
        stream_id: str,
        expected_sequence: int,
        events: Sequence[NewEvent],
    ) -> tuple[StoredEvent, ...]:
        return await self._database.write(
            lambda connection: _append_events(connection, stream_id, expected_sequence, events)
        )

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        return await self._database.read(lambda connection: _read_events(connection, stream_id, after_sequence, limit))

    async def latest_sequence(self, stream_id: str) -> int:
        return await self._database.read(lambda connection: _latest_sequence(connection, stream_id))

    async def terminal_event(self, stream_id: str) -> StoredEvent | None:
        return await self._database.read(lambda connection: _terminal_event(connection, stream_id))


class SqliteEntityStore:
    def __init__(
        self,
        target: DatabaseTarget,
        *,
        busy_timeout_ms: int = 5_000,
        entity_codecs: EntityCodecRegistry | None = None,
    ) -> None:
        self._database = _database(target, busy_timeout_ms)
        self._entity_codecs = entity_codecs or default_entity_codecs()

    async def get(self, collection: str, entity_id: str) -> Any | None:
        return await self._database.read(
            lambda connection: _get_entity(connection, self._entity_codecs, collection, entity_id)
        )

    async def get_revision(self, collection: str, entity_id: str) -> int:
        return await self._database.read(lambda connection: _get_entity_revision(connection, collection, entity_id))

    async def list(
        self,
        collection: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> tuple[EntityRecord, ...]:
        return await self._database.read(
            lambda connection: _list_entities(
                connection,
                self._entity_codecs,
                collection,
                after_id,
                limit,
            )
        )

    async def put(
        self,
        collection: str,
        entity_id: str,
        value: Any,
        *,
        expected_revision: int | None,
    ) -> int:
        return await self._database.write(
            lambda connection: _put_entity(
                connection,
                self._entity_codecs,
                collection,
                entity_id,
                value,
                expected_revision,
            )
        )

    async def delete(
        self,
        collection: str,
        entity_id: str,
        *,
        expected_revision: int | None,
    ) -> None:
        await self._database.write(
            lambda connection: _delete_entity(connection, collection, entity_id, expected_revision)
        )


class SqliteInvocationJournal:
    def __init__(self, target: DatabaseTarget, *, busy_timeout_ms: int = 5_000) -> None:
        self._database = _database(target, busy_timeout_ms)

    async def get(self, scope: str, idempotency_key: str) -> InvocationRecord | None:
        return await self._database.read(lambda connection: _get_journal(connection, scope, idempotency_key))

    async def start(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        started_at: datetime,
    ) -> InvocationRecord:
        return await self._database.write(
            lambda connection: _start_journal(connection, scope, idempotency_key, request_hash, started_at)
        )

    async def complete(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        result: ToolResult,
        completed_at: datetime,
    ) -> InvocationRecord:
        return await self._database.write(
            lambda connection: _complete_journal(connection, scope, idempotency_key, request_hash, result, completed_at)
        )

    async def mark_unknown(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        completed_at: datetime,
    ) -> InvocationRecord:
        return await self._database.write(
            lambda connection: _mark_journal_unknown(connection, scope, idempotency_key, request_hash, completed_at)
        )


class _UnitOfWorkEventStore:
    def __init__(self, unit_of_work: SqliteUnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    async def append(
        self,
        stream_id: str,
        expected_sequence: int,
        events: Sequence[NewEvent],
    ) -> tuple[StoredEvent, ...]:
        return await self._unit_of_work._execute(_append_events, stream_id, expected_sequence, events)

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        return await self._unit_of_work._execute(_read_events, stream_id, after_sequence, limit)

    async def latest_sequence(self, stream_id: str) -> int:
        return await self._unit_of_work._execute(_latest_sequence, stream_id)

    async def terminal_event(self, stream_id: str) -> StoredEvent | None:
        return await self._unit_of_work._execute(_terminal_event, stream_id)


class _UnitOfWorkEntityStore:
    def __init__(self, unit_of_work: SqliteUnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    async def get(self, collection: str, entity_id: str) -> Any | None:
        return await self._unit_of_work._execute(
            _get_entity,
            self._unit_of_work._entity_codecs,
            collection,
            entity_id,
        )

    async def list(
        self,
        collection: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> tuple[EntityRecord, ...]:
        return await self._unit_of_work._execute(
            _list_entities,
            self._unit_of_work._entity_codecs,
            collection,
            after_id,
            limit,
        )

    async def put(
        self,
        collection: str,
        entity_id: str,
        value: Any,
        *,
        expected_revision: int | None,
    ) -> int:
        return await self._unit_of_work._execute(
            _put_entity,
            self._unit_of_work._entity_codecs,
            collection,
            entity_id,
            value,
            expected_revision,
        )

    async def delete(
        self,
        collection: str,
        entity_id: str,
        *,
        expected_revision: int | None,
    ) -> None:
        await self._unit_of_work._execute(_delete_entity, collection, entity_id, expected_revision)


class _UnitOfWorkInvocationJournal:
    def __init__(self, unit_of_work: SqliteUnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    async def get(self, scope: str, idempotency_key: str) -> InvocationRecord | None:
        return await self._unit_of_work._execute(_get_journal, scope, idempotency_key)

    async def start(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        started_at: datetime,
    ) -> InvocationRecord:
        return await self._unit_of_work._execute(_start_journal, scope, idempotency_key, request_hash, started_at)

    async def complete(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        result: ToolResult,
        completed_at: datetime,
    ) -> InvocationRecord:
        return await self._unit_of_work._execute(
            _complete_journal, scope, idempotency_key, request_hash, result, completed_at
        )

    async def mark_unknown(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        completed_at: datetime,
    ) -> InvocationRecord:
        return await self._unit_of_work._execute(
            _mark_journal_unknown, scope, idempotency_key, request_hash, completed_at
        )


class SqliteUnitOfWork:
    def __init__(self, database: SqliteDatabase, entity_codecs: EntityCodecRegistry) -> None:
        self._database = database
        self._entity_codecs = entity_codecs
        self._connection: sqlite3.Connection | None = None
        self._operation_lock = asyncio.Lock()
        self._entered = False
        self._committed = False
        self._rolled_back = False

    def _open_transaction(self) -> sqlite3.Connection:
        connection = self._database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _rollback_and_close(connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()

    async def __aenter__(self) -> SqliteUnitOfWork:
        if self._entered:
            raise RuntimeError("unit of work cannot be entered twice")
        self._entered = True
        await self._database.initialize()
        open_task = asyncio.create_task(asyncio.to_thread(self._open_transaction))
        try:
            self._connection = await asyncio.shield(open_task)
        except asyncio.CancelledError:
            try:
                connection = await open_task
            except Exception:
                pass
            else:
                await run_in_worker(self._rollback_and_close, connection)
            raise
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            if not self._committed and not self._rolled_back:
                await self.rollback()
        finally:
            await run_in_worker(connection.close)
            self._connection = None

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("unit of work is not active")
        if self._committed:
            raise RuntimeError("unit of work has already committed")
        if self._rolled_back:
            raise RuntimeError("unit of work has already rolled back")
        return self._connection

    async def _execute(self, function: Callable[..., T], *args: object) -> T:
        async with self._operation_lock:
            connection = self._require_connection()
            return await run_in_worker(function, connection, *args)

    @property
    def entities(self) -> _UnitOfWorkEntityStore:
        self._require_connection()
        return _UnitOfWorkEntityStore(self)

    @property
    def events(self) -> _UnitOfWorkEventStore:
        self._require_connection()
        return _UnitOfWorkEventStore(self)

    @property
    def journal(self) -> _UnitOfWorkInvocationJournal:
        self._require_connection()
        return _UnitOfWorkInvocationJournal(self)

    async def commit(self) -> None:
        if self._committed:
            return
        if self._rolled_back:
            raise RuntimeError("cannot commit a rolled-back unit of work")
        async with self._operation_lock:
            connection = self._require_connection()
            commit_task = asyncio.create_task(asyncio.to_thread(connection.commit))
            try:
                await asyncio.shield(commit_task)
            except asyncio.CancelledError:
                try:
                    await commit_task
                except Exception:
                    pass
                else:
                    self._committed = True
                raise
            self._committed = True

    async def rollback(self) -> None:
        if self._committed:
            raise RuntimeError("cannot roll back a committed unit of work")
        if self._rolled_back:
            return
        async with self._operation_lock:
            if self._connection is None:
                raise RuntimeError("unit of work is not active")
            rollback_task = asyncio.create_task(asyncio.to_thread(self._connection.rollback))
            try:
                await asyncio.shield(rollback_task)
            except asyncio.CancelledError:
                try:
                    await rollback_task
                except Exception:
                    pass
                else:
                    self._rolled_back = True
                raise
            self._rolled_back = True


class SqliteUnitOfWorkFactory:
    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        entity_codecs: EntityCodecRegistry | None = None,
    ) -> None:
        self._database = SqliteDatabase(path, busy_timeout_ms=busy_timeout_ms)
        self._entity_codecs = entity_codecs or default_entity_codecs()
        self.event_store = SqliteEventStore(self._database)
        self.entity_store = SqliteEntityStore(self._database, entity_codecs=self._entity_codecs)
        self.invocation_journal = SqliteInvocationJournal(self._database)

    @property
    def path(self) -> Path:
        return self._database.path

    async def initialize(self) -> None:
        await self._database.initialize()

    def begin(self) -> SqliteUnitOfWork:
        return SqliteUnitOfWork(self._database, self._entity_codecs)

    async def diagnostics(self) -> SqliteDatabaseInfo:
        return await self._database.diagnostics()

    async def get_entity(self, collection: str, entity_id: str) -> Any | None:
        return await self.entity_store.get(collection, entity_id)

    async def get_entity_revision(self, collection: str, entity_id: str) -> int:
        return await self.entity_store.get_revision(collection, entity_id)

    async def list_entities(
        self,
        collection: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> tuple[EntityRecord, ...]:
        return await self.entity_store.list(collection, after_id, limit)

    async def get_journal(self, scope: str, idempotency_key: str) -> InvocationRecord | None:
        return await self.invocation_journal.get(scope, idempotency_key)


__all__ = [
    "SqliteEntityStore",
    "SqliteEventStore",
    "SqliteInvocationJournal",
    "SqliteUnitOfWork",
    "SqliteUnitOfWorkFactory",
    "default_entity_codecs",
]
