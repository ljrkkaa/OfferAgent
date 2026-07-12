from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class WorkerLifecycle(str, Enum):
    COLD = "cold"
    STARTING = "starting"
    INDEXING = "indexing"
    READY = "ready"
    BUSY = "busy"
    IDLE = "idle"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    CRASHED = "crashed"
    RECOVERING = "recovering"


ALLOWED_TRANSITIONS: dict[WorkerLifecycle, frozenset[WorkerLifecycle]] = {
    WorkerLifecycle.COLD: frozenset({WorkerLifecycle.STARTING, WorkerLifecycle.STOPPED}),
    WorkerLifecycle.STARTING: frozenset(
        {WorkerLifecycle.INDEXING, WorkerLifecycle.READY, WorkerLifecycle.DEGRADED, WorkerLifecycle.CRASHED}
    ),
    WorkerLifecycle.INDEXING: frozenset(
        {
            WorkerLifecycle.READY,
            WorkerLifecycle.BUSY,
            WorkerLifecycle.DEGRADED,
            WorkerLifecycle.STOPPING,
            WorkerLifecycle.CRASHED,
        }
    ),
    WorkerLifecycle.READY: frozenset(
        {
            WorkerLifecycle.BUSY,
            WorkerLifecycle.IDLE,
            WorkerLifecycle.INDEXING,
            WorkerLifecycle.DEGRADED,
            WorkerLifecycle.STOPPING,
            WorkerLifecycle.CRASHED,
        }
    ),
    WorkerLifecycle.BUSY: frozenset(
        {
            WorkerLifecycle.READY,
            WorkerLifecycle.IDLE,
            WorkerLifecycle.DEGRADED,
            WorkerLifecycle.STOPPING,
            WorkerLifecycle.CRASHED,
        }
    ),
    WorkerLifecycle.IDLE: frozenset(
        {
            WorkerLifecycle.BUSY,
            WorkerLifecycle.READY,
            WorkerLifecycle.INDEXING,
            WorkerLifecycle.STOPPING,
            WorkerLifecycle.CRASHED,
        }
    ),
    WorkerLifecycle.DEGRADED: frozenset(
        {WorkerLifecycle.READY, WorkerLifecycle.RECOVERING, WorkerLifecycle.STOPPING, WorkerLifecycle.CRASHED}
    ),
    WorkerLifecycle.STOPPING: frozenset({WorkerLifecycle.STOPPED, WorkerLifecycle.CRASHED}),
    WorkerLifecycle.STOPPED: frozenset({WorkerLifecycle.STARTING}),
    WorkerLifecycle.CRASHED: frozenset({WorkerLifecycle.RECOVERING, WorkerLifecycle.STOPPED}),
    WorkerLifecycle.RECOVERING: frozenset(
        {WorkerLifecycle.INDEXING, WorkerLifecycle.READY, WorkerLifecycle.DEGRADED, WorkerLifecycle.CRASHED}
    ),
}


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    state: WorkerLifecycle
    revision: int
    changed_at: datetime
    reason: str | None


class InvalidLifecycleTransition(RuntimeError):
    pass


class LifecycleMachine:
    def __init__(self, initial: WorkerLifecycle = WorkerLifecycle.COLD) -> None:
        self._snapshot = LifecycleSnapshot(initial, 0, datetime.now(timezone.utc), None)
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> LifecycleSnapshot:
        return self._snapshot

    async def transition(
        self,
        target: WorkerLifecycle,
        *,
        expected_revision: int,
        reason: str | None = None,
    ) -> LifecycleSnapshot:
        async with self._lock:
            current = self._snapshot
            if current.revision != expected_revision:
                raise InvalidLifecycleTransition(
                    f"lifecycle revision changed: expected {expected_revision}, actual {current.revision}"
                )
            if target not in ALLOWED_TRANSITIONS[current.state]:
                raise InvalidLifecycleTransition(
                    f"invalid lifecycle transition {current.state.value} -> {target.value}"
                )
            self._snapshot = LifecycleSnapshot(target, current.revision + 1, datetime.now(timezone.utc), reason)
            return self._snapshot
