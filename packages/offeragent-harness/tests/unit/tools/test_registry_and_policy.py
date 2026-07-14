from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from offeragent_harness.permissions import (
    CapabilityScope,
    PermissionMode,
    PolicyContext,
    PolicyDisposition,
    RiskClass,
)
from offeragent_harness.permissions.audit import NullPolicyAuditSink, PolicyAuditRecord
from offeragent_harness.permissions.evaluator import RuleBasedPolicyEvaluator
from offeragent_harness.permissions.rules import PolicyRule, PolicyRuleSet, RuleEffect
from offeragent_harness.sessions import AgentLineage
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
from offeragent_harness.tools.registry import (
    DuplicateToolDefinition,
    ToolCapabilityUnavailable,
    ToolPreflightUnavailable,
    ToolRegistry,
    ToolResultSensitivityUnavailable,
    ToolVersionUnavailable,
)
from offeragent_harness.vault import (
    client_vault_transaction_definition,
    legacy_vault_transaction_definition,
    vault_transaction_definition,
)

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def definition(
    name: str,
    version: str = "1",
    *,
    risk: RiskClass = RiskClass.READ,
    effect: SideEffectClass = SideEffectClass.READ,
    capabilities: frozenset[str] = frozenset({"workspace.read"}),
    concurrent: bool = True,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version=version,
        description=f"{name} test tool",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}, "expectedHash": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        executor_location=ExecutorLocation.LOCAL,
        risk=risk,
        side_effect_class=effect,
        required_capabilities=capabilities,
        concurrency_safe=concurrent,
        idempotent=True,
        retryable=True,
        timeout_ms=1_000,
        output_limit_bytes=1_024,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def call(tool: ToolDefinition, value: int = 1) -> ToolCall:
    arguments = {"value": value}
    return ToolCall(
        tool_call_id=f"call_{value}",
        run_id="run_1",
        workspace_id="ws_1",
        name=tool.name,
        version=tool.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=f"idem_{value}",
        deadline=None,
        lineage=AgentLineage.root("run_1"),
        definition_fingerprint=tool.fingerprint,
        result_sensitivity=tool.result_sensitivity,
    )


def context(tool_names: frozenset[str], capabilities: frozenset[str], *, mode: PermissionMode) -> PolicyContext:
    return PolicyContext(
        workspace_id="ws_1",
        session_id="session_1",
        principal_id="principal_1",
        run_id="run_1",
        permission_mode=mode,
        effective_scope=CapabilityScope(
            allowed_tools=tool_names,
            denied_tools=frozenset(),
            allowed_risks=frozenset(RiskClass),
            root_capabilities=capabilities,
            allow_network=True,
            allow_secret_handles=True,
        ),
        workspace_trusted=True,
        now=NOW,
    )


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[PolicyAuditRecord] = []

    async def record(self, audit: PolicyAuditRecord) -> None:
        self.records.append(audit)


def test_registry_is_an_immutable_versioned_capability_snapshot() -> None:
    v1 = definition("workspace.read", "1")
    v2 = definition("workspace.read", "2", capabilities=frozenset({"workspace.read", "vault.v2"}))
    registry = ToolRegistry("snapshot_1", (v2, v1))

    assert registry.definitions == (v1, v2)
    assert registry.versions("workspace.read") == ("1", "2")
    assert registry.resolve("workspace.read", "1", frozenset({"workspace.read"})) == v1
    with pytest.raises(ToolCapabilityUnavailable):
        registry.resolve("workspace.read", "2", frozenset({"workspace.read"}))
    with pytest.raises(ToolVersionUnavailable):
        registry.get("workspace.read", "3")
    assert registry.catalog(frozenset({"workspace.read"})) == (v1,)
    assert ToolRegistry("snapshot_1", (v1, v2)).snapshot_hash == registry.snapshot_hash
    with pytest.raises(AttributeError, match="immutable"):
        registry.snapshot_id = "mutated"


def test_registry_rejects_duplicate_name_and_version() -> None:
    tool = definition("workspace.read")
    with pytest.raises(DuplicateToolDefinition):
        ToolRegistry("snapshot_1", (tool, tool))


def test_registry_rejects_required_preflight_without_registered_provider() -> None:
    tool = vault_transaction_definition()

    with pytest.raises(ToolPreflightUnavailable):
        ToolRegistry("snapshot_1", (tool,))
    registry = ToolRegistry(
        "snapshot_1",
        (tool,),
        preflight_provider_ids=frozenset({"vault.transaction.v1"}),
    )
    assert registry.get(tool.name, tool.version) is tool


