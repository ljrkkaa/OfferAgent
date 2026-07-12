from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from offeragent_harness.agent import BudgetLedger, RunBudget
from offeragent_harness.agent.composer import Composer
from offeragent_harness.agent.loop import ToolKernel, run_agent_loop
from offeragent_harness.agent.planner import Planner
from offeragent_harness.agent.state import RunState
from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json, thaw_json
from offeragent_harness.ports import (
    Clock,
    EventSink,
    IdGenerator,
    NewEvent,
    StoredEvent,
    UnitOfWorkFactory,
)
from offeragent_harness.sessions import AgentLineage, Session, SessionStatus, Turn, TurnStatus
from offeragent_harness.tools import canonical_json_sha256

from .cancellation import CancellationCode, CancellationReason, CancellationScope
from .event_bus import DeliveryFailure, UowRunRecorder
from .turn_manager import TurnManager


class HarnessServiceError(RuntimeError):
    pass


class EntityNotFound(HarnessServiceError):
    pass


class IdempotencyKeyConflict(HarnessServiceError):
    pass


@dataclass(frozen=True, slots=True)
class CreateSessionCommand:
    workspace_id: str
    profile_id: str
    title: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if not self.workspace_id or not self.profile_id or not self.idempotency_key:
            raise ValueError("session command identity fields must not be empty")


@dataclass(frozen=True, slots=True)
class SessionReceipt:
    session_id: str
    workspace_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class StartTurnCommand:
    workspace_id: str
    session_id: str
    turn_id: str
    idempotency_key: str
    input_blocks: tuple[Mapping[str, Any], ...]
    run_config: Mapping[str, Any]
    requires_write_outcome: bool = False

    def __post_init__(self) -> None:
        if not self.workspace_id or not self.session_id or not self.turn_id or not self.idempotency_key:
            raise ValueError("turn command identity fields must not be empty")
        if not self.input_blocks:
            raise ValueError("turn input cannot be empty")
        frozen_blocks = tuple(freeze_json(block) for block in self.input_blocks)
        if any(not isinstance(block, FrozenJsonObject) for block in frozen_blocks):
            raise TypeError("turn input blocks must be JSON objects")
        frozen_config = freeze_json(self.run_config)
        if not isinstance(frozen_config, FrozenJsonObject):
            raise TypeError("run config must be a JSON object")
        object.__setattr__(self, "input_blocks", frozen_blocks)
        object.__setattr__(self, "run_config", frozen_config)


@dataclass(frozen=True, slots=True)
class TurnReceipt:
    session_id: str
    turn_id: str
    run_id: str
    accepted: bool


@dataclass(frozen=True, slots=True)
class _ReceiptRecord:
    request_hash: str
    receipt: SessionReceipt | TurnReceipt


@dataclass(frozen=True, slots=True)
class RunComponents:
    planner: Planner
    composer: Composer
    tool_kernel: ToolKernel
    budget: RunBudget


class RunComponentsFactory(Protocol):
    def build(self, command: StartTurnCommand, state: RunState) -> RunComponents: ...


@dataclass(slots=True)
class HarnessDiagnostics:
    delivery_failures: list[DeliveryFailure] = field(default_factory=list)


