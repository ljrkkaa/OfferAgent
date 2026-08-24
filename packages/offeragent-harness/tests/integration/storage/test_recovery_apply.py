from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWork, SqliteUnitOfWorkFactory
from offeragent_harness.agent import BudgetCheckpoint, BudgetDelta, RunBudget
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import (
    EntityStore,
    EventStore,
    InvocationJournal,
    JournalState,
    NewEvent,
    StoredEvent,
    UnitOfWork,
    UnitOfWorkFactory,
)
from offeragent_harness.protocol.content import VaultSourceRef
from offeragent_harness.protocol.events import (
    EventType,
    RuntimeWarningPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    TurnInterruptedPayload,
    parse_persisted_domain_event,
)
from offeragent_harness.runtime.recovery import RecoveryCoordinator, RecoveryDisposition, RecoveryPlan
from offeragent_harness.runtime.recovery_apply import RecoveryApplyBlocked, RecoveryPlanApplier
from offeragent_harness.sessions import (
    AgentLineage,
    Run,
    RunKind,
    RunStatus,
    TerminationReason,
    Turn,
    TurnStatus,
)
from offeragent_harness.testing import DeterministicIdGenerator, ManualClock
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffect,
    SideEffectClass,
    SideEffectKind,
    SideEffectState,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
    invocation_journal_scope,
    invocation_request_fingerprint,
)
from offeragent_harness.tools.registry import ToolRegistry

NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
RECOVERY_TIME = NOW + timedelta(minutes=5)
WORKSPACE_ID = "ws_recovery_apply"


@dataclass(frozen=True, slots=True)
class _RunBundle:
    run: Run
    state: RunState
    turn: Turn


class _FakeRecoveryLookup:
    def __init__(self, responses: dict[str, ToolResult | None]) -> None:
        self._responses = responses

    async def lookup_result(self, definition: ToolDefinition, call: ToolCall) -> ToolResult | None:
        del definition
        return self._responses.get(call.tool_call_id)


class _AckLossUnitOfWork:
    def __init__(self, inner: SqliteUnitOfWork, factory: _AckLossFactory) -> None:
        self._inner = inner
        self._factory = factory

    @property
    def entities(self) -> EntityStore:
        return self._inner.entities

    @property
    def events(self) -> EventStore:
        return self._inner.events

    @property
    def journal(self) -> InvocationJournal:
        return self._inner.journal

    async def __aenter__(self) -> _AckLossUnitOfWork:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self._inner.__aexit__(exc_type, exc, traceback)

    async def commit(self) -> None:
        await self._inner.commit()
        if self._factory.fail_next_commit_ack:
            self._factory.fail_next_commit_ack = False
            raise ConnectionError("simulated SQLite commit ACK loss")

    async def rollback(self) -> None:
        await self._inner.rollback()


class _AckLossFactory:
    def __init__(self, inner: SqliteUnitOfWorkFactory) -> None:
        self.inner = inner
        self.fail_next_commit_ack = True

    def begin(self) -> UnitOfWork:
        return _AckLossUnitOfWork(self.inner.begin(), self)


class _AppendFailureEventStore:
    def __init__(self, inner: EventStore) -> None:
        self._inner = inner

    async def append(
        self,
        stream_id: str,
        expected_sequence: int,
        events: Sequence[NewEvent],
    ) -> tuple[StoredEvent, ...]:
        del stream_id, expected_sequence, events
        raise OSError("simulated event append failure")

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        return await self._inner.read(stream_id, after_sequence=after_sequence, limit=limit)

    async def latest_sequence(self, stream_id: str) -> int:
        return await self._inner.latest_sequence(stream_id)

    async def terminal_event(self, stream_id: str) -> StoredEvent | None:
        return await self._inner.terminal_event(stream_id)


class _AppendFailureUnitOfWork:
    def __init__(self, inner: SqliteUnitOfWork) -> None:
        self._inner = inner

    @property
    def entities(self) -> EntityStore:
        return self._inner.entities

    @property
    def events(self) -> EventStore:
        return _AppendFailureEventStore(self._inner.events)

    @property
    def journal(self) -> InvocationJournal:
        return self._inner.journal

    async def __aenter__(self) -> _AppendFailureUnitOfWork:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self._inner.__aexit__(exc_type, exc, traceback)

    async def commit(self) -> None:
        await self._inner.commit()

    async def rollback(self) -> None:
        await self._inner.rollback()


