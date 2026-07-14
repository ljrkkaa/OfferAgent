"""Authenticated, fully duplex JSON-RPC transport primitives for Named Pipes.

The connection state machine is platform-neutral and accepts an injectable byte
stream.  Win32 handle creation and peer validation live in
``windows_named_pipe``; neither module owns application business behavior.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.ports.application_commands import ApplicationCommandContext, ApplicationCommandDispatcher
from offeragent_harness.protocol._base import JsonObject, WireModel
from offeragent_harness.protocol.errors import ProtocolViolation, protocol_error
from offeragent_harness.protocol.events import EventEnvelope
from offeragent_harness.protocol.framing import (
    DEFAULT_MAX_MESSAGE_BYTES,
    LengthPrefixedJsonRpcDecoder,
    encode_frame,
)
from offeragent_harness.protocol.jsonrpc import (
    BidirectionalRequestIds,
    EventNotification,
    JsonRpcError,
    JsonRpcErrorResponse,
    JsonRpcMessage,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    RequestDirection,
    RpcCancelNotification,
    RpcCancelParams,
    make_error_response,
    validate_request,
    validate_response,
)
from offeragent_harness.protocol.messages import CommandDirection

from .application_errors import map_application_exception
from .cancellation import CancellationCode, CancellationReason, CancellationScope

_PIPE_NAME = re.compile(r"^\\\\\.\\pipe\\OfferAgent\.[0-9a-f]{64}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_DISCOVERY_SCHEMA_VERSION = 1
_DISCOVERY_FILENAME = "named-pipe.discovery.dpapi"
_MAX_DISCOVERY_BYTES = 64 * 1024
_HANDSHAKE_DOMAIN = b"OfferAgent.NamedPipe.Handshake.v1"

NonceSource = Callable[[int], bytes]


class NamedPipeTransportError(RuntimeError):
    pass


class TransportDisconnected(NamedPipeTransportError):
    pass


class TransportBackpressure(NamedPipeTransportError):
    pass


class HandshakeRejected(NamedPipeTransportError):
    pass


class RemoteRpcError(NamedPipeTransportError):
    def __init__(self, error: JsonRpcError) -> None:
        self.error = error
        super().__init__(error.data.user_visible_message)


@runtime_checkable
class PipeByteStream(Protocol):
    """One connected full-duplex byte stream.

    Production implementations must run blocking I/O outside the Agent event
    loop and make ``cancel_pending_io`` safe from any thread.
    """

    async def read(self, max_bytes: int) -> bytes: ...

    async def write(self, data: bytes) -> None: ...

    def cancel_pending_io(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class MaterialProtector(Protocol):
    """Current-user protection boundary (DPAPI in production)."""

    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class DiscoveryMaterial:
    pipe_name: str
    bootstrap_nonce: bytes = field(repr=False)
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if _PIPE_NAME.fullmatch(self.pipe_name) is None:
            raise ValueError("discovery pipe name is not an OfferAgent random Named Pipe")
        if len(self.bootstrap_nonce) != 32:
            raise ValueError("discovery bootstrap nonce must contain exactly 256 bits")
        for value in (self.issued_at, self.expires_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("discovery timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("discovery expiry must be after issuance")

    def require_fresh(self, now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("handshake clock must be timezone-aware")
        if now < self.issued_at - timedelta(seconds=5) or now >= self.expires_at:
            raise HandshakeRejected("Named Pipe discovery material is not currently valid")


class DiscoveryMaterialStore:
    """DPAPI-protected discovery material confined to one Runtime directory."""

    def __init__(
        self,
        runtime_directory: Path,
        *,
        protector: MaterialProtector,
        nonce_source: NonceSource = secrets.token_bytes,
    ) -> None:
        self._runtime_directory = Path(os.path.abspath(os.fspath(runtime_directory.expanduser())))
        if os.name == "nt":
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise ValueError("LOCALAPPDATA is required for production Named Pipe discovery")
            local_root = os.path.normcase(os.path.abspath(local_app_data))
            candidate = os.path.normcase(os.path.abspath(self._runtime_directory))
            try:
                contained = os.path.commonpath((local_root, candidate)) == local_root
            except ValueError:
                contained = False
            if not contained:
                raise ValueError("Named Pipe discovery must stay under LOCALAPPDATA")
        self._protector = protector
        self._nonce_source = nonce_source

    @property
    def path(self) -> Path:
        return self._runtime_directory / _DISCOVERY_FILENAME

    def issue(self, *, now: datetime, lifetime: timedelta = timedelta(hours=24)) -> DiscoveryMaterial:
        if lifetime <= timedelta(0):
            raise ValueError("discovery lifetime must be positive")
        random_name = _nonce(self._nonce_source)
        bootstrap_nonce = _nonce(self._nonce_source)
        material = DiscoveryMaterial(
            pipe_name=f"\\\\.\\pipe\\OfferAgent.{random_name.hex()}",
            bootstrap_nonce=bootstrap_nonce,
            issued_at=now,
            expires_at=now + lifetime,
        )
        plaintext = _canonical_discovery_bytes(material)
        protected = self._protector.protect(plaintext)
        if not protected or len(protected) > _MAX_DISCOVERY_BYTES:
            raise HandshakeRejected("protected discovery material has an invalid size")
        self._runtime_directory.mkdir(parents=True, exist_ok=True)
        temporary = self._runtime_directory / f".{_DISCOVERY_FILENAME}.{secrets.token_hex(16)}.tmp"
        try:
            _write_private_file(temporary, protected)
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return material

    def load(self, *, now: datetime) -> DiscoveryMaterial:
        try:
            with self.path.open("rb", buffering=0) as stream:
                protected = stream.read(_MAX_DISCOVERY_BYTES + 1)
                if stream.read(1):
                    raise HandshakeRejected("discovery material exceeds its hard limit")
        except FileNotFoundError as error:
            raise HandshakeRejected("Named Pipe discovery material is missing") from error
        if not protected or len(protected) > _MAX_DISCOVERY_BYTES:
            raise HandshakeRejected("discovery material exceeds its hard limit")
        try:
            plaintext = self._protector.unprotect(protected)
            material = _parse_discovery_bytes(plaintext)
        except HandshakeRejected:
            raise
        except Exception as error:
            raise HandshakeRejected("Named Pipe discovery material could not be unprotected") from error
        material.require_fresh(now)
        return material

    def remove(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class HandshakeReplayGuard:
    """Connection-local challenges are one-shot until their expiry."""

    def __init__(self, *, max_entries: int = 4096) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("handshake replay guard max_entries must be positive")
        self._max_entries = max_entries
        self._seen: dict[bytes, datetime] = {}
        self._lock = asyncio.Lock()

    async def consume(self, fingerprint: bytes, *, expires_at: datetime, now: datetime) -> None:
        async with self._lock:
            self._seen = {key: expiry for key, expiry in self._seen.items() if expiry > now}
            if fingerprint in self._seen:
                raise HandshakeRejected("Named Pipe handshake replay was rejected")
            if len(self._seen) >= self._max_entries:
                raise HandshakeRejected("Named Pipe handshake replay guard is full")
            self._seen[fingerprint] = expires_at


async def authenticate_server_stream(
    stream: PipeByteStream,
    material: DiscoveryMaterial,
    *,
    now: Callable[[], datetime],
    replay_guard: HandshakeReplayGuard,
    nonce_source: NonceSource = secrets.token_bytes,
    timeout_seconds: float = 10.0,
    max_message_bytes: int = 16 * 1024,
) -> None:
    """Authenticate a just-connected client before application initialization."""

    _validate_handshake_limits(timeout_seconds, max_message_bytes)
    current = now()
    material.require_fresh(current)
    challenge = _nonce(nonce_source)
    challenge_id = f"auth_{_nonce(nonce_source).hex()}"
    expires_at = min(material.expires_at, current + timedelta(seconds=timeout_seconds))
    expires_text = expires_at.astimezone(timezone.utc).isoformat()
    server_proof = _handshake_proof(
        material.bootstrap_nonce,
        b"server",
        material.pipe_name.encode(),
        challenge,
        expires_text.encode(),
    )
    request = JsonRpcRequest(
        jsonrpc="2.0",
        id=challenge_id,
        method="transport/challenge",
        params={
            "challenge": _b64url(challenge),
            "expiresAt": expires_text,
            "serverProof": _b64url(server_proof),
        },
    )
    await _write_with_deadline(stream, encode_frame(request, max_message_bytes=max_message_bytes), timeout_seconds)
    response = await _read_single_handshake_message(
        stream,
        timeout_seconds=timeout_seconds,
        max_message_bytes=max_message_bytes,
    )
    if not isinstance(response, JsonRpcSuccessResponse) or response.id != challenge_id:
        raise HandshakeRejected("Named Pipe challenge received an invalid response envelope")
    result = response.result
    if not isinstance(result, dict) or set(result) != {"clientNonce", "clientProof"}:
        raise HandshakeRejected("Named Pipe challenge response has an invalid shape")
    client_nonce = _decode_nonce(result["clientNonce"])
    client_proof = _decode_nonce(result["clientProof"])
    expected = _handshake_proof(
        material.bootstrap_nonce,
        b"client",
        material.pipe_name.encode(),
        challenge,
        client_nonce,
        expires_text.encode(),
    )
    if not hmac.compare_digest(client_proof, expected):
        raise HandshakeRejected("Named Pipe challenge proof was rejected")
    validated_at = now()
    if validated_at >= expires_at:
        raise HandshakeRejected("Named Pipe challenge expired before validation")
    fingerprint = hashlib.sha256(challenge + client_nonce + client_proof).digest()
    await replay_guard.consume(fingerprint, expires_at=expires_at, now=validated_at)


async def authenticate_client_stream(
    stream: PipeByteStream,
    material: DiscoveryMaterial,
    *,
    now: Callable[[], datetime],
    nonce_source: NonceSource = secrets.token_bytes,
    timeout_seconds: float = 10.0,
    max_message_bytes: int = 16 * 1024,
) -> None:
    """Verify the Worker challenge and answer it exactly once."""

    _validate_handshake_limits(timeout_seconds, max_message_bytes)
    material.require_fresh(now())
    message = await _read_single_handshake_message(
        stream,
        timeout_seconds=timeout_seconds,
        max_message_bytes=max_message_bytes,
    )
    if not isinstance(message, JsonRpcRequest) or message.method != "transport/challenge":
        raise HandshakeRejected("Named Pipe server did not present the required challenge")
    params = message.params
    if set(params) != {"challenge", "expiresAt", "serverProof"}:
        raise HandshakeRejected("Named Pipe server challenge has an invalid shape")
    challenge = _decode_nonce(params["challenge"])
    server_proof = _decode_nonce(params["serverProof"])
    expires_text = params["expiresAt"]
    if not isinstance(expires_text, str):
        raise HandshakeRejected("Named Pipe challenge expiry is invalid")
    try:
        expires_at = datetime.fromisoformat(expires_text)
    except ValueError as error:
        raise HandshakeRejected("Named Pipe challenge expiry is invalid") from error
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise HandshakeRejected("Named Pipe challenge expiry is not timezone-aware")
    current = now()
    if current >= expires_at or expires_at > material.expires_at:
        raise HandshakeRejected("Named Pipe server challenge is expired or out of bounds")
    expected_server = _handshake_proof(
        material.bootstrap_nonce,
        b"server",
        material.pipe_name.encode(),
        challenge,
        expires_text.encode(),
    )
    if not hmac.compare_digest(server_proof, expected_server):
        raise HandshakeRejected("Named Pipe server proof was rejected")
    client_nonce = _nonce(nonce_source)
    client_proof = _handshake_proof(
        material.bootstrap_nonce,
        b"client",
        material.pipe_name.encode(),
        challenge,
        client_nonce,
        expires_text.encode(),
    )
    response = JsonRpcSuccessResponse(
        jsonrpc="2.0",
        id=message.id,
        result={"clientNonce": _b64url(client_nonce), "clientProof": _b64url(client_proof)},
    )
    await _write_with_deadline(stream, encode_frame(response, max_message_bytes=max_message_bytes), timeout_seconds)


class ConnectionRole(str, Enum):
    CLIENT = "client"
    SERVER = "server"


class ConnectionState(str, Enum):
    AUTHENTICATED = "authenticated"
    INITIALIZING = "initializing"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"
    POISONED = "poisoned"


@dataclass(frozen=True, slots=True)
class NamedPipeTransportConfig:
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    read_chunk_bytes: int = 64 * 1024
    max_outbound_frames: int = 128
    max_inflight_requests_per_direction: int = 128
    max_abandoned_request_ids: int = 256
    max_buffered_events: int = 256
    queue_timeout_seconds: float = 5.0
    write_timeout_seconds: float = 30.0
    request_timeout_seconds: float = 120.0
    idle_timeout_seconds: float = 300.0
    partial_frame_timeout_seconds: float = 15.0
    shutdown_grace_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not 2 <= self.max_message_bytes <= 0xFFFFFFFF:
            raise ValueError("max_message_bytes is out of range")
        if not 1 <= self.read_chunk_bytes <= self.max_message_bytes + 4:
            raise ValueError("read_chunk_bytes is out of range")
        for count in (
            self.max_outbound_frames,
            self.max_inflight_requests_per_direction,
            self.max_abandoned_request_ids,
            self.max_buffered_events,
        ):
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("transport queue and inflight limits must be positive integers")
        for timeout in (
            self.queue_timeout_seconds,
            self.write_timeout_seconds,
            self.request_timeout_seconds,
            self.idle_timeout_seconds,
            self.partial_frame_timeout_seconds,
            self.shutdown_grace_seconds,
        ):
            if timeout <= 0:
                raise ValueError("transport deadlines must be positive")


@dataclass(slots=True)
class _PendingLocalRequest:
    method: str
    future: asyncio.Future[object]


@dataclass(slots=True)
class _PendingRemoteRequest:
    scope: CancellationScope
    task: asyncio.Task[None]


@dataclass(slots=True)
class _OutboundFrame:
    payload: bytes
    acknowledgement: asyncio.Future[None]


class DuplexJsonRpcConnection:
    """One authenticated full-duplex JSON-RPC connection.

    Requests in each direction have independent pending-ID namespaces.  A single
    reader remains active while application handlers and reverse Client Tools run
    in separate tasks; a single writer serializes all frames.
    """

    def __init__(
        self,
        stream: PipeByteStream,
        *,
        role: ConnectionRole,
        dispatcher: ApplicationCommandDispatcher,
        config: NamedPipeTransportConfig | None = None,
        nonce_source: NonceSource = secrets.token_bytes,
        connection_id: str | None = None,
        response_flushed: Callable[[str], None] | None = None,
        request_finalized: Callable[[str], None] | None = None,
    ) -> None:
        self._stream = stream
        self._role = role
        self._dispatcher = dispatcher
        self._config = config or NamedPipeTransportConfig()
        self._nonce_source = nonce_source
        self._response_flushed = response_flushed
        self._request_finalized = request_finalized
        self._connection_id = connection_id or f"pipe-{role.value}-{secrets.token_hex(16)}"
        if not 1 <= len(self._connection_id) <= 128 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in self._connection_id
        ):
            raise ValueError("Named Pipe connection identity is invalid")
        self._decoder = LengthPrefixedJsonRpcDecoder(max_message_bytes=self._config.max_message_bytes)
        self._ids = BidirectionalRequestIds()
        self._pending_local: dict[int | str, _PendingLocalRequest] = {}
        self._pending_remote: dict[int | str, _PendingRemoteRequest] = {}
        self._abandoned_local_order: deque[int | str] = deque()
        self._abandoned_local: set[int | str] = set()
        self._outbound: asyncio.Queue[_OutboundFrame] = asyncio.Queue(maxsize=self._config.max_outbound_frames)
        self._events: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=self._config.max_buffered_events)
        self._background: set[asyncio.Task[Any]] = set()
        self._reader_task: asyncio.Task[None] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._state = ConnectionState.AUTHENTICATED

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

    @property
    def pending_local_count(self) -> int:
        return len(self._pending_local)

    @property
    def pending_remote_count(self) -> int:
        return len(self._pending_remote)

    async def start(self) -> None:
        if self._reader_task is not None or self._closing:
            raise NamedPipeTransportError("connection can only be started once")
        self._writer_task = asyncio.create_task(self._writer_loop(), name=f"pipe-{self._role.value}-writer")
        self._reader_task = asyncio.create_task(self._reader_loop(), name=f"pipe-{self._role.value}-reader")

    async def wait_ready(self) -> None:
        if self.ready:
            return
        ready = asyncio.create_task(self._ready_event.wait(), name="pipe-wait-ready")
        closed = asyncio.create_task(self._closed.wait(), name="pipe-wait-ready-closed")
        try:
            done, _ = await asyncio.wait((ready, closed), return_when=asyncio.FIRST_COMPLETED)
            if closed in done and not self.ready:
                raise TransportDisconnected("Named Pipe closed before initialize succeeded")
        finally:
            for task in (ready, closed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(ready, closed, return_exceptions=True)

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | WireModel,
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        self._require_started()
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        initializes = self._validate_local_request_state(method)
        if len(self._pending_local) >= self._config.max_inflight_requests_per_direction:
            raise TransportBackpressure("local pending request limit reached")
        request_id = self._new_request_id()
        params_value = params.to_wire() if isinstance(params, WireModel) else dict(params)
        request = JsonRpcRequest(jsonrpc="2.0", id=request_id, method=method, params=cast(JsonObject, params_value))
        validated = validate_request(request)
        self._validate_direction(validated.spec.direction, local=True)
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._ids.register(RequestDirection.LOCAL, request_id)
        self._pending_local[request_id] = _PendingLocalRequest(method=method, future=future)
        if initializes:
            self._state = ConnectionState.INITIALIZING
        try:
            await self._send_message(request)
            timeout = self._config.request_timeout_seconds if timeout_seconds is None else timeout_seconds
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._abandon_local_request(request_id)
            self._schedule_transport_cancel(request_id)
            if initializes:
                await self._terminate(
                    TransportDisconnected("initialize was cancelled or exceeded its deadline"),
                    poisoned=True,
                )
            raise
        except BaseException:
            pending = self._pending_local.pop(request_id, None)
            self._ids.complete(RequestDirection.LOCAL, request_id)
            if pending is not None and not pending.future.done():
                pending.future.cancel()
            if initializes and not self._closing:
                self._state = ConnectionState.AUTHENTICATED
            raise

    async def send_event(self, event: EventEnvelope) -> None:
        if not self.ready:
            raise NamedPipeTransportError("events cannot be sent before initialize succeeds")
        await self._send_message(EventNotification(jsonrpc="2.0", method="event", params=event))

    async def next_event(self, *, timeout_seconds: float | None = None) -> EventEnvelope:
        if timeout_seconds is None:
            return await self._events.get()
        return await asyncio.wait_for(self._events.get(), timeout=timeout_seconds)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        await self._terminate(TransportDisconnected("Named Pipe connection closed"), poisoned=False)

    def _require_started(self) -> None:
        if self._reader_task is None or self._writer_task is None or self._closing:
            raise TransportDisconnected("Named Pipe connection is not running")

    def _new_request_id(self) -> str:
        for _ in range(16):
            candidate = f"rpc_{_nonce(self._nonce_source).hex()}"
            if candidate not in self._pending_local:
                return candidate
        raise NamedPipeTransportError("could not allocate a unique local request ID")

    def _validate_local_request_state(self, method: str) -> bool:
        if self._state is ConnectionState.AUTHENTICATED:
            if self._role is not ConnectionRole.CLIENT or method != "initialize":
                raise NamedPipeTransportError("initialize must be the first client request")
            return True
        if self._state is ConnectionState.INITIALIZING:
            raise NamedPipeTransportError("initialize is still pending")
        if self._state is not ConnectionState.READY:
            raise TransportDisconnected("connection is not ready")
        if method == "initialize":
            raise NamedPipeTransportError("initialize cannot be repeated")
        return False

    def _validate_direction(self, direction: CommandDirection, *, local: bool) -> None:
        expected = (
            CommandDirection.CLIENT_TO_WORKER
            if (self._role is ConnectionRole.CLIENT) == local
            else CommandDirection.WORKER_TO_CLIENT
        )
        if direction is not expected:
            raise protocol_error(
                ErrorCode.PROTOCOL_INVALID_REQUEST,
                "JSON-RPC method is not permitted in this transport direction.",
                details={"expectedDirection": expected.value},
            )

    async def _send_message(self, message: object) -> None:
        if self._closing:
            raise TransportDisconnected("Named Pipe connection is closing")
        payload = encode_frame(message, max_message_bytes=self._config.max_message_bytes)
        acknowledgement: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        outbound = _OutboundFrame(payload=payload, acknowledgement=acknowledgement)
        try:
            await asyncio.wait_for(
                self._outbound.put(outbound),
                timeout=self._config.queue_timeout_seconds,
            )
            await asyncio.wait_for(
                asyncio.shield(acknowledgement),
                timeout=self._config.write_timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            if not acknowledgement.done():
                acknowledgement.cancel()
            self._stream.cancel_pending_io()
            await self._terminate(
                TransportDisconnected("Named Pipe write deadline made framing outcome unknown"),
                poisoned=True,
            )
            raise TransportBackpressure("Named Pipe outbound queue/write deadline exceeded") from error

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
                        outbound.acknowledgement.set_exception(TransportDisconnected("Named Pipe writer was stopped"))
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
            await self._terminate(TransportDisconnected("Named Pipe writer failed"), poisoned=False, cause=error)

    async def _reader_loop(self) -> None:
        try:
            while True:
                partial = self._decoder.buffered_bytes > 0 or self._decoder.expected_length is not None
                timeout = self._config.partial_frame_timeout_seconds if partial else self._config.idle_timeout_seconds
                chunk = await asyncio.wait_for(
                    self._stream.read(self._config.read_chunk_bytes),
                    timeout=timeout,
                )
                if not chunk:
                    self._decoder.end_of_stream()
                    raise TransportDisconnected("Named Pipe peer disconnected")
                for message in self._decoder.feed(chunk):
                    await self._accept_message(message)
        except asyncio.CancelledError:
            return
        except ProtocolViolation as error:
            await self._terminate(
                TransportDisconnected("Named Pipe stream was poisoned by a protocol violation"),
                poisoned=True,
                cause=error,
            )
        except BaseException as error:
            self._stream.cancel_pending_io()
            await self._terminate(TransportDisconnected("Named Pipe reader failed"), poisoned=False, cause=error)

    async def _accept_message(self, message: JsonRpcMessage) -> None:
        if isinstance(message, JsonRpcRequest):
            self._accept_remote_request(message)
            return
        if isinstance(message, RpcCancelNotification):
            self._accept_transport_cancel(message)
            return
        if isinstance(message, EventNotification):
            if not self.ready:
                raise protocol_error(
                    ErrorCode.PROTOCOL_INVALID_REQUEST,
                    "events cannot arrive before initialize succeeds",
                )
            try:
                self._events.put_nowait(message.params)
            except asyncio.QueueFull as error:
                raise protocol_error(
                    ErrorCode.PROTOCOL_MESSAGE_TOO_LARGE,
                    "event consumer backpressure limit was exceeded",
                ) from error
            return
        if isinstance(message, (JsonRpcSuccessResponse, JsonRpcErrorResponse)):
            self._accept_response(message)
            return
        raise protocol_error(ErrorCode.PROTOCOL_INVALID_REQUEST, "unsupported JSON-RPC transport message")

    def _accept_remote_request(self, request: JsonRpcRequest) -> None:
        if len(self._pending_remote) >= self._config.max_inflight_requests_per_direction:
            raise protocol_error(
                ErrorCode.PROTOCOL_MESSAGE_TOO_LARGE,
                "remote pending request limit was exceeded",
            )
        initializes = False
        if self._role is ConnectionRole.SERVER:
            if self._state is ConnectionState.AUTHENTICATED and request.method == "initialize":
                initializes = True
            elif self._state is not ConnectionState.READY:
                raise protocol_error(
                    ErrorCode.PROTOCOL_INVALID_REQUEST,
                    "initialize must be the first application request",
                )
            elif request.method == "initialize":
                raise protocol_error(ErrorCode.PROTOCOL_INVALID_REQUEST, "initialize cannot be repeated")
        elif self._state is not ConnectionState.READY:
            raise protocol_error(
                ErrorCode.PROTOCOL_INVALID_REQUEST,
                "reverse requests cannot arrive before initialize succeeds",
            )
        try:
            validated = validate_request(request)
            self._validate_direction(validated.spec.direction, local=False)
        except ProtocolViolation as error:
            self._track_background(
                asyncio.create_task(
                    self._send_message(make_error_response(request.id, error)),
                    name=f"pipe-reject:{request.id}",
                )
            )
            return
        self._ids.register(RequestDirection.REMOTE, request.id)
        scope = CancellationScope(name=f"pipe-request:{request.id}")
        try:
            task = asyncio.create_task(
                self._dispatch_remote_request(request, scope),
                name=f"pipe-dispatch:{request.method}:{request.id}",
            )
            self._pending_remote[request.id] = _PendingRemoteRequest(scope=scope, task=task)
            if initializes:
                self._state = ConnectionState.INITIALIZING
        except BaseException:
            self._ids.complete(RequestDirection.REMOTE, request.id)
            if initializes:
                self._state = ConnectionState.AUTHENTICATED
            raise

    async def _dispatch_remote_request(
        self,
        request: JsonRpcRequest,
        scope: CancellationScope,
    ) -> None:
        initialize = request.method == "initialize"
        response_flushed = False
        try:
            self._dispatcher.require_ready()
            result = await self._dispatcher.dispatch(
                request.method,
                # The dispatcher owns the second boundary validation.  Forward the
                # original JSON subtree instead of serializing the first validated
                # DTO: Pydantic intentionally renders SecretStr as "**********",
                # which would otherwise replace a real secrets/put value before the
                # application handler can move it into the DPAPI SecretStore.
                request.params,
                scope,
                context=ApplicationCommandContext(
                    transport="windows-named-pipe",
                    client_id=self._connection_id,
                    peer="current-windows-sid",
                ),
            )
            validated_result = validate_response(
                request.method,
                JsonRpcSuccessResponse(jsonrpc="2.0", id=request.id, result=cast(Any, result)),
            )
            response = JsonRpcSuccessResponse(
                jsonrpc="2.0",
                id=request.id,
                result=validated_result.result.to_wire(),
            )
            if initialize:
                self._state = ConnectionState.READY
            await self._send_message(response)
            response_flushed = True
            if initialize:
                self._ready_event.set()
        except (Exception, asyncio.CancelledError) as error:
            violation = map_application_exception(error)
            try:
                await self._send_message(make_error_response(request.id, violation))
            except NamedPipeTransportError:
                pass
            if initialize:
                self._schedule_termination(poisoned=True)
        finally:
            self._pending_remote.pop(request.id, None)
            self._ids.complete(RequestDirection.REMOTE, request.id)
            try:
                await scope.close()
            finally:
                if self._request_finalized is not None:
                    # Finalization is wider than successful flush: application
                    # shutdown must also terminate after peer cancellation or a
                    # write failure once this request cannot deliver a reply.
                    self._request_finalized(request.method)
        if response_flushed and self._response_flushed is not None:
            # The writer acknowledgement proves that the complete response
            # frame has left this connection.  Invoke lifecycle hooks only
            # after request-scope cleanup, so a hook may safely begin closing
            # the connection without cancelling the response or its handler.
            self._response_flushed(request.method)

    def _accept_transport_cancel(self, message: RpcCancelNotification) -> None:
        pending = self._pending_remote.get(message.params.request_id)
        if pending is None:
            return

        async def propagate() -> None:
            await pending.scope.cancel(
                CancellationReason.now(CancellationCode.USER, "remote transport request was cancelled")
            )

        self._track_background(asyncio.create_task(propagate(), name="pipe-cancel-propagation"))

    def _accept_response(self, response: JsonRpcSuccessResponse | JsonRpcErrorResponse) -> None:
        request_id = response.id
        if request_id is None:
            raise protocol_error(ErrorCode.PROTOCOL_INVALID_REQUEST, "response id cannot be null")
        if request_id in self._abandoned_local:
            self._abandoned_local.remove(request_id)
            try:
                self._abandoned_local_order.remove(request_id)
            except ValueError:
                pass
            return
        pending = self._pending_local.pop(request_id, None)
        if pending is None or not self._ids.complete(RequestDirection.LOCAL, request_id):
            raise protocol_error(
                ErrorCode.PROTOCOL_INVALID_REQUEST,
                "response does not match a pending local request",
            )
        if isinstance(response, JsonRpcErrorResponse):
            if not pending.future.done():
                pending.future.set_exception(RemoteRpcError(response.error))
            if pending.method == "initialize":
                self._schedule_termination(poisoned=True)
            return
        try:
            validated = validate_response(pending.method, response)
        except ProtocolViolation as error:
            if not pending.future.done():
                pending.future.set_exception(error)
            raise
        if pending.method == "initialize":
            self._state = ConnectionState.READY
            self._ready_event.set()
        if not pending.future.done():
            pending.future.set_result(validated.result)

    def _abandon_local_request(self, request_id: int | str) -> None:
        pending = self._pending_local.pop(request_id, None)
        self._ids.complete(RequestDirection.LOCAL, request_id)
        if pending is not None and not pending.future.done():
            pending.future.cancel()
        if request_id in self._abandoned_local:
            return
        while len(self._abandoned_local_order) >= self._config.max_abandoned_request_ids:
            expired = self._abandoned_local_order.popleft()
            self._abandoned_local.discard(expired)
        self._abandoned_local_order.append(request_id)
        self._abandoned_local.add(request_id)

    def _schedule_transport_cancel(self, request_id: int | str) -> None:
        if self._closing:
            return
        notification = RpcCancelNotification(
            jsonrpc="2.0",
            method="rpc/cancel",
            params=RpcCancelParams(request_id=request_id),
        )
        self._track_background(asyncio.create_task(self._send_message(notification), name=f"pipe-cancel:{request_id}"))

    def _schedule_termination(self, *, poisoned: bool) -> None:
        self._track_background(
            asyncio.create_task(
                self._terminate(
                    TransportDisconnected("Named Pipe initialization failed"),
                    poisoned=poisoned,
                ),
                name="pipe-initialize-failure",
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
        del cause  # Never stringify transport causes: they may contain peer-controlled material.
        if self._closing:
            await self._closed.wait()
            return
        self._closing = True
        self._state = ConnectionState.POISONED if poisoned else ConnectionState.CLOSING
        self._stream.cancel_pending_io()
        current = asyncio.current_task()

        cancellation = CancellationReason.now(CancellationCode.SHUTDOWN, "Named Pipe connection disconnected")
        scopes = [pending.scope.cancel(cancellation) for pending in tuple(self._pending_remote.values())]
        if scopes:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*scopes, return_exceptions=True),
                    timeout=self._config.shutdown_grace_seconds,
                )
            except asyncio.TimeoutError:
                pass
        tasks_to_join: set[asyncio.Task[Any]] = set()
        for remote in tuple(self._pending_remote.values()):
            if remote.task is not current:
                remote.task.cancel()
                tasks_to_join.add(remote.task)
        for local in tuple(self._pending_local.values()):
            if not local.future.done():
                local.future.set_exception(reason)
        self._pending_local.clear()
        self._pending_remote.clear()
        self._abandoned_local.clear()
        self._abandoned_local_order.clear()
        self._ids.clear()

        for task in (self._reader_task, self._writer_task):
            if task is not None and task is not current:
                task.cancel()
                tasks_to_join.add(task)
        for task in tuple(self._background):
            if task is not current:
                task.cancel()
                tasks_to_join.add(task)
        while not self._outbound.empty():
            outbound = self._outbound.get_nowait()
            if not outbound.acknowledgement.done():
                outbound.acknowledgement.set_exception(reason)
            self._outbound.task_done()
        if tasks_to_join:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_join, return_exceptions=True),
                    timeout=self._config.shutdown_grace_seconds,
                )
            except asyncio.TimeoutError:
                pass
        try:
            await self._stream.close()
        finally:
            self._state = ConnectionState.POISONED if poisoned else ConnectionState.CLOSED
            self._closed.set()


async def _read_single_handshake_message(
    stream: PipeByteStream,
    *,
    timeout_seconds: float,
    max_message_bytes: int,
) -> JsonRpcMessage:
    decoder = LengthPrefixedJsonRpcDecoder(max_message_bytes=max_message_bytes)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            stream.cancel_pending_io()
            raise HandshakeRejected("Named Pipe handshake read deadline expired")
        try:
            chunk = await asyncio.wait_for(stream.read(4096), timeout=remaining)
        except asyncio.TimeoutError as error:
            stream.cancel_pending_io()
            raise HandshakeRejected("Named Pipe handshake read deadline expired") from error
        if not chunk:
            try:
                decoder.end_of_stream()
            except ProtocolViolation as error:
                raise HandshakeRejected("Named Pipe handshake ended on a partial frame") from error
            raise HandshakeRejected("Named Pipe peer disconnected during handshake")
        try:
            messages = decoder.feed(chunk)
        except ProtocolViolation as error:
            raise HandshakeRejected("Named Pipe handshake contained a malformed frame") from error
        if messages:
            if len(messages) != 1 or decoder.buffered_bytes or decoder.expected_length is not None:
                raise HandshakeRejected("Named Pipe handshake attempted frame smuggling")
            return messages[0]


async def _write_with_deadline(stream: PipeByteStream, payload: bytes, timeout_seconds: float) -> None:
    try:
        await asyncio.wait_for(stream.write(payload), timeout=timeout_seconds)
    except asyncio.TimeoutError as error:
        stream.cancel_pending_io()
        raise HandshakeRejected("Named Pipe handshake write deadline expired") from error


def _canonical_discovery_bytes(material: DiscoveryMaterial) -> bytes:
    return (
        json.dumps(
            {
                "bootstrapNonce": _b64url(material.bootstrap_nonce),
                "expiresAt": material.expires_at.astimezone(timezone.utc).isoformat(),
                "issuedAt": material.issued_at.astimezone(timezone.utc).isoformat(),
                "pipeName": material.pipe_name,
                "schemaVersion": _DISCOVERY_SCHEMA_VERSION,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def serialize_discovery_material(material: DiscoveryMaterial) -> bytes:
    """Return the one canonical payload allowed on Host discovery fd 3."""

    return _canonical_discovery_bytes(material)


def parse_discovery_material(payload: bytes, *, now: datetime | None = None) -> DiscoveryMaterial:
    """Parse the canonical Host fd3 payload and optionally require freshness."""

    material = _parse_discovery_bytes(payload)
    if now is not None:
        material.require_fresh(now)
    return material


def _parse_discovery_bytes(payload: bytes) -> DiscoveryMaterial:
    if len(payload) > _MAX_DISCOVERY_BYTES:
        raise HandshakeRejected("unprotected discovery material exceeds its hard limit")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HandshakeRejected("unprotected discovery material is malformed") from error
    expected = {"bootstrapNonce", "expiresAt", "issuedAt", "pipeName", "schemaVersion"}
    if not isinstance(value, dict) or set(value) != expected or value["schemaVersion"] != _DISCOVERY_SCHEMA_VERSION:
        raise HandshakeRejected("unprotected discovery material has an unsupported schema")
    try:
        material = DiscoveryMaterial(
            pipe_name=_required_text(value["pipeName"]),
            bootstrap_nonce=_decode_nonce(value["bootstrapNonce"]),
            issued_at=datetime.fromisoformat(_required_text(value["issuedAt"])),
            expires_at=datetime.fromisoformat(_required_text(value["expiresAt"])),
        )
    except (TypeError, ValueError) as error:
        raise HandshakeRejected("unprotected discovery material has invalid fields") from error
    if _canonical_discovery_bytes(material) != payload:
        raise HandshakeRejected("unprotected discovery material is not canonical")
    return material


def _write_private_file(path: Path, content: bytes) -> None:
    if os.name == "nt":
        from .windows_named_pipe import write_current_user_only_file

        write_current_user_only_file(path, content)
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", buffering=0, closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _nonce(source: NonceSource) -> bytes:
    value = source(32)
    if not isinstance(value, bytes) or len(value) != 32:
        raise HandshakeRejected("nonce source did not return exactly 256 random bits")
    return value


def _validate_handshake_limits(timeout_seconds: float, max_message_bytes: int) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise TypeError("handshake timeout_seconds must be numeric")
    if timeout_seconds <= 0:
        raise ValueError("handshake timeout_seconds must be positive")
    if isinstance(max_message_bytes, bool) or not isinstance(max_message_bytes, int):
        raise TypeError("handshake max_message_bytes must be an integer")
    if not 2 <= max_message_bytes <= 0xFFFFFFFF:
        raise ValueError("handshake max_message_bytes is out of range")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_nonce(value: object) -> bytes:
    if not isinstance(value, str) or not _BASE64URL.fullmatch(value):
        raise HandshakeRejected("handshake nonce encoding is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as error:
        raise HandshakeRejected("handshake nonce encoding is invalid") from error
    if len(decoded) != 32 or _b64url(decoded) != value:
        raise HandshakeRejected("handshake nonce must contain exactly 256 canonical bits")
    return decoded


def _handshake_proof(key: bytes, label: bytes, *parts: bytes) -> bytes:
    digest = hmac.new(key, digestmod=hashlib.sha256)
    digest.update(_HANDSHAKE_DOMAIN)
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    for part in parts:
        digest.update(len(part).to_bytes(4, "big"))
        digest.update(part)
    return digest.digest()


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("discovery field must be non-empty text")
    return value


__all__ = [
    "ConnectionRole",
    "ConnectionState",
    "DiscoveryMaterial",
    "DiscoveryMaterialStore",
    "DuplexJsonRpcConnection",
    "HandshakeRejected",
    "HandshakeReplayGuard",
    "MaterialProtector",
    "NamedPipeTransportConfig",
    "NamedPipeTransportError",
    "PipeByteStream",
    "RemoteRpcError",
    "TransportBackpressure",
    "TransportDisconnected",
    "authenticate_client_stream",
    "authenticate_server_stream",
    "parse_discovery_material",
    "serialize_discovery_material",
]
