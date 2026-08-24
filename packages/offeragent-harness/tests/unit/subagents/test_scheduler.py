from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from offeragent_harness.permissions import (
    CapabilityScope,
    PermissionMode,
    PolicyContext,
    PolicyDecision,
    PolicyDisposition,
    RiskClass,
)
from offeragent_harness.permissions.audit import NullPolicyAuditSink
from offeragent_harness.ports.subagents import ParentRunAuthority
from offeragent_harness.subagents import (
    AgentBudget,
    AgentUsage,
    ChildRunScheduler,
    EffectiveToolScope,
    ExecutionPriority,
    SubagentLifetime,
    SubagentResult,
    SubagentRunRecord,
    SubagentRunStatus,
    SubagentScopePolicy,
    builtin_agent_definitions,
    subagent_tool_definitions,
)
from offeragent_harness.tools import ToolCall, canonical_json_sha256

NOW = datetime(2026, 7, 13, 8, tzinfo=timezone.utc)


class _Source:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: Any = None
        self.closed = False

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> Any:
        return self._reason

    async def cancel(self, reason: Any) -> bool:
        if self.cancelled:
            return False
        self._reason = reason
        self._event.set()
        return True

    async def wait(self) -> Any:
        await self._event.wait()
        return self._reason

    def checkpoint(self) -> None:
        return

    async def close(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self) -> None:
        self.sources: dict[str, _Source] = {}

    def create(self, *, root_run_id: str, parent_run_id: str, child_run_id: str) -> _Source:
        del root_run_id, parent_run_id
        source = _Source()
        self.sources[child_run_id] = source
        return source


def _record(run_id: str, root_run_id: str, priority: ExecutionPriority) -> SubagentRunRecord:
    task = f"task for {run_id}"
    tools = subagent_tool_definitions()
    scope = CapabilityScope(
        frozenset(item.name for item in tools),
        frozenset(),
        frozenset({RiskClass.READ}),
        frozenset(capability for item in tools for capability in item.required_capabilities),
        False,
        False,
    )
    schema = builtin_agent_definitions(
        available_tools=scope.allowed_tools,
        root_capabilities=scope.root_capabilities,
    )[-1].result_schema
    return SubagentRunRecord(
        run_id,
        root_run_id,
        root_run_id,
        (root_run_id,),
        "ses_test",
        "turn_test",
        "ws_test",
        f"trace_{run_id.removeprefix('run_')}",
        f"call_{run_id.removeprefix('run_')}",
        "general",
        "1.0.0",
        task,
        "sha256:" + hashlib.sha256(task.encode()).hexdigest(),
        1,
        SubagentLifetime.PARENT,
        f"ctx_{run_id.removeprefix('run_')}",
        PermissionMode.READ_ONLY,
        scope,
        EffectiveToolScope(
            {item.name: (item.version,) for item in tools if item.risk is RiskClass.READ},
            {},
            "sha256:" + "1" * 64,
        ),
        AgentBudget(1_000, 1_000, 2, 2, 60, 8_192, 0),
        AgentUsage(),
        NOW + timedelta(minutes=1),
        schema,
        SubagentRunStatus.QUEUED,
        "queued",
        priority,
        NOW,
        NOW,
    )


def _result(run_id: str) -> SubagentResult:
    return SubagentResult(run_id, "completed", "done", (), (), (), (), (), {}, None)


