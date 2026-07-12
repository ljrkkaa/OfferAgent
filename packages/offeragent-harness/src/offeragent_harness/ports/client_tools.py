"""Obsidian bridge reverse-tool boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from offeragent_harness.tools import ToolCall, ToolResult

from .cancellation import CancellationToken


@dataclass(frozen=True)
class ClientToolInvocation:
    invocation_id: str
    call: ToolCall
    deadline: datetime

    def __post_init__(self) -> None:
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise ValueError("client invocation deadline must be timezone-aware")


@runtime_checkable
class ClientToolPort(Protocol):
    async def invoke(self, invocation: ClientToolInvocation, cancellation: CancellationToken) -> ToolResult: ...

    async def cancel(self, invocation_id: str, reason: str) -> None: ...

    async def lookup_result(self, invocation_id: str) -> ToolResult | None: ...


__all__ = ["ClientToolInvocation", "ClientToolPort"]
