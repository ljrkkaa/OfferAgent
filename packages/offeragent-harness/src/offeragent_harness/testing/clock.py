"""Manual time and deterministic IDs without wall-clock sleeps."""

from __future__ import annotations

import asyncio
import re
import threading
from datetime import datetime, timedelta, timezone


class ManualClock:
    def __init__(self, initial: datetime | None = None, *, monotonic_start: float = 0.0) -> None:
        self._now = initial or datetime(2000, 1, 1, tzinfo=timezone.utc)
        if self._now.tzinfo is None or self._now.utcoffset() is None:
            raise ValueError("manual clock requires a timezone-aware initial time")
        self._monotonic = monotonic_start
        self._lock = threading.RLock()
        self._waiters: list[tuple[datetime, asyncio.Future[None], asyncio.AbstractEventLoop]] = []

    def utcnow(self) -> datetime:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._monotonic

    async def sleep_until(self, deadline: datetime) -> None:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")
        with self._lock:
            if deadline <= self._now:
                return
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters.append((deadline, future, future.get_loop()))
        try:
            await future
        finally:
            with self._lock:
                self._waiters = [(at, item, loop) for at, item, loop in self._waiters if item is not future]

    def advance(self, delta: timedelta) -> datetime:
        seconds = delta.total_seconds()
        if seconds < 0:
            raise ValueError("manual time cannot move backwards")
        with self._lock:
            self._now += delta
            self._monotonic += seconds
            due = [(future, loop) for deadline, future, loop in self._waiters if deadline <= self._now]
        for future, loop in due:
            loop.call_soon_threadsafe(_resolve_future, future)
        return self._now


def _resolve_future(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class DeterministicIdGenerator:
    def __init__(self, *, start: int = 1, width: int = 4) -> None:
        if start < 0 or width < 1:
            raise ValueError("invalid deterministic ID configuration")
        self._start = start
        self._width = width
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()

    def new_id(self, namespace: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", namespace):
            raise ValueError("ID namespace must be lowercase ASCII")
        with self._lock:
            value = self._counters.get(namespace, self._start)
            self._counters[namespace] = value + 1
        return f"{namespace}_{value:0{self._width}d}"


__all__ = ["DeterministicIdGenerator", "ManualClock"]