class _AppendFailureFactory:
    def __init__(self, inner: SqliteUnitOfWorkFactory) -> None:
        self.inner = inner

    def begin(self) -> UnitOfWork:
        return _AppendFailureUnitOfWork(self.inner.begin())


def _definition(name: str, side_effect_class: SideEffectClass) -> ToolDefinition:
    side_effect_free = side_effect_class in {SideEffectClass.NONE, SideEffectClass.READ}
    return ToolDefinition(
        name=name,
        version="1",
        description=f"{name} recovery test definition",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.READ if side_effect_free else RiskClass.WRITE,
        side_effect_class=side_effect_class,
        required_capabilities=frozenset({f"test.{name}"}),
        concurrency_safe=side_effect_free,
        idempotent=True,
        retryable=side_effect_free,
        timeout_ms=5_000,
        output_limit_bytes=64 * 1024,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def _call(definition: ToolDefinition, run_id: str, call_id: str) -> ToolCall:
    arguments = {"path": f"tests/{call_id}.md"}
    return ToolCall(
        tool_call_id=call_id,
        run_id=run_id,
        workspace_id=WORKSPACE_ID,
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=f"idem-{call_id}",
        deadline=None,
        lineage=AgentLineage.root(run_id),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


def _result(call: ToolCall, definition: ToolDefinition) -> ToolResult:
    if definition.side_effect_class is SideEffectClass.READ:
        effects = (
            SideEffect(
                SideEffectKind.READ,
                SideEffectState.OBSERVED,
                f"vault:{call.arguments['path']}",
                None,
                None,
            ),
        )
    else:
        effects = (
            SideEffect(
                SideEffectKind.FILE_WRITE,
                SideEffectState.COMMITTED,
                f"vault:{call.arguments['path']}",
                {"hash": "before"},
                {"hash": "after"},
            ),
        )
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data={"path": call.arguments["path"]},
        user_visible_summary="工具已完成",
        artifact_ids=(),
        source_refs=(),
        side_effects=effects,
        retryable=False,
        before_state=None,
        after_state=None,
        error=None,
        source_references=(
            {
                "type": "vault",
                "file": {"workspaceId": WORKSPACE_ID, "path": str(call.arguments["path"])},
                "freshness": "unknown",
            },
        ),
    )


def _failed_result(call: ToolCall) -> ToolResult:
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.FAILED,
        data=None,
        user_visible_summary="工具失败",
        artifact_ids=(),
        source_refs=(),
        side_effects=(
            SideEffect(
                SideEffectKind.FILE_WRITE,
                SideEffectState.ROLLED_BACK,
                f"vault:{call.arguments['path']}",
                {"hash": "before"},
                {"hash": "before"},
            ),
        ),
        retryable=False,
        before_state=None,
        after_state=None,
        error=ToolError(
            code="write_failed",
            message="写入失败且已回滚",
            retryable=False,
            cancelled=False,
        ),
    )


def _bundle(suffix: str, calls: tuple[ToolCall, ...]) -> _RunBundle:
    run_id = f"run_{suffix}"
    session_id = f"ses_{suffix}"
    turn_id = f"turn_{suffix}"
    lineage = AgentLineage.root(run_id)
    state = RunState(
        workspace_id=WORKSPACE_ID,
        session_id=session_id,
        turn_id=turn_id,
        run_id=run_id,
        lineage=lineage,
        phase=RunPhase.EXECUTING_TOOLS,
        revision=7 if calls else 8,
        model_rounds=1,
        assistant_text="已保存的部分回答",
        budget_checkpoint=BudgetCheckpoint(
            budget=RunBudget(8, 20, 4, 300, 20_000, 8_000, Decimal("10"), 1_000_000, 4),
            started_at=NOW,
            used=BudgetDelta(model_rounds=1, tool_calls=len(calls)),
            reserved=BudgetDelta(),
            captured_at=NOW,
            elapsed_seconds=0,
        ),
    )
    if calls:
        state = state.accept_tool_calls(calls)
    return _RunBundle(
        run=Run(
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
            workspace_id=WORKSPACE_ID,
            lineage=lineage,
            kind=RunKind.ROOT,
            status=RunStatus.EXECUTING_TOOLS,
            attempt=1,
            event_sequence=0,
            config_snapshot={"model": "fake"},
            created_at=NOW,
            updated_at=NOW,
            deadline_at=NOW + timedelta(seconds=300),
        ),
        state=state,
        turn=Turn(
            turn_id=turn_id,
            session_id=session_id,
            ordinal=1,
            status=TurnStatus.RUNNING,
            input_blocks=({"type": "text", "text": "recover"},),
            created_at=NOW,
            updated_at=NOW,
        ),
    )


