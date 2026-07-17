from __future__ import annotations

import ast
import asyncio
import os
import struct
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.ports import ApplicationCommandContext, CancellationToken
from offeragent_harness.protocol.framing import LengthPrefixedJsonRpcDecoder, encode_frame
from offeragent_harness.protocol.jsonrpc import (
    EventNotification,
    JsonRpcErrorResponse,
    JsonRpcMessage,
    JsonRpcSuccessResponse,
    parse_jsonrpc_message,
)
from offeragent_harness.protocol.schemas import build_examples
from offeragent_harness.runtime import production_worker_composition as worker_composition
from offeragent_harness.runtime.duplex_json_rpc import (
    ConnectionRole,
    ConnectionState,
    DuplexJsonRpcConnection,
    JsonRpcTransportConfig,
    JsonRpcTransportError,
    TransportDisconnected,
)


class MemoryDuplexStream:
    def __init__(self, *, fragment_bytes: int = 2**31 - 1) -> None:
        self.fragment_bytes = fragment_bytes
        self.peer: MemoryDuplexStream | None = None
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.buffer = bytearray()
        self.closed = False
        self.completed_writes: list[bytes] = []

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
        peer = self.peer
        if self.closed or peer is None or peer.closed:
            raise BrokenPipeError("memory stdio peer is closed")
        for start in range(0, len(data), self.fragment_bytes):
            await peer.incoming.put(data[start : start + self.fragment_bytes])
            await asyncio.sleep(0)
        self.completed_writes.append(bytes(data))

    def cancel_pending_io(self) -> None:
        self.incoming.put_nowait(None)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.incoming.put_nowait(None)
        if self.peer is not None:
            self.peer.incoming.put_nowait(None)


def memory_stream_pair(*, fragment_bytes: int = 2**31 - 1) -> tuple[MemoryDuplexStream, MemoryDuplexStream]:
    client = MemoryDuplexStream(fragment_bytes=fragment_bytes)
    server = MemoryDuplexStream(fragment_bytes=fragment_bytes)
    client.peer = server
    server.peer = client
    return client, server


def _state(connection: DuplexJsonRpcConnection) -> ConnectionState:
    return connection.state


class FramedReader:
    def __init__(self, stream: MemoryDuplexStream) -> None:
        self._stream = stream
        self._decoder = LengthPrefixedJsonRpcDecoder()
        self._pending: deque[JsonRpcMessage] = deque()

    async def read(self) -> JsonRpcMessage:
        while not self._pending:
            chunk = await self._stream.read(4096)
            if not chunk:
                self._decoder.end_of_stream()
                raise EOFError("stdio peer closed")
            self._pending.extend(self._decoder.feed(chunk))
        return self._pending.popleft()


class ScriptedDispatcher:
    def __init__(self) -> None:
        self.initialize_result = build_examples()["initialize.response.json"]["result"]
        self.calls: list[str] = []
        self.contexts: list[ApplicationCommandContext] = []
        self.ready_gate_calls = 0
        self.block_method: str | None = None
        self.blocked = asyncio.Event()
        self.cancelled = asyncio.Event()

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
        self.calls.append(method)
        if context is not None:
            self.contexts.append(context)
        if method == self.block_method:
            self.blocked.set()
            await cancellation.wait()
            self.cancelled.set()
            cancellation.checkpoint()
        if method == "initialize":
            return self.initialize_result
        if method == "runtime/ping":
            return {
                "nonce": params["nonce"],
                "timestamp": "2026-07-17T00:00:00Z",
                "workerPid": 4242,
            }
        raise AssertionError(f"unexpected method: {method}")


def initialize_request() -> dict[str, object]:
    request = dict(build_examples()["initialize.request.json"])
    request["id"] = "initialize-1"
    return request


def ping_request(request_id: str, nonce: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "runtime/ping",
        "params": {"nonce": nonce},
    }


