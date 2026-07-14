from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

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
from offeragent_harness.permissions.audit import NullPolicyAuditSink
from offeragent_harness.permissions.evaluator import RuleBasedPolicyEvaluator
from offeragent_harness.permissions.grants import grant_id_for_approval
from offeragent_harness.permissions.rules import PolicyRule, RuleEffect
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import InMemoryUnitOfWorkFactory, ManualCancellationToken, ManualClock
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

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def write_definition(*, version: str = "1") -> ToolDefinition:
    return ToolDefinition(
        name="vault.transaction",
        version=version,
        description="transactional test write",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "expectedHash": {"type": "string"},
            },
            "required": ["path", "content", "expectedHash"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        required_capabilities=frozenset({"vault.write"}),
        concurrency_safe=False,
        idempotent=True,
        retryable=False,
        timeout_ms=1_000,
        output_limit_bytes=1_024,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def tool_call(
    definition: ToolDefinition,
    *,
    lineage: AgentLineage | None = None,
    workspace_id: str = "ws_1",
    path: str = "tests/grant.md",
    expected_hash: str = "sha256:" + "b" * 64,
) -> ToolCall:
    actual_lineage = lineage or AgentLineage.root("run_1")
    arguments = {"path": path, "content": "hello", "expectedHash": expected_hash}
    return ToolCall(
        tool_call_id=f"call_{actual_lineage.run_id}_{definition.version}",
        run_id=actual_lineage.run_id,
        workspace_id=workspace_id,
        name=definition.name,
        version=definition.version,
        definition_fingerprint=definition.fingerprint,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=f"idem_{actual_lineage.run_id}_{definition.version}",
        deadline=None,
        lineage=actual_lineage,
        result_sensitivity=definition.result_sensitivity,
    )


def policy_context(
    call: ToolCall,
    *,
    now: datetime = NOW,
    session_id: str = "session_1",
    mode: PermissionMode = PermissionMode.NORMAL,
    trusted: bool = True,
    allowed: bool = True,
) -> PolicyContext:
    return PolicyContext(
        workspace_id=call.workspace_id,
        session_id=session_id,
        principal_id="principal_1",
        run_id=call.run_id,
        permission_mode=mode,
        effective_scope=CapabilityScope(
            allowed_tools=frozenset({call.name}) if allowed else frozenset(),
            denied_tools=frozenset(),
            allowed_risks=frozenset(RiskClass),
            root_capabilities=frozenset({"vault.write"}),
            allow_network=False,
            allow_secret_handles=False,
        ),
        workspace_trusted=trusted,
        now=now,
    )


async def approve(
    manager: ApprovalManager,
    definition: ToolDefinition,
    call: ToolCall,
    *,
    scope: ApprovalScope,
    include_descendants: bool = False,
    session_id: str = "session_1",
    approval_id: str = "approval_1",
) -> None:
    evaluator = RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink())
    decision = await evaluator.evaluate(definition, call, policy_context(call, session_id=session_id))
    assert decision.disposition is PolicyDisposition.ASK
    assert decision.approval_binding is not None
    request = ApprovalRequest(
        approval_id=approval_id,
        tool_call_id=call.tool_call_id,
        binding=decision.approval_binding,
        risk=definition.risk,
        summary="approve exact transaction",
        diff_artifact_ids=("art_diff",),
    )
    waiting = asyncio.create_task(manager.request(request, ManualCancellationToken()))
    await asyncio.sleep(0)
    resolved = ApprovalResolution(
        approval_id=approval_id,
        state=ApprovalState.APPROVED,
        scope=scope,
        resolved_at=NOW,
        resolver_id="user_1",
        include_descendants=include_descendants,
    )
    assert await manager.resolve(resolved) == resolved
    assert (await waiting).resolution == resolved


@pytest.mark.asyncio
async def test_reusable_grant_is_atomic_durable_and_once_never_propagates() -> None:
    uow = InMemoryUnitOfWorkFactory()
    manager = ApprovalManager(unit_of_work=uow, clock=ManualClock(NOW))
    definition = write_definition()
    call = tool_call(definition)

    await approve(manager, definition, call, scope=ApprovalScope.RUN)
    restarted = ApprovalManager(unit_of_work=uow, clock=ManualClock(NOW))
    evaluator = RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink(), grant_store=restarted.grants)
    decision = await evaluator.evaluate(definition, call, policy_context(call))
    assert decision.disposition is PolicyDisposition.ALLOW
    assert decision.reason_code == "approval_grant"
    assert decision.audit_facts["approvalGrantId"] == grant_id_for_approval("approval_1")

    once_uow = InMemoryUnitOfWorkFactory()
    once_manager = ApprovalManager(unit_of_work=once_uow, clock=ManualClock(NOW))
    await approve(once_manager, definition, call, scope=ApprovalScope.ONCE)
    once_evaluator = RuleBasedPolicyEvaluator(
        audit_sink=NullPolicyAuditSink(),
        grant_store=once_manager.grants,
    )
    assert (await once_evaluator.evaluate(definition, call, policy_context(call))).disposition is PolicyDisposition.ASK
    assert await once_uow.list_entities("approval_grants") == ()


