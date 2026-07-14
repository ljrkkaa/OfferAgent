"""Crash/orphan Run recovery planning without executing tools or models.

The coordinator snapshots authoritative entities and invocation-journal records
inside one Unit of Work, releases the database transaction, and only then asks an
optional lookup adapter about already-started invocations.  Its output is a strict
plan: applying results, replaying an original call with its original idempotency
key, or atomically interrupting the Run are deliberately left to Harness wiring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

from offeragent_harness.agent.budgets import BudgetDelta
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.permissions import ApprovalState
from offeragent_harness.ports import (
    Clock,
    EntityRecord,
    EntityStore,
    InvocationJournal,
    InvocationRecord,
    JournalState,
    UnitOfWorkFactory,
)
from offeragent_harness.sessions import Run, RunKind, Turn, TurnStatus
from offeragent_harness.tools import (
    ResultSensitivity,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
    invocation_journal_scope,
    invocation_request_fingerprint,
    is_safe_crash_replay,
    is_side_effect_free,
)
from offeragent_harness.tools.registry import ToolNotFound, ToolRegistry, ToolVersionUnavailable

from .approval_manager import ApprovalRecord

_ISSUE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_ACTIVE_TURN_STATUSES = frozenset({TurnStatus.CREATED, TurnStatus.RUNNING})


class RecoveryError(RuntimeError):
    pass


class RecoveryScanError(RecoveryError):
    """The repository cannot produce a trustworthy recovery snapshot."""


class RecoveryDisposition(str, Enum):
    RESUME = "resume"
    MANUAL_REVIEW = "manual_review"
    INTERRUPT = "interrupt"


class RecoveryActionKind(str, Enum):
    APPLY_JOURNAL_RESULT = "apply_journal_result"
    APPLY_LOOKUP_RESULT = "apply_lookup_result"
    REPLAY_ORIGINAL_CALL = "replay_original_call"


@dataclass(frozen=True, slots=True)
class RecoveryIssue:
    code: str
    message: str
    disposition: RecoveryDisposition
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if _ISSUE_CODE.fullmatch(self.code) is None:
            raise ValueError("recovery issue code must be canonical snake_case")
        if not self.message:
            raise ValueError("recovery issue message must not be empty")
        if self.disposition is RecoveryDisposition.RESUME:
            raise ValueError("a recovery issue must block automatic resume")
        if self.tool_call_id is not None and not self.tool_call_id:
            raise ValueError("tool_call_id cannot be empty")


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    kind: RecoveryActionKind
    call: ToolCall
    definition: ToolDefinition
    journal_scope: str
    journal_state: JournalState | None
    result: ToolResult | None = None

    def __post_init__(self) -> None:
        if not self.journal_scope:
            raise ValueError("recovery action journal_scope must not be empty")
        if (self.call.name, self.call.version) != (self.definition.name, self.definition.version):
            raise ValueError("recovery action definition does not match its ToolCall")
        if self.call.definition_fingerprint != self.definition.fingerprint:
            raise ValueError("recovery action cannot carry a drifted ToolDefinition")
        if (
            self.call.result_sensitivity is ResultSensitivity.UNKNOWN
            or self.call.result_sensitivity is not self.definition.result_sensitivity
        ):
            raise ValueError("recovery action cannot carry an unbound result sensitivity")
        if self.kind is RecoveryActionKind.REPLAY_ORIGINAL_CALL:
            if self.result is not None or self.journal_state not in {None, JournalState.STARTED}:
                raise ValueError("replay action only accepts no journal or a STARTED journal without a result")
            return
        if self.result is None:
            raise ValueError("result application actions require a ToolResult")
        if self.result.tool_call_id != self.call.tool_call_id:
            raise ValueError("recovered result identity must match the pending ToolCall")
        if self.result.status is ToolResultStatus.UNKNOWN_OUTCOME:
            raise ValueError("unknown outcomes cannot become automatic recovery actions")
        if self.kind is RecoveryActionKind.APPLY_JOURNAL_RESULT and self.journal_state is not JournalState.COMPLETED:
            raise ValueError("journal result action requires a COMPLETED journal")
        if self.kind is RecoveryActionKind.APPLY_LOOKUP_RESULT and self.journal_state is not JournalState.STARTED:
            raise ValueError("lookup result action requires a STARTED journal")


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    run: Run
    run_entity_revision: int
    state: RunState | None
    state_entity_revision: int | None
    turn: Turn | None
    turn_entity_revision: int | None
    disposition: RecoveryDisposition
    actions: tuple[RecoveryAction, ...]
    issues: tuple[RecoveryIssue, ...]

    def __post_init__(self) -> None:
        if self.run.status.is_terminal:
            raise ValueError("terminal Runs cannot have a recovery plan")
        if self.run_entity_revision < 1:
            raise ValueError("run_entity_revision must be positive")
        if (self.state is None) != (self.state_entity_revision is None):
            raise ValueError("state snapshot and entity revision must be present together")
        if (self.turn is None) != (self.turn_entity_revision is None):
            raise ValueError("turn snapshot and entity revision must be present together")
        if self.state_entity_revision is not None and self.state_entity_revision < 1:
            raise ValueError("state_entity_revision must be positive")
        if self.turn_entity_revision is not None and self.turn_entity_revision < 1:
            raise ValueError("turn_entity_revision must be positive")
        if self.state is not None and (
            self.state.run_id != self.run.run_id
            or self.state.session_id != self.run.session_id
            or self.state.turn_id != self.run.turn_id
            or self.state.workspace_id != self.run.workspace_id
        ):
            raise ValueError("RecoveryPlan RunState identity does not match Run")
        if self.turn is not None and (
            self.turn.turn_id != self.run.turn_id or self.turn.session_id != self.run.session_id
        ):
            raise ValueError("RecoveryPlan Turn identity does not match Run")

        action_ids = tuple(action.call.tool_call_id for action in self.actions)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("RecoveryPlan cannot contain multiple actions for one ToolCall")
        if any(action.call.run_id != self.run.run_id for action in self.actions):
            raise ValueError("RecoveryPlan actions cannot cross Run boundaries")
        expected_disposition = _disposition(self.issues)
        if self.disposition is not expected_disposition:
            raise ValueError("RecoveryPlan disposition does not match its issues")

        if self.disposition is RecoveryDisposition.RESUME:
            if self.state is None or self.turn is None:
                raise ValueError("automatic resume requires complete RunState and Turn snapshots")
            if self.state.phase.terminal or self.turn.status not in _ACTIVE_TURN_STATUSES:
                raise ValueError("automatic resume requires non-terminal state and Turn")
            pending_ids = self.state.pending.tool_call_ids
            if not pending_ids or frozenset(action_ids) != pending_ids:
                raise ValueError("automatic resume must recover every persisted pending ToolCall exactly once")

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @property
    def requires_interruption(self) -> bool:
        return self.disposition is not RecoveryDisposition.RESUME

    @property
    def manual_review_required(self) -> bool:
        return self.disposition is RecoveryDisposition.MANUAL_REVIEW

    @property
    def original_calls_to_replay(self) -> tuple[ToolCall, ...]:
        return tuple(action.call for action in self.actions if action.kind is RecoveryActionKind.REPLAY_ORIGINAL_CALL)

    @property
    def recovered_results(self) -> tuple[ToolResult, ...]:
        return tuple(action.result for action in self.actions if action.result is not None)


@runtime_checkable
class RecoveryLookup(Protocol):
    """Read-only reconciliation boundary; it never executes a ToolCall."""

    async def lookup_result(self, definition: ToolDefinition, call: ToolCall) -> ToolResult | None: ...


@runtime_checkable
class RecoveryDefinitionResolver(Protocol):
    """Resolve the exact persisted definition when locations share a name/version."""

    def resolve_definition(self, call: ToolCall) -> ToolDefinition: ...


@dataclass(frozen=True, slots=True)
class _CallSnapshot:
    call: ToolCall
    definition: ToolDefinition
    journal_scope: str
    journal: InvocationRecord | None


@dataclass(frozen=True, slots=True)
class _RunSnapshot:
    run: Run
    run_revision: int
    state: RunState | None
    state_revision: int | None
    turn: Turn | None
    turn_revision: int | None
    calls: tuple[_CallSnapshot, ...]
    issues: tuple[RecoveryIssue, ...]


class RecoveryCoordinator:
    """Build deterministic startup plans for non-terminal authoritative Runs."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        registry: ToolRegistry,
        lookup: RecoveryLookup | None = None,
        definition_resolver: RecoveryDefinitionResolver | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._registry = registry
        self._lookup = lookup
        self._definition_resolver = definition_resolver
        self._clock = clock

    async def scan(self, *, page_size: int = 100) -> tuple[RecoveryPlan, ...]:
        if not 1 <= page_size <= 1_000:
            raise ValueError("page_size must be between 1 and 1000")
        snapshots = await self._snapshot(page_size)
        plans = [await self._resolve(snapshot) for snapshot in snapshots]
        return tuple(plans)

    async def _snapshot(self, page_size: int) -> tuple[_RunSnapshot, ...]:
        async with self._unit_of_work.begin() as unit_of_work:
            run_records = await _scan_collection(unit_of_work.entities, "runs", page_size)
            state_records = {
                record.entity_id: record
                for record in await _scan_collection(unit_of_work.entities, "run_states", page_size)
            }
            turn_records = {
                record.entity_id: record for record in await _scan_collection(unit_of_work.entities, "turns", page_size)
            }
            approval_records = {
                record.entity_id: record
                for record in await _scan_collection(unit_of_work.entities, "approvals", page_size)
            }
            run_ids = {record.entity_id for record in run_records}
            for entity_id, record in state_records.items():
                if entity_id in run_ids:
                    continue
                state = record.value
                if not isinstance(state, RunState):
                    raise RecoveryScanError(f"run_states/{entity_id} has an invalid entity type")
                if not state.phase.terminal:
                    raise RecoveryScanError(f"non-terminal run_states/{entity_id} has no authoritative Run")

            snapshots: list[_RunSnapshot] = []
            for run_record in run_records:
                run = run_record.value
                if not isinstance(run, Run):
                    raise RecoveryScanError(f"runs/{run_record.entity_id} has an invalid entity type")
                if run.kind is RunKind.SUBAGENT:
                    # Child Runs have durable leases, result Artifacts and root-budget
                    # reservations that are reconciled by the one in-Worker
                    # SubagentService.  Treating them as root Agent Runs here would
                    # apply a second recovery protocol to the same Run.
                    continue
                if run.status.is_terminal:
                    continue
                snapshots.append(
                    await self._snapshot_run(
                        run_record,
                        state_records.get(run_record.entity_id),
                        turn_records.get(run.turn_id),
                        approval_records,
                        unit_of_work.journal,
                    )
                )
            return tuple(snapshots)

    async def _snapshot_run(
        self,
        run_record: EntityRecord,
        state_record: EntityRecord | None,
        turn_record: EntityRecord | None,
        approval_records: dict[str, EntityRecord],
        journal: InvocationJournal,
    ) -> _RunSnapshot:
        run = run_record.value
        assert isinstance(run, Run)
        issues: list[RecoveryIssue] = []
        if run_record.revision < 1:
            raise RecoveryScanError(f"runs/{run_record.entity_id} has an invalid revision")
        if run_record.entity_id != run.run_id:
            issues.append(_interrupt("run_identity_mismatch", "Run 实体键与持久化 run_id 不一致。"))

        state: RunState | None = None
        state_revision: int | None = None
        if state_record is None:
            issues.append(_interrupt("run_state_missing", "活动 Run 缺少权威 RunState, 禁止猜测恢复点。"))
        elif not isinstance(state_record.value, RunState):
            issues.append(_interrupt("run_state_corrupt", "活动 Run 的 RunState 类型损坏。"))
        else:
            state = state_record.value
            state_revision = state_record.revision
            issues.extend(
                _validate_state(
                    run,
                    state_record.entity_id,
                    state,
                    None if self._clock is None else self._clock.utcnow(),
                )
            )

        turn: Turn | None = None
        turn_revision: int | None = None
        if turn_record is None:
            issues.append(_interrupt("turn_missing", "活动 Run 缺少对应 Turn, 禁止自动恢复。"))
        elif not isinstance(turn_record.value, Turn):
            issues.append(_interrupt("turn_corrupt", "活动 Run 对应的 Turn 类型损坏。"))
        else:
            turn = turn_record.value
            turn_revision = turn_record.revision
            issues.extend(_validate_turn(run, turn_record.entity_id, turn))

        call_snapshots: list[_CallSnapshot] = []
        if state is not None:
            pending = state.pending
            persisted_ids = frozenset(call.tool_call_id for call in pending.tool_calls)
            for missing_call_id in sorted(pending.tool_call_ids - persisted_ids):
                issues.append(
                    _interrupt(
                        "pending_tool_call_payload_missing",
                        "PendingWork 只有 ToolCall ID, 没有完整、可校验的 ToolCall 快照。",
                        missing_call_id,
                    )
                )
            if not pending.tool_call_ids:
                issues.append(
                    _interrupt("no_exact_recovery_checkpoint", "Run 没有待处理 ToolCall, 恢复将需要重新规划, 已禁止。")
                )
            for approval_id in sorted(pending.approval_ids):
                issue = _validate_pending_approval(
                    run,
                    state,
                    approval_id,
                    approval_records,
                    None if self._clock is None else self._clock.utcnow(),
                )
                if issue is not None:
                    issues.append(issue)
            for invocation_id in sorted(pending.client_invocation_ids):
                issues.append(
                    _manual(
                        "pending_client_invocation",
                        f"Client invocation {invocation_id} 尚未协调, 禁止假定其结果。",
                    )
                )
            for child_run_id in sorted(pending.child_run_ids):
                issues.append(_manual("pending_child_run", f"Child Run {child_run_id} 尚未协调, 必须先恢复 Run tree。"))

            recorded_result_ids = {result.tool_call_id for result in state.tool_results}
            journal_keys: set[tuple[str, str]] = set()
            for call in pending.tool_calls:
                call_issues = _validate_call(run, state, call, recorded_result_ids)
                if call_issues:
                    issues.extend(call_issues)
                    continue
                try:
                    definition = (
                        self._registry.get(call.name, call.version)
                        if self._definition_resolver is None
                        else self._definition_resolver.resolve_definition(call)
                    )
                except (ToolNotFound, ToolVersionUnavailable):
                    issues.append(
                        _interrupt(
                            "tool_definition_unavailable",
                            "待恢复 ToolCall 的定义已不在当前 Registry, 禁止执行旧快照。",
                            call.tool_call_id,
                        )
                    )
                    continue
                if call.definition_fingerprint != definition.fingerprint:
                    issues.append(
                        _interrupt(
                            "definition_fingerprint_mismatch",
                            "待恢复 ToolCall 的定义指纹与当前 Registry 不一致。",
                            call.tool_call_id,
                        )
                    )
                    continue
                if (
                    definition.result_sensitivity is ResultSensitivity.UNKNOWN
                    or call.result_sensitivity is not definition.result_sensitivity
                ):
                    issues.append(
                        _interrupt(
                            "tool_result_sensitivity_definition_mismatch",
                            "待恢复 ToolCall 的结果敏感度与当前工具定义不一致。",
                            call.tool_call_id,
                        )
                    )
                    continue
                scope = invocation_journal_scope(call, definition)
                journal_key = (scope, call.idempotency_key)
                if journal_key in journal_keys:
                    issues.append(
                        _interrupt(
                            "duplicate_pending_idempotency_key",
                            "同一 Run 的多个 Pending ToolCall 共享 Invocation Journal 键。",
                            call.tool_call_id,
                        )
                    )
                    continue
                journal_keys.add(journal_key)
                try:
                    record = await journal.get(scope, call.idempotency_key)
                except Exception as error:
                    issues.append(
                        _interrupt(
                            "invocation_journal_unavailable",
                            f"读取 Invocation Journal 失败: {type(error).__name__}。",
                            call.tool_call_id,
                        )
                    )
                    continue
                if record is not None and not isinstance(record, InvocationRecord):
                    issues.append(
                        _interrupt(
                            "invocation_journal_corrupt",
                            "Invocation Journal 返回了无效记录类型。",
                            call.tool_call_id,
                        )
                    )
                    continue
                call_snapshots.append(_CallSnapshot(call, definition, scope, record))

        return _RunSnapshot(
            run=run,
            run_revision=run_record.revision,
            state=state,
            state_revision=state_revision,
            turn=turn,
            turn_revision=turn_revision,
            calls=tuple(call_snapshots),
            issues=tuple(issues),
        )

    async def _resolve(self, snapshot: _RunSnapshot) -> RecoveryPlan:
        actions: list[RecoveryAction] = []
        issues = list(snapshot.issues)
        for call_snapshot in snapshot.calls:
            call = call_snapshot.call
            definition = call_snapshot.definition
            record = call_snapshot.journal
            if record is None:
                actions.append(
                    RecoveryAction(
                        RecoveryActionKind.REPLAY_ORIGINAL_CALL,
                        call,
                        definition,
                        call_snapshot.journal_scope,
                        None,
                    )
                )
                continue
            if (
                record.scope != call_snapshot.journal_scope
                or record.idempotency_key != call.idempotency_key
                or record.request_hash != invocation_request_fingerprint(call)
            ):
                issues.append(
                    _interrupt(
                        "invocation_journal_binding_mismatch",
                        "Invocation Journal 与原 ToolCall 的 scope、幂等键或请求指纹不一致。",
                        call.tool_call_id,
                    )
                )
                continue
            journal_shape_issue = _validate_journal_shape(record, call)
            if journal_shape_issue is not None:
                issues.append(journal_shape_issue)
                continue
            if record.state is JournalState.COMPLETED:
                result = record.result
                if result is None or result.status is ToolResultStatus.UNKNOWN_OUTCOME:
                    issues.append(
                        _interrupt(
                            "completed_journal_result_corrupt",
                            "COMPLETED Invocation Journal 缺少确定的 ToolResult。",
                            call.tool_call_id,
                        )
                    )
                    continue
                actions.append(
                    RecoveryAction(
                        RecoveryActionKind.APPLY_JOURNAL_RESULT,
                        call,
                        definition,
                        call_snapshot.journal_scope,
                        JournalState.COMPLETED,
                        replace(result, tool_call_id=call.tool_call_id),
                    )
                )
                continue
            if record.state is JournalState.UNKNOWN:
                issues.append(_unknown_issue(definition, call, "Invocation Journal 已明确记录 unknown outcome。"))
                continue
            action, issue = await self._resolve_started(call_snapshot)
            if action is not None:
                actions.append(action)
            if issue is not None:
                issues.append(issue)

        disposition = _disposition(tuple(issues))
        return RecoveryPlan(
            run=snapshot.run,
            run_entity_revision=snapshot.run_revision,
            state=snapshot.state,
            state_entity_revision=snapshot.state_revision,
            turn=snapshot.turn,
            turn_entity_revision=snapshot.turn_revision,
            disposition=disposition,
            actions=tuple(actions),
            issues=tuple(issues),
        )

    async def _resolve_started(
        self,
        snapshot: _CallSnapshot,
    ) -> tuple[RecoveryAction | None, RecoveryIssue | None]:
        call = snapshot.call
        definition = snapshot.definition
        lookup_error: Exception | None = None
        recovered: ToolResult | None = None
        if self._lookup is not None:
            try:
                recovered = await self._lookup.lookup_result(definition, call)
            except Exception as error:
                lookup_error = error
        if recovered is not None:
            if recovered.tool_call_id != call.tool_call_id:
                return None, _interrupt(
                    "lookup_result_identity_mismatch",
                    "RecoveryLookup 返回了其他 ToolCall 的结果。",
                    call.tool_call_id,
                )
            if recovered.status is ToolResultStatus.UNKNOWN_OUTCOME:
                return None, _unknown_issue(definition, call, "RecoveryLookup 仍无法确认既有调用结果。")
            return (
                RecoveryAction(
                    RecoveryActionKind.APPLY_LOOKUP_RESULT,
                    call,
                    definition,
                    snapshot.journal_scope,
                    JournalState.STARTED,
                    recovered,
                ),
                None,
            )
        if is_safe_crash_replay(definition):
            return (
                RecoveryAction(
                    RecoveryActionKind.REPLAY_ORIGINAL_CALL,
                    call,
                    definition,
                    snapshot.journal_scope,
                    JournalState.STARTED,
                ),
                None,
            )
        if is_side_effect_free(definition):
            detail = (
                "RecoveryLookup 未返回结果。"
                if lookup_error is None
                else f"RecoveryLookup 失败: {type(lookup_error).__name__}。"
            )
            return None, _interrupt(
                "started_read_not_safely_replayable",
                f"此前读取处于 STARTED, 且工具未声明安全重放; {detail}",
                call.tool_call_id,
            )
        detail = "无法确认既有副作用。" if lookup_error is None else f"结果查询失败: {type(lookup_error).__name__}。"
        return None, _manual(
            "started_effectful_invocation_unconfirmed",
            f"有副作用的调用已进入 STARTED; {detail} 禁止自动重放。",
            call.tool_call_id,
        )


