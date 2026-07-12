from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .state import RunState


class StopReason(str, Enum):
    MODEL_FINISHED = "model_finished"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    RUNTIME_INTERRUPTED = "runtime_interrupted"


@dataclass(frozen=True, slots=True)
class TerminationDecision:
    can_compose: bool
    partial: bool
    reason: StopReason
    blockers: tuple[str, ...]


def evaluate_termination(state: RunState, *, reason: StopReason) -> TerminationDecision:
    blockers: list[str] = []
    if state.pending.tool_call_ids:
        blockers.append("tool_calls_pending")
    if state.pending.approval_ids:
        blockers.append("approvals_pending")
    if state.pending.client_invocation_ids:
        blockers.append("client_invocations_pending")
    if state.pending.child_run_ids:
        blockers.append("child_runs_pending")
    if not state.write_obligation.satisfied:
        blockers.append("write_outcome_required")

    if reason in {StopReason.CANCELLED, StopReason.RUNTIME_INTERRUPTED}:
        return TerminationDecision(False, True, reason, tuple(blockers))
    if blockers:
        return TerminationDecision(False, False, reason, tuple(blockers))

    partial = reason in {StopReason.BUDGET_EXHAUSTED, StopReason.MODEL_ERROR, StopReason.TOOL_ERROR}
    partial = partial or state.write_obligation.requires_manual_review
    return TerminationDecision(True, partial, reason, ())
