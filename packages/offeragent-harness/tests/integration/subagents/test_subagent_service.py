from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.agent import BudgetDelta, BudgetLedger, RunBudget
from offeragent_harness.agent.state import RunState
from offeragent_harness.permissions import CapabilityScope, PermissionMode, RiskClass
from offeragent_harness.ports import ArtifactMetadata, ArtifactState, OperationCancelled, Sensitivity
from offeragent_harness.ports.subagents import (
    ParentRunAuthority,
    StoredSubagentResultArtifact,
)
from offeragent_harness.runtime.subagent_runtime import ProtocolSubagentEventFactory
from offeragent_harness.sessions import AgentLineage, Run, RunKind, RunStatus
from offeragent_harness.subagents import (
    AgentBudget,
    AgentCancelCommand,
    AgentDefinitionCatalog,
    AgentSendCommand,
    AgentSpawnCommand,
    AgentWaitCommand,
    ChildRunScheduler,
    ContextForker,
    ContextForkMode,
    ContextSnapshot,
    DurableMailbox,
    EffectiveToolScope,
    ExecutionPriority,
    MailboxMode,
    ScopeDeriver,
    SubagentBudgetTree,
    SubagentLifetime,
    SubagentResult,
    SubagentRunRecord,
    SubagentRunStatus,
    SubagentService,
    SubagentServiceError,
    WaitMode,
    builtin_agent_definitions,
    subagent_tool_definitions,
)
from offeragent_harness.subagents.models import AgentUsage
from offeragent_harness.subagents.serialization import (
    context_to_value,
    run_record_from_value,
    run_record_to_value,
)
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
    RecordingEventSink,
)
from offeragent_harness.tools import canonical_json_sha256

NOW = datetime(2026, 7, 13, 8, tzinfo=timezone.utc)


class _Code(str, Enum):
    USER = "user"


@dataclass(frozen=True)
class _Reason:
    code: _Code
    message: str
    requested_at: datetime


class _CancellationSource:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: Any = None
        self.closed = False

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> Any:
        return self._reason

    async def cancel(self, reason: Any) -> bool:
        if self.cancelled:
            return False
        self._reason = reason
        self._event.set()
        return True

    async def wait(self) -> Any:
        await self._event.wait()
        return self._reason

    def checkpoint(self) -> None:
        if self.cancelled:
            raise OperationCancelled(self._reason)

    async def close(self) -> None:
        self.closed = True


class _CancellationFactory:
    def __init__(self) -> None:
        self.sources: dict[str, _CancellationSource] = {}

    def create(self, *, root_run_id: str, parent_run_id: str, child_run_id: str) -> _CancellationSource:
        del root_run_id, parent_run_id
        source = _CancellationSource()
        self.sources[child_run_id] = source
        return source


class _RootAuthorities:
    def __init__(self, authority: ParentRunAuthority) -> None:
        self.authority = authority

    async def authority_for(self, run_id: str) -> ParentRunAuthority:
        if run_id != self.authority.lineage.run_id:
            raise ValueError("unknown root")
        return self.authority


class _Runner:
    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.started = asyncio.Event()
        self.executions: list[Any] = []
        self.messages: list[Any] = []

    async def deliver_message(self, run_id: str, message: Any) -> bool:
        self.messages.append((run_id, message))
        return True

    async def execute(self, execution: Any, cancellation: Any) -> SubagentResult:
        self.executions.append(execution)
        self.started.set()
        if self.block:
            await cancellation.wait()
            cancellation.checkpoint()
        return SubagentResult(
            execution.record.run_id,
            "completed",
            "Verified child conclusion",
            ({"title": "Finding", "summary": "Evidence-backed"},),
            (),
            (),
            (),
            (),
            {
                "inputTokens": 20,
                "outputTokens": 10,
                "modelCalls": 1,
                "toolCalls": 0,
                "wallTimeSeconds": 0.1,
                "artifactBytes": 0,
                "childCount": 0,
                "costMicros": 5,
            },
        )


class _Artifacts:
    def __init__(self) -> None:
        self.items: dict[str, tuple[Any, ArtifactMetadata]] = {}

    async def store(self, record: Any, result: Any, cancellation: Any) -> StoredSubagentResultArtifact:
        cancellation.checkpoint()
        artifact_id = f"art_result_{len(self.items) + 1}"
        state = {
            "completed": ArtifactState.COMPLETE,
            "cancelled": ArtifactState.CANCELLED,
            "failed": ArtifactState.FAILED,
            "interrupted": ArtifactState.PARTIAL,
        }.get(result.status, ArtifactState.UNVERIFIED)
        metadata = ArtifactMetadata(
            artifact_id,
            record.workspace_id,
            record.run_id,
            "application/vnd.offeragent.subagent-result+json",
            512,
            "sha256:" + "a" * 64,
            Sensitivity.WORKSPACE,
            state,
            NOW,
            {},
        )
        self.items[record.run_id] = (result, metadata)
        return StoredSubagentResultArtifact(metadata)


