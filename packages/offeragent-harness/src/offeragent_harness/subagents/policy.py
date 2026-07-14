"""Per-call child scope guard layered in front of the normal PolicyEvaluator."""

from __future__ import annotations

from itertools import count

from jsonschema import Draft202012Validator

from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.permissions import PermissionMode, PolicyContext, PolicyDecision, PolicyDisposition, RiskClass
from offeragent_harness.permissions.audit import PolicyAuditRecord, PolicyAuditSink
from offeragent_harness.ports import PolicyEvaluator
from offeragent_harness.ports.subagents import ParentRunAuthorityProvider
from offeragent_harness.tools import SideEffectClass, ToolCall, ToolDefinition

from .models import SubagentRunRecord


class SubagentScopePolicy:
    """Rechecks immutable child scope and live parent revocation on every call."""

    def __init__(
        self,
        record: SubagentRunRecord,
        parent_authorities: ParentRunAuthorityProvider,
        downstream: PolicyEvaluator,
        *,
        audit_sink: PolicyAuditSink,
    ) -> None:
        self._record = record
        self._parents = parent_authorities
        self._downstream = downstream
        self._audit = audit_sink
        self._audit_sequence = count(1)

    async def evaluate(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
    ) -> PolicyDecision:
        if (
            context.workspace_id != self._record.workspace_id
            or context.session_id != self._record.session_id
            or context.run_id != self._record.run_id
            or context.permission_mode != self._record.permission_mode
            or self._record.permission_mode is PermissionMode.BYPASS
        ):
            return await self._deny(
                definition,
                call,
                context,
                "subagent_context_mismatch",
                "Policy context or permission does not match the child Run",
            )
        if call.run_id != self._record.run_id or call.lineage != self._record.lineage:
            return await self._deny(
                definition,
                call,
                context,
                "subagent_lineage_mismatch",
                "ToolCall lineage does not match the child Run",
            )
        versions = self._record.tool_scope.allowed_versions.get(definition.name)
        if versions is None or definition.version not in versions:
            return await self._deny(
                definition,
                call,
                context,
                "subagent_tool_scope",
                "Tool name/version is outside the child Tool scope",
            )
        constraint = self._record.tool_scope.argument_constraints.get(definition.name)
        if constraint is not None and not Draft202012Validator(thaw_json(constraint)).is_valid(
            thaw_json(call.arguments)
        ):
            return await self._deny(
                definition,
                call,
                context,
                "subagent_argument_scope",
                "Tool arguments exceed the child constraint",
            )
        try:
            parent = await self._parents.authority_for(self._record.parent_run_id)
        except Exception:
            return await self._deny(
                definition,
                call,
                context,
                "subagent_parent_scope_unavailable",
                "Parent authority could not be revalidated for this ToolCall",
            )
        live_definition = next(
            (
                item
                for item in parent.tool_definitions
                if item.name == definition.name
                and item.version == definition.version
                and item.fingerprint == definition.fingerprint
            ),
            None,
        )
        parent_scope = parent.effective_scope
        if (
            not parent.active
            or parent.workspace_id != self._record.workspace_id
            or parent.session_id != self._record.session_id
            or parent.turn_id != self._record.turn_id
            or parent.lineage.run_id != self._record.parent_run_id
            or parent.registry_snapshot_hash != self._record.tool_scope.registry_snapshot_hash
            or context.now >= parent.deadline_at
            or live_definition is None
            or definition.name not in parent_scope.allowed_tools
            or definition.name in parent_scope.denied_tools
            or definition.risk not in parent_scope.allowed_risks
            or not definition.required_capabilities <= parent_scope.root_capabilities
            or (definition.risk is RiskClass.NETWORK and not parent_scope.allow_network)
            or (definition.risk is RiskClass.SECRET_ACCESS and not parent_scope.allow_secret_handles)
            or (
                parent.permission_mode in {PermissionMode.READ_ONLY, PermissionMode.PLAN} and not _safe_read(definition)
            )
        ):
            return await self._deny(
                definition,
                call,
                context,
                "subagent_parent_scope_revoked",
                "Parent authority was revoked or narrowed after child spawn",
            )
        return await self._downstream.evaluate(definition, call, context)

    async def _deny(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
        code: str,
        message: str,
    ) -> PolicyDecision:
        decision = PolicyDecision(
            PolicyDisposition.DENY,
            definition.risk,
            code,
            message,
            {"subagentScopeGuard": True},
        )
        await self._audit.record(
            PolicyAuditRecord(
                audit_id=(
                    f"audit:{call.tool_call_id}:{context.now.isoformat()}:deny:{code}:"
                    f"subagent:{next(self._audit_sequence):08d}"
                ),
                workspace_id=self._record.workspace_id,
                session_id=self._record.session_id,
                run_id=self._record.run_id,
                root_run_id=self._record.root_run_id,
                tool_call_id=call.tool_call_id,
                tool_name=definition.name,
                tool_version=definition.version,
                args_hash=call.args_hash,
                disposition=decision.disposition,
                risk=definition.risk,
                reason_code=decision.reason_code,
                matched_rule_ids=(),
                evaluated_at=context.now,
                facts={
                    **decision.audit_facts,
                    "contextIdentityMatched": (
                        context.workspace_id == self._record.workspace_id
                        and context.session_id == self._record.session_id
                        and context.run_id == self._record.run_id
                    ),
                },
            )
        )
        return decision


def _safe_read(definition: ToolDefinition) -> bool:
    return definition.risk is RiskClass.READ and definition.side_effect_class in {
        SideEffectClass.NONE,
        SideEffectClass.READ,
    }


__all__ = ["SubagentScopePolicy"]
