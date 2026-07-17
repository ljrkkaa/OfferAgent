from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from offeragent_harness.agent.budgets import BudgetLedger, RunBudget
from offeragent_harness.agent.loop import ToolExecution, run_agent_loop
from offeragent_harness.agent.planner import PlanningAttempt, PlanningAttemptOutcome, PlanningStep
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.models import ModelUsage
from offeragent_harness.ports import ApplicationCommandContext, CancellationToken, ToolLifecycleObserver
from offeragent_harness.protocol.messages import COMMAND_REGISTRY
from offeragent_harness.runtime.application_dispatcher import RuntimeApplicationCommandDispatcher
from offeragent_harness.runtime.cancellation import CancellationScope
from offeragent_harness.runtime.plugin_tools import PluginToolExecutor, plugin_tool_completion_handlers
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools import ToolCall, ToolDefinition, canonical_json_sha256


class _ReadyApplication:
    def require_ready(self) -> object:
        return self


class _ContractAwarePlanner:
    def __init__(self, call: ToolCall) -> None:
        self._call = call
        self._round = 0

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        self._round += 1
        if self._round == 1:
            step = PlanningStep((self._call,), False, None)
        else:
            assert len(state.tool_results) == 1
            assert state.tool_results[0].data == {
                "path": "agent.md",
                "content": "# OfferAgent\n\nUse Vault evidence.",
                "contentHash": "sha256:" + "b" * 64,
            }
            step = PlanningStep((), False, "已按 Vault Agent Contract 回答。")
        return replace(
            step,
            attempts=(
                PlanningAttempt(
                    request_id=f"request-{self._round}",
                    repair_index=0,
                    outcome=PlanningAttemptOutcome.SUCCEEDED,
                    usage=ModelUsage(1, 1, 0, 0),
                ),
            ),
        )


class _PluginKernel:
    def __init__(self, definition: ToolDefinition, executor: PluginToolExecutor) -> None:
        self._definition = definition
        self._executor = executor

    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        assert tuple(calls) and observer is not None
        call = calls[0]
        await observer.execution_started(call, self._definition)
        result = await self._executor.execute(call, cancellation)
        await observer.result_available(call, self._definition, result)
        return (ToolExecution(call, self._definition, result),)


class _RoundTripRecorder:
    def __init__(self, dispatcher: RuntimeApplicationCommandDispatcher) -> None:
        self.events: list[str] = []
        self.payloads: list[Mapping[str, object]] = []
        self._dispatcher = dispatcher
        self._completions: list[asyncio.Task[object]] = []

    async def commit(
        self,
        state: RunState,
        *,
        event_type: str,
        payload: Mapping[str, object],
        terminal: bool = False,
    ) -> None:
        del state, terminal
        self.events.append(event_type)
        self.payloads.append(payload)
        if event_type != "tool.started":
            return
        call = payload["call"]
        assert isinstance(call, Mapping)
        task = asyncio.create_task(
            self._dispatcher.dispatch(
                "plugin-tools/complete",
                {
                    "workspaceId": call["workspaceId"],
                    "runId": call["runId"],
                    "definitionFingerprint": call["definitionFingerprint"],
                    "argsHash": call["argsHash"],
                    "idempotencyKey": call["idempotencyKey"],
                    "result": {
                        "toolCallId": call["toolCallId"],
                        "status": "succeeded",
                        "summary": "Read the Vault Agent Contract.",
                        "data": {
                            "path": "agent.md",
                            "content": "# OfferAgent\n\nUse Vault evidence.",
                            "contentHash": "sha256:" + "b" * 64,
                        },
                        "sourceRefs": [
                            {
                                "type": "vault",
                                "file": {
                                    "workspaceId": call["workspaceId"],
                                    "path": "agent.md",
                                    "contentHash": "sha256:" + "b" * 64,
                                },
                                "freshness": "fresh",
                            }
                        ],
                    },
                },
                CancellationScope(name="plugin-completion"),
                context=ApplicationCommandContext(transport="stdio", client_id="obsidian-plugin"),
            )
        )
        self._completions.append(task)

    async def join(self) -> None:
        await asyncio.gather(*self._completions)


@pytest.mark.asyncio
async def test_agent_contract_round_trips_from_started_event_to_the_same_agent_loop() -> None:
    from offeragent_harness.runtime.plugin_tools import plugin_tool_definitions

    definition = plugin_tool_definitions()[0]
    call = ToolCall(
        tool_call_id="call_contract",
        run_id="run_contract",
        workspace_id="ws_vault",
        name=definition.name,
        version=definition.version,
        arguments={},
        args_hash=canonical_json_sha256({}),
        idempotency_key="contract-read-1",
        deadline=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
        lineage=AgentLineage.root("run_contract"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )
    executor = PluginToolExecutor()

    async def unused(*args: object) -> Mapping[str, object]:
        del args
        return {}

    handlers = {method: unused for method in COMMAND_REGISTRY}
    handlers.update(plugin_tool_completion_handlers(executor=executor))
    dispatcher = RuntimeApplicationCommandDispatcher(application=_ReadyApplication(), handlers=handlers)
    recorder = _RoundTripRecorder(dispatcher)
    state = RunState(
        "ws_vault",
        "ses_contract",
        "turn_contract",
        "run_contract",
        AgentLineage.root("run_contract"),
    )
    budget = BudgetLedger(
        RunBudget(4, 4, 2, 60, 100, 100, Decimal("1"), 1_000, 1),
        started_at=datetime.now(timezone.utc),
    )

    result = await run_agent_loop(
        state,
        planner=_ContractAwarePlanner(call),
        tool_kernel=_PluginKernel(definition, executor),
        recorder=recorder,
        budget=budget,
        cancellation=CancellationScope(name="contract-run"),
        now=lambda: datetime.now(timezone.utc),
    )
    await recorder.join()

    assert result.phase is RunPhase.COMPLETED, list(zip(recorder.events, recorder.payloads, strict=True))
    assert recorder.events.index("tool.started") < recorder.events.index("tool.completed")
    assert recorder.events.index("tool.completed") < recorder.events.index("assistant.completed")
