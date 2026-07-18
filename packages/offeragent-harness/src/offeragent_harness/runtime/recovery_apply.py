"""Atomic application of a previously validated :mod:`runtime.recovery` plan."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.models import thaw_json
from offeragent_harness.ports import (
    Clock,
    EntityRecord,
    EntityStore,
    IdGenerator,
    InvocationJournal,
    InvocationRecord,
    JournalState,
    NewEvent,
    StoredEvent,
    UnitOfWork,
    UnitOfWorkFactory,
)
from offeragent_harness.protocol._base import validate_wire
from offeragent_harness.protocol.common import ToolCallStatus, ToolResultDescriptor, UsageSnapshot
from offeragent_harness.protocol.content import ContentBlock, ContentFormat, TextContentBlock
from offeragent_harness.protocol.errors import ErrorEnvelope
from offeragent_harness.protocol.events import (
    EventType,
    PersistedSideEffectFact,
    RuntimeWarningPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    TurnInterruptedPayload,
    make_domain_event_record,
    parse_persisted_domain_event,
)
from offeragent_harness.sessions import Run, RunStatus, TerminationReason, Turn, TurnStatus
from offeragent_harness.storage.serialization import tool_result_to_value
from offeragent_harness.tools import (
    ToolCall,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
    invocation_request_fingerprint,
)

from .recovery import (
    RecoveryAction,
    RecoveryActionKind,
    RecoveryDisposition,
    RecoveryError,
    RecoveryPlan,
)


class RecoveryApplyBlocked(RecoveryError):
    def __init__(self, code: str, run_id: str, message: str) -> None:
        self.code = code
        self.run_id = run_id
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RecoveryApplyResult:
    plan_fingerprint: str
    disposition: RecoveryDisposition
    run: Run
    run_entity_revision: int
    state: RunState
    state_entity_revision: int
    turn: Turn
    turn_entity_revision: int
    accepted_tool_call_ids: tuple[str, ...]
    replay_calls: tuple[ToolCall, ...]
    events: tuple[StoredEvent, ...]
    lease_released: bool
    reconciled: bool

    def __post_init__(self) -> None:
        if not self.plan_fingerprint.startswith("sha256:"):
            raise ValueError("plan_fingerprint must be a canonical digest")
        if min(self.run_entity_revision, self.state_entity_revision, self.turn_entity_revision) < 1:
            raise ValueError("RecoveryApplyResult entity revisions must be positive")
        if not self.events:
            raise ValueError("RecoveryApplyResult requires at least one persisted event")
        if any(event.stream_id != self.run.run_id for event in self.events):
            raise ValueError("RecoveryApplyResult events must belong to its Run")
        if tuple(event.sequence for event in self.events) != tuple(
            range(self.events[0].sequence, self.run.event_sequence + 1)
        ):
            raise ValueError("RecoveryApplyResult events must be contiguous through the Run cursor")
        if self.events[-1].sequence != self.run.event_sequence:
            raise ValueError("RecoveryApplyResult event cursor must match Run")
        if self.state.run_id != self.run.run_id or self.turn.turn_id != self.run.turn_id:
            raise ValueError("RecoveryApplyResult entities must share one Run identity")
        if self.disposition is RecoveryDisposition.RESUME:
            if self.events[-1].terminal or self.events[-1].event_type != EventType.RUNTIME_WARNING.value:
                raise ValueError("resume result requires one non-terminal runtime.warning")
            if any(
                event.terminal or event.event_type not in {EventType.TOOL_COMPLETED.value, EventType.TOOL_FAILED.value}
                for event in self.events[:-1]
            ):
                raise ValueError("resume result may only precede its warning with non-terminal tool result events")
            if self.lease_released:
                raise ValueError("resume cannot release the active Run lease")
            if not self.accepted_tool_call_ids or len(self.accepted_tool_call_ids) != len(
                set(self.accepted_tool_call_ids)
            ):
                raise ValueError("resume must expose one ordered, unique accepted ToolCall batch")
            replay_ids = tuple(call.tool_call_id for call in self.replay_calls)
            accepted_replay_ids = tuple(
                call_id for call_id in self.accepted_tool_call_ids if call_id in set(replay_ids)
            )
            if replay_ids != accepted_replay_ids:
                raise ValueError("replay calls must retain their relative accepted-batch order")
            if self.replay_calls != self.state.pending.tool_calls:
                raise ValueError("replay calls must remain exact pending ToolCalls in persisted order")
            if len(self.events) - 1 != len(self.accepted_tool_call_ids) - len(self.replay_calls):
                raise ValueError("each recovered result requires exactly one persisted tool result event")
        else:
            if len(self.events) != 1:
                raise ValueError("interrupted recovery requires exactly one terminal event")
            if not self.events[0].terminal or self.events[0].event_type != EventType.TURN_INTERRUPTED.value:
                raise ValueError("blocked recovery requires one terminal turn.interrupted event")
            if self.accepted_tool_call_ids or self.replay_calls or not self.lease_released:
                raise ValueError("interrupted recovery cannot return replay calls and must release its lease")
            if (
                self.run.status is not RunStatus.INTERRUPTED
                or self.state.phase is not RunPhase.INTERRUPTED
                or self.turn.status is not TurnStatus.INTERRUPTED
            ):
                raise ValueError("interrupted result must project all authoritative entities to interrupted")

    @property
    def event(self) -> StoredEvent:
        """Final audit/terminal event and authoritative Run cursor."""

        return self.events[-1]


@dataclass(frozen=True, slots=True)
class _RecoveredResult:
    action: RecoveryAction
    state_revision: int


@dataclass(frozen=True, slots=True)
class _Projection:
    state: RunState
    run: Run
    turn: Turn
    accepted_tool_call_ids: tuple[str, ...]
    replay_calls: tuple[ToolCall, ...]
    recovered_results: tuple[_RecoveredResult, ...]
    run_revision: int
    state_revision: int
    turn_revision: int
    lease_released: bool


class RecoveryPlanApplier:
    """CAS-apply recovery facts; never invokes Planner, ModelGateway, or a Tool executor."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._ids = ids

    async def apply(self, plan: RecoveryPlan) -> RecoveryApplyResult:
        if plan.state is None or plan.turn is None:
            raise RecoveryApplyBlocked(
                "incomplete_recovery_snapshot",
                plan.run_id,
                "RecoveryPlan 缺少 RunState 或 Turn, 禁止猜测创建 terminal 状态。",
            )
        fingerprint = _plan_fingerprint(plan)
        existing = await self._reconcile(plan, fingerprint)
        if existing is not None:
            return existing

        occurred_at = self._clock.utcnow()
        trace_id = self._ids.new_id("trace")
        projection = _project(plan, occurred_at)
        events = _new_events(plan, projection, fingerprint, trace_id, occurred_at)
        try:
            return await self._apply_once(plan, projection, fingerprint, events)
        except RecoveryApplyBlocked:
            recovered = await self._reconcile(plan, fingerprint)
            if recovered is not None:
                return recovered
            raise
        except BaseException as error:
            recovered = await self._reconcile(plan, fingerprint)
            if recovered is None:
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise RecoveryApplyBlocked(
                    "commit_outcome_unconfirmed",
                    plan.run_id,
                    f"Recovery UoW 失败且无法对账: {type(error).__name__}",
                ) from error
            if isinstance(error, asyncio.CancelledError):
                raise
            return recovered

    async def _apply_once(
        self,
        plan: RecoveryPlan,
        projection: _Projection,
        fingerprint: str,
        events: tuple[NewEvent, ...],
    ) -> RecoveryApplyResult:
        state_entity_revision = plan.state_entity_revision
        turn = plan.turn
        turn_entity_revision = plan.turn_entity_revision
        assert state_entity_revision is not None
        assert turn is not None and turn_entity_revision is not None
        async with self._unit_of_work.begin() as unit_of_work:
            await _validate_entities_and_cursor(unit_of_work, plan)
            lease_record = await _require_owned_lease(unit_of_work.entities, plan)
            if plan.disposition is RecoveryDisposition.RESUME:
                await _apply_resume_journal(unit_of_work.journal, plan, events[0].occurred_at)

            state_revision = await unit_of_work.entities.put(
                "run_states",
                plan.run_id,
                projection.state,
                expected_revision=state_entity_revision,
            )
            run_revision = await unit_of_work.entities.put(
                "runs",
                plan.run_id,
                projection.run,
                expected_revision=plan.run_entity_revision,
            )
            turn_revision = turn_entity_revision
            if plan.disposition is not RecoveryDisposition.RESUME:
                turn_revision = await unit_of_work.entities.put(
                    "turns",
                    turn.turn_id,
                    projection.turn,
                    expected_revision=turn_entity_revision,
                )
                await unit_of_work.entities.delete(
                    "active_root_runs",
                    plan.run.session_id,
                    expected_revision=lease_record.revision,
                )
            stored = await unit_of_work.events.append(
                plan.run_id,
                plan.run.event_sequence,
                events,
            )
            await unit_of_work.commit()

        return RecoveryApplyResult(
            plan_fingerprint=fingerprint,
            disposition=plan.disposition,
            run=projection.run,
            run_entity_revision=run_revision,
            state=projection.state,
            state_entity_revision=state_revision,
            turn=projection.turn,
            turn_entity_revision=turn_revision,
            accepted_tool_call_ids=projection.accepted_tool_call_ids,
            replay_calls=projection.replay_calls,
            events=stored,
            lease_released=projection.lease_released,
            reconciled=False,
        )

    async def _reconcile(self, plan: RecoveryPlan, fingerprint: str) -> RecoveryApplyResult | None:
        expected_count = _expected_event_count(plan)
        async with self._unit_of_work.begin() as unit_of_work:
            events = await unit_of_work.events.read(
                plan.run_id,
                after_sequence=plan.run.event_sequence,
                limit=expected_count + 1,
            )
            if not events:
                return None
            if len(events) != expected_count:
                raise RecoveryApplyBlocked(
                    "recovery_event_sequence_advanced",
                    plan.run_id,
                    "RecoveryPlan 之后的事件数量与确定性 recovery batch 不一致。",
                )
            try:
                first_parsed = parse_persisted_domain_event(events[0].payload)
            except (TypeError, ValueError) as error:
                raise RecoveryApplyBlocked(
                    "recovery_event_corrupt",
                    plan.run_id,
                    "已持久化 recovery event 不是严格 typed event。",
                ) from error
            projection = _project(plan, events[0].occurred_at)
            expected_events = _new_events(
                plan,
                projection,
                fingerprint,
                first_parsed.trace_id,
                events[0].occurred_at,
            )
            for offset, (stored, expected) in enumerate(zip(events, expected_events, strict=True), start=1):
                if not _stored_event_matches(
                    stored,
                    expected,
                    expected_sequence=plan.run.event_sequence + offset,
                ):
                    raise RecoveryApplyBlocked(
                        "recovery_event_conflict",
                        plan.run_id,
                        "RecoveryPlan 的确定性 event batch 与已持久化事实不一致。",
                    )
                try:
                    parse_persisted_domain_event(stored.payload)
                except (TypeError, ValueError) as error:
                    raise RecoveryApplyBlocked(
                        "recovery_event_corrupt",
                        plan.run_id,
                        "已持久化 recovery event 不是严格 typed event。",
                    ) from error
            if events[-1].sequence != projection.run.event_sequence:
                raise RecoveryApplyBlocked(
                    "recovery_event_cursor_mismatch",
                    plan.run_id,
                    "RecoveryPlan 的确定性 event batch 未落在投影后的 Run cursor。",
                )
            await _validate_reconciled_entities(unit_of_work, plan, projection)
            await _validate_reconciled_journal(unit_of_work.journal, plan)
            lease_record = await _find_entity_record(
                unit_of_work.entities,
                "active_root_runs",
                plan.run.session_id,
            )
            if plan.disposition is RecoveryDisposition.RESUME:
                _validate_lease_value(plan, lease_record)
            elif lease_record is not None:
                raise RecoveryApplyBlocked(
                    "recovery_lease_not_released",
                    plan.run_id,
                    "terminal recovery event 已存在, 但 active_root_runs lease 尚未释放。",
                )

        return RecoveryApplyResult(
            plan_fingerprint=fingerprint,
            disposition=plan.disposition,
            run=projection.run,
            run_entity_revision=projection.run_revision,
            state=projection.state,
            state_entity_revision=projection.state_revision,
            turn=projection.turn,
            turn_entity_revision=projection.turn_revision,
            accepted_tool_call_ids=projection.accepted_tool_call_ids,
            replay_calls=projection.replay_calls,
            events=events,
            lease_released=projection.lease_released,
            reconciled=True,
        )


