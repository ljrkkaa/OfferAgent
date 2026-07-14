"""Persistence port for content-free outbound network audit records."""

from typing import Protocol, runtime_checkable

from offeragent_harness.foundation.network_audit import NetworkAuditRecord


@runtime_checkable
class NetworkAuditSink(Protocol):
    async def record(self, audit: NetworkAuditRecord) -> None: ...


__all__ = ["NetworkAuditSink"]