async def _persist_bundle(
    unit_of_work: UnitOfWork,
    bundle: _RunBundle,
    *,
    include_turn: bool = True,
) -> None:
    await unit_of_work.entities.put("runs", bundle.run.run_id, bundle.run, expected_revision=0)
    await unit_of_work.entities.put("run_states", bundle.run.run_id, bundle.state, expected_revision=0)
    if include_turn:
        await unit_of_work.entities.put("turns", bundle.turn.turn_id, bundle.turn, expected_revision=0)
    await unit_of_work.entities.put(
        "active_root_runs",
        bundle.run.session_id,
        {
            "schemaVersion": 1,
            "workspaceId": bundle.run.workspace_id,
            "sessionId": bundle.run.session_id,
            "runId": bundle.run.run_id,
            "acquiredAt": NOW.isoformat(),
        },
        expected_revision=0,
    )


def _scope(call: ToolCall, definition: ToolDefinition) -> str:
    return invocation_journal_scope(call, definition)


async def _one_plan(
    factory: UnitOfWorkFactory,
    definitions: tuple[ToolDefinition, ...],
    *,
    lookup: _FakeRecoveryLookup | None = None,
) -> RecoveryPlan:
    plans = await RecoveryCoordinator(
        unit_of_work=factory,
        registry=ToolRegistry("restart", definitions),
        lookup=lookup,
        clock=ManualClock(NOW),
    ).scan()
    assert len(plans) == 1
    return plans[0]


def _applier(factory: UnitOfWorkFactory) -> RecoveryPlanApplier:
    return RecoveryPlanApplier(
        unit_of_work=factory,
        clock=ManualClock(RECOVERY_TIME),
        ids=DeterministicIdGenerator(),
    )


@pytest.mark.asyncio
async def test_completed_write_result_is_applied_and_original_pending_order_is_replayed_idempotently(
    tmp_path: Path,
) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "batch.sqlite")
    write = _definition("vault.write", SideEffectClass.WRITE)
    run_id = "run_batch_apply"
    first = _call(write, run_id, "call_first_write")
    second = _call(write, run_id, "call_second_write")
    bundle = _bundle("batch_apply", (first, second))
    first_result = _result(first, write)
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, bundle)
        await unit_of_work.journal.start(
            _scope(first, write), first.idempotency_key, invocation_request_fingerprint(first), NOW
        )
        await unit_of_work.journal.complete(
            _scope(first, write),
            first.idempotency_key,
            invocation_request_fingerprint(first),
            first_result,
            NOW,
        )
        await unit_of_work.commit()

    plan = await _one_plan(factory, (write,))
    applied = await _applier(factory).apply(plan)

    assert applied.disposition is RecoveryDisposition.RESUME
    assert applied.state.tool_results == (first_result,)
    assert applied.state.pending.tool_calls == (second,)
    assert applied.accepted_tool_call_ids == (first.tool_call_id, second.tool_call_id)
    assert applied.replay_calls == (second,)
    assert applied.replay_calls[0].idempotency_key == second.idempotency_key
    assert applied.run.event_sequence == 2
    assert applied.run.updated_at == RECOVERY_TIME
    assert applied.event.event_type == EventType.RUNTIME_WARNING.value
    assert not applied.event.terminal
    recovered_event = parse_persisted_domain_event(applied.events[0].payload)
    warning_event = parse_persisted_domain_event(applied.events[1].payload)
    assert isinstance(recovered_event.payload, ToolCompletedPayload)
    assert recovered_event.payload.result.tool_call_id == first.tool_call_id
    assert recovered_event.state_revision == bundle.state.revision + 1
    assert isinstance(warning_event.payload, RuntimeWarningPayload)
    assert warning_event.payload.code == "recovery.plan_applied"
    assert await factory.get_entity("active_root_runs", bundle.run.session_id) is not None

    repeated = await _applier(factory).apply(plan)
    assert repeated.reconciled
    assert repeated.event == applied.event
    assert repeated.events == applied.events
    assert repeated.replay_calls == (second,)
    assert len(await factory.event_store.read(run_id)) == 2
    assert await factory.get_entity_revision("runs", run_id) == 2
    assert await factory.get_entity_revision("run_states", run_id) == 2
    assert await factory.get_entity_revision("turns", bundle.turn.turn_id) == 1


