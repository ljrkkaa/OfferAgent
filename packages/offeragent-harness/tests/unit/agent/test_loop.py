from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from offeragent_harness.agent.budgets import BudgetLedger, RunBudget
from offeragent_harness.agent.composer import CompositionEvent
from offeragent_harness.agent.loop import ToolExecution, run_agent_loop
from offeragent_harness.agent.planner import PlanningStep
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.models import ModelUsage
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import CancellationToken
from offeragent_harness.runtime import CancellationScope
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools import (
    ExecutorLocation,
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


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, bool]] = []
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


class Kernel:
    def __init__(self, execution: ToolExecution | None = None) -> None:
        self.execution = execution
        self.saw_accepted_event = False
        self.recorder: Recorder | None = None

    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
    ) -> tuple[ToolExecution, ...]:
        cancellation.checkpoint()
        assert self.recorder is not None
        self.saw_accepted_event = ("tool.calls.accepted", False) in self.recorder.events
        if self.execution is None:
            raise AssertionError("unexpected tool execution")
        return (self.execution,)


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
            max_subagent_depth=1,
        ),
        started_at=datetime.now(timezone.utc),
    )


def write_call() -> ToolCall:
    arguments: dict[str, list[object]] = {"operations": []}
    return ToolCall(
        tool_call_id="call-write",
        run_id="run",
        workspace_id="ws",
        name="vault.transaction",
        version="1",
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="idem-write",
        deadline=None,
        lineage=AgentLineage.root("run"),
    )


def write_definition() -> ToolDefinition:
    return ToolDefinition(
        name="vault.transaction",
        version="1",
        description="write",
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


@pytest.mark.asyncio
async def test_no_tool_turn_streams_and_commits_one_terminal_event() -> None:
    planner = ScriptedPlanner([PlanningStep((), False, "done")])
    composer = TextComposer()
    recorder = Recorder()
    kernel = Kernel()
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

    assert result.phase is RunPhase.COMPLETED
    assert result.assistant_text == "done"
    assert recorder.events[-1] == ("turn.completed", True)


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

    result = await run_agent_loop(
        run_state(),
        planner=planner,
        composer=composer,
        tool_kernel=kernel,
        recorder=recorder,
        budget=run_budget(rounds=1),
        cancellation=CancellationScope(name="run"),
        now=lambda: datetime.now(timezone.utc),
    )

    assert result.phase is RunPhase.FAILED
    assert composer.calls == []
    assert recorder.events[-1] == ("turn.failed", True)
