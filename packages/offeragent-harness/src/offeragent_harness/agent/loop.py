from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Protocol, TypeVar

from offeragent_harness.models import ModelUsage
from offeragent_harness.ports import CancellationToken, OperationCancelled
from offeragent_harness.tools import ToolCall, ToolDefinition, ToolResult

from .budgets import BudgetDelta, BudgetExceeded, BudgetLedger
from .composer import Composer
from .planner import Planner
from .state import ALLOWED_PHASE_TRANSITIONS, RunPhase, RunState
from .termination import StopReason, evaluate_termination


@dataclass(frozen=True, slots=True)
class ToolExecution:
    call: ToolCall
    definition: ToolDefinition
    result: ToolResult

    def __post_init__(self) -> None:
        if self.call.tool_call_id != self.result.tool_call_id:
            raise ValueError("tool execution call/result identities differ")
        if (self.call.name, self.call.version) != (self.definition.name, self.definition.version):
            raise ValueError("tool execution definition does not match call")


class ToolKernel(Protocol):
    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
    ) -> tuple[ToolExecution, ...]: ...


class RunRecorder(Protocol):
    async def commit(
        self,
        state: RunState,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        terminal: bool = False,
    ) -> None: ...


class AgentLoopFailure(RuntimeError):
    def __init__(self, state: RunState, cause: BaseException) -> None:
        super().__init__(f"agent loop failed in {state.phase.value}: {cause}")
        self.state = state
        self.__cause__ = cause


F = TypeVar("F", bound=Callable[..., Any])


def agent_loop_entrypoint(function: F) -> F:
    """Marks the one production Agent Loop entrypoint for architecture checks."""

    return function


async def _commit_phase(state: RunState, target: RunPhase, recorder: RunRecorder) -> RunState:
    state = state.transition(target)
    await recorder.commit(
        state,
        event_type="phase.changed",
        payload={"phase": target.value, "revision": state.revision},
    )
    return state


def _usage_delta(usage: ModelUsage | None) -> BudgetDelta:
    if usage is None:
        return BudgetDelta()
    return BudgetDelta(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost=usage.cost or Decimal("0"),
    )


async def _compose(
    state: RunState,
    *,
    partial: bool,
    composer: Composer,
    recorder: RunRecorder,
    budget: BudgetLedger,
    cancellation: CancellationToken,
) -> RunState:
    state = await _commit_phase(state, RunPhase.COMPOSING, recorder)
    text_parts: list[str] = []
    async for event in composer.stream(state, partial=partial, cancellation=cancellation):
        cancellation.checkpoint()
        if event.text_delta is not None:
            text_parts.append(event.text_delta)
            await recorder.commit(
                state,
                event_type="assistant.delta",
                payload={"text": event.text_delta},
            )
        else:
            await budget.consume(_usage_delta(event.usage))
            assert event.usage is not None
            await recorder.commit(
                state,
                event_type="usage.updated",
                payload={
                    "inputTokens": event.usage.input_tokens,
                    "outputTokens": event.usage.output_tokens,
                    "cachedInputTokens": event.usage.cached_input_tokens,
                    "reasoningTokens": event.usage.reasoning_tokens,
                },
            )
    state = replace(state, assistant_text="".join(text_parts), revision=state.revision + 1)
    await recorder.commit(
        state,
        event_type="assistant.completed",
        payload={"partial": partial, "textLength": len(state.assistant_text)},
    )
    state = await _commit_phase(state, RunPhase.PERSISTING, recorder)
    state = state.transition(RunPhase.COMPLETED)
    await recorder.commit(
        state,
        event_type="turn.completed",
        payload={"partial": partial},
        terminal=True,
    )
    return state


async def _cancelled(state: RunState, recorder: RunRecorder, cancelled: OperationCancelled) -> RunState:
    if RunPhase.CANCELLING in ALLOWED_PHASE_TRANSITIONS[state.phase]:
        state = await _commit_phase(state, RunPhase.CANCELLING, recorder)
        state = state.transition(RunPhase.CANCELLED)
    elif RunPhase.INTERRUPTED in ALLOWED_PHASE_TRANSITIONS[state.phase]:
        state = state.transition(RunPhase.INTERRUPTED)
    else:
        raise AgentLoopFailure(state, cancelled) from cancelled
    await recorder.commit(
        state,
        event_type="turn.cancelled" if state.phase is RunPhase.CANCELLED else "turn.interrupted",
        payload={
            "code": cancelled.reason.code.value,
            "message": cancelled.reason.message,
        },
        terminal=True,
    )
    return state