async def _validate_entities_and_cursor(unit_of_work: UnitOfWork, plan: RecoveryPlan) -> None:
    run_record = await _find_entity_record(unit_of_work.entities, "runs", plan.run_id)
    state_record = await _find_entity_record(unit_of_work.entities, "run_states", plan.run_id)
    turn_record = await _find_entity_record(unit_of_work.entities, "turns", plan.run.turn_id)
    _require_exact_record(
        plan,
        "run_snapshot_changed",
        "Run",
        run_record,
        plan.run_entity_revision,
        plan.run,
    )
    assert plan.state_entity_revision is not None and plan.state is not None
    _require_exact_record(
        plan,
        "run_state_snapshot_changed",
        "RunState",
        state_record,
        plan.state_entity_revision,
        plan.state,
    )
    assert plan.turn_entity_revision is not None and plan.turn is not None
    _require_exact_record(
        plan,
        "turn_snapshot_changed",
        "Turn",
        turn_record,
        plan.turn_entity_revision,
        plan.turn,
    )
    latest_sequence = await unit_of_work.events.latest_sequence(plan.run_id)
    terminal = await unit_of_work.events.terminal_event(plan.run_id)
    if latest_sequence != plan.run.event_sequence or terminal is not None:
        raise RecoveryApplyBlocked(
            "event_cursor_changed",
            plan.run_id,
            "Event Store cursor 或 terminal 状态已变化, RecoveryPlan 已过期。",
        )


