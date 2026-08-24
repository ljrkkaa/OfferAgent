"""Config-snapshot model discovery and fixed-content provider health probes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Literal

from offeragent_harness.config import ConfigScope, ModelProvider, ModelSettings, RunConfigSnapshot
from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.models import (
    ModelContentBlock,
    ModelEvent,
    ModelEventKind,
    ModelMessage,
    ModelOutputMode,
    ModelPurpose,
    ModelRequest,
    ModelRole,
    TraceContext,
)
from offeragent_harness.ports import CancellationToken, Clock, IdGenerator, ModelGateway
from offeragent_harness.ports.secrets import SecretHandle, SecretKind, SecretStore
from offeragent_harness.protocol.errors import ErrorEnvelope
from offeragent_harness.protocol.messages import (
    ModelDescriptor,
    ModelsHealthParams,
    ModelsHealthResult,
    ModelsListParams,
    ModelsListResult,
)
from offeragent_harness.providers import model_secret_provider_id

from .config_service import ConfigService

ModelGatewayFactory = Callable[[ModelSettings, bool], ModelGateway]
ModelHealthStatus = Literal["healthy", "degraded", "unreachable", "auth_required", "unsupported"]
_HEALTH_TIMEOUT = timedelta(seconds=30)
_MAX_HEALTH_TIMEOUT = timedelta(seconds=60)


class ProductionModelCommandService:
    """Read current Config and probe only through the sole ModelGateway port."""

    def __init__(
        self,
        *,
        config: ConfigService,
        managed_owner_id: str,
        profile_id: str,
        workspace_id: str,
        secrets: SecretStore,
        gateway_factory: ModelGatewayFactory,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        if not all((managed_owner_id, profile_id, workspace_id)):
            raise ValueError("model management identities must be non-empty")
        self._config = config
        self._managed_owner_id = managed_owner_id
        self._profile_id = profile_id
        self._workspace_id = workspace_id
        self._secrets = secrets
        self._gateway_factory = gateway_factory
        self._clock = clock
        self._ids = ids

    async def list_models(
        self,
        params: ModelsListParams,
        cancellation: CancellationToken,
    ) -> ModelsListResult:
        cancellation.checkpoint()
        snapshot = await self._snapshot()
        layer = await self._config.layer(ConfigScope.WORKSPACE, self._workspace_id)
        settings = snapshot.config.model
        provider = settings.provider.value
        if params.provider not in {None, provider} or not settings.model:
            return ModelsListResult(models=[], config_revision=layer.revision)
        available, _ = await self._availability(settings, snapshot.config.network.model_provider_enabled)
        cancellation.checkpoint()
        descriptor = ModelDescriptor(
            provider=provider,
            model=settings.model,
            display_name=settings.model,
            local=settings.provider is ModelProvider.LOCAL,
            supports_streaming=True,
            supports_structured_output=True,
            max_context_tokens=None,
            available=available,
        )
        return ModelsListResult(
            models=[descriptor] if available or params.include_unavailable else [],
            config_revision=layer.revision,
        )

    async def health(
        self,
        params: ModelsHealthParams,
        cancellation: CancellationToken,
    ) -> ModelsHealthResult:
        cancellation.checkpoint()
        snapshot = await self._snapshot()
        settings = snapshot.config.model
        model = params.model or settings.model or None
        if params.provider != settings.provider.value or not settings.model or model != settings.model:
            return self._result(params.provider, model, "unsupported", None, "model_selection_unavailable")
        available, availability_code = await self._availability(
            settings,
            snapshot.config.network.model_provider_enabled,
        )
        if not available:
            status: ModelHealthStatus = (
                "auth_required" if availability_code == "credential_unavailable" else "unreachable"
            )
            code = ErrorCode.AUTH_REQUIRED if status == "auth_required" else ErrorCode.PROVIDER_UNREACHABLE
            return self._result(params.provider, model, status, None, availability_code, code=code)

        now = self._clock.utcnow()
        deadline = _deadline(params.deadline, now)
        if deadline <= now:
            return self._result(
                params.provider,
                model,
                "unreachable",
                0,
                "health_deadline_expired",
                code=ErrorCode.REQUEST_DEADLINE_EXCEEDED,
            )
        try:
            gateway = self._gateway_factory(settings, snapshot.config.network.model_provider_enabled)
        except Exception:
            return self._result(params.provider, model, "unsupported", None, "provider_configuration_invalid")

        request = ModelRequest(
            request_id=self._ids.new_id("model-health"),
            model=settings.model,
            purpose=ModelPurpose.GROUNDING,
            messages=(
                ModelMessage(
                    ModelRole.SYSTEM,
                    (ModelContentBlock.text("OfferAgent provider health probe. Reply exactly OK."),),
                    name="offeragent-health-probe",
                ),
            ),
            output_mode=ModelOutputMode.TEXT,
            output_schema=None,
            max_output_tokens=4,
            reasoning_effort=None,
            temperature=None,
            seed=None,
            trace_context=TraceContext(self._ids.new_id("trace")),
            metadata={
                "operation": "model_health",
                "clientRequestId": params.client_request_id,
                "contentSource": "fixed_runtime_probe",
            },
        )
        started = self._clock.monotonic()
        terminal: ModelEvent | None = None
        timed_out = False
        try:
            terminal, timed_out = await _consume_probe(
                gateway.stream(request, cancellation),
                cancellation,
                deadline=deadline,
                clock=self._clock,
            )
        except Exception:
            cancellation.checkpoint()
            return self._result(
                params.provider,
                model,
                "degraded",
                _latency_ms(started, self._clock.monotonic()),
                "provider_probe_failed",
            )
        latency = _latency_ms(started, self._clock.monotonic())
        if timed_out:
            return self._result(
                params.provider,
                model,
                "unreachable",
                latency,
                "health_deadline_exceeded",
                code=ErrorCode.REQUEST_DEADLINE_EXCEEDED,
            )
        if terminal is None:
            return self._result(params.provider, model, "degraded", latency, "provider_stream_missing_terminal")
        if terminal.kind is ModelEventKind.COMPLETED:
            return self._result(params.provider, model, "healthy", latency, None)
        if terminal.kind is ModelEventKind.CANCELLED:
            cancellation.checkpoint()
            return self._result(params.provider, model, "degraded", latency, "provider_cancelled_probe")
        assert terminal.kind is ModelEventKind.ERROR and terminal.error is not None
        status, code = _error_status(terminal.error.code)
        return self._result(params.provider, model, status, latency, terminal.error.code, code=code)

    async def _snapshot(self) -> RunConfigSnapshot:
        return await self._config.snapshot(
            managed_owner_id=self._managed_owner_id,
            profile_id=self._profile_id,
            workspace_id=self._workspace_id,
        )

    async def _availability(self, settings: ModelSettings, network_enabled: bool) -> tuple[bool, str | None]:
        if not network_enabled:
            return False, "model_provider_network_disabled"
        requires_credential = settings.provider in {
            ModelProvider.DEEPSEEK,
            ModelProvider.CODEX,
            ModelProvider.OPENAI,
        }
        if settings.credential_handle is None:
            return (False, "credential_unavailable") if requires_credential else (True, None)
        try:
            handle = SecretHandle(settings.credential_handle)
            metadata = await asyncio.to_thread(self._secrets.metadata, handle, scope_id=self._workspace_id)
            expected_provider_id = model_secret_provider_id(settings.provider.value, settings.base_url or None)
        except Exception:
            return False, "credential_unavailable"
        if (
            metadata.handle != handle
            or metadata.scope_id != self._workspace_id
            or metadata.kind is not SecretKind.MODEL_PROVIDER
            or metadata.provider_id != expected_provider_id
        ):
            return False, "credential_unavailable"
        return True, None

    def _result(
        self,
        provider: str,
        model: str | None,
        status: ModelHealthStatus,
        latency_ms: int | None,
        reason: str | None,
        *,
        code: ErrorCode | None = None,
    ) -> ModelsHealthResult:
        error = None
        if reason is not None:
            error = ErrorEnvelope(
                code=code or ErrorCode.INTERNAL_ERROR,
                retryable=status in {"degraded", "unreachable"},
                cancelled=False,
                user_visible_message=_health_message(status),
                details={"reason": reason},
            )
        return ModelsHealthResult(
            provider=provider,
            model=model,
            status=status,
            checked_at=self._clock.utcnow().isoformat(),
            latency_ms=latency_ms,
            error=error,
        )


async def _consume_probe(
    stream: AsyncIterator[ModelEvent],
    cancellation: CancellationToken,
    *,
    deadline: datetime,
    clock: Clock,
) -> tuple[ModelEvent | None, bool]:
    iterator = stream.__aiter__()
    try:
        while True:
            cancellation.checkpoint()
            remaining = (deadline - clock.utcnow()).total_seconds()
            if remaining <= 0:
                return None, True
            next_event: asyncio.Future[ModelEvent] = asyncio.ensure_future(anext(iterator))
            cancel_wait = asyncio.create_task(cancellation.wait())
            try:
                done, _ = await asyncio.wait(
                    (next_event, cancel_wait),
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_wait in done:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    cancellation.checkpoint()
                if next_event not in done:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    return None, True
                try:
                    event = await next_event
                except StopAsyncIteration:
                    return None, False
                if event.kind in {ModelEventKind.COMPLETED, ModelEventKind.ERROR, ModelEventKind.CANCELLED}:
                    return event, False
            finally:
                if not cancel_wait.done():
                    cancel_wait.cancel()
                await asyncio.gather(cancel_wait, return_exceptions=True)
    finally:
        close = getattr(iterator, "aclose", None)
        if callable(close):
            with suppress(Exception):
                await close()


def _deadline(value: str | None, now: datetime) -> datetime:
    if value is None:
        return now + _HEALTH_TIMEOUT
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("health deadline must include a UTC offset")
    return min(parsed, now + _MAX_HEALTH_TIMEOUT)


def _latency_ms(started: float, finished: float) -> int:
    return max(0, round((finished - started) * 1_000))


def _error_status(code: str) -> tuple[ModelHealthStatus, ErrorCode]:
    if code in {"auth_account_changed", "auth_required"}:
        return "auth_required", ErrorCode.AUTH_REQUIRED
    if code in {
        "insufficient_balance",
        "provider_unreachable",
        "provider_unavailable",
        "provider_rate_limited",
    }:
        return "unreachable", ErrorCode.PROVIDER_UNREACHABLE
    if code in {"provider_configuration", "model_unsupported"}:
        return "unsupported", ErrorCode.INTERNAL_ERROR
    return "degraded", ErrorCode.INTERNAL_ERROR


def _health_message(status: ModelHealthStatus) -> str:
    return {
        "healthy": "模型 Provider 健康。",
        "degraded": "模型 Provider 响应异常, 请查看本地诊断。",
        "unreachable": "无法连接模型 Provider, 请检查网络与端点。",
        "auth_required": "模型 Provider 需要有效的 API 凭据或本机 Codex 登录。",
        "unsupported": "当前模型或 Provider 配置不受支持。",
    }[status]


__all__ = ["ModelGatewayFactory", "ProductionModelCommandService"]