async def _scan_collection(entities: EntityStore, collection: str, page_size: int) -> tuple[EntityRecord, ...]:
    records: list[EntityRecord] = []
    after_id: str | None = None
    seen: set[str] = set()
    while True:
        page = await entities.list(collection, after_id, page_size)
        if len(page) > page_size:
            raise RecoveryScanError(f"{collection} repository exceeded the requested page size")
        ids = tuple(record.entity_id for record in page)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise RecoveryScanError(f"{collection} repository pagination is not stable and unique")
        if after_id is not None and ids and ids[0] <= after_id:
            raise RecoveryScanError(f"{collection} repository did not advance its pagination cursor")
        if any(entity_id in seen for entity_id in ids):
            raise RecoveryScanError(f"{collection} repository repeated an entity across pages")
        records.extend(page)
        seen.update(ids)
        if len(page) < page_size:
            break
        after_id = page[-1].entity_id
    return tuple(records)


def _validate_state(
    run: Run,
    entity_id: str,
    state: RunState,
    now: datetime | None,
) -> tuple[RecoveryIssue, ...]:
    issues: list[RecoveryIssue] = []
    if entity_id != state.run_id:
        issues.append(_interrupt("run_state_entity_mismatch", "RunState 实体键与 state.run_id 不一致。"))
    if (
        state.run_id != run.run_id
        or state.session_id != run.session_id
        or state.turn_id != run.turn_id
        or state.workspace_id != run.workspace_id
        or state.lineage != run.lineage
    ):
        issues.append(_interrupt("run_state_identity_mismatch", "RunState 与权威 Run 的身份或 lineage 不一致。"))
    if state.phase.terminal:
        issues.append(_interrupt("run_state_terminal_mismatch", "非终态 Run 对应了终态 RunState。"))
    result_ids = tuple(result.tool_call_id for result in state.tool_results)
    if len(result_ids) != len(set(result_ids)):
        issues.append(
            _interrupt(
                "duplicate_tool_result",
                "活动 RunState 包含重复 ToolResult; 禁止猜测哪个结果有效。",
            )
        )
    expected_binding_ids = set(result_ids) | set(state.pending.tool_call_ids)
    binding_ids = set(state.tool_result_sensitivities)
    for tool_call_id in sorted(expected_binding_ids - binding_ids):
        issues.append(
            _interrupt(
                "tool_result_sensitivity_binding_missing",
                "活动 RunState 缺少 ToolCall 的结果敏感度绑定。",
                tool_call_id,
            )
        )
    for tool_call_id in sorted(expected_binding_ids & binding_ids):
        if state.tool_result_sensitivities[tool_call_id] is ResultSensitivity.UNKNOWN:
            issues.append(
                _interrupt(
                    "tool_result_sensitivity_unknown",
                    "活动 RunState 包含 UNKNOWN 结果敏感度; 禁止恢复。",
                    tool_call_id,
                )
            )
    for tool_call_id in sorted(binding_ids - expected_binding_ids):
        issues.append(
            _interrupt(
                "tool_result_sensitivity_binding_orphan",
                "活动 RunState 包含没有对应 Pending ToolCall 或 ToolResult 的敏感度绑定。",
                tool_call_id,
            )
        )
    checkpoint = state.budget_checkpoint
    if checkpoint is None:
        issues.append(
            _interrupt(
                "budget_checkpoint_missing",
                "活动 Run 缺少完整 BudgetCheckpoint, 禁止在重启后重置或猜测预算。",
            )
        )
    else:
        if checkpoint.used.model_rounds != state.model_rounds or checkpoint.used.tool_calls != state.tool_calls:
            issues.append(
                _interrupt(
                    "budget_checkpoint_counter_mismatch",
                    "BudgetCheckpoint used 计数与 RunState model/tool 计数不一致。",
                )
            )
        if state.pending.tool_call_ids and checkpoint.reserved != BudgetDelta(model_rounds=1):
            issues.append(
                _interrupt(
                    "composer_reservation_missing",
                    "待恢复 ToolCall 的 reservation 必须只包含一个 canonical Composer model round。",
                )
            )
        if (
            run.created_at.tzinfo is None
            or run.created_at.utcoffset() is None
            or checkpoint.started_at.tzinfo is None
            or checkpoint.started_at.utcoffset() is None
            or run.created_at != checkpoint.started_at
        ):
            issues.append(
                _interrupt(
                    "run_budget_start_mismatch",
                    "Run.created_at 与 BudgetCheckpoint.started_at 必须是相同的 aware 时间。",
                )
            )
        if now is None:
            issues.append(_interrupt("recovery_clock_unavailable", "恢复阶段无法验证 BudgetCheckpoint/deadline。"))
        elif checkpoint.captured_at > now:
            issues.append(
                _interrupt(
                    "budget_checkpoint_from_future",
                    "BudgetCheckpoint.captured_at 晚于恢复时钟, 禁止在时钟回退或状态损坏时恢复。",
                )
            )
        expected_deadline = checkpoint.started_at + timedelta(seconds=checkpoint.budget.max_wall_seconds)
        if run.deadline_at is None:
            issues.append(_interrupt("run_deadline_missing", "活动 Run 缺少绝对 deadline_at。"))
        elif run.deadline_at != expected_deadline:
            issues.append(
                _interrupt(
                    "run_deadline_budget_mismatch",
                    "Run.deadline_at 与 BudgetCheckpoint.started_at + max_wall_seconds 不一致。",
                )
            )
        elif now is not None and now >= run.deadline_at:
            issues.append(
                _interrupt(
                    "run_deadline_expired",
                    "Run 的绝对 deadline 已过期, 禁止自动恢复并重置 wall-time 预算。",
                )
            )
    phase_values = {phase.value for phase in RunPhase}
    if run.status.value in phase_values and run.status.value != state.phase.value:
        issues.append(_interrupt("run_phase_status_mismatch", "Run.status 与 RunState.phase 不一致。"))
    return tuple(issues)


