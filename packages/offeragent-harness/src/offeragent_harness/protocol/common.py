"""Reusable, typed protocol value objects shared by commands and events."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from ._base import JsonObject, WireModel
from .content import ArtifactRef, ContentBlock, FileRef, RelativeVaultPath, SourceRef
from .errors import ErrorEnvelope
from .ids import (
    ApprovalId,
    ArtifactId,
    Rfc3339DateTime,
    RunId,
    SessionId,
    Sha256Digest,
    ToolCallId,
    TurnId,
    WorkspaceId,
)


class PermissionMode(str, Enum):
    READ_ONLY = "read-only"
    NORMAL = "normal"
    TRUSTED_WORKSPACE = "trusted-workspace"
    PLAN = "plan"
    BYPASS = "bypass"


class ReasoningEffort(str, Enum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


class RunPhase(str, Enum):
    CREATED = "created"
    LOADING_CONTEXT = "loading_context"
    SELECTING_MEMORY = "selecting_memory"
    PLANNING = "planning"
    VALIDATING_CALLS = "validating_calls"
    CHECKING_POLICY = "checking_policy"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING_TOOLS = "executing_tools"
    RECORDING_RESULTS = "recording_results"
    WAITING_CHILDREN = "waiting_children"
    COMPOSING = "composing"
    PERSISTING = "persisting"
    CANCELLING = "cancelling"
    TERMINAL = "terminal"


class RunStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    WAITING_CHILDREN = "waiting_children"
    COMPLETED = "completed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    ORPHANED = "orphaned"


class TurnStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class TerminationReason(str, Enum):
    COMPLETED = "completed"
    CANCELLED_BY_USER = "cancelled_by_user"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    APPROVAL_EXPIRED = "approval_expired"
    RUNTIME_INTERRUPTED = "runtime_interrupted"
    PROVIDER_UNREACHABLE = "provider_unreachable"


class ToolRisk(str, Enum):
    READ = "read"
    NETWORK = "network"
    WRITE = "write"
    EXECUTE = "execute"
    DESTRUCTIVE = "destructive"
    EXTERNAL_PATH = "external_path"
    SECRET_ACCESS = "secret_access"


class ToolCallStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CONFLICT = "conflict"
    PARTIAL = "partial"
    UNKNOWN_OUTCOME = "unknown_outcome"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalDecision(str, Enum):
    DENY = "deny"
    ALLOW_ONCE = "allow_once"
    ALLOW_RUN = "allow_run"
    ALLOW_SESSION = "allow_session"
    ALLOW_PERSISTENT = "allow_persistent"


class ApprovalScope(str, Enum):
    ONCE = "once"
    RUN = "run"
    SESSION = "session"
    PERSISTENT = "persistent"


class BudgetSnapshot(WireModel):
    max_model_rounds: int = Field(ge=1, le=1024)
    max_tool_calls: int = Field(ge=0, le=100_000)
    max_parallel_reads: int = Field(ge=1, le=256)
    max_wall_time_ms: int = Field(ge=1, le=604_800_000)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_cost_micros: int | None = Field(default=None, ge=0)
    max_artifact_bytes: int = Field(ge=0)


class RunConfigSnapshot(WireModel):
    provider: str = Field(default="codex", min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    reasoning_effort: ReasoningEffort = ReasoningEffort.MEDIUM
    permission_mode: PermissionMode = PermissionMode.NORMAL
    budgets: BudgetSnapshot | None = None
    enabled_skills: list[str] = Field(default_factory=list, max_length=256)


class EditorSelection(WireModel):
    text: str = Field(max_length=262_144)
    anchor_offset: int = Field(ge=0)
    head_offset: int = Field(ge=0)


class ClientMetadataSnapshot(WireModel):
    frontmatter: JsonObject = Field(default_factory=dict, max_length=256)
    tags: list[str] = Field(default_factory=list, max_length=1024)
    links: list[RelativeVaultPath] = Field(default_factory=list, max_length=2048)
    unresolved_links: list[str] = Field(default_factory=list, max_length=2048)


class ClientBacklinkSnapshot(WireModel):
    path: RelativeVaultPath
    count: int = Field(ge=1, le=1_000_000)


class ClientContextSnapshot(WireModel):
    active_file: RelativeVaultPath | None = None
    active_file_hash: Sha256Digest | None = None
    active_file_revision: int | None = Field(default=None, ge=0)
    selection: EditorSelection | None = None
    selection_revision: int | None = Field(default=None, ge=0)
    cursor_offset: int | None = Field(default=None, ge=0)
    has_unsaved_changes: bool = False
    metadata_cache_revision: int | None = Field(default=None, ge=0)
    metadata: ClientMetadataSnapshot | None = None
    backlinks: list[ClientBacklinkSnapshot] | None = Field(default=None, max_length=2048)


class UsageSnapshot(WireModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    cost_micros: int | None = Field(default=None, ge=0)
    wall_time_ms: int = Field(default=0, ge=0)


class SideEffect(WireModel):
    kind: Literal["file_created", "file_modified", "file_renamed", "file_trashed", "process", "network"]
    resource: str = Field(min_length=1, max_length=4096)
    before_hash: Sha256Digest | None = None
    after_hash: Sha256Digest | None = None
    confirmed: bool


class ToolCallDescriptor(WireModel):
    tool_call_id: ToolCallId
    name: str = Field(min_length=3, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    version: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    arguments: JsonObject
    args_hash: Sha256Digest
    idempotency_key: str = Field(min_length=1, max_length=256)
    risk: ToolRisk
    reason: str | None = Field(default=None, max_length=4096)
    agent_lineage: list[RunId] = Field(min_length=1, max_length=16)


class ToolResultDescriptor(WireModel):
    tool_call_id: ToolCallId
    status: ToolCallStatus
    summary: str = Field(min_length=1, max_length=8192)
    data: JsonObject = Field(default_factory=dict)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list, max_length=256)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=512)
    side_effects: list[SideEffect] = Field(default_factory=list, max_length=256)
    retryable: bool = False
    error: ErrorEnvelope | None = None

    @model_validator(mode="after")
    def _error_matches_status(self) -> ToolResultDescriptor:
        failed = self.status in {
            ToolCallStatus.FAILED,
            ToolCallStatus.TIMED_OUT,
            ToolCallStatus.UNKNOWN_OUTCOME,
        }
        if failed and self.error is None:
            raise ValueError("failed/timed-out/unknown tool results require an error")
        if self.status == ToolCallStatus.SUCCEEDED and self.error is not None:
            raise ValueError("a succeeded tool result cannot contain an error")
        return self


class ApprovalDescriptor(WireModel):
    approval_id: ApprovalId
    status: ApprovalStatus
    tool_call: ToolCallDescriptor
    workspace_id: WorkspaceId
    run_id: RunId
    expected_state_hash: Sha256Digest | None = None
    expires_at: Rfc3339DateTime
    diff_artifact: ArtifactRef | None = None
    include_descendants: bool = False


class SessionSummary(WireModel):
    session_id: SessionId
    workspace_id: WorkspaceId
    title: str = Field(min_length=1, max_length=512)
    created_at: Rfc3339DateTime
    updated_at: Rfc3339DateTime
    active_run_id: RunId | None = None
    turn_count: int = Field(ge=0)
    deleted: bool = False


class RunSnapshot(WireModel):
    run_id: RunId
    root_run_id: RunId
    parent_run_id: RunId | None = None
    session_id: SessionId
    turn_id: TurnId
    status: RunStatus
    phase: RunPhase
    agent_name: str = Field(default="root", min_length=1, max_length=128)
    depth: int = Field(default=0, ge=0, le=32)
    started_at: Rfc3339DateTime | None = None
    completed_at: Rfc3339DateTime | None = None
    last_sequence: int = Field(default=0, ge=0)
    usage: UsageSnapshot
    termination_reason: TerminationReason | None = None
    error: ErrorEnvelope | None = None


class TurnSnapshot(WireModel):
    turn_id: TurnId
    session_id: SessionId
    status: TurnStatus
    input: list[ContentBlock] = Field(min_length=1, max_length=256)
    runs: list[RunSnapshot] = Field(min_length=1, max_length=128)
    selected_run_id: RunId
    assistant_content: list[ContentBlock] = Field(default_factory=list, max_length=256)
    created_at: Rfc3339DateTime
    updated_at: Rfc3339DateTime


class Finding(WireModel):
    title: str = Field(min_length=1, max_length=512)
    summary: str = Field(min_length=1, max_length=8192)
    evidence: list[SourceRef] = Field(default_factory=list, max_length=256)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ProposedAction(WireModel):
    description: str = Field(min_length=1, max_length=4096)
    artifact_id: ArtifactId
    target_files: list[FileRef] = Field(default_factory=list, max_length=256)


class SubagentResult(WireModel):
    run_id: RunId
    status: RunStatus
    summary: str = Field(min_length=1, max_length=16_384)
    findings: list[Finding] = Field(default_factory=list, max_length=512)
    evidence: list[JsonObject] = Field(default_factory=list, max_length=512)
    artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=256)
    proposed_actions: list[ProposedAction] = Field(default_factory=list, max_length=256)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=256)
    usage: UsageSnapshot
    error: ErrorEnvelope | None = None


__all__ = [
    "ApprovalDecision",
    "ApprovalDescriptor",
    "ApprovalScope",
    "ApprovalStatus",
    "BudgetSnapshot",
    "ClientBacklinkSnapshot",
    "ClientContextSnapshot",
    "ClientMetadataSnapshot",
    "EditorSelection",
    "Finding",
    "PermissionMode",
    "ProposedAction",
    "ReasoningEffort",
    "RunConfigSnapshot",
    "RunPhase",
    "RunSnapshot",
    "RunStatus",
    "SessionSummary",
    "SideEffect",
    "SubagentResult",
    "TerminationReason",
    "ToolCallDescriptor",
    "ToolCallStatus",
    "ToolResultDescriptor",
    "ToolRisk",
    "TurnSnapshot",
    "TurnStatus",
    "UsageSnapshot",
]
