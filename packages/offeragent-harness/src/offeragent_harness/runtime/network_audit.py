"""Durable local storage for content-free model-provider network audit events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from offeragent_harness.foundation.network_audit import NetworkAuditRecord
from offeragent_harness.ports.network_audit import NetworkAuditSink
from offeragent_harness.ports.storage import EntityRevisionConflict, EntityStore
from offeragent_harness.ports.system import IdGenerator


class EntityNetworkAuditSink(NetworkAuditSink):
    """Persists model-provider audit records through the local EntityStore."""

    COLLECTION = "network_audit_records"

    def __init__(self, entities: EntityStore, ids: IdGenerator) -> None:
        self._entities = entities
        self._ids = ids

    async def record(self, audit: NetworkAuditRecord) -> None:
        audit_id = self._ids.new_id("network_audit") if audit.event_id is None else f"network_audit_{audit.event_id}"
        payload: dict[str, Any] = {
            "schemaVersion": 4,
            "auditId": audit_id,
            "category": audit.category.value,
            "workspaceId": audit.workspace_id,
            "runId": audit.run_id,
            "toolCallId": audit.tool_call_id,
            "toolName": audit.tool_name,
            "providerId": audit.provider_id,
            "host": audit.host,
            "port": audit.port,
            "outcome": audit.outcome,
            "statusCode": audit.status_code,
            "sentBytes": audit.sent_bytes,
            "receivedBytes": audit.received_bytes,
            "redirectCount": audit.redirect_count,
            "attempt": audit.attempt,
            "approvalSource": audit.approval_source,
            "occurredAt": audit.occurred_at.isoformat(),
            "operationId": audit.operation_id,
            "operationPurpose": (None if audit.operation_purpose is None else audit.operation_purpose.value),
            "phase": audit.phase,
            "stage": audit.stage,
            "sequence": audit.sequence,
            "eventId": audit.event_id,
        }
        existing = await self._entities.get(self.COLLECTION, audit_id)
        if existing is not None:
            if _same_network_audit_event(existing, payload):
                return
            raise ValueError("network audit event ID was reused for different content")
        try:
            await self._entities.put(self.COLLECTION, audit_id, payload, expected_revision=0)
        except EntityRevisionConflict:
            existing = await self._entities.get(self.COLLECTION, audit_id)
            if not _same_network_audit_event(existing, payload):
                raise


def _same_network_audit_event(existing: Any, candidate: Mapping[str, Any]) -> bool:
    if not isinstance(existing, Mapping):
        return False
    left = dict(existing)
    right = dict(candidate)
    left.pop("occurredAt", None)
    right.pop("occurredAt", None)
    return left == right


__all__ = ["EntityNetworkAuditSink"]
