"""Account-bound Codex subscription model catalog and pinned provider identity.

The public module returns only normalized catalog snapshots.  Credential layout,
HTTP headers, raw backend JSON, retry classification, and last-success caching
remain implementation details behind one injected HTTP adapter.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

import httpx

from .openai_responses import ModelCredentialLease, ModelCredentialSource, ModelCredentialSourceError

CODEX_SUBSCRIPTION_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_SUBSCRIPTION_PROVIDER_ID = "codex-subscription"

# This is a protocol-compatibility declaration, not a model-name default.  It
# tracks the Codex catalog schema implemented by this adapter.
CODEX_CATALOG_CLIENT_VERSION = "0.144.5"
CODEX_SUBSCRIPTION_MODELS_ENDPOINT = (
    f"{CODEX_SUBSCRIPTION_BASE_URL}/models?client_version={CODEX_CATALOG_CLIENT_VERSION}"
)

_MAX_CATALOG_BYTES = 8 * 1024 * 1024
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ACCOUNT_BINDING = re.compile(r"^sha256:[0-9a-f]{64}$")
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
_RunBindingErrorCode = Literal[
    "auth_account_changed",
    "auth_required",
    "catalog_empty",
    "catalog_invalid_response",
    "catalog_rate_limited",
    "catalog_unavailable",
    "catalog_unreachable",
    "model_unavailable",
]


@dataclass(frozen=True, slots=True)
class CodexCatalogHttpRequest:
    """A bounded request passed only to the injected catalog HTTP adapter."""

    endpoint: str
    headers: Mapping[str, str] = field(repr=False)
    timeout_seconds: float = 5.0
    proxy_url: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 60:
            raise ValueError("catalog HTTP timeout must be finite and within 60 seconds")
        if self.proxy_url is not None:
            _normalize_loopback_proxy(self.proxy_url)


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


class AccountBoundModelCredentialSource:
    """Fail closed before inference when the current Codex account no longer matches a Run."""

    def __init__(self, source: ModelCredentialSource, account_binding: str) -> None:
        if _ACCOUNT_BINDING.fullmatch(account_binding) is None:
            raise ValueError("Codex account binding is invalid")
        self._source = source
        self._account_binding = account_binding

    @contextmanager
    def lease(self) -> Iterator[ModelCredentialLease]:
        with self._source.lease() as lease:
            if _account_binding(lease.account_fingerprint) != self._account_binding:
                raise ModelCredentialSourceError("auth_account_changed")
            yield lease


class HttpxCodexCatalogHttpAdapter:
    """Production synchronous adapter; callers run the deep module off-loop."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        proxy_url: str | None = None,
    ) -> None:
        self._transport = transport
        self._proxy_url = _normalize_loopback_proxy(proxy_url) if proxy_url is not None else None

    def get(self, request: CodexCatalogHttpRequest) -> CodexCatalogHttpResponse:
        proxy_url = request.proxy_url if request.proxy_url is not None else self._proxy_url
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=httpx.Timeout(request.timeout_seconds),
                follow_redirects=False,
                trust_env=False,
                proxy=proxy_url,
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
    account_binding: str | None
    error: CodexCatalogError | None

    @property
    def display_only(self) -> bool:
        return self.freshness != "fresh"