async def _validate_reconciled_entities(
    unit_of_work: UnitOfWork,
    plan: RecoveryPlan,
    projection: _Projection,
) -> None:
    run_record = await _find_entity_record(unit_of_work.entities, "runs", plan.run_id)
    state_record = await _find_entity_record(unit_of_work.entities, "run_states", plan.run_id)
    turn_record = await _find_entity_record(unit_of_work.entities, "turns", plan.run.turn_id)
    _require_exact_record(
        plan,
        "reconciled_run_mismatch",
        "Run",
        run_record,
        projection.run_revision,
        projection.run,
    )
    _require_exact_record(
        plan,
        "reconciled_state_mismatch",
        "RunState",
        state_record,
        projection.state_revision,
        projection.state,
    )
    _require_exact_record(
        plan,
        "reconciled_turn_mismatch",
        "Turn",
        turn_record,
        projection.turn_revision,
        projection.turn,
    )


def _require_exact_record(
    plan: RecoveryPlan,
    code: str,
    label: str,
    record: EntityRecord | None,
    expected_revision: int,
    expected_value: object,
) -> None:
    if record is None or record.revision != expected_revision or record.value != expected_value:
        raise RecoveryApplyBlocked(code, plan.run_id, f"{label} 与 RecoveryPlan 携带的 CAS 快照不一致。")


