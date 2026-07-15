from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.config import HarnessConfig
from offeragent_harness.ports import ApplicationCommandContext, CancellationToken
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.errors import ProtocolViolation
from offeragent_harness.protocol.framing import LengthPrefixedJsonRpcDecoder, encode_frame
from offeragent_harness.protocol.jsonrpc import (
    EventNotification,
    JsonRpcErrorResponse,
    JsonRpcMessage,
    JsonRpcSuccessResponse,
    parse_jsonrpc_message,
)
from offeragent_harness.protocol.messages import (
    COMMAND_REGISTRY,
    InitializeResult,
    RuntimePingParams,
    RuntimePingResult,
    SecretMetadataSnapshot,
    SecretsPutParams,
    SecretsPutResult,
    SessionCreateResult,
)
from offeragent_harness.protocol.schemas import build_examples
from offeragent_harness.runtime.application_dispatcher import (
    ApplicationCommandHandler,
    RuntimeApplicationCommandDispatcher,
)
from offeragent_harness.runtime.application_domain_handlers import DomainCommandIdentity, _session_handlers
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.runtime.cancellation import CancellationScope
from offeragent_harness.runtime.named_pipe import (
    ConnectionRole,
    ConnectionState,
    DiscoveryMaterial,
    DiscoveryMaterialStore,
    DuplexJsonRpcConnection,
    HandshakeRejected,
    HandshakeReplayGuard,
    NamedPipeTransportConfig,
    RemoteRpcError,
    TransportBackpressure,
    TransportDisconnected,
    authenticate_client_stream,
    authenticate_server_stream,
)
from offeragent_harness.runtime.session_service import SessionLifecycleService
from offeragent_harness.runtime.turn_manager import TurnManager
from offeragent_harness.testing import DeterministicIdGenerator, ManualClock, RecordingEventSink


class XorProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + bytes(value ^ 0xA5 for value in plaintext)

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"protected:"):
            raise ValueError("not protected")
        return bytes(value ^ 0xA5 for value in ciphertext.removeprefix(b"protected:"))


class SequenceNonce:
    def __init__(self, start: int = 1) -> None:
        self.value = start

    def __call__(self, size: int) -> bytes:
        assert size == 32
        value = self.value
        self.value += 1
        return value.to_bytes(32, "big")


class FixedNonce:
    def __init__(self, value: int) -> None:
        self.value = value.to_bytes(32, "big")

    def __call__(self, size: int) -> bytes:
        assert size == 32
        return self.value


class MemoryPipeStream:
    def __init__(self, *, fragment_bytes: int = 2**31 - 1, write_delay: float = 0.0) -> None:
        self.fragment_bytes = fragment_bytes
        self.write_delay = write_delay
        self.peer: MemoryPipeStream | None = None
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.buffer = bytearray()
        self.closed = False
        self.writing = False
        self.concurrent_write_detected = False
        self.completed_writes: list[bytes] = []
        self.fail_next_write = False

    async def read(self, max_bytes: int) -> bytes:
        if self.buffer:
            result = bytes(self.buffer[:max_bytes])
            del self.buffer[:max_bytes]
            return result
        item = await self.incoming.get()
        if item is None:
            return b""
        self.buffer.extend(item)
        result = bytes(self.buffer[:max_bytes])
        del self.buffer[:max_bytes]
        return result

    async def write(self, data: bytes) -> None:
        if self.peer is None or self.peer.closed:
            raise OSError("memory pipe peer is closed")
        if self.writing:
            self.concurrent_write_detected = True
        self.writing = True
        try:
            if self.fail_next_write:
                self.fail_next_write = False
                raise OSError("injected memory pipe write failure")
            if self.write_delay:
                await asyncio.sleep(self.write_delay)
            for start in range(0, len(data), self.fragment_bytes):
                await self.peer.incoming.put(data[start : start + self.fragment_bytes])
                await asyncio.sleep(0)
            self.completed_writes.append(bytes(data))
        finally:
            self.writing = False

    def cancel_pending_io(self) -> None:
        self.incoming.put_nowait(None)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.incoming.put_nowait(None)
        if self.peer is not None:
            self.peer.incoming.put_nowait(None)


def memory_pipe_pair(
    *,
    fragment_bytes: int = 2**31 - 1,
    write_delay: float = 0.0,
) -> tuple[MemoryPipeStream, MemoryPipeStream]:
    first = MemoryPipeStream(fragment_bytes=fragment_bytes, write_delay=write_delay)
    second = MemoryPipeStream(fragment_bytes=fragment_bytes, write_delay=write_delay)
    first.peer = second
    second.peer = first
    return first, second