class HarnessService:
    """The only application entrypoint shared by Pipe, Loopback and Direct adapters."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        event_sink: EventSink,
        clock: Clock,
        ids: IdGenerator,
        components: RunComponentsFactory,
        turn_manager: TurnManager | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._event_sink = event_sink
        self._clock = clock
        self._ids = ids
        self._components = components
        self._turn_manager = turn_manager or TurnManager()
        self._command_lock = asyncio.Lock()
        self.diagnostics = HarnessDiagnostics()

    async def create_session(self, command: CreateSessionCommand) -> SessionReceipt:
        request_hash = canonical_json_sha256(
            {
                "workspaceId": command.workspace_id,
                "profileId": command.profile_id,
                "title": command.title,
            }
        )
        async with self._command_lock:
            async with self._unit_of_work.begin() as uow:
                existing = await uow.entities.get("session_idempotency", command.idempotency_key)
            if existing is not None:
                if not isinstance(existing, _ReceiptRecord) or existing.request_hash != request_hash:
                    raise IdempotencyKeyConflict("session idempotency key is bound to a different request")
                if not isinstance(existing.receipt, SessionReceipt):
                    raise HarnessServiceError("session idempotency record is corrupt")
                return replace(existing.receipt, created=False)

            now = self._clock.utcnow()
            session_id = self._ids.new_id("ses")
            session = Session(
                session_id=session_id,
                workspace_id=command.workspace_id,
                profile_id=command.profile_id,
                title=command.title,
                status=SessionStatus.ACTIVE,
                created_at=now,
                updated_at=now,
                revision=1,
            )
            receipt = SessionReceipt(session_id, command.workspace_id, True)
            record = _ReceiptRecord(request_hash, receipt)
            async with self._unit_of_work.begin() as uow:
                await uow.entities.put("sessions", session_id, session, expected_revision=0)
                await uow.entities.put(
                    "session_idempotency",
                    command.idempotency_key,
                    record,
                    expected_revision=0,
                )
                await uow.commit()
            return receipt

    async def start_turn(self, command: StartTurnCommand) -> TurnReceipt:
        request_hash = canonical_json_sha256(
            {
                "workspaceId": command.workspace_id,
                "sessionId": command.session_id,
                "turnId": command.turn_id,
                "input": thaw_json(command.input_blocks),
                "runConfig": thaw_json(command.run_config),
                "requiresWriteOutcome": command.requires_write_outcome,
            }
        )
        idempotency_id = f"{command.session_id}:{command.idempotency_key}"
        async with self._command_lock:
            async with self._unit_of_work.begin() as uow:
                existing = await uow.entities.get("turn_idempotency", idempotency_id)
                session = await uow.entities.get("sessions", command.session_id)
            if existing is not None:
                if not isinstance(existing, _ReceiptRecord) or existing.request_hash != request_hash:
                    raise IdempotencyKeyConflict("turn idempotency key is bound to a different request")
                if not isinstance(existing.receipt, TurnReceipt):
                    raise HarnessServiceError("turn idempotency record is corrupt")
                return existing.receipt
            if not isinstance(session, Session):
                raise EntityNotFound(f"session {command.session_id!r} does not exist")
            if session.workspace_id != command.workspace_id:
                raise HarnessServiceError("session belongs to a different workspace")
            if session.status is not SessionStatus.ACTIVE:
                raise HarnessServiceError("session is not active")

            run_id = self._ids.new_id("run")
            state = RunState(
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                turn_id=command.turn_id,
                run_id=run_id,
                lineage=AgentLineage.root(run_id),
            )
            if command.requires_write_outcome:
                state = state.require_write_outcome("turn.start.requires_write_outcome")
            components = self._components.build(command, state)
            ready: asyncio.Future[tuple[UowRunRecorder, RunComponents]] = asyncio.get_running_loop().create_future()

            async def execute(cancellation: CancellationScope) -> RunState:
                recorder, resolved = await ready
                return await run_agent_loop(
                    state,
                    planner=resolved.planner,
                    composer=resolved.composer,
                    tool_kernel=resolved.tool_kernel,
                    recorder=recorder,
                    budget=BudgetLedger(resolved.budget, started_at=self._clock.utcnow()),
                    cancellation=cancellation,
                    now=self._clock.utcnow,
                )

            active = await self._turn_manager.start(
                session_id=command.session_id,
                run_id=run_id,
                factory=execute,
            )
            try:
                receipt, recorder = await self._persist_turn_start(
                    command=command,
                    request_hash=request_hash,
                    idempotency_id=idempotency_id,
                    session=session,
                    state=state,
                )
            except Exception as error:
                ready.set_exception(error)
                await active.cancellation.cancel(
                    CancellationReason.now(CancellationCode.START_FAILED, "turn start failed")
                )
                await asyncio.gather(active.task, return_exceptions=True)
                raise
            ready.set_result((recorder, components))
            return receipt

    async def _persist_turn_start(
        self,
        *,
        command: StartTurnCommand,
        request_hash: str,
        idempotency_id: str,
        session: Session,
        state: RunState,
    ) -> tuple[TurnReceipt, UowRunRecorder]:
        now = self._clock.utcnow()
        receipt = TurnReceipt(command.session_id, command.turn_id, state.run_id, True)
        turn = Turn(
            turn_id=command.turn_id,
            session_id=command.session_id,
            ordinal=session.revision,
            status=TurnStatus.RUNNING,
            input_blocks=command.input_blocks,
            created_at=now,
            updated_at=now,
        )
        updated_session = replace(session, updated_at=now, revision=session.revision + 1)
        event = NewEvent(
            event_id=self._ids.new_id("evt"),
            event_type="turn.started",
            payload={
                "workspaceId": command.workspace_id,
                "sessionId": command.session_id,
                "turnId": command.turn_id,
                "runId": state.run_id,
                "rootRunId": state.run_id,
                "parentRunId": None,
                "stateRevision": state.revision,
            },
            occurred_at=now,
            terminal=False,
            idempotency_key=f"{state.run_id}:1:turn.started",
        )
        async with self._unit_of_work.begin() as uow:
            await uow.entities.put(
                "sessions",
                session.session_id,
                updated_session,
                expected_revision=session.revision,
            )
            await uow.entities.put("turns", command.turn_id, turn, expected_revision=0)
            entity_revision = await uow.entities.put("run_states", state.run_id, state, expected_revision=0)
            await uow.entities.put(
                "turn_idempotency",
                idempotency_id,
                _ReceiptRecord(request_hash, receipt),
                expected_revision=0,
            )
            stored = await uow.events.append(state.run_id, 0, (event,))
            await uow.commit()

        try:
            await self._event_sink.publish(stored)
        except Exception as error:
            self.diagnostics.delivery_failures.append(
                DeliveryFailure(
                    event_ids=tuple(item.event_id for item in stored),
                    error_type=type(error).__name__,
                    message=str(error),
                )
            )
        recorder = UowRunRecorder(
            unit_of_work=self._unit_of_work,
            event_sink=self._event_sink,
            clock=self._clock,
            ids=self._ids,
            run_id=state.run_id,
            expected_entity_revision=entity_revision,
            expected_event_sequence=stored[-1].sequence,
        )
        return receipt, recorder

    async def cancel_turn(self, run_id: str, *, reason: str = "cancelled by user") -> bool:
        return await self._turn_manager.cancel(
            run_id,
            CancellationReason.now(CancellationCode.USER, reason),
        )

    async def replay_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        async with self._unit_of_work.begin() as uow:
            return await uow.events.read(run_id, after_sequence=after_sequence, limit=limit)

    async def get_run_state(self, run_id: str) -> RunState:
        async with self._unit_of_work.begin() as uow:
            state = await uow.entities.get("run_states", run_id)
        if not isinstance(state, RunState):
            raise EntityNotFound(f"run {run_id!r} does not exist")
        return state

    async def shutdown(self, *, grace_seconds: float = 10.0) -> None:
        await self._turn_manager.shutdown(grace_seconds=grace_seconds)


__all__ = [
    "CreateSessionCommand",
    "EntityNotFound",
    "HarnessService",
    "HarnessServiceError",
    "IdempotencyKeyConflict",
    "RunComponents",
    "RunComponentsFactory",
    "SessionReceipt",
    "StartTurnCommand",
    "TurnReceipt",
]
