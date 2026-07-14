"""Authority projection for root and nested parent Runs from durable state."""

from __future__ import annotations

from offeragent_harness.permissions import PermissionMode
from offeragent_harness.ports import UnitOfWorkFactory
from offeragent_harness.ports.subagents import ParentRunAuthority, ParentRunAuthorityProvider
from offeragent_harness.sessions import Run

from .catalog import AgentDefinitionCatalog
from .models import AgentBudget, AgentUsage, SubagentRunStatus
from .serialization import context_from_value, run_record_from_value


class CompositeParentRunAuthorityProvider:
    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        catalog: AgentDefinitionCatalog,
        roots: ParentRunAuthorityProvider,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._catalog = catalog
        self._roots = roots

    async def authority_for(self, run_id: str) -> ParentRunAuthority:
        async with self._unit_of_work.begin() as uow:
            raw = await uow.entities.get("subagent_runs", run_id)
        if raw is None:
            return await self._roots.authority_for(run_id)
        record = run_record_from_value(raw)
        async with self._unit_of_work.begin() as uow:
            context_raw = await uow.entities.get("subagent_contexts", record.context_snapshot_id)
            run = await uow.entities.get("runs", run_id)
        if context_raw is None or not isinstance(run, Run):
            raise ValueError("nested parent Subagent state is missing or corrupt")
        context = context_from_value(context_raw)
        root = await self._roots.authority_for(record.root_run_id)
        effective_scope = record.effective_scope.intersect(root.effective_scope)
        allowed = record.tool_scope.allowed_versions
        definitions = tuple(
            definition
            for definition in root.tool_definitions
            if definition.name in allowed and definition.version in allowed[definition.name]
        )
        return ParentRunAuthority(
            workspace_id=record.workspace_id,
            session_id=record.session_id,
            turn_id=record.turn_id,
            lineage=record.lineage,
            permission_mode=_narrow_permission(record.permission_mode, root.permission_mode),
            effective_scope=effective_scope,
            tool_definitions=definitions,
            registry_snapshot_hash=record.tool_scope.registry_snapshot_hash,
            remaining_budget=_remaining(record.budget_limit, record.budget_used),
            deadline_at=min(record.deadline_at, root.deadline_at),
            context=context.content,
            run_config=run.config_snapshot,
            # Child Runs are isolated workers, never coordinators.  Recursive
            # delegation therefore has no configuration or recovery path.
            can_spawn_children=False,
            active=root.active
            and record.status
            in {
                SubagentRunStatus.QUEUED,
                SubagentRunStatus.STARTING,
                SubagentRunStatus.RUNNING,
                SubagentRunStatus.WAITING_TOOL,
                SubagentRunStatus.WAITING_APPROVAL,
                SubagentRunStatus.WAITING_CHILDREN,
            },
        )


def _remaining(limit: AgentBudget, used: AgentUsage) -> AgentBudget:
    remaining = tuple(maximum - value for maximum, value in zip(limit.as_tuple(), used.as_tuple(), strict=True))
    return AgentBudget(
        int(remaining[0]),
        int(remaining[1]),
        int(remaining[2]),
        int(remaining[3]),
        float(remaining[4]),
        int(remaining[5]),
        int(remaining[6]),
        int(remaining[7]),
    )


def _narrow_permission(child: PermissionMode, root: PermissionMode) -> PermissionMode:
    modes = {child, root}
    if PermissionMode.PLAN in modes:
        return PermissionMode.PLAN
    if PermissionMode.READ_ONLY in modes:
        return PermissionMode.READ_ONLY
    if PermissionMode.BYPASS in modes:
        return PermissionMode.NORMAL
    if modes == {PermissionMode.TRUSTED_WORKSPACE}:
        return PermissionMode.TRUSTED_WORKSPACE
    return PermissionMode.NORMAL


__all__ = ["CompositeParentRunAuthorityProvider"]
