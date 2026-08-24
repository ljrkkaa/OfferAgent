"""Durable Session compaction and active-Run steering commands."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from offeragent_harness.agent.state import RunControlMessage
from offeragent_harness.ports import (
    ArtifactMetadata,
    CancellationToken,
    Clock,
    EntityRecord,
    EntityStore,
    EventSink,
    IdGenerator,
    NewEvent,
    StoredEvent,
    UnitOfWorkFactory,
)
from offeragent_harness.protocol.content import ArtifactRef, ArtifactSensitivity
from offeragent_harness.protocol.content import ArtifactState as WireArtifactState
from offeragent_harness.protocol.events import ContextCompactedPayload, make_domain_event_record
from offeragent_harness.protocol.messages import SessionCompactResult, TurnSteerResult
from offeragent_harness.sessions import Run, Session, SessionStatus, Turn
from offeragent_harness.tools import canonical_json_sha256

from .turn_manager import TurnManager

SESSION_COMPACTION_COLLECTION = "session_compactions"
RUN_CONTROL_COLLECTION = "run_control_messages"


class ConversationControlError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CompactionExecution:
    summary_artifact: ArtifactMetadata
    replaced_turn_count: int
    replaced_sequence_start: int
    replaced_sequence_end: int
    model: str
    summary_id: str | None = None
    trigger: str = "manual"
    estimated_before_tokens: int = 0
    estimated_after_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        if self.replaced_turn_count < 1 or self.replaced_sequence_start < 1:
            raise ValueError("compaction execution must replace a non-empty durable range")
        if self.replaced_sequence_end < self.replaced_sequence_start or not self.model:
            raise ValueError("compaction execution sequence/model is invalid")
        if self.trigger not in {"manual", "auto", "hard_limit"}:
            raise ValueError("compaction execution trigger is invalid")
        if (
            min(
                self.estimated_before_tokens,
                self.estimated_after_tokens,
                self.input_tokens,
                self.output_tokens,
                self.cached_input_tokens,
            )
            < 0
        ):
            raise ValueError("compaction execution usage cannot be negative")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("compaction cached input cannot exceed input usage")


class SessionCompactionRunner(Protocol):
    """Adapter that persists one lossless context boundary for a durable event batch."""

    async def compact(
        self,
        *,
        workspace_id: str,
        session_id: str,
        through_turn_id: str,
        selected_run: Run,
        events: Sequence[StoredEvent],
        force: bool,
        cancellation: CancellationToken,
    ) -> CompactionExecution: ...


class ConversationControlService:
    def __init__(
        self,
        *,
        workspace_id: str,
        unit_of_work: UnitOfWorkFactory,
        event_sink: EventSink,
        clock: Clock,
        ids: IdGenerator,
        turn_manager: TurnManager,
        compaction_runner: SessionCompactionRunner,
    ) -> None:
        if not workspace_id:
            raise ValueError("Conversation controls require a Workspace")
        self._workspace_id = workspace_id
        self._unit_of_work = unit_of_work
        self._event_sink = event_sink
        self._clock = clock
        self._ids = ids
        self._turn_manager = turn_manager
        self._compaction_runner = compaction_runner

    async def compact(
        self,
        *,
        session_id: str,
        through_turn_id: str | None,
        force: bool,
        cancellation: CancellationToken,
    ) -> SessionCompactResult:
        cancellation.checkpoint()
        session, turns, runs = await self._load_session_graph(session_id)
        if session.workspace_id != self._workspace_id or session.status is not SessionStatus.ACTIVE:
            raise ConversationControlError("Session is outside this Workspace or is not active")
        ordered = sorted(turns, key=lambda item: item.ordinal)
        if not ordered:
            return SessionCompactResult(
                session_id=session_id,
                compacted=False,
                boundary_artifact=None,
                replaced_turn_count=0,
            )
        boundary_turn: Turn | None = None
        if through_turn_id is None:
            boundary_turn = ordered[-1]
        else:
            for item in ordered:
                if item.turn_id == through_turn_id:
                    boundary_turn = item
                    break
        if boundary_turn is None:
            raise ConversationControlError("compaction boundary Turn does not exist in this Session")
        included_turns = tuple(item for item in ordered if item.ordinal <= boundary_turn.ordinal)
        selected_run = self._selected_run(boundary_turn, runs)
        events = await self._read_all_events(selected_run.run_id)
        if not events:
            raise ConversationControlError("selected Run has no durable events to compact")
        request_hash = canonical_json_sha256(
            {
                "workspaceId": self._workspace_id,
                "sessionId": session_id,
                "throughTurnId": boundary_turn.turn_id,
                "selectedRunId": selected_run.run_id,
                "selectedSequence": events[-1].sequence,
                "force": force,
            }
        )
        key = f"{session_id}:{boundary_turn.turn_id}:{request_hash.removeprefix('sha256:')[:24]}"
        async with self._unit_of_work.begin() as uow:
            existing = await uow.entities.get(SESSION_COMPACTION_COLLECTION, key)
        if isinstance(existing, SessionCompactResult):
            return existing
        execution = await self._compaction_runner.compact(
            workspace_id=self._workspace_id,
            session_id=session_id,
            through_turn_id=boundary_turn.turn_id,
            selected_run=selected_run,
            events=events,
            force=force,
            cancellation=cancellation,
        )
        artifact = _artifact_ref(execution.summary_artifact)
        result = SessionCompactResult(
            session_id=session_id,
            compacted=True,
            boundary_artifact=artifact,
            replaced_turn_count=min(len(included_turns), execution.replaced_turn_count),
            summary_id=execution.summary_id,
            model=execution.model,
            trigger="manual",
            estimated_before_tokens=execution.estimated_before_tokens,
            estimated_after_tokens=execution.estimated_after_tokens,
            input_tokens=execution.input_tokens,
            output_tokens=execution.output_tokens,
            cached_input_tokens=execution.cached_input_tokens,
        )
        stream_id = f"session-{session_id}-compaction"
        async with self._unit_of_work.begin() as uow:
            if await uow.entities.get(SESSION_COMPACTION_COLLECTION, key) is not None:
                replay = await uow.entities.get(SESSION_COMPACTION_COLLECTION, key)
                if isinstance(replay, SessionCompactResult):
                    return replay
                raise ConversationControlError("compaction receipt is corrupt")
            sequence = await uow.events.latest_sequence(stream_id)
            boundary_id = f"cmp_{hashlib.sha256(key.encode()).hexdigest()[:24]}"
            record = make_domain_event_record(
                event_type="context.compacted",
                payload=ContextCompactedPayload(
                    boundary_id=boundary_id,
                    replaced_sequence_start=execution.replaced_sequence_start,
                    replaced_sequence_end=execution.replaced_sequence_end,
                    summary_artifact=artifact,
                    model=execution.model,
                ),
                trace_id=f"trace_{hashlib.sha256((key + ':trace').encode()).hexdigest()[:24]}",
                workspace_id=self._workspace_id,
                session_id=session_id,
                turn_id=boundary_turn.turn_id,
                run_id=None,
                root_run_id=None,
                parent_run_id=None,
                state_revision=session.revision,
            )
            stored = await uow.events.append(
                stream_id,
                sequence,
                (
                    NewEvent(
                        event_id=self._ids.new_id("evt"),
                        event_type="context.compacted",
                        payload=record.to_wire(),
                        occurred_at=self._clock.utcnow(),
                        terminal=False,
                        idempotency_key=f"context.compacted:{key}",
                    ),
                ),
            )
            await uow.entities.put(SESSION_COMPACTION_COLLECTION, key, result, expected_revision=0)
            await uow.commit()
        await self._event_sink.publish(stored)
        return result

    async def steer(
        self,
        *,
        run_id: str,
        message_id: str,
        input_blocks: tuple[dict[str, object], ...],
        mode: str,
        cancellation: CancellationToken,
    ) -> TurnSteerResult:
        cancellation.checkpoint()
        active = await self._turn_manager.get(run_id)
        if active is None or active.task.done():
            raise ConversationControlError("Run is not active")
        async with self._unit_of_work.begin() as uow:
            run = await uow.entities.get("runs", run_id)
            state = await uow.entities.get("run_states", run_id)
            existing = await uow.entities.get(RUN_CONTROL_COLLECTION, f"{run_id}:{message_id}")
            sequence = await uow.events.latest_sequence(run_id)
        if not isinstance(run, Run) or run.workspace_id != self._workspace_id or state is None:
            raise ConversationControlError("Run does not belong to this Workspace")
        if isinstance(existing, TurnSteerResult):
            return existing
        message = RunControlMessage(message_id, input_blocks, mode, sequence)
        result = TurnSteerResult(run_id=run_id, accepted=True, apply_after_sequence=sequence)
        async with self._unit_of_work.begin() as uow:
            await uow.entities.put(
                RUN_CONTROL_COLLECTION,
                f"{run_id}:{message_id}",
                result,
                expected_revision=0,
            )
            await uow.commit()
        accepted = await self._turn_manager.steer(run_id, message)
        if not accepted:
            raise ConversationControlError("Run left the active safe-point queue before steering")
        return result

    async def _load_session_graph(self, session_id: str) -> tuple[Session, tuple[Turn, ...], tuple[Run, ...]]:
        async with self._unit_of_work.begin() as uow:
            session = await uow.entities.get("sessions", session_id)
            turns = await _list_values(uow.entities, "turns")
            runs = await _list_values(uow.entities, "runs")
        if not isinstance(session, Session):
            raise ConversationControlError("Session does not exist")
        return (
            session,
            tuple(item for item in turns if isinstance(item, Turn) and item.session_id == session_id),
            tuple(item for item in runs if isinstance(item, Run) and item.session_id == session_id),
        )

    @staticmethod
    def _selected_run(turn: Turn, runs: Sequence[Run]) -> Run:
        candidates = [item for item in runs if item.turn_id == turn.turn_id]
        if not candidates:
            raise ConversationControlError("Turn has no durable Run")
        return max(candidates, key=lambda item: (item.attempt, item.created_at, item.run_id))

    async def _read_all_events(self, run_id: str) -> tuple[StoredEvent, ...]:
        output: list[StoredEvent] = []
        after = 0
        while True:
            async with self._unit_of_work.begin() as uow:
                page = await uow.events.read(run_id, after_sequence=after, limit=1_000)
            output.extend(page)
            if len(page) < 1_000:
                return tuple(output)
            after = page[-1].sequence


async def _list_values(store: EntityStore, collection: str) -> tuple[object, ...]:
    values: list[object] = []
    after_id: str | None = None
    while True:
        records = await store.list(collection, after_id=after_id, limit=500)
        values.extend(record.value for record in records if isinstance(record, EntityRecord))
        if len(records) < 500:
            return tuple(values)
        after_id = records[-1].entity_id


def _artifact_ref(metadata: ArtifactMetadata) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=metadata.artifact_id,
        content_hash=metadata.sha256,
        media_type=metadata.mime_type,
        size_bytes=metadata.byte_length,
        sensitivity=ArtifactSensitivity(metadata.sensitivity.value),
        state=WireArtifactState(metadata.state.value),
    )


__all__ = [
    "RUN_CONTROL_COLLECTION",
    "SESSION_COMPACTION_COLLECTION",
    "CompactionExecution",
    "ConversationControlError",
    "ConversationControlService",
    "SessionCompactionRunner",
]
