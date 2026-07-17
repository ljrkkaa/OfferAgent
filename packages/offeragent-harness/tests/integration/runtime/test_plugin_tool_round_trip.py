from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import cast

import pytest

from offeragent_harness.agent.budgets import BudgetLedger, RunBudget
from offeragent_harness.agent.loop import ToolExecution, run_agent_loop
from offeragent_harness.agent.planner import PlanningAttempt, PlanningAttemptOutcome, PlanningStep
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.models import ModelUsage
from offeragent_harness.ports import ApplicationCommandContext, CancellationToken, ToolLifecycleObserver
from offeragent_harness.protocol.messages import COMMAND_REGISTRY
from offeragent_harness.runtime.application_dispatcher import (
    ApplicationCommandHandler,
    RuntimeApplicationCommandDispatcher,
)
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

    handlers: dict[str, ApplicationCommandHandler] = {
        method: cast(ApplicationCommandHandler, unused) for method in COMMAND_REGISTRY
    }
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


class _PreciseEvidencePlanner:
    def __init__(self, search: ToolCall, read: ToolCall) -> None:
        self._search = search
        self._read = read
        self._round = 0

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        self._round += 1
        if self._round == 1:
            step = PlanningStep((self._search,), False, None)
        elif self._round == 2:
            assert len(state.tool_results) == 1
            data = cast(Mapping[str, object], state.tool_results[0].data)
            entries = cast(Sequence[object], data["entries"])
            candidate = cast(Mapping[str, object], entries[0])
            assert candidate["contentHash"] == "sha256:" + "c" * 64
            assert not state.tool_results[0].source_references
            step = PlanningStep((self._read,), False, None)
        else:
            assert len(state.tool_results) == 2
            precise = state.tool_results[-1]
            assert cast(Mapping[str, object], precise.data)["content"] == "Precise current evidence"
            assert precise.source_references[0]["file"]["lineStart"] == 7
            step = PlanningStep((), False, "依据精确读取的当前来源: Precise current evidence")
        return replace(
            step,
            attempts=(
                PlanningAttempt(
                    request_id=f"evidence-request-{self._round}",
                    repair_index=0,
                    outcome=PlanningAttemptOutcome.SUCCEEDED,
                    usage=ModelUsage(1, 1, 0, 0),
                ),
            ),
        )


class _PluginDefinitionKernel:
    def __init__(self, definitions: Mapping[str, ToolDefinition], executor: PluginToolExecutor) -> None:
        self._definitions = definitions
        self._executor = executor

    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        assert len(calls) == 1 and observer is not None
        call = calls[0]
        definition = self._definitions[call.name]
        await observer.execution_started(call, definition)
        result = await self._executor.execute(call, cancellation)
        await observer.result_available(call, definition, result)
        return (ToolExecution(call, definition, result),)


class _EvidenceRoundTripRecorder:
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
        name = call["name"]
        if name == "vault.search":
            data: Mapping[str, object] = {
                "entries": [
                    {
                        "path": "notes/source.md",
                        "modifiedVersion": "mtime:17:size:81",
                        "contentHash": "sha256:" + "c" * 64,
                        "matchTier": "body",
                        "snippets": [{"content": "locator only", "lineStart": 7, "lineEnd": 7}],
                    }
                ],
                "truncated": False,
            }
            source_refs: list[Mapping[str, object]] = []
            summary = "Located a candidate; precise read required."
        else:
            assert name == "vault.read"
            assert call["arguments"] == {
                "path": "notes/source.md",
                "lineStart": 7,
                "lineEnd": 7,
                "expectedContentHash": "sha256:" + "c" * 64,
                "expectedModifiedVersion": "mtime:17:size:81",
            }
            data = {
                "path": "notes/source.md",
                "lineStart": 7,
                "lineEnd": 7,
                "modifiedVersion": "mtime:17:size:81",
                "contentHash": "sha256:" + "c" * 64,
                "content": "Precise current evidence",
                "truncated": False,
            }
            source_refs = [
                {
                    "type": "vault",
                    "file": {
                        "workspaceId": call["workspaceId"],
                        "path": "notes/source.md",
                        "contentHash": "sha256:" + "c" * 64,
                        "lineStart": 7,
                        "lineEnd": 7,
                    },
                    "freshness": "fresh",
                }
            ]
            summary = "Read precise current evidence."
        self._completions.append(
            asyncio.create_task(
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
                            "summary": summary,
                            "data": data,
                            "sourceRefs": source_refs,
                        },
                    },
                    CancellationScope(name=f"completion-{name}"),
                    context=ApplicationCommandContext(transport="stdio", client_id="obsidian-plugin"),
                )
            )
        )

    async def join(self) -> None:
        await asyncio.gather(*self._completions)


