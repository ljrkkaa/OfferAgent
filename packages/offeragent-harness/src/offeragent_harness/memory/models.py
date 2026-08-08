"""Typed, source-bound records for durable personal memory."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from offeragent_harness.tools import canonical_json_sha256

_KEY = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*){0,7}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class MemoryKind(str, Enum):
    PROFILE = "profile"
    PREFERENCE = "preference"
    DECISION = "decision"
    TASK = "task"
    EPISODIC = "episodic"


class MemoryScope(str, Enum):
    PROFILE = "profile"
    SESSION = "session"


class MemoryStatus(str, Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"
    EXPIRED = "expired"


class MemoryEventType(str, Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


@dataclass(frozen=True, slots=True)
class MemorySource:
    session_id: str
    turn_id: str
    run_id: str
    quote: str

    def __post_init__(self) -> None:
        if any(not value or value.strip() != value or "\x00" in value for value in self.identities):
            raise ValueError("memory source identities must be canonical")
        if not self.quote or self.quote.strip() != self.quote or len(self.quote.encode("utf-8")) > 4096:
            raise ValueError("memory source quote must be bounded canonical text")

    @property
    def identities(self) -> tuple[str, str, str]:
        return self.session_id, self.turn_id, self.run_id


@dataclass(frozen=True, slots=True)
class MemoryItem:
    memory_id: str
    workspace_id: str
    profile_id: str
    session_id: str | None
    key: str
    kind: MemoryKind
    scope: MemoryScope
    status: MemoryStatus
    content: str
    pinned: bool
    importance: int
    source: MemorySource
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    supersedes_id: str | None
    content_hash: str
    revision: int

    def __post_init__(self) -> None:
        identities = (self.memory_id, self.workspace_id, self.profile_id)
        if any(not value or value.strip() != value or "\x00" in value for value in identities):
            raise ValueError("memory identities must be canonical")
        if _KEY.fullmatch(self.key) is None:
            raise ValueError("memory key must be bounded lowercase dotted syntax")
        if not self.content or self.content.strip() != self.content or "\x00" in self.content:
            raise ValueError("memory content must be canonical non-empty text")
        if len(self.content.encode("utf-8")) > 16 * 1024:
            raise ValueError("memory content exceeds 16 KiB")
        if type(self.pinned) is not bool or type(self.importance) is not int or not 1 <= self.importance <= 5:
            raise ValueError("memory pin and importance values are invalid")
        if self.scope is MemoryScope.SESSION:
            if self.session_id != self.source.session_id:
                raise ValueError("session memory must be bound to its source Session")
        elif self.session_id is not None:
            raise ValueError("profile memory must not carry a Session scope")
        if self.revision < 1:
            raise ValueError("memory revision must be positive")
        for value in (self.created_at, self.updated_at, self.expires_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("memory timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("memory update cannot precede creation")
        if _HASH.fullmatch(self.content_hash) is None or self.content_hash != self.expected_hash:
            raise ValueError("memory content hash is invalid")

    @property
    def expected_hash(self) -> str:
        return memory_content_hash(
            workspace_id=self.workspace_id,
            profile_id=self.profile_id,
            session_id=self.session_id,
            key=self.key,
            kind=self.kind,
            scope=self.scope,
            content=self.content,
            source=self.source,
        )

    def visible_at(self, now: datetime, *, profile_id: str, session_id: str) -> bool:
        if self.profile_id != profile_id or self.status is not MemoryStatus.CONFIRMED:
            return False
        if self.expires_at is not None and self.expires_at <= now:
            return False
        return self.scope is MemoryScope.PROFILE or self.session_id == session_id

    def transition(
        self,
        status: MemoryStatus,
        *,
        now: datetime,
        supersedes_id: str | None = None,
    ) -> MemoryItem:
        allowed = {
            MemoryStatus.PROPOSED: {MemoryStatus.CONFIRMED, MemoryStatus.FORGOTTEN},
            MemoryStatus.CONFIRMED: {MemoryStatus.SUPERSEDED, MemoryStatus.FORGOTTEN, MemoryStatus.EXPIRED},
            MemoryStatus.SUPERSEDED: set(),
            MemoryStatus.FORGOTTEN: set(),
            MemoryStatus.EXPIRED: set(),
        }
        if status not in allowed[self.status]:
            raise ValueError(f"invalid memory transition: {self.status.value} -> {status.value}")
        return replace(
            self,
            status=status,
            updated_at=now,
            supersedes_id=supersedes_id if supersedes_id is not None else self.supersedes_id,
            revision=self.revision + 1,
        )


@dataclass(frozen=True, slots=True)
class MemoryEvent:
    event_id: str
    memory_id: str
    workspace_id: str
    profile_id: str
    event_type: MemoryEventType
    status: MemoryStatus
    occurred_at: datetime
    actor_run_id: str
    source_turn_id: str
    revision: int

    def __post_init__(self) -> None:
        identities = (
            self.event_id,
            self.memory_id,
            self.workspace_id,
            self.profile_id,
            self.actor_run_id,
            self.source_turn_id,
        )
        if any(not value or value.strip() != value or "\x00" in value for value in identities):
            raise ValueError("memory event identities must be canonical")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None or self.revision < 1:
            raise ValueError("memory event time/revision is invalid")


def memory_content_hash(
    *,
    workspace_id: str,
    profile_id: str,
    session_id: str | None,
    key: str,
    kind: MemoryKind,
    scope: MemoryScope,
    content: str,
    source: MemorySource,
) -> str:
    return canonical_json_sha256(
        {
            "workspaceId": workspace_id,
            "profileId": profile_id,
            "sessionId": session_id,
            "key": key,
            "kind": kind.value,
            "scope": scope.value,
            "content": content,
            "source": {
                "sessionId": source.session_id,
                "turnId": source.turn_id,
                "runId": source.run_id,
                "quote": source.quote,
            },
        }
    )


def memory_item_to_json(item: MemoryItem) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "memoryId": item.memory_id,
        "workspaceId": item.workspace_id,
        "profileId": item.profile_id,
        "sessionId": item.session_id,
        "key": item.key,
        "kind": item.kind.value,
        "scope": item.scope.value,
        "status": item.status.value,
        "content": item.content,
        "pinned": item.pinned,
        "importance": item.importance,
        "source": {
            "sessionId": item.source.session_id,
            "turnId": item.source.turn_id,
            "runId": item.source.run_id,
            "quote": item.source.quote,
        },
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
        "expiresAt": None if item.expires_at is None else item.expires_at.isoformat(),
        "supersedesId": item.supersedes_id,
        "contentHash": item.content_hash,
        "revision": item.revision,
    }


def memory_item_from_json(value: Any) -> MemoryItem:
    raw = _object(value, "memory item")
    expected = {
        "schemaVersion",
        "memoryId",
        "workspaceId",
        "profileId",
        "sessionId",
        "key",
        "kind",
        "scope",
        "status",
        "content",
        "pinned",
        "importance",
        "source",
        "createdAt",
        "updatedAt",
        "expiresAt",
        "supersedesId",
        "contentHash",
        "revision",
    }
    if set(raw) != expected or raw["schemaVersion"] != 1:
        raise ValueError("memory item schema is incompatible")
    source = _object(raw["source"], "memory source")
    if set(source) != {"sessionId", "turnId", "runId", "quote"}:
        raise ValueError("memory source schema is incompatible")
    return MemoryItem(
        memory_id=_string(raw["memoryId"], "memoryId"),
        workspace_id=_string(raw["workspaceId"], "workspaceId"),
        profile_id=_string(raw["profileId"], "profileId"),
        session_id=_optional_string(raw["sessionId"], "sessionId"),
        key=_string(raw["key"], "key"),
        kind=MemoryKind(_string(raw["kind"], "kind")),
        scope=MemoryScope(_string(raw["scope"], "scope")),
        status=MemoryStatus(_string(raw["status"], "status")),
        content=_string(raw["content"], "content"),
        pinned=_boolean(raw["pinned"], "pinned"),
        importance=_integer(raw["importance"], "importance"),
        source=MemorySource(
            _string(source["sessionId"], "source.sessionId"),
            _string(source["turnId"], "source.turnId"),
            _string(source["runId"], "source.runId"),
            _string(source["quote"], "source.quote"),
        ),
        created_at=_datetime(raw["createdAt"], "createdAt"),
        updated_at=_datetime(raw["updatedAt"], "updatedAt"),
        expires_at=_optional_datetime(raw["expiresAt"], "expiresAt"),
        supersedes_id=_optional_string(raw["supersedesId"], "supersedesId"),
        content_hash=_string(raw["contentHash"], "contentHash"),
        revision=_integer(raw["revision"], "revision"),
    )


def memory_event_to_json(event: MemoryEvent) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "eventId": event.event_id,
        "memoryId": event.memory_id,
        "workspaceId": event.workspace_id,
        "profileId": event.profile_id,
        "eventType": event.event_type.value,
        "status": event.status.value,
        "occurredAt": event.occurred_at.isoformat(),
        "actorRunId": event.actor_run_id,
        "sourceTurnId": event.source_turn_id,
        "revision": event.revision,
    }


def memory_event_from_json(value: Any) -> MemoryEvent:
    raw = _object(value, "memory event")
    expected = {
        "schemaVersion",
        "eventId",
        "memoryId",
        "workspaceId",
        "profileId",
        "eventType",
        "status",
        "occurredAt",
        "actorRunId",
        "sourceTurnId",
        "revision",
    }
    if set(raw) != expected or raw["schemaVersion"] != 1:
        raise ValueError("memory event schema is incompatible")
    return MemoryEvent(
        event_id=_string(raw["eventId"], "eventId"),
        memory_id=_string(raw["memoryId"], "memoryId"),
        workspace_id=_string(raw["workspaceId"], "workspaceId"),
        profile_id=_string(raw["profileId"], "profileId"),
        event_type=MemoryEventType(_string(raw["eventType"], "eventType")),
        status=MemoryStatus(_string(raw["status"], "status")),
        occurred_at=_datetime(raw["occurredAt"], "occurredAt"),
        actor_run_id=_string(raw["actorRunId"], "actorRunId"),
        source_turn_id=_string(raw["sourceTurnId"], "sourceTurnId"),
        revision=_integer(raw["revision"], "revision"),
    )


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _datetime(value: Any, label: str) -> datetime:
    raw = _string(value, label)
    try:
        result = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return result


def _optional_datetime(value: Any, label: str) -> datetime | None:
    return None if value is None else _datetime(value, label)


__all__ = [
    "MemoryEvent",
    "MemoryEventType",
    "MemoryItem",
    "MemoryKind",
    "MemoryScope",
    "MemorySource",
    "MemoryStatus",
    "memory_content_hash",
    "memory_event_from_json",
    "memory_event_to_json",
    "memory_item_from_json",
    "memory_item_to_json",
]
