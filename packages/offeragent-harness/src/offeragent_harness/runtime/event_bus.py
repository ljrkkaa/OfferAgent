from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from offeragent_harness.agent.state import RunState
from offeragent_harness.ports import Clock, EventSink, IdGenerator, NewEvent, UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class DeliveryFailure:
    event_ids: tuple[str, ...]
    error_type: str
    message: str


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
        expected_entity_revision: int,
        expected_event_sequence: int,
    ) -> None:
        if expected_entity_revision < 0 or expected_event_sequence < 0:
            raise ValueError("expected revisions cannot be negative")
        self._unit_of_work = unit_of_work
        self._event_sink = event_sink
        self._clock = clock
        self._ids = ids
        self._run_id = run_id
        self._entity_revision = expected_entity_revision
        self._event_sequence = expected_event_sequence
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
    ) -> None:
        if state.run_id != self._run_id:
            raise ValueError("recorder cannot commit a different run")
        if terminal != state.phase.terminal:
            raise ValueError("terminal event flag must agree with RunState phase")

        async with self._lock:
            next_sequence = self._event_sequence + 1
            event_id = self._ids.new_id("evt")
            event = NewEvent(
                event_id=event_id,
                event_type=event_type,
                payload={
                    "workspaceId": state.workspace_id,
                    "sessionId": state.session_id,
                    "turnId": state.turn_id,
                    "runId": state.run_id,
                    "rootRunId": state.lineage.root_run_id,
                    "parentRunId": state.lineage.parent_run_id,
                    "stateRevision": state.revision,
                    "data": dict(payload),
                },
                occurred_at=self._clock.utcnow(),
                terminal=terminal,
                idempotency_key=f"{self._run_id}:{next_sequence}:{event_type}",
            )
            async with self._unit_of_work.begin() as uow:
                entity_revision = await uow.entities.put(
                    "run_states",
                    self._run_id,
                    state,
                    expected_revision=self._entity_revision,
                )
                stored = await uow.events.append(self._run_id, self._event_sequence, (event,))
                await uow.commit()

            self._entity_revision = entity_revision
            self._event_sequence = stored[-1].sequence

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


__all__ = ["DeliveryFailure", "UowRunRecorder"]