def _validate_pending_approval(
    run: Run,
    state: RunState,
    approval_id: str,
    approval_records: dict[str, EntityRecord],
    now: datetime | None,
) -> RecoveryIssue | None:
    entity = approval_records.get(approval_id)
    if entity is None:
        return _interrupt("pending_approval_missing", f"审批 {approval_id} 缺少持久 ApprovalRecord。")
    if entity.entity_id != approval_id or not isinstance(entity.value, ApprovalRecord):
        return _interrupt("pending_approval_corrupt", f"审批 {approval_id} 的持久记录类型或实体键损坏。")
    record = entity.value
    if entity.revision != record.revision or record.request.approval_id != approval_id:
        return _interrupt("pending_approval_corrupt", f"审批 {approval_id} 的持久 revision 或身份不一致。")
    if record.state is not ApprovalState.PENDING or record.resolution is not None:
        return _interrupt("pending_approval_not_pending", f"审批 {approval_id} 已决定或状态不一致。")
    binding = record.request.binding
    if not binding.has_recovery_identity:
        return _interrupt("pending_approval_binding_incomplete", f"审批 {approval_id} 缺少完整恢复绑定。")
    if now is None:
        return _interrupt("pending_approval_clock_unavailable", f"审批 {approval_id} 无法验证有效期限。")
    if now >= binding.expires_at:
        return _interrupt("pending_approval_expired", f"审批 {approval_id} 已过期, 禁止恢复等待。")
    same_call_records = tuple(
        candidate.value
        for candidate in approval_records.values()
        if isinstance(candidate.value, ApprovalRecord)
        and candidate.value.request.tool_call_id == record.request.tool_call_id
    )
    if len(same_call_records) != 1:
        return _interrupt("pending_approval_duplicate", f"审批 {approval_id} 的 ToolCall 存在重复持久审批。")
    matching_calls = tuple(
        call for call in state.pending.tool_calls if call.tool_call_id == record.request.tool_call_id
    )
    if len(matching_calls) != 1 or record.request.tool_call_id not in state.pending.tool_call_ids:
        return _interrupt("pending_approval_tool_call_missing", f"审批 {approval_id} 没有唯一待处理 ToolCall。")
    call = matching_calls[0]
    expected_argument = call.arguments.get("expectedHash")
    expected_argument_hash = expected_argument if isinstance(expected_argument, str) else None
    identity_matches = (
        binding.tool_name == call.name
        and binding.tool_version == call.version
        and binding.definition_fingerprint == call.definition_fingerprint
        and binding.args_hash == call.args_hash
        and binding.workspace_id == call.workspace_id == run.workspace_id == state.workspace_id
        and binding.session_id == run.session_id == state.session_id
        and binding.root_run_id == call.lineage.root_run_id
        and binding.run_id == call.run_id == run.run_id == state.run_id
        and binding.agent_name == call.lineage.agent_name
        and binding.ancestor_run_ids == call.lineage.ancestor_run_ids
    )
    if not identity_matches:
        return _interrupt("pending_approval_binding_mismatch", f"审批 {approval_id} 与 ToolCall/Run 绑定漂移。")
    if expected_argument_hash is not None and binding.expected_state_hash != expected_argument_hash:
        return _interrupt("pending_approval_expected_hash_mismatch", f"审批 {approval_id} 的 expectedHash 已漂移。")
    return None


