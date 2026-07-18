from __future__ import annotations

import asyncio
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
from offeragent_harness.ports.secrets import SecretHandle, SecretKind, SecretMetadata
from offeragent_harness.protocol.messages import ModelsHealthParams, ModelsListParams
from offeragent_harness.providers.codex_subscription import (
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
HANDLE = SecretHandle("secret:v1:12345678123442348234123456789abc")
CLIENT_REQUEST_ID = "req_model_health_1"


class _SecretStore:
    def __init__(self, metadata: SecretMetadata | BaseException | None) -> None:
        self.value = metadata
        self.calls: list[tuple[SecretHandle, str]] = []

    def metadata(self, handle: SecretHandle, *, scope_id: str) -> SecretMetadata:
        self.calls.append((handle, scope_id))
        if isinstance(self.value, BaseException):
            raise self.value
        if self.value is None:
            raise KeyError(handle)
        return self.value


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


def _metadata(
    *,
    scope_id: str = WORKSPACE_ID,
    provider_id: str = "openai",
) -> SecretMetadata:
    return SecretMetadata(HANDLE, scope_id, SecretKind.MODEL_PROVIDER, provider_id, 1, NOW, NOW)


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


def _service(
    config: ConfigService,
    clock: ManualClock,
    secrets: _SecretStore,
    factory: _GatewayFactory,
    *,
    catalog: Any | None = None,
) -> ProductionModelCommandService:
    return ProductionModelCommandService(
        config=config,
        managed_owner_id=MANAGED_ID,
        profile_id=PROFILE_ID,
        workspace_id=WORKSPACE_ID,
        secrets=secrets,  # type: ignore[arg-type]
        gateway_factory=factory,
        clock=clock,
        ids=DeterministicIdGenerator(),
        catalog=catalog,
    )


class _Catalog:
    def __init__(self, snapshot: CodexModelCatalogSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def refresh(self) -> CodexModelCatalogSnapshot:
        self.calls += 1
        return self.snapshot


@pytest.mark.asyncio
async def test_model_list_projects_the_live_codex_catalog_through_the_public_protocol() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    catalog = _Catalog(
        CodexModelCatalogSnapshot(
            models=(
                CodexCatalogModel(
                    model_id="gpt-catalog",
                    display_name="GPT Catalog",
                    description="Catalog-backed model",
                    input_modalities=("text", "image"),
                    supports_image_detail_original=True,
                    supports_hosted_search=True,
                    web_search_tool_type="text_and_image",
                    context_window=272_000,
                    max_context_window=1_000_000,
                    effective_context_window_percent=95,
                    additional_speed_tiers=("fast",),
                    service_tiers=(CodexModelServiceTier("priority", "Fast", "1.5x speed"),),
                    default_service_tier=None,
                ),
            ),
            freshness="fresh",
            catalog_revision="sha256:" + "a" * 64,
            fetched_at=NOW,
            account_binding="sha256:" + "b" * 64,
            error=None,
        )
    )
    factory = _GatewayFactory(_Gateway())
    service = _service(config, clock, _SecretStore(None), factory, catalog=catalog)

    result = await service.list_models(ModelsListParams(), ManualCancellationToken())

    assert catalog.calls == 1
    assert result.catalog_freshness == "fresh"
    assert result.catalog_revision == "sha256:" + "a" * 64
    assert result.fetched_at == NOW.isoformat()
    assert result.account_binding == "sha256:" + "b" * 64
    assert result.error is None
    assert len(result.models) == 1
    descriptor = result.models[0]
    assert descriptor.provider == "codex-subscription-experimental"
    assert descriptor.model == "gpt-catalog"
    assert descriptor.input_modalities == ["text", "image"]
    assert descriptor.supports_image_detail_original is True
    assert descriptor.supports_hosted_search is True
    assert descriptor.supports_fast_mode is True
    assert descriptor.service_tiers[0].id == "priority"
    assert descriptor.available is True
    assert factory.calls == []


@pytest.mark.asyncio
async def test_model_list_reads_current_config_snapshot_instead_of_frozen_startup_settings() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(
        config,
        {
            "model": {
                "provider": "openai",
                "model": "gpt-initial",
                "credential_handle": HANDLE.opaque_id,
            }
        },
    )
    service = _service(config, clock, _SecretStore(_metadata()), _GatewayFactory(_Gateway()))

    initial = await service.list_models(ModelsListParams(), ManualCancellationToken())
    await _update(config, {"model": {"model": "gpt-current"}}, revision=1)
    current = await service.list_models(ModelsListParams(), ManualCancellationToken())

    assert initial.config_revision == 1
    assert [item.model for item in initial.models] == ["gpt-initial"]
    assert current.config_revision == 2
    assert [item.model for item in current.models] == ["gpt-current"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret",
    [
        None,
        RuntimeError("protected secret is missing"),
        _metadata(scope_id="wsi_22345678-1234-4234-8234-123456789abc"),
        _metadata(provider_id="codex"),
    ],
)
async def test_model_health_fails_closed_for_missing_or_wrong_secret_metadata(
    secret: SecretMetadata | BaseException | None,
) -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(
        config,
        {
            "model": {
                "provider": "openai",
                "model": "gpt-health",
                "credential_handle": HANDLE.opaque_id,
            }
        },
    )
    factory = _GatewayFactory(_Gateway())
    service = _service(config, clock, _SecretStore(secret), factory)

    result = await service.health(
        ModelsHealthParams(provider="openai", client_request_id=CLIENT_REQUEST_ID),
        ManualCancellationToken(),
    )

    assert result.status == "auth_required"
    assert result.error is not None and result.error.code is ErrorCode.AUTH_REQUIRED
    assert factory.calls == []


@pytest.mark.asyncio
async def test_deepseek_health_requires_endpoint_bound_secret_before_probe() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(
        config,
        {
            "model": {
                "provider": "deepseek",
                "wire_api": "chat-completions",
                "model": "deepseek-v4-flash",
            }
        },
    )
    missing_factory = _GatewayFactory(_Gateway())
    missing = _service(config, clock, _SecretStore(None), missing_factory)

    missing_result = await missing.health(
        ModelsHealthParams(provider="deepseek", client_request_id=CLIENT_REQUEST_ID),
        ManualCancellationToken(),
    )

    assert missing_result.status == "auth_required"
    assert missing_result.error is not None and missing_result.error.code is ErrorCode.AUTH_REQUIRED
    assert missing_factory.calls == []

    await _update(
        config,
        {"model": {"credential_handle": HANDLE.opaque_id}},
        revision=1,
    )
    gateway = _Gateway()
    configured_factory = _GatewayFactory(gateway)
    configured = _service(
        config,
        clock,
        _SecretStore(_metadata(provider_id="deepseek")),
        configured_factory,
    )

    configured_result = await configured.health(
        ModelsHealthParams(provider="deepseek", client_request_id=CLIENT_REQUEST_ID),
        ManualCancellationToken(),
    )

    assert configured_result.status == "healthy"
    assert len(configured_factory.calls) == 1


@pytest.mark.asyncio
async def test_local_model_without_credential_is_available_and_can_be_probed() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(
        config,
        {
            "model": {
                "provider": "local",
                "wire_api": "ollama-chat",
                "model": "qwen-local",
                "base_url": "http://127.0.0.1:11434/api",
            }
        },
    )
    gateway = _Gateway()
    service = _service(config, clock, _SecretStore(None), _GatewayFactory(gateway))

    listed = await service.list_models(ModelsListParams(), ManualCancellationToken())
    health = await service.health(
        ModelsHealthParams(provider="local", client_request_id=CLIENT_REQUEST_ID),
        ManualCancellationToken(),
    )

    assert len(listed.models) == 1 and listed.models[0].local and listed.models[0].available
    assert health.status == "healthy"


@pytest.mark.asyncio
async def test_health_probe_has_fixed_runtime_content_and_no_vault_session_or_tool_context() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(
        config,
        {
            "model": {
                "provider": "openai",
                "model": "gpt-health",
                "credential_handle": HANDLE.opaque_id,
            }
        },
    )
    gateway = _Gateway()
    service = _service(config, clock, _SecretStore(_metadata()), _GatewayFactory(gateway))

    result = await service.health(
        ModelsHealthParams(provider="openai", client_request_id=CLIENT_REQUEST_ID),
        ManualCancellationToken(),
    )

    assert result.status == "healthy"
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.purpose is ModelPurpose.GROUNDING
    assert request.max_output_tokens == 4
    assert request.messages[0].role is ModelRole.SYSTEM
    assert request.messages[0].content[0].data == {"text": "OfferAgent provider health probe. Reply exactly OK."}
    assert dict(request.metadata) == {
        "operation": "model_health",
        "clientRequestId": CLIENT_REQUEST_ID,
        "contentSource": "fixed_runtime_probe",
        "capability": "text",
    }
    serialized = repr(request)
    assert WORKSPACE_ID not in serialized
    assert all(marker not in serialized.lower() for marker in ("vault", "session", "tool_result"))


@pytest.mark.asyncio
async def test_vision_probe_uses_a_fixed_image_and_updates_model_capability() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(
        config,
        {
            "model": {
                "provider": "openai",
                "model": "gpt-vision",
                "credential_handle": HANDLE.opaque_id,
            }
        },
    )
    gateway = _Gateway()
    service = _service(config, clock, _SecretStore(_metadata()), _GatewayFactory(gateway))

    before = await service.list_models(ModelsListParams(), ManualCancellationToken())
    result = await service.health(
        ModelsHealthParams(
            provider="openai",
            client_request_id=CLIENT_REQUEST_ID,
            capability="vision",
        ),
        ManualCancellationToken(),
    )
    after = await service.list_models(ModelsListParams(), ManualCancellationToken())

    assert before.models[0].input_modalities == ["text"]
    assert result.status == "healthy" and result.capability == "vision"
    image = gateway.requests[0].messages[0].content[1]
    assert image.kind == "image" and image.binary_data is not None
    assert "binary_data" not in repr(gateway.requests[0])
    assert after.models[0].input_modalities == ["text", "image"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_error", "expected_status", "expected_code"),
    [
        ("auth_required", "auth_required", ErrorCode.AUTH_REQUIRED),
        ("provider_unreachable", "unreachable", ErrorCode.PROVIDER_UNREACHABLE),
        ("model_unsupported", "unsupported", ErrorCode.INTERNAL_ERROR),
        ("protocol_invalid_response", "degraded", ErrorCode.INTERNAL_ERROR),
    ],
)
async def test_health_maps_terminal_provider_errors(
    provider_error: str,
    expected_status: str,
    expected_code: ErrorCode,
) -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(
        config,
        {
            "model": {
                "provider": "openai",
                "model": "gpt-health",
                "credential_handle": HANDLE.opaque_id,
            }
        },
    )
    gateway = _Gateway(terminal=ModelEventKind.ERROR, error_code=provider_error)
    service = _service(config, clock, _SecretStore(_metadata()), _GatewayFactory(gateway))

    result = await service.health(
        ModelsHealthParams(provider="openai", client_request_id=CLIENT_REQUEST_ID),
        ManualCancellationToken(),
    )

    assert result.status == expected_status
    assert result.error is not None and result.error.code is expected_code
    assert result.error.details == {"reason": provider_error}


@pytest.mark.asyncio
async def test_rejected_vision_probe_is_actionable_and_cached_as_unsupported() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(
        config,
        {
            "model": {
                "provider": "openai",
                "model": "text-only",
                "credential_handle": HANDLE.opaque_id,
            }
        },
    )
    gateway = _Gateway(terminal=ModelEventKind.ERROR, error_code="provider_configuration")
    service = _service(config, clock, _SecretStore(_metadata()), _GatewayFactory(gateway))

    result = await service.health(
        ModelsHealthParams(
            provider="openai",
            client_request_id=CLIENT_REQUEST_ID,
            capability="vision",
        ),
        ManualCancellationToken(),
    )
    listed = await service.list_models(ModelsListParams(), ManualCancellationToken())

    assert result.status == "unsupported"
    assert result.error is not None and result.error.details == {"reason": "vision_unsupported"}
    assert listed.models[0].input_modalities == ["text"]


@pytest.mark.asyncio
async def test_health_rejects_unsupported_selection_and_invalid_gateway_without_probe() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(
        config,
        {
            "model": {
                "provider": "openai",
                "model": "gpt-health",
                "credential_handle": HANDLE.opaque_id,
            }
        },
    )
    factory = _GatewayFactory(ValueError("unsupported provider configuration"))
    service = _service(config, clock, _SecretStore(_metadata()), factory)

    wrong = await service.health(
        ModelsHealthParams(provider="codex", client_request_id=CLIENT_REQUEST_ID),
        ManualCancellationToken(),
    )
    invalid = await service.health(
        ModelsHealthParams(provider="openai", client_request_id=CLIENT_REQUEST_ID),
        ManualCancellationToken(),
    )

    assert wrong.status == "unsupported"
    assert invalid.status == "unsupported"
    assert len(factory.calls) == 1


@pytest.mark.asyncio
async def test_health_honors_explicit_deadline_without_calling_gateway() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(
        config,
        {
            "model": {
                "provider": "openai",
                "model": "gpt-health",
                "credential_handle": HANDLE.opaque_id,
            }
        },
    )
    factory = _GatewayFactory(_Gateway())
    service = _service(config, clock, _SecretStore(_metadata()), factory)

    result = await service.health(
        ModelsHealthParams(
            provider="openai",
            client_request_id=CLIENT_REQUEST_ID,
            deadline=(NOW - timedelta(seconds=1)).isoformat(),
        ),
        ManualCancellationToken(),
    )

    assert result.status == "unreachable"
    assert result.error is not None and result.error.code is ErrorCode.REQUEST_DEADLINE_EXCEEDED
    assert factory.calls == []


@pytest.mark.asyncio
async def test_health_cancellation_propagates_and_closes_blocked_provider_stream() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(
        config,
        {
            "model": {
                "provider": "openai",
                "model": "gpt-health",
                "credential_handle": HANDLE.opaque_id,
            }
        },
    )
    gateway = _Gateway()
    gateway.block = True
    token = ManualCancellationToken()
    service = _service(config, clock, _SecretStore(_metadata()), _GatewayFactory(gateway))

    task = asyncio.create_task(
        service.health(
            ModelsHealthParams(provider="openai", client_request_id=CLIENT_REQUEST_ID),
            token,
        )
    )
    await gateway.started.wait()
    token.cancel()

    with pytest.raises(FakeRunCancelled):
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_network_switch_makes_configured_model_unavailable_without_probe() -> None:
    clock = ManualClock(NOW)
    config = _config(clock)
    await _update(
        config,
        {
            "model": {
                "provider": "openai",
                "model": "gpt-health",
                "credential_handle": HANDLE.opaque_id,
            },
            "network": {"model_provider_enabled": False},
        },
    )
    factory = _GatewayFactory(_Gateway())
    service = _service(config, clock, _SecretStore(_metadata()), factory)

    hidden = await service.list_models(ModelsListParams(), ManualCancellationToken())
    visible = await service.list_models(
        ModelsListParams(include_unavailable=True),
        ManualCancellationToken(),
    )
    health = await service.health(
        ModelsHealthParams(provider="openai", client_request_id=CLIENT_REQUEST_ID),
        ManualCancellationToken(),
    )

    assert hidden.models == []
    assert len(visible.models) == 1 and not visible.models[0].available
    assert health.status == "unreachable"
    assert factory.calls == []
