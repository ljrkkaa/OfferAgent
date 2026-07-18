from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Protocol, TypeVar, runtime_checkable

from offeragent_harness.error_codes import ErrorCode, PolicyDeniedCause
from offeragent_harness.hooks import HookDecision, HookEvent, HookExecutionContext, HookInvocation, HookOutcome
from offeragent_harness.models import ModelCitation, thaw_json
from offeragent_harness.permissions import ApprovalRequest, ApprovalResolution, ApprovalState
from offeragent_harness.ports import CancellationToken, HookLifecyclePort, OperationCancelled, ToolLifecycleObserver
from offeragent_harness.tools import ToolCall, ToolDefinition, ToolResult, ToolResultStatus

from .budgets import BudgetDelta, BudgetExceeded, BudgetLedger
from .context_manager import ContextBudgetExceeded, ContextCompactionRequired
from .model_planner import ModelProviderFailure
from .planner import AuditedPlanningFailure, Planner, PlanningAttempt
from .preparation import RunPreparationFailure, RunPreparationPort, safe_preparation_failure_details
from .state import ALLOWED_PHASE_TRANSITIONS, RunControlMessage, RunPhase, RunState
from .termination import evaluate_response_readiness

_ASSISTANT_EVENT_CHUNK_CHARACTERS = 256


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


@dataclass(frozen=True, slots=True)
class RecoveredToolBatch:
    """Exact accepted batch cursor used to resume the canonical Agent Loop."""

    accepted_tool_call_ids: tuple[str, ...]
    replay_calls: tuple[ToolCall, ...]

    def __post_init__(self) -> None:
        if not self.accepted_tool_call_ids:
            raise ValueError("recovered tool batch must identify the original accepted batch")
        if len(self.accepted_tool_call_ids) != len(set(self.accepted_tool_call_ids)):
            raise ValueError("recovered tool batch contains duplicate accepted ToolCall IDs")
        replay_ids = tuple(call.tool_call_id for call in self.replay_calls)
        if len(replay_ids) != len(set(replay_ids)):
            raise ValueError("recovered tool batch contains duplicate replay ToolCalls")
        if not set(replay_ids).issubset(self.accepted_tool_call_ids):
            raise ValueError("replay ToolCalls must belong to the original accepted batch")


class ToolKernel(Protocol):
    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]: ...


@runtime_checkable
class HookContextHintConsumer(Protocol):
    def set_hook_context_hints(self, hints: Sequence[str]) -> None: ...


class RunRecorder(Protocol):
    async def commit(
        self,
        state: RunState,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        terminal: bool = False,
    ) -> None: ...


class RunControlInbox(Protocol):
    async def drain(self) -> tuple[RunControlMessage, ...]: ...


async def _apply_run_controls(
    state: RunState,
    inbox: RunControlInbox | None,
    recorder: RunRecorder,
) -> tuple[RunState, bool]:
    if inbox is None:
        return state, False
    messages = await inbox.drain()
    changed = False
    for message in messages:
        updated = state.apply_control(message)
        if updated is state:
            continue
        state = updated
        changed = True
        await recorder.commit(
            state,
            event_type="turn.steered",
            payload={
                "messageId": message.message_id,
                "mode": message.mode,
                "input": [thaw_json(item) for item in message.input_blocks],
                "applyAfterSequence": message.apply_after_sequence,
            },
        )
    return state, changed


class AgentLoopFailure(RuntimeError):
    def __init__(self, state: RunState, cause: BaseException) -> None:
        super().__init__(f"agent loop failed in {state.phase.value}: {cause}")
        self.state = state
        self.__cause__ = cause


class AgentHookDenied(RuntimeError, PolicyDeniedCause):
    def __init__(self, event: HookEvent, decision: HookDecision) -> None:
        self.event = event
        self.decision = decision
        super().__init__(f"{event.value} Hook returned {decision.value}")


def _hosted_references(citations: Sequence[ModelCitation]) -> list[dict[str, object]]:
    references: dict[tuple[str, str, str, str, str], dict[str, object]] = {}
    for citation in citations:
        key = (citation.provider_id, citation.model, citation.request_id, citation.url, citation.title)
        references[key] = {
            "type": "hostedWeb",
            "url": citation.url,
            "title": citation.title,
            "providerId": citation.provider_id,
            "model": citation.model,
            "modelRequestId": citation.request_id,
            "freshness": "unknown",
        }
    return list(references.values())


def _text_content(text: str, citations: Sequence[ModelCitation] = ()) -> list[dict[str, object]]:
    if not text:
        return []
    return [{"type": "text", "text": text, "format": "markdown", "references": _hosted_references(citations)}]


def _cost_micros(cost: Decimal | None) -> int | None:
    if cost is None:
        return None
    return int(cost * 1_000_000)


async def _run_usage_payload(
    state: RunState,
    budget: BudgetLedger,
    *,
    now: Callable[[], Any],
) -> dict[str, int | None]:
    snapshot = await budget.snapshot(now=now())
    return {
        "inputTokens": snapshot.used.input_tokens,
        "outputTokens": snapshot.used.output_tokens,
        "cachedInputTokens": 0,
        "reasoningTokens": 0,
        "modelCalls": state.model_rounds,
        "toolCalls": state.tool_calls,
        "costMicros": _cost_micros(snapshot.used.cost),
        "wallTimeMs": max(0, int(snapshot.elapsed_seconds * 1_000)),
    }


