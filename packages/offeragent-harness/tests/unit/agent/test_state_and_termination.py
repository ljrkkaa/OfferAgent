from __future__ import annotations

from dataclasses import replace

import pytest

from offeragent_harness.agent.state import PendingWork, RunState
from offeragent_harness.agent.termination import StopReason, evaluate_termination
from offeragent_harness.foundation import canonical_json_sha256
from offeragent_harness.permissions import RiskClass
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffect,
    SideEffectClass,
    SideEffectKind,
    SideEffectState,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
)


def definition(side_effect: SideEffectClass) -> ToolDefinition:
    return ToolDefinition(
        name="vault.transaction" if side_effect is SideEffectClass.WRITE else "workspace.read",
        version="1",
        description="test",
        result_sensitivity=ResultSensitivity.WORKSPACE,
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
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def state() -> RunState:
    return RunState("ws", "session", "turn", "run", AgentLineage.root("run"))


def result(status: ToolResultStatus, *, tool_call_id: str) -> ToolResult:
    error = None
    side_effects: tuple[SideEffect, ...] = ()
    if status is not ToolResultStatus.SUCCEEDED:
        error = ToolError(code=status.value, message=status.value, retryable=False, cancelled=False)
    if status is ToolResultStatus.UNKNOWN_OUTCOME:
        side_effects = (
            SideEffect(
                kind=SideEffectKind.FILE_WRITE,
                state=SideEffectState.UNKNOWN,
                resource_id="vault:unknown",
                before_state=None,
                after_state=None,
            ),
        )
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


def call(tool_definition: ToolDefinition, tool_call_id: str) -> ToolCall:
    arguments: dict[str, object] = {}
    return ToolCall(
        tool_call_id=tool_call_id,
        run_id="run",
        workspace_id="ws",
        name=tool_definition.name,
        version=tool_definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=f"idem-{tool_call_id}",
        deadline=None,
        lineage=AgentLineage.root("run"),
        definition_fingerprint=tool_definition.fingerprint,
        result_sensitivity=tool_definition.result_sensitivity,
    )


def record(current: RunState, tool_definition: ToolDefinition, status: ToolResultStatus) -> RunState:
    tool_call = call(tool_definition, f"call-{current.tool_calls}")
    return current.accept_tool_calls((tool_call,)).record_tool_result(
        tool_definition,
        result(status, tool_call_id=tool_call.tool_call_id),
    )


def test_write_obligation_requires_a_successful_write_tool_result() -> None:
    current = state().require_write_outcome("canonical plan requires a write outcome")
    assert evaluate_termination(current, reason=StopReason.MODEL_FINISHED).blockers == (
        "write_outcome_required",
    )

    current = record(current, definition(SideEffectClass.READ), ToolResultStatus.SUCCEEDED)
    assert not current.write_obligation.satisfied

    current = record(current, definition(SideEffectClass.WRITE), ToolResultStatus.DENIED)
    assert not current.write_obligation.satisfied

    current = record(current, definition(SideEffectClass.WRITE), ToolResultStatus.SUCCEEDED)
    assert current.write_obligation.satisfied
    assert evaluate_termination(current, reason=StopReason.MODEL_FINISHED).can_compose


def test_tool_result_binding_is_exactly_once_and_call_ids_cannot_be_reused() -> None:
    tool_definition = definition(SideEffectClass.READ)
    tool_call = call(tool_definition, "bound-call")
    accepted = state().accept_tool_calls((tool_call,))
    completed_result = result(ToolResultStatus.SUCCEEDED, tool_call_id=tool_call.tool_call_id)

    completed = accepted.record_tool_result(tool_definition, completed_result)
    assert completed.record_tool_result(tool_definition, completed_result) is completed
    with pytest.raises(ValueError, match="conflicting duplicate ToolResult"):
        completed.record_tool_result(
            tool_definition,
            replace(completed_result, user_visible_summary="different result"),
        )
    with pytest.raises(ValueError, match="cannot reuse completed or previously bound state"):
        completed.accept_tool_calls((tool_call,))


def test_pending_work_blocks_composer_without_a_write_obligation() -> None:
    current = replace(state(), pending=PendingWork(child_run_ids=frozenset({"child"})))
    decision = evaluate_termination(current, reason=StopReason.MODEL_FINISHED)
    assert not decision.can_compose
    assert decision.blockers == ("child_runs_pending",)


def test_unknown_write_outcome_requires_manual_review_and_does_not_satisfy_the_obligation() -> None:
    current = record(
        state().require_write_outcome("write"),
        definition(SideEffectClass.WRITE),
        ToolResultStatus.UNKNOWN_OUTCOME,
    )
    assert current.write_obligation.requires_manual_review
    decision = evaluate_termination(current, reason=StopReason.MODEL_FINISHED)
    assert not decision.can_compose
    assert decision.blockers == ("write_outcome_required",)