async def initialized_connection(
    *,
    response_flushed: list[str] | None = None,
    request_finalized: list[str] | None = None,
) -> tuple[DuplexJsonRpcConnection, ScriptedDispatcher, MemoryDuplexStream, FramedReader]:
    client, server = memory_stream_pair(fragment_bytes=3)
    dispatcher = ScriptedDispatcher()
    connection = DuplexJsonRpcConnection(
        server,
        role=ConnectionRole.SERVER,
        dispatcher=dispatcher,
        command_transport="stdio",
        command_peer="parent-process",
        connection_id="stdio-test-connection",
        response_flushed=None if response_flushed is None else response_flushed.append,
        request_finalized=None if request_finalized is None else request_finalized.append,
    )
    reader = FramedReader(client)
    await connection.start()
    await client.write(encode_frame(initialize_request()))
    response = await asyncio.wait_for(reader.read(), timeout=1)
    assert isinstance(response, JsonRpcSuccessResponse)
    await asyncio.wait_for(connection.wait_ready(), timeout=1)
    return connection, dispatcher, client, reader


async def wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


@pytest.mark.asyncio
async def test_initialize_request_event_notification_and_close_use_one_direct_stdio_authority() -> None:
    flushed: list[str] = []
    finalized: list[str] = []
    client, server = memory_stream_pair(fragment_bytes=2)
    dispatcher = ScriptedDispatcher()
    connection = DuplexJsonRpcConnection(
        server,
        role=ConnectionRole.SERVER,
        dispatcher=dispatcher,
        command_transport="stdio",
        command_peer="parent-process",
        connection_id="stdio-explicit-client",
        response_flushed=flushed.append,
        request_finalized=finalized.append,
    )
    event_message = parse_jsonrpc_message(build_examples()["tool-completed.event.json"])
    assert isinstance(event_message, EventNotification)
    await connection.start()
    try:
        with pytest.raises(JsonRpcTransportError, match="before initialize"):
            await connection.send_event(event_message.params)

        invalid = initialize_request() | {"params": {}}
        await client.write(encode_frame(invalid))
        reader = FramedReader(client)
        invalid_response = await asyncio.wait_for(reader.read(), timeout=1)
        assert isinstance(invalid_response, JsonRpcErrorResponse)
        assert connection.state is ConnectionState.CONNECTED
        assert dispatcher.calls == []

        await client.write(encode_frame(initialize_request()))
        initialized = await asyncio.wait_for(reader.read(), timeout=1)
        assert isinstance(initialized, JsonRpcSuccessResponse)
        await asyncio.wait_for(connection.wait_ready(), timeout=1)

        await client.write(encode_frame(ping_request("ping-1", "req_nonce_one")))
        ping = await asyncio.wait_for(reader.read(), timeout=1)
        assert isinstance(ping, JsonRpcSuccessResponse)
        assert isinstance(ping.result, Mapping)
        assert ping.result["nonce"] == "req_nonce_one"

        await connection.send_event(event_message.params)
        outbound_event = await asyncio.wait_for(reader.read(), timeout=1)
        assert isinstance(outbound_event, EventNotification)
        assert outbound_event.params == event_message.params
        await asyncio.sleep(0)

        assert dispatcher.calls == ["initialize", "runtime/ping"]
        assert dispatcher.contexts == [
            ApplicationCommandContext("stdio", "stdio-explicit-client", "parent-process"),
            ApplicationCommandContext("stdio", "stdio-explicit-client", "parent-process"),
        ]
        assert flushed == ["initialize", "runtime/ping"]
        assert finalized == ["initialize", "runtime/ping"]
    finally:
        await asyncio.gather(connection.close(), connection.close())
    assert _state(connection) is ConnectionState.CLOSED
    assert server.closed


