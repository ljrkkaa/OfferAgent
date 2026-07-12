from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .cancellation import CancellationCode, CancellationReason, CancellationScope


class SessionRunConflict(RuntimeError):
    pass


RunFactory = Callable[[CancellationScope], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ActiveRun:
    session_id: str
    run_id: str
    task: asyncio.Task[Any]
    cancellation: CancellationScope


class TurnManager:
    """Owns active root Runs and enforces one root Run per Session."""

    def __init__(self, *, max_active_runs: int = 4) -> None:
        if max_active_runs < 1:
            raise ValueError("max_active_runs must be positive")
        self._max_active_runs = max_active_runs
        self._active_by_session: dict[str, ActiveRun] = {}
        self._active_by_run: dict[str, ActiveRun] = {}
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max_active_runs)
        self._accepting = True

    async def start(self, *, session_id: str, run_id: str, factory: RunFactory) -> ActiveRun:
        async with self._lock:
            if not self._accepting:
                raise RuntimeError("turn manager is shutting down")
            existing = self._active_by_session.get(session_id)
            if existing is not None and not existing.task.done():
                raise SessionRunConflict(f"session {session_id} already has active run {existing.run_id}")
            if run_id in self._active_by_run:
                raise SessionRunConflict(f"run {run_id} is already active")
            cancellation = CancellationScope(name=f"run:{run_id}")

            async def guarded() -> Any:
                async with self._slots:
                    return await factory(cancellation)

            task = asyncio.create_task(guarded(), name=f"offeragent-run:{run_id}")
            active = ActiveRun(session_id=session_id, run_id=run_id, task=task, cancellation=cancellation)
            self._active_by_session[session_id] = active
            self._active_by_run[run_id] = active
            task.add_done_callback(lambda _: asyncio.create_task(self._remove(run_id)))
            return active

    async def _remove(self, run_id: str) -> None:
        async with self._lock:
            active = self._active_by_run.pop(run_id, None)
            if active is not None and self._active_by_session.get(active.session_id) is active:
                self._active_by_session.pop(active.session_id, None)
        if active is not None:
            await active.cancellation.close()

    async def cancel(self, run_id: str, reason: CancellationReason) -> bool:
        async with self._lock:
            active = self._active_by_run.get(run_id)
        if active is None:
            return False
        return await active.cancellation.cancel(reason)

    async def get(self, run_id: str) -> ActiveRun | None:
        async with self._lock:
            return self._active_by_run.get(run_id)

    async def shutdown(self, *, grace_seconds: float = 10.0) -> None:
        async with self._lock:
            self._accepting = False
            active = tuple(self._active_by_run.values())
        reason = CancellationReason.now(CancellationCode.SHUTDOWN, "runtime is shutting down")
        await asyncio.gather(*(item.cancellation.cancel(reason) for item in active), return_exceptions=True)
        if active:
            _done, pending = await asyncio.wait(
                [item.task for item in active], timeout=grace_seconds, return_when=asyncio.ALL_COMPLETED
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def active_runs(self) -> tuple[ActiveRun, ...]:
        async with self._lock:
            return tuple(self._active_by_run.values())