async def _require_owned_lease(entities: EntityStore, plan: RecoveryPlan) -> EntityRecord:
    record = await _find_entity_record(entities, "active_root_runs", plan.run.session_id)
    _validate_lease_value(plan, record)
    assert record is not None
    return record


def _validate_lease_value(plan: RecoveryPlan, record: EntityRecord | None) -> None:
    if record is None or not isinstance(record.value, Mapping):
        raise RecoveryApplyBlocked(
            "active_run_lease_missing",
            plan.run_id,
            "active_root_runs lease 缺失或损坏, 禁止恢复或释放其他 Run 的 lease。",
        )
    lease = record.value
    if (
        lease.get("schemaVersion") != 1
        or lease.get("workspaceId") != plan.run.workspace_id
        or lease.get("sessionId") != plan.run.session_id
        or lease.get("runId") != plan.run_id
    ):
        raise RecoveryApplyBlocked(
            "active_run_lease_owner_mismatch",
            plan.run_id,
            "active_root_runs lease 不属于 RecoveryPlan 的 Run。",
        )


async def _apply_resume_journal(journal: InvocationJournal, plan: RecoveryPlan, completed_at: datetime) -> None:
    for action in plan.actions:
        current = await journal.get(action.journal_scope, action.call.idempotency_key)
        _validate_journal_binding(plan, action, current)
        if action.kind is RecoveryActionKind.APPLY_LOOKUP_RESULT:
            assert action.result is not None
            if current is not None and current.state is JournalState.COMPLETED:
                _require_same_journal_result(plan, action, current)
                continue
            if (
                current is None
                or current.state is not action.journal_state
                or current.state
                not in {
                    JournalState.STARTED,
                    JournalState.UNKNOWN,
                }
            ):
                raise RecoveryApplyBlocked(
                    "lookup_journal_state_changed",
                    plan.run_id,
                    "Lookup recovery 要求原始 STARTED 或 UNKNOWN journal, 但当前状态已变化。",
                )
            completed = await journal.complete(
                action.journal_scope,
                action.call.idempotency_key,
                invocation_request_fingerprint(action.call),
                action.result,
                completed_at,
            )
            _require_same_journal_result(plan, action, completed)
        elif action.kind is RecoveryActionKind.APPLY_JOURNAL_RESULT:
            if current is None or current.state is not JournalState.COMPLETED:
                raise RecoveryApplyBlocked(
                    "completed_journal_state_changed",
                    plan.run_id,
                    "Journal-result recovery 要求 COMPLETED journal。",
                )
            _require_same_journal_result(plan, action, current)
        elif action.journal_state is None:
            if current is not None:
                raise RecoveryApplyBlocked(
                    "unstarted_call_journal_appeared",
                    plan.run_id,
                    "原本未开始的 ToolCall 已出现 journal, 禁止并发重复执行。",
                )
        elif current is None or current.state is not JournalState.STARTED:
            raise RecoveryApplyBlocked(
                "replay_journal_state_changed",
                plan.run_id,
                "安全读 replay 的 STARTED journal 已变化。",
            )


