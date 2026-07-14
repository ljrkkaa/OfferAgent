"""Typed, replayable semantic events emitted only by the Harness Core."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, Protocol

from pydantic import Field, JsonValue, ValidationError, model_validator
from typing_extensions import TypeAliasType

from offeragent_harness.foundation import MAX_ARTIFACT_REFERENCES, MAX_AUDIT_EFFECTS, MAX_SOURCE_REFERENCES

from ._base import JsonObject, WireModel, validate_wire
from .capabilities import CapabilityName
from .common import (
    ApprovalDecision,
    ApprovalDescriptor,
    ApprovalScope,
    BudgetSnapshot,
    ClientContextSnapshot,
    RunConfigSnapshot,
    RunPhase,
    RunStatus,
    SessionSummary,
    SubagentResult,
    TerminationReason,
    ToolCallDescriptor,
    ToolCallStatus,
    ToolResultDescriptor,
    UsageSnapshot,
)
from .content import ArtifactRef, ContentBlock, SourceRef
from .errors import ErrorEnvelope
from .ids import (
    ApprovalId,
    ArtifactId,
    CompactBoundaryId,
    EventId,
    MessageId,
    ProtocolVersion,
    Rfc3339DateTime,
    RunId,
    SchemaVersion,
    SessionId,
    Sha256Digest,
    ToolCallId,
    TraceId,
    TurnId,
    WorkspaceId,
)


class EventType(str, Enum):
    TURN_STARTED = "turn.started"
    PHASE_CHANGED = "phase.changed"
    REASONING_SUMMARY = "reasoning.summary"
    ASSISTANT_DELTA = "assistant.delta"
    ASSISTANT_COMPLETED = "assistant.completed"
    TOOL_QUEUED = "tool.queued"
    TOOL_STARTED = "tool.started"
    TOOL_PROGRESS = "tool.progress"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_RESOLVED = "approval.resolved"
    APPROVAL_EXPIRED = "approval.expired"
    REFERENCES_UPDATED = "references.updated"
    ARTIFACT_CREATED = "artifact.created"
    USAGE_UPDATED = "usage.updated"
    CONTEXT_COMPACTED = "context.compacted"
    SESSION_UPDATED = "session.updated"
    SKILL_CATALOG_UPDATED = "skill.catalog_updated"
    SKILL_TRUST_CHANGED = "skill.trust_changed"
    SUBAGENT_QUEUED = "subagent.queued"
    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_PROGRESS = "subagent.progress"
    SUBAGENT_MESSAGE_RECEIVED = "subagent.message_received"
    SUBAGENT_WAITING = "subagent.waiting"
    SUBAGENT_RESULT_AVAILABLE = "subagent.result_available"
    SUBAGENT_COMPLETED = "subagent.completed"
    SUBAGENT_FAILED = "subagent.failed"
    SUBAGENT_CANCEL_REQUESTED = "subagent.cancel_requested"
    SUBAGENT_CANCELLED = "subagent.cancelled"
    SUBAGENT_INTERRUPTED = "subagent.interrupted"
    SUBAGENT_ORPHANED = "subagent.orphaned"
    SUBAGENT_RECOVERED = "subagent.recovered"
    TURN_COMPLETED = "turn.completed"
    TURN_CANCELLED = "turn.cancelled"
    TURN_FAILED = "turn.failed"
    RUNTIME_WARNING = "runtime.warning"
    # Durable Core facts which are required for deterministic recovery and
    # auditing, even though a UI may choose not to render them.
    MODEL_ATTEMPT = "model.attempt"
    MODEL_COMPOSITION_STARTED = "model.composition.started"
    TOOL_CALLS_ACCEPTED = "tool.calls.accepted"
    WRITE_OUTCOME_REQUIRED = "write.outcome_required"
    RUN_CONTINUATION_REQUIRED = "run.continuation_required"
    TURN_INTERRUPTED = "turn.interrupted"
    TURN_STEERED = "turn.steered"


class TurnStartedPayload(WireModel):
    input: list[ContentBlock] = Field(min_length=1, max_length=256)
    run_config: RunConfigSnapshot
    client_context: ClientContextSnapshot | None = None
    attempt: int = Field(default=1, ge=1, le=10_000)


class PhaseChangedPayload(WireModel):
    previous_phase: RunPhase | None = None
    phase: RunPhase
    reason: str | None = Field(default=None, max_length=4096)


class ReasoningSummaryPayload(WireModel):
    summary: str = Field(min_length=1, max_length=65_536)
    partial: bool = False


class AssistantDeltaPayload(WireModel):
    block_index: int = Field(ge=0, le=10_000)
    offset: int = Field(ge=0)
    delta: str = Field(min_length=1, max_length=65_536)


class AssistantCompletedPayload(WireModel):
    content: list[ContentBlock] = Field(min_length=1, max_length=256)
    finish_reason: Literal["stop", "length", "cancelled", "interrupted"]


class ToolQueuedPayload(WireModel):
    call: ToolCallDescriptor
    ordinal: int = Field(ge=0)


class ToolStartedPayload(WireModel):
    call: ToolCallDescriptor
    attempt: int = Field(default=1, ge=1, le=100)


class ToolProgressPayload(WireModel):
    tool_call_id: ToolCallId
    message: str = Field(min_length=1, max_length=4096)
    completed_units: int | None = Field(default=None, ge=0)
    total_units: int | None = Field(default=None, ge=1)
    artifact: ArtifactRef | None = None

    @model_validator(mode="after")
    def _progress_is_bounded(self) -> ToolProgressPayload:
        if self.completed_units is not None and self.total_units is None:
            raise ValueError("completedUnits requires totalUnits")
        if (
            self.completed_units is not None
            and self.total_units is not None
            and self.completed_units > self.total_units
        ):
            raise ValueError("completedUnits cannot exceed totalUnits")
        return self


class TurnSteeredPayload(WireModel):
    message_id: MessageId
    mode: Literal["append", "steer"]
    input: list[ContentBlock] = Field(min_length=1, max_length=256)
    apply_after_sequence: int = Field(ge=0)


class PersistedSideEffectFact(WireModel):
    kind: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=128)
    resource_id: str = Field(min_length=1, max_length=4096)
    before_state: JsonValue | None = None
    after_state: JsonValue | None = None
    metadata: JsonObject = Field(default_factory=dict)


class ToolCompletedPayload(WireModel):
    result: ToolResultDescriptor
    artifact_ids: list[ArtifactId] = Field(default_factory=list, max_length=MAX_ARTIFACT_REFERENCES)
    source_reference_ids: list[str] = Field(default_factory=list, max_length=MAX_SOURCE_REFERENCES)
    side_effect_facts: list[PersistedSideEffectFact] = Field(default_factory=list, max_length=MAX_AUDIT_EFFECTS)

    @model_validator(mode="after")
    def _completed_status_is_not_a_failure(self) -> ToolCompletedPayload:
        if self.result.status in {
            ToolCallStatus.FAILED,
            ToolCallStatus.TIMED_OUT,
            ToolCallStatus.UNKNOWN_OUTCOME,
        }:
            raise ValueError("tool.completed cannot carry a failed, timed-out, or unknown outcome")
        return self


class ToolFailedPayload(WireModel):
    result: ToolResultDescriptor
    artifact_ids: list[ArtifactId] = Field(default_factory=list, max_length=MAX_ARTIFACT_REFERENCES)
    source_reference_ids: list[str] = Field(default_factory=list, max_length=MAX_SOURCE_REFERENCES)
    side_effect_facts: list[PersistedSideEffectFact] = Field(default_factory=list, max_length=MAX_AUDIT_EFFECTS)

    @model_validator(mode="after")
    def _failed_status_is_a_failure(self) -> ToolFailedPayload:
        if self.result.status not in {
            ToolCallStatus.FAILED,
            ToolCallStatus.TIMED_OUT,
            ToolCallStatus.UNKNOWN_OUTCOME,
        }:
            raise ValueError("tool.failed requires a failed, timed-out, or unknown outcome")
        return self


class ApprovalRequiredPayload(WireModel):
    approval: ApprovalDescriptor
    explanation: str = Field(min_length=1, max_length=8192)
    diff_artifact_ids: list[ArtifactId] = Field(default_factory=list, max_length=256)


class ApprovalResolvedPayload(WireModel):
    approval_id: ApprovalId
    decision: ApprovalDecision
    scope: ApprovalScope
    resolved_at: Rfc3339DateTime
    resolved_by: Literal["user", "policy", "system"]
    status: Literal["approved", "denied", "cancelled"]
    resolver_id: str = Field(min_length=1, max_length=256)
    include_descendants: bool = False
    reason: str | None = Field(default=None, max_length=4096)


class ApprovalExpiredPayload(WireModel):
    approval_id: ApprovalId
    expired_at: Rfc3339DateTime
    reason: str = Field(min_length=1, max_length=4096)
    resolver_id: str = Field(min_length=1, max_length=256)


class ReferencesUpdatedPayload(WireModel):
    references: list[SourceRef] = Field(max_length=1024)
    replace: bool = False


class ArtifactCreatedPayload(WireModel):
    artifact: ArtifactRef
    owner_type: Literal["run", "tool", "approval", "subagent", "index"]
    owner_id: str = Field(min_length=1, max_length=128)


class UsageUpdatedPayload(WireModel):
    usage: UsageSnapshot
    scope: Literal["model_call", "run", "turn", "agent_tree"]


class ContextCompactedPayload(WireModel):
    boundary_id: CompactBoundaryId
    replaced_sequence_start: int = Field(ge=1)
    replaced_sequence_end: int = Field(ge=1)
    summary_artifact: ArtifactRef
    model: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _sequence_range_is_ordered(self) -> ContextCompactedPayload:
        if self.replaced_sequence_end < self.replaced_sequence_start:
            raise ValueError("replacedSequenceEnd must not precede replacedSequenceStart")
        return self


class SessionForkReferencePayload(WireModel):
    source_workspace_id: WorkspaceId
    source_session_id: SessionId
    source_turn_id: TurnId
    source_run_id: RunId
    source_event_sequence: int = Field(ge=1)
    artifact_link_ids: list[ArtifactId] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def _artifact_links_are_unique(self) -> SessionForkReferencePayload:
        if self.artifact_link_ids != sorted(set(self.artifact_link_ids)):
            raise ValueError("fork Artifact link IDs must be sorted and unique")
        return self


class SessionUpdatedPayload(WireModel):
    session: SessionSummary
    changed_fields: list[
        Literal[
            "created",
            "title",
            "activeRunId",
            "turnCount",
            "updatedAt",
            "deleted",
            "compaction",
            "forkReference",
        ]
    ] = Field(min_length=1, max_length=16)
    fork_reference: SessionForkReferencePayload | None = None

    @model_validator(mode="after")
    def _fork_reference_matches_changed_fields(self) -> SessionUpdatedPayload:
        if len(self.changed_fields) != len(set(self.changed_fields)):
            raise ValueError("session.updated changedFields must be unique")
        declares_fork = "forkReference" in self.changed_fields
        if declares_fork != (self.fork_reference is not None):
            raise ValueError("forkReference payload is required exactly when changedFields declares it")
        return self


class SubagentQueuedPayload(WireModel):
    child_run_id: RunId
    parent_run_id: RunId
    agent_name: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=32_768)
    depth: int = Field(ge=1, le=32)


class SubagentStartedPayload(WireModel):
    child_run_id: RunId
    parent_run_id: RunId
    agent_name: str = Field(min_length=1, max_length=128)
    context_mode: Literal["none", "summary", "selected", "full"]
    budget: BudgetSnapshot


class SubagentProgressPayload(WireModel):
    child_run_id: RunId
    phase: RunPhase
    message: str = Field(min_length=1, max_length=4096)
    completed_units: int | None = Field(default=None, ge=0)
    total_units: int | None = Field(default=None, ge=1)


class SubagentMessageReceivedPayload(WireModel):
    child_run_id: RunId
    message_id: MessageId
    mode: Literal["append", "steer"]
    content: list[ContentBlock] = Field(min_length=1, max_length=256)


class SubagentWaitingPayload(WireModel):
    child_run_id: RunId
    reason: Literal["tool", "approval", "children", "mailbox", "provider"]
    waiting_on_ids: list[str] = Field(default_factory=list, max_length=256)


class SubagentResultAvailablePayload(WireModel):
    child_run_id: RunId
    result_artifact_id: ArtifactId
    summary: str = Field(min_length=1, max_length=16_384)


class SubagentCompletedPayload(WireModel):
    result: SubagentResult

    @model_validator(mode="after")
    def _result_is_completed(self) -> SubagentCompletedPayload:
        if self.result.status != RunStatus.COMPLETED:
            raise ValueError("subagent.completed requires a completed result")
        return self


class SubagentFailedPayload(WireModel):
    child_run_id: RunId
    error: ErrorEnvelope
    usage: UsageSnapshot


class SubagentCancelRequestedPayload(WireModel):
    child_run_id: RunId
    reason: str = Field(min_length=1, max_length=4096)
    cascade: bool


class SubagentCancelledPayload(WireModel):
    child_run_id: RunId
    reason: str = Field(min_length=1, max_length=4096)
    usage: UsageSnapshot


class SubagentInterruptedPayload(WireModel):
    child_run_id: RunId
    error: ErrorEnvelope
    safe_checkpoint_available: bool


class SubagentOrphanedPayload(WireModel):
    child_run_id: RunId
    lease_expired_at: Rfc3339DateTime


class SubagentRecoveredPayload(WireModel):
    child_run_id: RunId
    previous_status: RunStatus
    checkpoint_sequence: int = Field(ge=0)

    @model_validator(mode="after")
    def _previous_status_was_recoverable(self) -> SubagentRecoveredPayload:
        if self.previous_status not in {RunStatus.INTERRUPTED, RunStatus.ORPHANED}:
            raise ValueError("subagent.recovered requires an interrupted or orphaned previous status")
        return self


class TurnCompletedPayload(WireModel):
    reason: TerminationReason
    assistant_content: list[ContentBlock] = Field(min_length=1, max_length=256)
    usage: UsageSnapshot


class TurnCancelledPayload(WireModel):
    reason: str = Field(min_length=1, max_length=4096)
    usage: UsageSnapshot
    partial_content: list[ContentBlock] = Field(default_factory=list, max_length=256)
    code: str | None = Field(default=None, min_length=1, max_length=128)


class TurnFailedPayload(WireModel):
    error: ErrorEnvelope
    usage: UsageSnapshot
    partial_content: list[ContentBlock] = Field(default_factory=list, max_length=256)


class TurnInterruptedPayload(WireModel):
    error: ErrorEnvelope
    usage: UsageSnapshot
    partial_content: list[ContentBlock] = Field(default_factory=list, max_length=256)
    safe_checkpoint_available: bool


class ModelAttemptUsage(WireModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cost: str | None = Field(default=None, pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
    currency: str | None = Field(default=None, min_length=3, max_length=16)


class ModelAttemptPayload(WireModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=256)
    repair_index: int = Field(ge=0, le=100)
    outcome: Literal["succeeded", "invalid", "failed"]
    error_code: str | None = Field(default=None, min_length=1, max_length=256)
    violations: list[str] = Field(default_factory=list, max_length=256)
    retry_of_request_id: str | None = Field(default=None, min_length=1, max_length=256)
    projection: Literal["normal", "overflow_references"] | None = None
    projection_hash: Sha256Digest | None = None
    omitted_context_ids: list[str] = Field(default_factory=list, max_length=4096)
    usage: ModelAttemptUsage | None = None

    @model_validator(mode="after")
    def _outcome_fields_are_consistent(self) -> ModelAttemptPayload:
        if self.outcome == "succeeded" and (self.error_code is not None or self.violations):
            raise ValueError("successful model attempts cannot carry failure details")
        if self.outcome == "invalid" and not self.violations:
            raise ValueError("invalid model attempts require schema violations")
        if self.outcome == "failed" and self.error_code is None:
            raise ValueError("failed model attempts require an errorCode")
        if self.projection is None:
            if self.projection_hash is not None or self.retry_of_request_id is not None or self.omitted_context_ids:
                raise ValueError("legacy model attempts cannot carry partial projection identity")
        elif self.projection_hash is None:
            raise ValueError("projected model attempts require projectionHash")
        if self.retry_of_request_id is not None and self.projection != "overflow_references":
            raise ValueError("context-overflow retries require overflow_references projection")
        _validate_context_ids(self.omitted_context_ids)
        return self


class ModelCompositionStartedPayload(WireModel):
    partial: bool
    request_id: str | None = Field(default=None, min_length=1, max_length=256)
    retry_of_request_id: str | None = Field(default=None, min_length=1, max_length=256)
    projection: Literal["overflow_references"] | None = None
    projection_hash: Sha256Digest | None = None
    omitted_context_ids: list[str] = Field(default_factory=list, max_length=4096)
    reason: Literal["context_overflow"] | None = None

    @model_validator(mode="after")
    def _retry_identity_is_complete(self) -> ModelCompositionStartedPayload:
        identity = (
            self.request_id,
            self.retry_of_request_id,
            self.projection,
            self.projection_hash,
            self.reason,
        )
        if any(value is not None for value in identity) and any(value is None for value in identity):
            raise ValueError("composition retry projection identity must be complete")
        if all(value is None for value in identity) and self.omitted_context_ids:
            raise ValueError("initial composition cannot carry retry omissions")
        _validate_context_ids(self.omitted_context_ids)
        return self


def _validate_context_ids(values: list[str]) -> None:
    if len(values) != len(set(values)) or any(not value or len(value) > 256 or "\x00" in value for value in values):
        raise ValueError("omittedContextIds must be unique bounded identifiers")


class PersistedToolCallLineage(WireModel):
    root_run_id: RunId
    run_id: RunId
    parent_run_id: RunId | None = None
    ancestor_run_ids: list[RunId] = Field(default_factory=list, max_length=32)
    depth: int = Field(ge=0, le=32)
    agent_name: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _lineage_is_consistent(self) -> PersistedToolCallLineage:
        if self.depth != len(self.ancestor_run_ids):
            raise ValueError("lineage depth must equal ancestorRunIds length")
        if self.depth == 0:
            if self.parent_run_id is not None or self.ancestor_run_ids or self.root_run_id != self.run_id:
                raise ValueError("root lineage must reference itself and have no ancestors")
        elif self.parent_run_id != self.ancestor_run_ids[-1]:
            raise ValueError("parentRunId must be the nearest ancestor")
        return self


class PersistedToolCall(WireModel):
    tool_call_id: ToolCallId
    name: str = Field(min_length=3, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    version: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    arguments: JsonObject
    args_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    definition_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    result_sensitivity: Literal["public", "workspace", "private", "secret"] | None = None
    idempotency_key: str = Field(min_length=1, max_length=256)
    deadline: Rfc3339DateTime | None = None
    lineage: PersistedToolCallLineage


class ToolCallsAcceptedPayload(WireModel):
    calls: list[PersistedToolCall] = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _call_ids_are_unique(self) -> ToolCallsAcceptedPayload:
        call_ids = [call.tool_call_id for call in self.calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("accepted tool calls require unique toolCallIds")
        return self


class WriteOutcomeRequiredPayload(WireModel):
    reasons: list[str] = Field(min_length=1, max_length=256)


class RunContinuationRequiredPayload(WireModel):
    blockers: list[str] = Field(min_length=1, max_length=256)


class RuntimeWarningPayload(WireModel):
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    message: str = Field(min_length=1, max_length=8192)
    recommended_action: str | None = Field(default=None, max_length=4096)
    disabled_capabilities: list[CapabilityName] = Field(default_factory=list, max_length=32)


class SkillCatalogUpdatedPayload(WireModel):
    revision: int = Field(ge=1)
    snapshot_hash: Sha256Digest
    record_revision: int = Field(ge=1)
    discovered_count: int = Field(ge=0, le=100_000)
    enabled_count: int = Field(ge=0, le=100_000)
    partial: bool

    @model_validator(mode="after")
    def _enabled_is_discovered(self) -> SkillCatalogUpdatedPayload:
        if self.enabled_count > self.discovered_count:
            raise ValueError("enabledCount cannot exceed discoveredCount")
        return self


class SkillTrustChangedPayload(WireModel):
    root_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    package_path: str = Field(min_length=1, max_length=2048)
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    metadata_hash: Sha256Digest
    confirmed: bool
    record_revision: int = Field(ge=1)


_EventPayloadUnion = (
    TurnStartedPayload
    | PhaseChangedPayload
    | ReasoningSummaryPayload
    | AssistantDeltaPayload
    | AssistantCompletedPayload
    | ToolQueuedPayload
    | ToolStartedPayload
    | ToolProgressPayload
    | ToolCompletedPayload
    | ToolFailedPayload
    | ApprovalRequiredPayload
    | ApprovalResolvedPayload
    | ApprovalExpiredPayload
    | ReferencesUpdatedPayload
    | ArtifactCreatedPayload
    | UsageUpdatedPayload
    | ContextCompactedPayload
    | SessionUpdatedPayload
    | SubagentQueuedPayload
    | SubagentStartedPayload
    | SubagentProgressPayload
    | SubagentMessageReceivedPayload
    | SubagentWaitingPayload
    | SubagentResultAvailablePayload
    | SubagentCompletedPayload
    | SubagentFailedPayload
    | SubagentCancelRequestedPayload
    | SubagentCancelledPayload
    | SubagentInterruptedPayload
    | SubagentOrphanedPayload
    | SubagentRecoveredPayload
    | TurnCompletedPayload
    | TurnCancelledPayload
    | TurnFailedPayload
    | TurnInterruptedPayload
    | RuntimeWarningPayload
    | SkillCatalogUpdatedPayload
    | SkillTrustChangedPayload
    | ModelAttemptPayload
    | ModelCompositionStartedPayload
    | ToolCallsAcceptedPayload
    | WriteOutcomeRequiredPayload
    | RunContinuationRequiredPayload
    | TurnSteeredPayload
)

EventPayload = TypeAliasType("EventPayload", _EventPayloadUnion)


_EVENT_REGISTRY: dict[EventType, type[WireModel]] = {
    EventType.TURN_STARTED: TurnStartedPayload,
    EventType.PHASE_CHANGED: PhaseChangedPayload,
    EventType.REASONING_SUMMARY: ReasoningSummaryPayload,
    EventType.ASSISTANT_DELTA: AssistantDeltaPayload,
    EventType.ASSISTANT_COMPLETED: AssistantCompletedPayload,
    EventType.TOOL_QUEUED: ToolQueuedPayload,
    EventType.TOOL_STARTED: ToolStartedPayload,
    EventType.TOOL_PROGRESS: ToolProgressPayload,
    EventType.TOOL_COMPLETED: ToolCompletedPayload,
    EventType.TOOL_FAILED: ToolFailedPayload,
    EventType.APPROVAL_REQUIRED: ApprovalRequiredPayload,
    EventType.APPROVAL_RESOLVED: ApprovalResolvedPayload,
    EventType.APPROVAL_EXPIRED: ApprovalExpiredPayload,
    EventType.REFERENCES_UPDATED: ReferencesUpdatedPayload,
    EventType.ARTIFACT_CREATED: ArtifactCreatedPayload,
    EventType.USAGE_UPDATED: UsageUpdatedPayload,
    EventType.CONTEXT_COMPACTED: ContextCompactedPayload,
    EventType.SESSION_UPDATED: SessionUpdatedPayload,
    EventType.SUBAGENT_QUEUED: SubagentQueuedPayload,
    EventType.SUBAGENT_STARTED: SubagentStartedPayload,
    EventType.SUBAGENT_PROGRESS: SubagentProgressPayload,
    EventType.SUBAGENT_MESSAGE_RECEIVED: SubagentMessageReceivedPayload,
    EventType.SUBAGENT_WAITING: SubagentWaitingPayload,
    EventType.SUBAGENT_RESULT_AVAILABLE: SubagentResultAvailablePayload,
    EventType.SUBAGENT_COMPLETED: SubagentCompletedPayload,
    EventType.SUBAGENT_FAILED: SubagentFailedPayload,
    EventType.SUBAGENT_CANCEL_REQUESTED: SubagentCancelRequestedPayload,
    EventType.SUBAGENT_CANCELLED: SubagentCancelledPayload,
    EventType.SUBAGENT_INTERRUPTED: SubagentInterruptedPayload,
    EventType.SUBAGENT_ORPHANED: SubagentOrphanedPayload,
    EventType.SUBAGENT_RECOVERED: SubagentRecoveredPayload,
    EventType.TURN_COMPLETED: TurnCompletedPayload,
    EventType.TURN_CANCELLED: TurnCancelledPayload,
    EventType.TURN_FAILED: TurnFailedPayload,
    EventType.RUNTIME_WARNING: RuntimeWarningPayload,
    EventType.SKILL_CATALOG_UPDATED: SkillCatalogUpdatedPayload,
    EventType.SKILL_TRUST_CHANGED: SkillTrustChangedPayload,
    EventType.MODEL_ATTEMPT: ModelAttemptPayload,
    EventType.MODEL_COMPOSITION_STARTED: ModelCompositionStartedPayload,
    EventType.TOOL_CALLS_ACCEPTED: ToolCallsAcceptedPayload,
    EventType.WRITE_OUTCOME_REQUIRED: WriteOutcomeRequiredPayload,
    EventType.RUN_CONTINUATION_REQUIRED: RunContinuationRequiredPayload,
    EventType.TURN_INTERRUPTED: TurnInterruptedPayload,
    EventType.TURN_STEERED: TurnSteeredPayload,
}

EVENT_REGISTRY: Mapping[EventType, type[WireModel]] = MappingProxyType(_EVENT_REGISTRY)


class EventEnvelope(WireModel):
    protocol_version: ProtocolVersion
    schema_version: SchemaVersion
    event_id: EventId
    sequence: int = Field(ge=1)
    timestamp: Rfc3339DateTime
    trace_id: TraceId
    workspace_id: WorkspaceId
    session_id: SessionId | None = None
    turn_id: TurnId | None = None
    run_id: RunId | None = None
    root_run_id: RunId | None = None
    parent_run_id: RunId | None = None
    type: EventType
    payload: EventPayload

    @model_validator(mode="before")
    @classmethod
    def _validate_payload_for_type(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        raw_type = value.get("type")
        try:
            event_type = raw_type if isinstance(raw_type, EventType) else EventType(raw_type)
        except (TypeError, ValueError):
            return value
        payload_type = EVENT_REGISTRY[event_type]
        raw_payload = value.get("payload")
        if not isinstance(raw_payload, payload_type):
            value = dict(value)
            value["payload"] = validate_wire(payload_type, raw_payload)
        return value

    @model_validator(mode="after")
    def _correlate_type_payload_and_lineage(self) -> EventEnvelope:
        expected_payload = EVENT_REGISTRY[self.type]
        if not isinstance(self.payload, expected_payload):
            raise ValueError(f"payload for {self.type.value} must be {expected_payload.__name__}")
        if self.run_id is not None:
            if self.session_id is None or self.turn_id is None or self.root_run_id is None:
                raise ValueError("run events require sessionId, turnId, and rootRunId")
        if self.parent_run_id is not None and self.run_id is None:
            raise ValueError("parentRunId requires runId")
        return self


def parse_event(value: object) -> EventEnvelope:
    return validate_wire(EventEnvelope, value)


PERSISTED_EVENT_RECORD_VERSION = 1
CURRENT_PROTOCOL_VERSION = "1.0"
CURRENT_SCHEMA_VERSION = "1"


class PersistedDomainEvent(WireModel):
    """Versioned semantic fact stored inside :class:`StoredEvent.payload`.

    The append-only store owns sequencing, identity and timestamp.  This record
    owns the protocol lineage and the strictly typed payload.  Keeping the event
    type in both the row and the record lets replay fail closed on corruption.
    """

    record_version: Literal[1]
    protocol_version: ProtocolVersion
    schema_version: SchemaVersion
    type: EventType
    trace_id: TraceId
    workspace_id: WorkspaceId
    session_id: SessionId | None = None
    turn_id: TurnId | None = None
    run_id: RunId | None = None
    root_run_id: RunId | None = None
    parent_run_id: RunId | None = None
    state_revision: int = Field(ge=0)
    payload: EventPayload

    @model_validator(mode="before")
    @classmethod
    def _validate_payload_for_type(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        raw_type = value.get("type")
        try:
            event_type = raw_type if isinstance(raw_type, EventType) else EventType(raw_type)
        except (TypeError, ValueError):
            return value
        payload_type = EVENT_REGISTRY[event_type]
        raw_payload = value.get("payload")
        if not isinstance(raw_payload, payload_type):
            value = dict(value)
            value["payload"] = validate_wire(payload_type, raw_payload)
        return value

    @model_validator(mode="after")
    def _correlate_type_payload_and_lineage(self) -> PersistedDomainEvent:
        expected_payload = EVENT_REGISTRY[self.type]
        if not isinstance(self.payload, expected_payload):
            raise ValueError(f"payload for {self.type.value} must be {expected_payload.__name__}")
        if self.run_id is not None:
            if self.session_id is None or self.turn_id is None or self.root_run_id is None:
                raise ValueError("run events require sessionId, turnId, and rootRunId")
        if self.parent_run_id is not None and self.run_id is None:
            raise ValueError("parentRunId requires runId")
        return self


class StoredEventMigrationRequired(ValueError):
    """Raised when a pre-versioned, lossy event row needs an explicit migration."""


class StoredEventLike(Protocol):
    @property
    def event_id(self) -> str: ...

    @property
    def event_type(self) -> str: ...

    @property
    def sequence(self) -> int: ...

    @property
    def payload(self) -> Mapping[str, Any]: ...

    @property
    def occurred_at(self) -> datetime: ...


def make_domain_event_record(
    *,
    event_type: EventType | str,
    payload: WireModel | Mapping[str, Any],
    trace_id: str,
    workspace_id: str,
    session_id: str | None,
    turn_id: str | None,
    run_id: str | None,
    root_run_id: str | None,
    parent_run_id: str | None,
    state_revision: int,
) -> PersistedDomainEvent:
    """Create the only persisted event record accepted from Core producers."""

    resolved_type = event_type if isinstance(event_type, EventType) else EventType(event_type)
    raw_payload = payload.to_wire() if isinstance(payload, WireModel) else dict(payload)
    return validate_wire(
        PersistedDomainEvent,
        {
            "recordVersion": PERSISTED_EVENT_RECORD_VERSION,
            "protocolVersion": CURRENT_PROTOCOL_VERSION,
            "schemaVersion": CURRENT_SCHEMA_VERSION,
            "type": resolved_type.value,
            "traceId": trace_id,
            "workspaceId": workspace_id,
            "sessionId": session_id,
            "turnId": turn_id,
            "runId": run_id,
            "rootRunId": root_run_id,
            "parentRunId": parent_run_id,
            "stateRevision": state_revision,
            "payload": raw_payload,
        },
    )


def parse_persisted_domain_event(value: object) -> PersistedDomainEvent:
    """Parse a versioned stored fact; legacy arbitrary dictionaries never pass."""

    if isinstance(value, Mapping) and "recordVersion" not in value:
        raise StoredEventMigrationRequired(
            "stored event predates persisted event record v1; run the explicit event-store migration"
        )
    try:
        return validate_wire(PersistedDomainEvent, _materialize_json(value))
    except ValidationError:
        raise


def _materialize_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _materialize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_materialize_json(item) for item in value]
    return value


def stored_event_to_envelope(event: StoredEventLike) -> EventEnvelope:
    """Losslessly convert one authoritative stored fact into its wire envelope."""

    record = parse_persisted_domain_event(event.payload)
    if record.type.value != event.event_type:
        raise ValueError(f"stored event row type {event.event_type!r} disagrees with record type {record.type.value!r}")
    return parse_event(
        {
            "protocolVersion": record.protocol_version,
            "schemaVersion": record.schema_version,
            "eventId": event.event_id,
            "sequence": event.sequence,
            "timestamp": event.occurred_at.isoformat(),
            "traceId": record.trace_id,
            "workspaceId": record.workspace_id,
            "sessionId": record.session_id,
            "turnId": record.turn_id,
            "runId": record.run_id,
            "rootRunId": record.root_run_id,
            "parentRunId": record.parent_run_id,
            "type": record.type.value,
            "payload": record.payload.to_wire(),
        }
    )


__all__ = [
    "EVENT_REGISTRY",
    "CURRENT_PROTOCOL_VERSION",
    "CURRENT_SCHEMA_VERSION",
    "EventEnvelope",
    "EventPayload",
    "EventType",
    "PERSISTED_EVENT_RECORD_VERSION",
    "PersistedDomainEvent",
    "SessionForkReferencePayload",
    "StoredEventMigrationRequired",
    "make_domain_event_record",
    "parse_event",
    "parse_persisted_domain_event",
    "stored_event_to_envelope",
] + [model.__name__ for model in EVENT_REGISTRY.values()]
