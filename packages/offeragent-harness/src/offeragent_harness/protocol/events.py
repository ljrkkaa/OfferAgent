"""Typed, replayable semantic events emitted only by the Harness Core."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Literal

from pydantic import Field, model_validator
from typing_extensions import TypeAliasType

from ._base import WireModel, validate_wire
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
    INDEX_STARTED = "index.started"
    INDEX_PROGRESS = "index.progress"
    INDEX_COMPLETED = "index.completed"


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


class ToolCompletedPayload(WireModel):
    result: ToolResultDescriptor

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


class ApprovalResolvedPayload(WireModel):
    approval_id: ApprovalId
    decision: ApprovalDecision
    scope: ApprovalScope
    resolved_at: Rfc3339DateTime
    resolved_by: Literal["user", "policy", "system"]


class ApprovalExpiredPayload(WireModel):
    approval_id: ApprovalId
    expired_at: Rfc3339DateTime
    reason: str = Field(min_length=1, max_length=4096)


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


class SessionUpdatedPayload(WireModel):
    session: SessionSummary
    changed_fields: list[Literal["title", "activeRunId", "turnCount", "updatedAt", "deleted", "compaction"]] = Field(
        min_length=1, max_length=16
    )


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


class TurnFailedPayload(WireModel):
    error: ErrorEnvelope
    usage: UsageSnapshot
    partial_content: list[ContentBlock] = Field(default_factory=list, max_length=256)


class RuntimeWarningPayload(WireModel):
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    message: str = Field(min_length=1, max_length=8192)
    recommended_action: str | None = Field(default=None, max_length=4096)
    disabled_capabilities: list[CapabilityName] = Field(default_factory=list, max_length=32)


class IndexStartedPayload(WireModel):
    generation: int = Field(ge=1)
    reason: Literal["initial", "manual", "change", "overflow", "corruption", "model_changed"]
    total_documents: int | None = Field(default=None, ge=0)


class IndexProgressPayload(WireModel):
    generation: int = Field(ge=1)
    completed_documents: int = Field(ge=0)
    total_documents: int = Field(ge=0)
    failed_documents: int = Field(default=0, ge=0)
    current_path: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _documents_are_bounded(self) -> IndexProgressPayload:
        if self.completed_documents + self.failed_documents > self.total_documents:
            raise ValueError("processed document count cannot exceed totalDocuments")
        return self


class IndexCompletedPayload(WireModel):
    generation: int = Field(ge=1)
    workspace_revision: int = Field(ge=0)
    document_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    failed_document_count: int = Field(ge=0)
    partial: bool
    duration_ms: int = Field(ge=0)


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
    | RuntimeWarningPayload
    | IndexStartedPayload
    | IndexProgressPayload
    | IndexCompletedPayload
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
    EventType.INDEX_STARTED: IndexStartedPayload,
    EventType.INDEX_PROGRESS: IndexProgressPayload,
    EventType.INDEX_COMPLETED: IndexCompletedPayload,
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


__all__ = [
    "EVENT_REGISTRY",
    "EventEnvelope",
    "EventPayload",
    "EventType",
    "parse_event",
] + [model.__name__ for model in EVENT_REGISTRY.values()]
