from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from offeragent_harness.providers.codex_subscription import (
    CODEX_SUBSCRIPTION_MODELS_ENDPOINT,
    CodexCatalogHttpRequest,
    CodexCatalogHttpResponse,
    CodexCatalogTransportError,
    CodexSubscriptionModelModule,
)
from offeragent_harness.providers.openai_responses import (
    ModelCredentialLease,
    ModelCredentialSourceError,
)

NOW = datetime(2026, 7, 18, 5, 0, tzinfo=timezone.utc)


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
        "service_tiers": [
            {"id": "priority", "name": "Fast", "description": "1.5x speed"}
        ],
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


def test_account_change_fails_closed_before_a_second_http_request() -> None:
    credentials = _CredentialSource(
        [(b"access-one", "acct-one"), (b"access-two", "acct-two")]
    )
    http = _Http([_response([_model("gpt-visible")])])
    module = CodexSubscriptionModelModule(credentials=credentials, http=http, now=lambda: NOW)

    assert module.refresh().freshness == "fresh"
    changed = module.refresh()

    assert changed.freshness == "unavailable"
    assert changed.models == ()
    assert changed.error is not None and changed.error.code == "auth_account_changed"
    assert len(http.requests) == 1


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
