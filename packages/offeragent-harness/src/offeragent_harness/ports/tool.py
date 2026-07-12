"""Uniform executor port for local, client, MCP and child-Agent tools."""

from typing import Protocol, runtime_checkable

from offeragent_harness.tools import ToolCall, ToolResult

from .cancellation import CancellationToken


@runtime_checkable
class ToolExecutor(Protocol):
    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult: ...


__all__ = ["ToolExecutor"]
