"""Read-only UOW projection for Session/Turn application commands."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

from pydantic import TypeAdapter

from offeragent_harness.agent.state import RunPhase as DomainRunPhase
from offeragent_harness.agent.state import RunState
from offeragent_harness.models import thaw_json
from offeragent_harness.ports import CancellationToken, EntityRecord, UnitOfWorkFactory
from offeragent_harness.protocol.common import (
    RunPhase,
    RunSnapshot,
    RunStatus,
    TerminationReason,
    TurnSnapshot,
    TurnStatus,
    UsageSnapshot,
)
from offeragent_harness.protocol.content import ContentBlock, TextContentBlock
from offeragent_harness.sessions import Run, Turn
from offeragent_harness.sessions import RunStatus as DomainRunStatus

_CONTENT_BLOCKS: TypeAdapter[Any] = TypeAdapter(list[ContentBlock])


class ConversationProjectionError(LookupError):
    pass


class UowConversationProjectionService:
    def __init__(self, *, workspace_id: str, unit_of_work: UnitOfWorkFactory) -> None:
        if not workspace_id:
            raise ValueError("conversation projection requires a Workspace")
        self._workspace_id = workspace_id
        self._unit_of_work = unit_of_work

    async def turns(self, session_id: str, cancellation: CancellationToken) -> tuple[TurnSnapshot, ...]:
        cancellation.checkpoint()
        async with self._unit_of_work.begin() as uow:
            turns = tuple(
                record.value
                for record in await _all(uow.entities, "turns")
                if isinstance(record.value, Turn) and record.value.session_id == session_id
            )
            runs = tuple(
                record.value
                for record in await _all(uow.entities, "runs")
                if isinstance(record.value, Run)
                and record.value.workspace_id == self._workspace_id
                and record.value.session_id == session_id
            )
            states = {
                record.entity_id: record.value
                for record in await _all(uow.entities, "run_states")
                if isinstance(record.value, RunState)
            }
        cancellation.checkpoint()
        by_turn: dict[str, list[Run]] = {}
        for run in runs:
            by_turn.setdefault(run.turn_id, []).append(run)
        return tuple(
            _turn_snapshot(turn, by_turn.get(turn.turn_id, ()), states)
            for turn in sorted(turns, key=lambda value: (value.ordinal, value.turn_id))
        )

    async def turn(
        self,
        session_id: str,
        turn_id: str,
        cancellation: CancellationToken,
    ) -> TurnSnapshot:
        values = await self.turns(session_id, cancellation)
        for value in values:
            if value.turn_id == turn_id:
                return value
        raise ConversationProjectionError(f"Turn {turn_id!r} does not exist in Session {session_id!r}")

    async def resolve_run_id(
        self,
        session_id: str,
        turn_id: str,
        requested_run_id: str | None,
        cancellation: CancellationToken,
    ) -> str:
        turn = await self.turn(session_id, turn_id, cancellation)
        if requested_run_id is None:
            return turn.selected_run_id
        if requested_run_id not in {run.run_id for run in turn.runs}:
            raise ConversationProjectionError("requested Run does not belong to the Turn")
        return requested_run_id

    async def active_run_ids(self) -> tuple[str, ...]:
        async with self._unit_of_work.begin() as uow:
            records = await _all(uow.entities, "runs")
        return tuple(
            sorted(
                record.value.run_id
                for record in records
                if isinstance(record.value, Run)
                and record.value.workspace_id == self._workspace_id
                and not record.value.status.is_terminal
            )
        )


async def _all(entities: object, collection: str) -> tuple[EntityRecord, ...]:
    values: list[EntityRecord] = []
    after_id: str | None = None
    while True:
        page = await entities.list(collection, after_id=after_id, limit=1000)  # type: ignore[attr-defined]
        if not page:
            return tuple(values)
        values.extend(page)
        if len(values) > 100_000:
            raise ConversationProjectionError("conversation projection exceeds the safe entity bound")
        after_id = page[-1].entity_id


def _turn_snapshot(turn: Turn, runs: Iterable[Run], states: dict[str, RunState]) -> TurnSnapshot:
    ordered = tuple(sorted(runs, key=lambda value: (value.attempt, value.created_at, value.run_id)))
    if not ordered:
        raise ConversationProjectionError(f"Turn {turn.turn_id!r} has no Run")
    selected = ordered[-1]
    state = states.get(selected.run_id)
    assistant: list[ContentBlock] = []
    if state is not None and state.assistant_text:
        assistant = [TextContentBlock(type="text", text=state.assistant_text)]
    return TurnSnapshot(
        turn_id=turn.turn_id,
        session_id=turn.session_id,
        status=TurnStatus(turn.status.value),
        input=_restore_content_blocks(turn.input_blocks),
        runs=[_run_snapshot(run, states.get(run.run_id)) for run in ordered],
        selected_run_id=selected.run_id,
        assistant_content=assistant,
        created_at=turn.created_at.isoformat(),
        updated_at=turn.updated_at.isoformat(),
    )


def _restore_content_blocks(value: object) -> list[ContentBlock]:
    """Rebuild persisted wire DTOs with the protocol's strict JSON semantics."""

    encoded = json.dumps(
        thaw_json(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return cast(list[ContentBlock], _CONTENT_BLOCKS.validate_json(encoded))


def _run_snapshot(run: Run, state: RunState | None) -> RunSnapshot:
    checkpoint = None if state is None else state.budget_checkpoint
    usage = UsageSnapshot(
        input_tokens=0 if checkpoint is None else checkpoint.used.input_tokens,
        output_tokens=0 if checkpoint is None else checkpoint.used.output_tokens,
        model_calls=0 if checkpoint is None else checkpoint.used.model_rounds,
        tool_calls=0 if checkpoint is None else checkpoint.used.tool_calls,
        cost_micros=None if checkpoint is None else int(checkpoint.used.cost * 1_000_000),
        wall_time_ms=0 if checkpoint is None else int(checkpoint.elapsed_seconds * 1000),
    )
    return RunSnapshot(
        run_id=run.run_id,
        root_run_id=run.lineage.root_run_id,
        parent_run_id=run.lineage.parent_run_id,
        session_id=run.session_id,
        turn_id=run.turn_id,
        status=_run_status(run.status),
        phase=_run_phase(run, state),
        agent_name=run.lineage.agent_name,
        depth=run.lineage.depth,
        started_at=run.created_at.isoformat(),
        completed_at=run.updated_at.isoformat() if run.status.is_terminal else None,
        last_sequence=run.event_sequence,
        usage=usage,
        termination_reason=(
            None if run.termination_reason is None else TerminationReason(run.termination_reason.value)
        ),
    )


def _run_status(value: DomainRunStatus) -> RunStatus:
    direct = {
        DomainRunStatus.CREATED: RunStatus.CREATED,
        DomainRunStatus.QUEUED: RunStatus.QUEUED,
        DomainRunStatus.AWAITING_APPROVAL: RunStatus.AWAITING_APPROVAL,
        DomainRunStatus.WAITING_CHILDREN: RunStatus.WAITING_CHILDREN,
        DomainRunStatus.CANCELLED: RunStatus.CANCELLED,
        DomainRunStatus.COMPLETED: RunStatus.COMPLETED,
        DomainRunStatus.FAILED: RunStatus.FAILED,
        DomainRunStatus.INTERRUPTED: RunStatus.INTERRUPTED,
        DomainRunStatus.ORPHANED: RunStatus.ORPHANED,
    }
    return direct.get(value, RunStatus.RUNNING)


def _run_phase(run: Run, state: RunState | None) -> RunPhase:
    if run.status.is_terminal:
        return RunPhase.TERMINAL
    if state is None:
        return RunPhase.CREATED
    if state.phase is DomainRunPhase.COMPLETED or state.phase.terminal:
        return RunPhase.TERMINAL
    return RunPhase(state.phase.value)


__all__ = ["ConversationProjectionError", "UowConversationProjectionService"]
