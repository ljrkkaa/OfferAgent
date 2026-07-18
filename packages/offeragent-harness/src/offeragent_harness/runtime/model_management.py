"""Catalog-backed Codex Subscription model discovery."""

from __future__ import annotations

import asyncio

from offeragent_harness.config import ConfigScope
from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.ports import CancellationToken
from offeragent_harness.protocol.errors import ErrorEnvelope
from offeragent_harness.protocol.messages import (
    ModelDescriptor,
    ModelServiceTierDescriptor,
    ModelsListParams,
    ModelsListResult,
)
from offeragent_harness.providers.codex_subscription import (
    CodexCatalogError,
    CodexSubscriptionModelModule,
)

from .config_service import ConfigService


class ProductionModelCommandService:
    """Project the sole authoritative account-bound Codex model catalog."""

    def __init__(
        self,
        *,
        config: ConfigService,
        workspace_id: str,
        catalog: CodexSubscriptionModelModule,
    ) -> None:
        if not workspace_id:
            raise ValueError("model management workspace identity must be non-empty")
        self._config = config
        self._workspace_id = workspace_id
        self._catalog = catalog

    async def list_models(
        self,
        params: ModelsListParams,
        cancellation: CancellationToken,
    ) -> ModelsListResult:
        del params
        cancellation.checkpoint()
        layer = await self._config.layer(ConfigScope.WORKSPACE, self._workspace_id)
        catalog = await asyncio.to_thread(self._catalog.refresh)
        cancellation.checkpoint()
        return ModelsListResult(
            models=[
                ModelDescriptor(
                    model=model.model_id,
                    display_name=model.display_name,
                    input_modalities=list(model.input_modalities),
                    supports_image_detail_original=model.supports_image_detail_original,
                    supports_hosted_search=model.supports_hosted_search,
                    web_search_tool_type=model.web_search_tool_type,
                    context_window=model.context_window,
                    max_context_window=model.max_context_window,
                    effective_context_window_percent=model.effective_context_window_percent,
                    additional_speed_tiers=list(model.additional_speed_tiers),
                    service_tiers=[
                        ModelServiceTierDescriptor(
                            id=tier.id,
                            name=tier.name,
                            description=tier.description,
                        )
                        for tier in model.service_tiers
                    ],
                    default_service_tier=model.default_service_tier,
                    supports_fast_mode=model.supports_fast_mode,
                    max_context_tokens=model.context_window,
                )
                for model in catalog.models
            ],
            config_revision=layer.revision,
            catalog_freshness=catalog.freshness,
            catalog_revision=catalog.catalog_revision,
            fetched_at=None if catalog.fetched_at is None else catalog.fetched_at.isoformat(),
            account_binding=catalog.account_binding,
            error=None if catalog.error is None else _catalog_error(catalog.error),
        )


def _catalog_error(error: CodexCatalogError) -> ErrorEnvelope:
    auth = error.code in {"auth_account_changed", "auth_required"}
    return ErrorEnvelope(
        code=ErrorCode.AUTH_REQUIRED if auth else ErrorCode.PROVIDER_UNREACHABLE,
        retryable=error.retryable,
        cancelled=False,
        user_visible_message=(
            "本机 Codex 登录已失效或账户发生变化, 请重新登录后重试。"
            if auth
            else "暂时无法验证 Codex 模型目录; 仍可浏览上次成功的目录快照。"
        ),
        details={"reason": error.code},
    )


__all__ = ["ProductionModelCommandService"]