async def read_raw_message(stream: MemoryPipeStream) -> JsonRpcMessage:
    decoder = LengthPrefixedJsonRpcDecoder()
    while True:
        messages = decoder.feed(await stream.read(4096))
        if messages:
            assert len(messages) == 1
            return messages[0]


class ScriptedApplicationDispatcher:
    def __init__(self, *, initialize_result: Mapping[str, Any] | None = None) -> None:
        examples = build_examples()
        self.initialize_result = initialize_result or examples["initialize.response.json"]["result"]
        self.calls: list[str] = []
        self.contexts: list[ApplicationCommandContext] = []
        self.ready_gate_calls = 0
        self.block_method: str | None = None
        self.delay_method: str | None = None
        self.delay_seconds = 0.0
        self.blocked_started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.fail_method: str | None = None
        self.cancellations: list[CancellationToken] = []

    def require_ready(self) -> None:
        self.ready_gate_calls += 1

    async def dispatch(
        self,
        method: str,
        params: Mapping[str, Any],
        cancellation: CancellationToken,
        *,
        context: ApplicationCommandContext | None = None,
    ) -> object:
        self.cancellations.append(cancellation)
        if context is not None:
            self.contexts.append(context)
        self.calls.append(method)
        if method == self.fail_method:
            raise RuntimeError("injected application failure")
        if method == self.block_method:
            self.blocked_started.set()
            await cancellation.wait()
            self.cancelled.set()
            cancellation.checkpoint()
        if method == self.delay_method:
            await asyncio.sleep(self.delay_seconds)
        if method == "initialize":
            return self.initialize_result
        if method == "runtime/ping":
            return {
                "nonce": params["nonce"],
                "timestamp": "2026-07-13T00:00:00Z",
                "workerPid": 4242,
            }
        raise AssertionError(f"unexpected method: {method}")


def discovery_material(now: datetime) -> DiscoveryMaterial:
    return DiscoveryMaterial(
        pipe_name="\\\\.\\pipe\\OfferAgent." + "a" * 64,
        bootstrap_nonce=b"k" * 32,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )


async def initialized_connections(
    *,
    fragment_bytes: int = 3,
    config: NamedPipeTransportConfig | None = None,
    server_response_flushed: Callable[[str], None] | None = None,
    server_request_finalized: Callable[[str], None] | None = None,
) -> tuple[
    DuplexJsonRpcConnection,
    DuplexJsonRpcConnection,
    ScriptedApplicationDispatcher,
    ScriptedApplicationDispatcher,
    MemoryPipeStream,
    MemoryPipeStream,
]:
    client_stream, server_stream = memory_pipe_pair(fragment_bytes=fragment_bytes, write_delay=0.001)
    server_dispatcher = ScriptedApplicationDispatcher()
    client_dispatcher = ScriptedApplicationDispatcher()
    client = DuplexJsonRpcConnection(
        client_stream,
        role=ConnectionRole.CLIENT,
        dispatcher=client_dispatcher,
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
        config=config,
        nonce_source=SequenceNonce(1),
    )
    server = DuplexJsonRpcConnection(
        server_stream,
        role=ConnectionRole.SERVER,
        dispatcher=server_dispatcher,
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
        config=config,
        nonce_source=SequenceNonce(2),
        response_flushed=server_response_flushed,
        request_finalized=server_request_finalized,
    )
    await asyncio.gather(client.start(), server.start())
    params = cast(dict[str, Any], build_examples()["initialize.request.json"]["params"])
    result = await client.request("initialize", params, timeout_seconds=2)
    assert isinstance(result, InitializeResult)
    assert client.ready and server.ready
    return client, server, client_dispatcher, server_dispatcher, client_stream, server_stream


def test_discovery_material_is_protected_canonical_bounded_and_expires(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    store = DiscoveryMaterialStore(tmp_path / "runtime", protector=XorProtector(), nonce_source=SequenceNonce())
    issued = store.issue(now=now, lifetime=timedelta(minutes=1))

    protected = store.path.read_bytes()
    assert issued.pipe_name.encode() not in protected
    assert issued.bootstrap_nonce not in protected
    assert "bootstrap_nonce" not in repr(issued)
    assert store.load(now=now + timedelta(seconds=30)) == issued
    with pytest.raises(HandshakeRejected, match="not currently valid"):
        store.load(now=now + timedelta(minutes=1))


@pytest.mark.asyncio
async def test_challenge_response_handles_fragmentation_and_rejects_exact_replay() -> None:
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)

    def current() -> datetime:
        return now

    material = discovery_material(now)
    guard = HandshakeReplayGuard()

    first_client, first_server = memory_pipe_pair(fragment_bytes=1)
    await asyncio.gather(
        authenticate_server_stream(
            first_server,
            material,
            now=current,
            replay_guard=guard,
            nonce_source=FixedNonce(7),
        ),
        authenticate_client_stream(
            first_client,
            material,
            now=current,
            nonce_source=FixedNonce(9),
        ),
    )

    replay_client, replay_server = memory_pipe_pair(fragment_bytes=2)
    results = await asyncio.gather(
        authenticate_server_stream(
            replay_server,
            material,
            now=current,
            replay_guard=guard,
            nonce_source=FixedNonce(7),
        ),
        authenticate_client_stream(
            replay_client,
            material,
            now=current,
            nonce_source=FixedNonce(9),
        ),
        return_exceptions=True,
    )
    assert any(isinstance(result, HandshakeRejected) and "replay" in str(result) for result in results)


