from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from offeragent_harness.config import ConfigPatch, ConfigScope
from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.models import (
    ModelError,
    ModelEvent,
    ModelEventKind,
    ModelFinishReason,
    ModelPurpose,
    ModelRequest,
    ModelRole,
)
from offeragent_harness.ports import CancellationToken
from offeragent_harness.protocol.messages import ModelsHealthParams, ModelsListParams
from offeragent_harness.providers.codex_subscription import (
    CODEX_SUBSCRIPTION_PROVIDER_ID,
    CodexCatalogError,
    CodexCatalogModel,
    CodexModelCatalogSnapshot,
    CodexModelServiceTier,
)
from offeragent_harness.runtime.config_service import ConfigService, ConfigUpdateCommand
from offeragent_harness.runtime.model_management import ProductionModelCommandService
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    FakeRunCancelled,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
    RecordingEventSink,
)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
WORKSPACE_ID = "wsi_12345678-1234-4234-8234-123456789abc"
PROFILE_ID = "profile-local"
MANAGED_ID = "machine-local"
CLIENT_REQUEST_ID = "req_model_health_1"
ACCOUNT_BINDING = "sha256:" + "b" * 64


class _SecretStore:
    def metadata(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("Codex Subscription model management must not read legacy secrets")


class _Gateway:
    def __init__(self, *, terminal: ModelEventKind = ModelEventKind.COMPLETED, error_code: str = "") -> None:
        self.terminal = terminal
        self.error_code = error_code
        self.requests: list[ModelRequest] = []
        self.started = asyncio.Event()
        self.block = False

    async def stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        self.started.set()
        yield ModelEvent(request.request_id, 1, ModelEventKind.STARTED)
        if self.block:
            await asyncio.Event().wait()
        cancellation.checkpoint()
        if self.terminal is ModelEventKind.COMPLETED:
            yield ModelEvent(
                request.request_id,
                2,
                ModelEventKind.COMPLETED,
                finish_reason=ModelFinishReason.STOP,
            )
        elif self.terminal is ModelEventKind.ERROR:
            yield ModelEvent(
                request.request_id,
                2,
                ModelEventKind.ERROR,
                error=ModelError(self.error_code, "provider probe error", False, False),
            )
        else:
            yield ModelEvent(
                request.request_id,
                2,
                ModelEventKind.CANCELLED,
                finish_reason=ModelFinishReason.CANCELLED,
            )


class _GatewayFactory:
    def __init__(self, gateway: _Gateway | BaseException) -> None:
        self.gateway = gateway
        self.calls: list[tuple[Any, bool]] = []

    def __call__(self, settings: Any, network_enabled: bool) -> _Gateway:
        self.calls.append((settings, network_enabled))
        if isinstance(self.gateway, BaseException):
            raise self.gateway
        return self.gateway


class _Catalog:
    def __init__(self, snapshot: CodexModelCatalogSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def refresh(self, *, timeout_seconds: float = 5.0) -> CodexModelCatalogSnapshot:
        assert 0 < timeout_seconds <= 60
        self.calls += 1
        return self.snapshot


def _config(clock: ManualClock) -> ConfigService:
    return ConfigService(
        unit_of_work=InMemoryUnitOfWorkFactory(),
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
    )


async def _update(config: ConfigService, payload: dict[str, Any], *, revision: int = 0) -> None:
    await config.update(
        ConfigUpdateCommand(
            ConfigScope.WORKSPACE,
            WORKSPACE_ID,
            revision,
            f"model-config-{revision}",
            "local-user",
            ConfigPatch.model_validate(payload),
        )
    )


async def _select_model(
    config: ConfigService,
    model_id: str,
    *,
    account_binding: str = ACCOUNT_BINDING,
    revision: int = 0,
) -> None:
    await _update(
        config,
        {"model": {"model": model_id, "account_binding": account_binding}},
        revision=revision,
    )


def _model(model_id: str, *, modalities: tuple[str, ...] = ("text",)) -> CodexCatalogModel:
    return CodexCatalogModel(
        model_id=model_id,
        display_name=model_id,
        description="Catalog-backed model",
        input_modalities=modalities,
        supports_image_detail_original="image" in modalities,
        supports_hosted_search=True,
        web_search_tool_type="text_and_image",
        context_window=272_000,
        max_context_window=1_000_000,
        effective_context_window_percent=95,
        additional_speed_tiers=("fast",),
        service_tiers=(CodexModelServiceTier("priority", "Fast", "1.5x speed"),),
        default_service_tier=None,
    )


def _snapshot(
    *models: CodexCatalogModel,
    freshness: str = "fresh",
    account_binding: str | None = ACCOUNT_BINDING,
    error: CodexCatalogError | None = None,
) -> CodexModelCatalogSnapshot:
    return CodexModelCatalogSnapshot(
        models=tuple(models),
        freshness=freshness,  # type: ignore[arg-type]
        catalog_revision="sha256:" + "a" * 64 if models else None,
        fetched_at=NOW if models else None,
        account_binding=account_binding,
        error=error,
    )


def _service(
    config: ConfigService,
    clock: ManualClock,
    factory: _GatewayFactory,
    catalog: _Catalog,
) -> ProductionModelCommandService:
    return ProductionModelCommandService(
        config=config,
        managed_owner_id=MANAGED_ID,
        profile_id=PROFILE_ID,
        workspace_id=WORKSPACE_ID,
        secrets=_SecretStore(),  # type: ignore[arg-type]
        gateway_factory=factory,
        clock=clock,
        ids=DeterministicIdGenerator(),
        catalog=catalog,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_shell_execution_requires_an_explicit_persisted_configuration_flag() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(config, {"execution": {"shell_enabled": True}})

    snapshot = await config.snapshot(
        managed_owner_id=MANAGED_ID,
        profile_id=PROFILE_ID,
        workspace_id=WORKSPACE_ID,
    )

    assert snapshot.config.execution.shell_enabled is True
    assert snapshot.sources["execution.shell_enabled"] == ConfigScope.WORKSPACE.value


@pytest.mark.asyncio
async def test_model_list_projects_the_live_codex_catalog_through_the_public_protocol() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    catalog = _Catalog(_snapshot(_model("gpt-catalog", modalities=("text", "image"))))
    factory = _GatewayFactory(_Gateway())
    service = _service(config, clock, factory, catalog)

    result = await service.list_models(ModelsListParams(), ManualCancellationToken())

    assert catalog.calls == 1
    assert result.catalog_freshness == "fresh"
    assert result.catalog_revision == "sha256:" + "a" * 64
    assert result.fetched_at == NOW.isoformat()
    assert result.account_binding == ACCOUNT_BINDING
    assert result.error is None
    assert len(result.models) == 1
    descriptor = result.models[0]
    assert descriptor.provider == CODEX_SUBSCRIPTION_PROVIDER_ID
    assert descriptor.model == "gpt-catalog"
    assert descriptor.input_modalities == ["text", "image"]
    assert descriptor.supports_image_detail_original is True
    assert descriptor.supports_hosted_search is True
    assert descriptor.supports_fast_mode is True
    assert descriptor.service_tiers[0].id == "priority"
    assert descriptor.available is True
    assert factory.calls == []


@pytest.mark.asyncio
async def test_vision_health_uses_exact_live_catalog_capability_without_probe() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _select_model(config, "gpt-vision")
    catalog = _Catalog(_snapshot(_model("gpt-vision", modalities=("text", "image"))))
    factory = _GatewayFactory(AssertionError("vision health must not create a gateway"))
    service = _service(config, clock, factory, catalog)

    result = await service.health(
        ModelsHealthParams(
            provider=CODEX_SUBSCRIPTION_PROVIDER_ID,
            model="gpt-vision",
            capability="vision",
            client_request_id=CLIENT_REQUEST_ID,
        ),
        ManualCancellationToken(),
    )

    assert result.status == "healthy"
    assert result.capability == "vision"
    assert result.error is None
    assert catalog.calls == 1
    assert factory.calls == []


@pytest.mark.asyncio
async def test_vision_health_rejects_text_only_model_without_probe() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _select_model(config, "text-only")
    catalog = _Catalog(_snapshot(_model("text-only")))
    factory = _GatewayFactory(AssertionError("text-only vision must fail before gateway creation"))
    service = _service(config, clock, factory, catalog)

    result = await service.health(
        ModelsHealthParams(
            provider=CODEX_SUBSCRIPTION_PROVIDER_ID,
            model="text-only",
            capability="vision",
            client_request_id=CLIENT_REQUEST_ID,
        ),
        ManualCancellationToken(),
    )

    assert result.status == "unsupported"
    assert result.error is not None
    assert result.error.code is ErrorCode.PROVIDER_IMAGE_UNSUPPORTED
    assert result.error.details == {"reason": "image_unsupported"}
    assert factory.calls == []


@pytest.mark.asyncio
async def test_vision_health_without_catalog_fails_closed_without_probe() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _select_model(config, "gpt-vision")
    factory = _GatewayFactory(AssertionError("unknown vision capability must not create a gateway"))
    service = ProductionModelCommandService(
        config=config,
        managed_owner_id=MANAGED_ID,
        profile_id=PROFILE_ID,
        workspace_id=WORKSPACE_ID,
        secrets=_SecretStore(),  # type: ignore[arg-type]
        gateway_factory=factory,
        clock=clock,
        ids=DeterministicIdGenerator(),
        catalog=None,
    )

    result = await service.health(
        ModelsHealthParams(
            provider=CODEX_SUBSCRIPTION_PROVIDER_ID,
            model="gpt-vision",
            capability="vision",
            client_request_id=CLIENT_REQUEST_ID,
        ),
        ManualCancellationToken(),
    )

    assert result.status == "unsupported"
    assert result.error is not None and result.error.code is ErrorCode.PROVIDER_IMAGE_UNSUPPORTED
    assert result.error.details == {"reason": "image_capability_unverified"}
    assert factory.calls == []


@pytest.mark.asyncio
async def test_vision_health_fails_closed_for_unverified_or_changed_catalog() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _select_model(config, "gpt-vision")
    auth_error = CodexCatalogError("auth_account_changed", False)
    catalog = _Catalog(_snapshot(freshness="unavailable", account_binding=None, error=auth_error))
    factory = _GatewayFactory(AssertionError("unverified catalog must fail before gateway creation"))
    service = _service(config, clock, factory, catalog)

    result = await service.health(
        ModelsHealthParams(
            provider=CODEX_SUBSCRIPTION_PROVIDER_ID,
            model="gpt-vision",
            capability="vision",
            client_request_id=CLIENT_REQUEST_ID,
        ),
        ManualCancellationToken(),
    )

    assert result.status == "auth_required"
    assert result.error is not None and result.error.code is ErrorCode.AUTH_REQUIRED
    assert result.error.details == {"reason": "auth_account_changed"}
    assert factory.calls == []


@pytest.mark.asyncio
async def test_text_health_uses_selected_codex_model_and_no_user_or_vault_content() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _select_model(config, "gpt-health")
    gateway = _Gateway()
    factory = _GatewayFactory(gateway)
    service = _service(config, clock, factory, _Catalog(_snapshot(_model("gpt-health"))))

    result = await service.health(
        ModelsHealthParams(
            provider=CODEX_SUBSCRIPTION_PROVIDER_ID,
            model="gpt-health",
            client_request_id=CLIENT_REQUEST_ID,
        ),
        ManualCancellationToken(),
    )

    assert result.status == "healthy"
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.purpose is ModelPurpose.GROUNDING
    assert request.max_output_tokens == 4
    assert request.messages[0].role is ModelRole.SYSTEM
    assert request.messages[0].content[0].data == {"text": "OfferAgent provider health probe. Reply exactly OK."}
    assert all(block.kind != "image" for message in request.messages for block in message.content)
    assert WORKSPACE_ID not in repr(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_error", "expected_status", "expected_code", "expected_retryable"),
    [
        ("auth_required", "auth_required", ErrorCode.AUTH_REQUIRED, False),
        ("provider_unreachable", "unreachable", ErrorCode.PROVIDER_UNREACHABLE, False),
        ("provider_rate_limited", "unreachable", ErrorCode.PROVIDER_RATE_LIMITED, False),
        ("model_unsupported", "unsupported", ErrorCode.PROVIDER_UNSUPPORTED, False),
        ("protocol_invalid_response", "degraded", ErrorCode.PROVIDER_PROTOCOL_ERROR, False),
        ("provider_protocol_error", "degraded", ErrorCode.PROVIDER_PROTOCOL_ERROR, False),
    ],
)
async def test_text_health_maps_terminal_provider_errors(
    provider_error: str,
    expected_status: str,
    expected_code: ErrorCode,
    expected_retryable: bool,
) -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _select_model(config, "gpt-health")
    gateway = _Gateway(terminal=ModelEventKind.ERROR, error_code=provider_error)
    service = _service(
        config,
        clock,
        _GatewayFactory(gateway),
        _Catalog(_snapshot(_model("gpt-health"))),
    )

    result = await service.health(
        ModelsHealthParams(
            provider=CODEX_SUBSCRIPTION_PROVIDER_ID,
            model="gpt-health",
            client_request_id=CLIENT_REQUEST_ID,
        ),
        ManualCancellationToken(),
    )

    assert result.status == expected_status
    assert result.error is not None and result.error.code is expected_code
    assert result.error.retryable is expected_retryable
    assert result.error.details == {"reason": provider_error}


@pytest.mark.asyncio
async def test_health_rejects_wrong_provider_and_account_binding_without_probe() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _select_model(config, "gpt-health", account_binding="sha256:" + "c" * 64)
    factory = _GatewayFactory(AssertionError("invalid selection must not create a gateway"))
    service = _service(config, clock, factory, _Catalog(_snapshot(_model("gpt-health"))))

    wrong_provider = await service.health(
        ModelsHealthParams(
            provider="openai",
            model="gpt-health",
            client_request_id=CLIENT_REQUEST_ID,
        ),
        ManualCancellationToken(),
    )
    changed_account = await service.health(
        ModelsHealthParams(
            provider=CODEX_SUBSCRIPTION_PROVIDER_ID,
            model="gpt-health",
            client_request_id="req_model_health_2",
        ),
        ManualCancellationToken(),
    )

    assert wrong_provider.status == "unsupported"
    assert changed_account.status == "auth_required"
    assert changed_account.error is not None and changed_account.error.code is ErrorCode.AUTH_REQUIRED
    assert factory.calls == []


@pytest.mark.asyncio
async def test_health_honors_explicit_deadline_without_calling_gateway() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _select_model(config, "gpt-health")
    factory = _GatewayFactory(_Gateway())
    service = _service(config, clock, factory, _Catalog(_snapshot(_model("gpt-health"))))

    result = await service.health(
        ModelsHealthParams(
            provider=CODEX_SUBSCRIPTION_PROVIDER_ID,
            model="gpt-health",
            client_request_id=CLIENT_REQUEST_ID,
            deadline=(NOW - timedelta(seconds=1)).isoformat(),
        ),
        ManualCancellationToken(),
    )

    assert result.status == "unreachable"
    assert result.error is not None and result.error.code is ErrorCode.REQUEST_DEADLINE_EXCEEDED
    assert factory.calls == []


@pytest.mark.asyncio
async def test_catalog_refresh_that_crosses_deadline_cannot_report_vision_healthy() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _select_model(config, "gpt-vision")

    class CrossingDeadlineCatalog(_Catalog):
        def refresh(self, *, timeout_seconds: float = 5.0) -> CodexModelCatalogSnapshot:
            clock.advance(timedelta(seconds=2))
            return super().refresh(timeout_seconds=timeout_seconds)

    catalog = CrossingDeadlineCatalog(_snapshot(_model("gpt-vision", modalities=("text", "image"))))
    factory = _GatewayFactory(AssertionError("expired catalog result must not create a gateway"))
    service = _service(config, clock, factory, catalog)

    result = await service.health(
        ModelsHealthParams(
            provider=CODEX_SUBSCRIPTION_PROVIDER_ID,
            model="gpt-vision",
            capability="vision",
            client_request_id=CLIENT_REQUEST_ID,
            deadline=(NOW + timedelta(seconds=1)).isoformat(),
        ),
        ManualCancellationToken(),
    )

    assert result.status == "unreachable"
    assert result.error is not None and result.error.code is ErrorCode.REQUEST_DEADLINE_EXCEEDED
    assert result.latency_ms == 2_000
    assert factory.calls == []


@pytest.mark.asyncio
async def test_catalog_health_cancellation_returns_before_blocked_refresh_finishes() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _select_model(config, "gpt-vision")
    started = threading.Event()
    release = threading.Event()

    class BlockingCatalog(_Catalog):
        def refresh(self, *, timeout_seconds: float = 5.0) -> CodexModelCatalogSnapshot:
            started.set()
            release.wait(timeout=max(1.0, timeout_seconds))
            return super().refresh(timeout_seconds=timeout_seconds)

    catalog = BlockingCatalog(_snapshot(_model("gpt-vision", modalities=("text", "image"))))
    factory = _GatewayFactory(AssertionError("cancelled catalog health must not create a gateway"))
    service = _service(config, clock, factory, catalog)
    token = ManualCancellationToken()
    task = asyncio.create_task(
        service.health(
            ModelsHealthParams(
                provider=CODEX_SUBSCRIPTION_PROVIDER_ID,
                model="gpt-vision",
                capability="vision",
                client_request_id=CLIENT_REQUEST_ID,
            ),
            token,
        )
    )
    assert await asyncio.to_thread(started.wait, 1)

    token.cancel()
    try:
        with pytest.raises(FakeRunCancelled):
            await asyncio.wait_for(task, timeout=1)
    finally:
        release.set()

    assert factory.calls == []


@pytest.mark.asyncio
async def test_health_cancellation_propagates_and_closes_blocked_provider_stream() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _select_model(config, "gpt-health")
    gateway = _Gateway()
    gateway.block = True
    token = ManualCancellationToken()
    service = _service(
        config,
        clock,
        _GatewayFactory(gateway),
        _Catalog(_snapshot(_model("gpt-health"))),
    )

    task = asyncio.create_task(
        service.health(
            ModelsHealthParams(
                provider=CODEX_SUBSCRIPTION_PROVIDER_ID,
                model="gpt-health",
                client_request_id=CLIENT_REQUEST_ID,
            ),
            token,
        )
    )
    await gateway.started.wait()
    token.cancel()

    with pytest.raises(FakeRunCancelled):
        await asyncio.wait_for(task, timeout=1)