async def _validate_reconciled_journal(journal: InvocationJournal, plan: RecoveryPlan) -> None:
    if plan.disposition is not RecoveryDisposition.RESUME:
        return
    for action in plan.actions:
        current = await journal.get(action.journal_scope, action.call.idempotency_key)
        _validate_journal_binding(plan, action, current)
        if action.kind in {
            RecoveryActionKind.APPLY_JOURNAL_RESULT,
            RecoveryActionKind.APPLY_LOOKUP_RESULT,
        }:
            if current is None or current.state is not JournalState.COMPLETED:
                raise RecoveryApplyBlocked(
                    "reconciled_journal_incomplete",
                    plan.run_id,
                    "已应用 recovery event, 但对应 journal 未完成。",
                )
            _require_same_journal_result(plan, action, current)
        elif action.journal_state is None:
            if current is not None:
                raise RecoveryApplyBlocked(
                    "reconciled_unstarted_call_changed",
                    plan.run_id,
                    "已应用 recovery event 后, 未开始 ToolCall 的 journal 不应存在。",
                )
        elif current is None or current.state is not JournalState.STARTED:
            raise RecoveryApplyBlocked(
                "reconciled_replay_journal_changed",
                plan.run_id,
                "已应用 recovery event 后, safe-read journal 不再是 STARTED。",
            )


def _validate_journal_binding(
    plan: RecoveryPlan,
    action: RecoveryAction,
    record: InvocationRecord | None,
) -> None:
    if record is None:
        return
    if (
        record.scope != action.journal_scope
        or record.idempotency_key != action.call.idempotency_key
        or record.request_hash != invocation_request_fingerprint(action.call)
    ):
        raise RecoveryApplyBlocked(
            "journal_binding_changed",
            plan.run_id,
            "Invocation Journal 不再绑定 RecoveryPlan 的原始 ToolCall。",
        )


def _require_same_journal_result(
    plan: RecoveryPlan,
    action: RecoveryAction,
    record: InvocationRecord,
) -> None:
    expected = action.result
    actual = None if record.result is None else replace(record.result, tool_call_id=action.call.tool_call_id)
    if expected is None or actual != expected:
        raise RecoveryApplyBlocked(
            "journal_result_changed",
            plan.run_id,
            "Invocation Journal 的 ToolResult 与 RecoveryPlan 不一致。",
        )