@pytest.mark.asyncio
async def test_scheduler_is_root_fair_and_priority_never_bypasses_root_quota() -> None:
    factory = _Factory()
    scheduler = ChildRunScheduler(factory, max_workspace_active=2, max_root_active=1)
    started_order: list[str] = []
    gates = {run_id: asyncio.Event() for run_id in ("run_a1", "run_a2", "run_a3", "run_b1")}
    finished: list[tuple[str, bool]] = []

    async def started(run_id: str) -> None:
        started_order.append(run_id)

    async def run(record: SubagentRunRecord, cancellation: Any) -> SubagentResult:
        del cancellation
        await gates[record.run_id].wait()
        return _result(record.run_id)

    async def ended(run_id: str, result: Any, error: BaseException | None) -> None:
        finished.append((run_id, result is not None and error is None))

    await scheduler.submit(
        _record("run_a1", "run_root_a", ExecutionPriority.LOW), run=run, started=started, finished=ended
    )
    await scheduler.submit(
        _record("run_a2", "run_root_a", ExecutionPriority.LOW), run=run, started=started, finished=ended
    )
    await scheduler.submit(
        _record("run_a3", "run_root_a", ExecutionPriority.HIGH), run=run, started=started, finished=ended
    )
    await scheduler.submit(
        _record("run_b1", "run_root_b", ExecutionPriority.LOW), run=run, started=started, finished=ended
    )
    await asyncio.sleep(0)
    assert started_order == ["run_a1", "run_b1"]
    gates["run_a1"].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started_order[:3] == ["run_a1", "run_b1", "run_a3"]
    gates["run_b1"].set()
    gates["run_a3"].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gates["run_a2"].set()
    for _ in range(10):
        if len(finished) == 4:
            break
        await asyncio.sleep(0)
    assert len(finished) == 4 and all(ok for _, ok in finished)
    assert all(source.closed for source in factory.sources.values())
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_scheduler_child_failure_does_not_cancel_sibling() -> None:
    scheduler = ChildRunScheduler(_Factory(), max_workspace_active=2, max_root_active=2)
    results: dict[str, tuple[Any, BaseException | None]] = {}

    async def run(record: SubagentRunRecord, cancellation: Any) -> SubagentResult:
        del cancellation
        if record.run_id == "run_bad":
            raise ValueError("isolated")
        return _result(record.run_id)

    async def started(run_id: str) -> None:
        del run_id

    async def ended(run_id: str, result: Any, error: BaseException | None) -> None:
        results[run_id] = (result, error)

    await scheduler.submit(
        _record("run_bad", "run_root", ExecutionPriority.NORMAL), run=run, started=started, finished=ended
    )
    await scheduler.submit(
        _record("run_good", "run_root", ExecutionPriority.NORMAL), run=run, started=started, finished=ended
    )
    for _ in range(10):
        if len(results) == 2:
            break
        await asyncio.sleep(0)
    assert isinstance(results["run_bad"][1], ValueError)
    assert results["run_good"][0].status == "completed"
    await scheduler.shutdown()


class _ParentProvider:
    def __init__(self, authority: ParentRunAuthority) -> None:
        self.authority = authority

    async def authority_for(self, run_id: str) -> ParentRunAuthority:
        assert run_id == self.authority.lineage.run_id
        return self.authority


class _AllowPolicy:
    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, definition: Any, call: Any, context: Any) -> PolicyDecision:
        del call, context
        self.calls += 1
        return PolicyDecision(PolicyDisposition.ALLOW, definition.risk, "allowed", "allowed")


@pytest.mark.asyncio
async def test_scope_policy_rechecks_argument_constraint_and_live_parent_revocation() -> None:
    record = _record("run_child", "run_root", ExecutionPriority.NORMAL)
    definition = next(item for item in subagent_tool_definitions() if item.name == "agent.status")
    constrained = replace(
        record,
        tool_scope=EffectiveToolScope(
            {"agent.status": ("1",)},
            {
                "agent.status": {
                    "type": "object",
                    "properties": {"runId": {"const": "run_allowed"}},
                    "required": ["runId"],
                    "additionalProperties": False,
                }
            },
            record.tool_scope.registry_snapshot_hash,
        ),
    )
    parent_scope = replace(record.effective_scope, allowed_tools=frozenset({"agent.status"}))
    parent = ParentRunAuthority(
        "ws_test",
        "ses_test",
        "turn_test",
        constrained.lineage.__class__.root("run_root"),
        PermissionMode.READ_ONLY,
        parent_scope,
        (definition,),
        constrained.tool_scope.registry_snapshot_hash,
        constrained.budget_limit,
        NOW + timedelta(minutes=1),
        {},
        {},
        True,
        True,
    )
    provider = _ParentProvider(parent)
    downstream = _AllowPolicy()
    policy = SubagentScopePolicy(
        constrained,
        provider,
        downstream,
        audit_sink=NullPolicyAuditSink(),
    )
    context = PolicyContext(
        "ws_test",
        "ses_test",
        "principal_test",
        constrained.run_id,
        constrained.permission_mode,
        constrained.effective_scope,
        True,
        NOW,
    )

    def call(arguments: dict[str, Any]) -> ToolCall:
        return ToolCall(
            "call_status",
            constrained.run_id,
            "ws_test",
            definition.name,
            definition.version,
            arguments,
            canonical_json_sha256(arguments),
            "idem_status",
            NOW + timedelta(seconds=30),
            constrained.lineage,
            definition.fingerprint,
            definition.result_sensitivity,
        )

    denied_args = await policy.evaluate(definition, call({"runId": "run_denied"}), context)
    assert denied_args.reason_code == "subagent_argument_scope"
    assert downstream.calls == 0
    allowed = await policy.evaluate(definition, call({"runId": "run_allowed"}), context)
    assert allowed.disposition is PolicyDisposition.ALLOW and downstream.calls == 1
    provider.authority = replace(parent, effective_scope=replace(parent_scope, allowed_tools=frozenset()))
    revoked = await policy.evaluate(definition, call({"runId": "run_allowed"}), context)
    assert revoked.reason_code == "subagent_parent_scope_revoked"
    assert downstream.calls == 1
