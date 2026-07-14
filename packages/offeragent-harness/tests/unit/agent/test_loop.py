from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offeragent_harness.agent.budget_checkpoint import BudgetCheckpoint
from offeragent_harness.agent.budgets import BudgetDelta, BudgetLedger, RunBudget
from offeragent_harness.agent.composer import CompositionEvent, CompositionRetry
from offeragent_harness.agent.loop import AgentLoopFailure, RecoveredToolBatch, ToolExecution, run_agent_loop
from offeragent_harness.agent.model_planner import ModelProviderFailure
from offeragent_harness.agent.planner import PlanningStep
from offeragent_harness.agent.state import PendingWork, RunPhase, RunState
from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.hooks import HookEvent, HookExecutionContext, HookInvocation, HookOutcome
from offeragent_harness.models import ModelError, ModelUsage
from offeragent_harness.permissions import (
    ApprovalBinding,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalScope,
    ApprovalState,
    RiskClass,
)
from offeragent_harness.ports import CancellationToken, ToolLifecycleObserver
from offeragent_harness.protocol.errors import ErrorEnvelope
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
    ToolError,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
)


class ScriptedPlanner:
    def __init__(self, steps: list[PlanningStep]) -> None:
        self.steps = steps

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        if not self.steps:
            raise AssertionError("unexpected planner call")
        return self.steps.pop(0)


class ProviderFailingPlanner:
    def __init__(self, error: ModelError) -> None:
        self.error = error

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        del state
        cancellation.checkpoint()
        raise ModelProviderFailure("model-request-planner", self.error)


class TextComposer:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    async def stream(
        self,
        state: RunState,
        *,
        partial: bool,
        cancellation: CancellationToken,
    ) -> AsyncIterator[CompositionEvent]:
        self.calls.append(partial)
        cancellation.checkpoint()
        yield CompositionEvent(text_delta="done")
        yield CompositionEvent(
            usage=ModelUsage(
                input_tokens=1,
                output_tokens=1,
                cached_input_tokens=0,
                reasoning_tokens=0,
                cost=Decimal("0"),
                currency="USD",
            )
        )


class ProviderFailingComposer:
    def __init__(self, error: ModelError) -> None:
        self.error = error

    async def stream(
        self,
        state: RunState,
        *,
        partial: bool,
        cancellation: CancellationToken,
    ) -> AsyncIterator[CompositionEvent]:
        del state, partial
        cancellation.checkpoint()
        if False:  # pragma: no cover - marks this coroutine as an async iterator
            yield CompositionEvent(text_delta="unreachable")
        raise ModelProviderFailure("model-request-composer", self.error)


class ContextOverflowRetryComposer:
    async def stream(
        self,
        state: RunState,
        *,
        partial: bool,
        cancellation: CancellationToken,
    ) -> AsyncIterator[CompositionEvent]:
        del state, partial
        cancellation.checkpoint()
        yield CompositionEvent(usage=ModelUsage(2, 1, 0, 0))
        yield CompositionEvent(retry=CompositionRetry("request-retry", "request-first", "overflow_references"))
        cancellation.checkpoint()
        yield CompositionEvent(text_delta="done after retry")
        yield CompositionEvent(usage=ModelUsage(3, 2, 0, 0))


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, bool]] = []
        self.payloads: list[tuple[str, Mapping[str, object]]] = []
        self.terminal = False

    async def commit(
        self,
        state: RunState,
        *,
        event_type: str,
        payload: Mapping[str, object],
        terminal: bool = False,
    ) -> None:
        if terminal:
            assert not self.terminal
            self.terminal = True
        self.events.append((event_type, terminal))
        self.payloads.append((event_type, payload))


class RecordingPreparation:
    def __init__(self) -> None:
        self.calls: list[tuple[RunPhase, RunPhase]] = []

    async def prepare(
        self,
        state: RunState,
        phase: RunPhase,
        cancellation: CancellationToken,
    ) -> None:
        cancellation.checkpoint()
        self.calls.append((phase, state.phase))