def test_legacy_client_vault_route_is_recognizable_but_cannot_be_active() -> None:
    local = vault_transaction_definition()
    client = client_vault_transaction_definition()

    assert client.name == local.name == "vault.transaction"
    assert client.version == local.version
    assert client.input_schema == local.input_schema
    assert client.output_schema == local.output_schema
    assert client.executor_location is ExecutorLocation.CLIENT
    assert client.result_sensitivity is ResultSensitivity.UNKNOWN
    assert local.result_sensitivity is ResultSensitivity.WORKSPACE
    assert set(client.input_schema["properties"]) == {"operations"}
    assert client.input_schema["properties"]["operations"]["maxItems"] == 1
    legacy_local = legacy_vault_transaction_definition()
    legacy_client = legacy_vault_transaction_definition(executor_location=ExecutorLocation.CLIENT)
    assert legacy_local.fingerprint != local.fingerprint
    assert legacy_client.fingerprint != client.fingerprint
    assert legacy_local.input_schema["properties"]["operations"]["maxItems"] == 20
    with pytest.raises(ToolResultSensitivityUnavailable):
        ToolRegistry(
            "snapshot_invalid_dual_route",
            (local, client),
            preflight_provider_ids=frozenset({"vault.transaction.v1"}),
        )


def test_registry_rejects_any_unknown_result_sensitivity() -> None:
    with pytest.raises(ToolResultSensitivityUnavailable):
        ToolRegistry(
            "snapshot_legacy",
            (replace(definition("workspace.read"), result_sensitivity=ResultSensitivity.UNKNOWN),),
        )


@pytest.mark.asyncio
async def test_policy_precedence_is_deny_then_allow_then_ask_and_is_audited() -> None:
    tool = definition("workspace.read")
    rules = PolicyRuleSet.from_iterable(
        (
            PolicyRule("ask", RuleEffect.ASK, tool_names=frozenset({tool.name}), reason="ask"),
            PolicyRule("allow", RuleEffect.ALLOW, tool_names=frozenset({tool.name}), reason="allow"),
            PolicyRule("deny", RuleEffect.DENY, tool_names=frozenset({tool.name}), reason="deny"),
        )
    )
    audit = RecordingAudit()
    evaluator = RuleBasedPolicyEvaluator(rules, audit_sink=audit)
    decision = await evaluator.evaluate(
        tool,
        call(tool),
        context(frozenset({tool.name}), tool.required_capabilities, mode=PermissionMode.NORMAL),
    )

    assert decision.disposition is PolicyDisposition.DENY
    assert decision.reason_code == "rule_deny"
    assert len(audit.records) == 1
    assert audit.records[0].matched_rule_ids == ("allow", "ask", "deny")


@pytest.mark.asyncio
async def test_hard_read_only_and_scope_denials_cannot_be_overridden_by_allow_rule() -> None:
    write = definition(
        "vault.write",
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        capabilities=frozenset({"vault.write"}),
        concurrent=False,
    )
    evaluator = RuleBasedPolicyEvaluator(
        (PolicyRule("allow-write", RuleEffect.ALLOW, tool_names=frozenset({write.name})),),
        audit_sink=NullPolicyAuditSink(),
    )
    decision = await evaluator.evaluate(
        write,
        call(write),
        context(frozenset({write.name}), write.required_capabilities, mode=PermissionMode.READ_ONLY),
    )
    assert decision.disposition is PolicyDisposition.DENY
    assert decision.reason_code == "permission_mode_read_only"


@pytest.mark.asyncio
async def test_default_safe_read_allows_and_effectful_normal_mode_asks_with_hash_binding() -> None:
    read = definition("workspace.read")
    write = definition(
        "vault.write",
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        capabilities=frozenset({"vault.write"}),
        concurrent=False,
    )
    evaluator = RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink())

    read_decision = await evaluator.evaluate(
        read,
        call(read),
        context(frozenset({read.name}), read.required_capabilities, mode=PermissionMode.NORMAL),
    )
    write_call = call(write)
    write_decision = await evaluator.evaluate(
        write,
        write_call,
        context(frozenset({write.name}), write.required_capabilities, mode=PermissionMode.NORMAL),
    )
    assert read_decision.disposition is PolicyDisposition.ALLOW
    assert write_decision.disposition is PolicyDisposition.ASK
    assert write_decision.approval_binding is not None
    assert write_decision.approval_binding.args_hash == write_call.args_hash


@pytest.mark.asyncio
async def test_rule_argument_schema_is_exact_not_keyword_based() -> None:
    tool = definition("workspace.read")
    evaluator = RuleBasedPolicyEvaluator(
        (
            PolicyRule(
                "deny-value-7",
                RuleEffect.DENY,
                tool_names=frozenset({tool.name}),
                argument_schema={
                    "type": "object",
                    "properties": {"value": {"const": 7}},
                    "required": ["value"],
                },
            ),
        ),
        audit_sink=NullPolicyAuditSink(),
    )
    scope = context(frozenset({tool.name}), tool.required_capabilities, mode=PermissionMode.NORMAL)
    assert (await evaluator.evaluate(tool, call(tool, 7), scope)).disposition is PolicyDisposition.DENY
    assert (await evaluator.evaluate(tool, call(tool, 8), scope)).disposition is PolicyDisposition.ALLOW