@pytest.mark.asyncio
async def test_multiple_recovered_result_events_follow_original_pending_batch_order(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "ordered-results.sqlite")
    write = _definition("vault.write.ordered", SideEffectClass.WRITE)
    run_id = "run_ordered_results"
    first = _call(write, run_id, "call_ordered_first")
    replay = _call(write, run_id, "call_ordered_replay")
    last = _call(write, run_id, "call_ordered_last")
    bundle = _bundle("ordered_results", (first, replay, last))
    first_result = _result(first, write)
    last_result = _result(last, write)
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, bundle)
        for call, result in ((first, first_result), (last, last_result)):
            await unit_of_work.journal.start(
                _scope(call, write), call.idempotency_key, invocation_request_fingerprint(call), NOW
            )
            await unit_of_work.journal.complete(
                _scope(call, write),
                call.idempotency_key,
                invocation_request_fingerprint(call),
                result,
                NOW,
            )
        await unit_of_work.commit()

    plan = await _one_plan(factory, (write,))
    applied = await _applier(factory).apply(plan)
    recovered_ids = []
    for event in applied.events[:-1]:
        parsed = parse_persisted_domain_event(event.payload)
        assert isinstance(parsed.payload, ToolCompletedPayload)
        recovered_ids.append(parsed.payload.result.tool_call_id)

    assert applied.accepted_tool_call_ids == (
        first.tool_call_id,
        replay.tool_call_id,
        last.tool_call_id,
    )
    assert applied.replay_calls == (replay,)
    assert recovered_ids == [first.tool_call_id, last.tool_call_id]
    assert applied.state.tool_results == (first_result, last_result)
    assert applied.state.pending.tool_calls == (replay,)
    assert applied.run.event_sequence == 3


@pytest.mark.asyncio
async def test_lookup_result_completes_journal_and_run_state_in_one_recovery_uow(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "lookup.sqlite")
    write = _definition("vault.write.lookup", SideEffectClass.WRITE)
    run_id = "run_lookup_apply"
    call = _call(write, run_id, "call_lookup_write")
    result = _result(call, write)
    bundle = _bundle("lookup_apply", (call,))
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, bundle)
        await unit_of_work.journal.start(
            _scope(call, write), call.idempotency_key, invocation_request_fingerprint(call), NOW
        )
        await unit_of_work.commit()

    plan = await _one_plan(
        factory,
        (write,),
        lookup=_FakeRecoveryLookup({call.tool_call_id: result}),
    )
    applied = await _applier(factory).apply(plan)

    journal = await factory.get_journal(_scope(call, write), call.idempotency_key)
    state = await factory.get_entity("run_states", run_id)
    assert journal is not None and journal.state is JournalState.COMPLETED
    assert journal.result == result
    assert isinstance(state, RunState)
    assert state == applied.state
    assert state.tool_results == (result,)
    assert state.pending.tool_calls == ()
    assert applied.accepted_tool_call_ids == (call.tool_call_id,)
    assert applied.replay_calls == ()
    assert [event.event_type for event in applied.events] == [
        EventType.TOOL_COMPLETED.value,
        EventType.RUNTIME_WARNING.value,
    ]
    completed = parse_persisted_domain_event(applied.events[0].payload)
    assert isinstance(completed.payload, ToolCompletedPayload)
    reference = completed.payload.result.source_refs[0]
    assert isinstance(reference, VaultSourceRef)
    assert reference.file.path == call.arguments["path"]