class Kernel:
    def __init__(self, execution: ToolExecution | None = None) -> None:
        self.execution = execution
        self.saw_accepted_event = False
        self.recorder: Recorder | None = None

    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        cancellation.checkpoint()
        if observer is not None:
            await observer.execution_started()
        assert self.recorder is not None
        self.saw_accepted_event = ("tool.calls.accepted", False) in self.recorder.events
        if self.execution is None:
            raise AssertionError("unexpected tool execution")
        if observer is not None:
            await observer.result_available(
                self.execution.call,
                self.execution.definition,
                self.execution.result,
            )
        return (self.execution,)


class RecordingHooks:
    def __init__(self) -> None:
        self.invocations: list[HookInvocation] = []

    async def invoke(self, invocation: HookInvocation, cancellation: CancellationToken) -> HookOutcome:
        cancellation.checkpoint()
        self.invocations.append(invocation)
        return HookOutcome.continue_without_hooks()


class HintHooks(RecordingHooks):
    async def invoke(self, invocation: HookInvocation, cancellation: CancellationToken) -> HookOutcome:
        cancellation.checkpoint()
        self.invocations.append(invocation)
        if invocation.event is HookEvent.BEFORE_MODEL:
            return HookOutcome(
                decision=HookOutcome.continue_without_hooks().decision,
                audit_tags=(),
                context_hints=(f"hint:{invocation.facts['purpose']}",),
                applied_hook_ids=("hint",),
                warning_codes=(),
            )
        return HookOutcome.continue_without_hooks()


class HintPlanner(ScriptedPlanner):
    def __init__(self, steps: list[PlanningStep]) -> None:
        super().__init__(steps)
        self.hints: tuple[str, ...] = ()

    def set_hook_context_hints(self, hints: Sequence[str]) -> None:
        self.hints = tuple(hints)


class HintComposer(TextComposer):
    def __init__(self) -> None:
        super().__init__()
        self.hints: tuple[str, ...] = ()

    def set_hook_context_hints(self, hints: Sequence[str]) -> None:
        self.hints = tuple(hints)


class ApprovalKernel:
    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        assert observer is not None
        tool_call = calls[0]
        now = datetime.now(timezone.utc)
        request = ApprovalRequest(
            approval_id="approval_1",
            tool_call_id=tool_call.tool_call_id,
            binding=ApprovalBinding(
                tool_name=tool_call.name,
                tool_version=tool_call.version,
                definition_fingerprint=tool_call.definition_fingerprint,
                args_hash=tool_call.args_hash,
                workspace_id=tool_call.workspace_id,
                session_id="session",
                principal_id="principal",
                root_run_id=tool_call.lineage.root_run_id,
                run_id=tool_call.run_id,
                agent_name=tool_call.lineage.agent_name,
                ancestor_run_ids=tool_call.lineage.ancestor_run_ids,
                expected_state_hash=None,
                expires_at=now + timedelta(minutes=1),
            ),
            risk=RiskClass.WRITE,
            summary="approve write",
            diff_artifact_ids=("artifact_diff",),
        )
        await observer.required(request)
        await observer.resolved(
            request,
            ApprovalResolution(
                approval_id=request.approval_id,
                state=ApprovalState.APPROVED,
                scope=ApprovalScope.ONCE,
                resolved_at=now,
                resolver_id="user",
                include_descendants=False,
            ),
        )
        await observer.execution_started()
        execution = denied_execution()
        await observer.result_available(execution.call, execution.definition, execution.result)
        return (execution,)


def run_state() -> RunState:
    return RunState("ws", "session", "turn", "run", AgentLineage.root("run"))


def run_budget(*, rounds: int = 8) -> BudgetLedger:
    return BudgetLedger(
        RunBudget(
            max_model_rounds=rounds,
            max_tool_calls=10,
            max_parallel_reads=4,
            max_wall_seconds=60,
            max_input_tokens=100,
            max_output_tokens=100,
            max_cost=Decimal("1"),
            max_artifact_bytes=1_000,
            max_subagents=2,
        ),
        started_at=datetime.now(timezone.utc),
    )


def write_call(*, tool_call_id: str = "call-write", idempotency_key: str = "idem-write") -> ToolCall:
    arguments: dict[str, list[object]] = {"operations": []}
    definition = write_definition()
    return ToolCall(
        tool_call_id=tool_call_id,
        run_id="run",
        workspace_id="ws",
        name="vault.transaction",
        version="1",
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        definition_fingerprint=definition.fingerprint,
        idempotency_key=idempotency_key,
        deadline=None,
        lineage=AgentLineage.root("run"),
        result_sensitivity=definition.result_sensitivity,
    )


