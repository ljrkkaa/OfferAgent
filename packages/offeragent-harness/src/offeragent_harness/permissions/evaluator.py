"""Fail-closed rule-based Policy Evaluator."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from itertools import count

from offeragent_harness.tools.definitions import ExecutorLocation, SideEffectClass, ToolCall, ToolDefinition

from .audit import PolicyAuditRecord, PolicyAuditSink
from .grants import ApprovalGrantReader
from .policy import ApprovalBinding, PolicyContext, PolicyDecision, PolicyDisposition
from .risk import PermissionMode, RiskClass
from .rules import PolicyRule, PolicyRuleSet, RuleEffect


def _security_metadata_complete(definition: ToolDefinition) -> bool:
    return (
        isinstance(definition.executor_location, ExecutorLocation)
        and isinstance(definition.risk, RiskClass)
        and isinstance(definition.side_effect_class, SideEffectClass)
        and isinstance(definition.required_capabilities, frozenset)
        and bool(definition.required_capabilities)
        and isinstance(definition.concurrency_safe, bool)
        and isinstance(definition.idempotent, bool)
        and isinstance(definition.retryable, bool)
        and isinstance(definition.timeout_ms, int)
        and definition.timeout_ms > 0
        and isinstance(definition.output_limit_bytes, int)
        and definition.output_limit_bytes > 0
    )


class RuleBasedPolicyEvaluator:
    """Applies non-overridable safety gates, then DENY > ALLOW > ASK."""

    def __init__(
        self,
        rules: PolicyRuleSet | Iterable[PolicyRule] = (),
        *,
        audit_sink: PolicyAuditSink,
        approval_ttl: timedelta = timedelta(minutes=5),
        grant_store: ApprovalGrantReader | None = None,
    ) -> None:
        if approval_ttl.total_seconds() <= 0:
            raise ValueError("approval_ttl must be positive")
        self._rules = rules if isinstance(rules, PolicyRuleSet) else PolicyRuleSet.from_iterable(rules)
        self._audit = audit_sink
        self._approval_ttl = approval_ttl
        self._grants = grant_store
        self._audit_sequence = count(1)

    async def evaluate(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
    ) -> PolicyDecision:
        matched = self._rules.matching(definition, call, context)
        hard_denial = self._hard_denial(definition, context)
        if hard_denial is not None:
            decision = self._decision(
                PolicyDisposition.DENY,
                definition,
                call,
                context,
                hard_denial[0],
                hard_denial[1],
                matched,
            )
        elif denied_rules := tuple(rule for rule in matched if rule.effect is RuleEffect.DENY):
            decision = self._decision(
                PolicyDisposition.DENY,
                definition,
                call,
                context,
                "rule_deny",
                "; ".join(rule.reason for rule in denied_rules),
                matched,
            )
        elif self._grants is not None and (grant := await self._grants.find_matching(call, context)) is not None:
            decision = self._decision(
                PolicyDisposition.ALLOW,
                definition,
                call,
                context,
                "approval_grant",
                "已存在与当前工具、参数、Workspace 和 Agent 身份完全匹配的有效授权。",
                matched,
                extra_facts={
                    "approvalGrantId": grant.grant_id,
                    "approvalId": grant.approval_id,
                    "approvalScope": grant.scope.value,
                    "includeDescendants": grant.include_descendants,
                },
            )
        else:
            decision = self._rules_or_default(definition, call, context, matched)
        await self._audit.record(
            PolicyAuditRecord(
                audit_id=(
                    f"audit:{call.tool_call_id}:{context.now.isoformat()}:"
                    f"{decision.disposition.value}:{decision.reason_code}:{next(self._audit_sequence):08d}"
                ),
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                run_id=context.run_id,
                root_run_id=call.lineage.root_run_id,
                tool_call_id=call.tool_call_id,
                tool_name=definition.name,
                tool_version=definition.version,
                args_hash=call.args_hash,
                disposition=decision.disposition,
                risk=definition.risk,
                reason_code=decision.reason_code,
                matched_rule_ids=tuple(rule.rule_id for rule in matched),
                evaluated_at=context.now,
                facts=decision.audit_facts,
            )
        )
        return decision

    def _hard_denial(self, definition: ToolDefinition, context: PolicyContext) -> tuple[str, str] | None:
        if not _security_metadata_complete(definition):
            return "missing_security_metadata", "工具安全元数据不完整, 已拒绝执行。"
        scope = context.effective_scope
        if definition.name in scope.denied_tools:
            return "scope_tool_denied", "当前 Agent 权限范围明确拒绝此工具。"
        if definition.name not in scope.allowed_tools:
            return "scope_tool_not_allowed", "当前 Agent 权限范围未包含此工具。"
        if definition.risk not in scope.allowed_risks:
            return "scope_risk_not_allowed", "当前 Agent 权限范围不允许此风险类别。"
        missing_capabilities = definition.required_capabilities - scope.root_capabilities
        if missing_capabilities:
            return "scope_capability_missing", "当前 Workspace 缺少工具所需 capability。"
        if definition.risk is RiskClass.NETWORK and not scope.allow_network:
            return "scope_network_denied", "当前权限范围不允许网络访问。"
        if definition.risk is RiskClass.SECRET_ACCESS and not scope.allow_secret_handles:
            return "scope_secret_denied", "当前权限范围不允许使用 Secret handle。"
        if context.permission_mode in {PermissionMode.READ_ONLY, PermissionMode.PLAN} and not self._safe_read(
            definition
        ):
            return "permission_mode_read_only", "当前模式只允许无副作用的安全读取。"
        if not context.workspace_trusted and (
            "process.execute" in definition.required_capabilities
            or not self._safe_read(definition)
            or definition.executor_location is ExecutorLocation.SUBAGENT
        ):
            return "workspace_untrusted", "未信任 Workspace 只允许本地安全读取。"
        return None

    def _rules_or_default(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
        matched: tuple[PolicyRule, ...],
    ) -> PolicyDecision:
        for effect, disposition in (
            (RuleEffect.DENY, PolicyDisposition.DENY),
            (RuleEffect.ALLOW, PolicyDisposition.ALLOW),
            (RuleEffect.ASK, PolicyDisposition.ASK),
        ):
            selected = tuple(rule for rule in matched if rule.effect is effect)
            if selected:
                reason = "; ".join(rule.reason for rule in selected)
                return self._decision(
                    disposition,
                    definition,
                    call,
                    context,
                    f"rule_{effect.value}",
                    reason,
                    matched,
                )

        if context.permission_mode is PermissionMode.BYPASS:
            return self._decision(
                PolicyDisposition.ALLOW,
                definition,
                call,
                context,
                "mode_bypass",
                "显式 Bypass 模式允许执行; 仍保留审计。",
                matched,
            )
        if self._safe_read(definition):
            return self._decision(
                PolicyDisposition.ALLOW,
                definition,
                call,
                context,
                "default_safe_read",
                "安全读取默认允许。",
                matched,
            )
        return self._decision(
            PolicyDisposition.ASK,
            definition,
            call,
            context,
            "default_approval_required",
            "此工具具有副作用或高风险, 需要用户审批。",
            matched,
        )

    def _decision(
        self,
        disposition: PolicyDisposition,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
        reason_code: str,
        message: str,
        matched: tuple[PolicyRule, ...],
        *,
        extra_facts: dict[str, object] | None = None,
    ) -> PolicyDecision:
        binding = None
        if disposition is PolicyDisposition.ASK:
            expected_hash = call.arguments.get("expectedHash")
            binding = ApprovalBinding(
                tool_name=definition.name,
                tool_version=definition.version,
                definition_fingerprint=definition.fingerprint,
                args_hash=call.args_hash,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                principal_id=context.principal_id,
                root_run_id=call.lineage.root_run_id,
                run_id=call.run_id,
                agent_name=call.lineage.agent_name,
                ancestor_run_ids=call.lineage.ancestor_run_ids,
                expected_state_hash=expected_hash if isinstance(expected_hash, str) else None,
                expires_at=context.now + self._approval_ttl,
            )
        facts: dict[str, object] = {
            "matchedRuleIds": [rule.rule_id for rule in matched],
            "auditTags": sorted({tag for rule in matched for tag in rule.audit_tags}),
            "permissionMode": context.permission_mode.value,
            "workspaceTrusted": context.workspace_trusted,
            "principalId": context.principal_id,
            "registrySafetyFieldsPresent": _security_metadata_complete(definition),
        }
        if extra_facts:
            facts.update(extra_facts)
        return PolicyDecision(
            disposition=disposition,
            risk=definition.risk,
            reason_code=reason_code,
            user_message=message,
            audit_facts=facts,
            approval_binding=binding,
        )

    @staticmethod
    def _safe_read(definition: ToolDefinition) -> bool:
        return definition.risk is RiskClass.READ and definition.side_effect_class in {
            SideEffectClass.NONE,
            SideEffectClass.READ,
        }


__all__ = ["RuleBasedPolicyEvaluator"]
