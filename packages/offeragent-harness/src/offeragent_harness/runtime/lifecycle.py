"""Revision-guarded Host and Worker lifecycle state machines."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias, cast


class HostLifecycle(str, Enum):
    ABSENT = "absent"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    RESTARTING = "restarting"
    STOPPING = "stopping"
    STOPPED = "stopped"


class WorkerLifecycle(str, Enum):
    COLD = "cold"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    IDLE = "idle"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    CRASHED = "crashed"
    RECOVERING = "recovering"


HOST_TRANSITIONS: Mapping[HostLifecycle, frozenset[HostLifecycle]] = MappingProxyType(
    {
        HostLifecycle.ABSENT: frozenset({HostLifecycle.STARTING, HostLifecycle.STOPPED}),
        HostLifecycle.STARTING: frozenset(
            {HostLifecycle.READY, HostLifecycle.DEGRADED, HostLifecycle.STOPPING, HostLifecycle.STOPPED}
        ),
        HostLifecycle.READY: frozenset({HostLifecycle.DEGRADED, HostLifecycle.RESTARTING, HostLifecycle.STOPPING}),
        HostLifecycle.DEGRADED: frozenset({HostLifecycle.READY, HostLifecycle.RESTARTING, HostLifecycle.STOPPING}),
        HostLifecycle.RESTARTING: frozenset(
            {HostLifecycle.READY, HostLifecycle.DEGRADED, HostLifecycle.STOPPING, HostLifecycle.STOPPED}
        ),
        HostLifecycle.STOPPING: frozenset({HostLifecycle.STOPPED}),
        HostLifecycle.STOPPED: frozenset({HostLifecycle.STARTING}),
    }
)

WORKER_TRANSITIONS: Mapping[WorkerLifecycle, frozenset[WorkerLifecycle]] = MappingProxyType(
    {
        WorkerLifecycle.COLD: frozenset({WorkerLifecycle.STARTING, WorkerLifecycle.STOPPED}),
        WorkerLifecycle.STARTING: frozenset(
            {
                WorkerLifecycle.READY,
                WorkerLifecycle.DEGRADED,
                WorkerLifecycle.STOPPING,
                WorkerLifecycle.CRASHED,
            }
        ),
        WorkerLifecycle.READY: frozenset(
            {
                WorkerLifecycle.BUSY,
                WorkerLifecycle.IDLE,
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
                WorkerLifecycle.STOPPING,
                WorkerLifecycle.CRASHED,
            }
        ),
        WorkerLifecycle.DEGRADED: frozenset(
            {
                WorkerLifecycle.READY,
                WorkerLifecycle.RECOVERING,
                WorkerLifecycle.STOPPING,
                WorkerLifecycle.CRASHED,
            }
        ),
        WorkerLifecycle.STOPPING: frozenset({WorkerLifecycle.STOPPED, WorkerLifecycle.CRASHED}),
        WorkerLifecycle.STOPPED: frozenset({WorkerLifecycle.STARTING}),
        WorkerLifecycle.CRASHED: frozenset({WorkerLifecycle.RECOVERING, WorkerLifecycle.STOPPED}),
        WorkerLifecycle.RECOVERING: frozenset(
            {
                WorkerLifecycle.READY,
                WorkerLifecycle.DEGRADED,
                WorkerLifecycle.STOPPING,
                WorkerLifecycle.CRASHED,
            }
        ),
    }
)

# Compatibility alias used by the phase-0 tests and downstream adapters.
ALLOWED_TRANSITIONS = WORKER_TRANSITIONS

LifecycleState: TypeAlias = HostLifecycle | WorkerLifecycle
HostTransitionGraph: TypeAlias = Mapping[HostLifecycle, frozenset[HostLifecycle]]
WorkerTransitionGraph: TypeAlias = Mapping[WorkerLifecycle, frozenset[WorkerLifecycle]]


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    state: LifecycleState
    revision: int
    changed_at: datetime
    reason: str | None

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("lifecycle revision cannot be negative")
        if self.changed_at.tzinfo is None or self.changed_at.utcoffset() is None:
            raise ValueError("lifecycle timestamp must be timezone-aware")
        if self.reason is not None and (not self.reason or len(self.reason) > 2_048 or "\x00" in self.reason):
            raise ValueError("lifecycle reason must contain 1..2048 non-NUL characters")


class InvalidLifecycleTransition(RuntimeError):
    pass


class LifecycleWaitTimeout(TimeoutError):
    pass


class LifecycleMachine:
    """In-process CAS projection; persistence remains a Runtime repository concern."""

    def __init__(
        self,
        initial: LifecycleState = WorkerLifecycle.COLD,
        *,
        transitions: HostTransitionGraph | WorkerTransitionGraph | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if transitions is None:
            selected: Mapping[LifecycleState, frozenset[LifecycleState]]
            if isinstance(initial, HostLifecycle):
                selected = cast(Mapping[LifecycleState, frozenset[LifecycleState]], HOST_TRANSITIONS)
            else:
                selected = cast(Mapping[LifecycleState, frozenset[LifecycleState]], WORKER_TRANSITIONS)
        else:
            selected = cast(Mapping[LifecycleState, frozenset[LifecycleState]], transitions)
        if set(selected) != set(type(initial)) or any(
            not isinstance(target, type(initial)) for targets in selected.values() for target in targets
        ):
            raise ValueError("lifecycle transition graph does not cover exactly one lifecycle enum")
        self._transitions: Mapping[LifecycleState, frozenset[LifecycleState]] = selected
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._snapshot: LifecycleSnapshot = LifecycleSnapshot(initial, 0, self._timestamp(), None)
        self._condition = asyncio.Condition()

    @classmethod
    def host(
        cls,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> LifecycleMachine:
        return LifecycleMachine(HostLifecycle.ABSENT, transitions=HOST_TRANSITIONS, now=now)

    @classmethod
    def worker(
        cls,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> LifecycleMachine:
        return LifecycleMachine(WorkerLifecycle.COLD, transitions=WORKER_TRANSITIONS, now=now)

    @property
    def snapshot(self) -> LifecycleSnapshot:
        return self._snapshot

    async def transition(
        self,
        target: LifecycleState,
        *,
        expected_revision: int,
        reason: str | None = None,
    ) -> LifecycleSnapshot:
        async with self._condition:
            current = self._snapshot
            if current.revision != expected_revision:
                raise InvalidLifecycleTransition(
                    f"lifecycle revision changed: expected {expected_revision}, actual {current.revision}"
                )
            if target not in self._transitions[current.state]:
                raise InvalidLifecycleTransition(
                    f"invalid lifecycle transition {current.state.value} -> {target.value}"
                )
            changed_at = self._timestamp()
            if changed_at < current.changed_at:
                raise ValueError("lifecycle clock moved backwards")
            self._snapshot = LifecycleSnapshot(target, current.revision + 1, changed_at, reason)
            self._condition.notify_all()
            return self._snapshot

    async def wait_for(
        self,
        states: frozenset[LifecycleState],
        *,
        after_revision: int = -1,
        timeout_seconds: float | None = None,
    ) -> LifecycleSnapshot:
        if not states or any(not isinstance(state, type(self._snapshot.state)) for state in states):
            raise ValueError("wait states must be a non-empty set from this lifecycle enum")
        if after_revision < -1:
            raise ValueError("after_revision cannot be less than -1")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        async def wait() -> LifecycleSnapshot:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: self._snapshot.revision > after_revision and self._snapshot.state in states
                )
                return self._snapshot

        try:
            return await wait() if timeout_seconds is None else await asyncio.wait_for(wait(), timeout_seconds)
        except asyncio.TimeoutError as error:
            raise LifecycleWaitTimeout("lifecycle wait deadline expired") from error

    def _timestamp(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lifecycle clock must return timezone-aware timestamps")
        return value


__all__ = [
    "ALLOWED_TRANSITIONS",
    "HOST_TRANSITIONS",
    "WORKER_TRANSITIONS",
    "HostLifecycle",
    "InvalidLifecycleTransition",
    "LifecycleMachine",
    "LifecycleSnapshot",
    "LifecycleWaitTimeout",
    "WorkerLifecycle",
]