def write_definition() -> ToolDefinition:
    return ToolDefinition(
        name="vault.transaction",
        version="1",
        description="write",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {"operations": {"type": "array"}},
            "required": ["operations"],
            "additionalProperties": False,
        },
        output_schema={},
        executor_location=ExecutorLocation.CLIENT,
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        required_capabilities=frozenset({"vault.write"}),
        concurrency_safe=False,
        idempotent=True,
        retryable=False,
        timeout_ms=30_000,
        output_limit_bytes=1_024,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def denied_execution() -> ToolExecution:
    call = write_call()
    result = ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.DENIED,
        data=None,
        user_visible_summary="user denied",
        artifact_ids=(),
        source_refs=(),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state=None,
        error=ToolError("approval_denied", "user denied", False, False),
    )
    return ToolExecution(call, write_definition(), result)


def successful_execution(call: ToolCall) -> ToolExecution:
    return ToolExecution(
        call,
        write_definition(),
        ToolResult(
            tool_call_id=call.tool_call_id,
            status=ToolResultStatus.SUCCEEDED,
            data={"ok": True},
            user_visible_summary=f"completed {call.tool_call_id}",
            artifact_ids=(),
            source_refs=(),
            side_effects=(),
            retryable=False,
            before_state=None,
            after_state=None,
            error=None,
        ),
    )


class MidBatchApprovalKernel:
    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        del cancellation
        assert observer is not None
        first, second = calls
        first_execution = successful_execution(first)
        second_execution = successful_execution(second)
        await observer.execution_started()
        await observer.result_available(first, first_execution.definition, first_execution.result)
        now = datetime.now(timezone.utc)
        request = ApprovalRequest(
            approval_id="apr_2",
            tool_call_id=second.tool_call_id,
            binding=ApprovalBinding(
                tool_name=second.name,
                tool_version=second.version,
                definition_fingerprint=second.definition_fingerprint,
                args_hash=second.args_hash,
                workspace_id=second.workspace_id,
                session_id="session",
                principal_id="principal",
                root_run_id=second.lineage.root_run_id,
                run_id=second.run_id,
                agent_name=second.lineage.agent_name,
                ancestor_run_ids=second.lineage.ancestor_run_ids,
                expected_state_hash=None,
                expires_at=now + timedelta(minutes=1),
            ),
            risk=RiskClass.WRITE,
            summary="second write now requires approval",
            diff_artifact_ids=(),
        )
        await observer.required(request)
        await observer.resolved(
            request,
            ApprovalResolution(
                approval_id=request.approval_id,
                state=ApprovalState.APPROVED,
                scope=ApprovalScope.ONCE,
                resolved_at=now,
                resolver_id="user",
                include_descendants=False,
            ),
        )
        await observer.execution_started()
        await observer.result_available(second, second_execution.definition, second_execution.result)
        return (first_execution, second_execution)


class OutOfOrderResultKernel:
    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        del cancellation
        assert observer is not None
        executions = tuple(successful_execution(call) for call in calls)
        await observer.execution_started()
        for execution in reversed(executions):
            await observer.result_available(execution.call, execution.definition, execution.result)
        return executions


