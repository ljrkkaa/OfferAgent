"""Append-only policy audit boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from offeragent_harness.models import FrozenJsonObject, freeze_json

from .policy import PolicyDisposition
from .risk import RiskClass


@dataclass(frozen=True)
class PolicyAuditRecord:
    audit_id: str
    workspace_id: str
    session_id: str
    run_id: str
    root_run_id: str
    tool_call_id: str
    tool_name: str
    tool_version: str
    args_hash: str
    disposition: PolicyDisposition
    risk: RiskClass
    reason_code: str
    matched_rule_ids: tuple[str, ...]
    evaluated_at: datetime
    facts: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("policy audit timestamps must be timezone-aware")
        facts = freeze_json(self.facts)
        if not isinstance(facts, FrozenJsonObject):
            raise TypeError("policy audit facts must be a JSON object")
        object.__setattr__(self, "facts", facts)


@runtime_checkable
class PolicyAuditSink(Protocol):
    async def record(self, audit: PolicyAuditRecord) -> None: ...


class NullPolicyAuditSink:
    async def record(self, audit: PolicyAuditRecord) -> None:
        del audit


__all__ = ["NullPolicyAuditSink", "PolicyAuditRecord", "PolicyAuditSink"]