@pytest.mark.asyncio
async def test_non_initialize_first_request_poisons_without_dispatching_application_code() -> None:
    client, server = memory_stream_pair()
    dispatcher = ScriptedDispatcher()
    connection = DuplexJsonRpcConnection(
        server,
        role=ConnectionRole.SERVER,
        dispatcher=dispatcher,
        command_transport="stdio",
        command_peer="parent-process",
    )
    await connection.start()
    await client.write(encode_frame(ping_request("too-early", "req_too_early")))

    await asyncio.wait_for(connection.wait_closed(), timeout=1)
    with pytest.raises(TransportDisconnected, match="before initialize"):
        await connection.wait_ready()
    assert connection.state is ConnectionState.POISONED
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_rpc_cancel_notification_finalizes_request_and_connection_remains_usable() -> None:
    finalized: list[str] = []
    connection, dispatcher, client, reader = await initialized_connection(request_finalized=finalized)
    try:
        dispatcher.block_method = "runtime/ping"
        await client.write(encode_frame(ping_request("cancel-me", "req_nonce_cancel")))
        await asyncio.wait_for(dispatcher.blocked.wait(), timeout=1)
        await client.write(
            encode_frame(
                {
                    "jsonrpc": "2.0",
                    "method": "rpc/cancel",
                    "params": {"requestId": "cancel-me"},
                }
            )
        )
        cancelled = await asyncio.wait_for(reader.read(), timeout=1)
        assert isinstance(cancelled, JsonRpcErrorResponse)
        assert cancelled.id == "cancel-me"
        assert cancelled.error.data.code is ErrorCode.REQUEST_CANCELLED
        await asyncio.wait_for(dispatcher.cancelled.wait(), timeout=1)
        assert connection.ready

        dispatcher.block_method = None
        await client.write(encode_frame(ping_request("still-live", "req_nonce_live")))
        response = await asyncio.wait_for(reader.read(), timeout=1)
        assert isinstance(response, JsonRpcSuccessResponse)
        assert response.id == "still-live"
        await wait_until(lambda: len(finalized) == 3)
        assert finalized == ["initialize", "runtime/ping", "runtime/ping"]
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_peer_eof_cancels_inflight_request_and_finalizes_it_once() -> None:
    finalized: list[str] = []
    connection, dispatcher, client, _ = await initialized_connection(request_finalized=finalized)
    dispatcher.block_method = "runtime/ping"
    await client.write(encode_frame(ping_request("lost-parent", "req_nonce_eof")))
    await asyncio.wait_for(dispatcher.blocked.wait(), timeout=1)
    await client.close()

    await asyncio.wait_for(connection.wait_closed(), timeout=1)
    await asyncio.wait_for(dispatcher.cancelled.wait(), timeout=1)
    assert connection.state is ConnectionState.CLOSED
    assert finalized == ["initialize", "runtime/ping"]


@pytest.mark.asyncio
async def test_partial_and_oversized_frames_close_with_stable_states() -> None:
    config = JsonRpcTransportConfig(
        max_message_bytes=4096,
        read_chunk_bytes=4096,
        idle_timeout_seconds=1,
        partial_frame_timeout_seconds=0.02,
    )
    partial_client, partial_server = memory_stream_pair()
    partial = DuplexJsonRpcConnection(
        partial_server,
        role=ConnectionRole.SERVER,
        dispatcher=ScriptedDispatcher(),
        command_transport="stdio",
        command_peer="parent-process",
        config=config,
    )
    await partial.start()
    await partial_client.write(encode_frame(initialize_request())[:7])
    await asyncio.wait_for(partial.wait_closed(), timeout=1)
    assert partial.state is ConnectionState.CLOSED

    oversized_client, oversized_server = memory_stream_pair()
    oversized = DuplexJsonRpcConnection(
        oversized_server,
        role=ConnectionRole.SERVER,
        dispatcher=ScriptedDispatcher(),
        command_transport="stdio",
        command_peer="parent-process",
        config=config,
    )
    await oversized.start()
    await oversized_client.write(struct.pack(">I", 4097))
    await asyncio.wait_for(oversized.wait_closed(), timeout=1)
    assert oversized.state is ConnectionState.POISONED