@pytest.mark.asyncio
async def test_scripted_vault_answer_uses_locator_then_version_bound_precise_read() -> None:
    from offeragent_harness.runtime.plugin_tools import plugin_tool_definitions

    definitions = {definition.name: definition for definition in plugin_tool_definitions()}

    def call(name: str, arguments: Mapping[str, object], suffix: str) -> ToolCall:
        definition = definitions[name]
        return ToolCall(
            tool_call_id=f"call_{suffix}",
            run_id="run_evidence",
            workspace_id="ws_vault",
            name=name,
            version=definition.version,
            arguments=arguments,
            args_hash=canonical_json_sha256(arguments),
            idempotency_key=f"evidence-{suffix}",
            deadline=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
            lineage=AgentLineage.root("run_evidence"),
            definition_fingerprint=definition.fingerprint,
            result_sensitivity=definition.result_sensitivity,
        )

    search = call("vault.search", {"query": "current evidence", "limit": 10}, "search")
    read = call(
        "vault.read",
        {
            "path": "notes/source.md",
            "lineStart": 7,
            "lineEnd": 7,
            "expectedContentHash": "sha256:" + "c" * 64,
            "expectedModifiedVersion": "mtime:17:size:81",
        },
        "read",
    )
    executor = PluginToolExecutor()

    async def unused(*args: object) -> Mapping[str, object]:
        del args
        return {}

    handlers: dict[str, ApplicationCommandHandler] = {
        method: cast(ApplicationCommandHandler, unused) for method in COMMAND_REGISTRY
    }
    handlers.update(plugin_tool_completion_handlers(executor=executor))
    dispatcher = RuntimeApplicationCommandDispatcher(application=_ReadyApplication(), handlers=handlers)
    recorder = _EvidenceRoundTripRecorder(dispatcher)
    state = RunState(
        "ws_vault",
        "ses_evidence",
        "turn_evidence",
        "run_evidence",
        AgentLineage.root("run_evidence"),
    )
    budget = BudgetLedger(
        RunBudget(5, 4, 2, 60, 100, 100, Decimal("1"), 1_000, 1),
        started_at=datetime.now(timezone.utc),
    )

    result = await run_agent_loop(
        state,
        planner=_PreciseEvidencePlanner(search, read),
        tool_kernel=_PluginDefinitionKernel(definitions, executor),
        recorder=recorder,
        budget=budget,
        cancellation=CancellationScope(name="evidence-run"),
        now=lambda: datetime.now(timezone.utc),
    )
    await recorder.join()

    started_names: list[object] = []
    for event, payload in zip(recorder.events, recorder.payloads, strict=True):
        if event == "tool.started":
            started_names.append(cast(Mapping[str, object], payload["call"])["name"])
    assert result.phase is RunPhase.COMPLETED
    assert started_names == ["vault.search", "vault.read"]
    completed = recorder.payloads[recorder.events.index("assistant.completed")]
    assert completed["content"] == [
        {
            "type": "text",
            "text": "依据精确读取的当前来源: Precise current evidence",
            "format": "markdown",
            "references": [],
        }
    ]


class _VaultChangePlanner:
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
            result = state.tool_results[0]
            data = cast(Mapping[str, object], result.data)
            assert data["batchId"] == "batch_round_trip"
            assert data["state"] == "applied"
            assert result.retryable is False
            assert len(result.side_effects) == 1
            step = PlanningStep((), False, "Vault Change Batch 已应用。")
        return replace(
            step,
            attempts=(
                PlanningAttempt(
                    request_id=f"change-request-{self._round}",
                    repair_index=0,
                    outcome=PlanningAttemptOutcome.SUCCEEDED,
                    usage=ModelUsage(1, 1, 0, 0),
                ),
            ),
        )


class _VaultChangeRoundTripRecorder:
    def __init__(self, dispatcher: RuntimeApplicationCommandDispatcher) -> None:
        self.events: list[str] = []
        self.payloads: list[Mapping[str, object]] = []
        self.completion_results: list[object] = []
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
        assert call["name"] == "vault.changes.apply"
        assert call["risk"] == "write"
        params = {
            "workspaceId": call["workspaceId"],
            "runId": call["runId"],
            "definitionFingerprint": call["definitionFingerprint"],
            "argsHash": call["argsHash"],
            "idempotencyKey": call["idempotencyKey"],
            "result": {
                "toolCallId": call["toolCallId"],
                "status": "succeeded",
                "summary": "Applied Vault Change Batch 'batch_round_trip'.",
                "data": {
                    "batchId": "batch_round_trip",
                    "state": "applied",
                    "checkpointRef": "refs/offeragent/checkpoints/batch_round_trip",
                    "paths": ["notes/new.md"],
                    "beforeStateHash": "sha256:" + "a" * 64,
                    "afterStateHash": "sha256:" + "b" * 64,
                    "undoAvailable": True,
                },
                "sideEffects": [
                    {
                        "kind": "file_created",
                        "resource": "notes/new.md",
                        "beforeHash": None,
                        "afterHash": "sha256:" + "c" * 64,
                        "confirmed": True,
                    }
                ],
                "retryable": False,
            },
        }

        async def complete_twice() -> object:
            first = await self._dispatcher.dispatch(
                "plugin-tools/complete",
                params,
                CancellationScope(name="change-completion"),
                context=ApplicationCommandContext(transport="stdio", client_id="obsidian-plugin"),
            )
            second = await self._dispatcher.dispatch(
                "plugin-tools/complete",
                params,
                CancellationScope(name="change-completion-replay"),
                context=ApplicationCommandContext(transport="stdio", client_id="obsidian-plugin"),
            )
            self.completion_results.extend((first, second))
            return second

        self._completions.append(asyncio.create_task(complete_twice()))

    async def join(self) -> None:
        await asyncio.gather(*self._completions)


