"""Durable Harness-owned child AgentRun service and public command boundary."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from offeragent_harness.agent.state import RunState
from offeragent_harness.hooks import HookExecutionContext
from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import Clock, EventSink, IdGenerator, NewEvent, UnitOfWorkFactory
from offeragent_harness.ports.cancellation import CancellationToken, OperationCancelled
from offeragent_harness.ports.storage import EntityRecord
from offeragent_harness.ports.subagents import (
    ChildCancellationSource,
    ChildRunControlMessage,
    ChildRunExecution,
    ParentRunAuthorityProvider,
    SubagentEventFactory,
    SubagentOwnershipCleaner,
    SubagentResultArtifactWriter,
    SubagentRunExecutor,
)
from offeragent_harness.sessions import Run, RunKind, RunStatus, TerminationReason
from offeragent_harness.tools.canonical import canonical_json_sha256

from .budget import ChildBudgetReservation, SubagentBudgetError, SubagentBudgetTree
from .catalog import AgentDefinitionCatalog
from .context import ContextForker, ScopeDeriver
from .lifecycle import SubagentLifecycleHooks
from .mailbox import DurableMailbox, MailboxMessage
from .models import (
    AgentCancelCommand,
    AgentSendCommand,
    AgentSpawnCommand,
    AgentUsage,
    AgentWaitCommand,
    MailboxReceipt,
    SubagentCancelReceipt,
    SubagentHandle,
    SubagentLifetime,
    SubagentResult,
    SubagentRunRecord,
    SubagentRunStatus,
    SubagentSpawnRequest,
    SubagentStatusSnapshot,
    SubagentWaitResult,
    WaitMode,
)
from .scheduler import (
    ChildRunScheduler,
    SchedulerCancellationCode,
    SchedulerCancellationReason,
)
from .serialization import (
    context_from_value,
    context_to_value,
    result_from_value,
    result_to_value,
    run_record_from_value,
    run_record_to_value,
    usage_to_value,
)

_ACTIVE = frozenset(
    {
        SubagentRunStatus.CREATED,
        SubagentRunStatus.QUEUED,
        SubagentRunStatus.STARTING,
        SubagentRunStatus.RUNNING,
        SubagentRunStatus.WAITING_TOOL,
        SubagentRunStatus.WAITING_APPROVAL,
        SubagentRunStatus.WAITING_CHILDREN,
        SubagentRunStatus.COMPLETING,
        SubagentRunStatus.CANCEL_REQUESTED,
    }
)


class SubagentServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class SubagentLifecycleBindingProvider(Protocol):
    def binding_for(
        self,
        parent_run_id: str,
    ) -> tuple[SubagentLifecycleHooks, HookExecutionContext] | None: ...


@dataclass(frozen=True, slots=True)
class _ExecutionCursor:
    run_revision: int
    state_revision: int
    event_sequence: int


class SubagentService:
    """Owns child Runs while delegating reasoning to the one Harness Agent Loop."""

    def __init__(
        self,
        *,
        workspace_id: str,
        worker_id: str,
        unit_of_work: UnitOfWorkFactory,
        event_sink: EventSink,
        clock: Clock,
        ids: IdGenerator,
        catalog: AgentDefinitionCatalog,
        authorities: ParentRunAuthorityProvider,
        context_forker: ContextForker,
        scope_deriver: ScopeDeriver,
        budget_tree: SubagentBudgetTree,
        scheduler: ChildRunScheduler,
        mailbox: DurableMailbox,
        runner: SubagentRunExecutor,
        result_artifacts: SubagentResultArtifactWriter,
        event_factory: SubagentEventFactory,
        lifecycle_bindings: SubagentLifecycleBindingProvider | None = None,
        max_per_turn: int = 8,
        hard_max_depth: int = 3,
        lease_seconds: int = 30,
    ) -> None:
        if (
            not workspace_id
            or not worker_id
            or catalog.workspace_id != workspace_id
            or not 1 <= max_per_turn <= 128
            or not 1 <= hard_max_depth <= 3
            or not 5 <= lease_seconds <= 300
        ):
            raise ValueError("SubagentService identity/limits are invalid")
        self.workspace_id = workspace_id
        self._worker_id = worker_id
        self._unit_of_work = unit_of_work
        self._event_sink = event_sink
        self._clock = clock
        self._ids = ids
        self._catalog = catalog
        self._authorities = authorities
        self._context_forker = context_forker
        self._scope_deriver = scope_deriver
        self._budget_tree = budget_tree
        self._scheduler = scheduler
        self._mailbox = mailbox
        self._runner = runner
        self._result_artifacts = result_artifacts
        self._event_factory = event_factory
        self._lifecycle_bindings = lifecycle_bindings
        self._max_per_turn = max_per_turn
        self._hard_depth = hard_max_depth
        self._lease_seconds = lease_seconds
        self._lock = asyncio.Lock()
        self._reservations: dict[str, ChildBudgetReservation] = {}
        self._cursors: dict[str, _ExecutionCursor] = {}
        self._child_lifecycle_bindings: dict[
            str,
            tuple[SubagentLifecycleHooks, HookExecutionContext],
        ] = {}
        self._condition = asyncio.Condition()
        self._status_generation = 0
        self.delivery_failures: list[str] = []

    async def spawn(self, command: AgentSpawnCommand, cancellation: CancellationToken) -> SubagentHandle:
        cancellation.checkpoint()
        request_hash = canonical_json_sha256(_spawn_identity(command))
        receipt_id = f"{command.parent_run_id}:{command.spawn_call_id}"
        async with self._lock:
            replay = await self._spawn_replay(receipt_id, request_hash)
            if replay is not None:
                return replay
            authority = await self._authorities.authority_for(command.parent_run_id)
            self._validate_parent(authority, command)
            descriptor = self._catalog.resolve(command.profile)
            definition = descriptor.definition
            depth = authority.lineage.depth + 1
            if depth > min(self._hard_depth, definition.max_depth):
                raise SubagentServiceError("subagent_depth_limit", "Subagent depth limit exceeded")
            if not authority.can_spawn_children:
                raise SubagentServiceError("subagent_spawn_denied", "parent Agent Definition forbids child Runs")
            if (
                command.budget.model_calls < 1
                or command.budget.input_tokens < 1
                or command.budget.output_tokens < 1
                or command.budget.artifact_bytes < 1_024
                or command.budget.wall_time_seconds <= 0
            ):
                raise SubagentServiceError("subagent_budget_invalid", "child budget cannot execute an Agent Loop")
            deadline = min(
                command.deadline_at or authority.deadline_at,
                authority.deadline_at,
                self._clock.utcnow() + timedelta(seconds=command.budget.wall_time_seconds),
            )
            if deadline <= self._clock.utcnow():
                raise SubagentServiceError("subagent_deadline", "child deadline has already expired")
            await self._check_turn_and_duplicate(authority.turn_id, authority.lineage.root_run_id, command)
            derived = self._scope_deriver.derive(authority, definition, command)
            context = self._context_forker.fork(authority, command, definition)
            run_id = self._ids.new_id("run")
            lifecycle_binding = (
                None
                if self._lifecycle_bindings is None
                else self._lifecycle_bindings.binding_for(command.parent_run_id)
            )
            if lifecycle_binding is not None:
                lifecycle, hook_context = lifecycle_binding
                await lifecycle.before_start(
                    SubagentSpawnRequest(
                        spawn_call_id=command.spawn_call_id,
                        parent_lineage=authority.lineage,
                        child_run_id=run_id,
                        task=command.task,
                        profile=command.profile,
                        context_mode=command.context_mode,
                        selected_message_ids=command.selected_message_ids,
                        selected_artifact_ids=command.selected_artifact_ids,
                        requested_scope=command.requested_scope,
                        requested_permission_mode=command.requested_permission_mode,
                        requested_tool_versions=command.requested_tool_versions,
                        requested_tool_constraints=command.requested_tool_constraints,
                        budget=command.budget,
                        lifetime=command.lifetime,
                        deadline_at=deadline,
                        priority=command.priority,
                    ),
                    hook_context,
                    cancellation,
                )
            try:
                reservation = await self._budget_tree.reserve(
                    command.budget,
                    parent_remaining=authority.remaining_budget,
                    child_depth=depth,
                    root_run_id=authority.lineage.root_run_id,
                )
            except SubagentBudgetError as error:
                raise SubagentServiceError(f"subagent_{error.code}", str(error)) from error
            trace_id = self._ids.new_id("trace")
            now = self._clock.utcnow()
            record = SubagentRunRecord(
                run_id=run_id,
                root_run_id=authority.lineage.root_run_id,
                parent_run_id=authority.lineage.run_id,
                ancestor_run_ids=(*authority.lineage.ancestor_run_ids, authority.lineage.run_id),
                session_id=authority.session_id,
                turn_id=authority.turn_id,
                workspace_id=authority.workspace_id,
                trace_id=trace_id,
                spawn_call_id=command.spawn_call_id,
                agent_name=definition.name,
                agent_version=definition.version,
                task=command.task.strip(),
                task_fingerprint=_task_fingerprint(command.task),
                depth=depth,
                lifetime=command.lifetime,
                context_snapshot_id=context.snapshot_id,
                permission_mode=derived.permission_mode,
                effective_scope=derived.capability_scope,
                tool_scope=derived.tool_scope,
                budget_limit=command.budget,
                budget_used=AgentUsage(),
                deadline_at=deadline,
                result_schema=definition.result_schema,
                status=SubagentRunStatus.QUEUED,
                phase="queued",
                priority=command.priority,
                created_at=now,
                updated_at=now,
            )
            try:
                cursor, stored = await self._persist_spawn(
                    record,
                    context,
                    authority.run_config,
                    receipt_id,
                    request_hash,
                )
            except BaseException:
                committed = await self._committed_spawn(record.run_id, receipt_id, request_hash)
                if committed is None:
                    await reservation.release()
                    raise
                record, cursor, stored = committed
            self._reservations[run_id] = reservation
            self._cursors[run_id] = cursor
            if lifecycle_binding is not None:
                self._child_lifecycle_bindings[run_id] = lifecycle_binding
            await self._publish(stored)
            try:
                await self._scheduler.submit(
                    record,
                    run=self._run_child,
                    started=self._started,
                    finished=self._finished,
                )
            except BaseException as error:
                await self._terminal_failure(run_id, "subagent_schedule_failed", error)
                raise
            return SubagentHandle(run_id, command.parent_run_id, SubagentRunStatus.QUEUED, now)

    async def _committed_spawn(
        self,
        run_id: str,
        receipt_id: str,
        request_hash: str,
    ) -> tuple[SubagentRunRecord, _ExecutionCursor, tuple[Any, ...]] | None:
        replay = await self._spawn_replay(receipt_id, request_hash)
        if replay is None or replay.run_id != run_id:
            return None
        record, _ = await self._record_with_revision(run_id)
        run_record = await _find_entity_record(self._unit_of_work, "runs", run_id)
        state_record = await _find_entity_record(self._unit_of_work, "run_states", run_id)
        if run_record is None or state_record is None or not isinstance(run_record.value, Run):
            return None
        async with self._unit_of_work.begin() as uow:
            sequence = await uow.events.latest_sequence(run_id)
            stored = await uow.events.read(run_id, after_sequence=0)
        return record, _ExecutionCursor(run_record.revision, state_record.revision, sequence), stored

    async def send(self, command: AgentSendCommand, cancellation: CancellationToken) -> MailboxReceipt:
        cancellation.checkpoint()
        async with self._lock:
            receipt = await self._send_locked(command)
        cancellation.checkpoint()
        return receipt

    async def _send_locked(self, command: AgentSendCommand) -> MailboxReceipt:
        record = await self._managed_record(command.requester_run_id, command.run_id)
        if record.status.terminal:
            raise SubagentServiceError("subagent_terminal", "cannot send to a terminal child Run")
        if command.artifact_ids:
            authority = await self._authorities.authority_for(command.requester_run_id)
            authorized = {
                str(item.get("id"))
                for item in authority.context.get("artifacts", [])
                if isinstance(item, Mapping) and item.get("authorized") is True
            }
            if not set(command.artifact_ids) <= authorized:
                raise SubagentServiceError("subagent_artifact_scope", "agent.send includes an unauthorized Artifact")
        receipt, message = await self._mailbox.send(command)
        if not receipt.duplicate:
            await self._append_message_event(record, message)
            await self._runner.deliver_message(
                record.run_id,
                ChildRunControlMessage(
                    message.message_id,
                    message.mode.value,
                    message.message,
                    message.artifact_ids,
                    await self._latest_sequence(record.run_id),
                ),
            )
        return receipt

    async def wait(self, command: AgentWaitCommand, cancellation: CancellationToken) -> SubagentWaitResult:
        async with self._condition:
            generation = self._status_generation
        records = [await self._managed_record(command.requester_run_id, run_id) for run_id in command.run_ids]
        completed = tuple(item.run_id for item in records if item.status.terminal)
        if (command.mode is WaitMode.ANY and completed) or (
            command.mode is WaitMode.ALL and len(completed) == len(records)
        ):
            return SubagentWaitResult(
                completed, tuple(item.run_id for item in records if not item.status.terminal), False
            )
        if command.timeout_ms == 0:
            return SubagentWaitResult(
                completed, tuple(item.run_id for item in records if not item.status.terminal), True
            )
        wait_task = asyncio.create_task(self._wait_for_status_change(generation))
        cancel_task = asyncio.create_task(cancellation.wait())
        done, pending = await asyncio.wait(
            {wait_task, cancel_task},
            timeout=command.timeout_ms / 1000,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if cancel_task in done:
            cancellation.checkpoint()
        final = [await self._managed_record(command.requester_run_id, run_id) for run_id in command.run_ids]
        completed_ids = tuple(item.run_id for item in final if item.status.terminal)
        pending_ids = tuple(item.run_id for item in final if not item.status.terminal)
        satisfied = bool(completed_ids) if command.mode is WaitMode.ANY else not pending_ids
        return SubagentWaitResult(completed_ids, pending_ids, not satisfied)

    async def status(self, requester_run_id: str, run_id: str) -> SubagentStatusSnapshot:
        record = await self._managed_record(requester_run_id, run_id)
        projected_status = record.status
        projected_phase = record.phase
        projected_usage = record.budget_used
        if record.status in _ACTIVE:
            async with self._unit_of_work.begin() as uow:
                state = await uow.entities.get("run_states", run_id)
            if isinstance(state, RunState):
                projected_phase = state.phase.value
                projected_status = _status_for_phase(projected_phase, record.status)
                projected_usage = await self._checkpoint_usage(run_id)
        children = tuple(item.run_id for item in await self._children(run_id))
        return SubagentStatusSnapshot(
            record.run_id,
            record.root_run_id,
            record.parent_run_id,
            record.agent_name,
            projected_status,
            projected_phase,
            record.depth,
            record.budget_limit,
            projected_usage,
            record.deadline_at,
            children,
            record.updated_at,
        )

    async def result(
        self,
        requester_run_id: str,
        run_id: str,
        include: frozenset[str] = frozenset({"summary", "findings", "artifacts", "usage"}),
    ) -> SubagentResult:
        record = await self._managed_record(requester_run_id, run_id)
        if not record.status.terminal:
            raise SubagentServiceError("subagent_not_complete", "Subagent result is not available yet")
        async with self._unit_of_work.begin() as uow:
            raw = await uow.entities.get("subagent_results", run_id)
        if raw is None:
            raise SubagentServiceError("subagent_result_missing", "terminal Subagent result is missing")
        result = result_from_value(raw)
        supported = {"summary", "findings", "evidence", "artifacts", "proposedActions", "usage"}
        if not include <= supported:
            raise ValueError("agent.result include contains an unsupported field")
        return SubagentResult(
            result.run_id,
            result.status,
            result.summary if "summary" in include else "Result fields omitted by include filter",
            result.findings if "findings" in include else (),
            result.evidence if "evidence" in include else (),
            result.artifact_ids if "artifacts" in include else (),
            result.proposed_actions if "proposedActions" in include else (),
            result.unresolved_questions,
            result.usage if "usage" in include else {},
            result.error,
        )

    async def cancel(
        self,
        command: AgentCancelCommand,
        cancellation: CancellationToken,
    ) -> SubagentCancelReceipt:
        cancellation.checkpoint()
        target = await self._managed_record(command.requester_run_id, command.run_id)
        descendants = await self._descendants(target.run_id) if command.cascade else ()
        targets = (*sorted(descendants, key=lambda item: item.depth, reverse=True), target)
        accepted = False
        for record in targets:
            if record.status.terminal:
                continue
            await self._mark_cancel_requested(record, command.reason, command.cascade)
            accepted = await self._scheduler.cancel(record.run_id, command.reason) or accepted
        cancellation.checkpoint()
        return SubagentCancelReceipt(target.run_id, accepted, tuple(item.run_id for item in descendants))

    async def cancel_descendants(self, parent_run_id: str, reason: str) -> tuple[str, ...]:
        children = await self._children(parent_run_id)
        cancelled: list[str] = []
        for child in children:
            receipt = await self.cancel(
                AgentCancelCommand(parent_run_id, child.run_id, reason, True),
                _NeverCancelled(),
            )
            if receipt.accepted:
                cancelled.extend((receipt.run_id, *receipt.descendant_run_ids))
        return tuple(dict.fromkeys(cancelled))

    async def parent_finished(self, parent_run_id: str, *, turn_finished: bool, reason: str) -> tuple[str, ...]:
        children = await self._children(parent_run_id)
        cancelled: list[str] = []
        for child in children:
            if child.lifetime is SubagentLifetime.SESSION:
                continue
            if child.lifetime is SubagentLifetime.TURN and not turn_finished:
                continue
            receipt = await self.cancel(
                AgentCancelCommand(parent_run_id, child.run_id, reason, True), _NeverCancelled()
            )
            if receipt.accepted:
                cancelled.append(child.run_id)
        return tuple(cancelled)

    async def receive_messages(self, run_id: str, *, after_sequence: int) -> tuple[MailboxMessage, ...]:
        return await self._mailbox.receive(run_id, after_sequence=after_sequence)

    async def recover_orphans(self, cleaner: SubagentOwnershipCleaner) -> tuple[str, ...]:
        """Reconcile queued/expired child leases without replaying unsafe work."""

        recovered: list[str] = []
        async with self._lock:
            for record in await self._all_records():
                if record.status is SubagentRunStatus.QUEUED:
                    if await self._recover_queued(record):
                        recovered.append(record.run_id)
                    continue
                if record.status not in _ACTIVE or record.lease_expires_at is None:
                    continue
                if record.lease_owner == self._worker_id and record.lease_expires_at > self._clock.utcnow():
                    continue
                state_record = await _find_entity_record(self._unit_of_work, "run_states", record.run_id)
                state = (
                    state_record.value
                    if state_record is not None and isinstance(state_record.value, RunState)
                    else None
                )
                safe_checkpoint = _safe_to_resume(record, state)
                orphaned = await self._mark_orphaned(record, safe_checkpoint=safe_checkpoint)
                await cleaner.cleanup(record.run_id)
                if safe_checkpoint:
                    if await self._recover_queued(orphaned, previous_status=SubagentRunStatus.ORPHANED):
                        recovered.append(record.run_id)
                else:
                    await self._interrupt_orphan(orphaned)
        return tuple(recovered)

    async def heartbeat(self, run_id: str) -> None:
        async with self._lock:
            record, revision = await self._record_with_revision(run_id)
            if record.status not in _ACTIVE or record.lease_owner != self._worker_id:
                return
            updated = replace(
                record,
                lease_expires_at=self._clock.utcnow() + timedelta(seconds=self._lease_seconds),
                updated_at=self._clock.utcnow(),
                revision=record.revision + 1,
            )
            async with self._unit_of_work.begin() as uow:
                await uow.entities.put(
                    "subagent_runs",
                    run_id,
                    run_record_to_value(updated),
                    expected_revision=revision,
                )
                await uow.commit()

    async def _persist_spawn(
        self,
        record: SubagentRunRecord,
        context: Any,
        parent_run_config: Mapping[str, Any],
        receipt_id: str,
        request_hash: str,
    ) -> tuple[_ExecutionCursor, tuple[Any, ...]]:
        state = RunState(
            record.workspace_id,
            record.session_id,
            record.turn_id,
            record.run_id,
            record.lineage,
        )
        run_config = dict(thaw_json(parent_run_config))
        run_config["subagent"] = {
            "agentName": record.agent_name,
            "agentVersion": record.agent_version,
            "permissionMode": record.permission_mode.value,
            "contextSnapshotId": record.context_snapshot_id,
            "toolScope": thaw_json(record.tool_scope.allowed_versions),
        }
        run = Run(
            record.run_id,
            record.session_id,
            record.turn_id,
            record.workspace_id,
            record.lineage,
            RunKind.SUBAGENT,
            RunStatus.QUEUED,
            1,
            1,
            run_config,
            record.created_at,
            record.updated_at,
            record.deadline_at,
        )
        event = self._event(
            record,
            "subagent.queued",
            {
                "childRunId": record.run_id,
                "parentRunId": record.parent_run_id,
                "agentName": record.agent_name,
                "task": record.task,
                "depth": record.depth,
            },
            sequence=1,
            terminal=False,
        )
        async with self._unit_of_work.begin() as uow:
            parent_raw = await uow.entities.get("subagent_runs", record.parent_run_id)
            if parent_raw is not None:
                parent = run_record_from_value(parent_raw)
                parent_usage = replace(parent.budget_used, child_count=parent.budget_used.child_count + 1)
                if not parent_usage.fits_within(parent.budget_limit):
                    raise SubagentServiceError("subagent_parent_budget", "parent child-count budget is exhausted")
                await uow.entities.put(
                    "subagent_runs",
                    parent.run_id,
                    run_record_to_value(
                        replace(
                            parent,
                            budget_used=parent_usage,
                            updated_at=self._clock.utcnow(),
                            revision=parent.revision + 1,
                        )
                    ),
                    expected_revision=parent.revision,
                )
            run_revision = await uow.entities.put("runs", record.run_id, run, expected_revision=0)
            state_revision = await uow.entities.put("run_states", record.run_id, state, expected_revision=0)
            await uow.entities.put("subagent_runs", record.run_id, run_record_to_value(record), expected_revision=0)
            await uow.entities.put(
                "subagent_contexts",
                record.context_snapshot_id,
                context_to_value(context),
                expected_revision=0,
            )
            await uow.entities.put(
                "subagent_spawn_receipts",
                receipt_id,
                {"schemaVersion": 1, "requestHash": request_hash, "runId": record.run_id},
                expected_revision=0,
            )
            await uow.entities.put(
                "subagent_budget_reservations",
                record.run_id,
                {"schemaVersion": 1, "state": "reserved", "limit": run_record_to_value(record)["budgetLimit"]},
                expected_revision=0,
            )
            stored = await uow.events.append(record.run_id, 0, (event,))
            await uow.commit()
        return _ExecutionCursor(run_revision, state_revision, 1), stored

    async def _recover_queued(
        self,
        record: SubagentRunRecord,
        *,
        previous_status: SubagentRunStatus | None = None,
    ) -> bool:
        try:
            reservation = await self._budget_tree.adopt(
                record.budget_limit,
                root_run_id=record.root_run_id,
            )
        except (SubagentBudgetError, ValueError) as error:
            if previous_status is not None:
                await self._interrupt_orphan(record)
                return False
            raise SubagentServiceError(
                "subagent_budget_corrupt",
                "queued child reservation is absent from the authoritative root budget checkpoint",
            ) from error
        run_record = await _find_entity_record(self._unit_of_work, "runs", record.run_id)
        state_record = await _find_entity_record(self._unit_of_work, "run_states", record.run_id)
        if run_record is None or not isinstance(run_record.value, Run) or state_record is None:
            await reservation.release()
            if previous_status is not None:
                await self._interrupt_orphan(record)
            return False
        sequence = await self._latest_sequence(record.run_id)
        current = record
        if previous_status is not None:
            now = self._clock.utcnow()
            current = replace(
                record,
                status=SubagentRunStatus.QUEUED,
                phase="queued",
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now,
                revision=record.revision + 1,
            )
            event = self._event(
                current,
                "subagent.recovered",
                {
                    "childRunId": current.run_id,
                    "previousStatus": previous_status.value,
                    "checkpointSequence": sequence,
                },
                sequence=sequence + 1,
                terminal=False,
            )
            run = replace(
                run_record.value,
                status=RunStatus.QUEUED,
                event_sequence=sequence + 1,
                updated_at=now,
                termination_reason=None,
            )
            _, record_revision = await self._record_with_revision(record.run_id)
            async with self._unit_of_work.begin() as uow:
                await uow.entities.put(
                    "subagent_runs",
                    current.run_id,
                    run_record_to_value(current),
                    expected_revision=record_revision,
                )
                run_revision = await uow.entities.put(
                    "runs",
                    current.run_id,
                    run,
                    expected_revision=run_record.revision,
                )
                stored = await uow.events.append(current.run_id, sequence, (event,))
                await uow.commit()
            await self._publish(stored)
            sequence += 1
        else:
            run_revision = run_record.revision
        self._reservations[current.run_id] = reservation
        self._cursors[current.run_id] = _ExecutionCursor(run_revision, state_record.revision, sequence)
        try:
            await self._scheduler.submit(
                current,
                run=self._run_child,
                started=self._started,
                finished=self._finished,
            )
        except BaseException:
            await reservation.release()
            self._reservations.pop(current.run_id, None)
            return False
        return True

    async def _mark_orphaned(
        self,
        record: SubagentRunRecord,
        *,
        safe_checkpoint: bool,
    ) -> SubagentRunRecord:
        current, revision = await self._record_with_revision(record.run_id)
        sequence = await self._latest_sequence(record.run_id)
        expired_at = current.lease_expires_at or self._clock.utcnow()
        updated = replace(
            current,
            status=SubagentRunStatus.ORPHANED,
            phase="orphaned",
            lease_owner=None,
            lease_expires_at=None,
            safe_checkpoint=safe_checkpoint,
            updated_at=self._clock.utcnow(),
            revision=current.revision + 1,
        )
        event = self._event(
            updated,
            "subagent.orphaned",
            {"childRunId": updated.run_id, "leaseExpiredAt": expired_at.isoformat()},
            sequence=sequence + 1,
            terminal=False,
        )
        run_record = await _find_entity_record(self._unit_of_work, "runs", record.run_id)
        if run_record is None or not isinstance(run_record.value, Run):
            raise SubagentServiceError("subagent_corrupt", "authoritative orphaned Run is missing")
        run = replace(
            run_record.value,
            status=RunStatus.ORPHANED,
            event_sequence=sequence + 1,
            updated_at=self._clock.utcnow(),
            termination_reason=TerminationReason.RUNTIME_INTERRUPTED,
        )
        async with self._unit_of_work.begin() as uow:
            await uow.entities.put(
                "subagent_runs", updated.run_id, run_record_to_value(updated), expected_revision=revision
            )
            await uow.entities.put("runs", updated.run_id, run, expected_revision=run_record.revision)
            stored = await uow.events.append(updated.run_id, sequence, (event,))
            await uow.commit()
        await self._publish(stored)
        return updated

    async def _interrupt_orphan(self, record: SubagentRunRecord) -> None:
        current, _ = await self._record_with_revision(record.run_id)
        if current.status is not SubagentRunStatus.ORPHANED:
            return
        if current.run_id not in self._reservations:
            try:
                self._reservations[current.run_id] = await self._budget_tree.adopt(
                    current.budget_limit,
                    root_run_id=current.root_run_id,
                )
            except (SubagentBudgetError, ValueError) as error:
                raise SubagentServiceError(
                    "subagent_budget_corrupt",
                    "orphan budget reservation cannot be adopted",
                ) from error
        result = SubagentResult(
            current.run_id,
            "interrupted",
            "Subagent was interrupted after Worker recovery",
            (),
            (),
            (),
            (),
            (),
            {},
            {"code": "orphan_not_safely_recoverable"},
        )
        await self._terminal_simple(
            current.run_id,
            result,
            SubagentRunStatus.INTERRUPTED,
            "subagent.interrupted",
            "orphan_not_safely_recoverable",
        )

    async def _started(self, run_id: str) -> None:
        async with self._lock:
            await self._started_locked(run_id)

    async def _started_locked(self, run_id: str) -> None:
        record, record_revision = await self._record_with_revision(run_id)
        if record.status is SubagentRunStatus.CANCEL_REQUESTED:
            raise asyncio.CancelledError("cancelled while queued")
        now = self._clock.utcnow()
        updated = replace(
            record,
            status=SubagentRunStatus.RUNNING,
            phase="starting",
            lease_owner=self._worker_id,
            lease_expires_at=now + timedelta(seconds=self._lease_seconds),
            updated_at=now,
            revision=record.revision + 1,
        )
        sequence = await self._latest_sequence(run_id)
        event = self._event(
            updated,
            "subagent.started",
            {
                "childRunId": updated.run_id,
                "parentRunId": updated.parent_run_id,
                "agentName": updated.agent_name,
                "contextMode": (await self._context(updated.context_snapshot_id)).mode.value,
                "budget": _protocol_budget(updated),
            },
            sequence=sequence + 1,
            terminal=False,
        )
        run_record = await _find_entity_record(self._unit_of_work, "runs", run_id)
        state_record = await _find_entity_record(self._unit_of_work, "run_states", run_id)
        if run_record is None or not isinstance(run_record.value, Run) or state_record is None:
            raise SubagentServiceError("subagent_corrupt", "authoritative child Run state is missing")
        run = replace(run_record.value, status=RunStatus.STARTING, event_sequence=sequence + 1, updated_at=now)
        async with self._unit_of_work.begin() as uow:
            new_record_revision = await uow.entities.put(
                "subagent_runs", run_id, run_record_to_value(updated), expected_revision=record_revision
            )
            del new_record_revision
            run_revision = await uow.entities.put("runs", run_id, run, expected_revision=run_record.revision)
            stored = await uow.events.append(run_id, sequence, (event,))
            await uow.commit()
        self._cursors[run_id] = _ExecutionCursor(run_revision, state_record.revision, stored[-1].sequence)
        await self._publish(stored)

    async def _run_child(
        self,
        record: SubagentRunRecord,
        cancellation: ChildCancellationSource,
    ) -> SubagentResult:
        current, _ = await self._record_with_revision(record.run_id)
        if self._clock.utcnow() >= current.deadline_at:
            await cancellation.cancel(
                SchedulerCancellationReason(
                    SchedulerCancellationCode.DEADLINE,
                    "Subagent deadline expired in the scheduler queue",
                    self._clock.utcnow(),
                )
            )
            cancellation.checkpoint()
        cursor = self._cursors[record.run_id]
        context = await self._context(current.context_snapshot_id)
        run = await self._run(current.run_id)
        execution = ChildRunExecution(
            current,
            context,
            current.tool_scope,
            run.config_snapshot,
            cursor.run_revision,
            cursor.state_revision,
            cursor.event_sequence,
            current.trace_id,
            tuple(
                ChildRunControlMessage(
                    message.message_id,
                    message.mode.value,
                    message.message,
                    message.artifact_ids,
                    cursor.event_sequence,
                )
                for message in await self._mailbox.receive(current.run_id, after_sequence=0)
            ),
        )
        heartbeat = asyncio.create_task(
            self._maintain_lease(current.run_id, cancellation),
            name=f"subagent-heartbeat:{current.run_id}",
        )
        try:
            return await self._runner.execute(execution, cancellation)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _maintain_lease(self, run_id: str, cancellation: CancellationToken) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(cancellation.wait(), timeout=interval)
                return
            except TimeoutError:
                await self.heartbeat(run_id)

    async def _finished(
        self,
        run_id: str,
        result: SubagentResult | None,
        error: BaseException | None,
    ) -> None:
        async with self._lock:
            await self._finished_locked(run_id, result, error)
        binding = self._child_lifecycle_bindings.pop(run_id, None)
        if binding is None:
            return
        try:
            record, _ = await self._record_with_revision(run_id)
            hook_result = result or SubagentResult(
                run_id=run_id,
                status=("cancelled" if isinstance(error, (OperationCancelled, asyncio.CancelledError)) else "failed"),
                summary="Subagent stopped without a completed result",
                findings=(),
                evidence=(),
                artifact_ids=(),
                proposed_actions=(),
                unresolved_questions=(),
                usage={},
                error=None if error is None else {"code": "subagent_stopped"},
            )
            lifecycle, context = binding
            await lifecycle.after_stop(
                hook_result,
                parent_run_id=record.parent_run_id,
                root_run_id=record.root_run_id,
                context=context,
                cancellation=_NeverCancelled(),
            )
        except Exception as hook_error:
            self.delivery_failures.append(f"SubagentStop Hook failed for {run_id}: {type(hook_error).__name__}")

    async def _finished_locked(
        self,
        run_id: str,
        result: SubagentResult | None,
        error: BaseException | None,
    ) -> None:
        if error is not None:
            if isinstance(error, (OperationCancelled, asyncio.CancelledError)):
                await self._terminal_cancelled(run_id, str(error) or "Subagent cancelled")
            else:
                await self._terminal_failure(run_id, "subagent_execution_failed", error)
            return
        if result is None:
            await self._terminal_failure(run_id, "subagent_result_missing", RuntimeError("runner returned no result"))
            return
        current, _ = await self._record_with_revision(run_id)
        if current.status is SubagentRunStatus.CANCEL_REQUESTED:
            late = replace(
                result,
                status="late_result",
                error={"code": "late_result", "message": "retained for audit after cancellation"},
            )
            await self._terminal_simple(
                run_id,
                late,
                SubagentRunStatus.CANCELLED,
                "subagent.cancelled",
                "Cancellation completed; late result retained for audit only",
            )
            return
        await self._terminal_result(run_id, result)

    async def _terminal_result(self, run_id: str, result: SubagentResult) -> None:
        record, revision = await self._record_with_revision(run_id)
        if result.run_id != run_id:
            await self._terminal_failure(run_id, "subagent_result_run_mismatch", ValueError("result Run ID mismatch"))
            return
        candidate = {
            "summary": result.summary,
            "findings": thaw_json(result.findings),
            "evidence": thaw_json(result.evidence),
            "proposedActions": thaw_json(result.proposed_actions),
            "unresolvedQuestions": list(result.unresolved_questions),
        }
        if not Draft202012Validator(thaw_json(record.result_schema)).is_valid(candidate):
            await self._terminal_failure(
                run_id,
                "subagent_result_schema",
                ValueError("child result failed its Agent Definition schema"),
            )
            return
        usage = _usage_from_result(result)
        if not usage.fits_within(record.budget_limit):
            await self._terminal_failure(run_id, "subagent_usage_exceeded", ValueError("child usage exceeded budget"))
            return
        artifact = await self._result_artifacts.store(record, result, _NeverCancelled())
        usage = replace(usage, artifact_bytes=usage.artifact_bytes + artifact.metadata.byte_length)
        if not usage.fits_within(record.budget_limit):
            await self._terminal_failure(
                run_id,
                "subagent_artifact_budget",
                ValueError("structured result Artifact exceeded child budget"),
            )
            return
        final_status = SubagentRunStatus.COMPLETED if result.status == "completed" else SubagentRunStatus.FAILED
        now = self._clock.utcnow()
        updated = replace(
            record,
            status=final_status,
            phase="completed" if final_status is SubagentRunStatus.COMPLETED else "failed",
            budget_used=usage,
            result_artifact_id=artifact.metadata.artifact_id,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
            revision=record.revision + 1,
        )
        run_record = await _find_entity_record(self._unit_of_work, "runs", run_id)
        if run_record is None or not isinstance(run_record.value, Run):
            raise SubagentServiceError("subagent_corrupt", "authoritative child Run is missing")
        sequence = await self._latest_sequence(run_id)
        events = (
            self._event(
                updated,
                "subagent.result_available",
                {
                    "childRunId": run_id,
                    "resultArtifactId": artifact.metadata.artifact_id,
                    "summary": result.summary,
                },
                sequence=sequence + 1,
                terminal=False,
            ),
            self._terminal_event(updated, result, artifact.metadata, sequence + 2),
        )
        run = replace(
            run_record.value,
            status=RunStatus.COMPLETED if final_status is SubagentRunStatus.COMPLETED else RunStatus.FAILED,
            event_sequence=sequence + 2,
            updated_at=now,
            termination_reason=(
                TerminationReason.COMPLETED
                if final_status is SubagentRunStatus.COMPLETED
                else TerminationReason.MODEL_ERROR
            ),
        )
        async with self._unit_of_work.begin() as uow:
            await uow.entities.put("subagent_runs", run_id, run_record_to_value(updated), expected_revision=revision)
            await uow.entities.put("runs", run_id, run, expected_revision=run_record.revision)
            await uow.entities.put("subagent_results", run_id, result_to_value(result), expected_revision=0)
            budget_raw = await uow.entities.get("subagent_budget_reservations", run_id)
            if not isinstance(budget_raw, Mapping) or budget_raw.get("state") != "reserved":
                raise SubagentServiceError("subagent_budget_corrupt", "child budget reservation is missing")
            await uow.entities.put(
                "subagent_budget_reservations",
                run_id,
                {"schemaVersion": 1, "state": "settled", "usage": usage_to_value(usage)},
                expected_revision=1,
            )
            stored = await uow.events.append(run_id, sequence, events)
            await uow.commit()
        reservation = self._reservations.pop(run_id, None)
        if reservation is not None:
            await reservation.settle(usage)
        await self._publish(stored)
        await self._notify_status()

    async def _terminal_cancelled(self, run_id: str, reason: str) -> None:
        result = SubagentResult(run_id, "cancelled", reason[:16_384] or "Cancelled", (), (), (), (), (), {}, None)
        await self._terminal_simple(run_id, result, SubagentRunStatus.CANCELLED, "subagent.cancelled", reason)

    async def _terminal_failure(self, run_id: str, code: str, error: BaseException) -> None:
        result = SubagentResult(
            run_id,
            "failed",
            "Subagent failed",
            (),
            (),
            (),
            (),
            (),
            {},
            {"code": code, "errorType": type(error).__name__},
        )
        await self._terminal_simple(run_id, result, SubagentRunStatus.FAILED, "subagent.failed", code)

    async def _terminal_simple(
        self,
        run_id: str,
        result: SubagentResult,
        status: SubagentRunStatus,
        event_type: str,
        reason: str,
    ) -> None:
        record, revision = await self._record_with_revision(run_id)
        if record.status.terminal:
            return
        usage = await self._checkpoint_usage(run_id)
        result = replace(result, usage=usage_to_value(usage))
        artifact = await self._result_artifacts.store(record, result, _NeverCancelled())
        usage = replace(usage, artifact_bytes=usage.artifact_bytes + artifact.metadata.byte_length)
        if not usage.fits_within(record.budget_limit):
            raise SubagentServiceError(
                "subagent_artifact_budget",
                "terminal audit Artifact exceeds the reserved child budget",
            )
        now = self._clock.utcnow()
        updated = replace(
            record,
            status=status,
            phase=status.value,
            budget_used=usage,
            result_artifact_id=artifact.metadata.artifact_id,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
            revision=record.revision + 1,
        )
        run_record = await _find_entity_record(self._unit_of_work, "runs", run_id)
        if run_record is None or not isinstance(run_record.value, Run):
            raise SubagentServiceError("subagent_corrupt", "authoritative child Run is missing")
        sequence = await self._latest_sequence(run_id)
        payload: dict[str, Any]
        if event_type == "subagent.cancelled":
            payload = {"childRunId": run_id, "reason": reason[:4096], "usage": _protocol_usage(usage)}
            run_status = RunStatus.CANCELLED
            termination = TerminationReason.CANCELLED_BY_USER
        elif event_type == "subagent.interrupted":
            payload = {
                "childRunId": run_id,
                "error": _protocol_error(reason),
                "safeCheckpointAvailable": record.safe_checkpoint,
            }
            run_status = RunStatus.INTERRUPTED
            termination = TerminationReason.RUNTIME_INTERRUPTED
        else:
            payload = {
                "childRunId": run_id,
                "error": _protocol_error(reason),
                "usage": _protocol_usage(usage),
            }
            run_status = RunStatus.FAILED
            termination = TerminationReason.RUNTIME_INTERRUPTED
        events = (
            self._event(
                updated,
                "subagent.result_available",
                {
                    "childRunId": run_id,
                    "resultArtifactId": artifact.metadata.artifact_id,
                    "summary": result.summary,
                },
                sequence=sequence + 1,
                terminal=False,
            ),
            self._event(updated, event_type, payload, sequence=sequence + 2, terminal=True),
        )
        run = replace(
            run_record.value,
            status=run_status,
            event_sequence=sequence + 2,
            updated_at=now,
            termination_reason=termination,
        )
        async with self._unit_of_work.begin() as uow:
            await uow.entities.put("subagent_runs", run_id, run_record_to_value(updated), expected_revision=revision)
            await uow.entities.put("runs", run_id, run, expected_revision=run_record.revision)
            existing_result = await uow.entities.get("subagent_results", run_id)
            if existing_result is None:
                await uow.entities.put("subagent_results", run_id, result_to_value(result), expected_revision=0)
            budget_raw = await uow.entities.get("subagent_budget_reservations", run_id)
            if isinstance(budget_raw, Mapping) and budget_raw.get("state") == "reserved":
                await uow.entities.put(
                    "subagent_budget_reservations",
                    run_id,
                    {"schemaVersion": 1, "state": "settled", "usage": usage_to_value(usage)},
                    expected_revision=1,
                )
            stored = await uow.events.append(run_id, sequence, events)
            await uow.commit()
        reservation = self._reservations.pop(run_id, None)
        if reservation is not None:
            await reservation.settle(usage)
        await self._publish(stored)
        await self._notify_status()

    async def _checkpoint_usage(self, run_id: str) -> AgentUsage:
        async with self._unit_of_work.begin() as uow:
            state = await uow.entities.get("run_states", run_id)
        if not isinstance(state, RunState) or state.budget_checkpoint is None:
            return AgentUsage()
        checkpoint = state.budget_checkpoint
        return AgentUsage(
            input_tokens=checkpoint.used.input_tokens,
            output_tokens=checkpoint.used.output_tokens,
            model_calls=checkpoint.used.model_rounds,
            tool_calls=checkpoint.used.tool_calls,
            wall_time_seconds=checkpoint.elapsed_seconds,
            artifact_bytes=checkpoint.used.artifact_bytes,
            child_count=checkpoint.used.subagents,
            cost_micros=int(checkpoint.used.cost * 1_000_000),
        )

    async def _mark_cancel_requested(self, record: SubagentRunRecord, reason: str, cascade: bool) -> None:
        async with self._lock:
            await self._mark_cancel_requested_locked(record, reason, cascade)

    async def _mark_cancel_requested_locked(
        self,
        record: SubagentRunRecord,
        reason: str,
        cascade: bool,
    ) -> None:
        current, revision = await self._record_with_revision(record.run_id)
        if current.status.terminal or current.status is SubagentRunStatus.CANCEL_REQUESTED:
            return
        stream_id = f"subagent-control:{record.run_id}"
        sequence = await self._latest_sequence(stream_id)
        updated = replace(
            current,
            status=SubagentRunStatus.CANCEL_REQUESTED,
            phase="cancel_requested",
            updated_at=self._clock.utcnow(),
            revision=current.revision + 1,
        )
        event = self._event(
            updated,
            "subagent.cancel_requested",
            {"childRunId": current.run_id, "reason": reason, "cascade": cascade},
            sequence=sequence + 1,
            terminal=False,
        )
        async with self._unit_of_work.begin() as uow:
            await uow.entities.put(
                "subagent_runs", current.run_id, run_record_to_value(updated), expected_revision=revision
            )
            stored = await uow.events.append(stream_id, sequence, (event,))
            await uow.commit()
        await self._publish(stored)

    async def _append_message_event(self, record: SubagentRunRecord, message: MailboxMessage) -> None:
        stream_id = f"subagent-control:{record.run_id}"
        sequence = await self._latest_sequence(stream_id)
        event = self._event(
            record,
            "subagent.message_received",
            {
                "childRunId": record.run_id,
                "messageId": message.message_id,
                "mode": message.mode.value,
                "content": [{"type": "text", "text": message.message, "format": "plain", "references": []}],
            },
            sequence=sequence + 1,
            terminal=False,
        )
        async with self._unit_of_work.begin() as uow:
            stored = await uow.events.append(stream_id, sequence, (event,))
            await uow.commit()
        await self._publish(stored)

    async def _spawn_replay(self, receipt_id: str, request_hash: str) -> SubagentHandle | None:
        async with self._unit_of_work.begin() as uow:
            raw = await uow.entities.get("subagent_spawn_receipts", receipt_id)
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or raw.get("schemaVersion") != 1 or raw.get("requestHash") != request_hash:
            raise SubagentServiceError("subagent_idempotency_conflict", "spawnCallId is bound to another request")
        run_id = raw.get("runId")
        if not isinstance(run_id, str):
            raise SubagentServiceError("subagent_corrupt", "spawn receipt is corrupt")
        record, _ = await self._record_with_revision(run_id)
        return SubagentHandle(record.run_id, record.parent_run_id, record.status, record.created_at)

    def _validate_parent(self, authority: Any, command: AgentSpawnCommand) -> None:
        if authority.workspace_id != self.workspace_id or authority.lineage.run_id != command.parent_run_id:
            raise SubagentServiceError("subagent_workspace_mismatch", "parent Run belongs to another Workspace")
        if not authority.active:
            raise SubagentServiceError("subagent_parent_terminal", "parent Run is no longer active")

    async def _check_turn_and_duplicate(self, turn_id: str, root_run_id: str, command: AgentSpawnCommand) -> None:
        records = await self._all_records()
        turn_records = [item for item in records if item.turn_id == turn_id]
        if len(turn_records) >= self._max_per_turn:
            raise SubagentServiceError("subagent_turn_limit", "per-Turn Subagent count limit exceeded")
        fingerprint = _task_fingerprint(command.task)
        duplicate = next(
            (
                item
                for item in records
                if item.root_run_id == root_run_id
                and item.task_fingerprint == fingerprint
                and item.parent_run_id == command.parent_run_id
            ),
            None,
        )
        if duplicate is not None:
            qualifier = "active " if duplicate.status in _ACTIVE else "repeated "
            raise SubagentServiceError(
                "subagent_duplicate_task",
                f"duplicate {qualifier}Subagent task fingerprint was rejected",
            )

    async def _managed_record(self, requester_run_id: str, run_id: str) -> SubagentRunRecord:
        record, _ = await self._record_with_revision(run_id)
        if requester_run_id not in record.ancestor_run_ids:
            raise SubagentServiceError("subagent_lineage_denied", "requester does not manage this descendant Run")
        return record

    async def _record_with_revision(self, run_id: str) -> tuple[SubagentRunRecord, int]:
        record = await _find_entity_record(self._unit_of_work, "subagent_runs", run_id)
        if record is None:
            raise SubagentServiceError("subagent_not_found", f"Subagent Run {run_id!r} does not exist")
        try:
            return run_record_from_value(record.value), record.revision
        except (TypeError, ValueError) as error:
            raise SubagentServiceError("subagent_corrupt", "persisted Subagent Run is corrupt") from error

    async def _all_records(self) -> tuple[SubagentRunRecord, ...]:
        output: list[SubagentRunRecord] = []
        after: str | None = None
        while True:
            async with self._unit_of_work.begin() as uow:
                page = await uow.entities.list("subagent_runs", after_id=after, limit=256)
            if not page:
                break
            output.extend(run_record_from_value(item.value) for item in page)
            after = page[-1].entity_id
        return tuple(output)

    async def _children(self, parent_run_id: str) -> tuple[SubagentRunRecord, ...]:
        return tuple(item for item in await self._all_records() if item.parent_run_id == parent_run_id)

    async def _descendants(self, parent_run_id: str) -> tuple[SubagentRunRecord, ...]:
        return tuple(item for item in await self._all_records() if parent_run_id in item.ancestor_run_ids)

    async def _context(self, context_id: str) -> Any:
        async with self._unit_of_work.begin() as uow:
            raw = await uow.entities.get("subagent_contexts", context_id)
        if raw is None:
            raise SubagentServiceError("subagent_context_missing", "child context snapshot is missing")
        return context_from_value(raw)

    async def _run(self, run_id: str) -> Run:
        async with self._unit_of_work.begin() as uow:
            value = await uow.entities.get("runs", run_id)
        if not isinstance(value, Run):
            raise SubagentServiceError("subagent_corrupt", "authoritative child Run is missing")
        return value

    async def _latest_sequence(self, run_id: str) -> int:
        async with self._unit_of_work.begin() as uow:
            return await uow.events.latest_sequence(run_id)

    def _event(
        self,
        record: SubagentRunRecord,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        sequence: int,
        terminal: bool,
    ) -> NewEvent:
        return self._event_factory.make(
            event_type=event_type,
            payload=payload,
            trace_id=record.trace_id,
            workspace_id=record.workspace_id,
            session_id=record.session_id,
            turn_id=record.turn_id,
            run_id=record.run_id,
            root_run_id=record.root_run_id,
            parent_run_id=record.parent_run_id,
            state_revision=record.revision,
            sequence=sequence,
            occurred_at=self._clock.utcnow(),
            terminal=terminal,
        )

    def _terminal_event(
        self, record: SubagentRunRecord, result: SubagentResult, metadata: Any, sequence: int
    ) -> NewEvent:
        payload: dict[str, Any] = {
            "result": {
                "runId": result.run_id,
                "status": "completed" if record.status is SubagentRunStatus.COMPLETED else "failed",
                "summary": result.summary,
                "findings": [],
                "artifacts": [_artifact_ref(metadata)],
                "proposedActions": [],
                "unresolvedQuestions": list(result.unresolved_questions),
                "usage": _protocol_usage(record.budget_used),
                "error": None
                if record.status is SubagentRunStatus.COMPLETED
                else _protocol_error("child_result_failed"),
            }
        }
        event_type = "subagent.completed" if record.status is SubagentRunStatus.COMPLETED else "subagent.failed"
        if event_type == "subagent.failed":
            payload = {
                "childRunId": record.run_id,
                "error": _protocol_error("child_result_failed"),
                "usage": _protocol_usage(record.budget_used),
            }
        return self._event(record, event_type, payload, sequence=sequence, terminal=True)

    async def _publish(self, events: Any) -> None:
        try:
            await self._event_sink.publish(events)
        except Exception as error:
            self.delivery_failures.append(f"{type(error).__name__}: {error}")

    async def _notify_status(self) -> None:
        async with self._condition:
            self._status_generation += 1
            self._condition.notify_all()

    async def _wait_for_status_change(self, generation: int) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._status_generation != generation)


class _NeverCancelled:
    cancelled = False
    reason = None

    async def wait(self) -> Any:
        await asyncio.Future()

    def checkpoint(self) -> None:
        return


async def _find_entity_record(
    unit_of_work: UnitOfWorkFactory,
    collection: str,
    entity_id: str,
) -> EntityRecord | None:
    after: str | None = None
    while True:
        async with unit_of_work.begin() as uow:
            page = await uow.entities.list(collection, after_id=after, limit=256)
        if not page:
            return None
        for record in page:
            if record.entity_id == entity_id:
                return record
            if record.entity_id > entity_id:
                return None
        after = page[-1].entity_id


def _spawn_identity(command: AgentSpawnCommand) -> dict[str, Any]:
    return {
        "parentRunId": command.parent_run_id,
        "spawnCallId": command.spawn_call_id,
        "task": command.task.strip(),
        "profile": command.profile,
        "contextMode": command.context_mode.value,
        "selectedMessageIds": list(command.selected_message_ids),
        "selectedArtifactIds": list(command.selected_artifact_ids),
        "requestedTools": sorted(command.requested_scope.allowed_tools),
        "requestedPermission": command.requested_permission_mode.value,
        "requestedVersions": command.requested_tool_versions,
        "requestedConstraints": command.requested_tool_constraints,
        "budget": command.budget.as_tuple(),
        "lifetime": command.lifetime.value,
        "priority": command.priority.value,
        "deadline": None if command.deadline_at is None else command.deadline_at.isoformat(),
    }


def _task_fingerprint(task: str) -> str:
    return f"sha256:{hashlib.sha256(task.strip().encode()).hexdigest()}"


def _protocol_budget(record: SubagentRunRecord) -> dict[str, Any]:
    value = record.budget_limit
    return {
        "maxModelRounds": value.model_calls,
        "maxToolCalls": value.tool_calls,
        "maxParallelReads": max(1, min(256, value.tool_calls or 1)),
        "maxWallTimeMs": max(1, int(value.wall_time_seconds * 1000)),
        "maxInputTokens": max(1, value.input_tokens),
        "maxOutputTokens": max(1, value.output_tokens),
        "maxCostMicros": value.cost_micros,
        "maxArtifactBytes": value.artifact_bytes,
    }


def _protocol_usage(value: AgentUsage) -> dict[str, Any]:
    return {
        "inputTokens": value.input_tokens,
        "outputTokens": value.output_tokens,
        "cachedInputTokens": 0,
        "reasoningTokens": 0,
        "modelCalls": value.model_calls,
        "toolCalls": value.tool_calls,
        "costMicros": value.cost_micros,
        "wallTimeMs": int(value.wall_time_seconds * 1000),
    }


def _protocol_error(code: str) -> dict[str, Any]:
    return {
        "code": "internal.error",
        "retryable": False,
        "cancelled": False,
        "userVisibleMessage": "Subagent failed",
        "details": {"subagentCode": code},
        "retryAfterMs": None,
        "traceId": None,
    }


def _artifact_ref(metadata: Any) -> dict[str, Any]:
    return {
        "artifactId": metadata.artifact_id,
        "contentHash": metadata.sha256,
        "mediaType": metadata.mime_type,
        "sizeBytes": metadata.byte_length,
        "sensitivity": metadata.sensitivity.value,
        "state": metadata.state.value,
        "title": "Subagent structured result",
    }


def _usage_from_result(result: SubagentResult) -> AgentUsage:
    raw = result.usage
    return AgentUsage(
        input_tokens=_usage_int(raw, "inputTokens"),
        output_tokens=_usage_int(raw, "outputTokens"),
        model_calls=_usage_int(raw, "modelCalls"),
        tool_calls=_usage_int(raw, "toolCalls"),
        wall_time_seconds=_usage_number(raw, "wallTimeSeconds"),
        artifact_bytes=_usage_int(raw, "artifactBytes"),
        child_count=_usage_int(raw, "childCount"),
        cost_micros=_usage_int(raw, "costMicros"),
    )


def _usage_int(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key, 0)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise SubagentServiceError("subagent_usage_corrupt", f"Subagent usage {key} is invalid")
    return int(item)


def _usage_number(value: Mapping[str, Any], key: str) -> float:
    item = value.get(key, 0)
    if isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0:
        raise SubagentServiceError("subagent_usage_corrupt", f"Subagent usage {key} is invalid")
    return float(item)


def _safe_to_resume(record: SubagentRunRecord, state: RunState | None) -> bool:
    safe_phases = {
        "created",
        "queued",
        "starting",
        "loading_context",
        "selecting_memory",
        "planning",
        "recording_results",
    }
    return (
        state is not None
        and state.phase.value in safe_phases
        and state.pending.empty
        and record.effective_scope.allowed_risks <= {RiskClass.READ}
        and not record.effective_scope.allow_network
    )


def _status_for_phase(phase: str, fallback: SubagentRunStatus) -> SubagentRunStatus:
    if phase == "awaiting_approval":
        return SubagentRunStatus.WAITING_APPROVAL
    if phase == "executing_tools":
        return SubagentRunStatus.WAITING_TOOL
    if phase in {"composing", "persisting", "recording_results"}:
        return SubagentRunStatus.COMPLETING
    if phase == "cancelling":
        return SubagentRunStatus.CANCEL_REQUESTED
    if phase in {
        "loading_context",
        "selecting_memory",
        "planning",
        "validating_calls",
        "checking_policy",
    }:
        return SubagentRunStatus.RUNNING
    return fallback


__all__ = ["SubagentService", "SubagentServiceError"]
