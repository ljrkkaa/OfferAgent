"""Auditable capability-resolution boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CapabilityAuditRecord:
    audit_id: str
    workspace_id: str
    run_id: str
    config_fingerprint: str
    resolution_fingerprint: str
    enabled: tuple[str, ...]
    disabled_reasons: Mapping[str, str]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("capability audit timestamp must be timezone-aware")


@runtime_checkable
class CapabilityAuditSink(Protocol):
    async def record(self, audit: CapabilityAuditRecord) -> None: ...


class NullCapabilityAuditSink:
    async def record(self, audit: CapabilityAuditRecord) -> None:
        del audit


__all__ = ["CapabilityAuditRecord", "CapabilityAuditSink", "NullCapabilityAuditSink"]