def _project(plan: RecoveryPlan, occurred_at: datetime) -> _Projection:
    assert plan.state is not None and plan.state_entity_revision is not None
    assert plan.turn is not None and plan.turn_entity_revision is not None
    if plan.disposition is RecoveryDisposition.RESUME:
        state, replay_calls, recovered_results = _project_resume_state(plan)
        event_count = len(recovered_results) + 1
        run = replace(
            plan.run,
            status=RunStatus(state.phase.value),
            event_sequence=plan.run.event_sequence + event_count,
            updated_at=occurred_at,
            termination_reason=None,
        )
        return _Projection(
            state=state,
            run=run,
            turn=plan.turn,
            accepted_tool_call_ids=tuple(call.tool_call_id for call in plan.state.pending.tool_calls),
            replay_calls=replay_calls,
            recovered_results=recovered_results,
            run_revision=plan.run_entity_revision + 1,
            state_revision=plan.state_entity_revision + 1,
            turn_revision=plan.turn_entity_revision,
            lease_released=False,
        )

    state = replace(plan.state, phase=RunPhase.INTERRUPTED, revision=plan.state.revision + 1)
    run = replace(
        plan.run,
        status=RunStatus.INTERRUPTED,
        event_sequence=plan.run.event_sequence + 1,
        updated_at=occurred_at,
        termination_reason=TerminationReason.RUNTIME_INTERRUPTED,
    )
    turn = replace(
        plan.turn,
        status=TurnStatus.INTERRUPTED,
        updated_at=occurred_at,
        revision=plan.turn.revision + 1,
    )
    return _Projection(
        state=state,
        run=run,
        turn=turn,
        accepted_tool_call_ids=(),
        replay_calls=(),
        recovered_results=(),
        run_revision=plan.run_entity_revision + 1,
        state_revision=plan.state_entity_revision + 1,
        turn_revision=plan.turn_entity_revision + 1,
        lease_released=True,
    )


def _project_resume_state(
    plan: RecoveryPlan,
) -> tuple[RunState, tuple[ToolCall, ...], tuple[_RecoveredResult, ...]]:
    assert plan.state is not None
    actions = {action.call.tool_call_id: action for action in plan.actions}
    pending_calls = plan.state.pending.tool_calls
    if set(actions) != {call.tool_call_id for call in pending_calls}:
        raise RecoveryApplyBlocked(
            "recovery_actions_incomplete",
            plan.run_id,
            "RecoveryPlan 没有为每个 pending ToolCall 提供唯一 action。",
        )
    state = plan.state
    replay_calls: list[ToolCall] = []
    recovered_results: list[_RecoveredResult] = []
    for call in pending_calls:
        action = actions[call.tool_call_id]
        if action.call != call:
            raise RecoveryApplyBlocked(
                "recovery_call_snapshot_changed",
                plan.run_id,
                "RecoveryAction 未保留原始完整 ToolCall。",
            )
        if action.kind is RecoveryActionKind.REPLAY_ORIGINAL_CALL:
            replay_calls.append(call)
            continue
        assert action.result is not None
        state = state.record_tool_result(action.definition, action.result)
        recovered_results.append(_RecoveredResult(action, state.revision))
    return state, tuple(replay_calls), tuple(recovered_results)


def _new_events(
    plan: RecoveryPlan,
    projection: _Projection,
    fingerprint: str,
    trace_id: str,
    occurred_at: datetime,
) -> tuple[NewEvent, ...]:
    if plan.disposition is not RecoveryDisposition.RESUME:
        return (
            _new_domain_event(
                plan,
                fingerprint,
                trace_id,
                occurred_at,
                ordinal=1,
                event_type=EventType.TURN_INTERRUPTED,
                payload=_interrupted_payload(plan, trace_id),
                state_revision=projection.state.revision,
                terminal=True,
            ),
        )

    events: list[NewEvent] = []
    for ordinal, recovered in enumerate(projection.recovered_results, start=1):
        result = recovered.action.result
        assert result is not None
        event_type, payload = _tool_result_event(result)
        events.append(
            _new_domain_event(
                plan,
                fingerprint,
                trace_id,
                occurred_at,
                ordinal=ordinal,
                event_type=event_type,
                payload=payload,
                state_revision=recovered.state_revision,
                terminal=False,
            )
        )
    warning_ordinal = len(events) + 1
    events.append(
        _new_domain_event(
            plan,
            fingerprint,
            trace_id,
            occurred_at,
            ordinal=warning_ordinal,
            event_type=EventType.RUNTIME_WARNING,
            payload=RuntimeWarningPayload(
                code="recovery.plan_applied",
                message=(
                    f"Worker restart persisted {len(projection.recovered_results)} recovered tool results "
                    f"and retained {len(projection.replay_calls)} original ToolCalls for safe replay."
                ),
                recommended_action="Continue the persisted recovery actions before requesting a new model plan.",
                disabled_capabilities=[],
            ),
            state_revision=projection.state.revision,
            terminal=False,
        )
    )
    return tuple(events)


