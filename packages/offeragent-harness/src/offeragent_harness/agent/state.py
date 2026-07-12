from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools import SideEffectClass, ToolDefinition, ToolResult, ToolResultStatus


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
    COMPOSING = "composing"
    PERSISTING = "persisting"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {RunPhase.COMPLETED, RunPhase.CANCELLED, RunPhase.FAILED, RunPhase.INTERRUPTED}


ALLOWED_PHASE_TRANSITIONS: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.CREATED: frozenset({RunPhase.LOADING_CONTEXT, RunPhase.CANCELLING, RunPhase.FAILED}),
    RunPhase.LOADING_CONTEXT: frozenset(
        {RunPhase.SELECTING_MEMORY, RunPhase.CANCELLING, RunPhase.FAILED, RunPhase.INTERRUPTED}
    ),
    RunPhase.SELECTING_MEMORY: frozenset(
        {RunPhase.PLANNING, RunPhase.CANCELLING, RunPhase.FAILED, RunPhase.INTERRUPTED}
    ),
    RunPhase.PLANNING: frozenset(
        {RunPhase.VALIDATING_CALLS, RunPhase.COMPOSING, RunPhase.CANCELLING, RunPhase.FAILED, RunPhase.INTERRUPTED}
    ),
    RunPhase.VALIDATING_CALLS: frozenset(
        {RunPhase.CHECKING_POLICY, RunPhase.RECORDING_RESULTS, RunPhase.CANCELLING, RunPhase.FAILED}
    ),
    RunPhase.CHECKING_POLICY: frozenset(
        {RunPhase.AWAITING_APPROVAL, RunPhase.EXECUTING_TOOLS, RunPhase.RECORDING_RESULTS, RunPhase.CANCELLING}
    ),
    RunPhase.AWAITING_APPROVAL: frozenset(
        {RunPhase.EXECUTING_TOOLS, RunPhase.RECORDING_RESULTS, RunPhase.CANCELLING, RunPhase.FAILED}
    ),
    RunPhase.EXECUTING_TOOLS: frozenset(
        {RunPhase.RECORDING_RESULTS, RunPhase.CANCELLING, RunPhase.FAILED, RunPhase.INTERRUPTED}
    ),
    RunPhase.RECORDING_RESULTS: frozenset(
        {RunPhase.PLANNING, RunPhase.CANCELLING, RunPhase.FAILED, RunPhase.INTERRUPTED}
    ),
    RunPhase.COMPOSING: frozenset({RunPhase.PERSISTING, RunPhase.CANCELLING, RunPhase.FAILED, RunPhase.INTERRUPTED}),
    RunPhase.PERSISTING: frozenset({RunPhase.COMPLETED, RunPhase.INTERRUPTED, RunPhase.FAILED}),
    RunPhase.CANCELLING: frozenset({RunPhase.CANCELLED, RunPhase.INTERRUPTED}),
    RunPhase.COMPLETED: frozenset(),
    RunPhase.CANCELLED: frozenset(),
    RunPhase.FAILED: frozenset(),
    RunPhase.INTERRUPTED: frozenset(),
}


class InvalidRunTransition(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    tool_call_id: str
    status: ToolResultStatus
    summary: str

    @property
    def requires_manual_review(self) -> bool:
        return self.status in {ToolResultStatus.PARTIAL, ToolResultStatus.UNKNOWN_OUTCOME}


@dataclass(frozen=True, slots=True)
class WriteObligation:
    required: bool = False
    reasons: tuple[str, ...] = ()
    outcomes: tuple[WriteOutcome, ...] = ()

    @property
    def satisfied(self) -> bool:
        return not self.required or bool(self.outcomes)

    @property
    def requires_manual_review(self) -> bool:
        return any(outcome.requires_manual_review for outcome in self.outcomes)

    def require(self, reason: str) -> WriteObligation:
        if not reason:
            raise ValueError("write obligation reason must not be empty")
        if reason in self.reasons:
            return self
        return replace(self, required=True, reasons=(*self.reasons, reason))

    def observe(self, definition: ToolDefinition, result: ToolResult) -> WriteObligation:
        if definition.side_effect_class not in {
            SideEffectClass.WRITE,
            SideEffectClass.DESTRUCTIVE,
            SideEffectClass.UNKNOWN,
        }:
            return self
        if any(existing.tool_call_id == result.tool_call_id for existing in self.outcomes):
            return self
        outcome = WriteOutcome(result.tool_call_id, result.status, result.user_visible_summary)
        return replace(self, outcomes=(*self.outcomes, outcome))


@dataclass(frozen=True, slots=True)
class PendingWork:
    tool_call_ids: frozenset[str] = frozenset()
    approval_ids: frozenset[str] = frozenset()
    client_invocation_ids: frozenset[str] = frozenset()
    child_run_ids: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        return not (self.tool_call_ids or self.approval_ids or self.client_invocation_ids or self.child_run_ids)


@dataclass(frozen=True, slots=True)
class RunState:
    workspace_id: str
    session_id: str
    turn_id: str
    run_id: str
    lineage: AgentLineage
    phase: RunPhase = RunPhase.CREATED
    revision: int = 0
    model_rounds: int = 0
    tool_calls: int = 0
    pending: PendingWork = PendingWork()
    write_obligation: WriteObligation = WriteObligation()
    tool_results: tuple[ToolResult, ...] = ()
    assistant_text: str = ""

    def __post_init__(self) -> None:
        if self.run_id != self.lineage.run_id:
            raise ValueError("run_id must match lineage")
        if self.revision < 0 or self.model_rounds < 0 or self.tool_calls < 0:
            raise ValueError("run counters cannot be negative")

    def transition(self, target: RunPhase) -> RunState:
        if target not in ALLOWED_PHASE_TRANSITIONS[self.phase]:
            raise InvalidRunTransition(f"invalid run transition {self.phase.value} -> {target.value}")
        return replace(self, phase=target, revision=self.revision + 1)

    def require_write_outcome(self, reason: str) -> RunState:
        return replace(
            self,
            write_obligation=self.write_obligation.require(reason),
            revision=self.revision + 1,
        )

    def record_tool_result(self, definition: ToolDefinition, result: ToolResult) -> RunState:
        if result.tool_call_id in {stored.tool_call_id for stored in self.tool_results}:
            return self
        pending = replace(self.pending, tool_call_ids=self.pending.tool_call_ids - {result.tool_call_id})
        return replace(
            self,
            pending=pending,
            write_obligation=self.write_obligation.observe(definition, result),
            tool_results=(*self.tool_results, result),
            revision=self.revision + 1,
        )