@pytest.mark.asyncio
async def test_handshake_deadlines_and_configuration_bounds_fail_closed() -> None:
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    material = discovery_material(now)

    def current() -> datetime:
        return now

    silent_client, _ = memory_pipe_pair()
    with pytest.raises(HandshakeRejected, match="deadline"):
        await authenticate_client_stream(
            silent_client,
            material,
            now=current,
            timeout_seconds=0.01,
        )

    for invalid_timeout in (0, -1):
        with pytest.raises(ValueError, match="positive"):
            await authenticate_client_stream(
                silent_client,
                material,
                now=current,
                timeout_seconds=invalid_timeout,
            )

    with pytest.raises(ValueError, match="max_entries"):
        HandshakeReplayGuard(max_entries=0)
    for invalid_max in (0, 1, 0x1_0000_0000):
        with pytest.raises(ValueError, match="out of range"):
            await authenticate_client_stream(
                silent_client,
                material,
                now=current,
                max_message_bytes=invalid_max,
            )


@pytest.mark.asyncio
async def test_initialize_frame_before_challenge_response_is_rejected() -> None:
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    material = discovery_material(now)

    def current() -> datetime:
        return now

    malicious_client, server = memory_pipe_pair()
    authentication = asyncio.create_task(
        authenticate_server_stream(
            server,
            material,
            now=current,
            replay_guard=HandshakeReplayGuard(),
        )
    )
    await malicious_client.write(encode_frame(build_examples()["initialize.request.json"]))
    with pytest.raises(HandshakeRejected, match="response envelope"):
        await authentication


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_message_bytes": 1},
        {"read_chunk_bytes": 0},
        {"max_outbound_frames": 0},
        {"max_abandoned_request_ids": 0},
        {"idle_timeout_seconds": 0},
    ],
)
def test_transport_configuration_rejects_unbounded_or_nonpositive_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        NamedPipeTransportConfig(**kwargs)


@pytest.mark.asyncio
async def test_initialize_gate_concurrent_requests_and_serialized_writes() -> None:
    client, server, client_dispatcher, server_dispatcher, client_stream, server_stream = await initialized_connections(
        fragment_bytes=1
    )
    try:
        ping_one, ping_two = await asyncio.gather(
            client.request("runtime/ping", {"nonce": "req_one"}),
            client.request("runtime/ping", {"nonce": "req_two"}),
        )

        assert isinstance(ping_one, RuntimePingResult)
        assert isinstance(ping_two, RuntimePingResult)
        assert ping_one.nonce == "req_one"
        assert ping_two.nonce == "req_two"
        assert server_dispatcher.calls == ["initialize", "runtime/ping", "runtime/ping"]
        assert client_dispatcher.calls == []
        assert server_dispatcher.ready_gate_calls == 3
        assert client_dispatcher.ready_gate_calls == 0
        assert not client_stream.concurrent_write_detected
        assert not server_stream.concurrent_write_detected
        assert client.pending_local_count == server.pending_local_count == 0
        assert client.pending_remote_count == server.pending_remote_count == 0
    finally:
        await asyncio.gather(client.close(), server.close())


