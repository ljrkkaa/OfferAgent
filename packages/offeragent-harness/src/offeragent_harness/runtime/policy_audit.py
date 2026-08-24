"""Append-only production Policy audit persistence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from offeragent_harness.error_codes import ResourceConflictCause
from offeragent_harness.models import thaw_json
from offeragent_harness.permissions import PolicyDisposition, RiskClass
from offeragent_harness.permissions.audit import PolicyAuditRecord, PolicyAuditSink
from offeragent_harness.ports import EntityRevisionConflict, UnitOfWorkFactory
from offeragent_harness.tools import canonical_json_sha256

POLICY_AUDIT_COLLECTION = "policy_audits"
_FIELDS = frozenset(
    {
        "auditId",
        "workspaceId",
        "sessionId",
        "runId",
        "rootRunId",
        "toolCallId",
        "toolName",
        "toolVersion",
        "argsHash",
        "disposition",
        "risk",
        "reasonCode",
        "matchedRuleIds",
        "evaluatedAt",
        "facts",
    }
)


class PolicyAuditConflict(RuntimeError, ResourceConflictCause):
    """An existing append-only audit ID is bound to different content."""


class PolicyAuditCorrupt(RuntimeError):
    """Persisted Policy audit data cannot be reconstructed safely."""


class EntityPolicyAuditSink(PolicyAuditSink):
    """CAS-append redacted ``PolicyAuditRecord`` values to the Worker UOW.

    The sink deliberately exposes no update or delete operation.  Replaying the
    exact same audit ID is idempotent; binding that ID to different content is a
    hard conflict, including when two writers race.
    """

    def __init__(self, unit_of_work: UnitOfWorkFactory) -> None:
        self._unit_of_work = unit_of_work

    async def record(self, audit: PolicyAuditRecord) -> None:
        if not isinstance(audit, PolicyAuditRecord):
            raise TypeError("Policy audit sink accepts only PolicyAuditRecord")
        value = _audit_value(audit)
        try:
            async with self._unit_of_work.begin() as transaction:
                existing = await transaction.entities.get(POLICY_AUDIT_COLLECTION, audit.audit_id)
                if existing is not None:
                    _assert_same(audit.audit_id, existing, value)
                    return
                await transaction.entities.put(
                    POLICY_AUDIT_COLLECTION,
                    audit.audit_id,
                    value,
                    expected_revision=0,
                )
                await transaction.commit()
        except EntityRevisionConflict:
            async with self._unit_of_work.begin() as transaction:
                existing = await transaction.entities.get(POLICY_AUDIT_COLLECTION, audit.audit_id)
            if existing is None:
                raise PolicyAuditConflict("Policy audit CAS conflicted without a durable winner") from None
            _assert_same(audit.audit_id, existing, value)

    async def get(self, audit_id: str) -> PolicyAuditRecord | None:
        async with self._unit_of_work.begin() as transaction:
            value = await transaction.entities.get(POLICY_AUDIT_COLLECTION, audit_id)
        return None if value is None else _audit_from_value(audit_id, value)

    async def list_records(
        self,
        *,
        after_id: str | None = None,
        limit: int = 100,
    ) -> tuple[PolicyAuditRecord, ...]:
        if not 1 <= limit <= 1_000:
            raise ValueError("Policy audit page limit must be between 1 and 1000")
        async with self._unit_of_work.begin() as transaction:
            records = await transaction.entities.list(
                POLICY_AUDIT_COLLECTION,
                after_id=after_id,
                limit=limit,
            )
        return tuple(_audit_from_value(item.entity_id, item.value) for item in records)


def _audit_value(audit: PolicyAuditRecord) -> dict[str, Any]:
    return {
        "auditId": audit.audit_id,
        "workspaceId": audit.workspace_id,
        "sessionId": audit.session_id,
        "runId": audit.run_id,
        "rootRunId": audit.root_run_id,
        "toolCallId": audit.tool_call_id,
        "toolName": audit.tool_name,
        "toolVersion": audit.tool_version,
        "argsHash": audit.args_hash,
        "disposition": audit.disposition.value,
        "risk": audit.risk.value,
        "reasonCode": audit.reason_code,
        "matchedRuleIds": list(audit.matched_rule_ids),
        "evaluatedAt": audit.evaluated_at.isoformat(),
        "facts": thaw_json(audit.facts),
    }


def _audit_from_value(audit_id: str, value: object) -> PolicyAuditRecord:
    if not isinstance(value, Mapping) or frozenset(value) != _FIELDS or value.get("auditId") != audit_id:
        raise PolicyAuditCorrupt("persisted Policy audit shape or identity is invalid")
    matched = value.get("matchedRuleIds")
    facts = value.get("facts")
    if (
        not isinstance(matched, (list, tuple))
        or any(not isinstance(item, str) for item in matched)
        or not isinstance(facts, Mapping)
    ):
        raise PolicyAuditCorrupt("persisted Policy audit facts or rule IDs are invalid")
    text_fields = (
        "workspaceId",
        "sessionId",
        "runId",
        "rootRunId",
        "toolCallId",
        "toolName",
        "toolVersion",
        "argsHash",
        "reasonCode",
        "evaluatedAt",
    )
    if any(not isinstance(value.get(name), str) or not value[name] for name in text_fields):
        raise PolicyAuditCorrupt("persisted Policy audit text field is invalid")
    try:
        return PolicyAuditRecord(
            audit_id=audit_id,
            workspace_id=str(value["workspaceId"]),
            session_id=str(value["sessionId"]),
            run_id=str(value["runId"]),
            root_run_id=str(value["rootRunId"]),
            tool_call_id=str(value["toolCallId"]),
            tool_name=str(value["toolName"]),
            tool_version=str(value["toolVersion"]),
            args_hash=str(value["argsHash"]),
            disposition=PolicyDisposition(value["disposition"]),
            risk=RiskClass(value["risk"]),
            reason_code=str(value["reasonCode"]),
            matched_rule_ids=tuple(matched),
            evaluated_at=datetime.fromisoformat(str(value["evaluatedAt"])),
            facts=dict(facts),
        )
    except (TypeError, ValueError) as error:
        raise PolicyAuditCorrupt("persisted Policy audit value is invalid") from error


def _assert_same(audit_id: str, existing: object, proposed: Mapping[str, Any]) -> None:
    if not isinstance(existing, Mapping):
        raise PolicyAuditConflict(f"Policy audit {audit_id!r} is bound to corrupt content")
    if canonical_json_sha256(thaw_json(existing)) != canonical_json_sha256(thaw_json(proposed)):
        raise PolicyAuditConflict(f"Policy audit {audit_id!r} is already bound to different content")


__all__ = [
    "POLICY_AUDIT_COLLECTION",
    "EntityPolicyAuditSink",
    "PolicyAuditConflict",
    "PolicyAuditCorrupt",
]
