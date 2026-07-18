"""Run-snapshot composition for the sole ModelGateway instance."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from offeragent_harness.config import ModelProvider, ModelSettings, ModelWireApi
from offeragent_harness.ports.model import ModelGateway
from offeragent_harness.ports.network_audit import NetworkAuditSink
from offeragent_harness.ports.secrets import SecretHandle, SecretResolver
from offeragent_harness.ports.system import Clock

from .codex_subscription import CODEX_SUBSCRIPTION_BASE_URL, AccountBoundModelCredentialSource
from .deepseek_chat import DEEPSEEK_BASE_URL, build_deepseek_gateway
from .factory import ResponsesProviderKind, ResponsesProviderSelection, build_responses_provider
from .network_audit import ModelNetworkAuditor
from .ollama import OllamaConfig, OllamaLocalProvider
from .openai_responses import ModelCredentialSource, StaticModelEndpointPolicy

_OFFICIAL_BASE = "https://api.openai.com/v1"


def compose_model_gateway(
    settings: ModelSettings,
    *,
    secret_scope_id: str,
    secrets: SecretResolver,
    network_enabled: bool,
    responses_transport: httpx.BaseTransport | None = None,
    deepseek_transport: httpx.BaseTransport | None = None,
    ollama_transport: httpx.AsyncBaseTransport | None = None,
    network_audit: NetworkAuditSink | None = None,
    clock: Clock | None = None,
    codex_credential_source: ModelCredentialSource | None = None,
) -> ModelGateway:
    """Capture one immutable Provider adapter from an immutable Run config."""

    if (network_audit is None) != (clock is None):
        raise ValueError("model network audit requires both a sink and clock")
    handle = None if settings.credential_handle is None else SecretHandle(settings.credential_handle)
    if settings.provider is ModelProvider.DEEPSEEK:
        endpoint = f"{DEEPSEEK_BASE_URL}/chat/completions"
        return build_deepseek_gateway(
            secret_scope_id=secret_scope_id,
            credential_handle=handle,
            secrets=secrets,
            endpoint_policy=StaticModelEndpointPolicy(frozenset({endpoint}), enabled=network_enabled),
            transport=deepseek_transport,
            network_audit=network_audit,
            clock=clock,
        )
    if settings.wire_api is ModelWireApi.OLLAMA_CHAT:
        config = OllamaConfig(base_url=settings.base_url)
        network_auditor = (
            None
            if network_audit is None or clock is None
            else ModelNetworkAuditor(
                workspace_id=secret_scope_id,
                provider_id="ollama",
                endpoint=config.endpoint,
                sink=network_audit,
                clock=clock,
            )
        )
        return OllamaLocalProvider(
            config=config,
            endpoint_policy=StaticModelEndpointPolicy(frozenset({config.endpoint}), enabled=network_enabled),
            transport=ollama_transport,
            network_auditor=network_auditor,
        )

    selection = _responses_selection(settings, secret_scope_id, handle)
    base_url = (
        CODEX_SUBSCRIPTION_BASE_URL
        if selection.kind is ResponsesProviderKind.CODEX_SUBSCRIPTION_EXPERIMENTAL
        else selection.base_url or _OFFICIAL_BASE
    )
    endpoint = base_url.rstrip("/") + ("" if base_url.rstrip("/").endswith("/responses") else "/responses")
    credential_source = codex_credential_source
    if selection.kind is ResponsesProviderKind.CODEX_SUBSCRIPTION_EXPERIMENTAL:
        if credential_source is None:
            raise ValueError("Codex subscription requires a Runtime credential broker")
        if settings.account_binding is None:
            raise ValueError("Codex subscription requires a verified account binding")
        credential_source = AccountBoundModelCredentialSource(
            credential_source,
            settings.account_binding,
        )
    return build_responses_provider(
        selection,
        secrets=secrets,
        endpoint_policy=StaticModelEndpointPolicy(frozenset({endpoint}), enabled=network_enabled),
        transport=responses_transport,
        network_audit=network_audit,
        clock=clock,
        credential_source=credential_source,
    )


def _responses_selection(
    settings: ModelSettings,
    secret_scope_id: str,
    handle: SecretHandle | None,
) -> ResponsesProviderSelection:
    if settings.provider is ModelProvider.CODEX:
        kind = ResponsesProviderKind.CODEX
        base_url = settings.base_url or None
    elif settings.provider is ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL:
        kind = ResponsesProviderKind.CODEX_SUBSCRIPTION_EXPERIMENTAL
        base_url = None
    elif settings.provider is ModelProvider.OPENAI and not settings.base_url:
        kind = ResponsesProviderKind.OPENAI
        base_url = None
    elif settings.provider is ModelProvider.OPENAI and settings.base_url.rstrip("/") == _OFFICIAL_BASE:
        kind = ResponsesProviderKind.OPENAI
        base_url = settings.base_url
    elif settings.provider is ModelProvider.LOCAL and _literal_loopback(settings.base_url):
        kind = ResponsesProviderKind.LOCAL
        base_url = settings.base_url
    else:
        kind = ResponsesProviderKind.OPENAI_COMPATIBLE
        base_url = settings.base_url
    return ResponsesProviderSelection(
        kind=kind,
        secret_scope_id=secret_scope_id,
        credential_handle=handle,
        base_url=base_url,
        organization_id=settings.organization_id,
        project_id=settings.project_id,
        service_tier=settings.service_tier,
        proxy_url=settings.proxy_url or None,
    )


def _literal_loopback(value: str) -> bool:
    host = (urlsplit(value).hostname or "").casefold()
    return host in {"127.0.0.1", "::1"}


__all__ = ["compose_model_gateway"]