@pytest.mark.asyncio
async def test_no_tool_turn_streams_and_commits_one_terminal_event() -> None:
    planner = ScriptedPlanner([PlanningStep((), False, "done")])
    composer = TextComposer()
    recorder = Recorder()
    kernel = Kernel()
    kernel.recorder = recorder
    preparation = RecordingPreparation()

    result = await run_agent_loop(
        run_state(),
        planner=planner,
        composer=composer,
        tool_kernel=kernel,
        recorder=recorder,
        budget=run_budget(),
        cancellation=CancellationScope(name="run"),
        now=lambda: datetime.now(timezone.utc),
        run_preparation=preparation,
    )

    assert result.phase is RunPhase.COMPLETED
    assert result.assistant_text == "done"
    assert recorder.events[-1] == ("turn.completed", True)
    assert preparation.calls == [
        (RunPhase.LOADING_CONTEXT, RunPhase.LOADING_CONTEXT),
        (RunPhase.SELECTING_MEMORY, RunPhase.SELECTING_MEMORY),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_code", "expected_code", "retryable"),
    [
        ("auth_required", ErrorCode.AUTH_REQUIRED, False),
        ("provider_unreachable", ErrorCode.PROVIDER_UNREACHABLE, True),
        ("provider_unavailable", ErrorCode.PROVIDER_UNREACHABLE, True),
        ("provider_rate_limited", ErrorCode.PROVIDER_UNREACHABLE, True),
        ("context_overflow", ErrorCode.PROVIDER_CONTEXT_OVERFLOW, False),
        ("provider_configuration", ErrorCode.PROVIDER_UNSUPPORTED, False),
        ("model_unsupported", ErrorCode.PROVIDER_UNSUPPORTED, False),
    ],
)
async def test_planner_provider_failure_preserves_typed_error_envelope(
    provider_code: str,
    expected_code: ErrorCode,
    retryable: bool,
) -> None:
    recorder = Recorder()
    kernel = Kernel()
    kernel.recorder = recorder

    with pytest.raises(AgentLoopFailure) as caught:
        await run_agent_loop(
            run_state(),
            planner=ProviderFailingPlanner(ModelError(provider_code, "模型 Provider 请求失败。", retryable, False)),
            composer=TextComposer(),
            tool_kernel=kernel,
            recorder=recorder,
            budget=run_budget(),
            cancellation=CancellationScope(name="provider-failure-planner"),
            now=lambda: datetime.now(timezone.utc),
        )

    terminal_payload = next(payload for event_type, payload in recorder.payloads if event_type == "turn.failed")
    envelope = ErrorEnvelope.model_validate_json(json.dumps(terminal_payload["error"], ensure_ascii=False))
    assert caught.value.state.phase is RunPhase.FAILED
    assert envelope.code is expected_code
    assert envelope.retryable is retryable
    assert envelope.cancelled is False
    assert envelope.user_visible_message == "模型 Provider 请求失败。"
    assert envelope.details == {
        "errorType": "ModelProviderFailure",
        "failureCategory": "model",
        "providerErrorCode": provider_code,
        "modelRequestId": "model-request-planner",
    }


@pytest.mark.asyncio
async def test_composer_provider_failure_uses_the_same_typed_error_mapping() -> None:
    recorder = Recorder()
    kernel = Kernel()
    kernel.recorder = recorder

    with pytest.raises(AgentLoopFailure):
        await run_agent_loop(
            run_state(),
            planner=ScriptedPlanner([PlanningStep((), False, "done")]),
            composer=ProviderFailingComposer(
                ModelError("provider_rate_limited", "模型 Provider 当前限流。", True, False)
            ),
            tool_kernel=kernel,
            recorder=recorder,
            budget=run_budget(),
            cancellation=CancellationScope(name="provider-failure-composer"),
            now=lambda: datetime.now(timezone.utc),
        )

    terminal_payload = next(payload for event_type, payload in recorder.payloads if event_type == "turn.failed")
    envelope = ErrorEnvelope.model_validate_json(json.dumps(terminal_payload["error"], ensure_ascii=False))
    assert envelope.code is ErrorCode.PROVIDER_UNREACHABLE
    assert envelope.retryable is True
    assert envelope.details["failureCategory"] == "model"
    assert envelope.details["providerErrorCode"] == "provider_rate_limited"
    assert envelope.details["modelRequestId"] == "model-request-composer"


