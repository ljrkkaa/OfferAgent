from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteInvocationJournal, SqliteUnitOfWorkFactory
from offeragent_harness.agent import BudgetLedger, RunBudget
from offeragent_harness.agent.loop import ToolExecution, run_agent_loop
from offeragent_harness.agent.planner import PlanningAttempt, PlanningAttemptOutcome, PlanningStep
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.config import HarnessConfig
from offeragent_harness.hooks import (
    HookDecision,
    HookDefinition,
    HookEvent,
    HookImplementation,
    HookInvocation,
    HookLayer,
    HookOutput,
    HookScope,
)
from offeragent_harness.hooks.state import hook_layer_hash
from offeragent_harness.models import ModelUsage
from offeragent_harness.permissions import (
    ApprovalDecisionReceipt,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalScope,
    ApprovalState,
    CapabilityScope,
    PermissionMode,
    PolicyContext,
    RiskClass,
)
from offeragent_harness.permissions.audit import NullPolicyAuditSink
from offeragent_harness.permissions.evaluator import RuleBasedPolicyEvaluator
from offeragent_harness.ports import (
    ApprovalObserver,
    CancellationToken,
    EntityRevisionConflict,
    SupervisedProcessRequest,
    SupervisedProcessResult,
    ToolLifecycleObserver,
    UnitOfWorkFactory,
)
from offeragent_harness.runtime import CancellationScope
from offeragent_harness.runtime.production_hooks import (
    PreparedHookBundle,
    ProductionHookBundleError,
    ProductionHookBundleFactory,
)
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
    RecordingEventSink,
)
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffectClass,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
    ToolValidator,
    canonical_json_sha256,
)
from offeragent_harness.tools.dispatcher import ToolDispatcher
from offeragent_harness.tools.kernel import UnifiedToolKernel
from offeragent_harness.tools.registry import ToolRegistry
from offeragent_harness.tools.scheduler import ToolScheduler

NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


class NeverProcess:
    def __init__(self) -> None:
        self.requests: list[SupervisedProcessRequest] = []

    async def execute(
        self,
        request: SupervisedProcessRequest,
        cancellation: CancellationToken,
    ) -> SupervisedProcessResult:
        cancellation.checkpoint()
        self.requests.append(request)
        raise AssertionError("builtin-only production Hook tests must not spawn a process")


@dataclass
class RecordingHandler:
    output: HookOutput = field(default_factory=HookOutput)

    def __post_init__(self) -> None:
        self.invocations: list[HookInvocation] = []

    async def invoke(
        self,
        definition: HookDefinition,
        invocation: HookInvocation,
        cancellation: CancellationToken,
    ) -> HookOutput:
        del definition
        cancellation.checkpoint()
        self.invocations.append(invocation)
        return self.output


class MutationApprovalHandler(RecordingHandler):
    async def invoke(
        self,
        definition: HookDefinition,
        invocation: HookInvocation,
        cancellation: CancellationToken,
    ) -> HookOutput:
        del definition
        cancellation.checkpoint()
        self.invocations.append(invocation)
        if invocation.event is HookEvent.PRE_TOOL_USE:
            return HookOutput(HookDecision.ASK, argument_patch={"value": 3})
        return HookOutput()


def _definition(
    hook_id: str,
    scope: HookScope,
    owner_id: str,
    event: HookEvent,
) -> HookDefinition:
    return HookDefinition(
        hook_id,
        scope,
        owner_id,
        event,
        HookImplementation.BUILTIN,
        handler_id="builtin.events",
    )


def _managed(*events: HookEvent) -> HookLayer:
    return HookLayer(
        HookScope.MANAGED,
        "system",
        1,
        tuple(_definition(f"managed-{event.value}", HookScope.MANAGED, "system", event) for event in events),
    )


def _config(*, enabled: bool, trusted: bool = True) -> HarnessConfig:
    return HarnessConfig.model_validate(
        {
            "extensibility": {"hooks_enabled": enabled},
            "policy": {"workspace_trusted": trusted},
        }
    )


