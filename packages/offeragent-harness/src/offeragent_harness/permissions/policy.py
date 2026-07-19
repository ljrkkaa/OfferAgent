"""Pure policy and approval value objects; UI decisions do not live here."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json

from .risk import PermissionMode, RiskClass


class PolicyDisposition(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalScope(str, Enum):
    ONCE = "once"
    RUN = "run"
    SESSION = "session"
    PERSISTENT = "persistent"


@dataclass(frozen=True)
class CapabilityScope:
    """A finite capability ceiling that can be intersected without widening."""

    allowed_tools: frozenset[str]
    denied_tools: frozenset[str]
    allowed_risks: frozenset[RiskClass]
    root_capabilities: frozenset[str]
    allow_network: bool
    allow_secret_handles: bool

    def intersect(self, other: CapabilityScope) -> CapabilityScope:
        return CapabilityScope(
            allowed_tools=self.allowed_tools & other.allowed_tools,
            denied_tools=self.denied_tools | other.denied_tools,
            allowed_risks=self.allowed_risks & other.allowed_risks,
            root_capabilities=self.root_capabilities & other.root_capabilities,
            allow_network=self.allow_network and other.allow_network,
            allow_secret_handles=self.allow_secret_handles and other.allow_secret_handles,
        )

    def permits_tool(self, name: str, risk: RiskClass) -> bool:
        return name in self.allowed_tools and name not in self.denied_tools and risk in self.allowed_risks


@dataclass(frozen=True)
class ApprovalBinding:
    tool_name: str
    tool_version: str
    definition_fingerprint: str
    args_hash: str
    workspace_id: str
    session_id: str
    principal_id: str
    root_run_id: str
    run_id: str
    agent_name: str
    ancestor_run_ids: tuple[str, ...]
    expected_state_hash: str | None
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("approval expiry must be timezone-aware")
        required = (
            self.tool_name,
            self.tool_version,
            self.definition_fingerprint,
            self.args_hash,
            self.workspace_id,
            self.session_id,
            self.principal_id,
            self.root_run_id,
            self.run_id,
            self.agent_name,
        )
        if any(not value for value in required):
            raise ValueError("approval bindings require non-empty identity fields")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.definition_fingerprint) is None:
            raise ValueError("approval definition_fingerprint must be a canonical sha256 digest")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.args_hash) is None:
            raise ValueError("approval args_hash must be a canonical sha256 digest")
        expected_state_hash = self.expected_state_hash
        if expected_state_hash not in {None, "absent"} and (
            not isinstance(expected_state_hash, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_state_hash) is None
        ):
            raise ValueError("approval expected_state_hash must be a canonical sha256 digest or 'absent'")
        ancestors = tuple(self.ancestor_run_ids)
        object.__setattr__(self, "ancestor_run_ids", ancestors)
        if any(not value for value in ancestors):
            raise ValueError("approval lineage ancestors must not contain empty IDs")
        if not ancestors:
            if self.root_run_id != self.run_id:
                raise ValueError("root approval lineage must reference its own root run")
        else:
            if ancestors[0] != self.root_run_id:
                raise ValueError("approval lineage must start with root_run_id")
            if self.run_id in ancestors:
                raise ValueError("approval lineage cannot contain a cycle")

    @property
    def has_grant_identity(self) -> bool:
        """Whether this binding can safely back a reusable authorization."""

        return self.has_recovery_identity

    @property
    def has_recovery_identity(self) -> bool:
        return all(
            (
                self.tool_name,
                self.tool_version,
                self.definition_fingerprint,
                self.args_hash,
                self.workspace_id,
                self.session_id,
                self.principal_id,
                self.root_run_id,
                self.run_id,
                self.agent_name,
            )
        )

    @property
    def recovery_identity(self) -> tuple[object, ...]:
        """Stable authorization identity; expiry is checked, never extended by retry."""

        return (
            self.tool_name,
            self.tool_version,
            self.definition_fingerprint,
            self.args_hash,
            self.workspace_id,
            self.session_id,
            self.principal_id,
            self.root_run_id,
            self.run_id,
            self.agent_name,
            self.ancestor_run_ids,
            self.expected_state_hash,
        )

    def same_recovery_identity(self, other: ApprovalBinding) -> bool:
        return self.recovery_identity == other.recovery_identity


def approval_id_for(tool_call_id: str, binding: ApprovalBinding) -> str:
    if not tool_call_id:
        raise ValueError("tool_call_id must not be empty")
    encoded = json.dumps(
        [tool_call_id, *binding.recovery_identity],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"apr_{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True)
class PolicyDecision:
    disposition: PolicyDisposition
    risk: RiskClass
    reason_code: str
    user_message: str
    audit_facts: Mapping[str, Any] = field(default_factory=dict)
    approval_binding: ApprovalBinding | None = None
    result_context_activations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.reason_code or not self.user_message:
            raise ValueError("policy decisions require a reason code and user message")
        if (self.disposition is PolicyDisposition.ASK) != (self.approval_binding is not None):
            raise ValueError("only ask decisions carry an approval binding")
        activations = tuple(self.result_context_activations)
        if len(activations) != len(set(activations)) or any(
            not value or len(value) > 256 or "\x00" in value for value in activations
        ):
            raise ValueError("policy result context activations must be unique bounded identifiers")
        object.__setattr__(self, "result_context_activations", activations)
        frozen = freeze_json(self.audit_facts)
        if not isinstance(frozen, FrozenJsonObject):
            raise TypeError("audit_facts must be a JSON object")
        object.__setattr__(self, "audit_facts", frozen)


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    tool_call_id: str
    binding: ApprovalBinding
    risk: RiskClass
    summary: str
    diff_artifact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.approval_id or not self.tool_call_id or not self.summary:
            raise ValueError("approval request identity and summary must not be empty")
        if not self.binding.has_recovery_identity:
            raise ValueError("approval request requires a complete recovery binding")
        artifacts = tuple(self.diff_artifact_ids)
        if any(not artifact_id for artifact_id in artifacts):
            raise ValueError("approval diff artifact IDs must not be empty")
        object.__setattr__(self, "diff_artifact_ids", artifacts)


@dataclass(frozen=True)
class ApprovalResolution:
    approval_id: str
    state: ApprovalState
    scope: ApprovalScope
    resolved_at: datetime
    resolver_id: str
    include_descendants: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.state is ApprovalState.PENDING:
            raise ValueError("a resolution cannot remain pending")
        if self.resolved_at.tzinfo is None or self.resolved_at.utcoffset() is None:
            raise ValueError("resolved_at must be timezone-aware")
        if self.state is not ApprovalState.APPROVED and self.scope is not ApprovalScope.ONCE:
            raise ValueError("only approved resolutions may create reusable approval scopes")
        if self.include_descendants and (self.state is not ApprovalState.APPROVED or self.scope is ApprovalScope.ONCE):
            raise ValueError("one-time or non-approved decisions cannot propagate to descendants")


@dataclass(frozen=True)
class ApprovalDecisionReceipt:
    """Canonical persisted request plus its durable decision."""

    request: ApprovalRequest
    resolution: ApprovalResolution

    def __post_init__(self) -> None:
        if self.request.approval_id != self.resolution.approval_id:
            raise ValueError("approval receipt request and resolution identities must match")


@dataclass(frozen=True)
class PolicyContext:
    workspace_id: str
    session_id: str
    principal_id: str
    run_id: str
    permission_mode: PermissionMode
    effective_scope: CapabilityScope
    workspace_trusted: bool
    now: datetime

    def __post_init__(self) -> None:
        if not self.workspace_id or not self.session_id or not self.principal_id or not self.run_id:
            raise ValueError("policy context identity fields must not be empty")
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("policy context now must be timezone-aware")


__all__ = [
    "ApprovalBinding",
    "ApprovalDecisionReceipt",
    "ApprovalRequest",
    "ApprovalResolution",
    "ApprovalScope",
    "ApprovalState",
    "CapabilityScope",
    "PolicyContext",
    "PolicyDecision",
    "PolicyDisposition",
    "approval_id_for",
]
