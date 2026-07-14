from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from typing import Any, cast

import pytest

from offeragent_harness.ports import ApplicationCommandContext
from offeragent_harness.protocol.messages import WebLaunchParams, WebLaunchResult
from offeragent_harness.runtime.application_handlers import web_launch_handlers
from offeragent_harness.runtime.loopback_gateway import (
    LoopbackAsset,
    LoopbackGatewayConfig,
    LoopbackRequest,
    LoopbackResponse,
    LoopbackWebGateway,
    LoopbackWebSocketResponse,
    load_packaged_web_assets,
)
from offeragent_harness.runtime.loopback_server import AsyncioLoopbackServer
from offeragent_harness.testing import ManualCancellationToken, ManualClock


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    def require_ready(self) -> None:
        return None

    async def dispatch(self, method: str, params: Any, cancellation: Any, *, context: Any = None) -> object:
        cancellation.checkpoint()
        self.calls.append((method, dict(params), context.transport))
        return {"method": method, "params": dict(params), "transport": context.transport}


class FailingDispatcher:
    def require_ready(self) -> None:
        return None

    async def dispatch(self, method: str, params: Any, cancellation: Any, *, context: Any = None) -> object:
        del method, params, cancellation, context
        raise RuntimeError("injected command failure")


class _FlushGateway:
    def __init__(
        self,
        *,
        http_response: LoopbackResponse | None = None,
        websocket_response: LoopbackWebSocketResponse | None = None,
    ) -> None:
        self.config = LoopbackGatewayConfig(
            workspace_id="ws_flush",
            workspace_instance_id="wsi_flush",
            worker_pid=321,
        )
        self.http_response = http_response
        self.websocket_response = websocket_response
        self.websocket_dispatches = 0

    async def handle(self, request: LoopbackRequest, cancellation: Any) -> LoopbackResponse:
        del request, cancellation
        if self.http_response is None:
            raise AssertionError("HTTP response was not configured")
        return self.http_response

    def authorize_websocket(self, **kwargs: Any) -> None:
        del kwargs

    async def dispatch_websocket_command(self, **kwargs: Any) -> LoopbackWebSocketResponse:
        del kwargs
        self.websocket_dispatches += 1
        if self.websocket_response is None:
            raise AssertionError("WebSocket response was not configured")
        return self.websocket_response


class _GatedWriter:
    def __init__(
        self,
        *,
        blocked_drain: int | None,
        block_close: bool = True,
        failed_drain: int | None = None,
    ) -> None:
        self.buffer = bytearray()
        self.blocked_drain = blocked_drain
        self.drain_calls = 0
        self.drain_entered = asyncio.Event()
        self.drain_release = asyncio.Event()
        self.close_called = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_completed = False
        self.failed_drain = failed_drain
        if not block_close:
            self.close_release.set()

    def get_extra_info(self, name: str) -> object:
        return ("127.0.0.1", 49152) if name == "peername" else None

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        self.drain_calls += 1
        if self.drain_calls == self.failed_drain:
            raise OSError("injected loopback drain failure")
        if self.drain_calls == self.blocked_drain:
            self.drain_entered.set()
            await self.drain_release.wait()

    def close(self) -> None:
        self.close_called.set()

    async def wait_closed(self) -> None:
        await self.close_release.wait()
        self.close_completed = True


def _asset() -> LoopbackAsset:
    content = b"<!doctype html><script src='/app.js'></script>"
    return LoopbackAsset("/", "text/html; charset=utf-8", content, f"sha256:{hashlib.sha256(content).hexdigest()}")


def _gateway(*, clock: ManualClock | None = None, dispatcher: Any = None) -> LoopbackWebGateway:
    gateway = LoopbackWebGateway(
        config=LoopbackGatewayConfig(workspace_id="ws_test", workspace_instance_id="wsi_test", worker_pid=123),
        clock=clock or ManualClock(),
        dispatcher=dispatcher or RecordingDispatcher(),
        assets=(_asset(),),
    )
    gateway.bind_identity("127.0.0.1", 54321)
    return gateway


def _request(
    gateway: LoopbackWebGateway,
    method: str,
    target: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    peer: str = "127.0.0.1",
) -> LoopbackRequest:
    values = {"Host": gateway.origin.removeprefix("http://")}
    if headers:
        values.update(headers)
    payload = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
    return LoopbackRequest(method, target, values, payload, peer)