@pytest.mark.asyncio
async def test_vault_change_round_trip_returns_one_durable_write_outcome_and_replays_duplicate_ack() -> None:
    from offeragent_harness.runtime.plugin_tools import plugin_tool_definitions

    definition = next(item for item in plugin_tool_definitions() if item.name == "vault.changes.apply")
    arguments: Mapping[str, object] = {
        "batchId": "batch_round_trip",
        "task": "Create a note",
        "operations": [
            {
                "op": "create",
                "path": "notes/new.md",
                "content": "new evidence\n",
                "expectedContentHash": "absent",
            }
        ],
    }
    call = ToolCall(
        tool_call_id="call_change",
        run_id="run_change",
        workspace_id="ws_vault",
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="change-1",
        deadline=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
        lineage=AgentLineage.root("run_change"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )
    executor = PluginToolExecutor()

    async def unused(*args: object) -> Mapping[str, object]:
        del args
        return {}

    handlers: dict[str, ApplicationCommandHandler] = {
        method: cast(ApplicationCommandHandler, unused) for method in COMMAND_REGISTRY
    }
    handlers.update(plugin_tool_completion_handlers(executor=executor))
    recorder = _VaultChangeRoundTripRecorder(
        RuntimeApplicationCommandDispatcher(application=_ReadyApplication(), handlers=handlers)
    )
    state = RunState("ws_vault", "ses_change", "turn_change", "run_change", AgentLineage.root("run_change"))
    budget = BudgetLedger(
        RunBudget(4, 4, 2, 60, 100, 100, Decimal("1"), 1_000, 1),
        started_at=datetime.now(timezone.utc),
    )

    result = await run_agent_loop(
        state,
        planner=_VaultChangePlanner(call),
        tool_kernel=_PluginKernel(definition, executor),
        recorder=recorder,
        budget=budget,
        cancellation=CancellationScope(name="change-run"),
        now=lambda: datetime.now(timezone.utc),
    )
    await recorder.join()

    assert result.phase is RunPhase.COMPLETED
    assert recorder.events.index("tool.started") < recorder.events.index("tool.completed")
    assert [item["replayed"] for item in recorder.completion_results if isinstance(item, Mapping)] == [False, True]


class _SequentialProductPlanner:
    def __init__(self, calls: Sequence[ToolCall], final_response: str) -> None:
        self._calls = tuple(calls)
        self._final_response = final_response
        self._round = 0

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        assert len(state.tool_results) == min(self._round, len(self._calls))
        if self._round < len(self._calls):
            call = self._calls[self._round]
            step = PlanningStep((call,), call.name == "vault.changes.apply", None)
        else:
            step = PlanningStep((), False, self._final_response)
        self._round += 1
        return replace(
            step,
            attempts=(
                PlanningAttempt(
                    request_id=f"planning-product-{self._round}",
                    repair_index=0,
                    outcome=PlanningAttemptOutcome.SUCCEEDED,
                    usage=ModelUsage(1, 1, 0, 0),
                ),
            ),
        )


class _PlanningProductRecorder:
    def __init__(self, dispatcher: RuntimeApplicationCommandDispatcher) -> None:
        self.events: list[str] = []
        self.started_calls: list[Mapping[str, object]] = []
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
        if event_type != "tool.started":
            return
        call = cast(Mapping[str, object], payload["call"])
        self.started_calls.append(call)
        name = cast(str, call["name"])
        arguments = cast(Mapping[str, object], call["arguments"])
        source_refs: list[Mapping[str, object]] = []
        side_effects: list[Mapping[str, object]] = []
        if name == "agent_contract.read":
            data: Mapping[str, object] = {
                "path": "agent.md",
                "content": "# OfferAgent\n\nUse Planning Memory and preserve Study Evidence.",
                "contentHash": "sha256:" + "1" * 64,
            }
        elif name == "planning_memory.list":
            data = {
                "topics": [
                    {
                        "path": "memory/feedback/corrections.md",
                        "type": "feedback",
                        "name": "Corrections",
                        "description": "Durable planning corrections",
                        "modifiedVersion": "mtime:1:size:100",
                        "contentHash": "sha256:" + "2" * 64,
                    },
                    {
                        "path": "memory/study/agentic-rl.md",
                        "type": "study",
                        "name": "Agentic RL",
                        "description": "Cross-day study order",
                        "modifiedVersion": "mtime:2:size:100",
                        "contentHash": "sha256:" + "3" * 64,
                    },
                    {
                        "path": "memory/study/cooking.md",
                        "type": "study",
                        "name": "Cooking",
                        "description": "Unrelated weekend recipes",
                        "modifiedVersion": "mtime:3:size:100",
                        "contentHash": "sha256:" + "4" * 64,
                    },
                ],
                "truncated": False,
            }
        elif name == "planning_memory.read":
            assert arguments == {
                "topics": [
                    {
                        "path": "memory/feedback/corrections.md",
                        "expectedModifiedVersion": "mtime:1:size:100",
                        "expectedContentHash": "sha256:" + "2" * 64,
                    },
                    {
                        "path": "memory/study/agentic-rl.md",
                        "expectedModifiedVersion": "mtime:2:size:100",
                        "expectedContentHash": "sha256:" + "3" * 64,
                    },
                ]
            }
            data = {
                "topics": [
                    {
                        "path": "memory/feedback/corrections.md",
                        "modifiedVersion": "mtime:1:size:100",
                        "contentHash": "sha256:" + "2" * 64,
                        "content": "Prefer current explicit goals over older plans.",
                    },
                    {
                        "path": "memory/study/agentic-rl.md",
                        "modifiedVersion": "mtime:2:size:100",
                        "contentHash": "sha256:" + "3" * 64,
                        "content": "Continue reward modeling before policy optimization.",
                    },
                ]
            }
        elif name == "daily_note.context":
            data = {
                "resolvedDate": "2026-07-17",
                "dateFormat": "YYYY-MM-DD",
                "targetPath": "daily/2026-07-17.md",
                "targetExists": True,
                "targetContent": (
                    "---\ndate: 2026-07-17\n---\n\n- [x] Reviewed yesterday's notes\n\n"
                    "## 今日计划\n\n<!-- offeragent-plan -->\n\n## 随手记录\n\nKeep this.\n"
                ),
                "targetModifiedVersion": "mtime:4:size:180",
                "targetContentHash": "sha256:" + "5" * 64,
                "templatePath": "templates/daily.md",
                "templateContent": "---\ndate: {{date}}\n---\n\n## 今日计划\n",
                "templateModifiedVersion": "mtime:5:size:50",
                "templateContentHash": "sha256:" + "6" * 64,
            }
        else:
            assert name == "vault.changes.apply"
            operations = cast(Sequence[Mapping[str, object]], arguments["operations"])
            assert [item["path"] for item in operations] == [
                "daily/2026-07-17.md",
                "memory/study/agentic-rl.md",
                "memory/MEMORY.md",
            ]
            assert operations[0]["op"] == "replace"
            assert operations[0]["find"] == "## 今日计划\n\n<!-- offeragent-plan -->"
            assert "- [x] Reviewed" not in cast(str, operations[0]["replacement"])
            assert all("- [ ]" in cast(str, item.get("replacement", "")) for item in operations[:1])
            data = {
                "batchId": arguments["batchId"],
                "state": "applied",
                "checkpointRef": "refs/offeragent/checkpoints/daily-memory",
                "paths": [item["path"] for item in operations],
                "beforeStateHash": "sha256:" + "7" * 64,
                "afterStateHash": "sha256:" + "8" * 64,
                "undoAvailable": True,
            }
            side_effects = [
                {
                    "kind": "file_modified",
                    "resource": cast(str, item["path"]),
                    "beforeHash": "sha256:" + "9" * 64,
                    "afterHash": "sha256:" + "a" * 64,
                    "confirmed": True,
                }
                for item in operations
            ]
        self._completions.append(
            asyncio.create_task(
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
                            "summary": f"Completed {name}.",
                            "data": data,
                            "sourceRefs": source_refs,
                            "sideEffects": side_effects,
                        },
                    },
                    CancellationScope(name=f"completion-{name}"),
                    context=ApplicationCommandContext(transport="stdio", client_id="obsidian-plugin"),
                )
            )
        )

    async def join(self) -> None:
        await asyncio.gather(*self._completions)


