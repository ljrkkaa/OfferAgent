from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import cast

import pytest

from offeragent_harness.agent.budgets import BudgetLedger, RunBudget
from offeragent_harness.agent.loop import AgentLoopFailure, ToolExecution, run_agent_loop
from offeragent_harness.agent.model_planner import ModelProviderFailure
from offeragent_harness.agent.planner import PlanningAttempt, PlanningAttemptOutcome, PlanningStep
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.models import ModelError, ModelUsage
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import CancellationToken, ToolLifecycleObserver
from offeragent_harness.runtime import CancellationScope
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffectClass,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
)


class ScriptedPlanner:
    def __init__(self, steps: Sequence[PlanningStep]) -> None:
        self._steps = list(steps)

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        if not self._steps:
            raise AssertionError("unexpected Agent planning round")
        step = self._steps.pop(0)
        return replace(
            step,
            attempts=(
                PlanningAttempt(
                    request_id=f"request-{state.model_rounds + 1}",
                    repair_index=0,
                    outcome=PlanningAttemptOutcome.SUCCEEDED,
                    usage=ModelUsage(1, 1, 0, 0),
                ),
            ),
        )


class FailingPlanner:
    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        del state
        cancellation.checkpoint()
        raise ModelProviderFailure(
            "request-provider-failure",
            ModelError(
                "provider_protocol_error",
                "model provider protocol failed",
                False,
                False,
                {
                    "providerId": "deepseek",
                    "protocolReason": "terminal_metadata_missing",
                    "untrustedDetail": "must-not-cross-runtime-boundary",
                },
            ),
        )


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object], bool]] = []

    async def commit(
        self,
        state: RunState,
        *,
        event_type: str,
        payload: Mapping[str, object],
        terminal: bool = False,
    ) -> None:
        del state
        if terminal and any(item[2] for item in self.events):
            raise AssertionError("only one terminal event may be committed")
        self.events.append((event_type, payload, terminal))


class Kernel:
    def __init__(self, execution: ToolExecution | None = None) -> None:
        self._execution = execution

    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        cancellation.checkpoint()
        if self._execution is None or tuple(calls) != (self._execution.call,) or observer is None:
            raise AssertionError("unexpected Tool Kernel execution")
        await observer.execution_started(self._execution.call, self._execution.definition)
        await observer.result_available(
            self._execution.call,
            self._execution.definition,
            self._execution.result,
        )
        return (self._execution,)


def _state() -> RunState:
    return RunState("workspace", "session", "turn", "run", AgentLineage.root("run"))


def _budget(*, model_rounds: int = 4) -> BudgetLedger:
    return BudgetLedger(
        RunBudget(
            max_model_rounds=model_rounds,
            max_tool_calls=4,
            max_parallel_reads=2,
            max_wall_seconds=60,
            max_input_tokens=100,
            max_output_tokens=100,
            max_cost=Decimal("1"),
            max_artifact_bytes=1_000,
            max_subagents=1,
        ),
        started_at=datetime.now(timezone.utc),
    )


def _write_definition() -> ToolDefinition:
    return ToolDefinition(
        name="vault.transaction",
        version="1",
        description="Commit one CAS-checked Vault file transaction",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={},
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        required_capabilities=frozenset({"vault.write"}),
        concurrency_safe=False,
        idempotent=True,
        retryable=False,
        timeout_ms=30_000,
        output_limit_bytes=4_096,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    )


def _write_execution() -> ToolExecution:
    definition = _write_definition()
    call = ToolCall(
        tool_call_id="call-write",
        run_id="run",
        workspace_id="workspace",
        name=definition.name,
        version=definition.version,
        arguments={},
        args_hash=canonical_json_sha256({}),
        idempotency_key="write-once",
        deadline=None,
        lineage=AgentLineage.root("run"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )
    result = ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data={"committed": True},
        user_visible_summary="Vault 文件已提交",
        artifact_ids=(),
        source_refs=(),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state={"hash": "sha256:" + "a" * 64},
        error=None,
    )
    return ToolExecution(call, definition, result)


@pytest.mark.asyncio
async def test_one_agent_step_publishes_final_response_without_second_model_call() -> None:
    response = "已完成。" * 100
    recorder = Recorder()
    result = await run_agent_loop(
        _state(),
        planner=ScriptedPlanner((PlanningStep((), False, response),)),
        tool_kernel=Kernel(),
        recorder=recorder,
        budget=_budget(),
        cancellation=CancellationScope(name="test-run"),
        now=lambda: datetime.now(timezone.utc),
    )

    assert result.phase is RunPhase.COMPLETED
    assert result.assistant_text == response
    assert result.model_rounds == 1
    deltas = [cast(str, payload["delta"]) for event, payload, _ in recorder.events if event == "assistant.delta"]
    assert "".join(deltas) == response
    assert recorder.events[-1][0::2] == ("turn.completed", True)


@pytest.mark.asyncio
async def test_tool_result_returns_to_same_loop_before_final_response() -> None:
    execution = _write_execution()
    recorder = Recorder()
    result = await run_agent_loop(
        _state(),
        planner=ScriptedPlanner(
            (
                PlanningStep((execution.call,), True, None),
                PlanningStep((), True, "本周的计划文件已逐项写入。"),
            )
        ),
        tool_kernel=Kernel(execution),
        recorder=recorder,
        budget=_budget(),
        cancellation=CancellationScope(name="test-run"),
        now=lambda: datetime.now(timezone.utc),
    )

    assert result.phase is RunPhase.COMPLETED
    assert result.model_rounds == 2
    assert result.tool_calls == 1
    assert result.write_obligation.satisfied
    event_names = [event for event, _, _ in recorder.events]
    assert event_names.index("tool.completed") < event_names.index("assistant.completed")


def test_agent_step_contract_rejects_mixed_or_empty_terminal_states() -> None:
    call = _write_execution().call
    with pytest.raises(ValueError, match="cannot also contain"):
        PlanningStep((call,), False, "not allowed")
    with pytest.raises(ValueError, match="non-empty final response"):
        PlanningStep((), False, None)


@pytest.mark.asyncio
async def test_provider_protocol_failure_persists_only_stable_safe_discriminators() -> None:
    recorder = Recorder()
    with pytest.raises(AgentLoopFailure) as caught:
        await run_agent_loop(
            _state(),
            planner=FailingPlanner(),
            tool_kernel=Kernel(),
            recorder=recorder,
            budget=_budget(),
            cancellation=CancellationScope(name="test-run"),
            now=lambda: datetime.now(timezone.utc),
        )

    assert caught.value.state.phase is RunPhase.FAILED
    event, payload, terminal = recorder.events[-1]
    assert event == "turn.failed" and terminal
    error = cast(Mapping[str, object], payload["error"])
    assert error["details"] == {
        "errorType": "ModelProviderFailure",
        "failureCategory": "model",
        "providerErrorCode": "provider_protocol_error",
        "modelRequestId": "request-provider-failure",
        "providerProtocolReason": "terminal_metadata_missing",
    }
    assert "must-not-cross-runtime-boundary" not in repr(payload)
