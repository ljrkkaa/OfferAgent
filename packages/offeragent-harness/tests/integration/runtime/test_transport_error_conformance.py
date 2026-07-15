from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
from collections.abc import Callable, Mapping
from typing import Any, cast

import pytest

from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.ports import ApplicationCommandContext, CancellationToken
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.errors import ProtocolViolation
from offeragent_harness.protocol.messages import COMMAND_REGISTRY, InitializeResult
from offeragent_harness.protocol.schemas import build_examples
from offeragent_harness.runtime.application_dispatcher import (
    ApplicationCommandHandler,
    RuntimeApplicationCommandDispatcher,
)
from offeragent_harness.runtime.config_service import ConfigRevisionConflict
from offeragent_harness.runtime.loopback_gateway import LoopbackAsset, LoopbackGatewayConfig, LoopbackWebGateway
from offeragent_harness.runtime.loopback_server import AsyncioLoopbackServer
from offeragent_harness.runtime.named_pipe import (
    ConnectionRole,
    ConnectionState,
    DuplexJsonRpcConnection,
    RemoteRpcError,
)
from offeragent_harness.runtime.session_service import SessionNotFound
from offeragent_harness.testing import ManualCancellationToken, ManualClock


class _Ready:
    def require_ready(self) -> object:
        return self


class _FailureScript:
    def __init__(self) -> None:
        self.factory: Callable[[], BaseException] = lambda: RuntimeError("failure case was not selected")

    async def initialize(
        self,
        _raw: WireModel,
        _cancellation: CancellationToken,
        _context: ApplicationCommandContext,
    ) -> Mapping[str, object]:
        value = build_examples()["initialize.response.json"]["result"]
        assert isinstance(value, dict)
        return cast(dict[str, object], value)

    async def fail(
        self,
        _raw: WireModel,
        _cancellation: CancellationToken,
        _context: ApplicationCommandContext,
    ) -> WireModel:
        raise self.factory()


class _RejectServerRequests:
    def require_ready(self) -> None:
        return None

    async def dispatch(
        self,
        method: str,
        _params: Mapping[str, Any],
        _cancellation: CancellationToken,
        *,
        context: ApplicationCommandContext | None = None,
    ) -> object:
        del context
        raise AssertionError(f"unexpected server request: {method}")


class _MemoryPipeStream:
    def __init__(self) -> None:
        self.peer: _MemoryPipeStream | None = None
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.buffer = bytearray()
        self.closed = False

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
        await self.peer.incoming.put(bytes(data))

    def cancel_pending_io(self) -> None:
        self.incoming.put_nowait(None)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.incoming.put_nowait(None)
        if self.peer is not None:
            self.peer.incoming.put_nowait(None)


def _memory_pipe_pair() -> tuple[_MemoryPipeStream, _MemoryPipeStream]:
    first, second = _MemoryPipeStream(), _MemoryPipeStream()
    first.peer = second
    second.peer = first
    return first, second


def _asset() -> LoopbackAsset:
    content = b"<!doctype html><title>OfferAgent transport conformance</title>"
    return LoopbackAsset(
        "/",
        "text/html; charset=utf-8",
        content,
        f"sha256:{hashlib.sha256(content).hexdigest()}",
    )


async def _http_post_json(
    *,
    port: int,
    path: str,
    value: Mapping[str, object],
    origin: str,
    cookie: str | None = None,
    csrf_token: str | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    headers = {
        "Host": f"127.0.0.1:{port}",
        "Origin": origin,
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Connection": "close",
    }
    if cookie is not None:
        headers["Cookie"] = cookie
    if csrf_token is not None:
        headers["X-CSRF-Token"] = csrf_token
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        (
            f"POST {path} HTTP/1.1\r\n" + "".join(f"{name}: {item}\r\n" for name, item in headers.items()) + "\r\n"
        ).encode("iso-8859-1")
        + body
    )
    await writer.drain()
    try:
        status_line = (await reader.readline()).decode("ascii").rstrip("\r\n")
        protocol, raw_status, _reason = status_line.split(" ", maxsplit=2)
        assert protocol == "HTTP/1.1"
        response_headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line == b"\r\n":
                break
            name, separator, item = line.decode("iso-8859-1").rstrip("\r\n").partition(":")
            assert separator
            response_headers[name.casefold()] = item.strip()
        payload = await reader.readexactly(int(response_headers["content-length"]))
        decoded = json.loads(payload.decode())
        assert isinstance(decoded, dict)
        return int(raw_status), response_headers, cast(dict[str, Any], decoded)
    finally:
        writer.close()
        await writer.wait_closed()


async def _open_websocket(
    *,
    port: int,
    origin: str,
    cookie: str,
    csrf_token: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    key = base64.b64encode(b"0123456789abcdef").decode("ascii")
    writer.write(
        (
            "GET /ws HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: {origin}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Protocol: offeragent.v1, csrf.{csrf_token}\r\n"
            f"Cookie: {cookie}\r\n\r\n"
        ).encode("ascii")
    )
    await writer.drain()
    assert (await reader.readline()).startswith(b"HTTP/1.1 101 ")
    while await reader.readline() != b"\r\n":
        pass
    return reader, writer


async def _write_client_websocket_frame(writer: asyncio.StreamWriter, opcode: int, payload: bytes) -> None:
    mask = b"\x11\x22\x33\x44"
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | opcode, 0x80 | length))
    elif length <= 0xFFFF:
        header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", length)
    else:
        header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", length)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    writer.write(header + mask + masked)
    await writer.drain()


