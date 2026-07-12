from __future__ import annotations

from dataclasses import replace

from offeragent_harness.agent.state import PendingWork, RunState
from offeragent_harness.agent.termination import StopReason, evaluate_termination
from offeragent_harness.permissions import RiskClass
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools import (
    ExecutorLocation,
    SideEffect,
    SideEffectClass,
    SideEffectKind,
    SideEffectState,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
)


def definition(side_effect: SideEffectClass) -> ToolDefinition:
    return ToolDefinition(
        name="vault.transaction" if side_effect is SideEffectClass.WRITE else "vault.read",
        version="1",
        description="test",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={},
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.WRITE if side_effect is SideEffectClass.WRITE else RiskClass.READ,
        side_effect_class=side_effect,
        required_capabilities=frozenset({"vault"}),
        concurrency_safe=side_effect is SideEffectClass.READ,
        idempotent=True,
        retryable=side_effect is SideEffectClass.READ,
        timeout_ms=1_000,
        output_limit_bytes=1_024,
    )


def state() -> RunState:
    return RunState(
        workspace_id="ws",
        session_id="session",
        turn_id="turn",
        run_id="run",
        lineage=AgentLineage.root("run"),
    )


def result(
    status: ToolResultStatus,
    *,
    tool_call_id: str = "call",
    side_effects: tuple[SideEffect, ...] = (),
) -> ToolResult:
    error = None
    if status is not ToolResultStatus.SUCCEEDED:
        error = ToolError(code=status.value, message=status.value, retryable=False, cancelled=False)
    return ToolResult(
        tool_call_id=tool_call_id,
        status=status,
        data=None,
        user_visible_summary=status.value,
        artifact_ids=(),
        source_refs=(),
        side_effects=side_effects,
        retryable=False,
        before_state=None,
        after_state=None,
        error=error,
    )


def test_write_obligation_requires_real_terminal_tool_result() -> None:
    current = state().require_write_outcome("canonical plan requires a write outcome")
    decision = evaluate_termination(current, reason=StopReason.MODEL_FINISHED)
    assert not decision.can_compose
    assert decision.blockers == ("write_outcome_required",)

    current = current.record_tool_result(definition(SideEffectClass.READ), result(ToolResultStatus.SUCCEEDED))
    assert not evaluate_termination(current, reason=StopReason.MODEL_FINISHED).can_compose

    current = current.record_tool_result(
        definition(SideEffectClass.WRITE),
        result(ToolResultStatus.DENIED, tool_call_id="write-call"),
    )
    assert evaluate_termination(current, reason=StopReason.MODEL_FINISHED).can_compose


def test_pending_work_blocks_composer_even_without_write_obligation() -> None:
    current = replace(state(), pending=PendingWork(child_run_ids=frozenset({"child"})))
    decision = evaluate_termination(current, reason=StopReason.MODEL_FINISHED)
    assert not decision.can_compose
    assert decision.blockers == ("child_runs_pending",)


def test_unknown_write_outcome_allows_only_partial_manual_review_composition() -> None:
    current = state().require_write_outcome("write")
    unknown = result(
        ToolResultStatus.UNKNOWN_OUTCOME,
        side_effects=(
            SideEffect(
                kind=SideEffectKind.FILE_WRITE,
                state=SideEffectState.UNKNOWN,
                resource_id="vault:note.md",
                before_state=None,
                after_state=None,
            ),
        ),
    )
    current = current.record_tool_result(definition(SideEffectClass.WRITE), unknown)
    decision = evaluate_termination(current, reason=StopReason.MODEL_FINISHED)
    assert decision.can_compose
    assert decision.partial