def _factory(
    unit_of_work: UnitOfWorkFactory,
    handler: RecordingHandler,
    *,
    managed: HookLayer | None = None,
    process: NeverProcess | None = None,
) -> ProductionHookBundleFactory:
    return ProductionHookBundleFactory(
        workspace_id="workspace-1",
        managed_layer=managed or _managed(),
        builtin_handlers={"builtin.events": handler},
        unit_of_work=unit_of_work,
        event_sink=RecordingEventSink(),
        processes=process or NeverProcess(),
        clock=ManualClock(NOW),
        ids=DeterministicIdGenerator(),
        environment={"LANG": "zh_CN.UTF-8", "TOKEN": "must-not-be-forwarded"},
    )


async def _prepare(
    factory: ProductionHookBundleFactory,
    *,
    enabled: bool = True,
    trusted: bool = True,
    durable_snapshot: dict[str, Any] | None = None,
) -> PreparedHookBundle:
    return await factory.prepare(
        run_id="run-1",
        principal_id="principal-1",
        session_id="session-1",
        effective_config=_config(enabled=enabled, trusted=trusted),
        cancellation=ManualCancellationToken(),
        durable_snapshot=durable_snapshot,
    )


def _turn_invocation(bundle: Any, invocation_id: str = "turn-start:run-1") -> HookInvocation:
    assert bundle.context is not None
    return HookInvocation(
        invocation_id,
        "agent:run-1",
        HookEvent.TURN_START,
        bundle.context,
        "run-1",
        {"purpose": "integration"},
    )


@pytest.mark.asyncio
async def test_default_disabled_has_zero_state_initialization_and_zero_execution() -> None:
    durable = InMemoryUnitOfWorkFactory()
    handler = RecordingHandler()
    process = NeverProcess()
    factory = _factory(durable, handler, managed=_managed(HookEvent.TURN_START), process=process)

    prepared = await _prepare(factory, enabled=False)
    bundle = factory.build_prepared(prepared)

    assert not factory.configuration.initialized
    assert prepared.layers == ()
    assert prepared.evidence == ()
    assert bundle.hooks is None
    assert bundle.context_factory is None
    assert bundle.context is None
    assert bundle.worker_lifecycle is None
    assert bundle.compaction is None
    assert bundle.subagent_lifecycle is None
    assert handler.invocations == [] and process.requests == []


@pytest.mark.asyncio
async def test_workspace_builtin_is_projected_only_for_a_trusted_workspace() -> None:
    durable = InMemoryUnitOfWorkFactory()
    handler = RecordingHandler()
    factory = _factory(durable, handler)
    token = ManualCancellationToken()
    await _prepare(factory, trusted=False)
    workspace_layer = HookLayer(
        HookScope.WORKSPACE,
        "workspace-1",
        1,
        (_definition("workspace-turn", HookScope.WORKSPACE, "workspace-1", HookEvent.TURN_START),),
    )
    await factory.configuration.install_layer(
        workspace_layer,
        expected_revision=0,
        idempotency_key="install-workspace-turn",
        cancellation=token,
    )

    untrusted = factory.build_prepared(await _prepare(factory, trusted=False))
    assert untrusted.hooks is not None
    outcome = await untrusted.hooks.invoke(_turn_invocation(untrusted), token)
    assert outcome.applied_hook_ids == () and handler.invocations == []

    trusted = factory.build_prepared(await _prepare(factory, trusted=True))
    assert trusted.hooks is not None
    outcome = await trusted.hooks.invoke(_turn_invocation(trusted, "turn-start:trusted"), token)
    assert outcome.applied_hook_ids == ("workspace-turn",)
    assert len(handler.invocations) == 1