async def _exchange(gateway: LoopbackWebGateway, token: str) -> tuple[str, str]:
    response = await gateway.handle(
        _request(
            gateway,
            "POST",
            "/auth/exchange",
            body={"token": token},
            headers={"Origin": gateway.origin, "Content-Type": "application/json"},
        ),
        ManualCancellationToken(),
    )
    assert response.status == 200
    cookie = response.headers["Set-Cookie"].split(";", maxsplit=1)[0]
    csrf = json.loads(response.body)["csrfToken"]
    return cookie, csrf


@pytest.mark.asyncio
async def test_fragment_token_is_one_time_and_cookie_is_strict_httponly() -> None:
    gateway = _gateway()
    token = gateway.issue_launch_url().split("#", maxsplit=1)[1]
    cookie, csrf = await _exchange(gateway, token)
    assert cookie.startswith("oa_session=") and csrf
    replay = await gateway.handle(
        _request(
            gateway,
            "POST",
            "/auth/exchange",
            body={"token": token},
            headers={"Origin": gateway.origin, "Content-Type": "application/json"},
        ),
        ManualCancellationToken(),
    )
    assert replay.status == 401
    fresh = gateway.issue_launch_url().split("#", maxsplit=1)[1]
    response = await gateway.handle(
        _request(
            gateway,
            "POST",
            "/auth/exchange",
            body={"token": fresh},
            headers={"Origin": gateway.origin, "Content-Type": "application/json"},
        ),
        ManualCancellationToken(),
    )
    assert "HttpOnly" in response.headers["Set-Cookie"] and "SameSite=Strict" in response.headers["Set-Cookie"]


@pytest.mark.asyncio
async def test_web_launch_command_uses_bound_gateway_identity_and_fragment_token() -> None:
    gateway = _gateway()
    handler = web_launch_handlers(gateway=gateway)["web/launch"]
    result = await handler(
        WebLaunchParams(),
        ManualCancellationToken(),
        ApplicationCommandContext(transport="windows-named-pipe", client_id="plugin_1"),
    )
    assert isinstance(result, WebLaunchResult)
    assert result.worker_pid == 123 and result.workspace_instance_id == "wsi_test"
    assert result.url.startswith("http://127.0.0.1:54321/#") and "?" not in result.url
    token = result.url.split("#", maxsplit=1)[1]
    cookie, csrf = await _exchange(gateway, token)
    assert cookie and csrf
    with pytest.raises(PermissionError, match="plugin Pipe"):
        await handler(
            WebLaunchParams(),
            ManualCancellationToken(),
            ApplicationCommandContext(transport="loopback-http"),
        )


@pytest.mark.asyncio
async def test_host_origin_peer_and_csrf_fail_closed() -> None:
    gateway = _gateway()
    token = gateway.issue_launch_url().split("#", maxsplit=1)[1]
    cookie, csrf = await _exchange(gateway, token)
    command = {"method": "runtime/ping", "params": {"nonce": "req_1"}}
    cases = (
        _request(gateway, "POST", "/api/command", body=command, headers={"Origin": gateway.origin, "Cookie": cookie}),
        _request(
            gateway,
            "POST",
            "/api/command",
            body=command,
            headers={"Origin": "http://evil.invalid", "Cookie": cookie, "X-CSRF-Token": csrf},
        ),
        _request(
            gateway,
            "POST",
            "/api/command",
            body=command,
            headers={"Origin": gateway.origin, "Cookie": cookie, "X-CSRF-Token": csrf, "Host": "localhost:54321"},
        ),
        _request(
            gateway,
            "POST",
            "/api/command",
            body=command,
            headers={"Origin": gateway.origin, "Cookie": cookie, "X-CSRF-Token": csrf},
            peer="192.168.1.10",
        ),
    )
    statuses = [(await gateway.handle(item, ManualCancellationToken())).status for item in cases]
    assert statuses == [403, 403, 421, 403]


