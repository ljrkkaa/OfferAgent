from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offeragent_harness.agent import RunBudget
from offeragent_harness.agent.composer import CompositionEvent
from offeragent_harness.agent.loop import ToolExecution, ToolKernel
from offeragent_harness.agent.planner import Planner, PlanningAttempt, PlanningAttemptOutcome, PlanningStep
from offeragent_harness.agent.state import RunState
from offeragent_harness.models import ModelUsage
from offeragent_harness.permissions import (
    ApprovalBinding,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalScope,
    ApprovalState,
    RiskClass,
)
from offeragent_harness.ports import CancellationToken, StoredEvent, ToolLifecycleObserver
from offeragent_harness.protocol.content import VaultSourceRef
from offeragent_harness.protocol.events import (
    ToolCompletedPayload,
    ToolFailedPayload,
    parse_event,
    stored_event_to_envelope,
)
from offeragent_harness.runtime import TurnManager
from offeragent_harness.runtime.harness_service import (
    CreateSessionCommand,
    HarnessService,
    RunComponents,
    StartTurnCommand,
)
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualClock,
    RecordingEventSink,
)
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

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _StopPlanner:
    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        return _audited_step(state, (), False, "done")


class _WaitingPlanner:
    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        del state
        await cancellation.wait()
        cancellation.checkpoint()
        raise AssertionError("cancelled planner continued")


class _ToolPlanner:
    def __init__(self, call: ToolCall) -> None:
        self._call = call
        self._planned = False

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        if not self._planned:
            self._planned = True
            return _audited_step(state, (self._call,), True, None)
        return _audited_step(state, (), True, "tool outcome recorded")


def _audited_step(
    state: RunState,
    calls: tuple[ToolCall, ...],
    requires_write_outcome: bool,
    stop_reason: str | None,
) -> PlanningStep:
    return PlanningStep(
        calls,
        requires_write_outcome,
        stop_reason,
        attempts=(
            PlanningAttempt(
                request_id=f"test-replay-{state.model_rounds + 1}",
                repair_index=0,
                outcome=PlanningAttemptOutcome.SUCCEEDED,
                usage=ModelUsage(0, 0, 0, 0),
            ),
        ),
    )


class _Composer:
    async def stream(
        self,
        state: RunState,
        *,
        partial: bool,
        cancellation: CancellationToken,
    ) -> AsyncIterator[CompositionEvent]:
        del state, partial
        cancellation.checkpoint()
        yield CompositionEvent(text_delta="answer")


class _NoToolKernel:
    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        del calls, cancellation, observer
        raise AssertionError("no tool call expected")


class _ResultKernel:
    def __init__(self, execution: ToolExecution, *, approval: bool) -> None:
        self._execution = execution
        self._approval = approval

    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        cancellation.checkpoint()
        assert observer is not None
        assert tuple(calls) == (self._execution.call,)
        if self._approval:
            request = ApprovalRequest(
                approval_id="apr_1",
                tool_call_id=self._execution.call.tool_call_id,
                binding=ApprovalBinding(
                    tool_name=self._execution.call.name,
                    tool_version=self._execution.call.version,
                    definition_fingerprint=self._execution.call.definition_fingerprint,
                    args_hash=self._execution.call.args_hash,
                    workspace_id=self._execution.call.workspace_id,
                    session_id="ses_1",
                    principal_id="principal_1",
                    root_run_id=self._execution.call.lineage.root_run_id,
                    run_id=self._execution.call.run_id,
                    agent_name=self._execution.call.lineage.agent_name,
                    ancestor_run_ids=self._execution.call.lineage.ancestor_run_ids,
                    expected_state_hash=None,
                    expires_at=NOW + timedelta(minutes=5),
                ),
                risk=RiskClass.WRITE,
                summary="approve test write",
                diff_artifact_ids=("art_1",),
            )
            await observer.required(request)
            await observer.resolved(
                request,
                ApprovalResolution(
                    approval_id=request.approval_id,
                    state=ApprovalState.APPROVED,
                    scope=ApprovalScope.ONCE,
                    resolved_at=NOW,
                    resolver_id="user:test",
                    include_descendants=False,
                ),
            )
        await observer.execution_started(
            self._execution.call,
            self._execution.definition,
        )
        await observer.result_available(
            self._execution.call,
            self._execution.definition,
            self._execution.result,
        )
        return (self._execution,)


