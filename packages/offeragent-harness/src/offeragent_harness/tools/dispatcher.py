"""Routes validated calls to the one configured executor for each location."""

from __future__ import annotations

from collections.abc import Mapping

from offeragent_harness.ports import CancellationToken, OperationCancelled
from offeragent_harness.ports.tool import ToolExecutor

from .definitions import ExecutorLocation, ToolCall, ToolDefinition
from .results import ToolResult


class ToolDispatchError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        side_effect_possible: bool,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.side_effect_possible = side_effect_possible
        super().__init__(message)


class DispatcherUnavailable(ToolDispatchError):
    def __init__(self, location: ExecutorLocation) -> None:
        self.location = location
        super().__init__(
            "executor_unavailable",
            f"no executor is configured for {location.value}",
            retryable=False,
            side_effect_possible=False,
        )


class ToolDispatcher:
    def __init__(
        self,
        *,
        local: ToolExecutor | None = None,
        subagent: ToolExecutor | None = None,
    ) -> None:
        self._executors: Mapping[ExecutorLocation, ToolExecutor | None] = {
            ExecutorLocation.LOCAL: local,
            ExecutorLocation.SUBAGENT: subagent,
        }

    async def execute(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        cancellation: CancellationToken,
    ) -> ToolResult:
        if (definition.name, definition.version) != (call.name, call.version):
            raise ToolDispatchError(
                "definition_call_mismatch",
                "dispatcher received a definition that does not match the call",
                retryable=False,
                side_effect_possible=False,
            )
        executor = self._executors[definition.executor_location]
        if executor is None:
            raise DispatcherUnavailable(definition.executor_location)
        cancellation.checkpoint()
        try:
            return await executor.execute(call, cancellation)
        except (OperationCancelled, ToolDispatchError):
            raise
        except Exception as error:
            raise ToolDispatchError(
                "executor_exception",
                f"{type(error).__name__}: {error}",
                retryable=False,
                side_effect_possible=True,
            ) from error

__all__ = [
    "DispatcherUnavailable",
    "ToolDispatchError",
    "ToolDispatcher",
]