def _failure_category(state: RunState, cause: BaseException) -> str:
    if isinstance(cause, BudgetExceeded):
        return "budget"
    if state.phase in {RunPhase.PLANNING, RunPhase.RESPONDING}:
        return "model"
    if state.phase in {
        RunPhase.VALIDATING_CALLS,
        RunPhase.CHECKING_POLICY,
        RunPhase.AWAITING_APPROVAL,
        RunPhase.EXECUTING_TOOLS,
        RunPhase.RECORDING_RESULTS,
    }:
        return "tool"
    return "runtime"


async def _invoke_agent_hook(
    hooks: HookLifecyclePort | None,
    hook_context: HookExecutionContext | None,
    state: RunState,
    event: HookEvent,
    sequence: str,
    cancellation: CancellationToken,
    facts: Mapping[str, Any],
) -> HookOutcome:
    if hooks is None:
        return HookOutcome.continue_without_hooks()
    if hook_context is None:
        raise ValueError("hook_context is required when Agent lifecycle Hooks are configured")
    if hook_context.workspace_id != state.workspace_id or hook_context.session_id != state.session_id:
        raise ValueError("Agent Hook context does not belong to the Run workspace/session")
    outcome = await hooks.invoke(
        HookInvocation(
            invocation_id=f"agent:{state.run_id}:{event.value}:{sequence}",
            chain_id=f"agent:{state.run_id}",
            event=event,
            context=hook_context,
            run_id=state.run_id,
            facts={"phase": state.phase.value, "revision": state.revision, **facts},
        ),
        cancellation,
    )
    if outcome.decision is not HookDecision.CONTINUE:
        raise AgentHookDenied(event, outcome.decision)
    return outcome


def _apply_hook_context_hints(target: object, outcome: HookOutcome) -> None:
    if isinstance(target, HookContextHintConsumer):
        target.set_hook_context_hints(outcome.context_hints)


_MODEL_PROVIDER_ERROR_CODES: Mapping[str, ErrorCode] = {
    "auth_required": ErrorCode.AUTH_REQUIRED,
    "auth_account_changed": ErrorCode.AUTH_REQUIRED,
    "insufficient_balance": ErrorCode.PROVIDER_UNREACHABLE,
    "provider_unreachable": ErrorCode.PROVIDER_UNREACHABLE,
    "provider_unavailable": ErrorCode.PROVIDER_UNREACHABLE,
    "provider_rate_limited": ErrorCode.PROVIDER_RATE_LIMITED,
    "provider_protocol_error": ErrorCode.PROVIDER_PROTOCOL_ERROR,
    "provider_response_failed": ErrorCode.PROVIDER_PROTOCOL_ERROR,
    "provider_http_error": ErrorCode.PROVIDER_PROTOCOL_ERROR,
    "provider_audit_unavailable": ErrorCode.PROVIDER_PROTOCOL_ERROR,
    "provider_internal_error": ErrorCode.PROVIDER_PROTOCOL_ERROR,
    "provider_cancelled": ErrorCode.REQUEST_CANCELLED,
    "context_overflow": ErrorCode.PROVIDER_CONTEXT_OVERFLOW,
    "provider_configuration": ErrorCode.PROVIDER_UNSUPPORTED,
    "model_unsupported": ErrorCode.PROVIDER_UNSUPPORTED,
    "image_unsupported": ErrorCode.PROVIDER_IMAGE_UNSUPPORTED,
    "image_invalid": ErrorCode.INPUT_IMAGE_INVALID,
}

_PROVIDER_PROTOCOL_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _error_payload(cause: BaseException, *, category: str, cancelled: bool = False) -> dict[str, object]:
    if isinstance(cause, BudgetExceeded):
        code = ErrorCode.REQUEST_DEADLINE_EXCEEDED
    elif isinstance(cause, (ContextBudgetExceeded, ContextCompactionRequired)):
        code = ErrorCode.PROVIDER_CONTEXT_OVERFLOW
    elif isinstance(cause, RunPreparationFailure):
        code = cause.error_code
    elif isinstance(cause, ModelProviderFailure):
        code = _MODEL_PROVIDER_ERROR_CODES.get(cause.error.code, ErrorCode.INTERNAL_ERROR)
    else:
        code = ErrorCode.INTERNAL_ERROR
    retryable = (
        cause.error.retryable
        if isinstance(cause, ModelProviderFailure)
        else isinstance(cause, RunPreparationFailure) and cause.retryable
    )
    details: dict[str, object] = {"errorType": type(cause).__name__, "failureCategory": category}
    if isinstance(cause, RunPreparationFailure):
        details["preparationErrorCode"] = cause.code
        details.update(safe_preparation_failure_details(cause))
    elif isinstance(cause, ModelProviderFailure):
        details["providerErrorCode"] = cause.error.code
        details["modelRequestId"] = cause.request_id
        protocol_reason = cause.error.details.get("protocolReason")
        if isinstance(protocol_reason, str) and _PROVIDER_PROTOCOL_REASON.fullmatch(protocol_reason):
            details["providerProtocolReason"] = protocol_reason
    return {
        "code": code.value,
        "retryable": retryable,
        "cancelled": cause.error.cancelled if isinstance(cause, ModelProviderFailure) else cancelled,
        "userVisibleMessage": (
            cause.error.message if isinstance(cause, ModelProviderFailure) else str(cause) or type(cause).__name__
        ),
        "details": details,
        "retryAfterMs": None,
        "traceId": None,
    }


