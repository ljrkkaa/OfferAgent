"""Hook gates around child AgentRun lifecycle in the single Worker runtime."""

from __future__ import annotations

import hashlib

from offeragent_harness.error_codes import PolicyDeniedCause
from offeragent_harness.hooks import HookDecision, HookEvent, HookExecutionContext, HookInvocation
from offeragent_harness.ports import CancellationToken, HookLifecyclePort

from .models import SubagentResult, SubagentSpawnRequest


class SubagentHookDenied(RuntimeError, PolicyDeniedCause):
    def __init__(self, event: HookEvent, decision: HookDecision) -> None:
        self.event = event
        self.decision = decision
        super().__init__(f"{event.value} Hook returned {decision.value}")


class SubagentLifecycleHooks:
    """A gate to be called by the Worker child-run scheduler, not a new loop."""

    def __init__(self, hooks: HookLifecyclePort) -> None:
        self._hooks = hooks

    async def before_start(
        self,
        request: SubagentSpawnRequest,
        context: HookExecutionContext,
        cancellation: CancellationToken,
    ) -> None:
        outcome = await self._hooks.invoke(
            HookInvocation(
                invocation_id=f"subagent-start:{request.spawn_call_id}:{request.child_run_id}",
                chain_id=f"agent:{request.parent_lineage.root_run_id}",
                event=HookEvent.SUBAGENT_START,
                context=context,
                run_id=request.parent_lineage.run_id,
                facts={
                    "spawnCallId": request.spawn_call_id,
                    "parentRunId": request.parent_lineage.run_id,
                    "childRunId": request.child_run_id,
                    "profile": request.profile,
                    "childDepth": request.parent_lineage.depth + 1,
                    "taskHash": _text_hash(request.task),
                    "lifetime": request.lifetime.value,
                    "budget": {
                        "modelCalls": request.budget.model_calls,
                        "toolCalls": request.budget.tool_calls,
                        "wallTimeSeconds": request.budget.wall_time_seconds,
                        "artifactBytes": request.budget.artifact_bytes,
                    },
                },
            ),
            cancellation,
        )
        if outcome.decision is not HookDecision.CONTINUE:
            raise SubagentHookDenied(HookEvent.SUBAGENT_START, outcome.decision)

    async def after_stop(
        self,
        result: SubagentResult,
        *,
        parent_run_id: str,
        root_run_id: str,
        context: HookExecutionContext,
        cancellation: CancellationToken,
    ) -> None:
        outcome = await self._hooks.invoke(
            HookInvocation(
                invocation_id=f"subagent-stop:{result.run_id}:{result.status}",
                chain_id=f"agent:{root_run_id}",
                event=HookEvent.SUBAGENT_STOP,
                context=context,
                run_id=parent_run_id,
                facts={
                    "parentRunId": parent_run_id,
                    "childRunId": result.run_id,
                    "status": result.status,
                    "summaryHash": _text_hash(result.summary),
                    "artifactIds": list(result.artifact_ids),
                    "hasError": result.error is not None,
                },
            ),
            cancellation,
        )
        if outcome.decision is HookDecision.DENY:
            # The child has already stopped.  A Hook cannot rewrite its durable
            # result, but the denial remains visible in Hook audit.
            return


def _text_hash(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


__all__ = ["SubagentHookDenied", "SubagentLifecycleHooks"]