@pytest.mark.asyncio
async def test_scripted_daily_plan_selects_memory_semantically_and_applies_one_evidence_safe_batch() -> None:
    from offeragent_harness.runtime.plugin_tools import plugin_tool_definitions

    definitions = {definition.name: definition for definition in plugin_tool_definitions()}

    def call(name: str, arguments: Mapping[str, object], index: int) -> ToolCall:
        definition = definitions[name]
        return ToolCall(
            tool_call_id=f"call_planning_{index}",
            run_id="run_planning",
            workspace_id="ws_vault",
            name=name,
            version=definition.version,
            arguments=arguments,
            args_hash=canonical_json_sha256(arguments),
            idempotency_key=f"planning-{index}",
            deadline=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
            lineage=AgentLineage.root("run_planning"),
            definition_fingerprint=definition.fingerprint,
            result_sensitivity=definition.result_sensitivity,
        )

    calls = (
        call("agent_contract.read", {}, 1),
        call("planning_memory.list", {}, 2),
        call(
            "planning_memory.read",
            {
                "topics": [
                    {
                        "path": "memory/feedback/corrections.md",
                        "expectedModifiedVersion": "mtime:1:size:100",
                        "expectedContentHash": "sha256:" + "2" * 64,
                    },
                    {
                        "path": "memory/study/agentic-rl.md",
                        "expectedModifiedVersion": "mtime:2:size:100",
                        "expectedContentHash": "sha256:" + "3" * 64,
                    },
                ]
            },
            3,
        ),
        call("daily_note.context", {}, 4),
        call(
            "vault.changes.apply",
            {
                "batchId": "daily_memory_20260717",
                "task": "Fill today's plan and consolidate its cross-day Study Memory",
                "operations": [
                    {
                        "op": "replace",
                        "path": "daily/2026-07-17.md",
                        "find": "## 今日计划\n\n<!-- offeragent-plan -->",
                        "replacement": "## 今日计划\n\n- [ ] 复习 Reward Model 来源: Agentic RL Study Memory",
                        "expectedContentHash": "sha256:" + "5" * 64,
                    },
                    {
                        "op": "replace",
                        "path": "memory/study/agentic-rl.md",
                        "find": "Continue reward modeling before policy optimization.",
                        "replacement": "- [ ] Continue reward modeling, then policy optimization across study days.",
                        "expectedContentHash": "sha256:" + "3" * 64,
                    },
                    {
                        "op": "replace",
                        "path": "memory/MEMORY.md",
                        "find": "Agentic RL - old order",
                        "replacement": "Agentic RL - reward modeling before policy optimization",
                        "expectedContentHash": "sha256:" + "b" * 64,
                    },
                ],
            },
            5,
        ),
    )
    executor = PluginToolExecutor()

    async def unused(*args: object) -> Mapping[str, object]:
        del args
        return {}

    handlers: dict[str, ApplicationCommandHandler] = {
        method: cast(ApplicationCommandHandler, unused) for method in COMMAND_REGISTRY
    }
    handlers.update(plugin_tool_completion_handlers(executor=executor))
    recorder = _PlanningProductRecorder(
        RuntimeApplicationCommandDispatcher(application=_ReadyApplication(), handlers=handlers)
    )
    state = RunState("ws_vault", "ses_planning", "turn_planning", "run_planning", AgentLineage.root("run_planning"))
    budget = BudgetLedger(
        RunBudget(8, 8, 2, 60, 100, 100, Decimal("1"), 1_000, 1),
        started_at=datetime.now(timezone.utc),
    )

    result = await run_agent_loop(
        state,
        planner=_SequentialProductPlanner(calls, "今日计划已更新; 所有项目仍未完成, 计划不是 Study Evidence。"),
        tool_kernel=_PluginDefinitionKernel(definitions, executor),
        recorder=recorder,
        budget=budget,
        cancellation=CancellationScope(name="planning-run"),
        now=lambda: datetime.now(timezone.utc),
    )
    await recorder.join()

    assert result.phase is RunPhase.COMPLETED
    assert [item["name"] for item in recorder.started_calls] == [item.name for item in calls]
    assert sum(item["name"] == "vault.changes.apply" for item in recorder.started_calls) == 1
    assert "计划不是 Study Evidence" in result.assistant_text