def _tool_error_payload(result: ToolResult) -> dict[str, object] | None:
    if result.error is None:
        return None
    if result.status is ToolResultStatus.UNKNOWN_OUTCOME:
        code = ErrorCode.TOOL_UNKNOWN_OUTCOME
    elif result.status is ToolResultStatus.TIMED_OUT:
        code = ErrorCode.REQUEST_DEADLINE_EXCEEDED
    elif result.status is ToolResultStatus.CANCELLED:
        code = ErrorCode.REQUEST_CANCELLED
    elif result.status is ToolResultStatus.DENIED:
        code = ErrorCode.POLICY_DENIED
    else:
        code = ErrorCode.TOOL_FAILED
    return {
        "code": code.value,
        "retryable": result.error.retryable,
        "cancelled": result.error.cancelled,
        "userVisibleMessage": result.error.message,
        "details": {
            "toolErrorCode": result.error.code,
            "toolErrorDetails": thaw_json(result.error.details),
        },
        "retryAfterMs": None,
        "traceId": None,
    }


def _tool_result_payload(result: ToolResult) -> dict[str, object]:
    status = "conflict" if result.status is ToolResultStatus.CONFLICTED else result.status.value
    raw_data = thaw_json(result.data)
    data = dict(raw_data) if isinstance(raw_data, Mapping) else {"value": raw_data}
    descriptor: dict[str, object] = {
        "toolCallId": result.tool_call_id,
        "status": status,
        "summary": result.user_visible_summary,
        "data": data,
        "artifactRefs": [],
        "sourceRefs": [thaw_json(reference) for reference in result.source_references],
        "sideEffects": [],
        "retryable": result.retryable,
        "error": _tool_error_payload(result),
    }
    side_effect_facts = [
        {
            "kind": effect.kind.value,
            "state": effect.state.value,
            "resourceId": effect.resource_id,
            "beforeState": thaw_json(effect.before_state),
            "afterState": thaw_json(effect.after_state),
            "metadata": thaw_json(effect.metadata),
        }
        for effect in result.side_effects
    ]
    return {
        "result": descriptor,
        "artifactIds": list(result.artifact_ids),
        "sourceReferenceIds": list(result.source_refs),
        "sideEffectFacts": side_effect_facts,
    }


def _tool_result_event_type(result: ToolResult) -> str:
    failed_statuses = {
        ToolResultStatus.FAILED,
        ToolResultStatus.TIMED_OUT,
        ToolResultStatus.UNKNOWN_OUTCOME,
    }
    return "tool.failed" if result.status in failed_statuses else "tool.completed"


def _approval_tool_call(state: RunState, approval: ApprovalRequest) -> ToolCall:
    matches = [call for call in state.pending.tool_calls if call.tool_call_id == approval.tool_call_id]
    if len(matches) != 1:
        raise RuntimeError("approval must reference exactly one persisted pending ToolCall")
    return matches[0]


def _approval_descriptor(state: RunState, approval: ApprovalRequest) -> dict[str, object]:
    call = _approval_tool_call(state, approval)
    lineage = [*call.lineage.ancestor_run_ids, call.lineage.run_id]
    return {
        "approvalId": approval.approval_id,
        "status": "pending",
        "toolCall": {
            "toolCallId": call.tool_call_id,
            "name": call.name,
            "version": call.version,
            "arguments": thaw_json(call.arguments),
            "argsHash": call.args_hash,
            "idempotencyKey": call.idempotency_key,
            "risk": approval.risk.value,
            "reason": approval.summary,
            "agentLineage": lineage,
        },
        "workspaceId": approval.binding.workspace_id,
        "runId": approval.binding.run_id,
        "expectedStateHash": approval.binding.expected_state_hash,
        "expiresAt": approval.binding.expires_at.isoformat(),
        "diffArtifact": None,
        "includeDescendants": False,
    }


def _tool_call_descriptor(call: ToolCall, definition: ToolDefinition) -> dict[str, object]:
    lineage = [*call.lineage.ancestor_run_ids, call.lineage.run_id]
    return {
        "toolCallId": call.tool_call_id,
        "name": call.name,
        "version": call.version,
        "arguments": thaw_json(call.arguments),
        "argsHash": call.args_hash,
        "idempotencyKey": call.idempotency_key,
        "risk": definition.risk.value,
        "reason": None,
        "agentLineage": lineage,
        "workspaceId": call.workspace_id,
        "runId": call.run_id,
        "executorLocation": definition.executor_location.value,
        "definitionFingerprint": call.definition_fingerprint,
        "resultSensitivity": call.result_sensitivity.value,
        "deadline": call.deadline.isoformat() if call.deadline is not None else None,
    }