async def _read_server_websocket_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first, second = await reader.readexactly(2)
    assert first & 0x80 and not first & 0x70 and not second & 0x80
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    return first & 0x0F, await reader.readexactly(length)


@pytest.mark.asyncio
async def test_direct_pipe_http_and_websocket_share_sanitized_application_errors() -> None:
    script = _FailureScript()
    handlers: dict[str, ApplicationCommandHandler] = {method: script.fail for method in COMMAND_REGISTRY}
    handlers["initialize"] = script.initialize
    dispatcher = RuntimeApplicationCommandDispatcher(application=_Ready(), handlers=handlers)

    client_stream, server_stream = _memory_pipe_pair()
    client = DuplexJsonRpcConnection(
        client_stream,
        role=ConnectionRole.CLIENT,
        dispatcher=_RejectServerRequests(),
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
        connection_id="error-conformance-client",
    )
    pipe_server = DuplexJsonRpcConnection(
        server_stream,
        role=ConnectionRole.SERVER,
        dispatcher=dispatcher,
        command_transport="windows-named-pipe",
        command_peer="current-windows-sid",
        connection_id="error-conformance-server",
    )
    await asyncio.gather(client.start(), pipe_server.start())
    initialize_params = build_examples()["initialize.request.json"]["params"]
    assert isinstance(initialize_params, dict)
    initialized = await client.request("initialize", initialize_params)
    assert isinstance(initialized, InitializeResult)

    gateway = LoopbackWebGateway(
        config=LoopbackGatewayConfig(
            workspace_id="ws_xxx",
            workspace_instance_id="wsi_xxx",
            worker_pid=12042,
        ),
        clock=ManualClock(),
        dispatcher=dispatcher,
        assets=(_asset(),),
    )
    loopback = AsyncioLoopbackServer(gateway)
    port = await loopback.start()
    launch_token = gateway.issue_launch_url().rsplit("#", maxsplit=1)[1]
    auth_status, auth_headers, auth_payload = await _http_post_json(
        port=port,
        path="/auth/exchange",
        value={"token": launch_token},
        origin=gateway.origin,
    )
    assert auth_status == 200
    cookie = auth_headers["set-cookie"].split(";", maxsplit=1)[0]
    csrf_token = cast(str, auth_payload["csrfToken"])
    ws_reader, ws_writer = await _open_websocket(
        port=port,
        origin=gateway.origin,
        cookie=cookie,
        csrf_token=csrf_token,
    )

    cases: tuple[tuple[Callable[[], BaseException], ErrorCode, int, str], ...] = (
        (
            lambda: PermissionError("secret-permission-leak"),
            ErrorCode.POLICY_DENIED,
            403,
            "secret-permission-leak",
        ),
        (
            lambda: SessionNotFound("secret-not-found-leak"),
            ErrorCode.RESOURCE_NOT_FOUND,
            404,
            "secret-not-found-leak",
        ),
        (
            lambda: ConfigRevisionConflict(737373, 747474),
            ErrorCode.RESOURCE_CONFLICT,
            409,
            "737373",
        ),
        (
            lambda: asyncio.CancelledError("secret-cancel-leak"),
            ErrorCode.REQUEST_CANCELLED,
            499,
            "secret-cancel-leak",
        ),
        (
            lambda: RuntimeError("secret-unexpected-leak"),
            ErrorCode.INTERNAL_ERROR,
            500,
            "secret-unexpected-leak",
        ),
    )
    command = {"method": "runtime/status", "params": {}}
    try:
        for factory, expected_code, expected_status, forbidden in cases:
            script.factory = factory

            with pytest.raises(ProtocolViolation) as direct_rejected:
                await dispatcher.dispatch("runtime/status", {}, ManualCancellationToken())
            direct_error = direct_rejected.value.error.to_wire()

            with pytest.raises(RemoteRpcError) as pipe_rejected:
                await client.request("runtime/status", {})
            pipe_rpc_error = pipe_rejected.value.error.to_wire()
            pipe_error = pipe_rejected.value.error.data.to_wire()

            http_status, _headers, http_payload = await _http_post_json(
                port=port,
                path="/api/command",
                value=command,
                origin=gateway.origin,
                cookie=cookie,
                csrf_token=csrf_token,
            )

            await _write_client_websocket_frame(
                ws_writer,
                0x1,
                json.dumps(command, separators=(",", ":")).encode(),
            )
            ws_opcode, ws_payload = await asyncio.wait_for(_read_server_websocket_frame(ws_reader), timeout=5)
            assert ws_opcode == 0x1
            websocket_payload = json.loads(ws_payload.decode())

            assert direct_error["code"] == expected_code.value
            assert pipe_error == http_payload["error"] == websocket_payload["error"] == direct_error
            assert http_status == expected_status
            assert forbidden not in json.dumps(
                {
                    "direct": direct_error,
                    "directMessage": str(direct_rejected.value),
                    "pipe": pipe_rpc_error,
                    "pipeMessage": str(pipe_rejected.value),
                    "http": http_payload,
                    "websocket": websocket_payload,
                },
                ensure_ascii=False,
            )
            assert client.state is ConnectionState.READY
            assert pipe_server.state is ConnectionState.READY
    finally:
        await _write_client_websocket_frame(ws_writer, 0x8, b"")
        close_opcode, _payload = await _read_server_websocket_frame(ws_reader)
        assert close_opcode == 0x8
        ws_writer.close()
        await ws_writer.wait_closed()
        await loopback.stop()
        await asyncio.gather(client.close(), pipe_server.close())