@pytest.mark.asyncio
async def test_sqlite_persists_confirmation_and_two_writers_keep_cas(tmp_path: Path) -> None:
    database = tmp_path / "hooks.sqlite"
    handler = RecordingHandler()
    first = _factory(SqliteUnitOfWorkFactory(database), handler)
    token = ManualCancellationToken()
    await _prepare(first)
    layer = HookLayer(
        HookScope.USER,
        "principal-1",
        1,
        (_definition("user-turn", HookScope.USER, "principal-1", HookEvent.TURN_START),),
    )
    installed = await first.configuration.install_layer(
        layer,
        expected_revision=0,
        idempotency_key="install-user",
        cancellation=token,
    )
    await first.configuration.confirm_layer(
        HookScope.USER,
        "principal-1",
        hook_layer_hash(layer),
        expected_revision=installed.revision,
        idempotency_key="confirm-user",
        cancellation=token,
    )

    reopened = _factory(SqliteUnitOfWorkFactory(database), handler)
    prepared = await _prepare(reopened)
    assert [item.scope for item in prepared.layers] == [HookScope.MANAGED, HookScope.USER]

    race_path = tmp_path / "hooks-race.sqlite"
    left = _factory(SqliteUnitOfWorkFactory(race_path), RecordingHandler())
    right = _factory(SqliteUnitOfWorkFactory(race_path), RecordingHandler())
    await asyncio.gather(_prepare(left), _prepare(right))
    results = await asyncio.gather(
        left.configuration.install_layer(
            layer,
            expected_revision=0,
            idempotency_key="left-writer",
            cancellation=token,
        ),
        right.configuration.install_layer(
            layer,
            expected_revision=0,
            idempotency_key="right-writer",
            cancellation=token,
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    assert sum(isinstance(item, EntityRevisionConflict) for item in results) == 1


@pytest.mark.asyncio
async def test_prepare_recovery_and_build_fail_closed_on_schema_or_trust_drift(tmp_path: Path) -> None:
    database = tmp_path / "hook-recovery.sqlite"
    handler = RecordingHandler()
    factory = _factory(SqliteUnitOfWorkFactory(database), handler)
    prepared = await _prepare(factory)
    durable_snapshot = prepared.recovery_snapshot()
    malformed = dict(durable_snapshot)
    malformed["schemaVersion"] = 2

    with pytest.raises(ProductionHookBundleError, match="recovery snapshot"):
        await _prepare(factory, durable_snapshot=malformed)

    token = ManualCancellationToken()
    layer = HookLayer(
        HookScope.USER,
        "principal-1",
        1,
        (_definition("user-turn", HookScope.USER, "principal-1", HookEvent.TURN_START),),
    )
    installed = await factory.configuration.install_layer(
        layer,
        expected_revision=0,
        idempotency_key="recovery-install",
        cancellation=token,
    )
    await factory.configuration.confirm_layer(
        HookScope.USER,
        "principal-1",
        hook_layer_hash(layer),
        expected_revision=installed.revision,
        idempotency_key="recovery-confirm",
        cancellation=token,
    )
    with pytest.raises(ProductionHookBundleError, match="drifted before binding"):
        factory.build_prepared(prepared)

    restarted = _factory(SqliteUnitOfWorkFactory(database), handler)
    with pytest.raises(ProductionHookBundleError, match="recovery snapshot/configuration drifted"):
        await _prepare(restarted, durable_snapshot=durable_snapshot)


@pytest.mark.asyncio
async def test_bundle_exposes_same_port_to_worker_compaction_and_subagent_adapters() -> None:
    handler = RecordingHandler()
    factory = _factory(
        InMemoryUnitOfWorkFactory(),
        handler,
        managed=_managed(HookEvent.SESSION_START, HookEvent.RUNTIME_SHUTDOWN),
    )
    bundle = factory.build_prepared(await _prepare(factory))
    assert bundle.hooks is not None and bundle.context is not None
    assert bundle.worker_lifecycle is not None
    assert bundle.compaction is not None and bundle.compaction.hooks is bundle.hooks
    assert bundle.compaction.context is bundle.context
    assert bundle.subagent_lifecycle is not None

    token = ManualCancellationToken()
    await bundle.worker_lifecycle.session_start(connection_id="pipe-1", cancellation=token)
    await bundle.worker_lifecycle.runtime_shutdown(
        shutdown_id="shutdown-1",
        reason_code="normal",
        cancellation=token,
    )
    assert [item.event for item in handler.invocations] == [
        HookEvent.SESSION_START,
        HookEvent.RUNTIME_SHUTDOWN,
    ]
    assert all(item.context is bundle.context for item in handler.invocations)

    assert bundle.context_factory is not None
    with pytest.raises(ProductionHookBundleError, match="another Workspace"):
        bundle.context_factory("workspace-other", "session-1", "principal-1")
    assert handler.output.decision is HookDecision.CONTINUE


class _OneStepPlanner:
    def __init__(self) -> None:
        self._planned = False

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        if self._planned:
            raise AssertionError("unexpected second planning call")
        self._planned = True
        return PlanningStep(
            (),
            False,
            "done",
            attempts=(
                PlanningAttempt(
                    request_id=f"test-hook-{state.model_rounds + 1}",
                    repair_index=0,
                    outcome=PlanningAttemptOutcome.SUCCEEDED,
                    usage=ModelUsage(0, 0, 0, 0),
                ),
            ),
        )


class _NoToolKernel:
    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        del calls, cancellation, observer
        raise AssertionError("no-tool Agent turn must not reach Tool Kernel")


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def commit(
        self,
        state: RunState,
        *,
        event_type: str,
        payload: Mapping[str, object],
        terminal: bool = False,
    ) -> None:
        del state, payload, terminal
        self.events.append(event_type)


def _run_budget() -> BudgetLedger:
    return BudgetLedger(
        RunBudget(
            max_model_rounds=8,
            max_tool_calls=8,
            max_parallel_reads=2,
            max_wall_seconds=60,
            max_input_tokens=100,
            max_output_tokens=100,
            max_cost=Decimal("1"),
            max_artifact_bytes=1024,
            max_subagents=1,
        ),
        started_at=NOW,
    )


@pytest.mark.asyncio
async def test_prepared_production_port_drives_the_canonical_agent_loop_lifecycle() -> None:
    handler = RecordingHandler()
    lifecycle_events = (
        HookEvent.TURN_START,
        HookEvent.BEFORE_MODEL,
        HookEvent.AFTER_MODEL,
        HookEvent.TURN_STOP,
    )
    factory = _factory(InMemoryUnitOfWorkFactory(), handler, managed=_managed(*lifecycle_events))
    bundle = factory.build_prepared(await _prepare(factory))
    assert bundle.hooks is not None and bundle.context is not None

    result = await run_agent_loop(
        RunState("workspace-1", "session-1", "turn-1", "run-1", AgentLineage.root("run-1")),
        planner=_OneStepPlanner(),
        tool_kernel=_NoToolKernel(),
        recorder=_Recorder(),
        budget=_run_budget(),
        cancellation=CancellationScope(name="production-hook-agent"),
        now=lambda: NOW,
        hooks=bundle.hooks,
        hook_context=bundle.context,
    )

    assert result.phase is RunPhase.COMPLETED
    assert [item.event for item in handler.invocations] == [
        HookEvent.TURN_START,
        HookEvent.BEFORE_MODEL,
        HookEvent.AFTER_MODEL,
        HookEvent.TURN_STOP,
    ]


def _tool_definition() -> ToolDefinition:
    return ToolDefinition(
        name="workspace.read",
        version="1",
        description="read one value",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.READ,
        side_effect_class=SideEffectClass.READ,
        required_capabilities=frozenset({"workspace.read"}),
        concurrency_safe=True,
        idempotent=True,
        retryable=False,
        timeout_ms=5_000,
        output_limit_bytes=4_096,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


class _CollectingExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        self.calls.append(call)
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.SUCCEEDED,
            {"value": call.arguments["value"]},
            "ok",
            (),
            (),
            (),
            False,
            None,
            None,
            None,
        )


class _ApprovalCapture:
    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    async def request(
        self,
        approval: ApprovalRequest,
        cancellation: CancellationToken,
        observer: ApprovalObserver | None = None,
    ) -> ApprovalDecisionReceipt:
        cancellation.checkpoint()
        self.requests.append(approval)
        if observer is not None:
            await observer.required(approval)
        resolution = ApprovalResolution(
            approval.approval_id,
            ApprovalState.APPROVED,
            ApprovalScope.ONCE,
            NOW,
            "principal-1",
            False,
        )
        if observer is not None:
            await observer.resolved(approval, resolution)
        return ApprovalDecisionReceipt(approval, resolution)

    async def cancel(self, approval_id: str, reason: str) -> None:
        del approval_id, reason

    async def pending(self, approval_id: str) -> ApprovalRequest | None:
        del approval_id
        return None


@pytest.mark.asyncio
async def test_production_pretool_mutation_and_ask_bind_kernel_approval_to_mutated_args(tmp_path: Path) -> None:
    handler = MutationApprovalHandler()
    factory = _factory(
        SqliteUnitOfWorkFactory(tmp_path / "hook-kernel.sqlite"),
        handler,
        managed=_managed(HookEvent.PRE_TOOL_USE, HookEvent.APPROVAL_REQUIRED),
    )
    bundle = factory.build_prepared(await _prepare(factory))
    assert bundle.hooks is not None
    tool = _tool_definition()
    original_arguments = {"value": 1}
    original = ToolCall(
        "call-1",
        "run-1",
        "workspace-1",
        tool.name,
        tool.version,
        original_arguments,
        canonical_json_sha256(original_arguments),
        "idem-1",
        None,
        AgentLineage.root("run-1"),
        tool.fingerprint,
        tool.result_sensitivity,
    )
    policy_context = PolicyContext(
        workspace_id="workspace-1",
        session_id="session-1",
        principal_id="principal-1",
        run_id="run-1",
        permission_mode=PermissionMode.BYPASS,
        effective_scope=CapabilityScope(
            allowed_tools=frozenset({tool.name}),
            denied_tools=frozenset(),
            allowed_risks=frozenset(RiskClass),
            root_capabilities=tool.required_capabilities,
            allow_network=False,
            allow_secret_handles=False,
        ),
        workspace_trusted=True,
        now=NOW,
    )
    executor = _CollectingExecutor()
    approvals = _ApprovalCapture()
    clock = ManualClock(NOW)
    kernel = UnifiedToolKernel(
        registry=ToolRegistry("run-1-tools", (tool,)),
        validator=ToolValidator(),
        policy=RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink()),
        policy_context=lambda _: policy_context,
        scheduler=ToolScheduler(clock=clock, max_parallel_reads=1),
        dispatcher=ToolDispatcher(local=executor),
        journal=SqliteInvocationJournal(tmp_path / "hook-kernel.sqlite"),
        clock=clock,
        ids=DeterministicIdGenerator(start=100),
        approvals=approvals,
        hooks=bundle.hooks,
    )

    execution = (await kernel.execute_batch((original,), ManualCancellationToken()))[0]

    assert execution.result.status is ToolResultStatus.SUCCEEDED
    assert execution.call.arguments == {"value": 3}
    assert execution.call.args_hash != original.args_hash
    assert executor.calls == [execution.call]
    assert len(approvals.requests) == 1
    assert approvals.requests[0].binding.args_hash == execution.call.args_hash
    assert [item.event for item in handler.invocations] == [
        HookEvent.PRE_TOOL_USE,
        HookEvent.APPROVAL_REQUIRED,
    ]