@pytest.mark.asyncio
async def test_hard_safety_and_explicit_deny_always_precede_persistent_grant() -> None:
    uow = InMemoryUnitOfWorkFactory()
    manager = ApprovalManager(unit_of_work=uow, clock=ManualClock(NOW))
    definition = write_definition()
    call = tool_call(definition)
    await approve(manager, definition, call, scope=ApprovalScope.PERSISTENT)

    deny = RuleBasedPolicyEvaluator(
        (PolicyRule("global-deny", RuleEffect.DENY, tool_names=frozenset({definition.name})),),
        audit_sink=NullPolicyAuditSink(),
        grant_store=manager.grants,
    )
    assert (await deny.evaluate(definition, call, policy_context(call))).reason_code == "rule_deny"

    granted = RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink(), grant_store=manager.grants)
    read_only = await granted.evaluate(
        definition,
        call,
        policy_context(call, mode=PermissionMode.READ_ONLY),
    )
    untrusted = await granted.evaluate(definition, call, policy_context(call, trusted=False))
    missing_scope = await granted.evaluate(definition, call, policy_context(call, allowed=False))
    assert {read_only.reason_code, untrusted.reason_code, missing_scope.reason_code} == {
        "permission_mode_read_only",
        "workspace_untrusted",
        "scope_tool_not_allowed",
    }


@pytest.mark.asyncio
async def test_grant_binding_changes_fail_closed_and_descendants_require_explicit_same_root_inheritance() -> None:
    uow = InMemoryUnitOfWorkFactory()
    manager = ApprovalManager(unit_of_work=uow, clock=ManualClock(NOW))
    definition = write_definition()
    root = AgentLineage.root("run_1")
    original = tool_call(definition, lineage=root)
    await approve(manager, definition, original, scope=ApprovalScope.RUN, include_descendants=True)

    child = tool_call(definition, lineage=root.child("run_child", "researcher"))
    other_root = AgentLineage.root("run_other").child("run_other_child", "researcher")
    mismatches = (
        tool_call(write_definition(version="2"), lineage=root),
        tool_call(definition, lineage=root, path="tests/other.md"),
        tool_call(definition, lineage=root, workspace_id="ws_other"),
        tool_call(definition, lineage=root, expected_hash="sha256:" + "c" * 64),
        tool_call(definition, lineage=other_root),
    )
    assert await manager.grants.find_matching(child, policy_context(child)) is not None
    for changed in mismatches:
        assert await manager.grants.find_matching(changed, policy_context(changed)) is None

    no_inherit_uow = InMemoryUnitOfWorkFactory()
    no_inherit = ApprovalManager(unit_of_work=no_inherit_uow, clock=ManualClock(NOW))
    await approve(
        no_inherit,
        definition,
        original,
        scope=ApprovalScope.RUN,
        include_descendants=False,
    )
    assert await no_inherit.grants.find_matching(child, policy_context(child)) is None


@pytest.mark.asyncio
async def test_grants_revoke_expire_and_obey_run_session_lifetimes_after_restart() -> None:
    definition = write_definition()
    original = tool_call(definition)

    run_uow = InMemoryUnitOfWorkFactory()
    run_manager = ApprovalManager(unit_of_work=run_uow, clock=ManualClock(NOW))
    await approve(run_manager, definition, original, scope=ApprovalScope.RUN)
    await run_manager.revoke_run_grants("run_1", reason="run completed")
    restarted = ApprovalManager(unit_of_work=run_uow, clock=ManualClock(NOW))
    assert await restarted.grants.find_matching(original, policy_context(original)) is None

    session_uow = InMemoryUnitOfWorkFactory()
    session_manager = ApprovalManager(unit_of_work=session_uow, clock=ManualClock(NOW))
    await approve(session_manager, definition, original, scope=ApprovalScope.SESSION)
    later_root = tool_call(definition, lineage=AgentLineage.root("run_2"))
    assert await session_manager.grants.find_matching(later_root, policy_context(later_root)) is not None
    assert (
        await session_manager.grants.find_matching(
            later_root,
            policy_context(later_root, session_id="session_other"),
        )
        is None
    )
    await session_manager.revoke_session_grants("session_1", reason="session archived")
    assert await session_manager.grants.find_matching(later_root, policy_context(later_root)) is None

    expiry_uow = InMemoryUnitOfWorkFactory()
    expiry_clock = ManualClock(NOW)
    expiry_manager = ApprovalManager(
        unit_of_work=expiry_uow,
        clock=expiry_clock,
        grant_expiry_policy=lambda _request, resolution: resolution.resolved_at + timedelta(minutes=1),
    )
    await approve(expiry_manager, definition, original, scope=ApprovalScope.PERSISTENT)
    expiry_clock.advance(timedelta(minutes=2))
    await expiry_manager.expire_grants()
    expired_context = policy_context(original, now=expiry_clock.utcnow())
    assert await expiry_manager.grants.find_matching(original, expired_context) is None

    persistent_uow = InMemoryUnitOfWorkFactory()
    persistent_manager = ApprovalManager(unit_of_work=persistent_uow, clock=ManualClock(NOW))
    await approve(persistent_manager, definition, original, scope=ApprovalScope.PERSISTENT)
    other_session_root = tool_call(definition, lineage=AgentLineage.root("run_3"))
    assert (
        await persistent_manager.grants.find_matching(
            other_session_root,
            policy_context(other_session_root, session_id="session_2"),
        )
        is not None
    )
    await persistent_manager.revoke_grant(
        grant_id_for_approval("approval_1"),
        revoked_by="user_1",
        reason="user revoked persistent rule",
    )
    assert (
        await persistent_manager.grants.find_matching(
            other_session_root,
            policy_context(other_session_root, session_id="session_2"),
        )
        is None
    )
