from __future__ import annotations

import json
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
from offeragent_harness.storage.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS


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
async def test_obsolete_runtime_flag_is_removed_atomically_from_durable_facts(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-state.sqlite"
    layer = {
        "schemaVersion": 1,
        "codec": "str",
        "payload": {
            "config": {"runtime": {"diagnostic_stdio": True, "worker_idle_seconds": 60}},
        },
    }
    receipt = {
        "schemaVersion": 1,
        "codec": "str",
        "payload": {
            "changedFields": ["runtime.diagnostic_stdio", "runtime.worker_idle_seconds"],
        },
    }
    event = {
        "changedFields": ["runtime.diagnostic_stdio", "runtime.worker_idle_seconds"],
        "revision": 1,
    }
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version >= 1),
                name TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            ) STRICT
            """
        )
        for migration in MIGRATIONS[:2]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, "2026-07-15T00:00:00Z"),
            )
        connection.execute("PRAGMA user_version = 2")
        connection.execute(
            "INSERT INTO entities(collection, entity_id, revision, value_json) VALUES (?, ?, ?, ?)",
            ("config_layers", "workspace:ws_test", 1, json.dumps(layer, separators=(",", ":"))),
        )
        connection.execute(
            "INSERT INTO entities(collection, entity_id, revision, value_json) VALUES (?, ?, ?, ?)",
            ("config_receipts", "workspace:ws_test:bootstrap", 1, json.dumps(receipt, separators=(",", ":"))),
        )
        connection.execute(
            "INSERT INTO event_streams(stream_id, latest_sequence) VALUES (?, ?)",
            ("config:workspace:ws_test", 1),
        )
        connection.execute(
            """
            INSERT INTO events(
                stream_id, sequence, event_id, event_type, payload_json,
                occurred_at, terminal, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "config:workspace:ws_test",
                1,
                "evt_legacy_runtime_flag",
                "config.changed",
                json.dumps(event, separators=(",", ":")),
                "2026-07-15T00:00:00Z",
                0,
                "legacy-runtime-flag",
            ),
        )

    await SqliteUnitOfWorkFactory(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        stored_layer = json.loads(
            connection.execute("SELECT value_json FROM entities WHERE collection = 'config_layers'").fetchone()[0]
        )
        stored_receipt = json.loads(
            connection.execute("SELECT value_json FROM entities WHERE collection = 'config_receipts'").fetchone()[0]
        )
        stored_event = json.loads(connection.execute("SELECT payload_json FROM events").fetchone()[0])
        residue = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT value_json AS value FROM entities
                UNION ALL
                SELECT payload_json AS value FROM events
                UNION ALL
                SELECT result_json AS value FROM invocation_journal
            ) WHERE CAST(value AS TEXT) LIKE '%diagnostic_stdio%'
            """
        ).fetchone()[0]
    assert stored_layer["payload"]["config"]["runtime"] == {"worker_idle_seconds": 60}
    assert stored_receipt["payload"]["changedFields"] == ["runtime.worker_idle_seconds"]
    assert stored_event["changedFields"] == ["runtime.worker_idle_seconds"]
    assert residue == 0


@pytest.mark.asyncio
async def test_plugin_owned_vault_authority_is_removed_from_durable_state(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-client-authority.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            ) STRICT
            """
        )
        for migration in MIGRATIONS[:3]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, "2026-07-15T00:00:00Z"),
            )
        connection.execute("PRAGMA user_version = 3")
        for collection, entity_id in (
            ("run_client_bindings", "run_legacy"),
            ("run_headless_vault_bindings", "run_legacy"),
            ("headless_vault_authorizations", "op_headless_00000000000000000000000000000000"),
            ("approvals", "apr_headless_legacy"),
        ):
            connection.execute(
                "INSERT INTO entities(collection, entity_id, revision, value_json) VALUES (?, ?, 1, '{}')",
                (collection, entity_id),
            )
        connection.execute(
            "INSERT INTO event_streams(stream_id, latest_sequence) VALUES ('headless-vault:ws_test', 1)"
        )
        connection.execute(
            """
            INSERT INTO events(
                stream_id, sequence, event_id, event_type, payload_json,
                occurred_at, terminal, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "headless-vault:ws_test",
                1,
                "evt_headless_legacy",
                "approval.required",
                '{"approvalId":"apr_headless_legacy"}',
                "2026-07-15T00:00:00Z",
                0,
                "headless-legacy",
            ),
        )

    await SqliteUnitOfWorkFactory(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        authority_entities = connection.execute(
            """
            SELECT COUNT(*) FROM entities
            WHERE collection IN (
                'run_client_bindings',
                'run_headless_vault_bindings',
                'headless_vault_authorizations'
            ) OR entity_id LIKE 'apr_headless_%'
            """
        ).fetchone()[0]
        legacy_events = connection.execute(
            "SELECT COUNT(*) FROM events WHERE payload_json LIKE '%apr_headless_%'"
        ).fetchone()[0]
        legacy_streams = connection.execute(
            "SELECT COUNT(*) FROM event_streams WHERE stream_id LIKE 'headless-vault:%'"
        ).fetchone()[0]
    assert authority_entities == 0
    assert legacy_events == 0
    assert legacy_streams == 0


@pytest.mark.asyncio
async def test_obsolete_per_turn_write_snapshots_are_deleted_instead_of_decoded_compatibly(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-write-intent.sqlite"
    obsolete_state = {
        "schemaVersion": 1,
        "codec": "run_state",
        "payload": {
            "writeObligation": {
                "required": True,
                "reasons": ["obsolete"],
                "outcomes": [],
                "intent": {"targetPaths": ["daily/2026-07-18.md"]},
            }
        },
    }
    current_state = {
        "schemaVersion": 1,
        "codec": "run_state",
        "payload": {
            "writeObligation": {
                "required": False,
                "reasons": [],
                "outcomes": [],
            }
        },
    }
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            ) STRICT
            """
        )
        for migration in MIGRATIONS[:4]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, "2026-07-15T00:00:00Z"),
            )
        connection.execute("PRAGMA user_version = 4")
        for run_id, value in (("run_obsolete", obsolete_state), ("run_current", current_state)):
            for collection in ("run_states", "run_effective_configs", "run_capability_snapshots"):
                connection.execute(
                    "INSERT INTO entities(collection, entity_id, revision, value_json) VALUES (?, ?, 1, ?)",
                    (collection, run_id, json.dumps(value, separators=(",", ":"))),
                )

    await SqliteUnitOfWorkFactory(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        remaining = connection.execute(
            """
            SELECT collection, entity_id
            FROM entities
            WHERE collection IN ('run_states', 'run_effective_configs', 'run_capability_snapshots')
            ORDER BY collection, entity_id
            """
        ).fetchall()
        obsolete_residue = connection.execute(
            "SELECT COUNT(*) FROM entities WHERE value_json LIKE '%daily/2026-07-18.md%'"
        ).fetchone()[0]
    assert remaining == [
        ("run_capability_snapshots", "run_current"),
        ("run_effective_configs", "run_current"),
        ("run_states", "run_current"),
    ]
    assert obsolete_residue == 0


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