def _interrupted_payload(plan: RecoveryPlan, trace_id: str) -> TurnInterruptedPayload:
    state = plan.state
    assert state is not None
    manual = plan.disposition is RecoveryDisposition.MANUAL_REVIEW
    return TurnInterruptedPayload(
        error=ErrorEnvelope(
            code=ErrorCode.RUNTIME_INTERRUPTED,
            retryable=False,
            cancelled=False,
            user_visible_message=(
                "Run recovery requires manual review."
                if manual
                else "Run recovery was interrupted because its persisted checkpoint was unsafe."
            ),
            details={
                "failureCategory": "runtime",
                "recoveryDisposition": plan.disposition.value,
                "manualReviewRequired": manual,
                "issueCodes": [issue.code for issue in plan.issues],
            },
            retry_after_ms=None,
            trace_id=trace_id,
        ),
        usage=_usage(state),
        partial_content=_partial_content(state.assistant_text),
        safe_checkpoint_available=False,
    )


def _new_domain_event(
    plan: RecoveryPlan,
    fingerprint: str,
    trace_id: str,
    occurred_at: datetime,
    *,
    ordinal: int,
    event_type: EventType,
    payload: ToolCompletedPayload | ToolFailedPayload | RuntimeWarningPayload | TurnInterruptedPayload,
    state_revision: int,
    terminal: bool,
) -> NewEvent:
    record = make_domain_event_record(
        event_type=event_type,
        payload=payload,
        trace_id=trace_id,
        workspace_id=plan.run.workspace_id,
        session_id=plan.run.session_id,
        turn_id=plan.run.turn_id,
        run_id=plan.run_id,
        root_run_id=plan.run.lineage.root_run_id,
        parent_run_id=plan.run.lineage.parent_run_id,
        state_revision=state_revision,
    )
    return NewEvent(
        event_id=_event_id(fingerprint, ordinal),
        event_type=event_type.value,
        payload=record.to_wire(),
        occurred_at=occurred_at,
        terminal=terminal,
        idempotency_key=_event_idempotency_key(plan, fingerprint, ordinal),
    )


def _tool_result_event(result: ToolResult) -> tuple[EventType, ToolCompletedPayload | ToolFailedPayload]:
    failed = result.status in {
        ToolResultStatus.FAILED,
        ToolResultStatus.TIMED_OUT,
        ToolResultStatus.UNKNOWN_OUTCOME,
    }
    payload_type = ToolFailedPayload if failed else ToolCompletedPayload
    return (
        EventType.TOOL_FAILED if failed else EventType.TOOL_COMPLETED,
        payload_type(
            result=_tool_result_descriptor(result),
            artifact_ids=list(result.artifact_ids),
            source_reference_ids=list(result.source_refs),
            side_effect_facts=[
                PersistedSideEffectFact(
                    kind=effect.kind.value,
                    state=effect.state.value,
                    resource_id=effect.resource_id,
                    before_state=thaw_json(effect.before_state),
                    after_state=thaw_json(effect.after_state),
                    metadata=thaw_json(effect.metadata),
                )
                for effect in result.side_effects
            ],
        ),
    )


def _tool_result_descriptor(result: ToolResult) -> ToolResultDescriptor:
    raw_data = thaw_json(result.data)
    data = dict(raw_data) if isinstance(raw_data, Mapping) else {"value": raw_data}
    status = (
        ToolCallStatus.CONFLICT if result.status is ToolResultStatus.CONFLICTED else ToolCallStatus(result.status.value)
    )
    error = _tool_error(result)
    return validate_wire(
        ToolResultDescriptor,
        {
            "toolCallId": result.tool_call_id,
            "status": status.value,
            "summary": result.user_visible_summary,
            "data": data,
            "artifactRefs": [],
            "sourceRefs": [thaw_json(reference) for reference in result.source_references],
            "sideEffects": [],
            "retryable": result.retryable,
            "error": None if error is None else error.to_wire(),
        },
    )


