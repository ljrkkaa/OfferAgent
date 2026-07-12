"""Pure policy and approval value objects; UI decisions do not live here."""

from __future__ import annotations

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
    args_hash: str
    workspace_id: str
    root_run_id: str
    run_id: str
    expected_state_hash: str | None
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("approval expiry must be timezone-aware")
        required = (self.tool_name, self.tool_version, self.args_hash, self.workspace_id, self.root_run_id, self.run_id)
        if any(not value for value in required):
            raise ValueError("approval bindings require non-empty identity fields")


@dataclass(frozen=True)
class PolicyDecision:
    disposition: PolicyDisposition
    risk: RiskClass
    reason_code: str
    user_message: str
    audit_facts: Mapping[str, Any] = field(default_factory=dict)
    approval_binding: ApprovalBinding | None = None

    def __post_init__(self) -> None:
        if not self.reason_code or not self.user_message:
            raise ValueError("policy decisions require a reason code and user message")
        if (self.disposition is PolicyDisposition.ASK) != (self.approval_binding is not None):
            raise ValueError("only ask decisions carry an approval binding")
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


@dataclass(frozen=True)
class PolicyContext:
    workspace_id: str
    session_id: str
    run_id: str
    permission_mode: PermissionMode
    effective_scope: CapabilityScope
    workspace_trusted: bool
    now: datetime


__all__ = [
    "ApprovalBinding",
    "ApprovalRequest",
    "ApprovalResolution",
    "ApprovalScope",
    "ApprovalState",
    "CapabilityScope",
    "PolicyContext",
    "PolicyDecision",
    "PolicyDisposition",
]
