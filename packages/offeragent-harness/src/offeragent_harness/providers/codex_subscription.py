"""Account-bound Codex subscription model catalog and pinned provider identity.

The public module returns only normalized catalog snapshots.  Credential layout,
HTTP headers, raw backend JSON, retry classification, and last-success caching
remain implementation details behind one injected HTTP adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

import httpx

from .openai_responses import ModelCredentialSource, ModelCredentialSourceError

CODEX_SUBSCRIPTION_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_SUBSCRIPTION_PROVIDER_ID = "codex-subscription-experimental"

# This is a protocol-compatibility declaration, not a model-name default.  It
# tracks the Codex catalog schema implemented by this adapter.
CODEX_CATALOG_CLIENT_VERSION = "0.144.5"
CODEX_SUBSCRIPTION_MODELS_ENDPOINT = (
    f"{CODEX_SUBSCRIPTION_BASE_URL}/models?client_version={CODEX_CATALOG_CLIENT_VERSION}"
)

_MAX_CATALOG_BYTES = 8 * 1024 * 1024
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CatalogFreshness = Literal["fresh", "stale", "unavailable"]
_CatalogErrorCode = Literal[
    "auth_account_changed",
    "auth_required",
    "catalog_empty",
    "catalog_invalid_response",
    "catalog_rate_limited",
    "catalog_unavailable",
    "catalog_unreachable",
]


@dataclass(frozen=True, slots=True)
class CodexCatalogHttpRequest:
    """A bounded request passed only to the injected catalog HTTP adapter."""

    endpoint: str
    headers: Mapping[str, str] = field(repr=False)
    timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class CodexCatalogHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        if not 100 <= self.status <= 599:
            raise ValueError("catalog HTTP status is invalid")
        if len(self.body) > _MAX_CATALOG_BYTES:
            raise ValueError("catalog HTTP body exceeds its safety limit")


class CodexCatalogTransportError(RuntimeError):
    """A non-secret transport failure emitted by a catalog HTTP adapter."""

    def __init__(self, message: str = "catalog transport failed") -> None:
        del message
        super().__init__("Codex catalog transport is unavailable")


class CodexCatalogHttpAdapter(Protocol):
    def get(self, request: CodexCatalogHttpRequest) -> CodexCatalogHttpResponse: ...


class HttpxCodexCatalogHttpAdapter:
    """Production synchronous adapter; callers run the deep module off-loop."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        proxy_url: str | None = None,
    ) -> None:
        self._transport = transport
        self._proxy_url = (
            _normalize_loopback_proxy(proxy_url)
            if proxy_url is not None
            else None if transport is not None else _environment_loopback_proxy()
        )

    def get(self, request: CodexCatalogHttpRequest) -> CodexCatalogHttpResponse:
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=httpx.Timeout(request.timeout_seconds),
                follow_redirects=False,
                trust_env=False,
                proxy=self._proxy_url,
            ) as client:
                with client.stream("GET", request.endpoint, headers=request.headers) as response:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > _MAX_CATALOG_BYTES:
                            raise CodexCatalogTransportError()
                        body.extend(chunk)
                    status = response.status_code
                    response_headers = MappingProxyType(dict(response.headers.items()))
        except httpx.HTTPError as error:
            raise CodexCatalogTransportError() from error
        return CodexCatalogHttpResponse(
            status=status,
            headers=response_headers,
            body=bytes(body),
        )


@dataclass(frozen=True, slots=True)
class CodexModelServiceTier:
    id: str
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class CodexCatalogModel:
    """Normalized capability data for one picker-visible Codex model."""

    model_id: str
    display_name: str
    description: str | None
    input_modalities: tuple[str, ...]
    supports_image_detail_original: bool
    supports_hosted_search: bool
    web_search_tool_type: str | None
    context_window: int | None
    max_context_window: int | None
    effective_context_window_percent: int | None
    additional_speed_tiers: tuple[str, ...]
    service_tiers: tuple[CodexModelServiceTier, ...]
    default_service_tier: str | None

    @property
    def supports_fast_mode(self) -> bool:
        return "fast" in self.additional_speed_tiers or any(
            tier.id in {"fast", "priority"} for tier in self.service_tiers
        )