class _InterviewIngestionRecorder:
    def __init__(self, dispatcher: RuntimeApplicationCommandDispatcher) -> None:
        self.started_calls: list[Mapping[str, object]] = []
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
        if event_type != "tool.started":
            return
        call = cast(Mapping[str, object], payload["call"])
        self.started_calls.append(call)
        name = cast(str, call["name"])
        arguments = cast(Mapping[str, object], call["arguments"])
        side_effects: list[Mapping[str, object]] = []
        if name == "agent_contract.read":
            data: Mapping[str, object] = {
                "path": "agent.md",
                "content": "# OfferAgent\n\nIngest one submission as one atomic knowledge change.",
                "contentHash": "sha256:" + "1" * 64,
            }
        elif name == "interview_catalog.search":
            assert arguments["sourceUrl"] == "https://example.com/interview/42"
            assert arguments["questionTerms"] == ["Node.js event loop", "database transaction isolation"]
            data = {
                "experienceCandidates": [
                    {
                        "path": "interviews/experiences/acme-bob.md",
                        "experienceId": "exp_bob",
                        "sourceKind": "images",
                        "company": "Acme",
                        "role": "Backend Engineer",
                        "candidate": "bob",
                        "eventDate": "2026-06-10",
                        "round": "technical-1",
                        "exactSourceMatch": False,
                        "modifiedVersion": "mtime:10:size:300",
                        "contentHash": "sha256:" + "2" * 64,
                    }
                ],
                "questionCandidates": [
                    {
                        "path": "interviews/questions/node-event-loop.md",
                        "questionId": "question_node_event_loop",
                        "title": "Explain the Node.js event loop",
                        "answerState": "draft",
                        "frequency": 3,
                        "matchedTerms": ["Node.js event loop"],
                        "modifiedVersion": "mtime:11:size:400",
                        "contentHash": "sha256:" + "3" * 64,
                    }
                ],
                "truncated": False,
            }
        elif name == "vault.read":
            path = cast(str, arguments["path"])
            assert path in {
                "interviews/experiences/acme-bob.md",
                "interviews/questions/node-event-loop.md",
            }
            if path.endswith("acme-bob.md"):
                content = "---\ncandidate: bob\nevent-date: 2026-06-10\nround: technical-1\n---\n"
                digest = "sha256:" + "2" * 64
                version = "mtime:10:size:300"
            else:
                content = "---\nfrequency: 3\nanswer-state: draft\n---\n# Explain the Node.js event loop\n"
                digest = "sha256:" + "3" * 64
                version = "mtime:11:size:400"
            data = {
                "path": path,
                "modifiedVersion": version,
                "contentHash": digest,
                "content": content,
                "lineStart": 1,
                "lineEnd": len(content.splitlines()),
                "truncated": False,
            }
        else:
            assert name == "vault.changes.apply"
            operations = cast(Sequence[Mapping[str, object]], arguments["operations"])
            assert [operation["path"] for operation in operations] == [
                "interviews/experiences/acme-alice-2026-07-01.md",
                "interviews/questions/node-event-loop.md",
                "interviews/questions/database-transaction-isolation.md",
                "interviews/INDEX.md",
            ]
            assert sum(operation["op"] == "create" for operation in operations) == 2
            assert operations[1]["op"] == "replace"
            assert "frequency: 4" in cast(str, operations[1]["replacement"])
            assert "answer-state: needs-research" in cast(str, operations[2]["content"])
            experience_content = cast(str, operations[0]["content"])
            assert "source-url: https://example.com/interview/42" in experience_content
            assert "data:image" not in experience_content
            assert "raw transcript" not in experience_content
            data = {
                "batchId": arguments["batchId"],
                "state": "applied",
                "checkpointRef": "refs/offeragent/checkpoints/interview-42",
                "paths": [operation["path"] for operation in operations],
                "beforeStateHash": "sha256:" + "4" * 64,
                "afterStateHash": "sha256:" + "5" * 64,
                "undoAvailable": True,
            }
            side_effects = [
                {
                    "kind": "file_created" if operation["op"] == "create" else "file_modified",
                    "resource": cast(str, operation["path"]),
                    "beforeHash": None if operation["op"] == "create" else "sha256:" + "6" * 64,
                    "afterHash": "sha256:" + "7" * 64,
                    "confirmed": True,
                }
                for operation in operations
            ]
        self._completions.append(
            asyncio.create_task(
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
                            "summary": f"Completed {name}.",
                            "data": data,
                            "sourceRefs": [],
                            "sideEffects": side_effects,
                        },
                    },
                    CancellationScope(name=f"completion-{name}"),
                    context=ApplicationCommandContext(transport="stdio", client_id="obsidian-plugin"),
                )
            )
        )

    async def join(self) -> None:
        await asyncio.gather(*self._completions)