async def _failed(state: RunState, recorder: RunRecorder, cause: BaseException) -> RunState:
    if RunPhase.FAILED not in ALLOWED_PHASE_TRANSITIONS[state.phase]:
        raise AgentLoopFailure(state, cause) from cause
    state = state.transition(RunPhase.FAILED)
    await recorder.commit(
        state,
        event_type="turn.failed",
        payload={"errorType": type(cause).__name__, "message": str(cause)},
        terminal=True,
    )
    return state


@agent_loop_entrypoint
async def run_agent_loop(
    initial_state: RunState,
    *,
    planner: Planner,
    composer: Composer,
    tool_kernel: ToolKernel,
    recorder: RunRecorder,
    budget: BudgetLedger,
    cancellation: CancellationToken,
    now: Callable[[], Any],
) -> RunState:
    """Drive one root or child AgentRun through the single canonical state machine."""

    state = initial_state
    try:
        cancellation.checkpoint()
        state = await _commit_phase(state, RunPhase.LOADING_CONTEXT, recorder)
        state = await _commit_phase(state, RunPhase.SELECTING_MEMORY, recorder)
        state = await _commit_phase(state, RunPhase.PLANNING, recorder)

        while True:
            cancellation.checkpoint()
            await budget.enforce_wall_time(now=now())
            try:
                await budget.consume(BudgetDelta(model_rounds=1))
            except BudgetExceeded as exhausted:
                decision = evaluate_termination(state, reason=StopReason.BUDGET_EXHAUSTED)
                if decision.can_compose:
                    return await _compose(
                        state,
                        partial=True,
                        composer=composer,
                        recorder=recorder,
                        budget=budget,
                        cancellation=cancellation,
                    )
                return await _failed(state, recorder, exhausted)

            step = await planner.plan(state, cancellation)
            state = replace(state, model_rounds=state.model_rounds + 1, revision=state.revision + 1)
            await budget.consume(_usage_delta(step.usage))
            if step.requires_write_outcome:
                state = state.require_write_outcome("planner.requires_write_outcome")
                await recorder.commit(
                    state,
                    event_type="write.outcome_required",
                    payload={"reasons": state.write_obligation.reasons},
                )

            if not step.calls:
                decision = evaluate_termination(state, reason=StopReason.MODEL_FINISHED)
                if decision.can_compose:
                    return await _compose(
                        state,
                        partial=decision.partial,
                        composer=composer,
                        recorder=recorder,
                        budget=budget,
                        cancellation=cancellation,
                    )
                await recorder.commit(
                    state,
                    event_type="run.continuation_required",
                    payload={"blockers": decision.blockers},
                )
                continue

            await budget.consume(BudgetDelta(tool_calls=len(step.calls)))
            state = await _commit_phase(state, RunPhase.VALIDATING_CALLS, recorder)
            state = replace(
                state,
                pending=replace(state.pending, tool_call_ids=frozenset(call.tool_call_id for call in step.calls)),
                tool_calls=state.tool_calls + len(step.calls),
                revision=state.revision + 1,
            )
            await recorder.commit(
                state,
                event_type="tool.calls.accepted",
                payload={"toolCallIds": [call.tool_call_id for call in step.calls]},
            )
            state = await _commit_phase(state, RunPhase.CHECKING_POLICY, recorder)
            state = await _commit_phase(state, RunPhase.EXECUTING_TOOLS, recorder)
            executions = await tool_kernel.execute_batch(step.calls, cancellation)
            expected_ids = [call.tool_call_id for call in step.calls]
            actual_ids = [execution.call.tool_call_id for execution in executions]
            if actual_ids != expected_ids:
                raise ValueError(f"tool kernel result order mismatch: expected {expected_ids}, actual {actual_ids}")
            state = await _commit_phase(state, RunPhase.RECORDING_RESULTS, recorder)
            for execution in executions:
                state = state.record_tool_result(execution.definition, execution.result)
                await recorder.commit(
                    state,
                    event_type="tool.completed",
                    payload={
                        "toolCallId": execution.call.tool_call_id,
                        "status": execution.result.status.value,
                    },
                )
            state = await _commit_phase(state, RunPhase.PLANNING, recorder)
    except OperationCancelled as cancelled:
        return await _cancelled(state, recorder, cancelled)
    except AgentLoopFailure:
        raise
    except BaseException as cause:
        failed = await _failed(state, recorder, cause)
        raise AgentLoopFailure(failed, cause) from cause


__all__ = [
    "AgentLoopFailure",
    "RunRecorder",
    "ToolExecution",
    "ToolKernel",
    "agent_loop_entrypoint",
    "run_agent_loop",
]
