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

_ACCOUNT_BINDING = re.compile(r"sha256:[0-9a-f]{64}")


class ConfigScope(str, Enum):
    MANAGED = "managed"
    USER = "user"
    WORKSPACE = "workspace"
    SESSION = "session"
    RUN = "run"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


PositiveInt = Annotated[StrictInt, Field(ge=1)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
PositiveFloat = Annotated[StrictFloat, Field(gt=0)]


class RuntimeSettings(_StrictModel):
    worker_idle_seconds: PositiveInt = 300
    startup_timeout_seconds: PositiveInt = 30
    max_parallel_runs: PositiveInt = 2


class ModelSettings(_StrictModel):
    model: Annotated[StrictStr, Field(max_length=256)] = ""
    account_binding: StrictStr | None = None
    reasoning_effort: StrictStr = "medium"
    proxy_url: StrictStr | None = None

    @field_validator("account_binding")
    @classmethod
    def _account_binding(cls, value: str | None) -> str | None:
        if value is not None and _ACCOUNT_BINDING.fullmatch(value) is None:
            raise ValueError("account_binding must be a SHA-256 account proof")
        return value

    @field_validator("proxy_url")
    @classmethod
    def _proxy(cls, value: str | None) -> str | None:
        _validate_model_proxy(value)
        return value

    @model_validator(mode="after")
    def _atomic_selection(self) -> ModelSettings:
        if bool(self.model) != (self.account_binding is not None):
            raise ValueError("model and account binding require an atomic model selection")
        return self


class NetworkSettings(_StrictModel):
    model_provider_enabled: StrictBool = True


class PolicyApprovalSettings(_StrictModel):
    read_only: StrictBool = True
    workspace_trusted: StrictBool = False
    approve_vault_writes: StrictBool = True
    allow_bypass: StrictBool = False
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


class ExtensibilitySettings(_StrictModel):
    hooks_enabled: StrictBool = False


class AgentBudgetSettings(_StrictModel):
    max_iterations: PositiveInt = 32
    max_tool_calls: PositiveInt = 64
    max_parallel_reads: Annotated[StrictInt, Field(ge=1, le=256)] = 4
    max_wall_seconds: PositiveInt = 900
    max_cost_microunits: NonNegativeInt = 0


class UiSettings(_StrictModel):
    locale: StrictStr = "zh-CN"
    show_diagnostics: StrictBool = True


class TelemetrySettings(_StrictModel):
    enabled: StrictBool = False
    include_content: StrictBool = False


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

    @model_validator(mode="after")
    def _cross_field_safety(self) -> HarnessConfig:
        if self.telemetry.include_content and not self.telemetry.enabled:
            raise ValueError("telemetry content requires telemetry enabled")
        if self.execution.subagents_enabled and self.execution.max_subagents_per_vault < 1:
            raise ValueError("enabled subagents require a positive concurrent-run limit")
        return self


class RuntimePatch(_StrictModel):
    worker_idle_seconds: PositiveInt | None = None
    startup_timeout_seconds: PositiveInt | None = None
    max_parallel_runs: PositiveInt | None = None


class ModelPatch(_StrictModel):
    model: Annotated[StrictStr, Field(max_length=256)] | None = None
    account_binding: StrictStr | None = None
    reasoning_effort: StrictStr | None = None
    proxy_url: StrictStr | None = None

    @field_validator("account_binding")
    @classmethod
    def _account_binding(cls, value: str | None) -> str | None:
        if value is not None and _ACCOUNT_BINDING.fullmatch(value) is None:
            raise ValueError("account_binding must be a SHA-256 account proof")
        return value

    @field_validator("proxy_url")
    @classmethod
    def _proxy(cls, value: str | None) -> str | None:
        _validate_model_proxy(value)
        return value


class NetworkPatch(_StrictModel):
    model_provider_enabled: StrictBool | None = None


class PolicyPatch(_StrictModel):
    read_only: StrictBool | None = None
    workspace_trusted: StrictBool | None = None
    approve_vault_writes: StrictBool | None = None
    allow_bypass: StrictBool | None = None
    approve_shell: StrictBool | None = None
    approve_network: StrictBool | None = None
    approval_ttl_seconds: PositiveInt | None = None


class MemoryPatch(_StrictModel):
    memory_enabled: StrictBool | None = None


class ExecutionPatch(_StrictModel):
    shell_enabled: StrictBool | None = None
    subagents_enabled: StrictBool | None = None
    max_subagents_per_vault: Annotated[StrictInt, Field(ge=0, le=32)] | None = None


class ExtensibilityPatch(_StrictModel):
    hooks_enabled: StrictBool | None = None


class AgentBudgetPatch(_StrictModel):
    max_iterations: PositiveInt | None = None
    max_tool_calls: PositiveInt | None = None
    max_parallel_reads: Annotated[StrictInt, Field(ge=1, le=256)] | None = None
    max_wall_seconds: PositiveInt | None = None
    max_cost_microunits: NonNegativeInt | None = None


class UiPatch(_StrictModel):
    locale: StrictStr | None = None
    show_diagnostics: StrictBool | None = None


class TelemetryPatch(_StrictModel):
    enabled: StrictBool | None = None
    include_content: StrictBool | None = None


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
        "runtime.startup_timeout_seconds",
        "model.proxy_url",
        "budgets.max_parallel_reads",
    }
)


def _validate_model_proxy(proxy_url: str | None) -> None:
    if not proxy_url:
        return
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
    "ModelSettings",
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
]