@pytest.mark.asyncio
async def test_accepted_tool_event_thaws_nested_arguments_without_losing_structure() -> None:
    arguments: dict[str, object] = {
        "operations": [
            {
                "op": "append",
                "path": "note.md",
                "content": "nested payload",
                "metadata": {"source": "planner", "ordinals": [1, 2]},
            }
        ]
    }
    definition = write_definition()
    call = ToolCall(
        tool_call_id="call-nested-write",
        run_id="run",
        workspace_id="ws",
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        definition_fingerprint=definition.fingerprint,
        idempotency_key="idem-nested-write",
        deadline=None,
        lineage=AgentLineage.root("run"),
        result_sensitivity=definition.result_sensitivity,
    )
    recorder = Recorder()
    kernel = Kernel(successful_execution(call))
    kernel.recorder = recorder

    result = await run_agent_loop(
        run_state(),
        planner=ScriptedPlanner([PlanningStep((call,), False, None), PlanningStep((), False, "done")]),
        composer=TextComposer(),
        tool_kernel=kernel,
        recorder=recorder,
        budget=run_budget(),
        cancellation=CancellationScope(name="nested-tool-event"),
        now=lambda: datetime.now(timezone.utc),
    )

    accepted = next(payload for event_type, payload in recorder.payloads if event_type == "tool.calls.accepted")
    assert accepted["calls"][0]["arguments"] == arguments  # type: ignore[index]
    assert json.loads(json.dumps(accepted, ensure_ascii=False)) == accepted
    assert result.phase is RunPhase.COMPLETED


@pytest.mark.asyncio
async def test_composer_context_overflow_retry_is_budgeted_and_persisted_once() -> None:
    recorder = Recorder()
    kernel = Kernel()
    kernel.recorder = recorder
    budget = run_budget(rounds=3)

    result = await run_agent_loop(
        run_state(),
        planner=ScriptedPlanner([PlanningStep((), False, "done")]),
        composer=ContextOverflowRetryComposer(),
        tool_kernel=kernel,
        recorder=recorder,
        budget=budget,
        cancellation=CancellationScope(name="run"),
        now=lambda: datetime.now(timezone.utc),
    )

    snapshot = await budget.snapshot(now=datetime.now(timezone.utc))
    assert result.phase is RunPhase.COMPLETED
    assert result.model_rounds == 3
    assert result.assistant_text == "done after retry"
    assert snapshot.used.model_rounds == 3
    assert snapshot.used.input_tokens == 5
    assert snapshot.used.output_tokens == 3
    assert recorder.events.count(("model.composition.started", False)) == 2
    assert recorder.events.count(("usage.updated", False)) == 2


@pytest.mark.asyncio
async def test_agent_loop_emits_turn_and_model_lifecycle_hooks_in_order() -> None:
    planner = ScriptedPlanner([PlanningStep((), False, "done")])
    recorder = Recorder()
    kernel = Kernel()
    kernel.recorder = recorder
    hooks = RecordingHooks()

    result = await run_agent_loop(
        run_state(),
        planner=planner,
        composer=TextComposer(),
        tool_kernel=kernel,
        recorder=recorder,
        budget=run_budget(),
        cancellation=CancellationScope(name="run"),
        now=lambda: datetime.now(timezone.utc),
        hooks=hooks,
        hook_context=HookExecutionContext("system", "principal", "ws", "session", True),
    )

    assert result.phase is RunPhase.COMPLETED
    assert [invocation.event for invocation in hooks.invocations] == [
        HookEvent.TURN_START,
        HookEvent.BEFORE_MODEL,
        HookEvent.AFTER_MODEL,
        HookEvent.BEFORE_MODEL,
        HookEvent.AFTER_MODEL,
        HookEvent.TURN_STOP,
    ]
    assert [invocation.facts.get("purpose") for invocation in hooks.invocations[1:5]] == [
        "planner",
        "planner",
        "composer",
        "composer",
    ]


@pytest.mark.asyncio
async def test_before_model_hook_hints_reach_hint_aware_planner_and_composer() -> None:
    planner = HintPlanner([PlanningStep((), False, "done")])
    composer = HintComposer()
    recorder = Recorder()
    kernel = Kernel()
    kernel.recorder = recorder

    await run_agent_loop(
        run_state(),
        planner=planner,
        composer=composer,
        tool_kernel=kernel,
        recorder=recorder,
        budget=run_budget(),
        cancellation=CancellationScope(name="run"),
        now=lambda: datetime.now(timezone.utc),
        hooks=HintHooks(),
        hook_context=HookExecutionContext("system", "principal", "ws", "session", True),
    )

    assert planner.hints == ("hint:planner",)
    assert composer.hints == ("hint:composer",)