def _validate_turn(run: Run, entity_id: str, turn: Turn) -> tuple[RecoveryIssue, ...]:
    issues: list[RecoveryIssue] = []
    if entity_id != turn.turn_id:
        issues.append(_interrupt("turn_entity_mismatch", "Turn 实体键与 turn_id 不一致。"))
    if turn.turn_id != run.turn_id or turn.session_id != run.session_id:
        issues.append(_interrupt("turn_identity_mismatch", "Turn 与权威 Run 的身份不一致。"))
    if turn.status not in _ACTIVE_TURN_STATUSES:
        issues.append(_interrupt("turn_terminal_mismatch", "非终态 Run 对应了终态 Turn。"))
    return tuple(issues)


def _validate_call(
    run: Run,
    state: RunState,
    call: ToolCall,
    recorded_result_ids: set[str],
) -> tuple[RecoveryIssue, ...]:
    issues: list[RecoveryIssue] = []
    binding = state.tool_result_sensitivities.get(call.tool_call_id)
    if call.result_sensitivity is ResultSensitivity.UNKNOWN:
        issues.append(
            _interrupt(
                "tool_result_sensitivity_unknown",
                "旧 ToolCall 缺少结果敏感度快照; 禁止静默恢复或重放。",
                call.tool_call_id,
            )
        )
    if binding is None:
        issues.append(
            _interrupt(
                "tool_result_sensitivity_binding_missing",
                "RunState 缺少 ToolCall 的结果敏感度绑定; 禁止恢复。",
                call.tool_call_id,
            )
        )
    elif binding is ResultSensitivity.UNKNOWN or binding is not call.result_sensitivity:
        issues.append(
            _interrupt(
                "tool_result_sensitivity_binding_mismatch",
                "RunState 与 ToolCall 的结果敏感度绑定不一致; 禁止恢复。",
                call.tool_call_id,
            )
        )
    if (
        call.run_id != run.run_id
        or call.workspace_id != run.workspace_id
        or call.lineage != run.lineage
        or call.run_id != state.run_id
    ):
        issues.append(
            _interrupt(
                "pending_tool_call_identity_mismatch",
                "Pending ToolCall 与 Run/RunState 的 workspace、run 或 lineage 不一致。",
                call.tool_call_id,
            )
        )
    if call.tool_call_id in recorded_result_ids:
        issues.append(
            _interrupt(
                "pending_tool_call_already_recorded",
                "同一 ToolCall 同时存在于 pending 与已记录结果中。",
                call.tool_call_id,
            )
        )
    return tuple(issues)