@pytest.mark.asyncio
async def test_scripted_interview_submission_deduplicates_questions_in_one_atomic_batch() -> None:
    from offeragent_harness.runtime.plugin_tools import plugin_tool_definitions

    definitions = {definition.name: definition for definition in plugin_tool_definitions()}

    def call(name: str, arguments: Mapping[str, object], index: int) -> ToolCall:
        definition = definitions[name]
        return ToolCall(
            tool_call_id=f"call_interview_{index}",
            run_id="run_interview",
            workspace_id="ws_vault",
            name=name,
            version=definition.version,
            arguments=arguments,
            args_hash=canonical_json_sha256(arguments),
            idempotency_key=f"interview-{index}",
            deadline=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
            lineage=AgentLineage.root("run_interview"),
            definition_fingerprint=definition.fingerprint,
            result_sensitivity=definition.result_sensitivity,
        )

    calls = (
        call("agent_contract.read", {}, 1),
        call(
            "interview_catalog.search",
            {
                "sourceUrl": "https://example.com/interview/42",
                "sourceFingerprint": "sha256:" + "8" * 64,
                "company": "Acme",
                "role": "Backend Engineer",
                "questionTerms": ["Node.js event loop", "database transaction isolation"],
            },
            2,
        ),
        call(
            "vault.read",
            {
                "path": "interviews/experiences/acme-bob.md",
                "expectedModifiedVersion": "mtime:10:size:300",
                "expectedContentHash": "sha256:" + "2" * 64,
            },
            3,
        ),
        call(
            "vault.read",
            {
                "path": "interviews/questions/node-event-loop.md",
                "expectedModifiedVersion": "mtime:11:size:400",
                "expectedContentHash": "sha256:" + "3" * 64,
            },
            4,
        ),
        call(
            "vault.changes.apply",
            {
                "batchId": "interview_acme_42",
                "task": "Ingest one ordered Interview Submission and deduplicate its questions",
                "operations": [
                    {
                        "op": "create",
                        "path": "interviews/experiences/acme-alice-2026-07-01.md",
                        "content": (
                            "---\ntype: interview-experience\n"
                            "experience-id: exp_alice_20260701\nsource-kind: url-images\n"
                            "source-url: https://example.com/interview/42\nsource-fingerprint: sha256:"
                            + "8"
                            * 64
                            + "\ncompany: Acme\nrole: Backend Engineer\ncandidate: alice\nevent-date: 2026-07-01\n"
                            "round: technical-2\n---\n# Acme Backend Interview\n\n"
                            "## Context\nTwo ordered screenshots.\n## Questions\n"
                            "- [[../questions/node-event-loop]]\n"
                            "- [[../questions/database-transaction-isolation]]\n"
                        ),
                        "expectedContentHash": "absent",
                    },
                    {
                        "op": "replace",
                        "path": "interviews/questions/node-event-loop.md",
                        "find": "frequency: 3",
                        "replacement": "frequency: 4\n\n- exp_alice_20260701 · technical-2",
                        "expectedContentHash": "sha256:" + "3" * 64,
                    },
                    {
                        "op": "create",
                        "path": "interviews/questions/database-transaction-isolation.md",
                        "content": (
                            "---\ntype: interview-question\nquestion-id: question_database_isolation\n"
                            "title: Explain database transaction isolation\n"
                            "answer-state: needs-research\nfrequency: 1\n---\n"
                            "# Explain database transaction isolation\n\n"
                            "## Occurrences\n- exp_alice_20260701 · technical-2\n"
                        ),
                        "expectedContentHash": "absent",
                    },
                    {
                        "op": "append",
                        "path": "interviews/INDEX.md",
                        "content": "\n- [[experiences/acme-alice-2026-07-01]]",
                        "expectedContentHash": "sha256:" + "9" * 64,
                    },
                ],
            },
            5,
        ),
    )
    executor = PluginToolExecutor()

    async def unused(*args: object) -> Mapping[str, object]:
        del args
        return {}

    handlers: dict[str, ApplicationCommandHandler] = {
        method: cast(ApplicationCommandHandler, unused) for method in COMMAND_REGISTRY
    }
    handlers.update(plugin_tool_completion_handlers(executor=executor))
    recorder = _InterviewIngestionRecorder(
        RuntimeApplicationCommandDispatcher(application=_ReadyApplication(), handlers=handlers)
    )
    result = await run_agent_loop(
        RunState("ws_vault", "ses_interview", "turn_interview", "run_interview", AgentLineage.root("run_interview")),
        planner=_SequentialProductPlanner(
            calls,
            "已把一份按序图文提交原子入库; 复用一道题并新增一道 needs-research 题, 未保存原始素材。",
        ),
        tool_kernel=_PluginDefinitionKernel(definitions, executor),
        recorder=recorder,
        budget=BudgetLedger(
            RunBudget(8, 8, 2, 60, 100, 100, Decimal("1"), 1_000, 1),
            started_at=datetime.now(timezone.utc),
        ),
        cancellation=CancellationScope(name="interview-run"),
        now=lambda: datetime.now(timezone.utc),
    )
    await recorder.join()

    assert result.phase is RunPhase.COMPLETED
    assert [item["name"] for item in recorder.started_calls] == [item.name for item in calls]
    assert sum(item["name"] == "vault.changes.apply" for item in recorder.started_calls) == 1
    assert "未保存原始素材" in result.assistant_text