def _tool_error(result: ToolResult) -> ErrorEnvelope | None:
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
    return ErrorEnvelope(
        code=code,
        retryable=result.error.retryable,
        cancelled=result.error.cancelled,
        user_visible_message=result.error.message,
        details={
            "toolErrorCode": result.error.code,
            "toolErrorDetails": thaw_json(result.error.details),
        },
        retry_after_ms=None,
        trace_id=None,
    )


def _usage(state: RunState) -> UsageSnapshot:
    return UsageSnapshot(
        input_tokens=0,
        output_tokens=0,
        cached_input_tokens=0,
        reasoning_tokens=0,
        model_calls=state.model_rounds,
        tool_calls=state.tool_calls,
        cost_micros=None,
        wall_time_ms=0,
    )


def _partial_content(text: str) -> list[ContentBlock]:
    if not text:
        return []
    return [TextContentBlock(type="text", text=text, format=ContentFormat.MARKDOWN, references=[])]


def _plan_fingerprint(plan: RecoveryPlan) -> str:
    return canonical_json_sha256(
        {
            "runId": plan.run_id,
            "runEntityRevision": plan.run_entity_revision,
            "stateEntityRevision": plan.state_entity_revision,
            "turnEntityRevision": plan.turn_entity_revision,
            "eventSequence": plan.run.event_sequence,
            "disposition": plan.disposition.value,
            "acceptedToolCallIds": (
                None if plan.state is None else [call.tool_call_id for call in plan.state.pending.tool_calls]
            ),
            "actions": [
                {
                    "kind": action.kind.value,
                    "toolCallId": action.call.tool_call_id,
                    "idempotencyKey": action.call.idempotency_key,
                    "deadline": None if action.call.deadline is None else action.call.deadline.isoformat(),
                    "journalScope": action.journal_scope,
                    "journalState": None if action.journal_state is None else action.journal_state.value,
                    "requestFingerprint": invocation_request_fingerprint(action.call),
                    "result": None if action.result is None else tool_result_to_value(action.result),
                }
                for action in plan.actions
            ],
            "issues": [
                {
                    "code": issue.code,
                    "disposition": issue.disposition.value,
                    "toolCallId": issue.tool_call_id,
                }
                for issue in plan.issues
            ],
        }
    )


def _expected_event_count(plan: RecoveryPlan) -> int:
    if plan.disposition is not RecoveryDisposition.RESUME:
        return 1
    return 1 + sum(action.kind is not RecoveryActionKind.REPLAY_ORIGINAL_CALL for action in plan.actions)


def _event_id(fingerprint: str, ordinal: int) -> str:
    return f"evt_recovery_{fingerprint.removeprefix('sha256:')[:32]}_{ordinal:04d}"


def _event_idempotency_key(plan: RecoveryPlan, fingerprint: str, ordinal: int) -> str:
    return f"{plan.run_id}:recovery:{fingerprint}:{ordinal:04d}"


def _stored_event_matches(stored: StoredEvent, expected: NewEvent, *, expected_sequence: int) -> bool:
    return (
        stored.sequence == expected_sequence
        and stored.event_id == expected.event_id
        and stored.event_type == expected.event_type
        and stored.payload == expected.payload
        and stored.occurred_at == expected.occurred_at
        and stored.terminal == expected.terminal
        and stored.idempotency_key == expected.idempotency_key
    )


async def _find_entity_record(store: EntityStore, collection: str, entity_id: str) -> EntityRecord | None:
    after_id: str | None = None
    while True:
        page = await store.list(collection, after_id=after_id, limit=100)
        if not page:
            return None
        for record in page:
            if record.entity_id == entity_id:
                return record
            if record.entity_id > entity_id:
                return None
        after_id = page[-1].entity_id


__all__ = [
    "RecoveryApplyBlocked",
    "RecoveryApplyResult",
    "RecoveryPlanApplier",
]