class _LoopToolLifecycleObserver:
    def __init__(self, state: RunState, recorder: RunRecorder) -> None:
        self.state = state
        self._recorder = recorder
        self._lock = asyncio.Lock()
        self._started_tool_call_ids: set[str] = set()

    async def call_replaced(self, original: ToolCall, replacement: ToolCall) -> None:
        """Keep approval/recovery state aligned with a schema-valid Hook mutation."""

        async with self._lock:
            if original.tool_call_id != replacement.tool_call_id:
                raise RuntimeError("Hook mutation cannot replace ToolCall identity")
            matches = [call for call in self.state.pending.tool_calls if call.tool_call_id == original.tool_call_id]
            if len(matches) != 1 or matches[0] != original:
                raise RuntimeError("Hook mutation has no matching pending ToolCall snapshot")
            pending = replace(
                self.state.pending,
                tool_calls=tuple(
                    replacement if call.tool_call_id == original.tool_call_id else call
                    for call in self.state.pending.tool_calls
                ),
            )
            self.state = replace(self.state, pending=pending, revision=self.state.revision + 1)

    async def required(self, approval: ApprovalRequest) -> None:
        async with self._lock:
            if self.state.phase in {
                RunPhase.CHECKING_POLICY,
                RunPhase.EXECUTING_TOOLS,
                RunPhase.RECORDING_RESULTS,
            }:
                self.state = await _commit_phase(self.state, RunPhase.AWAITING_APPROVAL, self._recorder)
            if self.state.phase is not RunPhase.AWAITING_APPROVAL:
                raise RuntimeError(f"approval requested from invalid phase {self.state.phase.value}")
            pending = replace(
                self.state.pending,
                approval_ids=self.state.pending.approval_ids | {approval.approval_id},
            )
            self.state = replace(self.state, pending=pending, revision=self.state.revision + 1)
            await self._recorder.commit(
                self.state,
                event_type="approval.required",
                payload={
                    "approval": _approval_descriptor(self.state, approval),
                    "explanation": approval.summary,
                    "diffArtifactIds": list(approval.diff_artifact_ids),
                },
            )

    async def resolved(self, approval: ApprovalRequest, resolution: ApprovalResolution) -> None:
        async with self._lock:
            if approval.approval_id not in self.state.pending.approval_ids:
                raise RuntimeError("approval resolution has no matching pending Run state")
            pending = replace(
                self.state.pending,
                approval_ids=self.state.pending.approval_ids - {approval.approval_id},
            )
            self.state = replace(self.state, pending=pending, revision=self.state.revision + 1)
            expired = resolution.state is ApprovalState.EXPIRED
            event_type = "approval.expired" if expired else "approval.resolved"
            if expired:
                payload: Mapping[str, Any] = {
                    "approvalId": resolution.approval_id,
                    "expiredAt": resolution.resolved_at.isoformat(),
                    "reason": resolution.reason or "approval expired before execution",
                    "resolverId": resolution.resolver_id,
                }
            else:
                decision_by_scope = {
                    "once": "allow_once",
                    "run": "allow_run",
                    "session": "allow_session",
                    "persistent": "allow_persistent",
                }
                decision = (
                    decision_by_scope[resolution.scope.value] if resolution.state is ApprovalState.APPROVED else "deny"
                )
                resolved_by = resolution.resolver_id if resolution.resolver_id in {"policy", "system"} else "user"
                payload = {
                    "approvalId": resolution.approval_id,
                    "decision": decision,
                    "scope": resolution.scope.value,
                    "resolvedAt": resolution.resolved_at.isoformat(),
                    "resolvedBy": resolved_by,
                    "status": resolution.state.value,
                    "resolverId": resolution.resolver_id,
                    "includeDescendants": resolution.include_descendants,
                    "reason": resolution.reason,
                }
            await self._recorder.commit(
                self.state,
                event_type=event_type,
                payload=payload,
            )

    async def execution_started(self, call: ToolCall, definition: ToolDefinition) -> None:
        async with self._lock:
            if call.tool_call_id in self._started_tool_call_ids:
                return
            pending = next(
                (item for item in self.state.pending.tool_calls if item.tool_call_id == call.tool_call_id),
                None,
            )
            if pending != call:
                raise RuntimeError("tool execution start has no matching pending ToolCall")
            if self.state.pending.approval_ids:
                raise RuntimeError("tool execution cannot start with pending approvals")
            if self.state.phase in {
                RunPhase.CHECKING_POLICY,
                RunPhase.AWAITING_APPROVAL,
                RunPhase.RECORDING_RESULTS,
            }:
                self.state = await _commit_phase(self.state, RunPhase.EXECUTING_TOOLS, self._recorder)
            elif self.state.phase is not RunPhase.EXECUTING_TOOLS:
                raise RuntimeError(f"tool execution started from invalid phase {self.state.phase.value}")
            self._started_tool_call_ids.add(call.tool_call_id)
            await self._recorder.commit(
                self.state,
                event_type="tool.started",
                payload={"call": _tool_call_descriptor(call, definition), "attempt": 1},
            )

    async def result_available(
        self,
        call: ToolCall,
        definition: ToolDefinition,
        result: ToolResult,
    ) -> None:
        async with self._lock:
            if call.tool_call_id != result.tool_call_id:
                raise RuntimeError("tool result callback identities differ")
            if call.tool_call_id not in self.state.pending.tool_call_ids:
                raise RuntimeError("tool result callback has no matching pending ToolCall")
            if self.state.phase in {
                RunPhase.CHECKING_POLICY,
                RunPhase.AWAITING_APPROVAL,
                RunPhase.EXECUTING_TOOLS,
            }:
                self.state = await _commit_phase(self.state, RunPhase.RECORDING_RESULTS, self._recorder)
            elif self.state.phase is not RunPhase.RECORDING_RESULTS:
                raise RuntimeError(f"tool result arrived from invalid phase {self.state.phase.value}")
            self.state = self.state.record_tool_result(definition, result)
            await self._recorder.commit(
                self.state,
                event_type=_tool_result_event_type(result),
                payload=_tool_result_payload(result),
            )