class _ProjectTrainingRecorder:
    def __init__(self, dispatcher: RuntimeApplicationCommandDispatcher) -> None:
        self.started_calls: list[Mapping[str, object]] = []
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
        if event_type != "tool.started":
            return
        call = cast(Mapping[str, object], payload["call"])
        self.started_calls.append(call)
        name = cast(str, call["name"])
        arguments = cast(Mapping[str, object], call["arguments"])
        source_refs: list[Mapping[str, object]] = []
        side_effects: list[Mapping[str, object]] = []
        if name == "agent_contract.read":
            data: Mapping[str, object] = {
                "path": "agent.md",
                "content": "# OfferAgent\n\nGround Project Interview Training in registered evidence.",
                "contentHash": "sha256:" + "1" * 64,
            }
        elif name == "interview_catalog.search":
            data = {
                "experienceCandidates": [],
                "questionCandidates": [
                    {
                        "path": "interviews/questions/stale-cache-invalidation.md",
                        "questionId": "question_stale_cache",
                        "title": "How do you prevent stale cache invalidation?",
                        "answerState": "needs-research",
                        "frequency": 2,
                        "matchedTerms": ["cache invalidation project risk"],
                        "modifiedVersion": "mtime:20:size:240",
                        "contentHash": "sha256:" + "2" * 64,
                    }
                ],
                "truncated": False,
            }
        elif name == "project.search":
            assert arguments["projectId"] == "offeragent"
            data = {
                "projectId": "offeragent",
                "entries": [
                    {
                        "path": "src/cache.py",
                        "modifiedVersion": "mtime:21:size:500",
                        "contentHash": "sha256:" + "3" * 64,
                        "snippets": [
                            {
                                "lineStart": 10,
                                "lineEnd": 18,
                                "content": "if current.version == expected_version: cache.delete(key)",
                            }
                        ],
                    }
                ],
                "truncated": False,
            }
        elif name == "project.read":
            assert arguments["projectId"] == "offeragent"
            assert arguments["path"] == "src/cache.py"
            data = {
                "projectId": "offeragent",
                "path": "src/cache.py",
                "lineStart": 10,
                "lineEnd": 18,
                "modifiedVersion": "mtime:21:size:500",
                "contentHash": "sha256:" + "3" * 64,
                "content": (
                    "# A delayed invalidation is a no-op after refresh.\n"
                    "if current.version == expected_version:\n    cache.delete(key)"
                ),
                "truncated": False,
            }
            source_refs = [
                {
                    "type": "project",
                    "projectId": "offeragent",
                    "path": "src/cache.py",
                    "contentHash": "sha256:" + "3" * 64,
                    "modifiedVersion": "mtime:21:size:500",
                    "lineStart": 10,
                    "lineEnd": 18,
                    "freshness": "fresh",
                }
            ]
        elif name == "vault.read":
            path = cast(str, arguments["path"])
            if path == "interviews/questions/stale-cache-invalidation.md":
                content = "# How do you prevent stale cache invalidation?\n"
                digest = "sha256:" + "2" * 64
                version = "mtime:20:size:240"
            elif path == "projects/offeragent/profile.md":
                content = (
                    "---\nproject-id: offeragent\nownership: solo\n---\n"
                    "# Project Interview Profile\n\n<!-- training-outcomes -->\n\n## Unrelated\nKeep me.\n"
                )
                digest = "sha256:" + "4" * 64
                version = "mtime:22:size:180"
            else:
                assert path == "projects/offeragent/index.md"
                content = "# Project Answers\n\n- Existing answer\n"
                digest = "sha256:" + "5" * 64
                version = "mtime:23:size:40"
            data = {
                "path": path,
                "modifiedVersion": version,
                "contentHash": digest,
                "content": content,
                "lineStart": 1,
                "lineEnd": len(content.splitlines()),
                "truncated": False,
            }
        else:
            assert name == "vault.changes.apply"
            operations = cast(Sequence[Mapping[str, object]], arguments["operations"])
            assert [operation["path"] for operation in operations] == [
                "projects/offeragent/profile.md",
                "projects/offeragent/answers/stale-cache-invalidation.md",
                "projects/offeragent/index.md",
            ]
            assert operations[0]["op"] == "replace"
            assert operations[0]["find"] == "<!-- training-outcomes -->"
            assert "## Unrelated\nKeep me." not in cast(str, operations[0]["replacement"])
            assert operations[1]["op"] == "create"
            answer = cast(str, operations[1]["content"])
            assert "project://offeragent/src/cache.py#L10-L18" in answer
            assert "production latency: unknown" in answer
            assert "general-answer" not in answer
            assert operations[2]["op"] == "append"
            data = {
                "batchId": arguments["batchId"],
                "state": "applied",
                "checkpointRef": "refs/offeragent/checkpoints/project-training",
                "paths": [operation["path"] for operation in operations],
                "beforeStateHash": "sha256:" + "6" * 64,
                "afterStateHash": "sha256:" + "7" * 64,
                "undoAvailable": True,
            }
            side_effects = [
                {
                    "kind": "file_created" if operation["op"] == "create" else "file_modified",
                    "resource": cast(str, operation["path"]),
                    "beforeHash": None if operation["op"] == "create" else "sha256:" + "8" * 64,
                    "afterHash": "sha256:" + "9" * 64,
                    "confirmed": True,
                }
                for operation in operations
            ]
        self._completions.append(
            asyncio.create_task(
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
                            "summary": f"Completed {name}.",
                            "data": data,
                            "sourceRefs": source_refs,
                            "sideEffects": side_effects,
                        },
                    },
                    CancellationScope(name=f"completion-{name}"),
                    context=ApplicationCommandContext(transport="stdio", client_id="obsidian-plugin"),
                )
            )
        )

    async def join(self) -> None:
        await asyncio.gather(*self._completions)


