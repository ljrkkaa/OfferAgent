"""Composition root for the sole production ModelGateway adapter."""

from __future__ import annotations

import httpx

from offeragent_harness.config import ModelSettings
from offeragent_harness.ports.model import ModelGateway
from offeragent_harness.ports.network_audit import NetworkAuditSink
from offeragent_harness.ports.system import Clock

from .codex_subscription import AccountBoundModelCredentialSource
from .network_audit import ModelNetworkAuditor
from .openai_responses import (
    ModelCredentialSource,
    OpenAIResponsesConfig,
    OpenAIResponsesGateway,
    StaticModelEndpointPolicy,
)


def compose_model_gateway(
    settings: ModelSettings,
    *,
    workspace_id: str,
    network_enabled: bool,
    responses_transport: httpx.BaseTransport | None = None,
    network_audit: NetworkAuditSink | None = None,
    clock: Clock | None = None,
    codex_credential_source: ModelCredentialSource,
) -> ModelGateway:
    """Capture one account-bound Codex Subscription adapter for a Run."""

    if (network_audit is None) != (clock is None):
        raise ValueError("model network audit requires both a sink and clock")
    if settings.account_binding is None:
        raise ValueError("Codex subscription requires a verified account binding")

    config = OpenAIResponsesConfig(proxy_url=settings.proxy_url)
    network_auditor = (
        None
        if network_audit is None or clock is None
        else ModelNetworkAuditor(
            workspace_id=workspace_id,
            provider_id=config.provider_id,
            endpoint=config.endpoint,
            sink=network_audit,
            clock=clock,
        )
    )
    return OpenAIResponsesGateway(
        config=config,
        endpoint_policy=StaticModelEndpointPolicy(
            frozenset({config.endpoint}),
            enabled=network_enabled,
        ),
        transport=responses_transport,
        network_auditor=network_auditor,
        credential_source=AccountBoundModelCredentialSource(
            codex_credential_source,
            settings.account_binding,
        ),
    )


__all__ = ["compose_model_gateway"]