def _budget(**changes: int | float) -> AgentBudget:
    values: dict[str, Any] = {
        "input_tokens": 2_000,
        "output_tokens": 1_000,
        "model_calls": 4,
        "tool_calls": 4,
        "wall_time_seconds": 60,
        "artifact_bytes": 64_000,
        "child_count": 2,
        "cost_micros": 1_000_000,
    }
    values.update(changes)
    return AgentBudget(**values)


def _scope() -> CapabilityScope:
    tools = subagent_tool_definitions()
    return CapabilityScope(
        frozenset(item.name for item in tools),
        frozenset(),
        frozenset({RiskClass.READ, RiskClass.WRITE, RiskClass.EXECUTE}),
        frozenset(capability for item in tools for capability in item.required_capabilities),
        False,
        False,
    )


def _command(*, call_id: str = "call_spawn", task: str = "Review implementation") -> AgentSpawnCommand:
    return AgentSpawnCommand(
        "run_root",
        call_id,
        task,
        "general",
        ContextForkMode.NONE,
        (),
        (),
        _scope(),
        PermissionMode.READ_ONLY,
        {},
        {},
        _budget(),
        SubagentLifetime.PARENT,
        ExecutionPriority.NORMAL,
    )


def _build(
    *,
    block: bool = False,
    unit_of_work: Any | None = None,
    ledger: BudgetLedger | None = None,
) -> tuple[
    SubagentService,
    Any,
    RecordingEventSink,
    ChildRunScheduler,
    _Runner,
    _Artifacts,
]:
    tools = subagent_tool_definitions()
    scope = _scope()
    profiles = builtin_agent_definitions(
        available_tools=frozenset(item.name for item in tools),
        root_capabilities=scope.root_capabilities,
    )
    catalog = AgentDefinitionCatalog(workspace_id="ws_test", builtins=profiles)
    catalog.rescan(expected_revision=0)
    authority = ParentRunAuthority(
        "ws_test",
        "ses_test",
        "turn_test",
        AgentLineage.root("run_root"),
        PermissionMode.NORMAL,
        scope,
        tools,
        "sha256:" + "1" * 64,
        _budget(
            input_tokens=20_000,
            output_tokens=10_000,
            model_calls=20,
            tool_calls=30,
            wall_time_seconds=600,
            artifact_bytes=1_000_000,
            child_count=8,
            cost_micros=10_000_000,
        ),
        NOW + timedelta(minutes=10),
        {"summary": "root context"},
        {"provider": "fake"},
        True,
        True,
    )
    uow = unit_of_work or InMemoryUnitOfWorkFactory()
    sink = RecordingEventSink()
    clock = ManualClock(NOW)
    cancellations = _CancellationFactory()
    scheduler = ChildRunScheduler(cancellations)
    runner = _Runner(block=block)
    artifacts = _Artifacts()
    root_ledger = ledger or BudgetLedger(
        RunBudget(100, 100, 4, 1_000, 100_000, 100_000, Decimal("100"), 10_000_000, 20),
        started_at=NOW,
    )
    tree = SubagentBudgetTree(
        root_ledger,
        retained_final_budget=_budget(
            input_tokens=100,
            output_tokens=100,
            model_calls=1,
            tool_calls=0,
            wall_time_seconds=10,
            artifact_bytes=1_024,
            child_count=0,
            cost_micros=0,
        ),
    )
    service = SubagentService(
        workspace_id="ws_test",
        worker_id="worker_test",
        unit_of_work=uow,
        event_sink=sink,
        clock=clock,
        ids=DeterministicIdGenerator(),
        catalog=catalog,
        authorities=_RootAuthorities(authority),
        context_forker=ContextForker(DeterministicIdGenerator(), clock),
        scope_deriver=ScopeDeriver(scope),
        budget_tree=tree,
        scheduler=scheduler,
        mailbox=DurableMailbox(uow, clock),
        runner=runner,
        result_artifacts=artifacts,
        event_factory=ProtocolSubagentEventFactory(),
    )
    return service, uow, sink, scheduler, runner, artifacts