def _validate_journal_shape(record: InvocationRecord, call: ToolCall) -> RecoveryIssue | None:
    started_at = record.started_at
    completed_at = record.completed_at
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        return _interrupt(
            "invocation_journal_record_corrupt",
            "Invocation Journal 的 started_at 不是 timezone-aware 时间。",
            call.tool_call_id,
        )
    if completed_at is not None and (
        completed_at.tzinfo is None or completed_at.utcoffset() is None or completed_at < started_at
    ):
        return _interrupt(
            "invocation_journal_record_corrupt",
            "Invocation Journal 的 completed_at 无效。",
            call.tool_call_id,
        )
    if record.state is JournalState.STARTED and (completed_at is not None or record.result is not None):
        return _interrupt(
            "invocation_journal_record_corrupt",
            "STARTED Invocation Journal 不得包含完成时间或结果。",
            call.tool_call_id,
        )
    if record.state is JournalState.COMPLETED and (completed_at is None or record.result is None):
        return _interrupt(
            "invocation_journal_record_corrupt",
            "COMPLETED Invocation Journal 必须包含完成时间和结果。",
            call.tool_call_id,
        )
    if record.state is JournalState.UNKNOWN and (completed_at is None or record.result is not None):
        return _interrupt(
            "invocation_journal_record_corrupt",
            "UNKNOWN Invocation Journal 必须包含完成时间且不得伪造结果。",
            call.tool_call_id,
        )
    return None


