from __future__ import annotations

from dataclasses import dataclass

from .state import RunState


@dataclass(frozen=True, slots=True)
class ResponseReadiness:
    can_respond: bool
    blockers: tuple[str, ...]


def evaluate_response_readiness(state: RunState) -> ResponseReadiness:
    blockers: list[str] = []
    if state.pending.tool_call_ids:
        blockers.append("tool_calls_pending")
    if state.pending.approval_ids:
        blockers.append("approvals_pending")
    if state.pending.child_run_ids:
        blockers.append("child_runs_pending")
    if not state.write_obligation.satisfied:
        blockers.append("write_outcome_required")

    if blockers:
        return ResponseReadiness(False, tuple(blockers))
    return ResponseReadiness(True, ())


__all__ = ["ResponseReadiness", "evaluate_response_readiness"]
