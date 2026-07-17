"""Framed duplex JSON-RPC used by the direct stdio Worker transport."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.ports.application_commands import ApplicationCommandContext, ApplicationCommandDispatcher
from offeragent_harness.protocol.errors import ProtocolViolation, protocol_error
from offeragent_harness.protocol.events import EventEnvelope
from offeragent_harness.protocol.framing import DEFAULT_MAX_MESSAGE_BYTES, LengthPrefixedJsonRpcDecoder, encode_frame
from offeragent_harness.protocol.jsonrpc import (
    EventNotification,
    JsonRpcMessage,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    RpcCancelNotification,
    make_error_response,
    validate_request,
    validate_response,
)

from .application_errors import map_application_exception
from .cancellation import CancellationCode, CancellationReason, CancellationScope


class JsonRpcTransportError(RuntimeError):
    pass


class TransportDisconnected(JsonRpcTransportError):
    pass


class TransportBackpressure(JsonRpcTransportError):
    pass


@runtime_checkable
class DuplexByteStream(Protocol):
    """One connected full-duplex byte stream."""

    async def read(self, max_bytes: int) -> bytes: ...

    async def write(self, data: bytes) -> None: ...

    def cancel_pending_io(self) -> None: ...

    async def close(self) -> None: ...


class ConnectionRole(str, Enum):
    """The local Runtime currently accepts application requests as a server."""

    SERVER = "server"


class ConnectionState(str, Enum):
    CONNECTED = "connected"
    INITIALIZING = "initializing"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"
    POISONED = "poisoned"


@dataclass(frozen=True, slots=True)
class JsonRpcTransportConfig:
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    read_chunk_bytes: int = 64 * 1024
    max_outbound_frames: int = 128
    max_inflight_requests: int = 128
    queue_timeout_seconds: float = 5.0
    write_timeout_seconds: float = 30.0
    idle_timeout_seconds: float = 300.0
    partial_frame_timeout_seconds: float = 15.0
    shutdown_grace_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not 2 <= self.max_message_bytes <= 0xFFFFFFFF:
            raise ValueError("max_message_bytes is out of range")
        if not 1 <= self.read_chunk_bytes <= self.max_message_bytes + 4:
            raise ValueError("read_chunk_bytes is out of range")
        if self.max_outbound_frames < 1 or self.max_inflight_requests < 1:
            raise ValueError("transport queue and inflight limits must be positive")
        if any(
            timeout <= 0
            for timeout in (
                self.queue_timeout_seconds,
                self.write_timeout_seconds,
                self.idle_timeout_seconds,
                self.partial_frame_timeout_seconds,
                self.shutdown_grace_seconds,
            )
        ):
            raise ValueError("transport deadlines must be positive")


@dataclass(slots=True)
class _PendingRequest:
    scope: CancellationScope
    task: asyncio.Task[None]


@dataclass(slots=True)
class _OutboundFrame:
    payload: bytes
    acknowledgement: asyncio.Future[None]


class DuplexJsonRpcConnection:
    """Server-side direct-stream JSON-RPC with bounded queues and cancellation."""

    def __init__(
        self,
        stream: DuplexByteStream,
        *,
        role: ConnectionRole,
        dispatcher: ApplicationCommandDispatcher,
        command_transport: str,
        command_peer: str,
        config: JsonRpcTransportConfig | None = None,
        connection_id: str | None = None,
        response_flushed: Callable[[str], None] | None = None,
        request_finalized: Callable[[str], None] | None = None,
    ) -> None:
        if role is not ConnectionRole.SERVER:
            raise ValueError("direct Runtime JSON-RPC only supports the server role")
        self._stream = stream
        self._role = role
        self._dispatcher = dispatcher
        self._config = config or JsonRpcTransportConfig()
        self._response_flushed = response_flushed
        self._request_finalized = request_finalized
        self._connection_id = connection_id or f"stdio-{secrets.token_hex(16)}"
        if not 1 <= len(self._connection_id) <= 128 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in self._connection_id
        ):
            raise ValueError("JSON-RPC connection identity is invalid")
        self._command_context = ApplicationCommandContext(
            transport=command_transport,
            client_id=self._connection_id,
            peer=command_peer,
        )
        self._decoder = LengthPrefixedJsonRpcDecoder(max_message_bytes=self._config.max_message_bytes)
        self._pending: dict[int | str, _PendingRequest] = {}
        self._outbound: asyncio.Queue[_OutboundFrame] = asyncio.Queue(maxsize=self._config.max_outbound_frames)
        self._reader_task: asyncio.Task[None] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._closed = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._state = ConnectionState.CONNECTED

    @property
    def role(self) -> ConnectionRole:
        return self._role

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def ready(self) -> bool:
        return self._state is ConnectionState.READY

    @property
    def connection_id(self) -> str:
        return self._connection_id

    async def start(self) -> None:
        if self._reader_task is not None or self._closing:
            raise JsonRpcTransportError("connection can only be started once")
        self._writer_task = asyncio.create_task(self._writer_loop(), name="stdio-rpc-writer")
        self._reader_task = asyncio.create_task(self._reader_loop(), name="stdio-rpc-reader")

    async def send_event(self, event: EventEnvelope) -> None:
        if not self.ready:
            raise JsonRpcTransportError("events cannot be sent before initialize succeeds")
        await self._send_message(EventNotification(jsonrpc="2.0", method="event", params=event))

    async def wait_ready(self) -> None:
        if self.ready:
            return
        ready = asyncio.create_task(self._ready_event.wait(), name="stdio-wait-ready")
        closed = asyncio.create_task(self._closed.wait(), name="stdio-wait-ready-closed")
        try:
            done, _ = await asyncio.wait((ready, closed), return_when=asyncio.FIRST_COMPLETED)
            if closed in done and not self.ready:
                raise TransportDisconnected("JSON-RPC connection closed before initialize succeeded")
        finally:
            for task in (ready, closed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(ready, closed, return_exceptions=True)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        await self._terminate(TransportDisconnected("JSON-RPC connection closed"), poisoned=False)

    async def _send_message(self, message: object) -> None:
        if self._closing:
            raise TransportDisconnected("JSON-RPC connection is closing")
        acknowledgement: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        outbound = _OutboundFrame(
            payload=encode_frame(message, max_message_bytes=self._config.max_message_bytes),
            acknowledgement=acknowledgement,
        )
        try:
            await asyncio.wait_for(self._outbound.put(outbound), timeout=self._config.queue_timeout_seconds)
            await asyncio.wait_for(
                asyncio.shield(acknowledgement),
                timeout=self._config.write_timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            if not acknowledgement.done():
                acknowledgement.cancel()
            self._stream.cancel_pending_io()
            await self._terminate(
                TransportDisconnected("JSON-RPC write deadline made framing outcome unknown"),
                poisoned=True,
            )
            raise TransportBackpressure("JSON-RPC outbound queue/write deadline exceeded") from error

    async def _writer_loop(self) -> None:
        try:
            while True:
                outbound = await self._outbound.get()
                try:
                    await self._stream.write(outbound.payload)
                    if not outbound.acknowledgement.done():
                        outbound.acknowledgement.set_result(None)
                except asyncio.CancelledError:
                    if not outbound.acknowledgement.done():
                        outbound.acknowledgement.set_exception(TransportDisconnected("JSON-RPC writer stopped"))
                    raise
                except BaseException as error:
                    if not outbound.acknowledgement.done():
                        outbound.acknowledgement.set_exception(error)
                    raise
                finally:
                    self._outbound.task_done()
        except asyncio.CancelledError:
            return
        except BaseException as error:
            await self._terminate(TransportDisconnected("JSON-RPC writer failed"), poisoned=False, cause=error)

    async def _reader_loop(self) -> None:
        try:
            while True:
                partial = self._decoder.buffered_bytes > 0 or self._decoder.expected_length is not None
                timeout = self._config.partial_frame_timeout_seconds if partial else self._config.idle_timeout_seconds
                chunk = await asyncio.wait_for(self._stream.read(self._config.read_chunk_bytes), timeout=timeout)
                if not chunk:
                    self._decoder.end_of_stream()
                    raise TransportDisconnected("JSON-RPC peer disconnected")
                for message in self._decoder.feed(chunk):
                    self._accept_message(message)
        except asyncio.CancelledError:
            return
        except ProtocolViolation as error:
            await self._terminate(
                TransportDisconnected("JSON-RPC stream violated the protocol"),
                poisoned=True,
                cause=error,
            )
        except BaseException as error:
            self._stream.cancel_pending_io()
            await self._terminate(TransportDisconnected("JSON-RPC reader failed"), poisoned=False, cause=error)

    def _accept_message(self, message: JsonRpcMessage) -> None:
        if isinstance(message, JsonRpcRequest):
            self._accept_request(message)
            return
        if isinstance(message, RpcCancelNotification):
            self._accept_cancel(message)
            return
        raise protocol_error(
            ErrorCode.PROTOCOL_INVALID_REQUEST,
            "the direct Worker stream accepts only application requests and cancellation notifications",
        )

    def _accept_request(self, request: JsonRpcRequest) -> None:
        if len(self._pending) >= self._config.max_inflight_requests:
            raise protocol_error(ErrorCode.PROTOCOL_MESSAGE_TOO_LARGE, "remote pending request limit was exceeded")
        initializes = False
        if self._state is ConnectionState.CONNECTED and request.method == "initialize":
            initializes = True
        elif self._state is not ConnectionState.READY:
            raise protocol_error(ErrorCode.PROTOCOL_INVALID_REQUEST, "initialize must be the first request")
        elif request.method == "initialize":
            raise protocol_error(ErrorCode.PROTOCOL_INVALID_REQUEST, "initialize cannot be repeated")
        try:
            validate_request(request)
        except ProtocolViolation as error:
            self._track_background(
                asyncio.create_task(self._send_message(make_error_response(request.id, error)), name="stdio-rpc-reject")
            )
            return
        if request.id in self._pending:
            raise protocol_error(ErrorCode.PROTOCOL_INVALID_REQUEST, "request id is already in flight")
        scope = CancellationScope(name=f"stdio-request:{request.id}")
        task = asyncio.create_task(
            self._dispatch_request(request, scope, initializes=initializes),
            name=f"stdio-dispatch:{request.method}:{request.id}",
        )
        self._pending[request.id] = _PendingRequest(scope=scope, task=task)
        if initializes:
            self._state = ConnectionState.INITIALIZING

    async def _dispatch_request(
        self,
        request: JsonRpcRequest,
        scope: CancellationScope,
        *,
        initializes: bool,
    ) -> None:
        response_flushed = False
        try:
            self._dispatcher.require_ready()
            result = await self._dispatcher.dispatch(
                request.method,
                request.params,
                scope,
                context=self._command_context,
            )
            validated = validate_response(
                request.method,
                JsonRpcSuccessResponse.model_validate(
                    {"jsonrpc": "2.0", "id": request.id, "result": result},
                ),
            )
            response = JsonRpcSuccessResponse(
                jsonrpc="2.0",
                id=request.id,
                result=validated.result.to_wire(),
            )
            if initializes:
                self._state = ConnectionState.READY
            await self._send_message(response)
            response_flushed = True
            if initializes:
                self._ready_event.set()
        except (Exception, asyncio.CancelledError) as error:
            violation = map_application_exception(error)
            try:
                await self._send_message(make_error_response(request.id, violation))
            except JsonRpcTransportError:
                pass
            if initializes:
                self._schedule_termination(poisoned=True)
        finally:
            self._pending.pop(request.id, None)
            try:
                await scope.close()
            finally:
                if self._request_finalized is not None:
                    self._request_finalized(request.method)
        if response_flushed and self._response_flushed is not None:
            self._response_flushed(request.method)

    def _accept_cancel(self, message: RpcCancelNotification) -> None:
        pending = self._pending.get(message.params.request_id)
        if pending is None:
            return

        async def propagate() -> None:
            await pending.scope.cancel(
                CancellationReason.now(CancellationCode.USER, "remote transport request was cancelled")
            )

        self._track_background(asyncio.create_task(propagate(), name="stdio-cancel-propagation"))

    def _schedule_termination(self, *, poisoned: bool) -> None:
        self._track_background(
            asyncio.create_task(
                self._terminate(TransportDisconnected("JSON-RPC initialization failed"), poisoned=poisoned),
                name="stdio-initialize-failure",
            )
        )

    def _track_background(self, task: asyncio.Task[Any]) -> None:
        self._background.add(task)

        def consume(completed: asyncio.Task[Any]) -> None:
            self._background.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(consume)

    async def _terminate(
        self,
        reason: TransportDisconnected,
        *,
        poisoned: bool,
        cause: BaseException | None = None,
    ) -> None:
        del cause
        if self._closing:
            await self._closed.wait()
            return
        self._closing = True
        self._state = ConnectionState.POISONED if poisoned else ConnectionState.CLOSING
        self._stream.cancel_pending_io()
        current = asyncio.current_task()
        cancellation = CancellationReason.now(CancellationCode.SHUTDOWN, "JSON-RPC connection disconnected")
        scopes = [pending.scope.cancel(cancellation) for pending in tuple(self._pending.values())]
        if scopes:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*scopes, return_exceptions=True),
                    timeout=self._config.shutdown_grace_seconds,
                )
            except asyncio.TimeoutError:
                pass
        tasks: set[asyncio.Task[Any]] = set()
        for pending in tuple(self._pending.values()):
            if pending.task is not current:
                pending.task.cancel()
                tasks.add(pending.task)
        self._pending.clear()
        for task in (self._reader_task, self._writer_task, *tuple(self._background)):
            if task is not None and task is not current:
                task.cancel()
                tasks.add(task)
        while not self._outbound.empty():
            outbound = self._outbound.get_nowait()
            if not outbound.acknowledgement.done():
                outbound.acknowledgement.set_exception(reason)
            self._outbound.task_done()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self._config.shutdown_grace_seconds,
                )
            except asyncio.TimeoutError:
                pass
        try:
            await self._stream.close()
        finally:
            self._state = ConnectionState.POISONED if poisoned else ConnectionState.CLOSED
            self._closed.set()


__all__ = [
    "ConnectionRole",
    "ConnectionState",
    "DuplexByteStream",
    "DuplexJsonRpcConnection",
    "JsonRpcTransportConfig",
    "JsonRpcTransportError",
    "TransportBackpressure",
    "TransportDisconnected",
]