F = TypeVar("F", bound=Callable[..., Any])


def agent_loop_entrypoint(function: F) -> F:
    """Marks the one production Agent Loop entrypoint for architecture checks."""

    return function


async def _commit_phase(state: RunState, target: RunPhase, recorder: RunRecorder) -> RunState:
    previous = state.phase
    state = state.transition(target)
    await recorder.commit(
        state,
        event_type="phase.changed",
        payload={"previousPhase": previous.value, "phase": target.value, "reason": None},
    )
    return state


def _restore_planned_result_order(state: RunState, accepted_tool_call_ids: Sequence[str]) -> RunState:
    batch_ids = set(accepted_tool_call_ids)
    previous_results = [result for result in state.tool_results if result.tool_call_id not in batch_ids]
    batch_results = {result.tool_call_id: result for result in state.tool_results if result.tool_call_id in batch_ids}
    if set(batch_results) != batch_ids:
        raise RuntimeError("durable tool results do not match the accepted ToolCall batch")
    return replace(
        state,
        tool_results=tuple(
            [*previous_results, *(batch_results[tool_call_id] for tool_call_id in accepted_tool_call_ids)]
        ),
    )


async def _resume_recovered_tool_batch(
    state: RunState,
    recovered: RecoveredToolBatch,
    *,
    tool_kernel: ToolKernel,
    recorder: RunRecorder,
    cancellation: CancellationToken,
) -> RunState:
    allowed_phases = {
        RunPhase.VALIDATING_CALLS,
        RunPhase.CHECKING_POLICY,
        RunPhase.AWAITING_APPROVAL,
        RunPhase.EXECUTING_TOOLS,
        RunPhase.RECORDING_RESULTS,
    }
    if state.phase not in allowed_phases:
        raise ValueError(f"cannot resume a recovered tool batch from phase {state.phase.value}")
    if state.pending.child_run_ids:
        raise ValueError("recovered tool batch cannot bypass pending child Run coordination")
    if state.pending.tool_calls != recovered.replay_calls:
        raise ValueError("recovered replay calls must exactly match the persisted pending ToolCalls")
    if state.pending.tool_call_ids != frozenset(call.tool_call_id for call in recovered.replay_calls):
        raise ValueError("recovered replay call identities do not match PendingWork")

    if state.phase is RunPhase.VALIDATING_CALLS:
        state = await _commit_phase(state, RunPhase.CHECKING_POLICY, recorder)

    if recovered.replay_calls:
        observer = _LoopToolLifecycleObserver(state, recorder)
        executions = await tool_kernel.execute_batch(recovered.replay_calls, cancellation, observer)
        expected_ids = [call.tool_call_id for call in recovered.replay_calls]
        actual_ids = [execution.call.tool_call_id for execution in executions]
        if actual_ids != expected_ids:
            raise ValueError(f"tool kernel result order mismatch: expected {expected_ids}, actual {actual_ids}")
        state = observer.state

    if state.pending.tool_call_ids or state.pending.approval_ids:
        raise RuntimeError("recovered ToolKernel returned before every replay result and approval was durably observed")
    if state.phase is not RunPhase.RECORDING_RESULTS:
        state = await _commit_phase(state, RunPhase.RECORDING_RESULTS, recorder)
    state = _restore_planned_result_order(state, recovered.accepted_tool_call_ids)
    return await _commit_phase(state, RunPhase.PLANNING, recorder)


async def _complete_with_response(
    state: RunState,
    *,
    response: str,
    recorder: RunRecorder,
    budget: BudgetLedger,
    cancellation: CancellationToken,
    now: Callable[[], Any],
    hooks: HookLifecyclePort | None,
    hook_context: HookExecutionContext | None,
    citations: Sequence[ModelCitation] = (),
) -> RunState:
    cancellation.checkpoint()
    if not response.strip():
        raise ValueError("a completed Agent step requires a non-empty final response")
    state = await _commit_phase(state, RunPhase.RESPONDING, recorder)
    offset = 0
    while offset < len(response):
        cancellation.checkpoint()
        delta = response[offset : offset + _ASSISTANT_EVENT_CHUNK_CHARACTERS]
        await recorder.commit(
            state,
            event_type="assistant.delta",
            payload={"blockIndex": 0, "offset": offset, "delta": delta},
        )
        offset += len(delta)
    state = replace(state, assistant_text=response, revision=state.revision + 1)
    references = _hosted_references(citations)
    if references:
        await recorder.commit(
            state,
            event_type="references.updated",
            payload={"references": references, "replace": False},
        )
    await recorder.commit(
        state,
        event_type="assistant.completed",
        payload={
            "content": _text_content(state.assistant_text, citations),
            "finishReason": "stop",
        },
    )
    await _invoke_agent_hook(
        hooks,
        hook_context,
        state,
        HookEvent.TURN_STOP,
        "completed",
        cancellation,
        {"reason": "completed"},
    )
    state = await _commit_phase(state, RunPhase.PERSISTING, recorder)
    state = state.transition(RunPhase.COMPLETED)
    await recorder.commit(
        state,
        event_type="turn.completed",
        payload={
            "reason": "completed",
            "assistantContent": _text_content(state.assistant_text, citations),
            "usage": await _run_usage_payload(state, budget, now=now),
        },
        terminal=True,
    )
    return state


