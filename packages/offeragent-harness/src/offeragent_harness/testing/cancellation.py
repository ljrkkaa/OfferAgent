"""Manual cancellation token aligned with runtime.CancellationScope."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .errors import FakeRunCancelled


class ManualCancellationCode(str, Enum):
    TEST = "test"
    USER = "user"
    PARENT = "parent"
    DEADLINE = "deadline"
    SHUTDOWN = "shutdown"
    SUPERSEDED = "superseded"
    POLICY = "policy"


@dataclass(frozen=True)
class ManualCancellationReason:
    code: ManualCancellationCode
    message: str
    requested_at: datetime


class ManualCancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: ManualCancellationReason | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> ManualCancellationReason | None:
        return self._reason

    def cancel(
        self,
        code: ManualCancellationCode = ManualCancellationCode.TEST,
        message: str = "cancelled by test",
    ) -> bool:
        if self.cancelled:
            return False
        self._reason = ManualCancellationReason(code, message, datetime.now(timezone.utc))
        self._event.set()
        return True

    async def wait(self) -> ManualCancellationReason:
        await self._event.wait()
        assert self._reason is not None
        return self._reason

    def checkpoint(self) -> None:
        if self.cancelled:
            assert self._reason is not None
            raise FakeRunCancelled(self._reason)


__all__ = ["ManualCancellationCode", "ManualCancellationReason", "ManualCancellationToken"]
