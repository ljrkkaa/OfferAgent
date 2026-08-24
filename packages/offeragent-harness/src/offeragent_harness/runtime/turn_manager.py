from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from offeragent_harness.agent.state import RunControlMessage
from offeragent_harness.error_codes import ResourceConflictCause

from .cancellation import CancellationCode, CancellationReason, CancellationScope


class SessionRunConflict(RuntimeError, ResourceConflictCause):
    conflict_reason = "session_active_run"
    conflict_user_message = "the Session already has an active Run"


RunFactory = Callable[[CancellationScope], Awaitable[Any]]
TerminalRunPredicate = Callable[[str], Awaitable[bool]]


class TurnManagerObserver(Protocol):
    def active_runs_changed(self, count: int) -> None: ...

    def cancellation_requested(self, run_id: str) -> None: ...

    def run_finished(self, run_id: str) -> None: ...


class RunControlInbox:
    def __init__(self, *, max_messages: int = 256) -> None:
        if max_messages < 1:
            raise ValueError("Run control inbox limit must be positive")
        self._queue: asyncio.Queue[RunControlMessage] = asyncio.Queue(max_messages)
        self._message_ids: set[str] = set()
        self._lock = asyncio.Lock()

    async def offer(self, message: RunControlMessage) -> bool:
        async with self._lock:
            if message.message_id in self._message_ids:
                return False
            self._queue.put_nowait(message)
            self._message_ids.add(message.message_id)
            return True

    async def drain(self) -> tuple[RunControlMessage, ...]:
        values: list[RunControlMessage] = []
        while True:
            try:
                values.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return tuple(values)


@dataclass(frozen=True, slots=True)
class ActiveRun:
    session_id: str
    run_id: str
    task: asyncio.Task[Any]
    cancellation: CancellationScope
    controls: RunControlInbox


class TurnManager:
    """Owns active root Runs and enforces one root Run per Session."""

    def __init__(self, *, max_active_runs: int = 4, observer: TurnManagerObserver | None = None) -> None:
        if max_active_runs < 1:
            raise ValueError("max_active_runs must be positive")
        self._max_active_runs = max_active_runs
        self._active_by_session: dict[str, ActiveRun] = {}
        self._active_by_run: dict[str, ActiveRun] = {}
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max_active_runs)
        self._accepting = True
        self._observer = observer
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._notify_active_runs()

    async def start(
        self,
        *,
        session_id: str,
        run_id: str,
        factory: RunFactory,
        controls: RunControlInbox | None = None,
    ) -> ActiveRun:
        async with self._lock:
            if not self._accepting:
                raise RuntimeError("turn manager is shutting down")
            existing = self._active_by_session.get(session_id)
            if existing is not None and not existing.task.done():
                raise SessionRunConflict(f"session {session_id} already has active run {existing.run_id}")
            if run_id in self._active_by_run:
                raise SessionRunConflict(f"run {run_id} is already active")
            cancellation = CancellationScope(name=f"run:{run_id}")
            control_inbox = controls or RunControlInbox()

            async def guarded() -> Any:
                async with self._slots:
                    return await factory(cancellation)

            task = asyncio.create_task(guarded(), name=f"offeragent-run:{run_id}")
            active = ActiveRun(
                session_id=session_id,
                run_id=run_id,
                task=task,
                cancellation=cancellation,
                controls=control_inbox,
            )
            self._active_by_session[session_id] = active
            self._active_by_run[run_id] = active
            self._notify_active_runs()
            task.add_done_callback(lambda completed: self._on_task_finished(run_id, completed))
            return active

    def _on_task_finished(self, run_id: str, task: asyncio.Task[Any]) -> None:
        """Observe the Run outcome before relinquishing the manager's task reference.

        A Run failure is represented durably by the session lifecycle.  The
        scheduler must still retrieve the task exception so asyncio does not
        report it later as an unhandled background failure.
        """

        if not task.cancelled():
            task.exception()
        cleanup = asyncio.create_task(self._remove(run_id), name=f"offeragent-run-cleanup:{run_id}")
        self._cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._consume_cleanup_task)

    def _consume_cleanup_task(self, task: asyncio.Task[None]) -> None:
        self._cleanup_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _remove(self, run_id: str) -> None:
        async with self._lock:
            active = self._active_by_run.pop(run_id, None)
            if active is not None and self._active_by_session.get(active.session_id) is active:
                self._active_by_session.pop(active.session_id, None)
        if active is not None:
            await active.cancellation.close()
            self._notify_run_finished(run_id)
        self._notify_active_runs()

    async def settle_terminal(
        self,
        session_id: str,
        is_durably_terminal: TerminalRunPredicate,
    ) -> bool:
        """Wait only for cleanup of a Run whose terminal commit is authoritative.

        A terminal event can become observable just before the owning task
        finishes its component, Memory, Subagent and approval cleanup.  The
        next Run must not race that cleanup, while a genuinely active Run must
        still produce ``SessionRunConflict`` without being awaited here.
        """

        async with self._lock:
            active = self._active_by_session.get(session_id)
        if active is None:
            return False
        if active.task.done():
            return True
        if not await is_durably_terminal(active.run_id):
            return False
        await asyncio.shield(asyncio.gather(active.task, return_exceptions=True))
        return True

    async def cancel(self, run_id: str, reason: CancellationReason) -> bool:
        async with self._lock:
            active = self._active_by_run.get(run_id)
        if active is None:
            return False
        if not active.cancellation.cancelled:
            self._notify_cancellation_requested(run_id)
        return await active.cancellation.cancel(reason)

    async def get(self, run_id: str) -> ActiveRun | None:
        async with self._lock:
            return self._active_by_run.get(run_id)

    async def steer(self, run_id: str, message: RunControlMessage) -> bool:
        async with self._lock:
            active = self._active_by_run.get(run_id)
        if active is None or active.task.done() or active.cancellation.cancelled:
            return False
        return await active.controls.offer(message)

    async def shutdown(self, *, grace_seconds: float = 10.0) -> None:
        async with self._lock:
            self._accepting = False
            active = tuple(self._active_by_run.values())
        reason = CancellationReason.now(CancellationCode.SHUTDOWN, "runtime is shutting down")
        for item in active:
            if not item.cancellation.cancelled:
                self._notify_cancellation_requested(item.run_id)
        await asyncio.gather(*(item.cancellation.cancel(reason) for item in active), return_exceptions=True)
        if active:
            _done, pending = await asyncio.wait(
                [item.task for item in active], timeout=grace_seconds, return_when=asyncio.ALL_COMPLETED
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        cleanup = tuple(self._cleanup_tasks)
        if cleanup:
            await asyncio.gather(*cleanup, return_exceptions=True)

    async def active_runs(self) -> tuple[ActiveRun, ...]:
        async with self._lock:
            return tuple(self._active_by_run.values())

    def _notify_active_runs(self) -> None:
        if self._observer is None:
            return
        try:
            self._observer.active_runs_changed(len(self._active_by_run))
        except Exception:
            return

    def _notify_cancellation_requested(self, run_id: str) -> None:
        if self._observer is None:
            return
        try:
            self._observer.cancellation_requested(run_id)
        except Exception:
            return

    def _notify_run_finished(self, run_id: str) -> None:
        if self._observer is None:
            return
        try:
            self._observer.run_finished(run_id)
        except Exception:
            return
