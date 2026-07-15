"""Persistent Session/Turn/Run identities and immutable snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json


class SessionStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class TurnStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class RunKind(str, Enum):
    ROOT = "root"
    SUBAGENT = "subagent"


class RunStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    STARTING = "starting"
    LOADING_CONTEXT = "loading_context"
    SELECTING_MEMORY = "selecting_memory"
    PLANNING = "planning"
    VALIDATING_CALLS = "validating_calls"
    CHECKING_POLICY = "checking_policy"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING_TOOLS = "executing_tools"
    RECORDING_RESULTS = "recording_results"
    WAITING_TOOL = "waiting_tool"
    WAITING_CHILDREN = "waiting_children"
    COMPOSING = "composing"
    PERSISTING = "persisting"
    COMPLETING = "completing"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    ORPHANED = "orphaned"

    @property
    def is_terminal(self) -> bool:
        return self in {
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.INTERRUPTED,
            RunStatus.ORPHANED,
        }


class TerminationReason(str, Enum):
    COMPLETED = "completed"
    CANCELLED_BY_USER = "cancelled_by_user"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    APPROVAL_EXPIRED = "approval_expired"
    RUNTIME_INTERRUPTED = "runtime_interrupted"


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class AgentLineage:
    root_run_id: str
    run_id: str
    parent_run_id: str | None
    ancestor_run_ids: tuple[str, ...]
    depth: int
    agent_name: str

    def __post_init__(self) -> None:
        if not self.root_run_id or not self.run_id or not self.agent_name:
            raise ValueError("lineage identity fields must not be empty")
        if self.depth != len(self.ancestor_run_ids):
            raise ValueError("lineage depth must equal the ancestor count")
        if self.depth == 0:
            if self.parent_run_id is not None or self.ancestor_run_ids or self.root_run_id != self.run_id:
                raise ValueError("root lineage must reference itself and have no parent")
        else:
            if self.parent_run_id != self.ancestor_run_ids[-1]:
                raise ValueError("parent_run_id must be the nearest ancestor")
            if self.ancestor_run_ids[0] != self.root_run_id:
                raise ValueError("first ancestor must be root_run_id")
            if self.run_id in self.ancestor_run_ids:
                raise ValueError("lineage cannot contain a cycle")

    @classmethod
    def root(cls, run_id: str, agent_name: str = "root") -> AgentLineage:
        return cls(run_id, run_id, None, (), 0, agent_name)

    def child(self, run_id: str, agent_name: str) -> AgentLineage:
        return AgentLineage(
            root_run_id=self.root_run_id,
            run_id=run_id,
            parent_run_id=self.run_id,
            ancestor_run_ids=(*self.ancestor_run_ids, self.run_id),
            depth=self.depth + 1,
            agent_name=agent_name,
        )


@dataclass(frozen=True)
class Session:
    session_id: str
    workspace_id: str
    profile_id: str
    title: str
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    revision: int
    forked_from_session_id: str | None = None
    forked_from_turn_id: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.revision < 0:
            raise ValueError("revision cannot be negative")
        if (self.forked_from_session_id is None) != (self.forked_from_turn_id is None):
            raise ValueError("fork session and turn IDs must be provided together")


@dataclass(frozen=True)
class Turn:
    turn_id: str
    session_id: str
    ordinal: int
    status: TurnStatus
    input_blocks: tuple[Mapping[str, Any], ...]
    created_at: datetime
    updated_at: datetime
    revision: int = 1

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise ValueError("turn ordinal starts at 1")
        if self.revision < 1:
            raise ValueError("turn revision starts at 1")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        frozen_blocks = tuple(freeze_json(block) for block in self.input_blocks)
        if any(not isinstance(block, FrozenJsonObject) for block in frozen_blocks):
            raise TypeError("turn input blocks must be JSON objects")
        object.__setattr__(self, "input_blocks", frozen_blocks)


@dataclass(frozen=True)
class Run:
    run_id: str
    session_id: str
    turn_id: str
    workspace_id: str
    lineage: AgentLineage
    kind: RunKind
    status: RunStatus
    attempt: int
    event_sequence: int
    config_snapshot: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime
    deadline_at: datetime | None
    termination_reason: TerminationReason | None = None

    def __post_init__(self) -> None:
        if self.run_id != self.lineage.run_id:
            raise ValueError("run_id must match lineage.run_id")
        if (self.kind is RunKind.ROOT) != (self.lineage.depth == 0):
            raise ValueError("run kind and lineage depth disagree")
        if self.attempt < 1 or self.event_sequence < 0:
            raise ValueError("attempt starts at 1 and event sequence cannot be negative")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.deadline_at is not None:
            _require_aware(self.deadline_at, "deadline_at")
        if self.status.is_terminal != (self.termination_reason is not None):
            raise ValueError("terminal runs require exactly one termination reason")
        frozen = freeze_json(self.config_snapshot)
        if not isinstance(frozen, FrozenJsonObject):
            raise TypeError("config_snapshot must be a JSON object")
        object.__setattr__(self, "config_snapshot", frozen)


__all__ = [
    "AgentLineage",
    "Run",
    "RunKind",
    "RunStatus",
    "Session",
    "SessionStatus",
    "TerminationReason",
    "Turn",
    "TurnStatus",
]
