"""Strict, immutable configuration schema and layered snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

_SECRET_HANDLE = re.compile(r"secret:v1:[0-9a-f]{32}")
_PROVIDER_HEADER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class ConfigScope(str, Enum):
    MANAGED = "managed"
    USER = "user"
    WORKSPACE = "workspace"
    SESSION = "session"
    RUN = "run"


class ModelProvider(str, Enum):
    DEEPSEEK = "deepseek"
    CODEX = "codex"
    CODEX_SUBSCRIPTION_EXPERIMENTAL = "codex-subscription-experimental"
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai-compatible"
    LOCAL = "local"


class ModelWireApi(str, Enum):
    CHAT_COMPLETIONS = "chat-completions"
    RESPONSES = "responses"
    OLLAMA_CHAT = "ollama-chat"


class UpdateChannel(str, Enum):
    STABLE = "stable"
    BETA = "beta"
    DISABLED = "disabled"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


PositiveInt = Annotated[StrictInt, Field(ge=1)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
PositiveFloat = Annotated[StrictFloat, Field(gt=0)]


class RuntimeSettings(_StrictModel):
    worker_idle_seconds: PositiveInt = 300
    startup_timeout_seconds: PositiveInt = 30
    max_parallel_runs: PositiveInt = 2
    diagnostic_stdio: StrictBool = False


class ModelSettings(_StrictModel):
    provider: ModelProvider = ModelProvider.CODEX
    wire_api: ModelWireApi = ModelWireApi.RESPONSES
    model: StrictStr = ""
    reasoning_effort: StrictStr = "medium"
    service_tier: StrictStr = "default"
    temperature: Annotated[StrictFloat, Field(ge=0, le=2)] = 0.0
    credential_handle: StrictStr | None = None
    base_url: StrictStr = ""
    organization_id: StrictStr | None = None
    project_id: StrictStr | None = None
    allow_remote_https: StrictBool = False
    proxy_url: StrictStr | None = None

    @field_validator("credential_handle")
    @classmethod
    def _opaque_handle(cls, value: str | None) -> str | None:
        if value is not None and _SECRET_HANDLE.fullmatch(value) is None:
            raise ValueError("credential_handle must be an opaque SecretHandle")
        return value

    @field_validator("organization_id", "project_id")
    @classmethod
    def _provider_header_id(cls, value: str | None) -> str | None:
        if value is not None and _PROVIDER_HEADER_ID.fullmatch(value) is None:
            raise ValueError("provider organization/project IDs must be bounded non-secret identifiers")
        return value

    @model_validator(mode="after")
    def _endpoint_boundary(self) -> ModelSettings:
        _validate_model_endpoint(self.provider, self.wire_api, self.base_url, self.allow_remote_https)
        _validate_model_proxy(self.provider, self.proxy_url)
        if self.provider is ModelProvider.DEEPSEEK:
            if self.wire_api is not ModelWireApi.CHAT_COMPLETIONS:
                raise ValueError("DeepSeek requires the Chat Completions wire API")
            if self.base_url:
                raise ValueError("DeepSeek endpoint is fixed inside the local Worker")
            if self.organization_id is not None or self.project_id is not None or self.allow_remote_https:
                raise ValueError("DeepSeek does not accept API endpoint/header overrides")
        if self.provider is ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL:
            if self.wire_api is not ModelWireApi.RESPONSES:
                raise ValueError("Codex subscription requires the Responses wire API")
            if self.base_url or self.credential_handle is not None:
                raise ValueError("Codex subscription endpoint and login source cannot be overridden")
            if self.organization_id is not None or self.project_id is not None or self.allow_remote_https:
                raise ValueError("Codex subscription does not accept API endpoint/header overrides")
            if self.temperature != 0:
                raise ValueError("Codex subscription does not support temperature")
        return self


class NetworkSettings(_StrictModel):
    model_provider_enabled: StrictBool = True
    update_network_enabled: StrictBool = False


class PolicyApprovalSettings(_StrictModel):
    read_only: StrictBool = True
    workspace_trusted: StrictBool = False
    approve_vault_writes: StrictBool = True
    approve_shell: StrictBool = True
    approve_network: StrictBool = True
    approval_ttl_seconds: PositiveInt = 300


class MemorySettings(_StrictModel):
    """Whether the explicit Vault ``MEMORY.md`` entry point is loaded."""

    memory_enabled: StrictBool = False


class ExecutionSettings(_StrictModel):
    shell_enabled: StrictBool = False
    subagents_enabled: StrictBool = False
    max_subagents_per_vault: Annotated[StrictInt, Field(ge=0, le=32)] = 0
    max_subagent_depth: Annotated[StrictInt, Field(ge=0, le=3)] = 0


class ExtensibilitySettings(_StrictModel):
    skills_enabled: StrictBool = False
    hooks_enabled: StrictBool = False


class AgentBudgetSettings(_StrictModel):
    max_iterations: PositiveInt = 32
    max_tool_calls: PositiveInt = 64
    max_parallel_reads: Annotated[StrictInt, Field(ge=1, le=256)] = 4
    max_wall_seconds: PositiveInt = 900
    max_cost_microunits: NonNegativeInt = 0


class UiSettings(_StrictModel):
    loopback_web_enabled: StrictBool = False
    persistent_web_lease: StrictBool = False
    locale: StrictStr = "zh-CN"
    show_diagnostics: StrictBool = True


class TelemetrySettings(_StrictModel):
    enabled: StrictBool = False
    include_content: StrictBool = False


class UpdateSettings(_StrictModel):
    channel: UpdateChannel = UpdateChannel.DISABLED
    automatic_check: StrictBool = False
    automatic_install: StrictBool = False


class HarnessConfig(_StrictModel):
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    model: ModelSettings = Field(default_factory=ModelSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    policy: PolicyApprovalSettings = Field(default_factory=PolicyApprovalSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    extensibility: ExtensibilitySettings = Field(default_factory=ExtensibilitySettings)
    budgets: AgentBudgetSettings = Field(default_factory=AgentBudgetSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    update: UpdateSettings = Field(default_factory=UpdateSettings)

    @model_validator(mode="after")
    def _cross_field_safety(self) -> HarnessConfig:
        if self.ui.persistent_web_lease and not self.ui.loopback_web_enabled:
            raise ValueError("persistent Web lease requires loopback Web")
        if self.telemetry.include_content and not self.telemetry.enabled:
            raise ValueError("telemetry content requires telemetry enabled")
        if self.update.automatic_install and not self.update.automatic_check:
            raise ValueError("automatic install requires automatic update checks")
        if self.update.automatic_check and self.update.channel is UpdateChannel.DISABLED:
            raise ValueError("automatic update checks require a release channel")
        if self.execution.subagents_enabled and (
            self.execution.max_subagents_per_vault < 1 or self.execution.max_subagent_depth < 1
        ):
            raise ValueError("enabled subagents require positive count and depth limits")
        return self


class RuntimePatch(_StrictModel):
    worker_idle_seconds: PositiveInt | None = None
    startup_timeout_seconds: PositiveInt | None = None
    max_parallel_runs: PositiveInt | None = None
    diagnostic_stdio: StrictBool | None = None


class ModelPatch(_StrictModel):
    provider: ModelProvider | None = None
    wire_api: ModelWireApi | None = None
    model: StrictStr | None = None
    reasoning_effort: StrictStr | None = None
    service_tier: StrictStr | None = None
    temperature: Annotated[StrictFloat, Field(ge=0, le=2)] | None = None
    credential_handle: StrictStr | None = None
    base_url: StrictStr | None = None
    organization_id: StrictStr | None = None
    project_id: StrictStr | None = None
    allow_remote_https: StrictBool | None = None
    proxy_url: StrictStr | None = None

    @field_validator("credential_handle")
    @classmethod
    def _opaque_handle(cls, value: str | None) -> str | None:
        if value is not None and _SECRET_HANDLE.fullmatch(value) is None:
            raise ValueError("credential_handle must be an opaque SecretHandle")
        return value

    @field_validator("organization_id", "project_id")
    @classmethod
    def _provider_header_id(cls, value: str | None) -> str | None:
        if value is not None and _PROVIDER_HEADER_ID.fullmatch(value) is None:
            raise ValueError("provider organization/project IDs must be bounded non-secret identifiers")
        return value


class NetworkPatch(_StrictModel):
    model_provider_enabled: StrictBool | None = None
    update_network_enabled: StrictBool | None = None


class PolicyPatch(_StrictModel):
    read_only: StrictBool | None = None
    workspace_trusted: StrictBool | None = None
    approve_vault_writes: StrictBool | None = None
    approve_shell: StrictBool | None = None
    approve_network: StrictBool | None = None
    approval_ttl_seconds: PositiveInt | None = None


class MemoryPatch(_StrictModel):
    memory_enabled: StrictBool | None = None


class ExecutionPatch(_StrictModel):
    shell_enabled: StrictBool | None = None
    subagents_enabled: StrictBool | None = None
    max_subagents_per_vault: Annotated[StrictInt, Field(ge=0, le=32)] | None = None
    max_subagent_depth: Annotated[StrictInt, Field(ge=0, le=3)] | None = None


class ExtensibilityPatch(_StrictModel):
    skills_enabled: StrictBool | None = None
    hooks_enabled: StrictBool | None = None


class AgentBudgetPatch(_StrictModel):
    max_iterations: PositiveInt | None = None
    max_tool_calls: PositiveInt | None = None
    max_parallel_reads: Annotated[StrictInt, Field(ge=1, le=256)] | None = None
    max_wall_seconds: PositiveInt | None = None
    max_cost_microunits: NonNegativeInt | None = None


class UiPatch(_StrictModel):
    loopback_web_enabled: StrictBool | None = None
    persistent_web_lease: StrictBool | None = None
    locale: StrictStr | None = None
    show_diagnostics: StrictBool | None = None


class TelemetryPatch(_StrictModel):
    enabled: StrictBool | None = None
    include_content: StrictBool | None = None


class UpdatePatch(_StrictModel):
    channel: UpdateChannel | None = None
    automatic_check: StrictBool | None = None
    automatic_install: StrictBool | None = None


class ConfigPatch(_StrictModel):
    runtime: RuntimePatch | None = None
    model: ModelPatch | None = None
    network: NetworkPatch | None = None
    policy: PolicyPatch | None = None
    memory: MemoryPatch | None = None
    execution: ExecutionPatch | None = None
    extensibility: ExtensibilityPatch | None = None
    budgets: AgentBudgetPatch | None = None
    ui: UiPatch | None = None
    telemetry: TelemetryPatch | None = None
    update: UpdatePatch | None = None

    def payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_unset=True)


@dataclass(frozen=True, slots=True)
class ConfigLayer:
    scope: ConfigScope
    owner_id: str
    revision: int
    patch: ConfigPatch
    event_sequence: int = 0


@dataclass(frozen=True, slots=True)
class RunConfigSnapshot:
    config: HarnessConfig
    sources: MappingProxyType[str, str]
    layer_revisions: MappingProxyType[str, int]
    fingerprint: str
    captured_at: datetime

    def __post_init__(self) -> None:
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("RunConfigSnapshot timestamp must be timezone-aware")


RESTART_REQUIRED_PATHS = frozenset(
    {
        "runtime.diagnostic_stdio",
        "runtime.startup_timeout_seconds",
        "model.provider",
        "model.wire_api",
        "model.base_url",
        "model.proxy_url",
        "budgets.max_parallel_reads",
        "ui.loopback_web_enabled",
    }
)


def _validate_model_endpoint(
    provider: ModelProvider,
    wire_api: ModelWireApi,
    base_url: str,
    allow_remote_https: bool,
) -> None:
    if wire_api is ModelWireApi.CHAT_COMPLETIONS and provider is not ModelProvider.DEEPSEEK:
        raise ValueError("Chat Completions wire API is only valid for the DeepSeek provider")
    if provider is ModelProvider.DEEPSEEK and wire_api is not ModelWireApi.CHAT_COMPLETIONS:
        raise ValueError("DeepSeek requires the Chat Completions wire API")
    if wire_api is ModelWireApi.OLLAMA_CHAT and provider is not ModelProvider.LOCAL:
        raise ValueError("native Ollama wire API is only valid for the local model provider")
    if not base_url:
        if provider in {ModelProvider.LOCAL, ModelProvider.OPENAI_COMPATIBLE}:
            raise ValueError("local/compatible model provider requires an explicit base_url")
        return
    if base_url != base_url.strip() or len(base_url) > 2048:
        raise ValueError("model base_url has whitespace or exceeds its length limit")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("model base_url has an invalid host or port") from error
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        raise ValueError("model base_url must be an absolute HTTP(S) endpoint")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("model base_url must not contain credentials; use credential_handle")
    if parsed.query or parsed.fragment:
        raise ValueError("model base_url must not contain query parameters or a fragment")
    hostname = parsed.hostname.lower()
    loopback = hostname in {"127.0.0.1", "::1"}
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("model base_url port must be between 1 and 65535")
    if parsed.scheme == "http" and not loopback:
        raise ValueError("plaintext model endpoints are restricted to numeric loopback addresses")
    if provider is ModelProvider.LOCAL and loopback and port is None:
        raise ValueError("local loopback model base_url requires an explicit port")
    if parsed.scheme == "https" and not loopback and not allow_remote_https:
        raise ValueError("non-loopback HTTPS model endpoint requires explicit user authorization")
    if provider is ModelProvider.CODEX and base_url.rstrip("/") != "https://api.openai.com/v1":
        raise ValueError("Codex Responses endpoint is fixed to the official API")
    if provider is ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL:
        raise ValueError("Codex subscription endpoint is fixed inside the local Worker")
    if wire_api is ModelWireApi.OLLAMA_CHAT:
        if parsed.scheme != "http" or not loopback or port is None:
            raise ValueError("native Ollama wire API requires explicit numeric loopback HTTP")
        normalized_path = parsed.path.rstrip("/")
        if normalized_path != "/api":
            raise ValueError("native Ollama base_url path must be exactly /api")


def _validate_model_proxy(provider: ModelProvider, proxy_url: str | None) -> None:
    if not proxy_url:
        return
    if provider is not ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL:
        raise ValueError("explicit model proxy is reserved for Codex subscription")
    if proxy_url != proxy_url.strip() or len(proxy_url) > 2_048:
        raise ValueError("model proxy URL has whitespace or exceeds its length limit")
    try:
        parsed = urlsplit(proxy_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("model proxy URL has an invalid host or port") from error
    if (
        parsed.scheme != "http"
        or (parsed.hostname or "").casefold() not in {"127.0.0.1", "::1"}
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("model proxy must be explicit loopback HTTP with a port")


__all__ = [
    "RESTART_REQUIRED_PATHS",
    "AgentBudgetPatch",
    "AgentBudgetSettings",
    "ConfigLayer",
    "ConfigPatch",
    "ConfigScope",
    "ExecutionPatch",
    "ExecutionSettings",
    "ExtensibilityPatch",
    "ExtensibilitySettings",
    "HarnessConfig",
    "MemoryPatch",
    "MemorySettings",
    "ModelPatch",
    "ModelProvider",
    "ModelSettings",
    "ModelWireApi",
    "NetworkPatch",
    "NetworkSettings",
    "PolicyApprovalSettings",
    "PolicyPatch",
    "RunConfigSnapshot",
    "RuntimePatch",
    "RuntimeSettings",
    "TelemetryPatch",
    "TelemetrySettings",
    "UiPatch",
    "UiSettings",
    "UpdateChannel",
    "UpdatePatch",
    "UpdateSettings",
]
