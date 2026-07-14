"""Harness-owned child AgentRuns; exports are lazy to keep Ports acyclic."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AgentBudget": "models",
    "AgentCancelCommand": "models",
    "AgentDefinition": "definitions",
    "AgentDefinitionCatalog": "catalog",
    "AgentDefinitionDescriptor": "catalog",
    "AgentDefinitionError": "catalog",
    "AgentDefinitionLayer": "catalog",
    "AgentDefinitionRoot": "catalog",
    "AgentDefinitionTrust": "catalog",
    "AgentSendCommand": "models",
    "AgentSpawnCommand": "models",
    "AgentUsage": "models",
    "AgentWaitCommand": "models",
    "ChildRunScheduler": "scheduler",
    "CompositeParentRunAuthorityProvider": "authority",
    "ContextForkError": "context",
    "ContextForkMode": "models",
    "ContextForker": "context",
    "ContextSnapshot": "models",
    "DurableMailbox": "mailbox",
    "EffectiveToolScope": "models",
    "ExecutionPriority": "models",
    "MailboxMessage": "mailbox",
    "MailboxMode": "models",
    "MailboxReceipt": "models",
    "ModelPolicy": "definitions",
    "ResultReducer": "reducer",
    "RunRecoverySupervisor": "recovery",
    "ScopeDeriver": "context",
    "SubagentBudgetTree": "budget",
    "SubagentCancelReceipt": "models",
    "SubagentHandle": "models",
    "SubagentHookDenied": "lifecycle",
    "SubagentLifecycleHooks": "lifecycle",
    "SubagentLifetime": "models",
    "SubagentResult": "models",
    "SubagentResultArtifactManager": "artifacts",
    "SubagentRunRecord": "models",
    "SubagentRunStatus": "models",
    "SubagentScopePolicy": "policy",
    "SubagentService": "service",
    "SubagentServiceError": "service",
    "SubagentSpawnRequest": "models",
    "SubagentStatusSnapshot": "models",
    "SubagentToolExecutor": "tools",
    "SubagentWaitResult": "models",
    "WaitMode": "models",
    "WriteClaim": "write_coordinator",
    "WriteCoordinator": "write_coordinator",
    "WriteLease": "write_coordinator",
    "builtin_agent_definitions": "definitions",
    "subagent_tool_definitions": "tools",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *_EXPORTS))


__all__ = sorted(_EXPORTS)