async def _seed_expired_run(uow: InMemoryUnitOfWorkFactory, *, safe: bool) -> str:
    run_id = "run_orphan"
    lineage = AgentLineage.root("run_root").child(run_id, "general")
    tools = subagent_tool_definitions()
    allowed_risks = frozenset({RiskClass.READ}) if safe else frozenset({RiskClass.READ, RiskClass.WRITE})
    scope = CapabilityScope(
        frozenset(item.name for item in tools),
        frozenset(),
        allowed_risks,
        frozenset(capability for item in tools for capability in item.required_capabilities),
        False,
        False,
    )
    result_schema = builtin_agent_definitions(
        available_tools=frozenset(item.name for item in tools),
        root_capabilities=scope.root_capabilities,
    )[-1].result_schema
    content = {"workspaceId": "ws_test", "task": "recover"}
    context = ContextSnapshot(
        "ctx_orphan",
        "ws_test",
        "run_root",
        ContextForkMode.NONE,
        content,
        canonical_json_sha256(content),
        NOW,
    )
    budget = _budget()
    task = "Recover orphaned read-only analysis"
    record = SubagentRunRecord(
        run_id,
        "run_root",
        "run_root",
        ("run_root",),
        "ses_test",
        "turn_test",
        "ws_test",
        "trace_orphan",
        "call_orphan",
        "general",
        "1.0.0",
        task,
        "sha256:" + hashlib.sha256(task.encode()).hexdigest(),
        1,
        SubagentLifetime.PARENT,
        context.snapshot_id,
        PermissionMode.READ_ONLY if safe else PermissionMode.NORMAL,
        scope,
        EffectiveToolScope(
            {item.name: (item.version,) for item in tools if item.risk in allowed_risks},
            {},
            "sha256:" + "1" * 64,
        ),
        budget,
        AgentUsage(),
        NOW + timedelta(minutes=10),
        result_schema,
        SubagentRunStatus.RUNNING,
        "running",
        ExecutionPriority.NORMAL,
        NOW - timedelta(minutes=1),
        NOW - timedelta(seconds=31),
        "dead-worker",
        NOW - timedelta(seconds=1),
    )
    run = Run(
        run_id,
        "ses_test",
        "turn_test",
        "ws_test",
        lineage,
        RunKind.SUBAGENT,
        RunStatus.PLANNING,
        1,
        0,
        {"provider": "fake"},
        NOW - timedelta(minutes=1),
        NOW - timedelta(seconds=31),
        NOW + timedelta(minutes=10),
    )
    async with uow.begin() as transaction:
        await transaction.entities.put("subagent_runs", run_id, run_record_to_value(record), expected_revision=0)
        await transaction.entities.put("runs", run_id, run, expected_revision=0)
        await transaction.entities.put(
            "run_states", run_id, RunState("ws_test", "ses_test", "turn_test", run_id, lineage), expected_revision=0
        )
        await transaction.entities.put(
            "subagent_contexts",
            context.snapshot_id,
            context_to_value(context),
            expected_revision=0,
        )
        await transaction.entities.put(
            "subagent_budget_reservations",
            run_id,
            {"schemaVersion": 1, "state": "reserved"},
            expected_revision=0,
        )
        await transaction.commit()
    return run_id


def _restored_root_ledger() -> BudgetLedger:
    return BudgetLedger.restore(
        RunBudget(100, 100, 4, 1_000, 100_000, 100_000, Decimal("100"), 10_000_000, 20),
        started_at=NOW,
        used=BudgetDelta(),
        reserved=BudgetDelta(
            model_rounds=4,
            tool_calls=4,
            input_tokens=2_000,
            output_tokens=1_000,
            cost=Decimal("1"),
            artifact_bytes=64_000,
            subagents=1,
        ),
    )


class _Cleaner:
    def __init__(self) -> None:
        self.cleaned: list[str] = []

    async def cleanup(self, run_id: str) -> None:
        self.cleaned.append(run_id)


class _AckLossUnitOfWork:
    def __init__(self, inner: Any, owner: _AckLossFactory) -> None:
        self._inner = inner
        self._owner = owner

    @property
    def entities(self) -> Any:
        return self._inner.entities

    @property
    def events(self) -> Any:
        return self._inner.events

    @property
    def journal(self) -> Any:
        return self._inner.journal

    async def __aenter__(self) -> _AckLossUnitOfWork:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self._inner.__aexit__(exc_type, exc, traceback)

    async def commit(self) -> None:
        await self._inner.commit()
        self._owner.commits += 1
        if self._owner.commits == 1:
            raise OSError("commit ACK lost")

    async def rollback(self) -> None:
        await self._inner.rollback()