async def _record_planning_attempts(
    state: RunState,
    attempts: Sequence[PlanningAttempt],
    recorder: RunRecorder,
) -> RunState:
    for attempt in attempts:
        state = replace(state, model_rounds=state.model_rounds + 1, revision=state.revision + 1)
        usage = attempt.usage
        await recorder.commit(
            state,
            event_type="model.attempt",
            payload={
                "requestId": attempt.request_id,
                "repairIndex": attempt.repair_index,
                "outcome": attempt.outcome.value,
                "errorCode": attempt.error_code,
                "violations": list(attempt.violations),
                "retryOfRequestId": attempt.retry_of_request_id,
                "projection": attempt.projection,
                "projectionHash": attempt.projection_hash,
                "omittedContextIds": list(attempt.omitted_context_ids),
                "usage": (
                    None
                    if usage is None
                    else {
                        "inputTokens": usage.input_tokens,
                        "outputTokens": usage.output_tokens,
                        "cachedInputTokens": usage.cached_input_tokens,
                        "reasoningTokens": usage.reasoning_tokens,
                        "cost": None if usage.cost is None else format(usage.cost, "f"),
                        "currency": usage.currency,
                    }
                ),
            },
        )
    return state


async def _cancelled(
    state: RunState,
    recorder: RunRecorder,
    cancelled: OperationCancelled,
    *,
    budget: BudgetLedger,
    now: Callable[[], Any],
) -> RunState:
    code = cancelled.reason.code.value
    if code == "deadline":
        if RunPhase.FAILED not in ALLOWED_PHASE_TRANSITIONS[state.phase]:
            raise AgentLoopFailure(state, cancelled) from cancelled
        state = state.transition(RunPhase.FAILED)
        await recorder.commit(
            state,
            event_type="turn.failed",
            payload={
                "error": {
                    "code": ErrorCode.REQUEST_DEADLINE_EXCEEDED.value,
                    "retryable": False,
                    "cancelled": True,
                    "userVisibleMessage": cancelled.reason.message,
                    "details": {"cancellationCode": code, "failureCategory": "budget"},
                    "retryAfterMs": None,
                    "traceId": None,
                },
                "usage": await _run_usage_payload(state, budget, now=now),
                "partialContent": _text_content(state.assistant_text),
            },
            terminal=True,
        )
        return state
    should_interrupt = code in {"shutdown", "start_failed"} or (code == "parent" and state.lineage.depth == 0)
    target = RunPhase.INTERRUPTED if should_interrupt else RunPhase.CANCELLED
    if target in ALLOWED_PHASE_TRANSITIONS[state.phase]:
        state = state.transition(target)
    elif RunPhase.CANCELLING in ALLOWED_PHASE_TRANSITIONS[state.phase]:
        state = await _commit_phase(state, RunPhase.CANCELLING, recorder)
        if target not in ALLOWED_PHASE_TRANSITIONS[state.phase]:
            raise AgentLoopFailure(state, cancelled) from cancelled
        state = state.transition(target)
    else:
        raise AgentLoopFailure(state, cancelled) from cancelled
    usage = await _run_usage_payload(state, budget, now=now)
    if state.phase is RunPhase.CANCELLED:
        event_type = "turn.cancelled"
        payload: Mapping[str, Any] = {
            "reason": cancelled.reason.message,
            "code": cancelled.reason.code.value,
            "usage": usage,
            "partialContent": _text_content(state.assistant_text),
        }
    else:
        event_type = "turn.interrupted"
        payload = {
            "error": {
                "code": ErrorCode.RUNTIME_INTERRUPTED.value,
                "retryable": True,
                "cancelled": True,
                "userVisibleMessage": cancelled.reason.message,
                "details": {"cancellationCode": cancelled.reason.code.value},
                "retryAfterMs": None,
                "traceId": None,
            },
            "usage": usage,
            "partialContent": _text_content(state.assistant_text),
            "safeCheckpointAvailable": state.pending.empty,
        }
    await recorder.commit(state, event_type=event_type, payload=payload, terminal=True)
    return state


