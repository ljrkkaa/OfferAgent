"""Authenticated loopback Web command gateway for the one Worker application."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.resources as resources
import ipaddress
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from offeragent_harness.ports import ApplicationCommandContext, ApplicationCommandDispatcher, CancellationToken, Clock
from offeragent_harness.protocol.errors import ProtocolViolation

from .application_errors import application_error_http_status, map_application_exception

_HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ASSET_PATH = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_TOKEN_BYTES = 32
_SESSION_COOKIE = "oa_session"
_MAX_ASSET_BYTES = 8 * 1024 * 1024
_MAX_ASSET_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_ASSET_COUNT = 256
_ASSET_MEDIA_TYPES = MappingProxyType(
    {
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".ico": "image/x-icon",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".map": "application/json; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2",
    }
)


class LoopbackSecurityError(PermissionError):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class LoopbackGatewayConfig:
    workspace_id: str
    workspace_instance_id: str
    worker_pid: int
    host: str = "127.0.0.1"
    port: int = 0
    launch_token_ttl_seconds: float = 60.0
    session_idle_ttl_seconds: float = 15 * 60.0
    max_body_bytes: int = 4 * 1024 * 1024
    max_header_bytes: int = 64 * 1024
    request_timeout_seconds: float = 30.0
    max_connections: int = 64

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as error:
            raise ValueError("Loopback Gateway host must be a literal IP") from error
        if not address.is_loopback or self.host not in {"127.0.0.1", "::1"}:
            raise ValueError("Loopback Gateway may bind only 127.0.0.1 or ::1")
        if self.port != 0:
            raise ValueError("Loopback Gateway must request a random OS-assigned port")
        if not self.workspace_id or not self.workspace_instance_id or self.worker_pid < 1:
            raise ValueError("Loopback Gateway Workspace/PID identity is invalid")
        if min(self.launch_token_ttl_seconds, self.session_idle_ttl_seconds, self.request_timeout_seconds) <= 0:
            raise ValueError("Loopback Gateway timeouts must be positive")
        if not 1_024 <= self.max_body_bytes <= 16 * 1024 * 1024:
            raise ValueError("Loopback Gateway body limit is invalid")
        if not 1_024 <= self.max_header_bytes <= 1024 * 1024 or not 1 <= self.max_connections <= 1_024:
            raise ValueError("Loopback Gateway header/concurrency limit is invalid")


@dataclass(frozen=True, slots=True)
class LoopbackAsset:
    path: str
    media_type: str
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        if self.path != "/" and _ASSET_PATH.fullmatch(self.path) is None:
            raise ValueError("Loopback asset path is invalid")
        if not self.media_type or not self.sha256.startswith("sha256:"):
            raise ValueError("Loopback asset metadata is invalid")
        if f"sha256:{hashlib.sha256(self.content).hexdigest()}" != self.sha256:
            raise ValueError("Loopback asset hash does not match content")
        object.__setattr__(self, "content", bytes(self.content))


def load_packaged_web_assets() -> tuple[LoopbackAsset, ...]:
    """Load the immutable Web UI bundled inside the installed wheel.

    The public route is derived from a closed package layout: ``index.html``
    is available only as ``/`` and regular files below ``assets/`` are exposed
    below ``/assets/``.  Unknown types, links, traversal-like names and
    oversized bundles fail Worker composition before a listener can bind.
    """

    root = resources.files("offeragent_harness").joinpath("_web")
    index = root.joinpath("index.html")
    asset_root = root.joinpath("assets")
    if not index.is_file() or _resource_is_link(index) or not asset_root.is_dir():
        raise RuntimeError("packaged Loopback Web assets are incomplete or unsafe")

    candidates: list[tuple[str, Any]] = [("/", index)]

    def visit(directory: Any, parts: tuple[str, ...]) -> None:
        for item in sorted(directory.iterdir(), key=lambda value: value.name):
            if not _safe_asset_name(item.name) or _resource_is_link(item):
                raise RuntimeError("packaged Loopback Web asset has an unsafe path")
            child_parts = (*parts, item.name)
            if item.is_dir():
                visit(item, child_parts)
            elif item.is_file():
                candidates.append(("/assets/" + "/".join(child_parts), item))
            else:
                raise RuntimeError("packaged Loopback Web asset is not a regular file")

    visit(asset_root, ())
    if not 2 <= len(candidates) <= _MAX_ASSET_COUNT:
        raise RuntimeError("packaged Loopback Web asset count is outside the safe bound")

    loaded: list[LoopbackAsset] = []
    total = 0
    for path, item in candidates:
        suffix = ".html" if path == "/" else path.rsplit(".", maxsplit=1)[-1].lower()
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        media_type = _ASSET_MEDIA_TYPES.get(suffix)
        if media_type is None:
            raise RuntimeError(f"packaged Loopback Web asset type is forbidden: {path}")
        content = item.read_bytes()
        if not content or len(content) > _MAX_ASSET_BYTES:
            raise RuntimeError(f"packaged Loopback Web asset size is invalid: {path}")
        total += len(content)
        if total > _MAX_ASSET_TOTAL_BYTES:
            raise RuntimeError("packaged Loopback Web assets exceed the total byte bound")
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        loaded.append(LoopbackAsset(path, media_type, content, digest))
    return tuple(loaded)


def _safe_asset_name(name: str) -> bool:
    return (
        bool(name)
        and len(name) <= 128
        and name not in {".", ".."}
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is not None
    )


def _resource_is_link(item: Any) -> bool:
    checker = getattr(item, "is_symlink", None)
    return bool(checker()) if callable(checker) else False


@dataclass(frozen=True, slots=True)
class LoopbackRequest:
    method: str
    target: str
    headers: Mapping[str, str]
    body: bytes
    peer_ip: str

    def __post_init__(self) -> None:
        method = self.method.upper()
        if method not in {"GET", "HEAD", "POST"}:
            raise ValueError("Loopback HTTP method is invalid")
        if not self.target.startswith("/") or self.target.startswith("//") or "#" in self.target:
            raise ValueError("Loopback request target must be origin-form without a fragment")
        normalized: dict[str, str] = {}
        for key, value in self.headers.items():
            name = key.lower()
            if _HEADER.fullmatch(key) is None or name in normalized or "\r" in value or "\n" in value:
                raise ValueError("Loopback request headers are malformed")
            normalized[name] = value
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "headers", MappingProxyType(normalized))
        object.__setattr__(self, "body", bytes(self.body))


@dataclass(frozen=True, slots=True)
class LoopbackResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    command_method: str | None = None
    request_method: str | None = None

    def __post_init__(self) -> None:
        if not 100 <= self.status <= 599:
            raise ValueError("Loopback response status is invalid")
        if self.command_method is not None and not self.command_method:
            raise ValueError("Loopback response command method is invalid")
        if self.request_method is not None and not self.request_method:
            raise ValueError("Loopback response request method is invalid")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "body", bytes(self.body))


@dataclass(frozen=True, slots=True)
class LoopbackWebSocketResponse:
    payload: bytes
    command_method: str | None = None
    request_method: str | None = None

    def __post_init__(self) -> None:
        if self.command_method is not None and not self.command_method:
            raise ValueError("Loopback WebSocket command method is invalid")
        if self.request_method is not None and not self.request_method:
            raise ValueError("Loopback WebSocket request method is invalid")
        object.__setattr__(self, "payload", bytes(self.payload))


@dataclass(frozen=True, slots=True)
class LoopbackSession:
    cookie: str
    csrf_token: str
    workspace_id: str
    worker_pid: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LoopbackLaunch:
    url: str
    worker_pid: int
    workspace_instance_id: str
    expires_at: datetime


@dataclass(slots=True)
class _LaunchGrant:
    digest: bytes
    expires_at: datetime
    consumed: bool = False


@dataclass(slots=True)
class _BrowserSession:
    cookie_digest: bytes
    csrf_digest: bytes
    expires_at: datetime


class LoopbackTokenBroker:
    """One-time fragment token and short-lived browser session authority."""

    def __init__(self, config: LoopbackGatewayConfig, clock: Clock) -> None:
        self._config = config
        self._clock = clock
        self._launch: dict[bytes, _LaunchGrant] = {}
        self._sessions: dict[bytes, _BrowserSession] = {}

    def issue(self) -> str:
        token, _expires_at = self.issue_with_expiry()
        return token

    def issue_with_expiry(self) -> tuple[str, datetime]:
        self._purge()
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        digest = self._digest(token)
        expires_at = self._clock.utcnow() + timedelta(seconds=self._config.launch_token_ttl_seconds)
        self._launch[digest] = _LaunchGrant(
            digest,
            expires_at,
        )
        return token, expires_at

    def exchange(self, token: str) -> LoopbackSession:
        self._purge()
        digest = self._digest(token)
        grant = self._launch.get(digest)
        now = self._clock.utcnow()
        if grant is None or grant.consumed or grant.expires_at <= now:
            raise LoopbackSecurityError(401, "launch_token_invalid", "launch token is invalid, expired, or consumed")
        grant.consumed = True
        cookie = secrets.token_urlsafe(_TOKEN_BYTES)
        csrf = secrets.token_urlsafe(_TOKEN_BYTES)
        cookie_digest = self._digest(cookie)
        expires_at = now + timedelta(seconds=self._config.session_idle_ttl_seconds)
        self._sessions[cookie_digest] = _BrowserSession(cookie_digest, self._digest(csrf), expires_at)
        return LoopbackSession(cookie, csrf, self._config.workspace_id, self._config.worker_pid, expires_at)

    def authorize(self, cookie: str, csrf: str | None, *, require_csrf: bool) -> LoopbackSession:
        self._purge()
        cookie_digest = self._digest(cookie)
        session = self._sessions.get(cookie_digest)
        now = self._clock.utcnow()
        if session is None or session.expires_at <= now:
            raise LoopbackSecurityError(401, "session_invalid", "browser session is invalid or expired")
        if require_csrf and (csrf is None or not hmac.compare_digest(session.csrf_digest, self._digest(csrf))):
            raise LoopbackSecurityError(403, "csrf_invalid", "CSRF token is invalid")
        session.expires_at = now + timedelta(seconds=self._config.session_idle_ttl_seconds)
        return LoopbackSession(
            cookie, csrf or "", self._config.workspace_id, self._config.worker_pid, session.expires_at
        )

    @staticmethod
    def _digest(value: str) -> bytes:
        if not value or len(value) > 512 or any(ord(character) < 0x21 for character in value):
            return hashlib.sha256(b"invalid").digest()
        return hashlib.sha256(value.encode("utf-8")).digest()

    def _purge(self) -> None:
        now = self._clock.utcnow()
        self._launch = {
            key: value for key, value in self._launch.items() if not value.consumed and value.expires_at > now
        }
        self._sessions = {key: value for key, value in self._sessions.items() if value.expires_at > now}


class LoopbackWebGateway:
    """HTTP/WebSocket-neutral gateway; listeners contain no application logic."""

    def __init__(
        self,
        *,
        config: LoopbackGatewayConfig,
        clock: Clock,
        dispatcher: ApplicationCommandDispatcher,
        assets: tuple[LoopbackAsset, ...] | None = None,
    ) -> None:
        if assets is None:
            assets = load_packaged_web_assets()
        by_path = {item.path: item for item in assets}
        if len(by_path) != len(assets) or "/" not in by_path:
            raise ValueError("Loopback assets require one unique root document")
        self.config = config
        self._dispatcher = dispatcher
        self._assets = MappingProxyType(by_path)
        self._tokens = LoopbackTokenBroker(config, clock)
        self._port: int | None = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("Loopback Gateway has not been bound")
        return self._port

    @property
    def origin(self) -> str:
        host = f"[{self.config.host}]" if ":" in self.config.host else self.config.host
        return f"http://{host}:{self.port}"

    def bind_identity(self, host: str, port: int) -> None:
        if self._port is not None:
            raise RuntimeError("Loopback Gateway bind identity cannot change")
        if host != self.config.host or not 1_024 <= port <= 65_535:
            raise LoopbackSecurityError(500, "unsafe_bind", "listener did not bind the requested loopback endpoint")
        self._port = port

    def issue_launch_url(self) -> str:
        return self.issue_launch().url

    def issue_launch(self) -> LoopbackLaunch:
        token, expires_at = self._tokens.issue_with_expiry()
        return LoopbackLaunch(
            url=f"{self.origin}/#{token}",
            worker_pid=self.config.worker_pid,
            workspace_instance_id=self.config.workspace_instance_id,
            expires_at=expires_at,
        )

    async def handle(self, request: LoopbackRequest, cancellation: CancellationToken) -> LoopbackResponse:
        try:
            self._validate_local_request(request)
            path = urlsplit(request.target).path
            if request.method in {"GET", "HEAD"} and path in self._assets:
                return self._asset_response(self._assets[path], head=request.method == "HEAD")
            self._require_origin(request)
            if request.method == "POST" and path == "/auth/exchange":
                return self._exchange(request)
            if request.method == "POST" and path == "/api/command":
                return await self._command(request, cancellation, websocket=False)
            raise LoopbackSecurityError(404, "not_found", "local route does not exist")
        except LoopbackSecurityError as error:
            return self._json_response(error.status, {"error": {"code": error.code, "message": str(error)}})
        except ProtocolViolation as error:
            return self._json_response(400, {"error": error.error.to_wire()})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            return self._json_response(400, {"error": {"code": "invalid_request", "message": "request is invalid"}})

    async def handle_websocket_command(
        self,
        *,
        host: str,
        origin: str,
        peer_ip: str,
        cookie_header: str,
        csrf_token: str,
        payload: bytes,
        cancellation: CancellationToken,
    ) -> bytes:
        result = await self.dispatch_websocket_command(
            host=host,
            origin=origin,
            peer_ip=peer_ip,
            cookie_header=cookie_header,
            csrf_token=csrf_token,
            payload=payload,
            cancellation=cancellation,
        )
        return result.payload

    async def dispatch_websocket_command(
        self,
        *,
        host: str,
        origin: str,
        peer_ip: str,
        cookie_header: str,
        csrf_token: str,
        payload: bytes,
        cancellation: CancellationToken,
    ) -> LoopbackWebSocketResponse:
        request = LoopbackRequest(
            "POST",
            "/api/command",
            {
                "Host": host,
                "Origin": origin,
                "Cookie": cookie_header,
                "X-CSRF-Token": csrf_token,
            },
            payload,
            peer_ip,
        )
        self._validate_local_request(request)
        self._require_origin(request)
        response = await self._command(request, cancellation, websocket=True)
        return LoopbackWebSocketResponse(response.body, response.command_method, response.request_method)

    def authorize_websocket(
        self,
        *,
        host: str,
        origin: str,
        peer_ip: str,
        cookie_header: str,
        csrf_token: str,
    ) -> None:
        request = LoopbackRequest(
            "GET",
            "/ws",
            {"Host": host, "Origin": origin, "Cookie": cookie_header},
            b"",
            peer_ip,
        )
        self._validate_local_request(request)
        self._require_origin(request)
        cookie = self._cookie(cookie_header, _SESSION_COOKIE)
        self._tokens.authorize(cookie, csrf_token, require_csrf=True)

    def _validate_local_request(self, request: LoopbackRequest) -> None:
        try:
            peer = ipaddress.ip_address(request.peer_ip)
        except ValueError as error:
            raise LoopbackSecurityError(403, "peer_denied", "peer address is invalid") from error
        if not peer.is_loopback:
            raise LoopbackSecurityError(403, "peer_denied", "non-loopback peer is denied")
        expected = urlsplit(self.origin).netloc
        if request.headers.get("host") != expected:
            raise LoopbackSecurityError(421, "host_denied", "Host header does not match the bound numeric origin")
        if len(request.body) > self.config.max_body_bytes:
            raise LoopbackSecurityError(413, "body_too_large", "request body exceeds limit")
        header_bytes = sum(len(key) + len(value) + 4 for key, value in request.headers.items())
        if header_bytes > self.config.max_header_bytes:
            raise LoopbackSecurityError(431, "headers_too_large", "request headers exceed limit")

    def _require_origin(self, request: LoopbackRequest) -> None:
        if request.headers.get("origin") != self.origin:
            raise LoopbackSecurityError(403, "origin_denied", "Origin does not match the bound numeric origin")

    def _exchange(self, request: LoopbackRequest) -> LoopbackResponse:
        if request.headers.get("content-type", "").split(";", maxsplit=1)[0].strip() != "application/json":
            raise LoopbackSecurityError(415, "content_type_denied", "authentication exchange requires JSON")
        value = json.loads(request.body.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"token"} or not isinstance(value["token"], str):
            raise LoopbackSecurityError(400, "launch_token_invalid", "launch token body is invalid")
        session = self._tokens.exchange(value["token"])
        response = self._json_response(
            200,
            {
                "csrfToken": session.csrf_token,
                "workspaceId": session.workspace_id,
                "workerPid": session.worker_pid,
                "expiresAt": session.expires_at.isoformat(),
            },
        )
        headers = dict(response.headers)
        headers["Set-Cookie"] = (
            f"{_SESSION_COOKIE}={session.cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age="
            f"{int(self.config.session_idle_ttl_seconds)}"
        )
        return LoopbackResponse(response.status, headers, response.body)

    async def _command(
        self,
        request: LoopbackRequest,
        cancellation: CancellationToken,
        *,
        websocket: bool,
    ) -> LoopbackResponse:
        cookie = self._cookie(request.headers.get("cookie", ""), _SESSION_COOKIE)
        self._tokens.authorize(cookie, request.headers.get("x-csrf-token"), require_csrf=True)
        value = json.loads(request.body.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"method", "params"}:
            raise LoopbackSecurityError(400, "command_invalid", "command envelope is invalid")
        method, params = value["method"], value["params"]
        if not isinstance(method, str) or not isinstance(params, dict):
            raise LoopbackSecurityError(400, "command_invalid", "command method/params are invalid")
        try:
            result = await self._dispatcher.dispatch(
                method,
                params,
                cancellation,
                context=ApplicationCommandContext(
                    transport="loopback-websocket" if websocket else "loopback-http",
                    client_id=f"web-{hashlib.sha256(cookie.encode()).hexdigest()[:16]}",
                    peer=request.peer_ip,
                ),
            )
            return self._json_response(200, {"result": result}, command_method=method, request_method=method)
        except (Exception, asyncio.CancelledError) as error:
            violation = map_application_exception(error)
            return self._json_response(
                application_error_http_status(violation.error),
                {"error": violation.error.to_wire()},
                request_method=method,
            )

    def _asset_response(self, asset: LoopbackAsset, *, head: bool) -> LoopbackResponse:
        headers = self._security_headers()
        headers.update(
            {
                "Content-Type": asset.media_type,
                "Content-Length": str(len(asset.content)),
                "ETag": f'"{asset.sha256.removeprefix("sha256:")}"',
            }
        )
        return LoopbackResponse(200, headers, b"" if head else asset.content)

    def _json_response(
        self,
        status: int,
        value: Mapping[str, Any],
        *,
        command_method: str | None = None,
        request_method: str | None = None,
    ) -> LoopbackResponse:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers = self._security_headers()
        headers.update({"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(payload))})
        return LoopbackResponse(status, headers, payload, command_method, request_method)

    @staticmethod
    def _security_headers() -> dict[str, str]:
        return {
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            ),
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }

    @staticmethod
    def _cookie(header: str, name: str) -> str:
        found: list[str] = []
        for item in header.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key == name:
                found.append(value)
        if len(found) != 1 or not found[0]:
            raise LoopbackSecurityError(401, "session_invalid", "browser session cookie is missing or ambiguous")
        return found[0]


__all__ = [
    "LoopbackAsset",
    "LoopbackGatewayConfig",
    "LoopbackLaunch",
    "LoopbackRequest",
    "LoopbackResponse",
    "LoopbackSecurityError",
    "LoopbackSession",
    "LoopbackTokenBroker",
    "LoopbackWebGateway",
    "LoopbackWebSocketResponse",
    "load_packaged_web_assets",
]