@dataclass(frozen=True, slots=True)
class CodexRunBinding:
    """An exact model selection proven against one fresh account catalog."""

    model: CodexCatalogModel
    catalog_revision: str
    bound_at: datetime
    account_binding: str

    def durable_snapshot(self) -> dict[str, object]:
        """Return the complete non-secret proof needed to resume without reselection."""

        return {
            "schemaVersion": 1,
            "modelId": self.model.model_id,
            "catalogRevision": self.catalog_revision,
            "boundAt": self.bound_at.isoformat(),
            "accountBinding": self.account_binding,
            "modelCapabilities": {
                "displayName": self.model.display_name,
                "description": self.model.description,
                "inputModalities": list(self.model.input_modalities),
                "supportsImageDetailOriginal": self.model.supports_image_detail_original,
                "supportsHostedSearch": self.model.supports_hosted_search,
                "webSearchToolType": self.model.web_search_tool_type,
                "contextWindow": self.model.context_window,
                "maxContextWindow": self.model.max_context_window,
                "effectiveContextWindowPercent": self.model.effective_context_window_percent,
                "additionalSpeedTiers": list(self.model.additional_speed_tiers),
                "serviceTiers": [
                    {"id": tier.id, "name": tier.name, "description": tier.description}
                    for tier in self.model.service_tiers
                ],
                "defaultServiceTier": self.model.default_service_tier,
            },
        }

    @classmethod
    def from_durable_snapshot(
        cls,
        value: Mapping[str, object],
        *,
        expected_model_id: str,
        expected_account_binding: str,
    ) -> CodexRunBinding:
        """Strictly restore a fingerprinted historical binding for Run recovery."""

        if value.get("schemaVersion") != 1 or value.get("modelId") != expected_model_id:
            raise ValueError("durable Codex model binding does not match the immutable Run model")
        account_binding = value.get("accountBinding")
        if account_binding != expected_account_binding or not isinstance(account_binding, str):
            raise ValueError("durable Codex model binding does not match the selected account")
        if _ACCOUNT_BINDING.fullmatch(account_binding) is None:
            raise ValueError("durable Codex model account binding is invalid")
        revision = value.get("catalogRevision")
        if not isinstance(revision, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is None:
            raise ValueError("durable Codex model binding catalog revision is invalid")
        raw_bound_at = value.get("boundAt")
        if not isinstance(raw_bound_at, str):
            raise ValueError("durable Codex model binding timestamp is invalid")
        try:
            bound_at = _aware_utc(datetime.fromisoformat(raw_bound_at))
        except ValueError as error:
            raise ValueError("durable Codex model binding timestamp is invalid") from error
        capabilities = value.get("modelCapabilities")
        if not isinstance(capabilities, Mapping):
            raise ValueError("durable Codex model binding capabilities are invalid")
        try:
            model = _decode_model(
                {
                    "slug": expected_model_id,
                    "display_name": capabilities.get("displayName"),
                    "description": capabilities.get("description"),
                    "input_modalities": _plain_list(capabilities.get("inputModalities")),
                    "supports_image_detail_original": capabilities.get("supportsImageDetailOriginal"),
                    "supports_search_tool": capabilities.get("supportsHostedSearch"),
                    "web_search_tool_type": capabilities.get("webSearchToolType"),
                    "context_window": capabilities.get("contextWindow"),
                    "max_context_window": capabilities.get("maxContextWindow"),
                    "effective_context_window_percent": capabilities.get("effectiveContextWindowPercent"),
                    "additional_speed_tiers": _plain_list(capabilities.get("additionalSpeedTiers")),
                    "service_tiers": _plain_mapping_list(capabilities.get("serviceTiers")),
                    "default_service_tier": capabilities.get("defaultServiceTier"),
                }
            )
        except _CatalogDecodeError as error:
            raise ValueError("durable Codex model binding capabilities are invalid") from error
        return cls(
            model=model,
            catalog_revision=revision,
            bound_at=bound_at,
            account_binding=account_binding,
        )


class CodexRunBindingError(RuntimeError):
    """Stable, non-secret failure to bind a new Run to the live catalog."""

    def __init__(self, code: _RunBindingErrorCode, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__("Codex model selection could not be verified")


class CodexSubscriptionModelModule:
    """Fetch, normalize, account-bind, and cache the live subscription catalog."""

    def __init__(
        self,
        *,
        credentials: ModelCredentialSource,
        http: CodexCatalogHttpAdapter,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        proxy_url: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._credentials = credentials
        self._http = http
        self._now = now
        self._proxy_url = proxy_url
        self._lock = threading.Lock()
        self._bound_account_fingerprint: str | None = None
        self._last_success: CodexModelCatalogSnapshot | None = None

    def refresh(self, *, timeout_seconds: float = 5.0) -> CodexModelCatalogSnapshot:
        """Return a fresh catalog or an explicitly display-only prior success."""

        with self._lock:
            return self._refresh_locked(timeout_seconds=timeout_seconds)

    def bind_for_run(self, model_id: str, account_binding: str) -> CodexRunBinding:
        """Refresh and freeze one exact model; display-only snapshots never bind."""

        if _MODEL_ID.fullmatch(model_id) is None or _ACCOUNT_BINDING.fullmatch(account_binding) is None:
            raise CodexRunBindingError("model_unavailable")
        with self._lock:
            snapshot = self._refresh_locked(expected_account_binding=account_binding)
        if snapshot.freshness != "fresh":
            if snapshot.error is None:
                raise CodexRunBindingError("catalog_unavailable")
            raise CodexRunBindingError(
                cast(_RunBindingErrorCode, snapshot.error.code),
                retryable=snapshot.error.retryable,
            )
        model = next((candidate for candidate in snapshot.models if candidate.model_id == model_id), None)
        if model is None:
            raise CodexRunBindingError("model_unavailable")
        assert snapshot.catalog_revision is not None
        assert snapshot.fetched_at is not None
        assert snapshot.account_binding is not None
        return CodexRunBinding(
            model,
            snapshot.catalog_revision,
            snapshot.fetched_at,
            snapshot.account_binding,
        )

    def restore_for_run(
        self,
        value: Mapping[str, object],
        *,
        model_id: str,
        account_binding: str,
    ) -> CodexRunBinding:
        """Restore an immutable proof without making Worker startup depend on current auth."""

        return CodexRunBinding.from_durable_snapshot(
            value,
            expected_model_id=model_id,
            expected_account_binding=account_binding,
        )

    def _refresh_locked(
        self,
        *,
        expected_account_binding: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> CodexModelCatalogSnapshot:
        try:
            raw_proxy = self._proxy_url()
            if raw_proxy is not None and not isinstance(raw_proxy, str):
                raise ValueError("Codex catalog proxy source is invalid")
            proxy_url = None if not raw_proxy else _normalize_loopback_proxy(raw_proxy)
        except (TypeError, ValueError):
            return self._failure("catalog_unavailable", retryable=False)
        try:
            with self._credentials.lease() as lease:
                lease_account_binding = _account_binding(lease.account_fingerprint)
                if expected_account_binding is not None and lease_account_binding != expected_account_binding:
                    return self._failure("auth_account_changed", retryable=False, allow_stale=False)
                if (
                    self._bound_account_fingerprint is not None
                    and lease.account_fingerprint != self._bound_account_fingerprint
                ):
                    # A display refresh may follow the current Codex account so
                    # the user can explicitly reselect. Never show account A's
                    # stale catalog while fetching or failing under account B.
                    self._last_success = None
                self._bound_account_fingerprint = lease.account_fingerprint
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
                    timeout_seconds=timeout_seconds,
                    proxy_url=proxy_url,
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
                    account_binding=lease_account_binding,
                    error=None,
                )
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
                account_binding=cached.account_binding,
                error=error,
            )
        return CodexModelCatalogSnapshot((), "unavailable", None, None, None, error)


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


def _plain_list(value: object) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _CatalogDecodeError("durable sequence is invalid")
    return list(value)


def _plain_mapping_list(value: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for item in _plain_list(value):
        if not isinstance(item, Mapping):
            raise _CatalogDecodeError("durable mapping sequence is invalid")
        result.append(dict(item))
    return result


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
                {"id": tier.id, "name": tier.name, "description": tier.description} for tier in model.service_tiers
            ],
            "defaultServiceTier": model.default_service_tier,
        }
        for model in models
    ]
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _account_binding(account_fingerprint: str) -> str:
    if not account_fingerprint or len(account_fingerprint) > 512 or "\x00" in account_fingerprint:
        raise ValueError("Codex account fingerprint is invalid")
    digest = hashlib.sha256(account_fingerprint.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _safe_account_header(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 512
        and value.strip() == value
        and value.isascii()
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


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
    "CodexRunBinding",
    "CodexRunBindingError",
    "CodexSubscriptionModelModule",
    "HttpxCodexCatalogHttpAdapter",
]
