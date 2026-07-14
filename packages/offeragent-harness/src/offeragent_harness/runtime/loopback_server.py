"""Minimal bounded HTTP/WebSocket listener for :mod:`loopback_gateway`."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import struct
from collections.abc import Callable
from contextlib import suppress

from .cancellation import CancellationCode, CancellationReason, CancellationScope
from .loopback_gateway import LoopbackRequest, LoopbackResponse, LoopbackSecurityError, LoopbackWebGateway

_REQUEST_LINE = re.compile(rb"^(GET|HEAD|POST) ([^ ]{1,4096}) HTTP/1\.1$")
_HEADER_LINE = re.compile(rb"^([!#$%&'*+.^_`|~0-9A-Za-z-]+):[ \t]*([^\r\n]*)$")
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class LoopbackListenerError(RuntimeError):
    pass


class AsyncioLoopbackServer:
    """Owns only local socket/framing; commands stay in the shared dispatcher."""

    def __init__(
        self,
        gateway: LoopbackWebGateway,
        *,
        response_flushed: Callable[[str], None] | None = None,
        request_finalized: Callable[[str], None] | None = None,
        terminal_response: Callable[[str], bool] | None = None,
    ) -> None:
        self._gateway = gateway
        self._response_flushed = response_flushed
        self._request_finalized = request_finalized
        self._terminal_response = terminal_response
        self._server: asyncio.AbstractServer | None = None
        self._closed_task: asyncio.Task[None] | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(gateway.config.max_connections)
        self._shutdown = CancellationScope(name="loopback-server")

    @property
    def healthy(self) -> bool:
        return (
            self._server is not None
            and self._server.is_serving()
            and self._closed_task is not None
            and not self._closed_task.done()
        )

    @property
    def closed_task(self) -> asyncio.Task[None] | None:
        return self._closed_task

    async def start(self) -> int:
        if self._server is not None:
            raise LoopbackListenerError("Loopback server already started")
        try:
            server = await asyncio.start_server(
                self._accept,
                host=self._gateway.config.host,
                port=0,
                limit=self._gateway.config.max_header_bytes,
                reuse_address=False,
                reuse_port=False,
                start_serving=False,
            )
        except OSError as error:
            raise LoopbackListenerError("could not bind random loopback port") from error
        sockets = server.sockets or ()
        if len(sockets) != 1:
            server.close()
            await server.wait_closed()
            raise LoopbackListenerError("Loopback listener did not produce exactly one socket")
        host, port = sockets[0].getsockname()[:2]
        try:
            self._gateway.bind_identity(str(host), int(port))
        except BaseException:
            server.close()
            await server.wait_closed()
            raise
        self._server = server
        await server.start_serving()
        self._closed_task = asyncio.create_task(server.wait_closed(), name="offeragent-loopback-listener")
        return int(port)

    async def stop(self) -> None:
        await self._shutdown.cancel(CancellationReason.now(CancellationCode.SHUTDOWN, "Loopback server stopped"))
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        closed_task = self._closed_task
        self._closed_task = None
        if closed_task is not None:
            await asyncio.gather(closed_task, return_exceptions=True)
        tasks = tuple(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        command_method: str | None = None
        try:
            async with self._semaphore:
                command_method = await self._serve(reader, writer)
        finally:
            if task is not None:
                self._connections.discard(task)
            writer.close()
            with suppress(Exception, asyncio.CancelledError):
                await writer.wait_closed()
        if command_method is not None and self._response_flushed is not None:
            # HTTP drain/WebSocket frame drain and connection close have all
            # completed.  Runtime teardown can no longer truncate the reply.
            self._response_flushed(command_method)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str | None:
        scope = self._shutdown.child("loopback-connection")
        request_method: str | None = None
        deadline = scope.schedule_deadline(
            self._gateway.config.request_timeout_seconds,
            message="Loopback client request timed out",
        )
        try:
            request = await asyncio.wait_for(
                self._read_request(reader, writer),
                timeout=self._gateway.config.request_timeout_seconds,
            )
            if request.headers.get("upgrade", "").lower() == "websocket":
                deadline.cancel()
                return await self._serve_websocket(reader, writer, request, scope)
            command = asyncio.create_task(self._gateway.handle(request, scope))
            disconnected = asyncio.create_task(reader.read(1))
            done, _ = await asyncio.wait({command, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done and disconnected.result() == b"":
                await scope.cancel(CancellationReason.now(CancellationCode.USER, "Loopback client disconnected"))
            response = await command
            request_method = response.request_method
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            await self._write_response(writer, response)
            return response.command_method
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ValueError):
            await self._write_response(
                writer,
                LoopbackResponse(
                    400,
                    {"Content-Type": "text/plain", "Cache-Control": "no-store", "Connection": "close"},
                    b"Bad Request",
                ),
            )
            return None
        finally:
            deadline.cancel()
            try:
                await scope.close()
            finally:
                if request_method is not None and self._request_finalized is not None:
                    self._request_finalized(request_method)

    async def _read_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> LoopbackRequest:
        raw_headers = await reader.readuntil(b"\r\n\r\n")
        if len(raw_headers) > self._gateway.config.max_header_bytes:
            raise ValueError("headers exceed limit")
        lines = raw_headers[:-4].split(b"\r\n")
        if not lines:
            raise ValueError("request line missing")
        match = _REQUEST_LINE.fullmatch(lines[0])
        if match is None:
            raise ValueError("request line malformed")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            header = _HEADER_LINE.fullmatch(line)
            if header is None:
                raise ValueError("header malformed")
            name = header.group(1).decode("ascii")
            lowered = name.lower()
            if lowered in headers:
                raise ValueError("duplicate header")
            headers[lowered] = header.group(2).decode("iso-8859-1")
        if "transfer-encoding" in headers:
            raise ValueError("transfer encoding is forbidden")
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length < 0 or length > self._gateway.config.max_body_bytes:
            raise ValueError("body length invalid")
        body = await reader.readexactly(length) if length else b""
        peer = writer.get_extra_info("peername")
        if not isinstance(peer, tuple) or not peer:
            raise ValueError("peer identity unavailable")
        return LoopbackRequest(
            match.group(1).decode("ascii"),
            match.group(2).decode("ascii"),
            headers,
            body,
            str(peer[0]),
        )

    async def _write_response(self, writer: asyncio.StreamWriter, response: LoopbackResponse) -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            409: "Conflict",
            413: "Payload Too Large",
            415: "Unsupported Media Type",
            421: "Misdirected Request",
            431: "Request Header Fields Too Large",
            499: "Client Closed Request",
            500: "Internal Server Error",
            503: "Service Unavailable",
            504: "Gateway Timeout",
        }.get(response.status, "Error")
        headers = dict(response.headers)
        headers["Connection"] = "close"
        headers.setdefault("Content-Length", str(len(response.body)))
        head = f"HTTP/1.1 {response.status} {reason}\r\n" + "".join(
            f"{name}: {value}\r\n" for name, value in headers.items()
        )
        writer.write(head.encode("iso-8859-1") + b"\r\n" + response.body)
        await writer.drain()

    async def _serve_websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: LoopbackRequest,
        scope: CancellationScope,
    ) -> str | None:
        if request.method != "GET" or request.target != "/ws" or request.headers.get("sec-websocket-version") != "13":
            raise ValueError("invalid WebSocket upgrade")
        key = request.headers.get("sec-websocket-key", "")
        try:
            decoded = base64.b64decode(key, validate=True)
        except ValueError as error:
            raise ValueError("invalid WebSocket key") from error
        if len(decoded) != 16:
            raise ValueError("invalid WebSocket key length")
        protocols = [item.strip() for item in request.headers.get("sec-websocket-protocol", "").split(",")]
        csrf_values = [item.removeprefix("csrf.") for item in protocols if item.startswith("csrf.")]
        if "offeragent.v1" not in protocols or len(csrf_values) != 1:
            raise LoopbackSecurityError(403, "websocket_auth_invalid", "WebSocket subprotocol authentication failed")
        self._gateway.authorize_websocket(
            host=request.headers.get("host", ""),
            origin=request.headers.get("origin", ""),
            peer_ip=request.peer_ip,
            cookie_header=request.headers.get("cookie", ""),
            csrf_token=csrf_values[0],
        )
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "Sec-WebSocket-Protocol: offeragent.v1\r\n"
                "Cache-Control: no-store\r\n\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        while not scope.cancelled:
            opcode, payload = await self._read_ws_frame(reader)
            if opcode == 0x8:
                await self._write_ws_frame(writer, 0x8, b"")
                return None
            if opcode == 0x9:
                await self._write_ws_frame(writer, 0xA, payload)
                continue
            if opcode != 0x1:
                raise ValueError("unsupported WebSocket frame")
            response = await self._gateway.dispatch_websocket_command(
                host=request.headers.get("host", ""),
                origin=request.headers.get("origin", ""),
                peer_ip=request.peer_ip,
                cookie_header=request.headers.get("cookie", ""),
                csrf_token=csrf_values[0],
                payload=payload,
                cancellation=scope,
            )
            try:
                await self._write_ws_frame(writer, 0x1, response.payload)
            finally:
                if response.request_method is not None and self._request_finalized is not None:
                    self._request_finalized(response.request_method)
            if (
                response.command_method is not None
                and self._terminal_response is not None
                and self._terminal_response(response.command_method)
            ):
                return response.command_method
        return None

    async def _read_ws_frame(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        first, second = await reader.readexactly(2)
        if first & 0x80 == 0 or first & 0x70 or second & 0x80 == 0:
            raise ValueError("WebSocket frame must be final, unextended, and masked")
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await reader.readexactly(8))[0]
        if length > self._gateway.config.max_body_bytes:
            raise ValueError("WebSocket frame exceeds limit")
        mask = await reader.readexactly(4)
        payload = bytearray(await reader.readexactly(length))
        for index in range(length):
            payload[index] ^= mask[index % 4]
        return opcode, bytes(payload)

    @staticmethod
    async def _write_ws_frame(writer: asyncio.StreamWriter, opcode: int, payload: bytes) -> None:
        length = len(payload)
        prefix = (
            bytes((0x80 | opcode, length))
            if length < 126
            else (
                bytes((0x80 | opcode, 126)) + struct.pack("!H", length)
                if length <= 0xFFFF
                else bytes((0x80 | opcode, 127)) + struct.pack("!Q", length)
            )
        )
        writer.write(prefix + payload)
        await writer.drain()


__all__ = ["AsyncioLoopbackServer", "LoopbackListenerError"]
