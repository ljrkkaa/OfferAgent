from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Any

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools import (
    ResultSensitivity,
    SideEffectClass,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
)

from .budget_checkpoint import BudgetCheckpoint


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
    RESPONDING = "responding"
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
        {RunPhase.VALIDATING_CALLS, RunPhase.RESPONDING, RunPhase.CANCELLING, RunPhase.FAILED, RunPhase.INTERRUPTED}
    ),
    RunPhase.VALIDATING_CALLS: frozenset(
        {
            RunPhase.CHECKING_POLICY,
            RunPhase.RECORDING_RESULTS,
            RunPhase.CANCELLING,
            RunPhase.FAILED,
            RunPhase.INTERRUPTED,
        }
    ),
    RunPhase.CHECKING_POLICY: frozenset(
        {
            RunPhase.AWAITING_APPROVAL,
            RunPhase.EXECUTING_TOOLS,
            RunPhase.RECORDING_RESULTS,
            RunPhase.CANCELLING,
            RunPhase.FAILED,
            RunPhase.INTERRUPTED,
        }
    ),
    RunPhase.AWAITING_APPROVAL: frozenset(
        {
            RunPhase.EXECUTING_TOOLS,
            RunPhase.RECORDING_RESULTS,
            RunPhase.CANCELLING,
            RunPhase.FAILED,
            RunPhase.INTERRUPTED,
        }
    ),
    RunPhase.EXECUTING_TOOLS: frozenset(
        {
            RunPhase.AWAITING_APPROVAL,
            RunPhase.RECORDING_RESULTS,
            RunPhase.CANCELLING,
            RunPhase.FAILED,
            RunPhase.INTERRUPTED,
        }
    ),
    RunPhase.RECORDING_RESULTS: frozenset(
        {
            RunPhase.AWAITING_APPROVAL,
            RunPhase.EXECUTING_TOOLS,
            RunPhase.PLANNING,
            RunPhase.CANCELLING,
            RunPhase.FAILED,
            RunPhase.INTERRUPTED,
        }
    ),
    RunPhase.RESPONDING: frozenset({RunPhase.PERSISTING, RunPhase.CANCELLING, RunPhase.FAILED, RunPhase.INTERRUPTED}),
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

    def __post_init__(self) -> None:
        if not self.tool_call_id or not self.summary:
            raise ValueError("write outcome identity and summary must not be empty")

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
        if not self.required:
            return True
        return any(outcome.status is ToolResultStatus.SUCCEEDED for outcome in self.outcomes)

    @property
    def requires_manual_review(self) -> bool:
        return any(outcome.requires_manual_review for outcome in self.outcomes)

    def require(
        self,
        reason: str,
    ) -> WriteObligation:
        if not reason:
            raise ValueError("write obligation reason must not be empty")
        if reason in self.reasons:
            return self
        return replace(self, required=True, reasons=(*self.reasons, reason))

    def inherited_for_retry(self) -> WriteObligation:
        if not self.required:
            return self
        reason = "turn.retry.inherited_write_obligation"
        return replace(
            self,
            reasons=self.reasons if reason in self.reasons else (*self.reasons, reason),
            outcomes=(),
        )

    def observe(
        self,
        definition: ToolDefinition,
        result: ToolResult,
        call: ToolCall | None = None,
    ) -> WriteObligation:
        if definition.side_effect_class not in {
            SideEffectClass.WRITE,
            SideEffectClass.DESTRUCTIVE,
            SideEffectClass.UNKNOWN,
        }:
            return self
        del call
        if any(existing.tool_call_id == result.tool_call_id for existing in self.outcomes):
            return self
        outcome = WriteOutcome(
            result.tool_call_id,
            result.status,
            result.user_visible_summary,
        )
        return replace(self, outcomes=(*self.outcomes, outcome))


@dataclass(frozen=True, slots=True)
class PendingWork:
    tool_call_ids: frozenset[str] = frozenset()
    tool_calls: tuple[ToolCall, ...] = ()
    approval_ids: frozenset[str] = frozenset()
    child_run_ids: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        return not (self.tool_call_ids or self.approval_ids or self.child_run_ids)

    def __post_init__(self) -> None:
        call_ids = tuple(call.tool_call_id for call in self.tool_calls)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("pending ToolCalls contain duplicate IDs")
        if self.tool_calls and frozenset(call_ids) != self.tool_call_ids:
            raise ValueError("pending ToolCall records must exactly match tool_call_ids")