@pytest.mark.asyncio
async def test_authenticated_pipe_preserves_secret_params_through_dispatcher_revalidation() -> None:
    fixture_secret = "fixture-deepseek-secret-not-a-real-key"
    received_secret: str | None = None
    received_ping_nonce: str | None = None

    class ReadyApplication:
        def require_ready(self) -> object:
            return self

    async def unexpected_handler(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        del cancellation, context
        raise AssertionError(f"unexpected handler received {type(raw).__name__}")

    async def initialize_handler(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel | Mapping[str, object]:
        del raw, cancellation
        assert context.transport == "windows-named-pipe"
        return cast(dict[str, object], build_examples()["initialize.response.json"]["result"])

    async def secret_handler(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        nonlocal received_secret
        cancellation.checkpoint()
        assert context.transport == "windows-named-pipe"
        assert isinstance(raw, SecretsPutParams)
        received_secret = raw.secret.get_secret_value()
        return SecretsPutResult(
            secret=SecretMetadataSnapshot(
                handle="secret:v1:0123456789abcdef0123456789abcdef",
                kind="model-provider",
                provider_id="deepseek",
                version=1,
                created_at="2026-07-14T00:00:00Z",
                rotated_at="2026-07-14T00:00:00Z",
            ),
            created=True,
        )

    async def ping_handler(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        nonlocal received_ping_nonce
        cancellation.checkpoint()
        assert context.transport == "windows-named-pipe"
        assert isinstance(raw, RuntimePingParams)
        received_ping_nonce = raw.nonce
        return RuntimePingResult(
            nonce=raw.nonce,
            timestamp="2026-07-14T00:00:00Z",
            worker_pid=4242,
        )

    handlers: dict[str, ApplicationCommandHandler] = {method: unexpected_handler for method in COMMAND_REGISTRY}
    handlers.update(
        {
            "initialize": initialize_handler,
            "secrets/put": secret_handler,
            "runtime/ping": ping_handler,
        }
    )
    server_dispatcher = RuntimeApplicationCommandDispatcher(
        application=ReadyApplication(),
        handlers=handlers,
    )
    client_stream, server_stream = memory_pipe_pair(fragment_bytes=3, write_delay=0.001)
    client = DuplexJsonRpcConnection(
        client_stream,
        role=ConnectionRole.CLIENT,
        dispatcher=ScriptedApplicationDispatcher(),
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
        nonce_source=SequenceNonce(1),
    )
    server = DuplexJsonRpcConnection(
        server_stream,
        role=ConnectionRole.SERVER,
        dispatcher=server_dispatcher,
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
        nonce_source=SequenceNonce(2),
    )
    await asyncio.gather(client.start(), server.start())
    try:
        initialize_params = cast(dict[str, Any], build_examples()["initialize.request.json"]["params"])
        assert isinstance(await client.request("initialize", initialize_params), InitializeResult)

        secret_result = await client.request(
            "secrets/put",
            {
                "providerId": "deepseek",
                "kind": "model-provider",
                "secret": fixture_secret,
                "handle": None,
                "expectedVersion": None,
            },
        )
        ping_result = await client.request("runtime/ping", {"nonce": "req_plain_ping"})

        assert isinstance(secret_result, SecretsPutResult)
        assert received_secret == fixture_secret
        assert received_secret != "**********"
        assert isinstance(ping_result, RuntimePingResult)
        assert ping_result.nonce == received_ping_nonce == "req_plain_ping"
    finally:
        await asyncio.gather(client.close(), server.close())


@pytest.mark.asyncio
async def test_authenticated_pipe_creates_distinct_default_title_sessions_on_one_connection(tmp_path: Path) -> None:
    workspace_id = "ws_pipe_default_sessions"
    profile_id = "profile_pipe_default_sessions"
    clock = ManualClock(datetime(2026, 7, 14, tzinfo=timezone.utc))
    unit_of_work = SqliteUnitOfWorkFactory(tmp_path / "default-title-sessions.sqlite")
    sessions = SessionLifecycleService(
        unit_of_work=unit_of_work,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        turn_manager=TurnManager(),
        approval_manager=ApprovalManager(unit_of_work=unit_of_work, clock=clock),
    )

    class ReadyApplication:
        def require_ready(self) -> object:
            return self

    class StaticConfig:
        async def snapshot(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(config=HarnessConfig())

    class SessionHarness:
        def __init__(self) -> None:
            self.sessions = sessions
            self.connection_ids: list[str] = []

        async def session_started(self, **values: object) -> None:
            cancellation = values["cancellation"]
            assert isinstance(cancellation, CancellationToken)
            cancellation.checkpoint()
            connection_id = values["connection_id"]
            assert isinstance(connection_id, str)
            self.connection_ids.append(connection_id)

    async def unexpected_handler(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        del cancellation, context
        raise AssertionError(f"unexpected handler received {type(raw).__name__}")

    async def initialize_handler(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel | Mapping[str, object]:
        del raw, cancellation
        assert context.transport == "windows-named-pipe"
        return cast(dict[str, object], build_examples()["initialize.response.json"]["result"])

    harness = SessionHarness()
    session_create = _session_handlers(
        identity=DomainCommandIdentity(workspace_id, profile_id, "managed_local", "actor_local"),
        harness=harness,  # type: ignore[arg-type]
        projections=object(),  # type: ignore[arg-type]
        config=StaticConfig(),  # type: ignore[arg-type]
    )["session/create"]
    handlers: dict[str, ApplicationCommandHandler] = {method: unexpected_handler for method in COMMAND_REGISTRY}
    handlers.update({"initialize": initialize_handler, "session/create": session_create})
    server_dispatcher = RuntimeApplicationCommandDispatcher(
        application=ReadyApplication(),
        handlers=handlers,
    )
    client_stream, server_stream = memory_pipe_pair(fragment_bytes=3, write_delay=0.001)
    client = DuplexJsonRpcConnection(
        client_stream,
        role=ConnectionRole.CLIENT,
        dispatcher=ScriptedApplicationDispatcher(),
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
        nonce_source=SequenceNonce(1),
    )
    server = DuplexJsonRpcConnection(
        server_stream,
        role=ConnectionRole.SERVER,
        dispatcher=server_dispatcher,
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
        nonce_source=SequenceNonce(2),
    )
    await asyncio.gather(client.start(), server.start())
    try:
        initialize_params = cast(dict[str, Any], build_examples()["initialize.request.json"]["params"])
        assert isinstance(await client.request("initialize", initialize_params), InitializeResult)

        first = await client.request(
            "session/create",
            {"title": None, "clientRequestId": "req_default_session_one"},
        )
        second = await client.request(
            "session/create",
            {"title": None, "clientRequestId": "req_default_session_two"},
        )

        assert isinstance(first, SessionCreateResult)
        assert isinstance(second, SessionCreateResult)
        assert first.created and second.created
        assert first.session.title == second.session.title == "新会话"
        assert first.session.session_id != second.session.session_id
        assert len(set(harness.connection_ids)) == 1
        assert harness.connection_ids == [server.connection_id, server.connection_id]
    finally:
        await asyncio.gather(client.close(), server.close())


@pytest.mark.asyncio
async def test_response_flushed_hook_runs_after_complete_frame_and_scope_cleanup_and_can_close_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed_scopes: set[int] = set()
    original_close = CancellationScope.close

    async def tracked_close(scope: CancellationScope) -> None:
        await original_close(scope)
        closed_scopes.add(id(scope))

    monkeypatch.setattr(CancellationScope, "close", tracked_close)
    holder: dict[str, Any] = {}
    hook_methods: list[str] = []
    hook_called = asyncio.Event()
    observation: dict[str, Any] = {}

    def response_flushed(method: str) -> None:
        # Initialization uses the same success path, but this assertion targets
        # the post-readiness application request that is about to stop Runtime.
        if method != "runtime/ping":
            return
        server = cast(DuplexJsonRpcConnection, holder["server"])
        dispatcher = cast(ScriptedApplicationDispatcher, holder["dispatcher"])
        stream = cast(MemoryPipeStream, holder["stream"])

        observation.update(
            pending_remote_count=server.pending_remote_count,
            scope_closed=bool(dispatcher.cancellations) and id(dispatcher.cancellations[-1]) in closed_scopes,
            stream_writing=stream.writing,
            completed_frame=stream.completed_writes[-1] if stream.completed_writes else None,
        )
        hook_methods.append(method)
        # This deliberately races the client's reader.  Since the complete
        # response frame was acknowledged first, closing the server must not
        # turn the already-successful request into a disconnect.
        holder["close_task"] = asyncio.create_task(server.close())
        hook_called.set()

    client, server, _, dispatcher, _, server_stream = await initialized_connections(
        fragment_bytes=1,
        server_response_flushed=response_flushed,
    )
    holder.update(server=server, dispatcher=dispatcher, stream=server_stream)
    try:
        result = await client.request("runtime/ping", {"nonce": "req_flush_then_close"})
        assert isinstance(result, RuntimePingResult)
        assert result.nonce == "req_flush_then_close"
        await asyncio.wait_for(hook_called.wait(), timeout=1)
        assert hook_methods == ["runtime/ping"]
        assert observation["pending_remote_count"] == 0
        assert observation["scope_closed"] is True
        assert observation["stream_writing"] is False

        frame = cast(bytes, observation["completed_frame"])
        decoder = LengthPrefixedJsonRpcDecoder()
        messages = decoder.feed(frame)
        decoder.end_of_stream()
        assert len(messages) == 1
        assert isinstance(messages[0], JsonRpcSuccessResponse)
        assert isinstance(messages[0].result, Mapping)
        assert messages[0].result["nonce"] == "req_flush_then_close"

        close_task = cast(asyncio.Task[None], holder["close_task"])
        await asyncio.wait_for(close_task, timeout=1)
        assert server.state is ConnectionState.CLOSED
    finally:
        await asyncio.gather(client.close(), server.close(), return_exceptions=True)


@pytest.mark.asyncio
async def test_error_response_does_not_trigger_response_flushed_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    hook_methods: list[str] = []
    initialize_hook_called = asyncio.Event()
    request_scope_closed = asyncio.Event()
    track_request_scope = False
    original_close = CancellationScope.close

    async def tracked_close(scope: CancellationScope) -> None:
        await original_close(scope)
        if track_request_scope:
            request_scope_closed.set()

    monkeypatch.setattr(CancellationScope, "close", tracked_close)

    def response_flushed(method: str) -> None:
        hook_methods.append(method)
        if method == "initialize":
            initialize_hook_called.set()

    client, server, _, dispatcher, _, _ = await initialized_connections(
        server_response_flushed=response_flushed,
    )
    # Ignore the successful initialize response.  The failing application
    # request below must send an error frame without claiming success-flushed.
    await asyncio.wait_for(initialize_hook_called.wait(), timeout=1)
    hook_methods.clear()
    track_request_scope = True
    dispatcher.fail_method = "runtime/ping"
    try:
        with pytest.raises(RemoteRpcError):
            await client.request("runtime/ping", {"nonce": "req_expected_error"})
        await asyncio.wait_for(request_scope_closed.wait(), timeout=1)
        assert server.pending_remote_count == 0
        assert hook_methods == []
        assert client.ready and server.ready
    finally:
        await asyncio.gather(client.close(), server.close())


@pytest.mark.asyncio
async def test_response_write_failure_still_finalizes_remote_request() -> None:
    finalized_methods: list[str] = []
    initialize_finalized = asyncio.Event()
    request_finalized = asyncio.Event()

    def finalized(method: str) -> None:
        finalized_methods.append(method)
        if method == "initialize":
            initialize_finalized.set()
        elif method == "runtime/ping":
            request_finalized.set()

    client, server, _, _, _, server_stream = await initialized_connections(
        server_request_finalized=finalized,
    )
    await asyncio.wait_for(initialize_finalized.wait(), timeout=1)
    finalized_methods.clear()
    server_stream.fail_next_write = True
    try:
        with pytest.raises(TransportDisconnected):
            await client.request("runtime/ping", {"nonce": "req_lost_ack"})
        await asyncio.wait_for(request_finalized.wait(), timeout=1)
        assert finalized_methods == ["runtime/ping"]
        assert server.pending_remote_count == 0
    finally:
        await asyncio.gather(client.close(), server.close(), return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_remote_request_still_emits_request_finalized() -> None:
    finalized_methods: list[str] = []
    initialize_finalized = asyncio.Event()
    request_finalized = asyncio.Event()

    def finalized(method: str) -> None:
        finalized_methods.append(method)
        if method == "initialize":
            initialize_finalized.set()
        elif method == "runtime/ping":
            request_finalized.set()

    client, server, _, dispatcher, _, _ = await initialized_connections(
        server_request_finalized=finalized,
    )
    await asyncio.wait_for(initialize_finalized.wait(), timeout=1)
    finalized_methods.clear()
    dispatcher.block_method = "runtime/ping"
    pending = asyncio.create_task(client.request("runtime/ping", {"nonce": "req_cancel_and_finalize"}))
    try:
        await asyncio.wait_for(dispatcher.blocked_started.wait(), timeout=1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.wait_for(dispatcher.cancelled.wait(), timeout=1)
        await asyncio.wait_for(request_finalized.wait(), timeout=1)
        assert finalized_methods == ["runtime/ping"]
        assert server.pending_remote_count == 0
    finally:
        await asyncio.gather(client.close(), server.close(), return_exceptions=True)


@pytest.mark.asyncio
async def test_non_initialize_request_is_rejected_before_application_dispatch() -> None:
    client_stream, server_stream = memory_pipe_pair()
    client_dispatcher = ScriptedApplicationDispatcher()
    server_dispatcher = ScriptedApplicationDispatcher()
    client = DuplexJsonRpcConnection(
        client_stream,
        role=ConnectionRole.CLIENT,
        dispatcher=client_dispatcher,
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
    )
    server = DuplexJsonRpcConnection(
        server_stream,
        role=ConnectionRole.SERVER,
        dispatcher=server_dispatcher,
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
    )
    await asyncio.gather(client.start(), server.start())
    try:
        with pytest.raises(Exception, match="initialize"):
            await client.request("runtime/ping", {"nonce": "too_early"})
        assert server_dispatcher.calls == []
    finally:
        await asyncio.gather(client.close(), server.close())


@pytest.mark.asyncio
async def test_connection_dispatches_the_explicit_stdio_authority() -> None:
    client_stream, server_stream = memory_pipe_pair()
    server_dispatcher = ScriptedApplicationDispatcher()
    client = DuplexJsonRpcConnection(
        client_stream,
        role=ConnectionRole.CLIENT,
        dispatcher=ScriptedApplicationDispatcher(),
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
    )
    server = DuplexJsonRpcConnection(
        server_stream,
        role=ConnectionRole.SERVER,
        dispatcher=server_dispatcher,
        command_transport="stdio-dev",
        command_peer="parent-process",
    )
    await asyncio.gather(client.start(), server.start())
    try:
        params = cast(dict[str, Any], build_examples()["initialize.request.json"]["params"])
        await client.request("initialize", params)
        assert server_dispatcher.contexts == [
            ApplicationCommandContext(
                transport="stdio-dev",
                client_id=server.connection_id,
                peer="parent-process",
            )
        ]
    finally:
        await asyncio.gather(client.close(), server.close())


@pytest.mark.asyncio
async def test_each_initialized_pipe_dispatches_with_its_unique_connection_identity() -> None:
    first = await initialized_connections()
    second = await initialized_connections()
    first_client, first_server, _, first_dispatcher, _, _ = first
    second_client, second_server, _, second_dispatcher, _, _ = second
    try:
        await first_client.request("runtime/ping", {"nonce": "req_first"})
        await second_client.request("runtime/ping", {"nonce": "req_second"})
        assert first_server.connection_id != second_server.connection_id
        assert {item.client_id for item in first_dispatcher.contexts} == {first_server.connection_id}
        assert {item.client_id for item in second_dispatcher.contexts} == {second_server.connection_id}
    finally:
        await asyncio.gather(first_client.close(), first_server.close(), second_client.close(), second_server.close())


@pytest.mark.asyncio
async def test_local_task_cancellation_propagates_without_stopping_the_read_loop() -> None:
    client, server, _, server_dispatcher, _, _ = await initialized_connections()
    server_dispatcher.block_method = "runtime/ping"
    pending = asyncio.create_task(client.request("runtime/ping", {"nonce": "req_cancel_me"}))
    try:
        await asyncio.wait_for(server_dispatcher.blocked_started.wait(), timeout=1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.wait_for(server_dispatcher.cancelled.wait(), timeout=1)
        await asyncio.sleep(0.02)
        assert client.ready and server.ready
        server_dispatcher.block_method = None
        result = await client.request("runtime/ping", {"nonce": "req_still_alive"})
        assert isinstance(result, RuntimePingResult)
    finally:
        await asyncio.gather(client.close(), server.close())


@pytest.mark.asyncio
async def test_request_timeouts_use_bounded_tombstones_and_ignore_late_responses() -> None:
    config = NamedPipeTransportConfig(
        max_inflight_requests_per_direction=1,
        max_abandoned_request_ids=2,
        request_timeout_seconds=0.01,
        idle_timeout_seconds=1,
    )
    client, server, _, server_dispatcher, _, _ = await initialized_connections(config=config)
    server_dispatcher.delay_method = "runtime/ping"
    server_dispatcher.delay_seconds = 0.03
    try:
        for index in range(5):
            with pytest.raises(asyncio.TimeoutError):
                await client.request("runtime/ping", {"nonce": f"req_timeout_{index}"})
            assert client.pending_local_count == 0
            await asyncio.sleep(0.04)
            assert client.ready and server.ready

        with pytest.raises(ValueError, match="positive"):
            await client.request("runtime/ping", {"nonce": "req_invalid_timeout"}, timeout_seconds=0)
        assert client.ready
    finally:
        await asyncio.gather(client.close(), server.close())


@pytest.mark.asyncio
async def test_invalid_initialize_does_not_stick_client_state_and_write_timeout_poisons() -> None:
    client_stream, server_stream = memory_pipe_pair()
    client = DuplexJsonRpcConnection(
        client_stream,
        role=ConnectionRole.CLIENT,
        dispatcher=ScriptedApplicationDispatcher(),
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
    )
    server = DuplexJsonRpcConnection(
        server_stream,
        role=ConnectionRole.SERVER,
        dispatcher=ScriptedApplicationDispatcher(),
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
    )
    await asyncio.gather(client.start(), server.start())
    try:
        with pytest.raises(ProtocolViolation):
            await client.request("initialize", {})
        assert client.state is ConnectionState.AUTHENTICATED
        params = cast(dict[str, Any], build_examples()["initialize.request.json"]["params"])
        assert isinstance(await client.request("initialize", params), InitializeResult)
    finally:
        await asyncio.gather(client.close(), server.close())

    config = NamedPipeTransportConfig(
        write_timeout_seconds=0.2,
        queue_timeout_seconds=0.2,
        idle_timeout_seconds=1,
    )
    slow_client, slow_server, _, _, slow_stream, _ = await initialized_connections(config=config)
    slow_stream.write_delay = 1
    try:
        with pytest.raises(TransportBackpressure, match="deadline"):
            await slow_client.request("runtime/ping", {"nonce": "req_slow_write"})
        assert slow_client.state is ConnectionState.POISONED
    finally:
        await slow_server.close()


@pytest.mark.asyncio
async def test_invalid_remote_initialize_does_not_stick_server_state_or_reach_dispatcher() -> None:
    client_stream, server_stream = memory_pipe_pair(fragment_bytes=2)
    server_dispatcher = ScriptedApplicationDispatcher()
    server = DuplexJsonRpcConnection(
        server_stream,
        role=ConnectionRole.SERVER,
        dispatcher=server_dispatcher,
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
    )
    await server.start()
    invalid = build_examples()["initialize.request.json"] | {"params": {}}
    await client_stream.write(encode_frame(invalid))
    response = await asyncio.wait_for(read_raw_message(client_stream), timeout=1)
    assert isinstance(response, JsonRpcErrorResponse)
    assert server.state is ConnectionState.AUTHENTICATED
    assert server_dispatcher.calls == []

    client = DuplexJsonRpcConnection(
        client_stream,
        role=ConnectionRole.CLIENT,
        dispatcher=ScriptedApplicationDispatcher(),
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
    )
    await client.start()
    try:
        params = cast(dict[str, Any], build_examples()["initialize.request.json"]["params"])
        assert isinstance(await client.request("initialize", params), InitializeResult)
        assert client.ready and server.ready
    finally:
        await asyncio.gather(client.close(), server.close())


@pytest.mark.asyncio
async def test_disconnect_propagates_to_inflight_dispatch_and_fails_all_pending() -> None:
    client, server, _, server_dispatcher, _, _ = await initialized_connections()
    server_dispatcher.block_method = "runtime/ping"
    pending = asyncio.create_task(client.request("runtime/ping", {"nonce": "req_disconnect"}))
    await asyncio.wait_for(server_dispatcher.blocked_started.wait(), timeout=1)
    await client.close()

    with pytest.raises(TransportDisconnected):
        await pending
    await asyncio.wait_for(server_dispatcher.cancelled.wait(), timeout=1)
    await asyncio.wait_for(server.wait_closed(), timeout=1)
    assert server.state is ConnectionState.CLOSED


@pytest.mark.asyncio
async def test_malformed_or_partial_frame_poison_closes_connection() -> None:
    config = NamedPipeTransportConfig(
        max_message_bytes=4096,
        read_chunk_bytes=4096,
        partial_frame_timeout_seconds=0.05,
        idle_timeout_seconds=1,
    )
    client, server, _, _, client_stream, _ = await initialized_connections(config=config)
    try:
        await client_stream.write(struct.pack(">I", 4097))
        await asyncio.wait_for(server.wait_closed(), timeout=1)
        assert server.state is ConnectionState.POISONED
    finally:
        await client.close()

    partial_client, partial_server = memory_pipe_pair()
    dispatcher = ScriptedApplicationDispatcher()
    connection = DuplexJsonRpcConnection(
        partial_server,
        role=ConnectionRole.SERVER,
        dispatcher=dispatcher,
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
        config=config,
    )
    await connection.start()
    await partial_client.write(encode_frame(build_examples()["initialize.request.json"])[:7])
    await asyncio.wait_for(connection.wait_closed(), timeout=1)
    assert connection.state is ConnectionState.CLOSED

    idle_client, idle_server = memory_pipe_pair()
    idle_connection = DuplexJsonRpcConnection(
        idle_server,
        role=ConnectionRole.SERVER,
        dispatcher=ScriptedApplicationDispatcher(),
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
        config=NamedPipeTransportConfig(idle_timeout_seconds=0.05),
    )
    await idle_connection.start()
    await asyncio.wait_for(idle_connection.wait_closed(), timeout=1)
    assert idle_connection.state is ConnectionState.CLOSED
    await idle_client.close()


@pytest.mark.asyncio
async def test_bounded_event_queue_applies_backpressure_and_disconnects() -> None:
    config = NamedPipeTransportConfig(
        max_buffered_events=1,
        queue_timeout_seconds=0.05,
        idle_timeout_seconds=1,
    )
    client, server, _, _, _, _ = await initialized_connections(config=config)
    event_message = parse_jsonrpc_message(build_examples()["tool-completed.event.json"])
    assert isinstance(event_message, EventNotification)
    envelope = event_message.params
    try:
        await server.send_event(envelope)
        await server.send_event(envelope.model_copy(update={"sequence": 2, "event_id": "evt_02"}))
        await asyncio.wait_for(client.wait_closed(), timeout=1)
        assert client.state is ConnectionState.POISONED
    finally:
        await server.close()
