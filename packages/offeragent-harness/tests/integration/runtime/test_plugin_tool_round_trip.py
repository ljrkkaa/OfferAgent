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
