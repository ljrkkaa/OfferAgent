"""Fail-closed capability intersection and tool visibility projection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from offeragent_harness.config import RunConfigSnapshot
from offeragent_harness.ports.capabilities import (
    CapabilityAuditRecord,
    CapabilityAuditSink,
    NullCapabilityAuditSink,
)
from offeragent_harness.ports.system import Clock, IdGenerator


class RuntimeCapability(str, Enum):
    MODEL = "model"
    VAULT_WRITE = "vault.write"
    WORKSPACE_READ = "workspace.read"
    MEMORY = "memory"
    SHELL = "shell"
    SUBAGENT = "subagent"
    SKILLS = "skills"
    HOOKS = "hooks"
    LOOPBACK_WEB = "loopback.web"
    TELEMETRY = "telemetry"
    UPDATE = "update"


class CapabilityDisabledReason(str, Enum):
    HOST_UNAVAILABLE = "host_unavailable"
    DISABLED_BY_CONFIG = "disabled_by_config"
    WORKSPACE_UNTRUSTED = "workspace_untrusted"
    DENIED_BY_POLICY = "denied_by_policy"


class WorkspaceTrustLevel(str, Enum):
    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"
    PRIVILEGED = "privileged"


@dataclass(frozen=True, slots=True)
class HostCapabilitySnapshot:
    available: frozenset[RuntimeCapability]
    revision: int

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("host capability revision cannot be negative")


@dataclass(frozen=True, slots=True)
class WorkspaceTrustSnapshot:
    workspace_id: str
    level: WorkspaceTrustLevel
    allowed: frozenset[RuntimeCapability]
    revision: int

    def __post_init__(self) -> None:
        if not self.workspace_id or self.revision < 0:
            raise ValueError("workspace trust identity/revision is invalid")


@dataclass(frozen=True, slots=True)
class PolicyCapabilitySnapshot:
    allowed: frozenset[RuntimeCapability]
    denied: frozenset[RuntimeCapability]
    revision: int
    grant_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.allowed & self.denied:
            raise ValueError("policy capability cannot be both allowed and denied")
        if self.revision < 0 or len(set(self.grant_ids)) != len(self.grant_ids):
            raise ValueError("policy capability revision/grant IDs are invalid")


@dataclass(frozen=True, slots=True)
class ToolCapabilityRequirement:
    tool_name: str
    required: frozenset[RuntimeCapability]

    def __post_init__(self) -> None:
        if not self.tool_name or len(self.tool_name) > 160 or not self.required:
            raise ValueError("tool capability requirement is invalid")


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    capability: RuntimeCapability
    enabled: bool
    reason: CapabilityDisabledReason | None


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    enabled: frozenset[RuntimeCapability]
    decisions: tuple[CapabilityDecision, ...]
    visible_tools: tuple[str, ...]
    disabled_tools: MappingProxyType[str, tuple[str, ...]]
    fingerprint: str


class CapabilityResolver:
    def __init__(
        self,
        *,
        clock: Clock,
        ids: IdGenerator,
        audit_sink: CapabilityAuditSink | None = None,
    ) -> None:
        self._clock = clock
        self._ids = ids
        self._audit = audit_sink or NullCapabilityAuditSink()

    async def resolve(
        self,
        *,
        workspace_id: str,
        run_id: str,
        host: HostCapabilitySnapshot,
        config: RunConfigSnapshot,
        trust: WorkspaceTrustSnapshot,
        policy: PolicyCapabilitySnapshot,
        tools: tuple[ToolCapabilityRequirement, ...] = (),
    ) -> CapabilityResolution:
        if trust.workspace_id != workspace_id:
            raise ValueError("workspace trust snapshot belongs to another workspace")
        if len({item.tool_name for item in tools}) != len(tools):
            raise ValueError("tool capability requirements contain duplicate names")
        configured = _configured_capabilities(config)
        trusted = trust.allowed & _TRUST_CEILINGS[trust.level]
        decisions: list[CapabilityDecision] = []
        enabled: set[RuntimeCapability] = set()
        for capability in RuntimeCapability:
            reason: CapabilityDisabledReason | None = None
            if capability not in host.available:
                reason = CapabilityDisabledReason.HOST_UNAVAILABLE
            elif capability not in configured:
                reason = CapabilityDisabledReason.DISABLED_BY_CONFIG
            elif capability not in trusted:
                reason = CapabilityDisabledReason.WORKSPACE_UNTRUSTED
            elif capability in policy.denied or capability not in policy.allowed:
                reason = CapabilityDisabledReason.DENIED_BY_POLICY
            else:
                enabled.add(capability)
            decisions.append(CapabilityDecision(capability, reason is None, reason))
        reason_by_capability = {item.capability: item.reason for item in decisions}
        visible: list[str] = []
        disabled_tools: dict[str, tuple[str, ...]] = {}
        for tool in sorted(tools, key=lambda item: item.tool_name):
            missing_reasons: set[str] = set()
            for required in tool.required:
                reason = reason_by_capability[required]
                if required not in enabled and reason is not None:
                    missing_reasons.add(reason.value)
            missing = tuple(sorted(missing_reasons))
            if missing:
                disabled_tools[tool.tool_name] = missing
            else:
                visible.append(tool.tool_name)
        canonical = {
            "workspaceId": workspace_id,
            "runId": run_id,
            "hostRevision": host.revision,
            "configFingerprint": config.fingerprint,
            "trustRevision": trust.revision,
            "policyRevision": policy.revision,
            "policyGrantIds": list(policy.grant_ids),
            "enabled": sorted(item.value for item in enabled),
            "disabledTools": disabled_tools,
        }
        encoded = json.dumps(canonical, separators=(",", ":"), sort_keys=True)
        fingerprint = f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
        resolution = CapabilityResolution(
            frozenset(enabled),
            tuple(decisions),
            tuple(visible),
            MappingProxyType(disabled_tools),
            fingerprint,
        )
        disabled_reasons: dict[str, str] = {}
        for item in decisions:
            if item.reason is not None:
                disabled_reasons[item.capability.value] = item.reason.value
        await self._audit.record(
            CapabilityAuditRecord(
                self._ids.new_id("audit"),
                workspace_id,
                run_id,
                config.fingerprint,
                fingerprint,
                tuple(sorted(item.value for item in enabled)),
                MappingProxyType(disabled_reasons),
                self._clock.utcnow(),
            )
        )
        return resolution


def _configured_capabilities(snapshot: RunConfigSnapshot) -> frozenset[RuntimeCapability]:
    config = snapshot.config
    enabled = {RuntimeCapability.WORKSPACE_READ}
    if config.network.model_provider_enabled:
        enabled.add(RuntimeCapability.MODEL)
    if not config.policy.read_only:
        enabled.add(RuntimeCapability.VAULT_WRITE)
    enabled.add(RuntimeCapability.WORKSPACE_READ)
    if config.memory.memory_enabled:
        enabled.add(RuntimeCapability.MEMORY)
    if config.execution.shell_enabled:
        enabled.add(RuntimeCapability.SHELL)
    if config.execution.subagents_enabled:
        enabled.add(RuntimeCapability.SUBAGENT)
    if config.extensibility.skills_enabled:
        enabled.add(RuntimeCapability.SKILLS)
    if config.extensibility.hooks_enabled:
        enabled.add(RuntimeCapability.HOOKS)
    if config.ui.loopback_web_enabled:
        enabled.add(RuntimeCapability.LOOPBACK_WEB)
    if config.telemetry.enabled:
        enabled.add(RuntimeCapability.TELEMETRY)
    if config.update.automatic_check and config.network.update_network_enabled:
        enabled.add(RuntimeCapability.UPDATE)
    return frozenset(enabled)


_TRUST_CEILINGS = {
    WorkspaceTrustLevel.UNTRUSTED: frozenset(
        {
            RuntimeCapability.MODEL,
            RuntimeCapability.WORKSPACE_READ,
            RuntimeCapability.LOOPBACK_WEB,
        }
    ),
    WorkspaceTrustLevel.TRUSTED: frozenset(RuntimeCapability) - {RuntimeCapability.SHELL, RuntimeCapability.HOOKS},
    WorkspaceTrustLevel.PRIVILEGED: frozenset(RuntimeCapability),
}


__all__ = [
    "CapabilityDecision",
    "CapabilityDisabledReason",
    "CapabilityResolution",
    "CapabilityResolver",
    "HostCapabilitySnapshot",
    "PolicyCapabilitySnapshot",
    "RuntimeCapability",
    "ToolCapabilityRequirement",
    "WorkspaceTrustLevel",
    "WorkspaceTrustSnapshot",
]
