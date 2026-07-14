from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.permissions import (
    ApprovalRequest,
    ApprovalResolution,
    ApprovalScope,
    ApprovalState,
    CapabilityScope,
    PermissionMode,
    PolicyContext,
    PolicyDisposition,
    RiskClass,
)
from offeragent_harness.permissions.audit import PolicyAuditRecord
from offeragent_harness.permissions.evaluator import RuleBasedPolicyEvaluator
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.runtime.policy_audit import (
    POLICY_AUDIT_COLLECTION,
    EntityPolicyAuditSink,
    PolicyAuditConflict,
)
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import ManualCancellationToken, ManualClock
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffectClass,
    ToolCall,
    ToolDefinition,
    canonical_json_sha256,
)

NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)
_PERSISTED_FIELDS = {
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


def _record(audit_id: str = "audit:test:1") -> PolicyAuditRecord:
    return PolicyAuditRecord(
        audit_id=audit_id,
        workspace_id="ws_policy",
        session_id="ses_policy",
        run_id="run_policy",
        root_run_id="run_policy",
        tool_call_id="call_policy",
        tool_name="workspace.read",
        tool_version="1",
        args_hash="sha256:" + "a" * 64,
        disposition=PolicyDisposition.ALLOW,
        risk=RiskClass.READ,
        reason_code="default_safe_read",
        matched_rule_ids=(),
        evaluated_at=NOW,
        facts={"permissionMode": "normal", "workspaceTrusted": True},
    )


def _definition(*, write: bool) -> ToolDefinition:
    return ToolDefinition(
        name="vault.transaction" if write else "workspace.read",
        version="1",
        description="policy audit integration tool",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.WRITE if write else RiskClass.READ,
        side_effect_class=SideEffectClass.WRITE if write else SideEffectClass.READ,
        required_capabilities=frozenset({"vault.write" if write else "workspace.read"}),
        concurrency_safe=not write,
        idempotent=not write,
        retryable=not write,
        timeout_ms=1_000,
        output_limit_bytes=1_024,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def _call(definition: ToolDefinition, suffix: str) -> ToolCall:
    arguments = {"value": 1}
    return ToolCall(
        tool_call_id=f"call_policy_{suffix}",
        run_id="run_policy",
        workspace_id="ws_policy",
        name=definition.name,
        version=definition.version,
        definition_fingerprint=definition.fingerprint,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=f"idem_policy_{suffix}",
        deadline=None,
        lineage=AgentLineage.root("run_policy"),
        result_sensitivity=definition.result_sensitivity,
    )


def _context(definition: ToolDefinition, *, mode: PermissionMode) -> PolicyContext:
    return PolicyContext(
        workspace_id="ws_policy",
        session_id="ses_policy",
        principal_id="profile_policy",
        run_id="run_policy",
        permission_mode=mode,
        effective_scope=CapabilityScope(
            allowed_tools=frozenset({definition.name}),
            denied_tools=frozenset(),
            allowed_risks=frozenset(RiskClass),
            root_capabilities=definition.required_capabilities,
            allow_network=False,
            allow_secret_handles=False,
        ),
        workspace_trusted=True,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_policy_audit_is_cas_append_only_idempotent_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "policy-audit.sqlite"
    first = SqliteUnitOfWorkFactory(database)
    sink = EntityPolicyAuditSink(first)
    record = _record()

    await asyncio.gather(sink.record(record), sink.record(record), sink.record(record))
    raw = await first.list_entities(POLICY_AUDIT_COLLECTION)
    assert len(raw) == 1 and raw[0].revision == 1
    assert set(raw[0].value) == _PERSISTED_FIELDS

    restarted = EntityPolicyAuditSink(SqliteUnitOfWorkFactory(database))
    assert await restarted.get(record.audit_id) == record
    assert await restarted.list_records() == (record,)
    with pytest.raises(PolicyAuditConflict, match="different content"):
        await restarted.record(replace(record, reason_code="changed_after_append"))
    assert await restarted.list_records() == (record,)


@pytest.mark.asyncio
async def test_allow_deny_ask_and_post_approval_revalidation_are_durable_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "policy-decisions.sqlite"
    unit_of_work = SqliteUnitOfWorkFactory(database)
    sink = EntityPolicyAuditSink(unit_of_work)
    approvals = ApprovalManager(unit_of_work=unit_of_work, clock=ManualClock(NOW))
    read = _definition(write=False)
    write = _definition(write=True)
    read_call = _call(read, "allow")
    write_call = _call(write, "write")
    evaluator = RuleBasedPolicyEvaluator(audit_sink=sink, grant_store=approvals.grants)

    allowed = await evaluator.evaluate(read, read_call, _context(read, mode=PermissionMode.NORMAL))
    denied = await evaluator.evaluate(write, write_call, _context(write, mode=PermissionMode.READ_ONLY))
    asked = await evaluator.evaluate(write, write_call, _context(write, mode=PermissionMode.NORMAL))
    assert [allowed.disposition, denied.disposition, asked.disposition] == [
        PolicyDisposition.ALLOW,
        PolicyDisposition.DENY,
        PolicyDisposition.ASK,
    ]
    assert asked.approval_binding is not None

    request = ApprovalRequest(
        approval_id="approval_policy",
        tool_call_id=write_call.tool_call_id,
        binding=asked.approval_binding,
        risk=write.risk,
        summary="approve policy audit test",
        diff_artifact_ids=(),
    )
    waiting = asyncio.create_task(approvals.request(request, ManualCancellationToken()))
    for _ in range(100):
        if await approvals.get(request.approval_id) is not None:
            break
        await asyncio.sleep(0.01)
    assert await approvals.get(request.approval_id) is not None
    await approvals.resolve(
        ApprovalResolution(
            approval_id=request.approval_id,
            state=ApprovalState.APPROVED,
            scope=ApprovalScope.RUN,
            resolved_at=NOW,
            resolver_id="local-user",
            include_descendants=False,
        )
    )
    await waiting
    revalidated = await evaluator.evaluate(
        write,
        write_call,
        _context(write, mode=PermissionMode.NORMAL),
    )
    assert revalidated.disposition is PolicyDisposition.ALLOW
    assert revalidated.reason_code == "approval_grant"

    restarted = EntityPolicyAuditSink(SqliteUnitOfWorkFactory(database))
    records = await restarted.list_records(limit=100)
    assert {item.disposition for item in records} == {
        PolicyDisposition.ALLOW,
        PolicyDisposition.DENY,
        PolicyDisposition.ASK,
    }
    assert {item.reason_code for item in records} >= {
        "default_safe_read",
        "permission_mode_read_only",
        "default_approval_required",
        "approval_grant",
    }
    assert len(records) == 4