def _definition() -> ToolDefinition:
    return ToolDefinition(
        name="vault.transaction",
        version="1",
        description="typed replay test",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={},
        executor_location=ExecutorLocation.LOCAL,
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


def _execution(state: RunState, status: ToolResultStatus) -> ToolExecution:
    definition = _definition()
    arguments = {"path": "tests/note.md"}
    call = ToolCall(
        tool_call_id="call_1",
        run_id=state.run_id,
        workspace_id=state.workspace_id,
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        definition_fingerprint=definition.fingerprint,
        idempotency_key="typed-replay-tool",
        deadline=None,
        lineage=state.lineage,
        result_sensitivity=definition.result_sensitivity,
    )
    error = None
    if status is not ToolResultStatus.SUCCEEDED:
        error = ToolError("executor_failed", "executor failed", False, False)
    result = ToolResult(
        tool_call_id=call.tool_call_id,
        status=status,
        data={"status": status.value},
        user_visible_summary=f"tool {status.value}",
        artifact_ids=(),
        source_refs=(),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state=None,
        error=error,
        source_references=(
            {
                "type": "vault",
                "file": {
                    "workspaceId": state.workspace_id,
                    "path": "tests/note.md",
                    "lineStart": 2,
                    "lineEnd": 4,
                },
                "freshness": "fresh",
                "workspaceRevision": 7,
            },
        ),
    )
    return ToolExecution(call, definition, result)


class _Components:
    def __init__(self, mode: str, status: ToolResultStatus = ToolResultStatus.SUCCEEDED) -> None:
        self._mode = mode
        self._status = status

    def build(self, command: StartTurnCommand, state: RunState) -> RunComponents:
        del command
        planner: Planner
        kernel: ToolKernel
        if self._mode == "wait":
            planner = _WaitingPlanner()
            kernel = _NoToolKernel()
        elif self._mode == "stop":
            planner = _StopPlanner()
            kernel = _NoToolKernel()
        else:
            execution = _execution(state, self._status)
            planner = _ToolPlanner(execution.call)
            kernel = _ResultKernel(execution, approval=self._mode == "approval")
        return RunComponents(
            planner_factory=lambda budget: planner,
            tool_kernel_factory=lambda budget: kernel,
            composer=_Composer(),
            budget=RunBudget(8, 8, 2, 60, 1_000, 1_000, Decimal("1"), 10_000, 2),
        )


async def _run(mode: str, status: ToolResultStatus = ToolResultStatus.SUCCEEDED) -> tuple[HarnessService, str]:
    manager = TurnManager()
    harness = HarnessService(
        unit_of_work=InMemoryUnitOfWorkFactory(),
        event_sink=RecordingEventSink(),
        clock=ManualClock(NOW),
        ids=DeterministicIdGenerator(),
        components=_Components(mode, status),
        turn_manager=manager,
    )
    session = await harness.create_session(
        CreateSessionCommand("ws_test", "profile_test", "Typed replay", "create-session")
    )
    receipt = await harness.start_turn(
        StartTurnCommand(
            workspace_id="ws_test",
            session_id=session.session_id,
            turn_id="turn_1",
            idempotency_key="start-turn",
            input_blocks=({"type": "text", "text": "test"},),
            run_config={"model": "fake"},
        )
    )
    active = await manager.get(receipt.run_id)
    assert active is not None
    if mode == "wait":
        assert await harness.cancel_turn(receipt.run_id)
    await active.task
    return harness, receipt.run_id


def _assert_replay_is_wire_valid(events: tuple[StoredEvent, ...]) -> None:
    for stored in events:
        envelope = stored_event_to_envelope(stored)
        assert parse_event(envelope.to_wire()) == envelope


@pytest.mark.asyncio
async def test_plain_text_run_is_fully_wire_replayable() -> None:
    harness, run_id = await _run("stop")
    events = await harness.replay_events(run_id)
    _assert_replay_is_wire_valid(events)
    assert {event.event_type for event in events} >= {"assistant.delta", "assistant.completed", "turn.completed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "event_type"),
    [
        (ToolResultStatus.SUCCEEDED, "tool.completed"),
        (ToolResultStatus.FAILED, "tool.failed"),
    ],
)
async def test_tool_success_and_failure_are_distinct_wire_facts(
    status: ToolResultStatus,
    event_type: str,
) -> None:
    harness, run_id = await _run("tool", status)
    events = await harness.replay_events(run_id)
    _assert_replay_is_wire_valid(events)
    assert event_type in [event.event_type for event in events]
    stored = next(event for event in events if event.event_type == event_type)
    payload = stored_event_to_envelope(stored).payload
    assert isinstance(payload, (ToolCompletedPayload, ToolFailedPayload))
    assert len(payload.result.source_refs) == 1
    reference = payload.result.source_refs[0]
    assert isinstance(reference, VaultSourceRef)
    assert reference.file.path == "tests/note.md"
    assert (reference.file.line_start, reference.file.line_end) == (2, 4)
    assert reference.workspace_revision == 7
    assert not any(event.event_type == "references.updated" for event in events)


@pytest.mark.asyncio
async def test_approval_lifecycle_is_fully_wire_replayable() -> None:
    harness, run_id = await _run("approval")
    events = await harness.replay_events(run_id)
    _assert_replay_is_wire_valid(events)
    names = [event.event_type for event in events]
    assert names.index("approval.required") < names.index("approval.resolved") < names.index("tool.completed")


@pytest.mark.asyncio
async def test_cancelled_run_is_fully_wire_replayable() -> None:
    harness, run_id = await _run("wait")
    events = await harness.replay_events(run_id)
    _assert_replay_is_wire_valid(events)
    assert events[-1].event_type == "turn.cancelled"