@dataclass(frozen=True, slots=True)
class CodexCatalogError:
    code: _CatalogErrorCode
    retryable: bool


@dataclass(frozen=True, slots=True)
class CodexModelCatalogSnapshot:
    models: tuple[CodexCatalogModel, ...]
    freshness: _CatalogFreshness
    catalog_revision: str | None
    fetched_at: datetime | None
    error: CodexCatalogError | None

    @property
    def display_only(self) -> bool:
        return self.freshness != "fresh"


class CodexSubscriptionModelModule:
    """Fetch, normalize, account-bind, and cache the live subscription catalog."""

    def __init__(
        self,
        *,
        credentials: ModelCredentialSource,
        http: CodexCatalogHttpAdapter,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._credentials = credentials
        self._http = http
        self._now = now
        self._lock = threading.Lock()
        self._bound_account_fingerprint: str | None = None
        self._last_success: CodexModelCatalogSnapshot | None = None

    def refresh(self) -> CodexModelCatalogSnapshot:
        """Return a fresh catalog or an explicitly display-only prior success."""

        with self._lock:
            return self._refresh_locked()

    def _refresh_locked(self) -> CodexModelCatalogSnapshot:
        try:
            with self._credentials.lease() as lease:
                if (
                    self._bound_account_fingerprint is not None
                    and lease.account_fingerprint != self._bound_account_fingerprint
                ):
                    return self._failure("auth_account_changed", retryable=False, allow_stale=False)
                try:
                    token = lease.material.tobytes().decode("ascii", errors="strict")
                except UnicodeDecodeError:
                    return self._failure("auth_required", retryable=False)
                if not token or any(character in token for character in "\x00\r\n"):
                    return self._failure("auth_required", retryable=False)
                headers = dict(lease.headers)
                if not _safe_account_header(headers.get("ChatGPT-Account-ID")):
                    return self._failure("auth_required", retryable=False)
                headers.update(
                    {
                        "Accept": "application/json",
                        "Authorization": f"Bearer {token}",
                        "version": CODEX_CATALOG_CLIENT_VERSION,
                    }
                )
                request = CodexCatalogHttpRequest(
                    endpoint=CODEX_SUBSCRIPTION_MODELS_ENDPOINT,
                    headers=MappingProxyType(headers),
                )
                try:
                    response = self._http.get(request)
                except CodexCatalogTransportError:
                    return self._failure("catalog_unreachable", retryable=True)
                finally:
                    token = ""
                failure = _http_failure(response.status)
                if failure is not None:
                    code, retryable = failure
                    return self._failure(code, retryable=retryable)
                try:
                    models = _decode_catalog(response)
                except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError, _CatalogDecodeError):
                    return self._failure("catalog_invalid_response", retryable=False)
                if not models:
                    return self._failure("catalog_empty", retryable=False)
                snapshot = CodexModelCatalogSnapshot(
                    models=models,
                    freshness="fresh",
                    catalog_revision=_catalog_revision(models),
                    fetched_at=_aware_utc(self._now()),
                    error=None,
                )
                self._bound_account_fingerprint = lease.account_fingerprint
                self._last_success = snapshot
                return snapshot
        except ModelCredentialSourceError as error:
            return self._failure(cast(_CatalogErrorCode, error.code), retryable=error.retryable)

    def _failure(
        self,
        code: _CatalogErrorCode,
        *,
        retryable: bool,
        allow_stale: bool = True,
    ) -> CodexModelCatalogSnapshot:
        error = CodexCatalogError(code, retryable)
        if allow_stale and self._last_success is not None:
            cached = self._last_success
            return CodexModelCatalogSnapshot(
                models=cached.models,
                freshness="stale",
                catalog_revision=cached.catalog_revision,
                fetched_at=cached.fetched_at,
                error=error,
            )
        return CodexModelCatalogSnapshot((), "unavailable", None, None, error)


class _CatalogDecodeError(ValueError):
    pass


