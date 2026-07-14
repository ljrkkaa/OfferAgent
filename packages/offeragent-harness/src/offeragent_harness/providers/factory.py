"""Typed provider selection without changing the Agent Loop."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

import httpx

from offeragent_harness.ports.network_audit import NetworkAuditSink
from offeragent_harness.ports.secrets import SecretHandle, SecretResolver
from offeragent_harness.ports.system import Clock

from .codex_subscription import (
    CODEX_SUBSCRIPTION_BASE_URL,
    CODEX_SUBSCRIPTION_PROVIDER_ID,
)
from .network_audit import ModelNetworkAuditor
from .openai_responses import (
    ModelCredentialSource,
    ModelEndpointPolicy,
    ModelProviderConfigurationError,
    OpenAIResponsesConfig,
    OpenAIResponsesGateway,
    model_secret_provider_id,
)

_OPENAI_RESPONSES_BASE = "https://api.openai.com/v1"


class ResponsesProviderKind(str, Enum):
    CODEX = "codex"
    CODEX_SUBSCRIPTION_EXPERIMENTAL = "codex-subscription-experimental"
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai-compatible"
    LOCAL = "local"


@dataclass(frozen=True, slots=True)
class ResponsesProviderSelection:
    kind: ResponsesProviderKind
    secret_scope_id: str
    credential_handle: SecretHandle | None
    base_url: str | None = None
    organization_id: str | None = None
    project_id: str | None = None
    service_tier: str | None = None
    proxy_url: str | None = None


class CodexResponsesProvider(OpenAIResponsesGateway):
    """Codex model inference over Responses; never starts Codex app-server/CLI."""


class CodexSubscriptionProvider(OpenAIResponsesGateway):
    """Experimental ChatGPT-subscription inference with a read-only local login."""


class OpenAIProvider(OpenAIResponsesGateway):
    """Official OpenAI Responses provider authenticated by an opaque API-key handle."""


class OpenAICompatibleProvider(OpenAIResponsesGateway):
    """User-selected Responses-compatible endpoint with the same strict decoder."""


class LocalModelProvider(OpenAIResponsesGateway):
    """Loopback-only Responses-compatible local inference provider."""


def build_responses_provider(
    selection: ResponsesProviderSelection,
    *,
    secrets: SecretResolver,
    endpoint_policy: ModelEndpointPolicy,
    transport: httpx.BaseTransport | None = None,
    network_audit: NetworkAuditSink | None = None,
    clock: Clock | None = None,
    credential_source: ModelCredentialSource | None = None,
) -> OpenAIResponsesGateway:
    if (network_audit is None) != (clock is None):
        raise ModelProviderConfigurationError("model network audit requires both a sink and clock")
    base_url, provider_id, provider_type, require_credential = _provider_parameters(selection)
    subscription = selection.kind is ResponsesProviderKind.CODEX_SUBSCRIPTION_EXPERIMENTAL
    if subscription:
        if selection.credential_handle is not None or selection.base_url is not None:
            raise ModelProviderConfigurationError("Codex subscription credentials and endpoint are fixed locally")
        if selection.organization_id is not None or selection.project_id is not None:
            raise ModelProviderConfigurationError("Codex subscription does not accept API organization/project headers")
        if credential_source is None:
            raise ModelProviderConfigurationError("Codex subscription requires a Runtime credential broker")
        source = credential_source
    else:
        if credential_source is not None:
            raise ModelProviderConfigurationError("external credential source is reserved for Codex subscription")
        if selection.proxy_url is not None:
            raise ModelProviderConfigurationError("explicit proxy is reserved for Codex subscription")
        source = None
    config = OpenAIResponsesConfig(
        provider_id=provider_id,
        base_url=base_url,
        secret_scope_id=selection.secret_scope_id,
        credential_handle=selection.credential_handle,
        require_credential=require_credential,
        external_credential=subscription,
        organization_id=selection.organization_id,
        project_id=selection.project_id,
        service_tier=selection.service_tier,
        supports_max_output_tokens=not subscription,
        supports_temperature=not subscription,
        allow_missing_event_stream_content_type=subscription,
        project_codex_subscription_schema=subscription,
        proxy_url=selection.proxy_url,
    )
    network_auditor = (
        None
        if network_audit is None or clock is None
        else ModelNetworkAuditor(
            workspace_id=selection.secret_scope_id,
            provider_id=provider_id,
            endpoint=config.endpoint,
            sink=network_audit,
            clock=clock,
        )
    )
    return provider_type(
        config=config,
        secrets=secrets,
        endpoint_policy=endpoint_policy,
        transport=transport,
        network_auditor=network_auditor,
        credential_source=source,
    )


def _provider_parameters(
    selection: ResponsesProviderSelection,
) -> tuple[str, str, type[OpenAIResponsesGateway], bool]:
    if selection.kind is ResponsesProviderKind.CODEX:
        _require_official_base(selection.base_url)
        return (
            _OPENAI_RESPONSES_BASE,
            "codex",
            CodexResponsesProvider,
            True,
        )
    if selection.kind is ResponsesProviderKind.CODEX_SUBSCRIPTION_EXPERIMENTAL:
        return (
            CODEX_SUBSCRIPTION_BASE_URL,
            CODEX_SUBSCRIPTION_PROVIDER_ID,
            CodexSubscriptionProvider,
            True,
        )
    if selection.kind is ResponsesProviderKind.OPENAI:
        _require_official_base(selection.base_url)
        return (
            _OPENAI_RESPONSES_BASE,
            "openai",
            OpenAIProvider,
            True,
        )
    if selection.base_url is None:
        raise ModelProviderConfigurationError("custom/local model provider requires an explicit base URL")
    if selection.kind is ResponsesProviderKind.LOCAL:
        host = (urlsplit(selection.base_url).hostname or "").casefold()
        if host not in {"127.0.0.1", "::1"}:
            raise ModelProviderConfigurationError("LocalModelProvider must remain on a literal loopback endpoint")
        return selection.base_url, "local", LocalModelProvider, False
    return (
        selection.base_url,
        model_secret_provider_id("openai-compatible", selection.base_url),
        OpenAICompatibleProvider,
        selection.credential_handle is not None,
    )


def _require_official_base(value: str | None) -> None:
    if value is not None and value.rstrip("/") != _OPENAI_RESPONSES_BASE:
        raise ModelProviderConfigurationError("official Codex/OpenAI provider endpoint cannot be overridden")


__all__ = [
    "CodexResponsesProvider",
    "CodexSubscriptionProvider",
    "LocalModelProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "ResponsesProviderKind",
    "ResponsesProviderSelection",
    "build_responses_provider",
]
