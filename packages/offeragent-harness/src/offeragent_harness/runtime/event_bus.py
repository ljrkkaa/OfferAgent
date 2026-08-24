from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from offeragent_harness.agent import BudgetCheckpoint, BudgetLedger
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.ports import (
    Clock,
    EntityRecord,
    EntityStore,
    EventSink,
    IdGenerator,
    NewEvent,
    StoredEvent,
    UnitOfWorkFactory,
)
from offeragent_harness.protocol.events import make_domain_event_record
from offeragent_harness.sessions import Run, RunStatus, TerminationReason, Turn, TurnStatus


@dataclass(frozen=True, slots=True)
class DeliveryFailure:
    event_ids: tuple[str, ...]
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class AtomicEntityWrite:
    """An entity fact committed atomically with one RunState/event update."""

    collection: str
    entity_id: str
    value: Any
    expected_revision: int = 0

    def __post_init__(self) -> None:
        if not self.collection or not self.entity_id or self.expected_revision < 0:
            raise ValueError("atomic entity write identity/revision is invalid")


class UowRunRecorder:
    """Atomically stores RunState and its semantic event, then best-effort publishes.

    EventSink delivery is deliberately outside the Unit of Work. A slow or
    disconnected UI cannot roll back committed state or block replay authority.
    """

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        event_sink: EventSink,
        clock: Clock,
        ids: IdGenerator,
        run_id: str,
        trace_id: str,
        budget: BudgetLedger | None = None,
        expected_entity_revision: int,
        expected_event_sequence: int,
        expected_run_revision: int | None = None,
        terminal_entity_writes: Sequence[AtomicEntityWrite] = (),
    ) -> None:
        if expected_entity_revision < 0 or expected_event_sequence < 0:
            raise ValueError("expected revisions cannot be negative")
        self._unit_of_work = unit_of_work
        self._event_sink = event_sink
        self._clock = clock
        self._ids = ids
        self._run_id = run_id
        self._trace_id = trace_id
        self._budget = budget
        self._entity_revision = expected_entity_revision
        self._event_sequence = expected_event_sequence
        self._run_revision = expected_run_revision
        self._terminal_entity_writes = tuple(terminal_entity_writes)
        identities = tuple((write.collection, write.entity_id) for write in self._terminal_entity_writes)
        if len(identities) != len(set(identities)):
            raise ValueError("terminal atomic entity writes contain duplicate identities")
        self._lock = asyncio.Lock()
        self.delivery_failures: list[DeliveryFailure] = []

    @property
    def entity_revision(self) -> int:
        return self._entity_revision

    @property
    def event_sequence(self) -> int:
        return self._event_sequence

    async def commit(
        self,
        state: RunState,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        terminal: bool = False,
        entity_writes: Sequence[AtomicEntityWrite] = (),
    ) -> None:
        if state.run_id != self._run_id:
            raise ValueError("recorder cannot commit a different run")
        if terminal != state.phase.terminal:
            raise ValueError("terminal event flag must agree with RunState phase")
        stream_terminal = terminal and state.lineage.depth == 0
        terminal_writes = self._terminal_entity_writes if stream_terminal and event_type == "turn.completed" else ()
        combined_writes = (*entity_writes, *terminal_writes)
        write_identities = tuple((write.collection, write.entity_id) for write in combined_writes)
        if len(write_identities) != len(set(write_identities)):
            raise ValueError("atomic entity writes contain duplicate identities")

        async with self._lock:
            next_sequence = self._event_sequence + 1
            occurred_at = self._clock.utcnow()
            persisted_state = state
            if self._budget is not None:
                persisted_state = replace(
                    state,
                    budget_checkpoint=await BudgetCheckpoint.capture(self._budget, now=occurred_at),
                )
            record = make_domain_event_record(
                event_type=event_type,
                payload=payload,
                trace_id=self._trace_id,
                workspace_id=persisted_state.workspace_id,
                session_id=persisted_state.session_id,
                turn_id=persisted_state.turn_id,
                run_id=persisted_state.run_id,
                root_run_id=persisted_state.lineage.root_run_id,
                parent_run_id=persisted_state.lineage.parent_run_id,
                state_revision=persisted_state.revision,
            )
            record_bytes = json.dumps(
                record.to_wire(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            record_digest = hashlib.sha256(record_bytes).hexdigest()
            identity = f"{self._run_id}:{next_sequence}:{event_type}:{record_digest}".encode()
            event_id = f"evt_{hashlib.sha256(identity).hexdigest()}"
            event = NewEvent(
                event_id=event_id,
                event_type=event_type,
                payload=record.to_wire(),
                occurred_at=occurred_at,
                terminal=stream_terminal,
                idempotency_key=f"{self._run_id}:{next_sequence}:{event_type}",
            )
            updated_run: Run | None = None
            updated_turn: Turn | None = None
            entity_revision = self._entity_revision
            run_revision = self._run_revision
            committed_entity_writes: tuple[tuple[AtomicEntityWrite, int], ...] = ()
            deferred_error: BaseException | None = None
            try:
                async with self._unit_of_work.begin() as uow:
                    entity_revision = await uow.entities.put(
                        "run_states",
                        self._run_id,
                        persisted_state,
                        expected_revision=self._entity_revision,
                    )
                    if run_revision is not None:
                        run = await uow.entities.get("runs", self._run_id)
                        if not isinstance(run, Run):
                            raise RuntimeError(f"authoritative Run record {self._run_id!r} is missing or corrupt")
                        updated_run = _project_run(
                            run,
                            persisted_state,
                            event_sequence=next_sequence,
                            event_type=event_type,
                            payload=payload,
                            updated_at=occurred_at,
                        )
                        run_revision = await uow.entities.put(
                            "runs",
                            self._run_id,
                            updated_run,
                            expected_revision=run_revision,
                        )
                    write_revisions: list[tuple[AtomicEntityWrite, int]] = []
                    for write in combined_writes:
                        revision = await uow.entities.put(
                            write.collection,
                            write.entity_id,
                            write.value,
                            expected_revision=write.expected_revision,
                        )
                        write_revisions.append((write, revision))
                    committed_entity_writes = tuple(write_revisions)
                    stored = await uow.events.append(self._run_id, self._event_sequence, (event,))
                    if stream_terminal:
                        turn = await uow.entities.get("turns", persisted_state.turn_id)
                        if not isinstance(turn, Turn):
                            raise RuntimeError(
                                f"authoritative Turn record {persisted_state.turn_id!r} is missing or corrupt"
                            )
                        status_by_phase = {
                            RunPhase.COMPLETED: TurnStatus.COMPLETED,
                            RunPhase.CANCELLED: TurnStatus.CANCELLED,
                            RunPhase.FAILED: TurnStatus.FAILED,
                            RunPhase.INTERRUPTED: TurnStatus.INTERRUPTED,
                        }
                        updated_turn = replace(
                            turn,
                            status=status_by_phase[persisted_state.phase],
                            updated_at=occurred_at,
                            revision=turn.revision + 1,
                        )
                        await uow.entities.put(
                            "turns",
                            persisted_state.turn_id,
                            updated_turn,
                            expected_revision=turn.revision,
                        )
                        lease_record = await _find_entity_record(
                            uow.entities,
                            "active_root_runs",
                            persisted_state.session_id,
                        )
                        if not isinstance(lease_record, EntityRecord):
                            raise RuntimeError("active root Run lease is missing")
                        lease = lease_record.value
                        if not isinstance(lease, Mapping) or lease.get("runId") != persisted_state.run_id:
                            raise RuntimeError("active root Run lease is missing, corrupt, or owned by another Run")
                        await uow.entities.delete(
                            "active_root_runs",
                            persisted_state.session_id,
                            expected_revision=lease_record.revision,
                        )
                    await uow.commit()
            except BaseException as error:
                recovered = await self._recover_commit_ack(
                    state=persisted_state,
                    event=event,
                    expected_entity_revision=entity_revision,
                    expected_run=updated_run,
                    expected_run_revision=run_revision,
                    expected_turn=updated_turn,
                    terminal=stream_terminal,
                    entity_writes=committed_entity_writes,
                )
                if recovered is None:
                    raise
                stored = recovered
                if not isinstance(error, Exception):
                    deferred_error = error

            self._entity_revision = entity_revision
            self._run_revision = run_revision
            self._event_sequence = stored[-1].sequence

            if deferred_error is not None:
                raise deferred_error

        try:
            await self._event_sink.publish(stored)
        except Exception as error:
            self.delivery_failures.append(
                DeliveryFailure(
                    event_ids=tuple(item.event_id for item in stored),
                    error_type=type(error).__name__,
                    message=str(error),
                )
            )

    async def _recover_commit_ack(
        self,
        *,
        state: RunState,
        event: NewEvent,
        expected_entity_revision: int,
        expected_run: Run | None,
        expected_run_revision: int | None,
        expected_turn: Turn | None,
        terminal: bool,
        entity_writes: Sequence[tuple[AtomicEntityWrite, int]],
    ) -> tuple[StoredEvent, ...] | None:
        async with self._unit_of_work.begin() as uow:
            events = await uow.events.read(
                self._run_id,
                after_sequence=self._event_sequence,
                limit=2,
            )
            state_record = await _find_entity_record(uow.entities, "run_states", self._run_id)
            run_record = (
                None if expected_run_revision is None else await _find_entity_record(uow.entities, "runs", self._run_id)
            )
            turn_record = (
                None if expected_turn is None else await _find_entity_record(uow.entities, "turns", state.turn_id)
            )
            lease_record = (
                await _find_entity_record(uow.entities, "active_root_runs", state.session_id) if terminal else None
            )
            entity_record_items: list[EntityRecord | None] = []
            for write, _ in entity_writes:
                entity_record_items.append(await _find_entity_record(uow.entities, write.collection, write.entity_id))
            entity_records = tuple(entity_record_items)
        if len(events) != 1 or not _stored_matches(events[0], event):
            return None
        if state_record is None or state_record.revision != expected_entity_revision or state_record.value != state:
            return None
        if expected_run_revision is not None and (
            run_record is None or run_record.revision != expected_run_revision or run_record.value != expected_run
        ):
            return None
        if expected_turn is not None and (turn_record is None or turn_record.value != expected_turn):
            return None
        if terminal and lease_record is not None:
            return None
        if any(
            record is None or record.revision != revision or record.value != write.value
            for (write, revision), record in zip(entity_writes, entity_records, strict=True)
        ):
            return None
        return events


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


def _stored_matches(stored: StoredEvent, candidate: NewEvent) -> bool:
    return (
        stored.sequence >= 1
        and stored.event_id == candidate.event_id
        and stored.event_type == candidate.event_type
        and stored.payload == candidate.payload
        and stored.occurred_at == candidate.occurred_at
        and stored.terminal == candidate.terminal
        and stored.idempotency_key == candidate.idempotency_key
    )


def _project_run(
    run: Run,
    state: RunState,
    *,
    event_sequence: int,
    event_type: str,
    payload: Mapping[str, Any],
    updated_at: datetime,
) -> Run:
    status = RunStatus(state.phase.value)
    termination_reason: TerminationReason | None = None
    if status.is_terminal:
        termination_reason = _termination_reason(event_type, payload)
    return replace(
        run,
        status=status,
        event_sequence=event_sequence,
        updated_at=updated_at,
        termination_reason=termination_reason,
    )


def _termination_reason(event_type: str, payload: Mapping[str, Any]) -> TerminationReason:
    if event_type == "turn.completed":
        reason = payload.get("reason")
        if not isinstance(reason, str):
            raise ValueError("turn.completed requires a typed termination reason")
        return TerminationReason(reason)
    if event_type == "turn.interrupted":
        return TerminationReason.RUNTIME_INTERRUPTED
    if event_type == "turn.cancelled":
        code = payload.get("code")
        if code == "user":
            return TerminationReason.CANCELLED_BY_USER
        if code == "deadline":
            return TerminationReason.BUDGET_EXHAUSTED
        return TerminationReason.RUNTIME_INTERRUPTED
    if event_type == "turn.failed":
        error = payload.get("error")
        details = error.get("details") if isinstance(error, Mapping) else None
        if not isinstance(details, Mapping):
            raise ValueError("turn.failed requires typed failure details")
        category = details.get("failureCategory")
        reasons = {
            "budget": TerminationReason.BUDGET_EXHAUSTED,
            "tool": TerminationReason.TOOL_ERROR,
            "model": TerminationReason.MODEL_ERROR,
            "document_ingestion": TerminationReason.RUNTIME_INTERRUPTED,
            "runtime": TerminationReason.RUNTIME_INTERRUPTED,
        }
        if not isinstance(category, str) or category not in reasons:
            raise ValueError("turn.failed requires a supported failureCategory")
        return reasons[category]
    raise ValueError(f"unsupported terminal event type {event_type!r}")


__all__ = ["AtomicEntityWrite", "DeliveryFailure", "UowRunRecorder"]