@pytest.mark.asyncio
async def test_http_and_websocket_use_same_dispatcher_and_result_shape() -> None:
    dispatcher = RecordingDispatcher()
    gateway = _gateway(dispatcher=dispatcher)
    token = gateway.issue_launch_url().split("#", maxsplit=1)[1]
    cookie, csrf = await _exchange(gateway, token)
    command = {"method": "events/replay", "params": {"runId": "run_1", "afterSequence": 0}}
    http = await gateway.handle(
        _request(
            gateway,
            "POST",
            "/api/command",
            body=command,
            headers={"Origin": gateway.origin, "Cookie": cookie, "X-CSRF-Token": csrf},
        ),
        ManualCancellationToken(),
    )
    websocket = await gateway.handle_websocket_command(
        host=gateway.origin.removeprefix("http://"),
        origin=gateway.origin,
        peer_ip="127.0.0.1",
        cookie_header=cookie,
        csrf_token=csrf,
        payload=json.dumps(command, separators=(",", ":")).encode(),
        cancellation=ManualCancellationToken(),
    )
    assert json.loads(http.body)["result"]["method"] == json.loads(websocket)["result"]["method"]
    assert [item[2] for item in dispatcher.calls] == ["loopback-http", "loopback-websocket"]


@pytest.mark.asyncio
async def test_gateway_exposes_command_method_only_after_successful_dispatch() -> None:
    successful = _gateway(dispatcher=RecordingDispatcher())
    token = successful.issue_launch_url().split("#", maxsplit=1)[1]
    cookie, csrf = await _exchange(successful, token)
    command = {"method": "shutdown", "params": {"gracePeriodMs": 5000}}
    request = _request(
        successful,
        "POST",
        "/api/command",
        body=command,
        headers={"Origin": successful.origin, "Cookie": cookie, "X-CSRF-Token": csrf},
    )

    http_success = await successful.handle(request, ManualCancellationToken())
    websocket_success = await successful.dispatch_websocket_command(
        host=successful.origin.removeprefix("http://"),
        origin=successful.origin,
        peer_ip="127.0.0.1",
        cookie_header=cookie,
        csrf_token=csrf,
        payload=json.dumps(command, separators=(",", ":")).encode(),
        cancellation=ManualCancellationToken(),
    )
    assert http_success.status == 200 and http_success.command_method == "shutdown"
    assert websocket_success.command_method == "shutdown"

    failing = _gateway(dispatcher=FailingDispatcher())
    token = failing.issue_launch_url().split("#", maxsplit=1)[1]
    cookie, csrf = await _exchange(failing, token)
    request = _request(
        failing,
        "POST",
        "/api/command",
        body=command,
        headers={"Origin": failing.origin, "Cookie": cookie, "X-CSRF-Token": csrf},
    )
    http_error = await failing.handle(request, ManualCancellationToken())
    websocket_error = await failing.dispatch_websocket_command(
        host=failing.origin.removeprefix("http://"),
        origin=failing.origin,
        peer_ip="127.0.0.1",
        cookie_header=cookie,
        csrf_token=csrf,
        payload=json.dumps(command, separators=(",", ":")).encode(),
        cancellation=ManualCancellationToken(),
    )
    assert http_error.status == 500 and http_error.command_method is None
    assert websocket_error.command_method is None


@pytest.mark.asyncio
async def test_http_response_flushed_waits_for_drain_and_connection_close_before_shutdown() -> None:
    body = b'{"result":{"accepted":true}}'
    gateway = _FlushGateway(
        http_response=LoopbackResponse(
            200,
            {"Content-Type": "application/json", "Content-Length": str(len(body))},
            body,
            "shutdown",
        )
    )
    writer = _GatedWriter(blocked_drain=1)
    reader = asyncio.StreamReader()
    reader.feed_data(b"POST /api/command HTTP/1.1\r\nHost: 127.0.0.1:54321\r\nContent-Length: 2\r\n\r\n{}")
    callbacks: list[tuple[str, bool, bytes]] = []
    shutdown_tasks: list[asyncio.Task[None]] = []
    server: AsyncioLoopbackServer

    def response_flushed(method: str) -> None:
        callbacks.append((method, writer.close_completed, bytes(writer.buffer)))
        shutdown_tasks.append(asyncio.create_task(server.stop()))

    server = AsyncioLoopbackServer(cast(Any, gateway), response_flushed=response_flushed)
    serving = asyncio.create_task(server._accept(reader, cast(Any, writer)))

    await asyncio.wait_for(writer.drain_entered.wait(), timeout=1)
    assert callbacks == []
    assert writer.close_called.is_set() is False
    writer.drain_release.set()
    await asyncio.wait_for(writer.close_called.wait(), timeout=1)
    assert callbacks == []
    assert bytes(writer.buffer).endswith(body)
    writer.close_release.set()
    await asyncio.wait_for(serving, timeout=1)
    await asyncio.gather(*shutdown_tasks)

    assert callbacks == [("shutdown", True, bytes(writer.buffer))]
    assert b"HTTP/1.1 200 OK" in callbacks[0][2]
    assert callbacks[0][2].endswith(body)


