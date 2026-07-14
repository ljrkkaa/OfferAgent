"""Deterministic in-memory sink for model-provider network audit tests."""

from __future__ import annotations

from offeragent_harness.foundation.network_audit import NetworkAuditRecord
from offeragent_harness.ports.network_audit import NetworkAuditSink


class RecordingNetworkAuditSink(NetworkAuditSink):
    def __init__(self) -> None:
        self.records: list[NetworkAuditRecord] = []

    async def record(self, audit: NetworkAuditRecord) -> None:
        self.records.append(audit)


__all__ = ["RecordingNetworkAuditSink"]
