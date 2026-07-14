"""Routes validated calls to the one configured executor for each location."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from offeragent_harness.ports import CancellationToken, ClientToolInvocation, ClientToolPort, Clock, OperationCancelled
from offeragent_harness.ports.tool import ToolExecutor

from .canonical import canonical_json_sha256
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


class InvocationAcknowledgementLost(ToolDispatchError):
    def __init__(self, message: str = "tool invocation committed but acknowledgement was lost") -> None:
        super().__init__(
            "invocation_acknowledgement_lost",
            message,
            retryable=True,
            side_effect_possible=True,
        )


class ToolDispatcher:
    def __init__(
        self,
        *,
        clock: Clock,
        local: ToolExecutor | None = None,
        client: ClientToolPort | None = None,
        subagent: ToolExecutor | None = None,
    ) -> None:
        self._clock = clock
        self._executors: Mapping[ExecutorLocation, ToolExecutor | ClientToolPort | None] = {
            ExecutorLocation.LOCAL: local,
            ExecutorLocation.CLIENT: client,
            ExecutorLocation.SUBAGENT: subagent,
        }
        self._client = client

    @staticmethod
    def client_invocation_id(call: ToolCall) -> str:
        digest = canonical_json_sha256(
            {
                "workspaceId": call.workspace_id,
                "rootRunId": call.lineage.root_run_id,
                "runId": call.run_id,
                "toolName": call.name,
                "toolVersion": call.version,
                "idempotencyKey": call.idempotency_key,
                "argsHash": call.args_hash,
            }
        )
        return f"inv_{digest.removeprefix('sha256:')[:24]}"

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
            if definition.executor_location is ExecutorLocation.CLIENT:
                assert self._client is not None
                deadline = call.deadline or self._clock.utcnow() + timedelta(milliseconds=definition.timeout_ms)
                invocation = ClientToolInvocation(
                    invocation_id=self.client_invocation_id(call),
                    call=call,
                    deadline=deadline,
                )
                return await self._client.invoke(invocation, cancellation)
            assert isinstance(executor, ToolExecutor)
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

    async def lookup_result(self, definition: ToolDefinition, call: ToolCall) -> ToolResult | None:
        if definition.executor_location is not ExecutorLocation.CLIENT or self._client is None:
            return None
        try:
            return await self._client.lookup_result(self.client_invocation_id(call), run_id=call.run_id)
        except Exception as error:
            raise ToolDispatchError(
                "invocation_lookup_failed",
                f"{type(error).__name__}: {error}",
                retryable=True,
                side_effect_possible=False,
            ) from error


__all__ = [
    "DispatcherUnavailable",
    "InvocationAcknowledgementLost",
    "ToolDispatchError",
    "ToolDispatcher",
]
