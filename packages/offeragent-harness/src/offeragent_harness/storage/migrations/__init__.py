"""Ordered, checksum-verified SQLite migrations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import (
    v0001_initial,
    v0002_remove_legacy_skill_state,
    v0003_remove_obsolete_runtime_config,
    v0004_remove_client_vault_authority,
    v0005_remove_obsolete_write_intent_snapshots,
    v0006_canonicalize_tool_result_context,
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    checksum: str
    vacuum_after: bool = False
    legacy_identities: frozenset[tuple[str, str]] = frozenset()

    @classmethod
    def create(
        cls,
        version: int,
        name: str,
        statements: tuple[str, ...],
        *,
        vacuum_after: bool = False,
        legacy_identities: frozenset[tuple[str, str]] = frozenset(),
    ) -> Migration:
        material = "\n-- statement boundary --\n".join(statement.strip() for statement in statements)
        checksum = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return cls(
            version=version,
            name=name,
            statements=statements,
            checksum=checksum,
            vacuum_after=vacuum_after,
            legacy_identities=legacy_identities,
        )

    def matches_applied(self, name: str, checksum: str) -> bool:
        return (name == self.name and checksum == self.checksum) or (name, checksum) in self.legacy_identities


MIGRATIONS = (
    Migration.create(v0001_initial.VERSION, v0001_initial.NAME, v0001_initial.STATEMENTS),
    Migration.create(
        v0002_remove_legacy_skill_state.VERSION,
        v0002_remove_legacy_skill_state.NAME,
        v0002_remove_legacy_skill_state.STATEMENTS,
        vacuum_after=True,
    ),
    Migration.create(
        v0003_remove_obsolete_runtime_config.VERSION,
        v0003_remove_obsolete_runtime_config.NAME,
        v0003_remove_obsolete_runtime_config.STATEMENTS,
    ),
    Migration.create(
        v0004_remove_client_vault_authority.VERSION,
        v0004_remove_client_vault_authority.NAME,
        v0004_remove_client_vault_authority.STATEMENTS,
        vacuum_after=True,
    ),
    Migration.create(
        v0005_remove_obsolete_write_intent_snapshots.VERSION,
        v0005_remove_obsolete_write_intent_snapshots.NAME,
        v0005_remove_obsolete_write_intent_snapshots.STATEMENTS,
        vacuum_after=True,
    ),
    Migration.create(
        v0006_canonicalize_tool_result_context.VERSION,
        v0006_canonicalize_tool_result_context.NAME,
        v0006_canonicalize_tool_result_context.STATEMENTS,
        vacuum_after=True,
    ),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version
LATEST_REQUIRED_TABLES = frozenset(
    {
        "schema_migrations",
        "event_streams",
        "events",
        "entities",
        "invocation_journal",
    }
)

__all__ = ["LATEST_REQUIRED_TABLES", "LATEST_SCHEMA_VERSION", "MIGRATIONS", "Migration"]
