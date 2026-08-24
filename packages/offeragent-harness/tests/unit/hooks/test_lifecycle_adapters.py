from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from offeragent_harness.hooks import HookEvent, HookExecutionContext, HookInvocation, HookOutcome
from offeragent_harness.permissions import CapabilityScope, RiskClass
from offeragent_harness.ports import CancellationToken
from offeragent_harness.runtime.hook_lifecycle import WorkerLifecycleHooks
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.subagents import (
    AgentBudget,
    ContextForkMode,
    SubagentLifecycleHooks,
    SubagentLifetime,
    SubagentResult,
    SubagentSpawnRequest,
)
from offeragent_harness.testing import ManualCancellationToken


class RecordingHooks:
    def __init__(self) -> None:
        self.invocations: list[HookInvocation] = []

    async def invoke(self, invocation: HookInvocation, cancellation: CancellationToken) -> HookOutcome:
        cancellation.checkpoint()
        self.invocations.append(invocation)
        return HookOutcome.continue_without_hooks()


def _context() -> HookExecutionContext:
    return HookExecutionContext("system", "profile-1", "workspace-1", "session-1", True)


@pytest.mark.asyncio
async def test_worker_lifecycle_uses_same_port_for_session_start_and_shutdown() -> None:
    hooks = RecordingHooks()
    lifecycle = WorkerLifecycleHooks(hooks, _context())

    await lifecycle.session_start(connection_id="pipe-1", cancellation=ManualCancellationToken())
    await lifecycle.runtime_shutdown(
        shutdown_id="shutdown-1",
        reason_code="upgrade",
        cancellation=ManualCancellationToken(),
    )

    assert [item.event for item in hooks.invocations] == [HookEvent.SESSION_START, HookEvent.RUNTIME_SHUTDOWN]


@pytest.mark.asyncio
async def test_subagent_start_stop_hooks_never_create_another_runtime() -> None:
    hooks = RecordingHooks()
    lifecycle = SubagentLifecycleHooks(hooks)
    parent = AgentLineage.root("run-root")
    request = SubagentSpawnRequest(
        spawn_call_id="spawn-1",
        parent_lineage=parent,
        child_run_id="run-child",
        task="analyze bounded evidence",
        profile="researcher",
        context_mode=ContextForkMode.SUMMARY,
        selected_message_ids=(),
        selected_artifact_ids=(),
        requested_scope=CapabilityScope(
            allowed_tools=frozenset({"workspace.read"}),
            denied_tools=frozenset(),
            allowed_risks=frozenset({RiskClass.READ}),
            root_capabilities=frozenset({"workspace.read"}),
            allow_network=False,
            allow_secret_handles=False,
        ),
        budget=AgentBudget(100, 100, 2, 4, 30, 1_024, 0),
        lifetime=SubagentLifetime.TURN,
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    result = SubagentResult(
        run_id="run-child",
        status="completed",
        summary="done",
        findings=(),
        evidence=(),
        artifact_ids=(),
        proposed_actions=(),
        unresolved_questions=(),
        usage={"modelCalls": 1},
    )

    await lifecycle.before_start(request, _context(), ManualCancellationToken())
    await lifecycle.after_stop(
        result,
        parent_run_id="run-root",
        root_run_id="run-root",
        context=_context(),
        cancellation=ManualCancellationToken(),
    )

    assert [item.event for item in hooks.invocations] == [HookEvent.SUBAGENT_START, HookEvent.SUBAGENT_STOP]
    assert all("analyze bounded evidence" not in repr(item.facts) for item in hooks.invocations)