class RecordingEventHub:
    def __init__(self) -> None:
        self.added = asyncio.Event()
        self.connections: set[DuplexJsonRpcConnection] = set()

    async def add(self, connection: DuplexJsonRpcConnection) -> None:
        self.connections.add(connection)
        self.added.set()

    async def remove(self, connection: DuplexJsonRpcConnection) -> None:
        self.connections.discard(connection)


class StoppableApplication:
    def __init__(self) -> None:
        self.dispatcher = ScriptedDispatcher()
        self.event_hub = RecordingEventHub()
        self.stopped = asyncio.Event()
        self.finalized: list[str] = []

    def _application_request_finalized(self, method: str) -> None:
        self.finalized.append(method)

    async def wait_stopped(self) -> None:
        await self.stopped.wait()


@pytest.mark.asyncio
async def test_stdio_service_closes_after_application_shutdown_without_waiting_for_parent_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, server = memory_stream_pair(fragment_bytes=2)
    application = StoppableApplication()
    monkeypatch.setattr(worker_composition, "_StdioWorkerStream", lambda: server)
    serving = asyncio.create_task(worker_composition._serve_stdio_connection(application))  # type: ignore[arg-type]
    reader = FramedReader(client)
    try:
        await client.write(encode_frame(initialize_request()))
        response = await asyncio.wait_for(reader.read(), timeout=1)
        assert isinstance(response, JsonRpcSuccessResponse)
        await asyncio.wait_for(application.event_hub.added.wait(), timeout=1)

        application.stopped.set()
        await asyncio.wait_for(serving, timeout=1)
        assert server.closed
        assert application.event_hub.connections == set()
    finally:
        application.stopped.set()
        await asyncio.gather(serving, return_exceptions=True)


@pytest.mark.asyncio
async def test_stdio_write_fails_closed_when_the_os_reports_zero_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "set_blocking", lambda _fd, _blocking: None)
    stream = worker_composition._StdioWorkerStream()
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)
    with pytest.raises(BrokenPipeError, match="no progress"):
        await asyncio.wait_for(stream.write(b"payload"), timeout=1)
    await stream.close()


@pytest.mark.asyncio
async def test_stdio_cancellation_interrupts_nonblocking_read_and_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "set_blocking", lambda _fd, _blocking: None)
    monkeypatch.setattr(os, "read", lambda _fd, _maximum: (_ for _ in ()).throw(BlockingIOError()))
    monkeypatch.setattr(os, "write", lambda _fd, _data: (_ for _ in ()).throw(BlockingIOError()))
    stream = worker_composition._StdioWorkerStream()
    reading = asyncio.create_task(stream.read(1024))
    writing = asyncio.create_task(stream.write(b"payload"))
    await asyncio.sleep(0.01)

    stream.cancel_pending_io()

    assert await asyncio.wait_for(reading, timeout=1) == b""
    with pytest.raises(BrokenPipeError, match="closed"):
        await asyncio.wait_for(writing, timeout=1)


def test_stdio_requires_nonblocking_pipe_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_fd: int, _blocking: bool) -> None:
        raise OSError("not a pipe")

    monkeypatch.setattr(os, "set_blocking", fail)
    with pytest.raises(worker_composition.ProductionWorkerError, match="non-blocking pipe I/O"):
        worker_composition._StdioWorkerStream()


def test_direct_stdio_modules_have_no_formal_release_or_listener_dependencies() -> None:
    runtime = Path(worker_composition.__file__).parent
    imported_modules: set[str] = set()
    for path in (runtime / "duplex_json_rpc.py", runtime / "production_worker_composition.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_modules.update(
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
        )
    assert imported_modules.isdisjoint(
        {
            "offeragent_harness.runtime.host_supervisor",
            "offeragent_harness.runtime.named_pipe",
            "offeragent_harness.runtime.production_host_composition",
            "offeragent_harness.runtime.release_trust",
            "offeragent_harness.runtime.windows_named_pipe",
        }
    )
