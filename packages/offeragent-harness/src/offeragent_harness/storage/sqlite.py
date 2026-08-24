"""Async-safe stdlib SQLite connection factory and migration runner."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from .migrations import LATEST_REQUIRED_TABLES, LATEST_SCHEMA_VERSION, MIGRATIONS

T = TypeVar("T")

_INITIALIZATION_LOCKS_GUARD = threading.Lock()
_INITIALIZATION_LOCKS: dict[Path, threading.Lock] = {}


def _initialization_lock(path: Path) -> threading.Lock:
    """Return the process-wide migration lock for one canonical database path."""

    with _INITIALIZATION_LOCKS_GUARD:
        return _INITIALIZATION_LOCKS.setdefault(path, threading.Lock())


class SqliteStorageError(RuntimeError):
    pass


class SqliteMigrationError(SqliteStorageError):
    pass


class SqliteMigrationChecksumError(SqliteMigrationError):
    pass


@dataclass(frozen=True)
class SqliteDatabaseInfo:
    path: Path
    journal_mode: str
    foreign_keys: bool
    busy_timeout_ms: int
    synchronous: int
    schema_version: int


async def run_in_worker(function: Callable[..., T], *args: object) -> T:
    """Run SQLite work off-loop and finish it safely before propagating cancellation.

    Cancelling ``asyncio.to_thread`` does not stop its worker thread.  Shielding and
    joining the worker prevents a connection from being closed or rolled back while
    the SQLite call is still using it.
    """

    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


class SqliteDatabase:
    """Explicit-path SQLite database with no process-global workspace state."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        raw_path = str(path)
        if raw_path == ":memory:" or raw_path.startswith("file:"):
            raise ValueError("SQLite production storage requires an explicit filesystem path")
        self.path = Path(path).expanduser().resolve(strict=False)
        self.busy_timeout_ms = busy_timeout_ms
        self._initialize_lock = asyncio.Lock()
        self._initialized = False

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await run_in_worker(self._initialize_sync)
            self._initialized = True

    def _initialize_sync(self) -> None:
        with _initialization_lock(self.path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self.connect()
            try:
                self._ensure_wal(connection)
                if self._validated_schema_version(connection) == LATEST_SCHEMA_VERSION:
                    return

                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY CHECK (version >= 1),
                        name TEXT NOT NULL UNIQUE,
                        checksum TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    ) STRICT
                    """
                )
                current_version = self._validated_schema_version(connection)

                vacuum_after_commit = False
                for migration in MIGRATIONS:
                    if migration.version <= current_version:
                        continue
                    for statement in migration.statements:
                        connection.execute(statement)
                    applied_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                        (migration.version, migration.name, migration.checksum, applied_at),
                    )
                    connection.execute(f"PRAGMA user_version = {migration.version}")
                    vacuum_after_commit = vacuum_after_commit or migration.vacuum_after
                connection.commit()
                if vacuum_after_commit:
                    connection.execute("VACUUM")
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()

    def _ensure_wal(self, connection: sqlite3.Connection) -> None:
        current_mode_row = connection.execute("PRAGMA journal_mode").fetchone()
        current_mode = "" if current_mode_row is None else str(current_mode_row[0]).lower()
        if current_mode != "wal":
            mode_row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            current_mode = "" if mode_row is None else str(mode_row[0]).lower()
        if current_mode != "wal":
            raise SqliteMigrationError(f"SQLite refused WAL mode for {self.path}: {current_mode!r}")

    def _validated_schema_version(self, connection: sqlite3.Connection) -> int:
        migration_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if migration_table is None:
            if user_version != 0:
                raise SqliteMigrationError(f"PRAGMA user_version {user_version} exists without schema_migrations")
            return 0

        applied_rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied = {int(row["version"]): row for row in applied_rows}
        supported = {migration.version: migration for migration in MIGRATIONS}
        unknown = sorted(set(applied) - set(supported))
        if unknown:
            raise SqliteMigrationError(
                f"database schema version is newer or unknown: {unknown}; supported through {LATEST_SCHEMA_VERSION}"
            )
        current_version = max(applied, default=0)
        if sorted(applied) != list(range(1, current_version + 1)):
            raise SqliteMigrationError("schema_migrations contains a version gap")
        if user_version != current_version:
            raise SqliteMigrationError(
                f"PRAGMA user_version {user_version} disagrees with schema_migrations version {current_version}"
            )
        for version, row in applied.items():
            migration = supported[version]
            if not migration.matches_applied(str(row["name"]), str(row["checksum"])):
                raise SqliteMigrationChecksumError(f"migration {version} does not match the packaged name/checksum")
        if current_version == LATEST_SCHEMA_VERSION:
            tables = {
                str(row["name"])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            missing = sorted(LATEST_REQUIRED_TABLES - tables)
            if missing:
                raise SqliteMigrationError(f"current schema is missing required tables: {missing}")
        return current_version

    async def read(self, function: Callable[[sqlite3.Connection], T]) -> T:
        await self.initialize()
        return await run_in_worker(self._read_sync, function)

    def _read_sync(self, function: Callable[[sqlite3.Connection], T]) -> T:
        connection = self.connect()
        try:
            return function(connection)
        finally:
            connection.close()

    async def write(self, function: Callable[[sqlite3.Connection], T]) -> T:
        await self.initialize()
        return await run_in_worker(self._write_sync, function)

    def _write_sync(self, function: Callable[[sqlite3.Connection], T]) -> T:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = function(connection)
            connection.commit()
            return result
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    async def diagnostics(self) -> SqliteDatabaseInfo:
        await self.initialize()

        def inspect(connection: sqlite3.Connection) -> SqliteDatabaseInfo:
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            foreign_keys = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
            busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
            synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            return SqliteDatabaseInfo(
                path=self.path,
                journal_mode=journal_mode,
                foreign_keys=foreign_keys,
                busy_timeout_ms=busy_timeout,
                synchronous=synchronous,
                schema_version=schema_version,
            )

        return await self.read(inspect)


__all__ = [
    "SqliteDatabase",
    "SqliteDatabaseInfo",
    "SqliteMigrationChecksumError",
    "SqliteMigrationError",
    "SqliteStorageError",
    "run_in_worker",
]
