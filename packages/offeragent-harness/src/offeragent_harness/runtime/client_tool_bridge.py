"""Exactly-once reverse Client Tool adapter over the current local IPC channel."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol, cast

from offeragent_harness.models import thaw_json
from offeragent_harness.models.json_types import JsonValue
from offeragent_harness.ports import (
    CancellationToken,
    ClientToolCommitObservation,
    ClientToolCommitObservationPort,
    ClientToolCommitPathState,
    ClientToolInvocation,
    ClientToolPathState,
    ClientToolPreview,
    Clock,
    OperationCancelled,
)
from offeragent_harness.protocol._base import JsonObject as ProtocolJsonObject
from offeragent_harness.protocol._base import WireModel, validate_wire
from offeragent_harness.protocol.messages import (
    ClientToolCancelParams,
    ClientToolCommitObserveParams,
    ClientToolCommitObserveResult,
    ClientToolInvokeParams,
    ClientToolInvokeResult,
    ClientToolLookupParams,
    ClientToolLookupResult,
    ClientToolPreviewParams,
    ClientToolPreviewResult,
)
from offeragent_harness.tools import (
    SideEffect,
    SideEffectKind,
    SideEffectState,
    ToolError,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
)
from offeragent_harness.tools.dispatcher import InvocationAcknowledgementLost


class ReverseRequestChannel(Protocol):
    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | WireModel,
        *,
        timeout_seconds: float | None = None,
    ) -> object: ...


class ReverseRequestChannelProvider(Protocol):
    """Returns the current authenticated plugin channel after reconnects."""

    def channel(self, workspace_id: str) -> ReverseRequestChannel: ...

    def connection_lease(
        self,
        workspace_id: str,
    ) -> AbstractAsyncContextManager[ReverseRequestChannel]: ...


class NamedPipeClientToolPort:
    def __init__(
        self,
        *,
        workspace_id: str,
        channels: ReverseRequestChannelProvider,
        clock: Clock,
        _pinned_channel: ReverseRequestChannel | None = None,
    ) -> None:
        if not workspace_id:
            raise ValueError("Client Tool bridge requires a Workspace")
        self._workspace_id = workspace_id
        self._channels = channels
        self._clock = clock
        self._pinned_channel = _pinned_channel
        self._run_by_invocation: dict[str, str] = {}

    @asynccontextmanager
    async def hold_connection(self) -> AsyncIterator[ClientToolCommitObservationPort]:
        """Yield a preview port pinned to one live authenticated connection.

        The provider linearizes lease acquisition against disconnect removal.
        Once acquired, the exact channel identity cannot be replaced until the
        caller leaves this context.
        """

        if self._pinned_channel is not None:
            raise RuntimeError("Client Tool connection lease cannot be nested")
        async with self._channels.connection_lease(self._workspace_id) as channel:
            yield NamedPipeClientToolPort(
                workspace_id=self._workspace_id,
                channels=self._channels,
                clock=self._clock,
                _pinned_channel=channel,
            )

    async def observe_commit(
        self,
        invocation: ClientToolInvocation,
        paths: tuple[str, ...],
        cancellation: CancellationToken,
    ) -> ClientToolCommitObservation:
        call = invocation.call
        if call.workspace_id != self._workspace_id:
            raise ValueError("Client Tool commit observation belongs to a different Workspace")
        if not paths:
            raise ValueError("Client Tool commit observation requires affected paths")
        cancellation.checkpoint()
        params = ClientToolCommitObserveParams(
            invocation_id=invocation.invocation_id,
            tool_call_id=call.tool_call_id,
            run_id=call.run_id,
            paths=list(paths),
            deadline=invocation.deadline.isoformat(),
            trace_id=self._trace_id(call.tool_call_id),
        )
        timeout = max(0.001, (invocation.deadline - self._clock.utcnow()).total_seconds())
        raw = await self._request(
            "client/tool/commit-observe",
            params,
            cancellation,
            timeout_seconds=timeout,
        )
        value = validate_wire(ClientToolCommitObserveResult, raw)
        if value.invocation_id != invocation.invocation_id or value.tool_call_id != call.tool_call_id:
            raise ValueError("Client Tool commit observation identity does not match the invocation")
        if tuple(value.paths) != paths:
            raise ValueError("Client Tool commit observation paths do not match the Worker plan")
        return ClientToolCommitObservation(
            invocation_id=value.invocation_id,
            tool_call_id=value.tool_call_id,
            paths=tuple(value.paths),
            has_unsaved_editors=value.has_unsaved_editors,
            has_open_editors=value.has_open_editors,
            path_states=tuple(
                ClientToolCommitPathState(
                    path=item.path,
                    observed_hash=item.observed_hash,
                    unsaved_editor=item.unsaved_editor,
                    open_editor=item.open_editor,
                )
                for item in value.path_states
            ),
        )

    async def invoke(self, invocation: ClientToolInvocation, cancellation: CancellationToken) -> ToolResult:
        call = invocation.call
        if call.workspace_id != self._workspace_id:
            raise ValueError("Client Tool invocation belongs to a different Workspace")
        cancellation.checkpoint()
        self._run_by_invocation[invocation.invocation_id] = call.run_id
        wire_name, wire_arguments = self._wire_call(invocation)
        params = ClientToolInvokeParams(
            invocation_id=invocation.invocation_id,
            tool_call_id=call.tool_call_id,
            run_id=call.run_id,
            name=wire_name,
            arguments=wire_arguments,
            args_hash=canonical_json_sha256(wire_arguments),
            idempotency_key=call.idempotency_key,
            deadline=invocation.deadline.isoformat(),
            trace_id=self._trace_id(call.tool_call_id),
        )
        timeout = max(0.001, (invocation.deadline - self._clock.utcnow()).total_seconds())
        try:
            raw = await self._request(
                "client/tool/invoke",
                params,
                cancellation,
                timeout_seconds=timeout,
            )
            validated = validate_wire(ClientToolInvokeResult, raw)
            if validated.invocation_id != invocation.invocation_id or validated.tool_call_id != call.tool_call_id:
                raise ValueError("Client Tool result identity does not match the invocation")
            return self._to_tool_result(validated)
        except Exception as invoke_error:
            cancellation.checkpoint()
            try:
                recovered = await self.lookup_result(invocation.invocation_id, run_id=call.run_id)
            except Exception:
                raise InvocationAcknowledgementLost(
                    "Client Tool ACK and reconnect lookup were both unavailable"
                ) from invoke_error
            if recovered is not None:
                return recovered
            raise InvocationAcknowledgementLost(
                "Client Tool ACK was lost and no durable result is visible"
            ) from invoke_error

    async def preview(
        self,
        invocation: ClientToolInvocation,
        cancellation: CancellationToken,
    ) -> ClientToolPreview:
        call = invocation.call
        if call.workspace_id != self._workspace_id:
            raise ValueError("Client Tool preview belongs to a different Workspace")
        cancellation.checkpoint()
        wire_name, wire_arguments = self._wire_call(invocation)
        params = ClientToolPreviewParams(
            invocation_id=invocation.invocation_id,
            tool_call_id=call.tool_call_id,
            run_id=call.run_id,
            name=wire_name,
            arguments=wire_arguments,
            args_hash=canonical_json_sha256(wire_arguments),
            deadline=invocation.deadline.isoformat(),
            trace_id=self._trace_id(call.tool_call_id),
        )
        timeout = max(0.001, (invocation.deadline - self._clock.utcnow()).total_seconds())
        raw = await self._request(
            "client/tool/preview",
            params,
            cancellation,
            timeout_seconds=timeout,
        )
        value = validate_wire(ClientToolPreviewResult, raw)
        if value.invocation_id != invocation.invocation_id or value.tool_call_id != call.tool_call_id:
            raise ValueError("Client Tool preview identity does not match the invocation")
        return ClientToolPreview(
            invocation_id=value.invocation_id,
            tool_call_id=value.tool_call_id,
            state_hash=value.state_hash,
            after_state_hash=value.after_state_hash,
            paths=tuple(value.paths),
            diff=value.diff.encode("utf-8"),
            diff_sha256=value.diff_sha256,
            has_unsaved_editors=value.has_unsaved_editors,
            has_open_editors=value.has_open_editors,
            path_states=tuple(
                ClientToolPathState(
                    path=item.path,
                    before_hash=item.before_hash,
                    after_hash=item.after_hash,
                    unsaved_editor=item.unsaved_editor,
                    open_editor=item.open_editor,
                )
                for item in value.path_states
            ),
        )

    async def cancel(self, invocation_id: str, reason: str) -> None:
        if not invocation_id or not reason:
            raise ValueError("Client Tool cancellation identity/reason must not be empty")
        run_id = self._run_by_invocation.get(invocation_id)
        if run_id is None:
            raise ValueError("Client Tool cancellation has no active Run binding")
        await self._channel().request(
            "client/tool/cancel",
            ClientToolCancelParams(invocation_id=invocation_id, run_id=run_id, reason=reason),
        )

    async def lookup_result(self, invocation_id: str, *, run_id: str | None = None) -> ToolResult | None:
        if not invocation_id:
            raise ValueError("Client Tool lookup invocation_id must not be empty")
        resolved_run_id = run_id or self._run_by_invocation.get(invocation_id)
        if resolved_run_id is None:
            raise ValueError("Client Tool lookup requires the authoritative Run identity")
        raw = await self._channel().request(
            "client/tool/lookup",
            ClientToolLookupParams(invocation_id=invocation_id, run_id=resolved_run_id),
        )
        result = validate_wire(ClientToolLookupResult, raw)
        if result.invocation_id != invocation_id:
            raise ValueError("Client Tool lookup response does not match the requested invocation")
        if not result.found:
            return None
        assert result.result is not None
        return self._to_tool_result(result.result)

    @staticmethod
    def _to_tool_result(value: ClientToolInvokeResult) -> ToolResult:
        status = (
            ToolResultStatus.CONFLICTED if value.status.value == "conflict" else ToolResultStatus(value.status.value)
        )
        effects = tuple(
            SideEffect(
                kind=_side_effect_kind(item.kind),
                state=SideEffectState(item.state),
                resource_id=item.resource_id,
                before_state=cast(JsonValue | None, item.before_state),
                after_state=cast(JsonValue | None, item.after_state),
                metadata=item.metadata,
            )
            for item in value.side_effect_facts
        )
        error = (
            None
            if value.error is None
            else ToolError(
                value.error.code,
                value.error.message,
                value.error.retryable,
                value.error.cancelled,
                value.error.details,
            )
        )
        return ToolResult(
            tool_call_id=value.tool_call_id,
            status=status,
            data=cast(JsonValue | None, value.output),
            user_visible_summary=value.user_visible_summary,
            artifact_ids=tuple(value.artifact_ids),
            source_refs=tuple(value.source_reference_ids),
            side_effects=effects,
            retryable=value.error.retryable if value.error is not None else False,
            before_state=cast(JsonValue | None, value.before_state),
            after_state=cast(JsonValue | None, value.after_state),
            error=error,
        )

    @staticmethod
    def _trace_id(tool_call_id: str) -> str:
        return f"trace_{hashlib.sha256(tool_call_id.encode()).hexdigest()[:24]}"

    @staticmethod
    def _wire_call(invocation: ClientToolInvocation) -> tuple[str, ProtocolJsonObject]:
        """Translate public tools into private client executors at the IPC edge.

        ``obsidian.vault.transaction`` is intentionally not registered in the
        model-facing Tool Registry.  The plugin journal needs its own stable
        transaction identity, derived from the already durable invocation ID;
        models therefore cannot choose or collide that identity.
        """

        call = invocation.call
        arguments = thaw_json(call.arguments)
        if not isinstance(arguments, dict):
            raise TypeError("Client Tool arguments must be a JSON object")
        typed_arguments = cast(ProtocolJsonObject, arguments)
        if call.name != "vault.transaction":
            return call.name, typed_arguments
        if set(typed_arguments) != {"operations"}:
            raise ValueError("vault.transaction client route accepts exactly the public operations argument")
        transaction_digest = hashlib.sha256(invocation.invocation_id.encode()).hexdigest()[:24]
        return (
            "obsidian.vault.transaction",
            {"transactionId": f"tx_{transaction_digest}", "operations": typed_arguments["operations"]},
        )

    async def _request(
        self,
        method: str,
        params: WireModel,
        cancellation: CancellationToken,
        *,
        timeout_seconds: float,
    ) -> object:
        request = asyncio.create_task(
            self._channel().request(
                method,
                params,
                timeout_seconds=timeout_seconds,
            )
        )
        cancelled = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait({request, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if request in done:
                return request.result()
            reason = cancelled.result()
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            raise OperationCancelled(reason)
        finally:
            cancelled.cancel()

    def _channel(self) -> ReverseRequestChannel:
        if self._pinned_channel is not None:
            return self._pinned_channel
        return self._channels.channel(self._workspace_id)


def _side_effect_kind(value: str) -> SideEffectKind:
    try:
        return SideEffectKind(value)
    except ValueError:
        return SideEffectKind.EXTERNAL_SYSTEM


__all__ = ["NamedPipeClientToolPort", "ReverseRequestChannel", "ReverseRequestChannelProvider"]