@dataclass(frozen=True, slots=True)
class RunControlMessage:
    message_id: str
    input_blocks: tuple[Mapping[str, Any], ...]
    mode: str
    apply_after_sequence: int

    def __post_init__(self) -> None:
        if not self.message_id or not self.input_blocks or self.mode not in {"append", "steer"}:
            raise ValueError("Run control message identity/input/mode is invalid")
        if self.apply_after_sequence < 0:
            raise ValueError("Run control sequence cannot be negative")
        blocks = tuple(freeze_json(item) for item in self.input_blocks)
        if any(not isinstance(item, FrozenJsonObject) for item in blocks):
            raise TypeError("Run control input blocks must be JSON objects")
        object.__setattr__(self, "input_blocks", blocks)


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
    control_messages: tuple[RunControlMessage, ...] = ()
    budget_checkpoint: BudgetCheckpoint | None = None
    tool_result_sensitivities: Mapping[str, ResultSensitivity] = MappingProxyType({})

    def __post_init__(self) -> None:
        if self.run_id != self.lineage.run_id:
            raise ValueError("run_id must match lineage")
        if self.revision < 0 or self.model_rounds < 0 or self.tool_calls < 0:
            raise ValueError("run counters cannot be negative")
        if self.budget_checkpoint is not None and not isinstance(self.budget_checkpoint, BudgetCheckpoint):
            raise TypeError("budget_checkpoint must be a BudgetCheckpoint or None")
        sensitivities = dict(self.tool_result_sensitivities)
        if any(not key or not isinstance(value, ResultSensitivity) for key, value in sensitivities.items()):
            raise TypeError("tool result sensitivity bindings require non-empty IDs and domain enum values")
        object.__setattr__(self, "tool_result_sensitivities", MappingProxyType(sensitivities))

    def __deepcopy__(self, memo: dict[int, Any]) -> RunState:
        """RunState is deeply immutable; in-memory stores may safely retain it."""

        memo[id(self)] = self
        return self

    def transition(self, target: RunPhase) -> RunState:
        if target not in ALLOWED_PHASE_TRANSITIONS[self.phase]:
            raise InvalidRunTransition(f"invalid run transition {self.phase.value} -> {target.value}")
        return replace(self, phase=target, revision=self.revision + 1)

    def require_write_outcome(
        self,
        reason: str,
    ) -> RunState:
        return replace(
            self,
            write_obligation=self.write_obligation.require(reason),
            revision=self.revision + 1,
        )

    def record_tool_result(self, definition: ToolDefinition, result: ToolResult) -> RunState:
        stored_result = next(
            (stored for stored in self.tool_results if stored.tool_call_id == result.tool_call_id),
            None,
        )
        if stored_result is not None:
            sensitivity = self.tool_result_sensitivities.get(result.tool_call_id)
            if (
                stored_result != result
                or sensitivity is None
                or sensitivity is ResultSensitivity.UNKNOWN
                or definition.result_sensitivity is not sensitivity
            ):
                raise ValueError("conflicting duplicate ToolResult or result sensitivity binding")
            return self
        pending_call = next(
            (call for call in self.pending.tool_calls if call.tool_call_id == result.tool_call_id),
            None,
        )
        sensitivity = self.tool_result_sensitivities.get(result.tool_call_id)
        if (
            pending_call is None
            or sensitivity is None
            or sensitivity is ResultSensitivity.UNKNOWN
            or pending_call.result_sensitivity is not sensitivity
            or definition.result_sensitivity is not sensitivity
        ):
            raise ValueError("ToolResult has no exact persisted result sensitivity binding")
        pending = replace(
            self.pending,
            tool_call_ids=self.pending.tool_call_ids - {result.tool_call_id},
            tool_calls=tuple(call for call in self.pending.tool_calls if call.tool_call_id != result.tool_call_id),
        )
        return replace(
            self,
            pending=pending,
            write_obligation=self.write_obligation.observe(definition, result, pending_call),
            tool_results=(*self.tool_results, result),
            revision=self.revision + 1,
        )

    def accept_tool_calls(self, calls: tuple[ToolCall, ...]) -> RunState:
        """Persist immutable per-call result classifications before execution."""

        if not calls:
            raise ValueError("accepted ToolCall batch must not be empty")
        if self.pending.tool_call_ids or self.pending.tool_calls:
            raise ValueError("cannot replace an existing pending ToolCall batch")
        call_ids = tuple(call.tool_call_id for call in calls)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("accepted ToolCall batch contains duplicate IDs")
        completed_ids = {result.tool_call_id for result in self.tool_results}
        bindings = dict(self.tool_result_sensitivities)
        for call in calls:
            sensitivity = call.result_sensitivity
            if sensitivity is ResultSensitivity.UNKNOWN:
                raise ValueError("active ToolCall has unknown result sensitivity")
            if call.tool_call_id in completed_ids or call.tool_call_id in bindings:
                raise ValueError("ToolCall ID cannot reuse completed or previously bound state")
            bindings[call.tool_call_id] = sensitivity
        return replace(
            self,
            pending=replace(
                self.pending,
                tool_call_ids=frozenset(call_ids),
                tool_calls=calls,
            ),
            tool_calls=self.tool_calls + len(calls),
            tool_result_sensitivities=bindings,
            revision=self.revision + 1,
        )

    def apply_control(self, message: RunControlMessage) -> RunState:
        if any(item.message_id == message.message_id for item in self.control_messages):
            return self
        return replace(self, control_messages=(*self.control_messages, message), revision=self.revision + 1)
