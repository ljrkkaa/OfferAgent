"""Deterministic time and identity ports."""

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def utcnow(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep_until(self, deadline: datetime) -> None: ...


@runtime_checkable
class IdGenerator(Protocol):
    def new_id(self, namespace: str) -> str: ...


__all__ = ["Clock", "IdGenerator"]
