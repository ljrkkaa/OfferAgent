from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Final

from offeragent_harness.ports.cancellation import OperationCancelled


class CancellationCode(str, Enum):
    USER = "user"
    PARENT = "parent"
    DEADLINE = "deadline"
    SHUTDOWN = "shutdown"
    START_FAILED = "start_failed"
    SUPERSEDED = "superseded"
    POLICY = "policy"


@dataclass(frozen=True, slots=True)
class CancellationReason:
    code: CancellationCode
    message: str
    requested_at: datetime

    @classmethod
    def now(cls, code: CancellationCode, message: str) -> CancellationReason:
        return cls(code=code, message=message, requested_at=datetime.now(timezone.utc))


class RunCancelled(OperationCancelled, asyncio.CancelledError):
    def __init__(self, reason: CancellationReason) -> None:
        OperationCancelled.__init__(self, reason)


CancelCallback = Callable[[CancellationReason], Awaitable[None] | None]


class CancellationScope:
    """A hierarchical cancellation token owned by one event loop.

    Cancellation is monotonic and idempotent. A child created after its parent
    was cancelled starts cancelled with a derived parent reason. Callbacks are
    best-effort cleanup notifications; callback failures never stop propagation.
    """

    _CALLBACK_TIMEOUT_SECONDS: Final[float] = 5.0

    def __init__(self, *, parent: CancellationScope | None = None, name: str = "scope") -> None:
        self.name = name
        self._parent = parent
        self._event = asyncio.Event()
        self._reason: CancellationReason | None = None
        self._children: set[CancellationScope] = set()
        self._callbacks: list[CancelCallback] = []
        self._background_callbacks: set[asyncio.Future[None]] = set()
        self._lock = asyncio.Lock()
        if parent is not None:
            parent._children.add(self)
            if parent.cancelled and parent.reason is not None:
                self._reason = CancellationReason(
                    code=CancellationCode.PARENT,
                    message=f"parent {parent.name} cancelled: {parent.reason.message}",
                    requested_at=parent.reason.requested_at,
                )
                self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> CancellationReason | None:
        return self._reason

    def child(self, name: str) -> CancellationScope:
        return CancellationScope(parent=self, name=name)

    def add_callback(self, callback: CancelCallback) -> None:
        if self.cancelled and self._reason is not None:
            result = callback(self._reason)
            if isinstance(result, Awaitable):
                task = asyncio.ensure_future(result)
                self._background_callbacks.add(task)
                task.add_done_callback(self._background_callbacks.discard)
            return
        self._callbacks.append(callback)

    async def cancel(self, reason: CancellationReason) -> bool:
        async with self._lock:
            if self.cancelled:
                return False
            self._reason = reason
            self._event.set()
            children = tuple(self._children)
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()

        child_reason = CancellationReason(
            code=CancellationCode.PARENT,
            message=f"parent {self.name} cancelled: {reason.message}",
            requested_at=reason.requested_at,
        )
        if children:
            await asyncio.gather(*(child.cancel(child_reason) for child in children), return_exceptions=True)
        for callback in callbacks:
            try:
                result = callback(reason)
                if isinstance(result, Awaitable):
                    await asyncio.wait_for(result, timeout=self._CALLBACK_TIMEOUT_SECONDS)
            except (Exception, asyncio.CancelledError):
                # Cancellation must remain monotonic even when cleanup fails.
                continue
        return True

    async def wait(self) -> CancellationReason:
        await self._event.wait()
        assert self._reason is not None
        return self._reason

    def checkpoint(self) -> None:
        if self.cancelled:
            assert self._reason is not None
            raise RunCancelled(self._reason)

    async def close(self) -> None:
        if self._parent is not None:
            self._parent._children.discard(self)

    def schedule_deadline(self, delay_seconds: float, *, message: str = "deadline exceeded") -> asyncio.Task[None]:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")

        async def cancel_on_deadline() -> None:
            try:
                await asyncio.sleep(delay_seconds)
                await self.cancel(CancellationReason.now(CancellationCode.DEADLINE, message))
            except asyncio.CancelledError:
                return

        return asyncio.create_task(cancel_on_deadline(), name=f"{self.name}:deadline")