def _decode_catalog(response: CodexCatalogHttpResponse) -> tuple[CodexCatalogModel, ...]:
    if len(response.body) > _MAX_CATALOG_BYTES:
        raise _CatalogDecodeError("catalog is too large")
    content_type = _header(response.headers, "content-type")
    if content_type is not None and content_type.split(";", 1)[0].strip().casefold() != "application/json":
        raise _CatalogDecodeError("catalog content type is not JSON")
    root = json.loads(
        response.body.decode("utf-8", errors="strict"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(root, dict) or not isinstance(root.get("models"), list):
        raise _CatalogDecodeError("catalog root is invalid")
    models: list[CodexCatalogModel] = []
    seen: set[str] = set()
    for raw in root["models"]:
        if not isinstance(raw, dict):
            raise _CatalogDecodeError("catalog model is invalid")
        visibility = raw.get("visibility")
        if not isinstance(visibility, str):
            raise _CatalogDecodeError("catalog visibility is invalid")
        if visibility != "list":
            continue
        model = _decode_model(raw)
        if model.model_id in seen:
            raise _CatalogDecodeError("catalog model IDs are not unique")
        seen.add(model.model_id)
        models.append(model)
    return tuple(models)


def _decode_model(raw: Mapping[str, Any]) -> CodexCatalogModel:
    model_id = _bounded_text(raw.get("slug"), label="model ID", maximum=256, pattern=_MODEL_ID)
    display_name = _bounded_text(raw.get("display_name"), label="display name", maximum=512)
    description = _optional_text(raw.get("description"), label="description", maximum=2_048)
    modalities = _identifier_list(raw.get("input_modalities"), label="input modalities", maximum=16)
    image_original = _required_bool(raw.get("supports_image_detail_original"), "original image detail")
    hosted_search = _required_bool(raw.get("supports_search_tool"), "hosted search")
    search_type = _optional_identifier(raw.get("web_search_tool_type"), label="web search tool type")
    context_window = _optional_positive_int(raw.get("context_window"), label="context window")
    max_context_window = _optional_positive_int(raw.get("max_context_window"), label="max context window")
    effective_percent = _optional_positive_int(
        raw.get("effective_context_window_percent"),
        label="effective context window percent",
        maximum=100,
    )
    additional_speed_tiers = _identifier_list(
        raw.get("additional_speed_tiers", []),
        label="additional speed tiers",
        maximum=32,
        allow_empty=True,
    )
    service_tiers = _service_tiers(raw.get("service_tiers", []))
    default_service_tier = _optional_identifier(raw.get("default_service_tier"), label="default service tier")
    return CodexCatalogModel(
        model_id=model_id,
        display_name=display_name,
        description=description,
        input_modalities=modalities,
        supports_image_detail_original=image_original,
        supports_hosted_search=hosted_search,
        web_search_tool_type=search_type,
        context_window=context_window,
        max_context_window=max_context_window,
        effective_context_window_percent=effective_percent,
        additional_speed_tiers=additional_speed_tiers,
        service_tiers=service_tiers,
        default_service_tier=default_service_tier,
    )


def _service_tiers(value: object) -> tuple[CodexModelServiceTier, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise _CatalogDecodeError("service tiers are invalid")
    tiers: list[CodexModelServiceTier] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise _CatalogDecodeError("service tier is invalid")
        tier_id = _bounded_text(raw.get("id"), label="service tier ID", maximum=64, pattern=_CAPABILITY_ID)
        if tier_id in seen:
            raise _CatalogDecodeError("service tier IDs are not unique")
        seen.add(tier_id)
        tiers.append(
            CodexModelServiceTier(
                tier_id,
                _bounded_text(raw.get("name"), label="service tier name", maximum=128),
                _bounded_text(raw.get("description"), label="service tier description", maximum=1_024),
            )
        )
    return tuple(tiers)


def _identifier_list(
    value: object,
    *,
    label: str,
    maximum: int,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum or (not value and not allow_empty):
        raise _CatalogDecodeError(f"{label} are invalid")
    result: list[str] = []
    for item in value:
        result.append(_bounded_text(item, label=label, maximum=64, pattern=_CAPABILITY_ID))
    if len(set(result)) != len(result):
        raise _CatalogDecodeError(f"{label} are not unique")
    return tuple(result)


def _optional_identifier(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, label=label, maximum=64, pattern=_CAPABILITY_ID)


def _bounded_text(
    value: object,
    *,
    label: str,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise _CatalogDecodeError(f"{label} is invalid")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise _CatalogDecodeError(f"{label} is invalid")
    return value


def _optional_text(value: object, *, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, label=label, maximum=maximum)


def _required_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise _CatalogDecodeError(f"{label} capability is invalid")
    return value


def _optional_positive_int(value: object, *, label: str, maximum: int = 10_000_000) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise _CatalogDecodeError(f"{label} is invalid")
    return value


def _http_failure(status: int) -> tuple[_CatalogErrorCode, bool] | None:
    if 200 <= status < 300:
        return None
    if status in {401, 403}:
        return "auth_required", False
    if status == 429:
        return "catalog_rate_limited", True
    if status in {408, 409, 425, 500, 502, 503, 504}:
        return "catalog_unreachable", True
    return "catalog_unavailable", False


def _catalog_revision(models: Sequence[CodexCatalogModel]) -> str:
    value = [
        {
            "model": model.model_id,
            "displayName": model.display_name,
            "inputModalities": model.input_modalities,
            "supportsImageDetailOriginal": model.supports_image_detail_original,
            "supportsHostedSearch": model.supports_hosted_search,
            "webSearchToolType": model.web_search_tool_type,
            "contextWindow": model.context_window,
            "maxContextWindow": model.max_context_window,
            "effectiveContextWindowPercent": model.effective_context_window_percent,
            "additionalSpeedTiers": model.additional_speed_tiers,
            "serviceTiers": [
                {"id": tier.id, "name": tier.name, "description": tier.description}
                for tier in model.service_tiers
            ],
            "defaultServiceTier": model.default_service_tier,
        }
        for model in models
    ]
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _safe_account_header(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 512
        and value.strip() == value
        and value.isascii()
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _environment_loopback_proxy() -> str | None:
    for name in ("HTTPS_PROXY", "https_proxy"):
        value = os.environ.get(name)
        if value is None:
            continue
        try:
            return _normalize_loopback_proxy(value)
        except ValueError:
            return None
    return None


def _normalize_loopback_proxy(value: str) -> str:
    if not value or value != value.strip() or len(value) > 2_048:
        raise ValueError("Codex catalog proxy URL is invalid")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Codex catalog proxy port is invalid") from error
    if (
        parsed.scheme.casefold() != "http"
        or (parsed.hostname or "").casefold() not in {"127.0.0.1", "::1"}
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Codex catalog proxy must be explicit loopback HTTP with a port")
    host = (parsed.hostname or "").casefold()
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return urlunsplit(("http", authority, "", "", ""))


def _header(headers: Mapping[str, str], name: str) -> str | None:
    folded = name.casefold()
    return next((value for key, value in headers.items() if key.casefold() == folded), None)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _CatalogDecodeError("catalog JSON contains duplicate object members")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise _CatalogDecodeError(f"catalog JSON constant {value!r} is invalid")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("catalog clock must return an aware datetime")
    return value.astimezone(timezone.utc)


__all__ = [
    "CODEX_CATALOG_CLIENT_VERSION",
    "CODEX_SUBSCRIPTION_BASE_URL",
    "CODEX_SUBSCRIPTION_MODELS_ENDPOINT",
    "CODEX_SUBSCRIPTION_PROVIDER_ID",
    "CodexCatalogError",
    "CodexCatalogHttpAdapter",
    "CodexCatalogHttpRequest",
    "CodexCatalogHttpResponse",
    "CodexCatalogModel",
    "CodexCatalogTransportError",
    "CodexModelCatalogSnapshot",
    "CodexModelServiceTier",
    "CodexSubscriptionModelModule",
    "HttpxCodexCatalogHttpAdapter",
]