@pytest.mark.asyncio
async def test_write_request_cannot_compose_until_real_denial_is_recorded() -> None:
    call = write_call()
    planner = ScriptedPlanner(
        [
            PlanningStep((), True, "needs write"),
            PlanningStep((call,), True, None),
            PlanningStep((), True, "write resolved"),
        ]
    )
    composer = TextComposer()
    recorder = Recorder()
    kernel = Kernel(denied_execution())
    kernel.recorder = recorder

    result = await run_agent_loop(
        run_state(),
        planner=planner,
        composer=composer,
        tool_kernel=kernel,
        recorder=recorder,
        budget=run_budget(),
        cancellation=CancellationScope(name="run"),
        now=lambda: datetime.now(timezone.utc),
    )

    assert result.write_obligation.satisfied
    assert kernel.saw_accepted_event
    assert recorder.events.index(("tool.calls.accepted", False)) < recorder.events.index(("tool.completed", False))
    assert result.phase is RunPhase.COMPLETED


@pytest.mark.asyncio
async def test_budget_exhaustion_with_unresolved_write_fails_without_composer() -> None:
    planner = ScriptedPlanner([PlanningStep((), True, "no outcome")])
    composer = TextComposer()
    recorder = Recorder()
    kernel = Kernel()
    kernel.recorder = recorder
    budget = run_budget(rounds=2)

    result = await run_agent_loop(
        run_state(),
        planner=planner,
        composer=composer,
        tool_kernel=kernel,
        recorder=recorder,
        budget=budget,
        cancellation=CancellationScope(name="run"),
        now=lambda: datetime.now(timezone.utc),
    )

    assert result.phase is RunPhase.FAILED
    assert composer.calls == []
    assert recorder.events[-1] == ("turn.failed", True)
    assert (await budget.snapshot(now=datetime.now(timezone.utc))).reserved == BudgetDelta()


@pytest.mark.asyncio
async def test_each_legacy_planner_call_is_persisted_and_counted_once() -> None:
    planner = ScriptedPlanner([PlanningStep((), False, "done", usage=ModelUsage(2, 3, 0, 0))])
    recorder = Recorder()
    kernel = Kernel()
    kernel.recorder = recorder

    result = await run_agent_loop(
        run_state(),
        planner=planner,
        composer=TextComposer(),
        tool_kernel=kernel,
        recorder=recorder,
        budget=run_budget(),
        cancellation=CancellationScope(name="run"),
        now=lambda: datetime.now(timezone.utc),
    )

    assert result.model_rounds == 2
    assert recorder.events.count(("model.attempt", False)) == 1


@pytest.mark.asyncio
async def test_approval_lifecycle_is_persisted_in_run_pending_and_phase() -> None:
    tool_call = write_call()
    planner = ScriptedPlanner(
        [
            PlanningStep((tool_call,), True, None),
            PlanningStep((), True, "resolved"),
        ]
    )
    recorder = Recorder()

    result = await run_agent_loop(
        run_state(),
        planner=planner,
        composer=TextComposer(),
        tool_kernel=ApprovalKernel(),
        recorder=recorder,
        budget=run_budget(),
        cancellation=CancellationScope(name="run"),
        now=lambda: datetime.now(timezone.utc),
    )

    event_names = [name for name, _ in recorder.events]
    assert event_names.index("approval.required") < event_names.index("approval.resolved")
    assert event_names.index("approval.resolved") < event_names.index("tool.completed")
    assert result.pending.approval_ids == frozenset()
    assert result.phase is RunPhase.COMPLETED


@pytest.mark.asyncio
async def test_second_tool_can_require_approval_after_first_result_is_recorded() -> None:
    calls = (
        write_call(tool_call_id="call-first", idempotency_key="idem-first"),
        write_call(tool_call_id="call-second", idempotency_key="idem-second"),
    )
    planner = ScriptedPlanner([PlanningStep(calls, True, None), PlanningStep((), True, "done")])
    recorder = Recorder()

    result = await run_agent_loop(
        run_state(),
        planner=planner,
        composer=TextComposer(),
        tool_kernel=MidBatchApprovalKernel(),
        recorder=recorder,
        budget=run_budget(),
        cancellation=CancellationScope(name="run"),
        now=lambda: datetime.now(timezone.utc),
    )

    names = [name for name, _terminal in recorder.events]
    first_result = names.index("tool.completed")
    assert first_result < names.index("approval.required") < names.index("approval.resolved")
    assert names.index("approval.resolved") < names.index("tool.completed", first_result + 1)
    assert [item.tool_call_id for item in result.tool_results] == ["call-first", "call-second"]