@pytest.mark.asyncio
async def test_error_http_response_never_emits_response_flushed() -> None:
    body = b'{"error":{"code":"internal.error"}}'
    gateway = _FlushGateway(
        http_response=LoopbackResponse(
            500,
            {"Content-Type": "application/json", "Content-Length": str(len(body))},
            body,
        )
    )
    writer = _GatedWriter(blocked_drain=None, block_close=False)
    reader = asyncio.StreamReader()
    reader.feed_data(b"POST /api/command HTTP/1.1\r\nHost: 127.0.0.1:54321\r\nContent-Length: 2\r\n\r\n{}")
    callbacks: list[str] = []
    server = AsyncioLoopbackServer(
        cast(Any, gateway),
        response_flushed=callbacks.append,
    )

    await asyncio.wait_for(server._accept(reader, cast(Any, writer)), timeout=1)

    assert callbacks == []
    assert writer.close_completed is True
    assert bytes(writer.buffer).endswith(body)


@pytest.mark.asyncio
async def test_websocket_response_flushed_waits_for_frame_drain_and_connection_close() -> None:
    payload = b'{"result":{"accepted":true}}'
    gateway = _FlushGateway(websocket_response=LoopbackWebSocketResponse(payload, "shutdown"))
    writer = _GatedWriter(blocked_drain=2)
    reader = asyncio.StreamReader()
    key = "MDEyMzQ1Njc4OWFiY2RlZg=="
    request = (
        "GET /ws HTTP/1.1\r\n"
        "Host: 127.0.0.1:54321\r\n"
        "Origin: http://127.0.0.1:54321\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Protocol: offeragent.v1, csrf.test\r\n"
        "Cookie: oa_session=test\r\n\r\n"
    ).encode("ascii")
    command = b'{"method":"shutdown","params":{"gracePeriodMs":5000}}'
    mask = b"mask"
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(command))
    frame = bytes((0x81, 0x80 | len(command))) + mask + masked
    reader.feed_data(request + frame)
    callbacks: list[str] = []
    server = AsyncioLoopbackServer(
        cast(Any, gateway),
        response_flushed=callbacks.append,
        terminal_response=lambda method: method == "shutdown",
    )
    serving = asyncio.create_task(server._accept(reader, cast(Any, writer)))

    await asyncio.wait_for(writer.drain_entered.wait(), timeout=1)
    assert writer.drain_calls == 2
    assert callbacks == []
    assert writer.close_called.is_set() is False
    assert bytes(writer.buffer).endswith(bytes((0x81, len(payload))) + payload)
    writer.drain_release.set()
    await asyncio.wait_for(writer.close_called.wait(), timeout=1)
    assert callbacks == []
    writer.close_release.set()
    await asyncio.wait_for(serving, timeout=1)

    assert gateway.websocket_dispatches == 1
    assert writer.close_completed is True
    assert callbacks == ["shutdown"]


@pytest.mark.asyncio
async def test_http_shutdown_request_finalizes_when_response_drain_loses_ack() -> None:
    body = b'{"result":{"accepted":true}}'
    gateway = _FlushGateway(
        http_response=LoopbackResponse(
            200,
            {"Content-Type": "application/json", "Content-Length": str(len(body))},
            body,
            "shutdown",
            "shutdown",
        )
    )
    writer = _GatedWriter(blocked_drain=None, block_close=False, failed_drain=1)
    reader = asyncio.StreamReader()
    reader.feed_data(b"POST /api/command HTTP/1.1\r\nHost: 127.0.0.1:54321\r\nContent-Length: 2\r\n\r\n{}")
    finalized: list[str] = []
    flushed: list[str] = []
    server = AsyncioLoopbackServer(
        cast(Any, gateway),
        response_flushed=flushed.append,
        request_finalized=finalized.append,
    )

    with pytest.raises(OSError, match="loopback drain failure"):
        await asyncio.wait_for(server._accept(reader, cast(Any, writer)), timeout=1)

    assert finalized == ["shutdown"]
    assert flushed == []
    assert writer.close_completed is True
    assert bytes(writer.buffer).endswith(body)


