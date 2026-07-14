"""Root-run fair child scheduler with bounded workspace/root concurrency."""

from __future__ import annotations

import asyncio
import heapq
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from itertools import count

from offeragent_harness.ports.subagents import ChildCancellationFactory, ChildCancellationSource

from .models import SubagentResult, SubagentRunRecord


class SchedulerCancellationCode(str, Enum):
    USER = "user"
    PARENT = "parent"
    DEADLINE = "deadline"
    SHUTDOWN = "shutdown"
    POLICY = "policy"


@dataclass(frozen=True, slots=True)
class SchedulerCancellationReason:
    code: SchedulerCancellationCode
    message: str
    requested_at: datetime


RunCallback = Callable[[SubagentRunRecord, ChildCancellationSource], Awaitable[SubagentResult]]
StartedCallback = Callable[[str], Awaitable[None]]
FinishedCallback = Callable[[str, SubagentResult | None, BaseException | None], Awaitable[None]]


@dataclass(slots=True)
class _Job:
    record: SubagentRunRecord
    run: RunCallback
    started: StartedCallback
    finished: FinishedCallback
    sequence: int


class ChildRunScheduler:
    def __init__(
        self,
        cancellations: ChildCancellationFactory,
        *,
        max_workspace_active: int = 4,
        max_root_active: int = 3,
        max_queue: int = 1024,
    ) -> None:
        if not 1 <= max_root_active <= max_workspace_active <= 64 or not 1 <= max_queue <= 100_000:
            raise ValueError("Subagent scheduler limits are invalid")
        self._cancellations = cancellations
        self._max_workspace = max_workspace_active
        self._max_root = max_root_active
        self._max_queue = max_queue
        self._queues: dict[str, list[tuple[int, int, _Job]]] = {}
        self._root_order: deque[str] = deque()
        self._active: dict[str, tuple[_Job, ChildCancellationSource, asyncio.Task[None]]] = {}
        self._active_by_root: dict[str, int] = {}
        self._sequence = count(1)
        self._lock = asyncio.Lock()
        self._accepting = True

    async def submit(
        self,
        record: SubagentRunRecord,
        *,
        run: RunCallback,
        started: StartedCallback,
        finished: FinishedCallback,
    ) -> None:
        async with self._lock:
            if not self._accepting:
                raise RuntimeError("Subagent scheduler is shutting down")
            if record.run_id in self._active or any(
                item[2].record.run_id == record.run_id for queue in self._queues.values() for item in queue
            ):
                raise RuntimeError("Subagent Run is already scheduled")
            if sum(len(queue) for queue in self._queues.values()) >= self._max_queue:
                raise RuntimeError("Subagent scheduler queue is full")
            sequence = next(self._sequence)
            job = _Job(record, run, started, finished, sequence)
            queue = self._queues.setdefault(record.root_run_id, [])
            heapq.heappush(queue, (-record.priority.weight, sequence, job))
            if record.root_run_id not in self._root_order:
                self._root_order.append(record.root_run_id)
            self._dispatch_locked()

    async def cancel(self, run_id: str, reason: str) -> bool:
        async with self._lock:
            active = self._active.get(run_id)
            if active is not None:
                source = active[1]
                queued: _Job | None = None
            else:
                source = None
                queued = self._remove_queued_locked(run_id)
        cancellation = SchedulerCancellationReason(
            SchedulerCancellationCode.USER,
            reason,
            datetime.now(timezone.utc),
        )
        if source is not None:
            return await source.cancel(cancellation)
        if queued is not None:
            await queued.finished(run_id, None, asyncio.CancelledError(reason))
            return True
        return False

    async def shutdown(self) -> None:
        async with self._lock:
            self._accepting = False
            active = tuple(self._active.values())
            queued = [item[2] for queue in self._queues.values() for item in queue]
            self._queues.clear()
            self._root_order.clear()
        reason = SchedulerCancellationReason(
            SchedulerCancellationCode.SHUTDOWN,
            "Worker shutdown",
            datetime.now(timezone.utc),
        )
        await asyncio.gather(*(source.cancel(reason) for _, source, _ in active), return_exceptions=True)
        await asyncio.gather(
            *(job.finished(job.record.run_id, None, asyncio.CancelledError("Worker shutdown")) for job in queued),
            return_exceptions=True,
        )

    async def active_run_ids(self) -> tuple[str, ...]:
        async with self._lock:
            return tuple(sorted(self._active))

    def _dispatch_locked(self) -> None:
        while len(self._active) < self._max_workspace and self._root_order:
            selected: _Job | None = None
            rotations = len(self._root_order)
            for _ in range(rotations):
                root = self._root_order.popleft()
                queue = self._queues.get(root, [])
                if queue and self._active_by_root.get(root, 0) < self._max_root:
                    selected = heapq.heappop(queue)[2]
                    if queue:
                        self._root_order.append(root)
                    else:
                        self._queues.pop(root, None)
                    break
                if queue:
                    self._root_order.append(root)
                else:
                    self._queues.pop(root, None)
            if selected is None:
                return
            source = self._cancellations.create(
                root_run_id=selected.record.root_run_id,
                parent_run_id=selected.record.parent_run_id,
                child_run_id=selected.record.run_id,
            )
            task = asyncio.create_task(self._execute(selected, source), name=f"subagent:{selected.record.run_id}")
            self._active[selected.record.run_id] = (selected, source, task)
            self._active_by_root[selected.record.root_run_id] = (
                self._active_by_root.get(selected.record.root_run_id, 0) + 1
            )

    async def _execute(self, job: _Job, source: ChildCancellationSource) -> None:
        result: SubagentResult | None = None
        error: BaseException | None = None
        try:
            await job.started(job.record.run_id)
            result = await job.run(job.record, source)
        except BaseException as caught:
            error = caught
        try:
            await job.finished(job.record.run_id, result, error)
        finally:
            await source.close()
            async with self._lock:
                self._active.pop(job.record.run_id, None)
                count_for_root = self._active_by_root.get(job.record.root_run_id, 1) - 1
                if count_for_root:
                    self._active_by_root[job.record.root_run_id] = count_for_root
                else:
                    self._active_by_root.pop(job.record.root_run_id, None)
                if self._queues.get(job.record.root_run_id) and job.record.root_run_id not in self._root_order:
                    self._root_order.append(job.record.root_run_id)
                self._dispatch_locked()

    def _remove_queued_locked(self, run_id: str) -> _Job | None:
        for root, queue in tuple(self._queues.items()):
            for index, item in enumerate(queue):
                if item[2].record.run_id == run_id:
                    job = item[2]
                    queue.pop(index)
                    heapq.heapify(queue)
                    if not queue:
                        self._queues.pop(root, None)
                        self._root_order = deque(item for item in self._root_order if item != root)
                    return job
        return None


__all__ = [
    "ChildRunScheduler",
    "SchedulerCancellationCode",
    "SchedulerCancellationReason",
]
