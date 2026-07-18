from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from offeragent_harness.providers import codex_subscription as codex_subscription_module
from offeragent_harness.providers.codex_subscription import (
    CODEX_SUBSCRIPTION_MODELS_ENDPOINT,
    CodexCatalogHttpRequest,
    CodexCatalogHttpResponse,
    CodexCatalogTransportError,
    CodexRunBindingError,
    CodexSubscriptionModelModule,
    HttpxCodexCatalogHttpAdapter,
)
from offeragent_harness.providers.openai_responses import (
    ModelCredentialLease,
    ModelCredentialSourceError,
)

NOW = datetime(2026, 7, 18, 5, 0, tzinfo=timezone.utc)


def _account_binding(account: str) -> str:
    fingerprint = f"account-{account}"
    return f"sha256:{hashlib.sha256(fingerprint.encode()).hexdigest()}"


class _CredentialSource:
    def __init__(self, leases: list[tuple[bytes, str]] | ModelCredentialSourceError) -> None:
        self._leases = leases
        self.calls = 0

    @contextmanager
    def lease(self) -> Iterator[ModelCredentialLease]:
        self.calls += 1
        if isinstance(self._leases, ModelCredentialSourceError):
            raise self._leases
        token, account = self._leases[min(self.calls - 1, len(self._leases) - 1)]
        material = bytearray(token)
        view = memoryview(material)
        try:
            yield ModelCredentialLease(
                material=view,
                headers=MappingProxyType(
                    {
                        "ChatGPT-Account-ID": account,
                        "originator": "codex_cli_rs",
                        "User-Agent": "codex_cli_rs/test (OfferAgent)",
                    }
                ),
                credential_fingerprint=f"credential-{self.calls}",
                account_fingerprint=f"account-{account}",
            )
        finally:
            view.release()
            material[:] = b"\0" * len(material)