@pytest.mark.asyncio
async def test_parallel_completion_order_is_reordered_to_original_tool_plan_order() -> None:
    calls = (
        write_call(tool_call_id="call-first", idempotency_key="idem-first"),
        write_call(tool_call_id="call-second", idempotency_key="idem-second"),
    )
    result = await run_agent_loop(
        run_state(),
        planner=ScriptedPlanner([PlanningStep(calls, True, None), PlanningStep((), True, "done")]),
        composer=TextComposer(),
        tool_kernel=OutOfOrderResultKernel(),
        recorder=Recorder(),
        budget=run_budget(),
        cancellation=CancellationScope(name="run"),
        now=lambda: datetime.now(timezone.utc),
    )

    assert [item.tool_call_id for item in result.tool_results] == ["call-first", "call-second"]


@pytest.mark.asyncio
async def test_recovered_batch_continues_inside_the_canonical_loop_with_original_order_and_budget() -> None:
    first = write_call(tool_call_id="call-first", idempotency_key="idem-first")
    second = write_call(tool_call_id="call-second", idempotency_key="idem-second")
    second_result = successful_execution(second).result
    started_at = datetime.now(timezone.utc)
    budget_definition = RunBudget(8, 10, 4, 60, 100, 100, Decimal("1"), 1_000, 2)
    budget = BudgetLedger.restore(
        budget_definition,
        started_at=started_at,
        used=BudgetDelta(model_rounds=1, tool_calls=2),
        reserved=BudgetDelta(model_rounds=1),
    )
    checkpoint = await BudgetCheckpoint.capture(budget, now=started_at)
    recovered_state = RunState(
        "ws",
        "session",
        "turn",
        "run",
        AgentLineage.root("run"),
        phase=RunPhase.RECORDING_RESULTS,
        revision=12,
        model_rounds=1,
        tool_calls=2,
        pending=PendingWork(frozenset({first.tool_call_id}), (first,)),
        tool_results=(second_result,),
        budget_checkpoint=checkpoint,
        tool_result_sensitivities={
            first.tool_call_id: first.result_sensitivity,
            second.tool_call_id: second.result_sensitivity,
        },
    )
    recorder = Recorder()
    kernel = Kernel(successful_execution(first))
    kernel.recorder = recorder
    recovered_batch = RecoveredToolBatch(
        (first.tool_call_id, second.tool_call_id),
        (first,),
    )
    preparation = RecordingPreparation()

    with pytest.raises(ValueError, match="budget ledger"):
        await run_agent_loop(
            recovered_state,
            planner=ScriptedPlanner([PlanningStep((), False, "must not plan")]),
            composer=TextComposer(),
            tool_kernel=kernel,
            recorder=Recorder(),
            budget=BudgetLedger(budget_definition, started_at=started_at),
            cancellation=CancellationScope(name="mismatched-recovered-run"),
            now=lambda: started_at,
            recovered_tool_batch=recovered_batch,
        )

    result = await run_agent_loop(
        recovered_state,
        planner=ScriptedPlanner([PlanningStep((), False, "done")]),
        composer=TextComposer(),
        tool_kernel=kernel,
        recorder=recorder,
        budget=budget,
        cancellation=CancellationScope(name="recovered-run"),
        now=lambda: started_at,
        recovered_tool_batch=recovered_batch,
        run_preparation=preparation,
    )

    assert result.phase is RunPhase.COMPLETED
    assert [item.tool_call_id for item in result.tool_results] == ["call-first", "call-second"]
    assert ("phase.changed", False) in recorder.events
    assert recorder.events.count(("tool.calls.accepted", False)) == 0
    snapshot = await budget.snapshot(now=started_at)
    assert snapshot.used.model_rounds == 3
    assert snapshot.used.tool_calls == 2
    assert snapshot.reserved == BudgetDelta()
    assert preparation.calls == [
        (RunPhase.LOADING_CONTEXT, RunPhase.RECORDING_RESULTS),
        (RunPhase.SELECTING_MEMORY, RunPhase.RECORDING_RESULTS),
    ]
