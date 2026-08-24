"""Small repository contracts composed by a Unit of Work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from offeragent_harness.error_codes import ResourceConflictCause
from offeragent_harness.tools import ToolResult


@runtime_checkable
class EntityStore(Protocol):
    async def get(self, collection: str, entity_id: str) -> Any | None: ...

    async def list(
        self,
        collection: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> tuple[EntityRecord, ...]: ...

    async def put(self, collection: str, entity_id: str, value: Any, *, expected_revision: int | None) -> int: ...

    async def delete(self, collection: str, entity_id: str, *, expected_revision: int | None) -> None: ...


@dataclass(frozen=True)
class EntityRecord:
    """One revisioned entity returned by stable, ID-ordered pagination."""

    entity_id: str
    revision: int
    value: Any


class EntityRevisionConflict(RuntimeError, ResourceConflictCause):
    def __init__(self, collection: str, entity_id: str, expected: int, actual: int) -> None:
        self.collection = collection
        self.entity_id = entity_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"{collection}/{entity_id} expected revision {expected}, actual {actual}")


class JournalState(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InvocationRecord:
    scope: str
    idempotency_key: str
    request_hash: str
    state: JournalState
    started_at: datetime
    completed_at: datetime | None
    result: ToolResult | None


class InvocationJournalConflict(RuntimeError, ResourceConflictCause):
    pass


@runtime_checkable
class InvocationJournal(Protocol):
    async def get(self, scope: str, idempotency_key: str) -> InvocationRecord | None: ...

    async def start(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        started_at: datetime,
    ) -> InvocationRecord: ...

    async def complete(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        result: ToolResult,
        completed_at: datetime,
    ) -> InvocationRecord: ...

    async def mark_unknown(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        completed_at: datetime,
    ) -> InvocationRecord: ...


__all__ = [
    "EntityRecord",
    "EntityRevisionConflict",
    "EntityStore",
    "InvocationJournal",
    "InvocationJournalConflict",
    "InvocationRecord",
    "JournalState",
]