class _AckLossFactory:
    def __init__(self, inner: InMemoryUnitOfWorkFactory) -> None:
        self.inner = inner
        self.commits = 0

    def begin(self) -> _AckLossUnitOfWork:
        return _AckLossUnitOfWork(self.inner.begin(), self)


@pytest.mark.asyncio
async def test_spawn_uses_one_durable_child_run_and_returns_structured_result() -> None:
    service, uow, sink, scheduler, runner, artifacts = _build()
    token = ManualCancellationToken()
    handle = await service.spawn(_command(), token)
    replay = await service.spawn(_command(), token)
    assert replay.run_id == handle.run_id
    waited = await service.wait(
        AgentWaitCommand("run_root", (handle.run_id,), WaitMode.ALL, 5_000),
        token,
    )
    assert waited.completed_run_ids == (handle.run_id,)
    status = await service.status("run_root", handle.run_id)
    result = await service.result("run_root", handle.run_id)
    assert status.status is SubagentRunStatus.COMPLETED
    assert result.summary == "Verified child conclusion"
    assert len(runner.executions) == 1
    run = await uow.get_entity("runs", handle.run_id)
    assert isinstance(run, Run) and run.kind is RunKind.SUBAGENT
    stored = run_record_from_value(await uow.get_entity("subagent_runs", handle.run_id))
    assert stored.result_artifact_id == artifacts.items[handle.run_id][1].artifact_id
    assert stored.budget_used.model_calls == 1
    events = await uow.event_store.read(handle.run_id)
    assert [item.event_type for item in events] == [
        "subagent.queued",
        "subagent.started",
        "subagent.result_available",
        "subagent.completed",
    ]
    assert [item.event_type for item in sink.events] == [item.event_type for item in events]
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_cancel_unblocks_wait_and_isolates_terminal_audit_artifact() -> None:
    service, uow, _sink, scheduler, runner, artifacts = _build(block=True)
    token = ManualCancellationToken()
    handle = await service.spawn(_command(), token)
    await asyncio.wait_for(runner.started.wait(), 2)
    receipt = await service.cancel(
        AgentCancelCommand("run_root", handle.run_id, "user stopped child", True),
        token,
    )
    assert receipt.accepted
    waited = await service.wait(
        AgentWaitCommand("run_root", (handle.run_id,), WaitMode.ALL, 5_000),
        token,
    )
    assert not waited.timed_out
    status = await service.status("run_root", handle.run_id)
    assert status.status is SubagentRunStatus.CANCELLED
    result, metadata = artifacts.items[handle.run_id]
    assert result.status == "cancelled"
    assert metadata.state is ArtifactState.CANCELLED
    events = await uow.event_store.read(handle.run_id)
    assert events[-2].event_type == "subagent.result_available"
    assert events[-1].event_type == "subagent.cancelled" and events[-1].terminal
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_wait_timeout_does_not_change_child_state() -> None:
    service, _uow, _sink, scheduler, runner, _artifacts = _build(block=True)
    token = ManualCancellationToken()
    handle = await service.spawn(_command(), token)
    await asyncio.wait_for(runner.started.wait(), 2)
    waited = await service.wait(
        AgentWaitCommand("run_root", (handle.run_id,), WaitMode.ANY, 1),
        token,
    )
    assert waited.timed_out and waited.pending_run_ids == (handle.run_id,)
    assert (await service.status("run_root", handle.run_id)).status is SubagentRunStatus.RUNNING
    await service.cancel(AgentCancelCommand("run_root", handle.run_id, "cleanup", True), token)
    await service.wait(AgentWaitCommand("run_root", (handle.run_id,), WaitMode.ALL, 5_000), token)
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_send_is_durable_idempotent_and_delivered_to_active_safe_point() -> None:
    service, _uow, _sink, scheduler, runner, _artifacts = _build(block=True)
    token = ManualCancellationToken()
    handle = await service.spawn(_command(), token)
    await asyncio.wait_for(runner.started.wait(), 2)
    command = AgentSendCommand(
        "run_root",
        handle.run_id,
        MailboxMode.STEER,
        "Re-check the counterexample",
        (),
        "msg_steer",
    )
    receipt = await service.send(command, token)
    replay = await service.send(command, token)
    assert receipt.sequence == 1 and replay.duplicate
    assert len(runner.messages) == 1 and runner.messages[0][1].mode == "steer"
    await service.cancel(AgentCancelCommand("run_root", handle.run_id, "cleanup", True), token)
    await service.wait(AgentWaitCommand("run_root", (handle.run_id,), WaitMode.ALL, 5_000), token)
    await scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("safe", "expected_status", "expected_recovered"),
    [
        (True, SubagentRunStatus.COMPLETED, True),
        (False, SubagentRunStatus.INTERRUPTED, False),
    ],
)
async def test_expired_lease_recovers_only_safe_readonly_checkpoint(
    safe: bool,
    expected_status: SubagentRunStatus,
    expected_recovered: bool,
) -> None:
    uow = InMemoryUnitOfWorkFactory()
    run_id = await _seed_expired_run(uow, safe=safe)
    service, _uow, _sink, scheduler, _runner, artifacts = _build(
        unit_of_work=uow,
        ledger=_restored_root_ledger(),
    )
    cleaner = _Cleaner()
    recovered = await service.recover_orphans(cleaner)
    if expected_recovered:
        await service.wait(
            AgentWaitCommand("run_root", (run_id,), WaitMode.ALL, 5_000),
            ManualCancellationToken(),
        )
    assert (run_id in recovered) is expected_recovered
    assert cleaner.cleaned == [run_id]
    assert (await service.status("run_root", run_id)).status is expected_status
    result, metadata = artifacts.items[run_id]
    if safe:
        assert result.status == "completed" and metadata.state is ArtifactState.COMPLETE
    else:
        assert result.status == "interrupted" and metadata.state is ArtifactState.PARTIAL
    events = await uow.event_store.read(run_id)
    assert events[0].event_type == "subagent.orphaned"
    assert ("subagent.recovered" in [item.event_type for item in events]) is expected_recovered
    assert events[-1].terminal
    reservation = await uow.get_entity("subagent_budget_reservations", run_id)
    assert isinstance(reservation, dict)
    assert reservation["state"] == "settled"
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_sqlite_restart_preserves_child_run_result_and_event_replay(tmp_path: Path) -> None:
    database = tmp_path / "subagents.sqlite"
    durable = SqliteUnitOfWorkFactory(database)
    service, _uow, _sink, scheduler, _runner, _artifacts = _build(unit_of_work=durable)
    handle = await service.spawn(_command(), ManualCancellationToken())
    await service.wait(
        AgentWaitCommand("run_root", (handle.run_id,), WaitMode.ALL, 5_000),
        ManualCancellationToken(),
    )
    await scheduler.shutdown()

    restarted = SqliteUnitOfWorkFactory(database)
    record = run_record_from_value(await restarted.get_entity("subagent_runs", handle.run_id))
    result = await restarted.get_entity("subagent_results", handle.run_id)
    events = await restarted.event_store.read(handle.run_id)
    assert record.status is SubagentRunStatus.COMPLETED
    assert record.result_artifact_id is not None and result is not None
    assert [item.sequence for item in events] == [1, 2, 3, 4]
    assert events[-1].event_type == "subagent.completed" and events[-1].terminal


