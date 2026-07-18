from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from offeragent_harness.protocol.messages import ModelsListParams
from offeragent_harness.providers.codex_subscription import (
    CodexCatalogError,
    CodexCatalogModel,
    CodexModelCatalogSnapshot,
    CodexModelServiceTier,
)
from offeragent_harness.runtime.model_management import ProductionModelCommandService
from offeragent_harness.testing import ManualCancellationToken

NOW = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)


class _Config:
    async def layer(self, scope: object, owner_id: str) -> object:
        del scope
        assert owner_id == "ws_test"
        return SimpleNamespace(revision=7)


class _Catalog:
    def __init__(self, snapshot: CodexModelCatalogSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def refresh(self) -> CodexModelCatalogSnapshot:
        self.calls += 1
        return self.snapshot


def _model() -> CodexCatalogModel:
    return CodexCatalogModel(
        model_id="gpt-catalog",
        display_name="GPT Catalog",
        description="Catalog-backed model",
        model_instructions="catalog-owned model baseline",
        use_responses_lite=False,
        input_modalities=("text", "image"),
        supports_image_detail_original=True,
        supports_hosted_search=True,
        web_search_tool_type="text",
        context_window=128_000,
        max_context_window=256_000,
        effective_context_window_percent=95,
        additional_speed_tiers=("fast",),
        service_tiers=(CodexModelServiceTier("fast", "Fast", "Lower latency"),),
        default_service_tier="fast",
    )


@pytest.mark.asyncio
async def test_models_list_projects_only_catalog_authority_without_provider_or_probe_state() -> None:
    catalog = _Catalog(
        CodexModelCatalogSnapshot(
            models=(_model(),),
            freshness="fresh",
            catalog_revision="sha256:" + "a" * 64,
            fetched_at=NOW,
            account_binding="sha256:" + "b" * 64,
            error=None,
        )
    )
    service = ProductionModelCommandService(
        config=_Config(),  # type: ignore[arg-type]
        workspace_id="ws_test",
        catalog=catalog,  # type: ignore[arg-type]
    )

    result = await service.list_models(ModelsListParams(), ManualCancellationToken())

    assert catalog.calls == 1
    assert result.config_revision == 7
    assert result.catalog_freshness == "fresh"
    assert result.models[0].model == "gpt-catalog"
    assert result.models[0].input_modalities == ["text", "image"]
    assert result.models[0].supports_hosted_search is True
    assert "provider" not in result.models[0].to_wire()
    assert "available" not in result.models[0].to_wire()


@pytest.mark.asyncio
async def test_stale_catalog_is_display_only_and_reports_bounded_auth_failure() -> None:
    catalog = _Catalog(
        CodexModelCatalogSnapshot(
            models=(_model(),),
            freshness="stale",
            catalog_revision="sha256:" + "c" * 64,
            fetched_at=NOW,
            account_binding=None,
            error=CodexCatalogError("auth_required", False),
        )
    )
    service = ProductionModelCommandService(
        config=_Config(),  # type: ignore[arg-type]
        workspace_id="ws_test",
        catalog=catalog,  # type: ignore[arg-type]
    )

    result = await service.list_models(ModelsListParams(), ManualCancellationToken())

    assert result.models[0].model == "gpt-catalog"
    assert result.catalog_freshness == "stale"
    assert result.error is not None
    assert result.error.details == {"reason": "auth_required"}