@pytest.mark.asyncio
async def test_recovered_failed_result_persists_strict_tool_failed_fact_before_warning(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "failed-result.sqlite")
    write = _definition("vault.write.failed", SideEffectClass.WRITE)
    run_id = "run_failed_result"
    call = _call(write, run_id, "call_failed_result")
    result = _failed_result(call)
    bundle = _bundle("failed_result", (call,))
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, bundle)
        await unit_of_work.journal.start(
            _scope(call, write), call.idempotency_key, invocation_request_fingerprint(call), NOW
        )
        await unit_of_work.journal.complete(
            _scope(call, write),
            call.idempotency_key,
            invocation_request_fingerprint(call),
            result,
            NOW,
        )
        await unit_of_work.commit()

    applied = await _applier(factory).apply(await _one_plan(factory, (write,)))
    parsed = parse_persisted_domain_event(applied.events[0].payload)

    assert applied.state.tool_results == (result,)
    assert [event.event_type for event in applied.events] == [
        EventType.TOOL_FAILED.value,
        EventType.RUNTIME_WARNING.value,
    ]
    assert isinstance(parsed.payload, ToolFailedPayload)
    assert parsed.payload.result.tool_call_id == call.tool_call_id
    assert parsed.payload.result.error is not None
    assert parsed.payload.result.error.code is ErrorCode.TOOL_FAILED
    assert parsed.state_revision == bundle.state.revision + 1


@pytest.mark.asyncio
async def test_lookup_journal_completion_rolls_back_if_result_event_cannot_persist(tmp_path: Path) -> None:
    inner = SqliteUnitOfWorkFactory(tmp_path / "lookup-rollback.sqlite")
    write = _definition("vault.write.rollback", SideEffectClass.WRITE)
    run_id = "run_lookup_rollback"
    call = _call(write, run_id, "call_lookup_rollback")
    result = _result(call, write)
    bundle = _bundle("lookup_rollback", (call,))
    async with inner.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, bundle)
        await unit_of_work.journal.start(
            _scope(call, write), call.idempotency_key, invocation_request_fingerprint(call), NOW
        )
        await unit_of_work.commit()
    plan = await _one_plan(
        inner,
        (write,),
        lookup=_FakeRecoveryLookup({call.tool_call_id: result}),
    )

    with pytest.raises(RecoveryApplyBlocked) as captured:
        await _applier(_AppendFailureFactory(inner)).apply(plan)

    assert captured.value.code == "commit_outcome_unconfirmed"
    journal = await inner.get_journal(_scope(call, write), call.idempotency_key)
    assert journal is not None and journal.state is JournalState.STARTED
    assert journal.result is None
    assert await inner.get_entity("run_states", run_id) == bundle.state
    assert await inner.get_entity_revision("run_states", run_id) == 1
    assert await inner.event_store.read(run_id) == ()
    assert await inner.get_entity("active_root_runs", bundle.run.session_id) is not None