@pytest.mark.asyncio
async def test_lost_spawn_commit_ack_recovers_receipt_and_executes_exactly_once() -> None:
    durable = InMemoryUnitOfWorkFactory()
    unreliable = _AckLossFactory(durable)
    service, _uow, _sink, scheduler, runner, _artifacts = _build(unit_of_work=unreliable)
    handle = await service.spawn(_command(), ManualCancellationToken())
    await service.wait(
        AgentWaitCommand("run_root", (handle.run_id,), WaitMode.ALL, 5_000),
        ManualCancellationToken(),
    )
    replay = await service.spawn(_command(), ManualCancellationToken())
    assert replay.run_id == handle.run_id
    assert len(runner.executions) == 1
    assert len(await durable.list_entities("subagent_runs")) == 1
    events = await durable.event_store.read(handle.run_id)
    assert [item.sequence for item in events] == [1, 2, 3, 4]
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_repeated_task_fingerprint_is_stopped_after_first_completed_child() -> None:
    service, _uow, _sink, scheduler, _runner, _artifacts = _build()
    first = await service.spawn(_command(), ManualCancellationToken())
    await service.wait(
        AgentWaitCommand("run_root", (first.run_id,), WaitMode.ALL, 5_000),
        ManualCancellationToken(),
    )
    with pytest.raises(SubagentServiceError) as captured:
        await service.spawn(_command(call_id="call_again"), ManualCancellationToken())
    assert captured.value.code == "subagent_duplicate_task"
    await scheduler.shutdown()