async def _native_interrupted(
    state: RunState,
    recorder: RunRecorder,
    cause: asyncio.CancelledError,
    *,
    budget: BudgetLedger,
    now: Callable[[], Any],
) -> RunState:
    if RunPhase.INTERRUPTED in ALLOWED_PHASE_TRANSITIONS[state.phase]:
        state = state.transition(RunPhase.INTERRUPTED)
    elif RunPhase.CANCELLING in ALLOWED_PHASE_TRANSITIONS[state.phase]:
        state = await _commit_phase(state, RunPhase.CANCELLING, recorder)
        state = state.transition(RunPhase.INTERRUPTED)
    else:
        raise AgentLoopFailure(state, cause) from cause
    await recorder.commit(
        state,
        event_type="turn.interrupted",
        payload={
            "error": {
                "code": ErrorCode.RUNTIME_INTERRUPTED.value,
                "retryable": True,
                "cancelled": True,
                "userVisibleMessage": "run task was interrupted by runtime shutdown",
                "details": {"errorType": type(cause).__name__, "failureCategory": "runtime"},
                "retryAfterMs": None,
                "traceId": None,
            },
            "usage": await _run_usage_payload(state, budget, now=now),
            "partialContent": _text_content(state.assistant_text),
            "safeCheckpointAvailable": state.pending.empty,
        },
        terminal=True,
    )
    return state


async def _failed(
    state: RunState,
    recorder: RunRecorder,
    cause: BaseException,
    *,
    budget: BudgetLedger,
    now: Callable[[], Any],
) -> RunState:
    if RunPhase.FAILED not in ALLOWED_PHASE_TRANSITIONS[state.phase]:
        raise AgentLoopFailure(state, cause) from cause
    category = _failure_category(state, cause)
    state = state.transition(RunPhase.FAILED)
    await recorder.commit(
        state,
        event_type="turn.failed",
        payload={
            "error": _error_payload(cause, category=category),
            "usage": await _run_usage_payload(state, budget, now=now),
            "partialContent": _text_content(state.assistant_text),
        },
        terminal=True,
    )
    return state


