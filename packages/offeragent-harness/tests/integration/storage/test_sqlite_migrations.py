from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import (
    SqliteEntityStore,
    SqliteEventStore,
    SqliteInvocationJournal,
    SqliteUnitOfWorkFactory,
)
from offeragent_harness.ports import EntityStore, EventStore, InvocationJournal, UnitOfWorkFactory
from offeragent_harness.storage import SqliteMigrationChecksumError, SqliteMigrationError
from offeragent_harness.storage.migrations import LATEST_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_database_initialization_enforces_wal_pragmas_and_versioned_migrations(tmp_path: Path) -> None:
    database_path = tmp_path / "workspace" / "state.sqlite"
    factory = SqliteUnitOfWorkFactory(database_path, busy_timeout_ms=2_345)

    await factory.initialize()
    diagnostics = await factory.diagnostics()

    assert diagnostics.path == database_path.resolve()
    assert diagnostics.journal_mode == "wal"
    assert diagnostics.foreign_keys is True
    assert diagnostics.busy_timeout_ms == 2_345
    assert diagnostics.synchronous == 2  # FULL
    assert diagnostics.schema_version == LATEST_SCHEMA_VERSION

    with sqlite3.connect(database_path) as connection:
        migrations = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    assert [row[0] for row in migrations] == list(range(1, LATEST_SCHEMA_VERSION + 1))
    assert all(len(row[2]) == 64 for row in migrations)
    assert {
        "schema_migrations",
        "event_streams",
        "events",
        "entities",
        "invocation_journal",
    }.issubset(tables)

    # Initialization and migration application are restart-safe.
    await factory.initialize()
    reopened = SqliteUnitOfWorkFactory(database_path)
    await reopened.initialize()
    assert (await reopened.diagnostics()).schema_version == LATEST_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_migration_checksum_tampering_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    await SqliteUnitOfWorkFactory(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE schema_migrations SET checksum = ? WHERE version = 1", ("0" * 64,))

    with pytest.raises(SqliteMigrationChecksumError):
        await SqliteUnitOfWorkFactory(database_path).initialize()


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_its_migration_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE event_streams(unrelated TEXT)")

    with pytest.raises(sqlite3.OperationalError):
        await SqliteUnitOfWorkFactory(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        schema_migrations = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
    assert schema_migrations is None


@pytest.mark.asyncio
async def test_user_version_disagreement_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    await SqliteUnitOfWorkFactory(database_path).initialize()
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(SqliteMigrationError, match="disagrees"):
        await SqliteUnitOfWorkFactory(database_path).initialize()


def test_production_database_rejects_process_local_memory_and_uri_paths() -> None:
    with pytest.raises(ValueError, match="explicit filesystem path"):
        SqliteUnitOfWorkFactory(":memory:")
    with pytest.raises(ValueError, match="explicit filesystem path"):
        SqliteUnitOfWorkFactory("file:state.sqlite?mode=memory")


def test_sqlite_adapters_structurally_implement_storage_ports(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    event_store = SqliteEventStore(database_path)
    entity_store = SqliteEntityStore(database_path)
    journal = SqliteInvocationJournal(database_path)
    factory = SqliteUnitOfWorkFactory(database_path)

    assert isinstance(event_store, EventStore)
    assert isinstance(entity_store, EntityStore)
    assert isinstance(journal, InvocationJournal)
    assert isinstance(factory, UnitOfWorkFactory)
