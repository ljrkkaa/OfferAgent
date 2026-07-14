"""Subagent scheduling and result value objects."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json
from offeragent_harness.permissions import CapabilityScope, PermissionMode
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools.canonical import canonical_json_sha256

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MESSAGE_ID = re.compile(r"^msg_[A-Za-z0-9][A-Za-z0-9_-]{0,123}$")


class ContextForkMode(str, Enum):
    NONE = "none"
    SUMMARY = "summary"
    SELECTED = "selected"
    FULL = "full"


class SubagentLifetime(str, Enum):
    PARENT = "parent"
    TURN = "turn"
    SESSION = "session"


class SubagentRunStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_CHILDREN = "waiting_children"
    COMPLETING = "completing"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    ORPHANED = "orphaned"

    @property
    def terminal(self) -> bool:
        return self in {
            SubagentRunStatus.COMPLETED,
            SubagentRunStatus.CANCELLED,
            SubagentRunStatus.FAILED,
            SubagentRunStatus.INTERRUPTED,
        }


class MailboxMode(str, Enum):
    APPEND = "append"
    STEER = "steer"


class WaitMode(str, Enum):
    ANY = "any"
    ALL = "all"


class ExecutionPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"

    @property
    def weight(self) -> int:
        return {ExecutionPriority.LOW: 0, ExecutionPriority.NORMAL: 1, ExecutionPriority.HIGH: 2}[self]


@dataclass(frozen=True)
class AgentBudget:
    input_tokens: int
    output_tokens: int
    model_calls: int
    tool_calls: int
    wall_time_seconds: float
    artifact_bytes: int
    child_count: int
    cost_micros: int = 0

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.model_calls,
                self.tool_calls,
                self.wall_time_seconds,
                self.artifact_bytes,
                self.child_count,
                self.cost_micros,
            )
        ):
            raise ValueError("agent budgets cannot be negative")

    def fits_within(self, ceiling: AgentBudget) -> bool:
        return all(
            value <= maximum
            for value, maximum in zip(
                self.as_tuple(),
                ceiling.as_tuple(),
                strict=True,
            )
        )

    def as_tuple(self) -> tuple[int | float, ...]:
        return (
            self.input_tokens,
            self.output_tokens,
            self.model_calls,
            self.tool_calls,
            self.wall_time_seconds,
            self.artifact_bytes,
            self.child_count,
            self.cost_micros,
        )


@dataclass(frozen=True, slots=True)
class AgentUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    wall_time_seconds: float = 0
    artifact_bytes: int = 0
    child_count: int = 0
    cost_micros: int = 0

    def __post_init__(self) -> None:
        if any(value < 0 for value in self.as_tuple()):
            raise ValueError("agent usage cannot be negative")

    def as_tuple(self) -> tuple[int | float, ...]:
        return (
            self.input_tokens,
            self.output_tokens,
            self.model_calls,
            self.tool_calls,
            self.wall_time_seconds,
            self.artifact_bytes,
            self.child_count,
            self.cost_micros,
        )

    def fits_within(self, budget: AgentBudget) -> bool:
        return all(value <= maximum for value, maximum in zip(self.as_tuple(), budget.as_tuple(), strict=True))


@dataclass(frozen=True)
class SubagentSpawnRequest:
    spawn_call_id: str
    parent_lineage: AgentLineage
    child_run_id: str
    task: str
    profile: str
    context_mode: ContextForkMode
    selected_message_ids: tuple[str, ...]
    selected_artifact_ids: tuple[str, ...]
    requested_scope: CapabilityScope
    budget: AgentBudget
    lifetime: SubagentLifetime
    deadline_at: datetime
    requested_permission_mode: PermissionMode = PermissionMode.READ_ONLY
    requested_tool_versions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    requested_tool_constraints: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    priority: ExecutionPriority = ExecutionPriority.NORMAL

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("subagent task must not be empty")
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise ValueError("subagent deadline must be timezone-aware")
        versions = dict(self.requested_tool_versions)
        constraints = dict(self.requested_tool_constraints)
        if set(versions) - self.requested_scope.allowed_tools or set(constraints) - self.requested_scope.allowed_tools:
            raise ValueError("requested tool versions/constraints exceed the requested tool allowlist")
        frozen_constraints = freeze_json(constraints)
        if not isinstance(frozen_constraints, FrozenJsonObject):
            raise TypeError("requested tool constraints must be a JSON object")
        object.__setattr__(self, "requested_tool_versions", freeze_json(versions))
        object.__setattr__(self, "requested_tool_constraints", frozen_constraints)


@dataclass(frozen=True)
class SubagentResult:
    run_id: str
    status: str
    summary: str
    findings: tuple[Mapping[str, Any], ...]
    evidence: tuple[Mapping[str, Any], ...]
    artifact_ids: tuple[str, ...]
    proposed_actions: tuple[Mapping[str, Any], ...]
    unresolved_questions: tuple[str, ...]
    usage: Mapping[str, Any]
    error: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(freeze_json(item) for item in self.findings))
        object.__setattr__(self, "evidence", tuple(freeze_json(item) for item in self.evidence))
        object.__setattr__(self, "proposed_actions", tuple(freeze_json(item) for item in self.proposed_actions))
        usage = freeze_json(self.usage)
        if not isinstance(usage, FrozenJsonObject):
            raise TypeError("subagent usage must be a JSON object")
        object.__setattr__(self, "usage", usage)
        if self.error is not None:
            error = freeze_json(self.error)
            if not isinstance(error, FrozenJsonObject):
                raise TypeError("subagent error must be a JSON object")
            object.__setattr__(self, "error", error)


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    snapshot_id: str
    workspace_id: str
    parent_run_id: str
    mode: ContextForkMode
    content: Mapping[str, Any]
    content_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.workspace_id or not self.parent_run_id:
            raise ValueError("context snapshot identity is invalid")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("context snapshot timestamp must be timezone-aware")
        frozen = freeze_json(self.content)
        if not isinstance(frozen, FrozenJsonObject):
            raise TypeError("context snapshot content must be a JSON object")
        if canonical_json_sha256(frozen) != self.content_hash:
            raise ValueError("context snapshot hash does not match content")
        object.__setattr__(self, "content", frozen)


@dataclass(frozen=True, slots=True)
class EffectiveToolScope:
    allowed_versions: Mapping[str, tuple[str, ...]]
    argument_constraints: Mapping[str, Mapping[str, Any]]
    registry_snapshot_hash: str

    def __post_init__(self) -> None:
        if not self.registry_snapshot_hash.startswith("sha256:"):
            raise ValueError("effective Tool scope requires a Registry snapshot hash")
        versions = {name: tuple(items) for name, items in self.allowed_versions.items()}
        if any(not name or not items or len(items) != len(set(items)) for name, items in versions.items()):
            raise ValueError("effective Tool version scope is invalid")
        if set(self.argument_constraints) - set(versions):
            raise ValueError("Tool argument constraints require an allowed Tool")
        frozen_versions = freeze_json(versions)
        frozen_constraints = freeze_json(self.argument_constraints)
        if not isinstance(frozen_versions, FrozenJsonObject) or not isinstance(frozen_constraints, FrozenJsonObject):
            raise TypeError("effective Tool scope must be JSON objects")
        object.__setattr__(self, "allowed_versions", frozen_versions)
        object.__setattr__(self, "argument_constraints", frozen_constraints)


@dataclass(frozen=True, slots=True)
class SubagentRunRecord:
    run_id: str
    root_run_id: str
    parent_run_id: str
    ancestor_run_ids: tuple[str, ...]
    session_id: str
    turn_id: str
    workspace_id: str
    trace_id: str
    spawn_call_id: str
    agent_name: str
    agent_version: str
    task: str
    task_fingerprint: str
    depth: int
    lifetime: SubagentLifetime
    context_snapshot_id: str
    permission_mode: PermissionMode
    effective_scope: CapabilityScope
    tool_scope: EffectiveToolScope
    budget_limit: AgentBudget
    budget_used: AgentUsage
    deadline_at: datetime
    result_schema: Mapping[str, Any]
    status: SubagentRunStatus
    phase: str
    priority: ExecutionPriority
    created_at: datetime
    updated_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    safe_checkpoint: bool = False
    result_artifact_id: str | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        identities = (
            self.run_id,
            self.root_run_id,
            self.parent_run_id,
            self.session_id,
            self.turn_id,
            self.workspace_id,
            self.trace_id,
            self.spawn_call_id,
            self.agent_name,
            self.agent_version,
            self.context_snapshot_id,
        )
        if any(not value for value in identities) or self.depth < 1 or self.revision < 1:
            raise ValueError("Subagent Run identity/revision is invalid")
        if (
            len(self.ancestor_run_ids) != self.depth
            or self.ancestor_run_ids[0] != self.root_run_id
            or self.ancestor_run_ids[-1] != self.parent_run_id
            or len(self.ancestor_run_ids) != len(set(self.ancestor_run_ids))
        ):
            raise ValueError("Subagent Run ancestor lineage is invalid")
        if not self.task.strip() or _RUN_ID.fullmatch(self.run_id) is None:
            raise ValueError("Subagent Run task/run ID is invalid")
        expected_fingerprint = f"sha256:{hashlib.sha256(self.task.strip().encode('utf-8')).hexdigest()}"
        if self.task_fingerprint != expected_fingerprint:
            raise ValueError("Subagent task fingerprint does not match task")
        for value in (self.created_at, self.updated_at, self.deadline_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Subagent Run timestamps must be timezone-aware")
        if self.lease_expires_at is not None and (
            self.lease_expires_at.tzinfo is None or self.lease_expires_at.utcoffset() is None
        ):
            raise ValueError("Subagent Run lease expiry must be timezone-aware")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("Subagent lease owner/expiry must be present together")
        if not self.budget_used.fits_within(self.budget_limit):
            raise ValueError("Subagent usage exceeds its reserved budget")
        schema = freeze_json(self.result_schema)
        if not isinstance(schema, FrozenJsonObject):
            raise TypeError("Subagent result schema must be a JSON object")
        object.__setattr__(self, "result_schema", schema)

    @property
    def lineage(self) -> AgentLineage:
        return AgentLineage(
            self.root_run_id,
            self.run_id,
            self.parent_run_id,
            self.ancestor_run_ids,
            self.depth,
            self.agent_name,
        )


@dataclass(frozen=True, slots=True)
class AgentSpawnCommand:
    parent_run_id: str
    spawn_call_id: str
    task: str
    profile: str
    context_mode: ContextForkMode
    selected_message_ids: tuple[str, ...]
    selected_artifact_ids: tuple[str, ...]
    requested_scope: CapabilityScope
    requested_permission_mode: PermissionMode
    requested_tool_versions: Mapping[str, tuple[str, ...]]
    requested_tool_constraints: Mapping[str, Mapping[str, Any]]
    budget: AgentBudget
    lifetime: SubagentLifetime
    priority: ExecutionPriority = ExecutionPriority.NORMAL
    deadline_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.parent_run_id or not self.spawn_call_id or not self.task.strip() or not self.profile:
            raise ValueError("agent.spawn command identity/task/profile is invalid")
        if len(self.task) > 32_768 or len(self.selected_message_ids) > 256 or len(self.selected_artifact_ids) > 256:
            raise ValueError("agent.spawn command exceeds cardinality limits")
        if self.deadline_at is not None and (self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None):
            raise ValueError("agent.spawn deadline must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SubagentHandle:
    run_id: str
    parent_run_id: str
    status: SubagentRunStatus
    queued_at: datetime


@dataclass(frozen=True, slots=True)
class AgentSendCommand:
    requester_run_id: str
    run_id: str
    mode: MailboxMode
    message: str
    artifact_ids: tuple[str, ...]
    message_id: str

    def __post_init__(self) -> None:
        if (
            not self.requester_run_id
            or not self.run_id
            or not self.message.strip()
            or _MESSAGE_ID.fullmatch(self.message_id) is None
        ):
            raise ValueError("agent.send command is invalid")
        if len(self.message) > 262_144 or len(self.artifact_ids) > 256:
            raise ValueError("agent.send command exceeds limits")


@dataclass(frozen=True, slots=True)
class MailboxReceipt:
    run_id: str
    message_id: str
    sequence: int
    duplicate: bool


@dataclass(frozen=True, slots=True)
class AgentWaitCommand:
    requester_run_id: str
    run_ids: tuple[str, ...]
    mode: WaitMode
    timeout_ms: int

    def __post_init__(self) -> None:
        if not self.requester_run_id or not self.run_ids or len(self.run_ids) > 256:
            raise ValueError("agent.wait command requires 1..256 Run IDs")
        if len(self.run_ids) != len(set(self.run_ids)) or not 0 <= self.timeout_ms <= 300_000:
            raise ValueError("agent.wait Run IDs/timeout are invalid")


@dataclass(frozen=True, slots=True)
class SubagentWaitResult:
    completed_run_ids: tuple[str, ...]
    pending_run_ids: tuple[str, ...]
    timed_out: bool


@dataclass(frozen=True, slots=True)
class SubagentStatusSnapshot:
    run_id: str
    root_run_id: str
    parent_run_id: str
    agent_name: str
    status: SubagentRunStatus
    phase: str
    depth: int
    budget_limit: AgentBudget
    budget_used: AgentUsage
    deadline_at: datetime
    child_run_ids: tuple[str, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AgentCancelCommand:
    requester_run_id: str
    run_id: str
    reason: str
    cascade: bool = True

    def __post_init__(self) -> None:
        if not self.requester_run_id or not self.run_id or not self.reason.strip() or len(self.reason) > 4_096:
            raise ValueError("agent.cancel command is invalid")


@dataclass(frozen=True, slots=True)
class SubagentCancelReceipt:
    run_id: str
    accepted: bool
    descendant_run_ids: tuple[str, ...]


__all__ = [
    "AgentBudget",
    "AgentCancelCommand",
    "AgentSendCommand",
    "AgentSpawnCommand",
    "AgentUsage",
    "ContextForkMode",
    "ContextSnapshot",
    "EffectiveToolScope",
    "ExecutionPriority",
    "MailboxMode",
    "MailboxReceipt",
    "SubagentCancelReceipt",
    "SubagentHandle",
    "SubagentLifetime",
    "SubagentResult",
    "SubagentRunRecord",
    "SubagentRunStatus",
    "SubagentSpawnRequest",
    "SubagentStatusSnapshot",
    "SubagentWaitResult",
    "WaitMode",
]