@pytest.mark.asyncio
async def test_websocket_shutdown_request_finalizes_when_frame_drain_loses_ack() -> None:
    payload = b'{"result":{"accepted":true}}'
    gateway = _FlushGateway(websocket_response=LoopbackWebSocketResponse(payload, "shutdown", "shutdown"))
    writer = _GatedWriter(blocked_drain=None, block_close=False, failed_drain=2)
    reader = asyncio.StreamReader()
    key = "MDEyMzQ1Njc4OWFiY2RlZg=="
    request = (
        "GET /ws HTTP/1.1\r\n"
        "Host: 127.0.0.1:54321\r\n"
        "Origin: http://127.0.0.1:54321\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Protocol: offeragent.v1, csrf.test\r\n"
        "Cookie: oa_session=test\r\n\r\n"
    ).encode("ascii")
    command = b'{"method":"shutdown","params":{"gracePeriodMs":5000}}'
    mask = b"mask"
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(command))
    frame = bytes((0x81, 0x80 | len(command))) + mask + masked
    reader.feed_data(request + frame)
    finalized: list[str] = []
    flushed: list[str] = []
    server = AsyncioLoopbackServer(
        cast(Any, gateway),
        response_flushed=flushed.append,
        request_finalized=finalized.append,
        terminal_response=lambda method: method == "shutdown",
    )

    with pytest.raises(OSError, match="loopback drain failure"):
        await asyncio.wait_for(server._accept(reader, cast(Any, writer)), timeout=1)

    assert gateway.websocket_dispatches == 1
    assert finalized == ["shutdown"]
    assert flushed == []
    assert writer.close_completed is True
    assert bytes(writer.buffer).endswith(bytes((0x81, len(payload))) + payload)


@pytest.mark.asyncio
async def test_websocket_keeps_one_connection_for_multiple_nonterminal_commands() -> None:
    payload = b'{"result":{"nonce":"ok"}}'
    gateway = _FlushGateway(websocket_response=LoopbackWebSocketResponse(payload, "runtime/ping", "runtime/ping"))
    writer = _GatedWriter(blocked_drain=None, block_close=False)
    reader = asyncio.StreamReader()
    key = "MDEyMzQ1Njc4OWFiY2RlZg=="
    request = (
        "GET /ws HTTP/1.1\r\n"
        "Host: 127.0.0.1:54321\r\n"
        "Origin: http://127.0.0.1:54321\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Protocol: offeragent.v1, csrf.test\r\n"
        "Cookie: oa_session=test\r\n\r\n"
    ).encode("ascii")

    def client_frame(payload_bytes: bytes, mask: bytes) -> bytes:
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload_bytes))
        return bytes((0x81, 0x80 | len(payload_bytes))) + mask + masked

    first = b'{"method":"runtime/ping","params":{"nonce":"one"}}'
    second = b'{"method":"runtime/ping","params":{"nonce":"two"}}'
    close = bytes((0x88, 0x80)) + b"done"
    reader.feed_data(request + client_frame(first, b"one!") + client_frame(second, b"two!") + close)
    callbacks: list[str] = []
    server = AsyncioLoopbackServer(
        cast(Any, gateway),
        response_flushed=callbacks.append,
        terminal_response=lambda method: method == "shutdown",
    )

    await asyncio.wait_for(server._accept(reader, cast(Any, writer)), timeout=1)

    response_frame = bytes((0x81, len(payload))) + payload
    assert gateway.websocket_dispatches == 2
    assert bytes(writer.buffer).count(response_frame) == 2
    assert callbacks == []
    assert writer.close_completed is True


