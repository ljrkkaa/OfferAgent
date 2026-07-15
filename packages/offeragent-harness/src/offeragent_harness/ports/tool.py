"""Uniform executor port for local, client, and child-Agent tools."""

from typing import Protocol, runtime_checkable

from offeragent_harness.permissions import PolicyContext
from offeragent_harness.tools import ToolCall, ToolDefinition, ToolResult

from .approvals import ApprovalObserver
from .cancellation import CancellationToken


@runtime_checkable
class ToolExecutor(Protocol):
    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult: ...


@runtime_checkable
class ToolLifecycleObserver(ApprovalObserver, Protocol):
    async def call_replaced(self, original: ToolCall, replacement: ToolCall) -> None: ...

    async def execution_started(self, call: ToolCall, definition: ToolDefinition) -> None: ...

    async def result_available(
        self,
        call: ToolCall,
        definition: ToolDefinition,
        result: ToolResult,
    ) -> None: ...


@runtime_checkable
class ToolObservabilitySink(Protocol):
    """Content-free local telemetry boundary for the unified Tool Kernel."""

    async def result_recorded(
        self,
        call: ToolCall,
        definition: ToolDefinition,
        result: ToolResult,
        context: PolicyContext,
        elapsed_ms: int,
    ) -> None: ...

    async def approval_wait_recorded(
        self,
        call: ToolCall,
        context: PolicyContext,
        elapsed_ms: int,
    ) -> None: ...


__all__ = ["ToolExecutor", "ToolLifecycleObserver", "ToolObservabilitySink"]