def _unknown_issue(definition: ToolDefinition, call: ToolCall, message: str) -> RecoveryIssue:
    if is_side_effect_free(definition):
        return _interrupt("journal_unknown_read", message, call.tool_call_id)
    return _manual("journal_unknown_effectful", f"{message} 禁止重放未知副作用。", call.tool_call_id)


def _interrupt(code: str, message: str, tool_call_id: str | None = None) -> RecoveryIssue:
    return RecoveryIssue(code, message, RecoveryDisposition.INTERRUPT, tool_call_id)


def _manual(code: str, message: str, tool_call_id: str | None = None) -> RecoveryIssue:
    return RecoveryIssue(code, message, RecoveryDisposition.MANUAL_REVIEW, tool_call_id)


def _disposition(issues: tuple[RecoveryIssue, ...] | list[RecoveryIssue]) -> RecoveryDisposition:
    if any(issue.disposition is RecoveryDisposition.INTERRUPT for issue in issues):
        return RecoveryDisposition.INTERRUPT
    if issues:
        return RecoveryDisposition.MANUAL_REVIEW
    return RecoveryDisposition.RESUME


__all__ = [
    "RecoveryAction",
    "RecoveryActionKind",
    "RecoveryCoordinator",
    "RecoveryDefinitionResolver",
    "RecoveryDisposition",
    "RecoveryError",
    "RecoveryIssue",
    "RecoveryLookup",
    "RecoveryPlan",
    "RecoveryScanError",
]