class _Http:
    def __init__(self, outcomes: list[CodexCatalogHttpResponse | CodexCatalogTransportError]) -> None:
        self._outcomes = outcomes
        self.requests: list[CodexCatalogHttpRequest] = []

    def get(self, request: CodexCatalogHttpRequest) -> CodexCatalogHttpResponse:
        self.requests.append(request)
        outcome = self._outcomes[min(len(self.requests) - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, CodexCatalogTransportError):
            raise outcome
        return outcome


def test_catalog_http_ignores_ambient_proxy_when_runtime_config_selects_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class _Response:
        status_code = 200

        def __init__(self) -> None:
            self.headers = {"Content-Type": "application/json"}

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def iter_bytes(self) -> Iterator[bytes]:
            yield b'{"models":[]}'

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def stream(self, method: str, endpoint: str, *, headers: Mapping[str, str]) -> _Response:
            observed.update({"method": method, "endpoint": endpoint, "headers": headers})
            return _Response()

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:4567")
    monkeypatch.setattr(codex_subscription_module.httpx, "Client", _Client)

    result = HttpxCodexCatalogHttpAdapter().get(
        CodexCatalogHttpRequest(CODEX_SUBSCRIPTION_MODELS_ENDPOINT, {"Accept": "application/json"})
    )

    assert result.status == 200
    assert observed["trust_env"] is False
    assert observed["proxy"] is None


def _response(
    models: list[dict[str, object]],
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
) -> CodexCatalogHttpResponse:
    return CodexCatalogHttpResponse(
        status=status,
        headers=headers or {"Content-Type": "application/json"},
        body=json.dumps({"models": models}, separators=(",", ":")).encode(),
    )


def _model(
    slug: str,
    *,
    visibility: str = "list",
    modalities: list[str] | None = None,
) -> dict[str, object]:
    return {
        "slug": slug,
        "display_name": f"Display {slug}",
        "description": f"Description {slug}",
        "visibility": visibility,
        "input_modalities": modalities or ["text", "image"],
        "supports_image_detail_original": True,
        "supports_search_tool": True,
        "web_search_tool_type": "text_and_image",
        "context_window": 272_000,
        "max_context_window": 1_000_000,
        "effective_context_window_percent": 95,
        "additional_speed_tiers": ["fast"],
        "service_tiers": [{"id": "priority", "name": "Fast", "description": "1.5x speed"}],
        "default_service_tier": None,
    }


def test_refresh_uses_account_bound_auth_and_projects_only_visible_models() -> None:
    credentials = _CredentialSource([(b"access-one", "acct-one")])
    http = _Http(
        [
            _response(
                [
                    _model("gpt-visible"),
                    _model("gpt-hidden", visibility="hide"),
                    _model("gpt-internal", visibility="none"),
                    _model("text-only", modalities=["text"]),
                ]
            )
        ]
    )
    module = CodexSubscriptionModelModule(credentials=credentials, http=http, now=lambda: NOW)

    snapshot = module.refresh()

    assert snapshot.freshness == "fresh"
    assert snapshot.fetched_at == NOW
    assert snapshot.account_binding == _account_binding("acct-one")
    assert snapshot.error is None
    assert [model.model_id for model in snapshot.models] == ["gpt-visible", "text-only"]
    first = snapshot.models[0]
    assert first.display_name == "Display gpt-visible"
    assert first.description == "Description gpt-visible"
    assert first.input_modalities == ("text", "image")
    assert first.supports_image_detail_original is True
    assert first.supports_hosted_search is True
    assert first.web_search_tool_type == "text_and_image"
    assert first.context_window == 272_000
    assert first.max_context_window == 1_000_000
    assert first.effective_context_window_percent == 95
    assert first.additional_speed_tiers == ("fast",)
    assert first.service_tiers[0].id == "priority"
    assert first.supports_fast_mode is True
    request = http.requests[0]
    assert request.endpoint == CODEX_SUBSCRIPTION_MODELS_ENDPOINT
    assert request.headers["Authorization"] == "Bearer access-one"
    assert request.headers["ChatGPT-Account-ID"] == "acct-one"
    assert request.headers["originator"] == "codex_cli_rs"
    assert "refresh" not in repr(request).casefold()
    assert "access-one" not in repr(request)


def test_catalog_requests_use_the_current_validated_runtime_proxy_without_caching_it() -> None:
    current_proxy = "http://127.0.0.1:7896"
    http = _Http([_response([_model("gpt-visible")]), _response([_model("gpt-visible")])])
    module = CodexSubscriptionModelModule(
        credentials=_CredentialSource([(b"access-one", "acct-one")]),
        http=http,
        now=lambda: NOW,
        proxy_url=lambda: current_proxy,
    )

    assert module.refresh().freshness == "fresh"
    current_proxy = "http://[::1]:8080"
    assert module.refresh().freshness == "fresh"

    assert [request.proxy_url for request in http.requests] == [
        "http://127.0.0.1:7896",
        "http://[::1]:8080",
    ]


def test_failed_refresh_returns_last_success_as_display_only_stale_catalog() -> None:
    http = _Http([_response([_model("gpt-visible")]), CodexCatalogTransportError("offline")])
    module = CodexSubscriptionModelModule(
        credentials=_CredentialSource([(b"access-one", "acct-one")]),
        http=http,
        now=lambda: NOW,
    )

    fresh = module.refresh()
    stale = module.refresh()

    assert fresh.freshness == "fresh"
    assert stale.freshness == "stale"
    assert stale.display_only is True
    assert stale.models == fresh.models
    assert stale.catalog_revision == fresh.catalog_revision
    assert stale.error is not None
    assert stale.error.code == "catalog_unreachable"
    assert stale.error.retryable is True


@pytest.mark.parametrize(
    ("outcome", "expected_code", "retryable"),
    [
        (_response([]), "catalog_empty", False),
        (CodexCatalogHttpResponse(200, {}, b'{"models":['), "catalog_invalid_response", False),
        (_response([], status=401), "auth_required", False),
        (_response([], status=429, headers={"Retry-After": "7"}), "catalog_rate_limited", True),
        (CodexCatalogTransportError("offline"), "catalog_unreachable", True),
    ],
)
def test_catalog_failures_are_stable_non_secret_results(
    outcome: CodexCatalogHttpResponse | CodexCatalogTransportError,
    expected_code: str,
    retryable: bool,
) -> None:
    module = CodexSubscriptionModelModule(
        credentials=_CredentialSource([(b"secret-access-token", "acct-one")]),
        http=_Http([outcome]),
        now=lambda: NOW,
    )

    snapshot = module.refresh()

    assert snapshot.freshness == "unavailable"
    assert snapshot.models == ()
    assert snapshot.error is not None
    assert snapshot.error.code == expected_code
    assert snapshot.error.retryable is retryable
    assert "secret-access-token" not in repr(snapshot)


def test_credential_failure_does_not_touch_http() -> None:
    http = _Http([_response([_model("must-not-run")])])
    module = CodexSubscriptionModelModule(
        credentials=_CredentialSource(ModelCredentialSourceError("auth_required")),
        http=http,
        now=lambda: NOW,
    )

    snapshot = module.refresh()

    assert snapshot.freshness == "unavailable"
    assert snapshot.error is not None and snapshot.error.code == "auth_required"
    assert http.requests == []


def test_account_change_replaces_the_display_catalog_and_allows_explicit_reselection() -> None:
    credentials = _CredentialSource([(b"access-one", "acct-one"), (b"access-two", "acct-two")])
    http = _Http(
        [
            _response([_model("gpt-account-one")]),
            _response([_model("gpt-account-two")]),
        ]
    )
    module = CodexSubscriptionModelModule(credentials=credentials, http=http, now=lambda: NOW)

    assert module.refresh().models[0].model_id == "gpt-account-one"
    changed = module.refresh()

    assert changed.freshness == "fresh"
    assert changed.models[0].model_id == "gpt-account-two"
    assert changed.account_binding == _account_binding("acct-two")
    assert len(http.requests) == 2


def test_duplicate_or_malformed_visible_model_invalidates_the_whole_catalog() -> None:
    duplicate = _model("gpt-duplicate")
    malformed = _model("gpt-malformed")
    malformed["supports_search_tool"] = "yes"
    for models in ([duplicate, duplicate], [malformed]):
        module = CodexSubscriptionModelModule(
            credentials=_CredentialSource([(b"access-one", "acct-one")]),
            http=_Http([_response(models)]),
            now=lambda: NOW,
        )

        snapshot = module.refresh()

        assert snapshot.freshness == "unavailable"
        assert snapshot.error is not None and snapshot.error.code == "catalog_invalid_response"


def test_run_binding_refreshes_and_freezes_the_exact_catalog_model() -> None:
    http = _Http([_response([_model("gpt-selected"), _model("gpt-other")])])
    module = CodexSubscriptionModelModule(
        credentials=_CredentialSource([(b"access-one", "acct-one")]),
        http=http,
        now=lambda: NOW,
    )

    binding = module.bind_for_run("gpt-selected", _account_binding("acct-one"))

    assert binding.model.model_id == "gpt-selected"
    assert binding.catalog_revision.startswith("sha256:")
    assert binding.bound_at == NOW
    assert binding.account_binding == _account_binding("acct-one")
    assert len(http.requests) == 1
    with pytest.raises(AttributeError):
        binding.model = binding.model  # type: ignore[misc]


def test_run_binding_never_uses_missing_or_stale_catalog_models() -> None:
    http = _Http(
        [
            _response([_model("gpt-selected")]),
            _response([_model("gpt-other")]),
            CodexCatalogTransportError("offline"),
        ]
    )
    module = CodexSubscriptionModelModule(
        credentials=_CredentialSource([(b"access-one", "acct-one")]),
        http=http,
        now=lambda: NOW,
    )

    account_binding = _account_binding("acct-one")
    assert module.bind_for_run("gpt-selected", account_binding).model.model_id == "gpt-selected"
    with pytest.raises(CodexRunBindingError) as disappeared:
        module.bind_for_run("gpt-selected", account_binding)
    with pytest.raises(CodexRunBindingError) as stale:
        module.bind_for_run("gpt-other", account_binding)

    assert disappeared.value.code == "model_unavailable"
    assert stale.value.code == "catalog_unreachable"
    assert "gpt-selected" not in str(disappeared.value)


def test_run_binding_rejects_a_persisted_selection_from_another_account_before_http() -> None:
    selected = CodexSubscriptionModelModule(
        credentials=_CredentialSource([(b"access-one", "acct-one")]),
        http=_Http([_response([_model("gpt-selected")])]),
        now=lambda: NOW,
    ).refresh()
    http = _Http([_response([_model("gpt-selected")])])
    restarted = CodexSubscriptionModelModule(
        credentials=_CredentialSource([(b"access-two", "acct-two")]),
        http=http,
        now=lambda: NOW,
    )

    assert selected.account_binding is not None
    with pytest.raises(CodexRunBindingError) as caught:
        restarted.bind_for_run("gpt-selected", selected.account_binding)

    assert caught.value.code == "auth_account_changed"
    assert http.requests == []


def test_durable_run_binding_restores_without_requiring_startup_authentication() -> None:
    selected_module = CodexSubscriptionModelModule(
        credentials=_CredentialSource([(b"token-one", "acct-one")]),
        http=_Http([_response([_model("gpt-selected")])]),
        now=lambda: NOW,
    )
    selected = selected_module.bind_for_run("gpt-selected", _account_binding("acct-one"))
    unavailable_source = _CredentialSource(ModelCredentialSourceError("auth_required"))
    restarted = CodexSubscriptionModelModule(
        credentials=unavailable_source,
        http=_Http([CodexCatalogTransportError()]),
        now=lambda: NOW,
    )

    restored = restarted.restore_for_run(
        selected.durable_snapshot(),
        model_id="gpt-selected",
        account_binding=selected.account_binding,
    )

    assert restored == selected
    assert unavailable_source.calls == 0