@pytest.mark.asyncio
async def test_token_expiry_and_cross_workspace_replay_are_denied() -> None:
    clock = ManualClock()
    gateway = _gateway(clock=clock)
    token = gateway.issue_launch_url().split("#", maxsplit=1)[1]
    clock.advance(timedelta(seconds=61))
    expired = await gateway.handle(
        _request(
            gateway,
            "POST",
            "/auth/exchange",
            body={"token": token},
            headers={"Origin": gateway.origin, "Content-Type": "application/json"},
        ),
        ManualCancellationToken(),
    )
    assert expired.status == 401
    other = LoopbackWebGateway(
        config=LoopbackGatewayConfig(workspace_id="ws_other", workspace_instance_id="wsi_other", worker_pid=456),
        clock=clock,
        dispatcher=RecordingDispatcher(),
        assets=(_asset(),),
    )
    other.bind_identity("127.0.0.1", 54322)
    foreign = gateway.issue_launch_url().split("#", maxsplit=1)[1]
    rejected = await other.handle(
        _request(
            other,
            "POST",
            "/auth/exchange",
            body={"token": foreign},
            headers={"Origin": other.origin, "Content-Type": "application/json"},
        ),
        ManualCancellationToken(),
    )
    assert rejected.status == 401


@pytest.mark.asyncio
async def test_static_assets_have_strict_csp_and_no_store() -> None:
    gateway = _gateway()
    response = await gateway.handle(_request(gateway, "GET", "/"), ManualCancellationToken())
    assert response.status == 200
    assert response.headers["Cache-Control"].startswith("no-store")
    policy = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    assert "connect-src 'self';" in policy
    assert "http:" not in policy
    assert "ws:" not in policy


def test_packaged_asset_loader_maps_closed_routes_with_deterministic_metadata(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "_web"
    assets = package / "assets"
    assets.mkdir(parents=True)
    (package / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (assets / "app.js").write_text("export {};", encoding="utf-8")
    (assets / "app.css").write_text(":root{}", encoding="utf-8")
    monkeypatch.setattr("offeragent_harness.runtime.loopback_gateway.resources.files", lambda _package: tmp_path)

    loaded = load_packaged_web_assets()

    assert [item.path for item in loaded] == ["/", "/assets/app.css", "/assets/app.js"]
    assert loaded[0].media_type == "text/html; charset=utf-8"
    assert loaded[1].media_type == "text/css; charset=utf-8"
    assert all(item.sha256 == f"sha256:{hashlib.sha256(item.content).hexdigest()}" for item in loaded)


def test_packaged_asset_loader_rejects_unsafe_names_and_unknown_mime(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "_web"
    assets = package / "assets"
    assets.mkdir(parents=True)
    (package / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (assets / "..unsafe.js").write_text("x", encoding="utf-8")
    monkeypatch.setattr("offeragent_harness.runtime.loopback_gateway.resources.files", lambda _package: tmp_path)
    with pytest.raises(RuntimeError, match="unsafe path"):
        load_packaged_web_assets()
    (assets / "..unsafe.js").unlink()
    (assets / "data.exe").write_bytes(b"MZ")
    with pytest.raises(RuntimeError, match="type is forbidden"):
        load_packaged_web_assets()


@pytest.mark.asyncio
async def test_static_asset_routes_never_normalize_directory_traversal() -> None:
    gateway = _gateway()
    for target in ("/assets/../index.html", "/assets/%2e%2e/index.html", "/index.html"):
        response = await gateway.handle(
            _request(gateway, "GET", target, headers={"Origin": gateway.origin}),
            ManualCancellationToken(),
        )
        assert response.status == 404


@pytest.mark.asyncio
async def test_production_listener_uses_distinct_random_loopback_ports() -> None:
    first_gateway = LoopbackWebGateway(
        config=LoopbackGatewayConfig(workspace_id="ws_one", workspace_instance_id="wsi_one", worker_pid=1),
        clock=ManualClock(),
        dispatcher=RecordingDispatcher(),
        assets=(_asset(),),
    )
    second_gateway = LoopbackWebGateway(
        config=LoopbackGatewayConfig(workspace_id="ws_two", workspace_instance_id="wsi_two", worker_pid=2),
        clock=ManualClock(),
        dispatcher=RecordingDispatcher(),
        assets=(_asset(),),
    )
    first, second = AsyncioLoopbackServer(first_gateway), AsyncioLoopbackServer(second_gateway)
    try:
        first_port, second_port = await first.start(), await second.start()
        assert first_port != second_port
        assert first_port != 12805 and second_port != 12805
        reader, writer = await asyncio.open_connection("127.0.0.1", first_port)
        writer.write(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{first_port}\r\n\r\n".encode())
        await writer.drain()
        data = await reader.read()
        assert b"200 OK" in data and _asset().content in data
        writer.close()
        await writer.wait_closed()
    finally:
        await first.stop()
        await second.stop()
