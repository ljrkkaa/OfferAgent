"""Cancellation boundary shared by models, tools and client calls."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


class CancellationCodeLike(Protocol):
    @property
    def value(self) -> str: ...


@runtime_checkable
class CancellationReasonLike(Protocol):
    @property
    def code(self) -> CancellationCodeLike: ...

    @property
    def message(self) -> str: ...

    @property
    def requested_at(self) -> datetime: ...


class OperationCancelled(BaseException):
    """Common monotonic cancellation signal for every token implementation."""

    def __init__(self, reason: CancellationReasonLike) -> None:
        self.reason = reason
        super().__init__(str(reason))


@runtime_checkable
class CancellationToken(Protocol):
    @property
    def cancelled(self) -> bool: ...

    @property
    def reason(self) -> CancellationReasonLike | None: ...

    async def wait(self) -> CancellationReasonLike: ...

    def checkpoint(self) -> None: ...


__all__ = ["CancellationCodeLike", "CancellationReasonLike", "CancellationToken", "OperationCancelled"]