@pytest.mark.asyncio
async def test_unknown_write_is_terminal_manual_review_with_error_details_and_lease_release(
    tmp_path: Path,
) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "manual.sqlite")
    write = _definition("vault.write.unknown", SideEffectClass.WRITE)
    run_id = "run_manual_review"
    call = _call(write, run_id, "call_unknown_write")
    bundle = _bundle("manual_review", (call,))
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, bundle)
        await unit_of_work.journal.start(
            _scope(call, write), call.idempotency_key, invocation_request_fingerprint(call), NOW
        )
        await unit_of_work.journal.mark_unknown(
            _scope(call, write), call.idempotency_key, invocation_request_fingerprint(call), NOW
        )
        await unit_of_work.commit()

    plan = await _one_plan(factory, (write,))
    assert plan.disposition is RecoveryDisposition.MANUAL_REVIEW
    applied = await _applier(factory).apply(plan)

    assert applied.run.status is RunStatus.INTERRUPTED
    assert applied.run.termination_reason is TerminationReason.RUNTIME_INTERRUPTED
    assert applied.state.phase is RunPhase.INTERRUPTED
    assert applied.turn.status is TurnStatus.INTERRUPTED
    assert applied.lease_released
    assert await factory.get_entity("active_root_runs", bundle.run.session_id) is None
    assert applied.event.terminal
    assert applied.event.event_type == EventType.TURN_INTERRUPTED.value
    parsed = parse_persisted_domain_event(applied.event.payload)
    assert isinstance(parsed.payload, TurnInterruptedPayload)
    assert parsed.payload.error.details["manualReviewRequired"] is True
    assert parsed.payload.error.details["recoveryDisposition"] == "manual_review"
    assert parsed.payload.error.details["issueCodes"] == ["journal_unknown_effectful"]
    assert parsed.payload.partial_content[0].text == bundle.state.assistant_text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_revision_race_rolls_back_without_event_or_lease_release(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "race.sqlite")
    read = _definition("workspace.read.race", SideEffectClass.READ)
    run_id = "run_revision_race"
    call = _call(read, run_id, "call_revision_race")
    bundle = _bundle("revision_race", (call,))
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, bundle)
        await unit_of_work.commit()
    plan = await _one_plan(factory, (read,))
    async with factory.begin() as unit_of_work:
        await unit_of_work.entities.put(
            "run_states",
            run_id,
            replace(bundle.state, assistant_text="concurrent writer won"),
            expected_revision=1,
        )
        await unit_of_work.commit()

    with pytest.raises(RecoveryApplyBlocked, match="RunState") as captured:
        await _applier(factory).apply(plan)

    assert captured.value.code == "run_state_snapshot_changed"
    assert await factory.event_store.read(run_id) == ()
    assert await factory.get_entity("runs", run_id) == bundle.run
    assert await factory.get_entity_revision("run_states", run_id) == 2
    assert await factory.get_entity("active_root_runs", bundle.run.session_id) is not None


@pytest.mark.asyncio
async def test_missing_turn_blocks_without_fabricating_terminal_or_releasing_lease(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "missing-turn.sqlite")
    read = _definition("workspace.read.missing", SideEffectClass.READ)
    run_id = "run_missing_turn"
    call = _call(read, run_id, "call_missing_turn")
    bundle = _bundle("missing_turn", (call,))
    async with factory.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, bundle, include_turn=False)
        await unit_of_work.commit()
    plan = await _one_plan(factory, (read,))
    assert plan.turn is None
    assert plan.disposition is RecoveryDisposition.INTERRUPT

    with pytest.raises(RecoveryApplyBlocked) as captured:
        await _applier(factory).apply(plan)

    assert captured.value.code == "incomplete_recovery_snapshot"
    assert await factory.get_entity("turns", bundle.turn.turn_id) is None
    assert await factory.event_store.read(run_id) == ()
    assert await factory.get_entity("active_root_runs", bundle.run.session_id) is not None


@pytest.mark.asyncio
async def test_commit_ack_loss_reconciles_deterministic_event_and_repeat_is_idempotent(tmp_path: Path) -> None:
    inner = SqliteUnitOfWorkFactory(tmp_path / "ack-loss.sqlite")
    read = _definition("workspace.read.ack", SideEffectClass.READ)
    run_id = "run_ack_loss"
    call = _call(read, run_id, "call_ack_loss")
    bundle = _bundle("ack_loss", (call,))
    async with inner.begin() as unit_of_work:
        await _persist_bundle(unit_of_work, bundle)
        await unit_of_work.commit()
    plan = await _one_plan(inner, (read,))
    ack_loss = _AckLossFactory(inner)
    applier = _applier(ack_loss)

    applied = await applier.apply(plan)
    repeated = await applier.apply(plan)

    assert applied.reconciled
    assert repeated.reconciled
    assert repeated.event == applied.event
    events = await inner.event_store.read(run_id)
    assert events == (applied.event,)
    assert await inner.get_entity_revision("runs", run_id) == 2
    assert await inner.get_entity_revision("run_states", run_id) == 2
    assert await inner.get_entity("active_root_runs", bundle.run.session_id) is not None