@agent_loop_entrypoint
async def run_agent_loop(
    initial_state: RunState,
    *,
    planner: Planner,
    tool_kernel: ToolKernel,
    recorder: RunRecorder,
    budget: BudgetLedger,
    cancellation: CancellationToken,
    now: Callable[[], Any],
    recovered_tool_batch: RecoveredToolBatch | None = None,
    hooks: HookLifecyclePort | None = None,
    hook_context: HookExecutionContext | None = None,
    control_inbox: RunControlInbox | None = None,
    run_preparation: RunPreparationPort | None = None,
    required_root_initial_tool: str | None = None,
) -> RunState:
    """Drive one root or child AgentRun through the single canonical state machine."""

    state = initial_state
    if required_root_initial_tool is not None and (
        not required_root_initial_tool.strip() or len(required_root_initial_tool) > 256
    ):
        raise ValueError("required root initial Tool name must be a non-empty bounded string")
    initial_tool_pending = required_root_initial_tool is not None and state.lineage.depth == 0
    if hooks is not None and hook_context is None:
        raise ValueError("hook_context is required when Agent lifecycle Hooks are configured")
    planning_in_flight = False
    if recovered_tool_batch is None:
        if state.phase is not RunPhase.CREATED:
            raise ValueError("a fresh Agent Loop must start from the created phase")
    else:
        checkpoint = state.budget_checkpoint
        if checkpoint is None:
            raise ValueError("a recovered Agent Loop requires a durable budget checkpoint")
        snapshot = await budget.snapshot(now=checkpoint.captured_at)
        if (
            budget.budget != checkpoint.budget
            or budget.started_at != checkpoint.started_at
            or snapshot.used != checkpoint.used
            or snapshot.reserved != checkpoint.reserved
            or snapshot.elapsed_seconds != checkpoint.elapsed_seconds
        ):
            raise ValueError("recovered Agent Loop budget ledger does not match its durable checkpoint")
    try:
        cancellation.checkpoint()
        await _invoke_agent_hook(
            hooks,
            hook_context,
            state,
            HookEvent.TURN_START,
            "start",
            cancellation,
            {"recovered": recovered_tool_batch is not None},
        )
        if recovered_tool_batch is None:
            state = await _commit_phase(state, RunPhase.LOADING_CONTEXT, recorder)
            if run_preparation is not None:
                await run_preparation.prepare(state, RunPhase.LOADING_CONTEXT, cancellation)
            state = await _commit_phase(state, RunPhase.SELECTING_MEMORY, recorder)
            if run_preparation is not None:
                await run_preparation.prepare(state, RunPhase.SELECTING_MEMORY, cancellation)
            state = await _commit_phase(state, RunPhase.PLANNING, recorder)
        else:
            if run_preparation is not None:
                await run_preparation.prepare(state, RunPhase.LOADING_CONTEXT, cancellation)
                await run_preparation.prepare(state, RunPhase.SELECTING_MEMORY, cancellation)
            state = await _resume_recovered_tool_batch(
                state,
                recovered_tool_batch,
                tool_kernel=tool_kernel,
                recorder=recorder,
                cancellation=cancellation,
            )

        while True:
            cancellation.checkpoint()
            state, _ = await _apply_run_controls(state, control_inbox, recorder)
            await budget.enforce_wall_time(now=now())
            try:
                await budget.consume(BudgetDelta(model_rounds=1))
            except BudgetExceeded as exhausted:
                return await _failed(state, recorder, exhausted, budget=budget, now=now)

            planning_in_flight = True
            planning_hook_sequence = f"planner-{state.model_rounds + 1}"
            before_model = await _invoke_agent_hook(
                hooks,
                hook_context,
                state,
                HookEvent.BEFORE_MODEL,
                planning_hook_sequence,
                cancellation,
                {"purpose": "planner"},
            )
            _apply_hook_context_hints(planner, before_model)
            step = await planner.plan(state, cancellation)
            await _invoke_agent_hook(
                hooks,
                hook_context,
                state,
                HookEvent.AFTER_MODEL,
                planning_hook_sequence,
                cancellation,
                {"purpose": "planner", "status": "succeeded", "toolCallCount": len(step.calls)},
            )
            planning_in_flight = False
            if not step.attempts:
                raise ValueError("planner returned a step without an auditable model attempt")
            state = await _record_planning_attempts(state, step.attempts, recorder)
            state, steered_while_planning = await _apply_run_controls(state, control_inbox, recorder)
            if steered_while_planning:
                # Discard decisions made before the newly accepted control
                # message became visible. Effectful tools are never interrupted.
                continue
            if initial_tool_pending and (len(step.calls) != 1 or step.calls[0].name != required_root_initial_tool):
                await recorder.commit(
                    state,
                    event_type="run.continuation_required",
                    payload={"blockers": [f"required_initial_tool:{required_root_initial_tool}"]},
                )
                continue
            if step.requires_write_outcome:
                state = state.require_write_outcome("planner.requires_write_outcome")
                await recorder.commit(
                    state,
                    event_type="write.outcome_required",
                    payload={"reasons": list(state.write_obligation.reasons)},
                )

            if not step.calls:
                decision = evaluate_response_readiness(state)
                if decision.can_respond:
                    assert step.final_response is not None
                    return await _complete_with_response(
                        state,
                        response=step.final_response,
                        recorder=recorder,
                        budget=budget,
                        cancellation=cancellation,
                        now=now,
                        hooks=hooks,
                        hook_context=hook_context,
                        citations=step.citations,
                    )
                await recorder.commit(
                    state,
                    event_type="run.continuation_required",
                    payload={"blockers": list(decision.blockers)},
                )
                continue

            await budget.consume(BudgetDelta(tool_calls=len(step.calls)))
            state = await _commit_phase(state, RunPhase.VALIDATING_CALLS, recorder)
            state = state.accept_tool_calls(tuple(step.calls))
            await recorder.commit(
                state,
                event_type="tool.calls.accepted",
                payload={
                    "calls": [
                        {
                            "toolCallId": call.tool_call_id,
                            "name": call.name,
                            "version": call.version,
                            "arguments": thaw_json(call.arguments),
                            "argsHash": call.args_hash,
                            "definitionFingerprint": call.definition_fingerprint,
                            "resultSensitivity": call.result_sensitivity.value,
                            "idempotencyKey": call.idempotency_key,
                            "deadline": None if call.deadline is None else call.deadline.isoformat(),
                            "lineage": {
                                "rootRunId": call.lineage.root_run_id,
                                "runId": call.lineage.run_id,
                                "parentRunId": call.lineage.parent_run_id,
                                "ancestorRunIds": list(call.lineage.ancestor_run_ids),
                                "depth": call.lineage.depth,
                                "agentName": call.lineage.agent_name,
                            },
                        }
                        for call in step.calls
                    ]
                },
            )
            state = await _commit_phase(state, RunPhase.CHECKING_POLICY, recorder)
            tool_observer = _LoopToolLifecycleObserver(state, recorder)
            executions = await tool_kernel.execute_batch(step.calls, cancellation, tool_observer)
            expected_ids = [call.tool_call_id for call in step.calls]
            actual_ids = [execution.call.tool_call_id for execution in executions]
            if actual_ids != expected_ids:
                raise ValueError(f"tool kernel result order mismatch: expected {expected_ids}, actual {actual_ids}")
            state = tool_observer.state
            if state.phase is not RunPhase.RECORDING_RESULTS or state.pending.tool_call_ids:
                raise RuntimeError("ToolKernel returned before every result was durably observed")
            state = _restore_planned_result_order(state, expected_ids)
            if initial_tool_pending and executions[0].result.status is ToolResultStatus.SUCCEEDED:
                initial_tool_pending = False
            state = await _commit_phase(state, RunPhase.PLANNING, recorder)
    except OperationCancelled as cancelled:
        return await _cancelled(state, recorder, cancelled, budget=budget, now=now)
    except asyncio.CancelledError as cancelled:
        return await _native_interrupted(state, recorder, cancelled, budget=budget, now=now)
    except AgentLoopFailure:
        raise
    except BaseException as cause:
        if planning_in_flight:
            if isinstance(cause, AuditedPlanningFailure) and cause.planning_attempts:
                state = await _record_planning_attempts(state, cause.planning_attempts, recorder)
        failed = await _failed(state, recorder, cause, budget=budget, now=now)
        raise AgentLoopFailure(failed, cause) from cause


__all__ = [
    "AgentHookDenied",
    "AgentLoopFailure",
    "RunRecorder",
    "ToolExecution",
    "ToolKernel",
    "agent_loop_entrypoint",
    "run_agent_loop",
]