@pytest.mark.asyncio
async def test_project_training_stays_in_conversation_until_confirmed_outcome_batch() -> None:
    from offeragent_harness.runtime.plugin_tools import plugin_tool_definitions

    definitions = {definition.name: definition for definition in plugin_tool_definitions()}
    executor = PluginToolExecutor()

    async def unused(*args: object) -> Mapping[str, object]:
        del args
        return {}

    handlers: dict[str, ApplicationCommandHandler] = {
        method: cast(ApplicationCommandHandler, unused) for method in COMMAND_REGISTRY
    }
    handlers.update(plugin_tool_completion_handlers(executor=executor))
    recorder = _ProjectTrainingRecorder(
        RuntimeApplicationCommandDispatcher(application=_ReadyApplication(), handlers=handlers)
    )

    def call(run_id: str, name: str, arguments: Mapping[str, object], index: int) -> ToolCall:
        definition = definitions[name]
        return ToolCall(
            tool_call_id=f"call_{run_id}_{index}",
            run_id=run_id,
            workspace_id="ws_vault",
            name=name,
            version=definition.version,
            arguments=arguments,
            args_hash=canonical_json_sha256(arguments),
            idempotency_key=f"{run_id}-{index}",
            deadline=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
            lineage=AgentLineage.root(run_id),
            definition_fingerprint=definition.fingerprint,
            result_sensitivity=definition.result_sensitivity,
        )

    def evidence_calls(run_id: str, offset: int) -> tuple[ToolCall, ToolCall]:
        return (
            call(
                run_id,
                "project.search",
                {
                    "projectId": "offeragent",
                    "query": "stale cache invalidation expected version",
                    "limit": 5,
                },
                offset,
            ),
            call(
                run_id,
                "project.read",
                {
                    "projectId": "offeragent",
                    "path": "src/cache.py",
                    "lineStart": 10,
                    "lineEnd": 18,
                    "expectedModifiedVersion": "mtime:21:size:500",
                    "expectedContentHash": "sha256:" + "3" * 64,
                },
                offset + 1,
            ),
        )

    async def run_turn(run_id: str, calls: Sequence[ToolCall], response: str) -> RunState:
        result = await run_agent_loop(
            RunState("ws_vault", "ses_training", f"turn_{run_id}", run_id, AgentLineage.root(run_id)),
            planner=_SequentialProductPlanner(calls, response),
            tool_kernel=_PluginDefinitionKernel(definitions, executor),
            recorder=recorder,
            budget=BudgetLedger(
                RunBudget(12, 12, 2, 60, 100, 100, Decimal("1"), 1_000, 1),
                started_at=datetime.now(timezone.utc),
            ),
            cancellation=CancellationScope(name=run_id),
            now=lambda: datetime.now(timezone.utc),
        )
        await recorder.join()
        return result

    selected_run = "run_training_selected"
    selected = await run_turn(
        selected_run,
        (call(selected_run, "agent_contract.read", {}, 1), *evidence_calls(selected_run, 2)),
        "[Project Interview Question]\nFor the selected stale-invalidation question, what invariant did you enforce?",
    )
    assert selected.assistant_text.count("?") == 1
    assert not any(
        item["runId"] == selected_run and item["name"] == "interview_catalog.search" for item in recorder.started_calls
    )

    question_run = "run_training_question"
    question_calls = (
        call(question_run, "agent_contract.read", {}, 1),
        call(
            question_run,
            "interview_catalog.search",
            {
                "company": "Acme",
                "role": "Backend Engineer",
                "questionTerms": ["cache invalidation project risk"],
            },
            2,
        ),
        call(
            question_run,
            "vault.read",
            {
                "path": "interviews/questions/stale-cache-invalidation.md",
                "expectedModifiedVersion": "mtime:20:size:240",
                "expectedContentHash": "sha256:" + "2" * 64,
            },
            3,
        ),
        *evidence_calls(question_run, 4),
    )
    question = await run_turn(
        question_run,
        question_calls,
        "[Project Interview Question]\nHow did you prevent a stale request from invalidating a newer cache value?",
    )
    assert question.assistant_text.count("?") == 1

    followup_run = "run_training_followup"
    followup = await run_turn(
        followup_run,
        (call(followup_run, "agent_contract.read", {}, 1), *evidence_calls(followup_run, 2)),
        (
            "Your claim that every invalidation deletes the key contradicts the version guard at "
            "project://offeragent/src/cache.py#L10-L18. What happens after a newer refresh?"
        ),
    )
    assert "contradicts" in followup.assistant_text
    assert followup.assistant_text.count("?") == 1

    feedback_run = "run_training_feedback"
    feedback = await run_turn(
        feedback_run,
        (call(feedback_run, "agent_contract.read", {}, 1), *evidence_calls(feedback_run, 2)),
        (
            "Training Feedback: facts grounded; responsibility stated; trade-off is stale no-op; "
            "metrics remain unknown; failure case covered; follow-up readiness needs rehearsal; duration was concise. "
            "Confirm saving this refined outcome?"
        ),
    )
    assert all(
        label in feedback.assistant_text
        for label in (
            "facts",
            "responsibility",
            "trade-off",
            "metrics",
            "failure case",
            "follow-up",
            "duration",
        )
    )
    assert not any(token in feedback.assistant_text for token in ("/10", "/100", "total score"))
    assert not any(item["name"] == "vault.changes.apply" for item in recorder.started_calls)

    save_run = "run_training_save"
    save_calls = (
        call(save_run, "agent_contract.read", {}, 1),
        *evidence_calls(save_run, 2),
        call(
            save_run,
            "vault.read",
            {
                "path": "projects/offeragent/profile.md",
                "expectedModifiedVersion": "mtime:22:size:180",
                "expectedContentHash": "sha256:" + "4" * 64,
            },
            4,
        ),
        call(
            save_run,
            "vault.read",
            {
                "path": "projects/offeragent/index.md",
                "expectedModifiedVersion": "mtime:23:size:40",
                "expectedContentHash": "sha256:" + "5" * 64,
            },
            5,
        ),
        call(
            save_run,
            "vault.changes.apply",
            {
                "batchId": "project_training_stale_cache_v1",
                "task": "Save the user-confirmed Project Interview Training Outcome",
                "operations": [
                    {
                        "op": "replace",
                        "path": "projects/offeragent/profile.md",
                        "find": "<!-- training-outcomes -->",
                        "replacement": (
                            "<!-- training-outcomes -->\n\n## Stale cache invalidation\n"
                            "- Stable fact: expected-version guard prevents stale deletion.\n"
                            "- Weak point: production metrics are unknown.\n"
                            "- Likely follow-up: delayed invalidation after refresh.\n"
                            "- Retrain: explain race, invariant, guard, and evidence gap."
                        ),
                        "expectedContentHash": "sha256:" + "4" * 64,
                    },
                    {
                        "op": "create",
                        "path": "projects/offeragent/answers/stale-cache-invalidation.md",
                        "content": (
                            "---\ntype: project-answer\nproject-id: offeragent\n"
                            "question: stale-cache-invalidation\n---\n# Stale cache invalidation\n\n"
                            "The expected-version guard makes delayed invalidation a no-op.\n\n"
                            "Evidence: project://offeragent/src/cache.py#L10-L18 sha256:"
                            + "3" * 64
                            + "\n\nEvidence gaps: production latency: unknown.\n"
                        ),
                        "expectedContentHash": "absent",
                    },
                    {
                        "op": "append",
                        "path": "projects/offeragent/index.md",
                        "content": "\n- [[answers/stale-cache-invalidation]]",
                        "expectedContentHash": "sha256:" + "5" * 64,
                    },
                ],
            },
            6,
        ),
    )
    saved = await run_turn(save_run, save_calls, "Confirmed Training Outcome saved with exact Project Evidence.")
    assert saved.phase is RunPhase.COMPLETED
    assert sum(item["name"] == "vault.changes.apply" for item in recorder.started_calls) == 1
