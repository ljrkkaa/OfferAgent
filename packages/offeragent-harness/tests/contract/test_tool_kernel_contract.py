from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offeragent_harness.agent import BudgetLedger, RunBudget
from offeragent_harness.agent.loop import ToolKernel as AgentLoopToolKernel
from offeragent_harness.hooks import HookDecision, HookEvent, HookInvocation, HookOutcome
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
    approval_id_for,
)
from offeragent_harness.permissions.audit import NullPolicyAuditSink
from offeragent_harness.permissions.evaluator import RuleBasedPolicyEvaluator
from offeragent_harness.permissions.rules import PolicyRule, RuleEffect
from offeragent_harness.ports import (
    ApprovalObserver,
    ApprovalPort,
    ArtifactMetadata,
    CancellationToken,
    ClientToolInvocation,
    HookLifecyclePort,
    InvocationJournalConflict,
    InvocationRecord,
    JournalState,
    ToolExecutor,
    ToolObservabilitySink,
)
from offeragent_harness.runtime.approval_manager import ApprovalManager, ApprovalRecord
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import (
    ControlledBarrier,
    DeterministicIdGenerator,
    FakeRunCancelled,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
    ScriptedToolExecutor,
    ToolScriptStep,
)
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightEvidence,
    PreflightMode,
    PreflightRegistry,
    ResultSensitivity,
    SideEffectClass,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
    ToolValidator,
    canonical_json_sha256,
)
from offeragent_harness.tools.artifacts import ToolArtifactManager
from offeragent_harness.tools.dispatcher import InvocationAcknowledgementLost, ToolDispatcher
from offeragent_harness.tools.kernel import KeyedLockPool, UnifiedToolKernel
from offeragent_harness.tools.registry import ToolRegistry
from offeragent_harness.tools.scheduler import RetryPolicy, ToolScheduler

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def _accept_agent_loop_kernel(_: AgentLoopToolKernel) -> None: ...


def definition(
    name: str,
    *,
    location: ExecutorLocation = ExecutorLocation.LOCAL,
    risk: RiskClass = RiskClass.READ,
    effect: SideEffectClass = SideEffectClass.READ,
    concurrent: bool = True,
    output_limit: int = 4_096,
    output_schema: Mapping[str, object] | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version="1",
        description=name,
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema=output_schema
        or {
            "type": "object",
            "properties": {"value": {"type": ["integer", "string"]}},
            "required": ["value"],
            "additionalProperties": False,
        },
        executor_location=location,
        risk=risk,
        side_effect_class=effect,
        required_capabilities=frozenset({name}),
        concurrency_safe=concurrent,
        idempotent=True,
        retryable=True,
        timeout_ms=5_000,
        output_limit_bytes=output_limit,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def call(
    tool: ToolDefinition,
    number: int,
    *,
    value: object | None = None,
    idempotency_key: str | None = None,
) -> ToolCall:
    arguments = {"value": number if value is None else value}
    return ToolCall(
        tool_call_id=f"call_{number}",
        run_id="run_1",
        workspace_id="ws_1",
        name=tool.name,
        version=tool.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=idempotency_key or f"idem_{number}",
        deadline=None,
        lineage=AgentLineage.root("run_1"),
        definition_fingerprint=tool.fingerprint,
        result_sensitivity=tool.result_sensitivity,
    )


def success(tool_call_id: str, value: int | str) -> ToolResult:
    return ToolResult(
        tool_call_id,
        ToolResultStatus.SUCCEEDED,
        {"value": value},
        "ok",
        (),
        (),
        (),
        False,
        None,
        None,
        None,
    )


def retryable_failure(tool_call_id: str) -> ToolResult:
    return ToolResult(
        tool_call_id,
        ToolResultStatus.FAILED,
        None,
        "retry",
        (),
        (),
        (),
        True,
        None,
        None,
        ToolError("retryable", "retry", True, False),
    )


class MemoryJournal:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], InvocationRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, scope: str, idempotency_key: str) -> InvocationRecord | None:
        async with self._lock:
            return self.records.get((scope, idempotency_key))

    async def start(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        started_at: datetime,
    ) -> InvocationRecord:
        async with self._lock:
            key = (scope, idempotency_key)
            existing = self.records.get(key)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise InvocationJournalConflict("different request hash")
                return existing
            record = InvocationRecord(
                scope, idempotency_key, request_hash, JournalState.STARTED, started_at, None, None
            )
            self.records[key] = record
            return record

    async def complete(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        result: ToolResult,
        completed_at: datetime,
    ) -> InvocationRecord:
        async with self._lock:
            key = (scope, idempotency_key)
            existing = self.records.get(key)
            if existing is None or existing.request_hash != request_hash:
                raise InvocationJournalConflict("missing or mismatched start")
            if existing.state is JournalState.COMPLETED:
                if existing.result != result:
                    raise InvocationJournalConflict("different completed result")
                return existing
            record = InvocationRecord(
                scope,
                idempotency_key,
                request_hash,
                JournalState.COMPLETED,
                existing.started_at,
                completed_at,
                result,
            )
            self.records[key] = record
            return record

    async def mark_unknown(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        completed_at: datetime,
    ) -> InvocationRecord:
        async with self._lock:
            key = (scope, idempotency_key)
            existing = self.records.get(key)
            if existing is None or existing.request_hash != request_hash:
                raise InvocationJournalConflict("missing or mismatched start")
            record = InvocationRecord(
                scope,
                idempotency_key,
                request_hash,
                JournalState.UNKNOWN,
                existing.started_at,
                completed_at,
                None,
            )
            self.records[key] = record
            return record


class FinalizeFailingJournal(MemoryJournal):
    async def complete(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        result: ToolResult,
        completed_at: datetime,
    ) -> InvocationRecord:
        del scope, idempotency_key, request_hash, result, completed_at
        raise OSError("simulated journal fsync failure")


@dataclass(frozen=True)
class Attempt:
    expected: ToolCall
    outcome: ToolResult | Exception


class ExactAttemptExecutor:
    def __init__(self, attempts: Sequence[Attempt]) -> None:
        self.remaining = list(attempts)
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        if not self.remaining:
            raise AssertionError(f"unexpected call after exhaustion: {tool_call!r}")
        attempt = self.remaining.pop(0)
        if attempt.expected != tool_call:
            raise AssertionError(f"expected {attempt.expected!r}, got {tool_call!r}")
        self.calls.append(tool_call)
        if isinstance(attempt.outcome, Exception):
            raise attempt.outcome
        return attempt.outcome


def make_context(tools: Sequence[ToolDefinition], mode: PermissionMode) -> PolicyContext:
    return PolicyContext(
        workspace_id="ws_1",
        session_id="session_1",
        principal_id="principal_1",
        run_id="run_1",
        permission_mode=mode,
        effective_scope=CapabilityScope(
            allowed_tools=frozenset(tool.name for tool in tools),
            denied_tools=frozenset(),
            allowed_risks=frozenset(RiskClass),
            root_capabilities=frozenset(capability for tool in tools for capability in tool.required_capabilities),
            allow_network=True,
            allow_secret_handles=True,
        ),
        workspace_trusted=True,
        now=NOW,
    )


def kernel(
    tools: Sequence[ToolDefinition],
    executor: ToolExecutor,
    journal: MemoryJournal,
    *,
    mode: PermissionMode = PermissionMode.BYPASS,
    rules: Sequence[PolicyRule] = (),
    approvals: ApprovalPort | None = None,
    artifacts: ToolArtifactManager | None = None,
    retry_policy: RetryPolicy | None = None,
    policy_context: PolicyContext | None = None,
    policy_context_factory: Callable[[ToolCall], PolicyContext] | None = None,
    clock: ManualClock | None = None,
    hooks: HookLifecyclePort | None = None,
    observability: ToolObservabilitySink | None = None,
) -> UnifiedToolKernel:
    context = policy_context or make_context(tools, mode)
    runtime_clock = clock or ManualClock(NOW)
    return UnifiedToolKernel(
        registry=ToolRegistry("snapshot_1", tools),
        validator=ToolValidator(),
        policy=RuleBasedPolicyEvaluator(rules, audit_sink=NullPolicyAuditSink()),
        policy_context=policy_context_factory or (lambda _: context),
        scheduler=ToolScheduler(
            clock=runtime_clock,
            max_parallel_reads=4,
            retry_policy=retry_policy or RetryPolicy(),
        ),
        dispatcher=ToolDispatcher(clock=runtime_clock, local=executor),
        journal=journal,
        clock=runtime_clock,
        ids=DeterministicIdGenerator(),
        approvals=approvals,
        artifacts=artifacts,
        hooks=hooks,
        observability=observability,
    )


class PathPreflight:
    provider_id = "test.resource"

    async def prepare(
        self,
        tool: ToolDefinition,
        tool_call: ToolCall,
        cancellation: CancellationToken,
    ) -> PreflightEvidence:
        del tool
        cancellation.checkpoint()
        path = "Notes/Shared.md" if tool_call.run_id == "run_a" else "notes/shared.md"
        return PreflightEvidence(
            provider_id=self.provider_id,
            state_hash=canonical_json_sha256({"path": path.casefold()}),
            artifact_ids=(),
            lock_keys=(path,),
            token=f"prepared:{tool_call.run_id}:{tool_call.tool_call_id}",
            facts={"path": path},
        )

    async def revalidate(
        self,
        tool: ToolDefinition,
        tool_call: ToolCall,
        evidence: PreflightEvidence,
        cancellation: CancellationToken,
    ) -> None:
        del tool, tool_call, evidence
        cancellation.checkpoint()

    async def complete(
        self,
        tool: ToolDefinition,
        tool_call: ToolCall,
        evidence: PreflightEvidence,
        result: ToolResult,
    ) -> None:
        del tool, tool_call, evidence, result


class TrackingPathPreflight(PathPreflight):
    def __init__(
        self,
        *,
        block_prepare_number: int | None = None,
        block_revalidate: bool = False,
        block_complete: bool = False,
        fail_complete: bool = False,
    ) -> None:
        self.block_prepare_number = block_prepare_number
        self.block_revalidate = block_revalidate
        self.block_complete = block_complete
        self.fail_complete = fail_complete
        self.prepare_attempts = 0
        self.prepare_blocked = asyncio.Event()
        self.prepare_succeeded = asyncio.Event()
        self.revalidate_blocked = asyncio.Event()
        self.complete_started = asyncio.Event()
        self.complete_release = asyncio.Event()
        self.prepared: list[str] = []
        self.completed: list[tuple[str, ToolResultStatus]] = []

    async def prepare(
        self,
        tool: ToolDefinition,
        tool_call: ToolCall,
        cancellation: CancellationToken,
    ) -> PreflightEvidence:
        self.prepare_attempts += 1
        if self.prepare_attempts == self.block_prepare_number:
            self.prepare_blocked.set()
            await cancellation.wait()
            cancellation.checkpoint()
            raise AssertionError("cancelled preflight prepare resumed")
        evidence = await super().prepare(tool, tool_call, cancellation)
        self.prepared.append(tool_call.tool_call_id)
        self.prepare_succeeded.set()
        return evidence

    async def revalidate(
        self,
        tool: ToolDefinition,
        tool_call: ToolCall,
        evidence: PreflightEvidence,
        cancellation: CancellationToken,
    ) -> None:
        if self.block_revalidate:
            self.revalidate_blocked.set()
            await cancellation.wait()
            cancellation.checkpoint()
            raise AssertionError("cancelled preflight revalidation resumed")
        await super().revalidate(tool, tool_call, evidence, cancellation)

    async def complete(
        self,
        tool: ToolDefinition,
        tool_call: ToolCall,
        evidence: PreflightEvidence,
        result: ToolResult,
    ) -> None:
        self.completed.append((tool_call.tool_call_id, result.status))
        self.complete_started.set()
        if self.block_complete:
            await self.complete_release.wait()
        if self.fail_complete:
            raise RuntimeError("simulated provider cleanup failure")
        await super().complete(tool, tool_call, evidence, result)


class CancellationBlockingApproval:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def request(
        self,
        approval: ApprovalRequest,
        cancellation: CancellationToken,
        observer: ApprovalObserver | None = None,
    ) -> ApprovalDecisionReceipt:
        if observer is not None:
            await observer.required(approval)
        self.started.set()
        await cancellation.wait()
        cancellation.checkpoint()
        raise AssertionError("cancelled approval request resumed")

    async def cancel(self, approval_id: str, reason: str) -> None:
        del approval_id, reason

    async def pending(self, approval_id: str) -> ApprovalRequest | None:
        del approval_id
        return None


def resource_definition() -> ToolDefinition:
    return ToolDefinition(
        name="vault.mutate",
        version="1",
        description="vault.mutate",
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
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        required_capabilities=frozenset({"vault.mutate"}),
        concurrency_safe=False,
        idempotent=True,
        retryable=True,
        timeout_ms=5_000,
        output_limit_bytes=4_096,
        preflight_mode=PreflightMode.REQUIRED,
        preflight_provider=PathPreflight.provider_id,
        approval_evidence=ApprovalEvidence.NONE,
    )


def resource_call(
    tool: ToolDefinition,
    number: int,
    *,
    run_id: str,
    workspace_id: str = "ws_1",
) -> ToolCall:
    return replace(
        call(tool, number),
        run_id=run_id,
        workspace_id=workspace_id,
        lineage=AgentLineage.root(run_id),
    )


def resource_kernel(
    tool: ToolDefinition,
    executor: ToolExecutor,
    journal: MemoryJournal,
    lock_pool: KeyedLockPool,
    *,
    provider: PathPreflight | None = None,
    mode: PermissionMode = PermissionMode.BYPASS,
    approvals: ApprovalPort | None = None,
) -> UnifiedToolKernel:
    runtime_clock = ManualClock(NOW)
    runtime_provider = provider or PathPreflight()

    def context(tool_call: ToolCall) -> PolicyContext:
        return replace(
            make_context((tool,), mode),
            workspace_id=tool_call.workspace_id,
            run_id=tool_call.run_id,
        )

    return UnifiedToolKernel(
        registry=ToolRegistry(
            "resource-lock-snapshot",
            (tool,),
            preflight_provider_ids=frozenset({PathPreflight.provider_id}),
        ),
        validator=ToolValidator(),
        policy=RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink()),
        policy_context=context,
        scheduler=ToolScheduler(clock=runtime_clock, max_parallel_reads=1),
        dispatcher=ToolDispatcher(clock=runtime_clock, local=executor),
        journal=journal,
        clock=runtime_clock,
        ids=DeterministicIdGenerator(),
        approvals=approvals,
        preflights=PreflightRegistry((runtime_provider,)),
        lock_pool=lock_pool,
    )


class ResourceLockProbeExecutor:
    def __init__(self, blocked_call_id: str) -> None:
        self.blocked_call_id = blocked_call_id
        self.started: dict[str, asyncio.Event] = {}
        self.release_blocked = asyncio.Event()
        self.active = 0
        self.peak = 0

    async def execute(self, tool_call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.setdefault(tool_call.tool_call_id, asyncio.Event()).set()
        try:
            if tool_call.tool_call_id == self.blocked_call_id:
                await self.release_blocked.wait()
            cancellation.checkpoint()
            return success(tool_call.tool_call_id, int(tool_call.arguments["value"]))
        finally:
            self.active -= 1

    async def wait_started(self, tool_call_id: str) -> None:
        event = self.started.setdefault(tool_call_id, asyncio.Event())
        await asyncio.wait_for(event.wait(), timeout=1)


@pytest.mark.asyncio
async def test_shared_kernel_resource_lock_serializes_same_path_across_runs_and_cancel_does_not_leak() -> None:
    tool = resource_definition()
    lock_pool = KeyedLockPool()
    executor = ResourceLockProbeExecutor("call_1")
    first_kernel = resource_kernel(tool, executor, MemoryJournal(), lock_pool)
    second_kernel = resource_kernel(tool, executor, MemoryJournal(), lock_pool)
    third_kernel = resource_kernel(tool, executor, MemoryJournal(), lock_pool)

    first = asyncio.create_task(
        first_kernel.execute_batch(
            (resource_call(tool, 1, run_id="run_a"),),
            ManualCancellationToken(),
        )
    )
    await executor.wait_started("call_1")

    waiting_token = ManualCancellationToken()
    waiting = asyncio.create_task(
        second_kernel.execute_batch(
            (resource_call(tool, 2, run_id="run_b"),),
            waiting_token,
        )
    )
    await asyncio.sleep(0)
    assert "call_2" not in executor.started
    waiting_token.cancel()
    with pytest.raises(FakeRunCancelled):
        await asyncio.wait_for(waiting, timeout=1)

    replacement = asyncio.create_task(
        third_kernel.execute_batch(
            (resource_call(tool, 3, run_id="run_c"),),
            ManualCancellationToken(),
        )
    )
    await asyncio.sleep(0)
    assert "call_3" not in executor.started

    executor.release_blocked.set()
    await first
    await executor.wait_started("call_3")
    result = await replacement
    assert result[0].result.status is ToolResultStatus.SUCCEEDED
    assert executor.peak == 1


@pytest.mark.asyncio
async def test_batch_prepare_cancellation_releases_only_previously_prepared_provider_plans() -> None:
    tool = resource_definition()
    provider = TrackingPathPreflight(block_prepare_number=2)
    journal = MemoryJournal()
    tool_kernel = resource_kernel(
        tool,
        ExactAttemptExecutor(()),
        journal,
        KeyedLockPool(),
        provider=provider,
    )
    token = ManualCancellationToken()
    running = asyncio.create_task(
        tool_kernel.execute_batch(
            (
                resource_call(tool, 1, run_id="run_a"),
                resource_call(tool, 2, run_id="run_a"),
            ),
            token,
        )
    )
    await provider.prepare_blocked.wait()

    token.cancel()
    with pytest.raises(FakeRunCancelled):
        await running

    assert provider.prepared == ["call_1"]
    assert provider.completed == [("call_1", ToolResultStatus.CANCELLED)]
    assert journal.records == {}


@pytest.mark.asyncio
async def test_approval_cancellation_releases_current_preflight_without_starting_journal() -> None:
    tool = resource_definition()
    provider = TrackingPathPreflight()
    approvals = CancellationBlockingApproval()
    journal = MemoryJournal()
    token = ManualCancellationToken()
    running = asyncio.create_task(
        resource_kernel(
            tool,
            ExactAttemptExecutor(()),
            journal,
            KeyedLockPool(),
            provider=provider,
            mode=PermissionMode.NORMAL,
            approvals=approvals,
        ).execute_batch(
            (resource_call(tool, 1, run_id="run_a"),),
            token,
        )
    )
    await approvals.started.wait()

    token.cancel()
    with pytest.raises(FakeRunCancelled):
        await running

    assert provider.completed == [("call_1", ToolResultStatus.CANCELLED)]
    assert journal.records == {}


@pytest.mark.asyncio
async def test_preflight_revalidation_cancellation_releases_once_without_journal_completion() -> None:
    tool = resource_definition()
    provider = TrackingPathPreflight(block_revalidate=True)
    journal = MemoryJournal()
    tool_kernel = resource_kernel(
        tool,
        ExactAttemptExecutor(()),
        journal,
        KeyedLockPool(),
        provider=provider,
    )
    token = ManualCancellationToken()
    running = asyncio.create_task(
        tool_kernel.execute_batch(
            (resource_call(tool, 1, run_id="run_a"),),
            token,
        )
    )
    await provider.revalidate_blocked.wait()

    token.cancel()
    with pytest.raises(FakeRunCancelled):
        await running

    assert provider.completed == [("call_1", ToolResultStatus.CANCELLED)]
    assert journal.records == {}


@pytest.mark.asyncio
async def test_guard_cancellation_releases_waiting_preflight_once() -> None:
    tool = resource_definition()
    lock_pool = KeyedLockPool()
    executor = ResourceLockProbeExecutor("call_1")
    holder_provider = TrackingPathPreflight()
    waiting_provider = TrackingPathPreflight()
    holder = asyncio.create_task(
        resource_kernel(
            tool,
            executor,
            MemoryJournal(),
            lock_pool,
            provider=holder_provider,
        ).execute_batch(
            (resource_call(tool, 1, run_id="run_a"),),
            ManualCancellationToken(),
        )
    )
    await executor.wait_started("call_1")

    waiting_token = ManualCancellationToken()
    waiting = asyncio.create_task(
        resource_kernel(
            tool,
            executor,
            MemoryJournal(),
            lock_pool,
            provider=waiting_provider,
        ).execute_batch(
            (resource_call(tool, 2, run_id="run_b"),),
            waiting_token,
        )
    )
    await waiting_provider.prepare_succeeded.wait()
    assert waiting_provider.prepared == ["call_2"]
    assert "call_2" not in executor.started

    waiting_token.cancel()
    with pytest.raises(FakeRunCancelled):
        await waiting
    assert waiting_provider.completed == [("call_2", ToolResultStatus.CANCELLED)]

    executor.release_blocked.set()
    await holder
    assert holder_provider.completed == [("call_1", ToolResultStatus.SUCCEEDED)]


@pytest.mark.asyncio
@pytest.mark.parametrize("task_cancel", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_execute_cancellation_releases_preflight_once_and_leaves_started_journal(
    task_cancel: bool,
    cleanup_fails: bool,
) -> None:
    tool = resource_definition()
    provider = TrackingPathPreflight(fail_complete=cleanup_fails)
    executor = ResourceLockProbeExecutor("call_1")
    journal = MemoryJournal()
    token = ManualCancellationToken()
    running = asyncio.create_task(
        resource_kernel(
            tool,
            executor,
            journal,
            KeyedLockPool(),
            provider=provider,
        ).execute_batch(
            (resource_call(tool, 1, run_id="run_a"),),
            token,
        )
    )
    await executor.wait_started("call_1")

    if task_cancel:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    else:
        token.cancel()
        with pytest.raises(FakeRunCancelled):
            await running

    assert provider.completed == [("call_1", ToolResultStatus.CANCELLED)]
    record = next(iter(journal.records.values()))
    assert record.state is JournalState.STARTED
    assert record.result is None


@pytest.mark.asyncio
async def test_repeated_task_cancellation_cannot_interrupt_preflight_release() -> None:
    tool = resource_definition()
    provider = TrackingPathPreflight(block_complete=True)
    executor = ResourceLockProbeExecutor("call_1")
    journal = MemoryJournal()
    running = asyncio.create_task(
        resource_kernel(
            tool,
            executor,
            journal,
            KeyedLockPool(),
            provider=provider,
        ).execute_batch(
            (resource_call(tool, 1, run_id="run_a"),),
            ManualCancellationToken(),
        )
    )
    await executor.wait_started("call_1")

    running.cancel("first cancellation")
    await provider.complete_started.wait()
    running.cancel("second cancellation")
    await asyncio.sleep(0)
    assert not running.done()

    provider.complete_release.set()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert provider.completed == [("call_1", ToolResultStatus.CANCELLED)]
    assert next(iter(journal.records.values())).state is JournalState.STARTED


@pytest.mark.asyncio
async def test_shared_kernel_resource_lock_key_includes_workspace_identity() -> None:
    tool = resource_definition()
    lock_pool = KeyedLockPool()
    executor = ResourceLockProbeExecutor("call_1")
    first_kernel = resource_kernel(tool, executor, MemoryJournal(), lock_pool)
    second_kernel = resource_kernel(tool, executor, MemoryJournal(), lock_pool)

    first = asyncio.create_task(
        first_kernel.execute_batch(
            (resource_call(tool, 1, run_id="run_a", workspace_id="ws_a"),),
            ManualCancellationToken(),
        )
    )
    await executor.wait_started("call_1")
    second = asyncio.create_task(
        second_kernel.execute_batch(
            (resource_call(tool, 2, run_id="run_b", workspace_id="ws_b"),),
            ManualCancellationToken(),
        )
    )
    await executor.wait_started("call_2")
    assert executor.peak == 2

    executor.release_blocked.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_definition_snapshot_mismatch_is_denied_before_policy_or_dispatch() -> None:
    tool = definition("workspace.read")
    stale = replace(
        call(tool, 1),
        definition_fingerprint=canonical_json_sha256({"stale": True}),
    )
    executor = ExactAttemptExecutor(())

    result = await kernel((tool,), executor, MemoryJournal()).execute_batch(
        (stale,),
        ManualCancellationToken(),
    )

    assert result[0].result.status is ToolResultStatus.DENIED
    assert result[0].result.error is not None
    assert result[0].result.error.code == "definition_snapshot_mismatch"
    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_sensitivity", [ResultSensitivity.PRIVATE, ResultSensitivity.UNKNOWN])
async def test_result_sensitivity_snapshot_mismatch_is_denied_before_policy_or_dispatch(
    stale_sensitivity: ResultSensitivity,
) -> None:
    tool = definition("workspace.read")
    stale = replace(call(tool, 1), result_sensitivity=stale_sensitivity)
    executor = ExactAttemptExecutor(())

    result = await kernel((tool,), executor, MemoryJournal()).execute_batch(
        (stale,),
        ManualCancellationToken(),
    )

    assert result[0].result.status is ToolResultStatus.DENIED
    assert result[0].result.error is not None
    assert result[0].result.error.code == "result_sensitivity_snapshot_mismatch"
    assert executor.calls == []


@pytest.mark.asyncio
async def test_policy_scope_is_revalidated_after_waiting_for_the_effect_gate() -> None:
    tool = definition(
        "vault.write",
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        concurrent=False,
    )
    calls = (call(tool, 1), call(tool, 2))

    class BlockingFirstExecutor:
        def __init__(self) -> None:
            self.calls: list[ToolCall] = []
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, tool_call: ToolCall, cancellation: CancellationToken) -> ToolResult:
            cancellation.checkpoint()
            self.calls.append(tool_call)
            if len(self.calls) == 1:
                self.entered.set()
                await self.release.wait()
            return success(tool_call.tool_call_id, int(tool_call.arguments["value"]))

    executor = BlockingFirstExecutor()
    initial = make_context((tool,), PermissionMode.BYPASS)
    current = [initial]
    tool_kernel = kernel(
        (tool,),
        executor,
        MemoryJournal(),
        policy_context_factory=lambda _: current[0],
    )
    running = asyncio.create_task(tool_kernel.execute_batch(calls, ManualCancellationToken()))
    await executor.entered.wait()
    current[0] = replace(
        initial,
        effective_scope=replace(
            initial.effective_scope,
            denied_tools=frozenset({tool.name}),
        ),
    )
    executor.release.set()

    result = await running

    assert result[0].result.status is ToolResultStatus.SUCCEEDED
    assert result[1].result.status is ToolResultStatus.DENIED
    assert result[1].result.error is not None
    assert result[1].result.error.code == "execution_policy_denied"
    assert executor.calls == [calls[0]]


@pytest.mark.asyncio
async def test_effectful_journal_finalize_failure_preserves_real_result_as_partial() -> None:
    tool = definition(
        "vault.write",
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        concurrent=False,
    )
    tool_call = call(tool, 1)
    executor = ExactAttemptExecutor((Attempt(tool_call, success(tool_call.tool_call_id, 1)),))
    journal = FinalizeFailingJournal()

    result = await kernel((tool,), executor, journal).execute_batch(
        (tool_call,),
        ManualCancellationToken(),
    )

    assert result[0].result.status is ToolResultStatus.PARTIAL
    assert result[0].result.data == {"value": 1}
    assert result[0].result.error is not None
    assert result[0].result.error.code == "journal_finalize_failed"
    assert next(iter(journal.records.values())).state is JournalState.STARTED


@pytest.mark.asyncio
async def test_kernel_validates_schema_and_isolates_invalid_sibling_before_dispatch() -> None:
    tool = definition("workspace.read")
    invalid = call(tool, 1, value="not-an-integer")
    valid = call(tool, 2)
    executor = ExactAttemptExecutor((Attempt(valid, success("call_2", 2)),))
    executions = await kernel((tool,), executor, MemoryJournal()).execute_batch(
        (invalid, valid),
        ManualCancellationToken(),
    )
    assert executions[0].result.status is ToolResultStatus.FAILED
    assert executions[0].result.error is not None and executions[0].result.error.code == "invalid_arguments"
    assert executions[1].result.status is ToolResultStatus.SUCCEEDED
    assert executor.calls == [valid]


@pytest.mark.asyncio
async def test_policy_context_identity_mismatch_is_denied_before_dispatch() -> None:
    tool = definition("workspace.read")
    tool_call = call(tool, 1)
    executor = ExactAttemptExecutor(())
    context = make_context((tool,), PermissionMode.BYPASS)
    mismatched = PolicyContext(
        workspace_id="ws_other",
        session_id=context.session_id,
        principal_id=context.principal_id,
        run_id=context.run_id,
        permission_mode=context.permission_mode,
        effective_scope=context.effective_scope,
        workspace_trusted=context.workspace_trusted,
        now=context.now,
    )

    result = await kernel(
        (tool,),
        executor,
        MemoryJournal(),
        policy_context=mismatched,
    ).execute_batch((tool_call,), ManualCancellationToken())

    assert result[0].result.status is ToolResultStatus.DENIED
    assert result[0].result.error is not None
    assert result[0].result.error.code == "policy_context_identity_mismatch"
    assert executor.calls == []


@pytest.mark.asyncio
async def test_kernel_safe_reads_use_scheduler_barrier_and_results_remain_in_call_order() -> None:
    tool = definition("workspace.read")
    first = call(tool, 1)
    second = call(tool, 2)
    barrier = ControlledBarrier("kernel-read-concurrency")
    executor = ScriptedToolExecutor(
        (
            ToolScriptStep(first, success("call_1", 1), barrier),
            ToolScriptStep(second, success("call_2", 2), barrier),
        )
    )
    run = asyncio.create_task(
        kernel((tool,), executor, MemoryJournal()).execute_batch(
            (first, second),
            ManualCancellationToken(),
        )
    )
    await barrier.wait_for_arrivals(2)
    barrier.release()
    executions = await run
    assert [execution.call for execution in executions] == [first, second]


@pytest.mark.asyncio
async def test_completed_journal_replay_never_dispatches_twice() -> None:
    tool = definition("workspace.read")
    tool_call = call(tool, 1)
    executor = ExactAttemptExecutor((Attempt(tool_call, success("call_1", 1)),))
    journal = MemoryJournal()
    tool_kernel = kernel((tool,), executor, journal)
    _accept_agent_loop_kernel(tool_kernel)

    first = await tool_kernel.execute_batch((tool_call,), ManualCancellationToken())
    second = await tool_kernel.execute_batch((tool_call,), ManualCancellationToken())
    assert first[0].result == second[0].result
    assert executor.calls == [tool_call]


class CorruptReadJournal(MemoryJournal):
    def __init__(self, record: InvocationRecord) -> None:
        super().__init__()
        self._record = record

    async def get(self, scope: str, idempotency_key: str) -> InvocationRecord | None:
        del scope, idempotency_key
        return self._record


@pytest.mark.asyncio
async def test_corrupt_journal_records_never_mask_identity_shape_or_output_damage() -> None:
    tool = definition(
        "vault.write",
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        concurrent=False,
        output_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    tool_call = call(tool, 1)
    scope = "ws_1:run_1:run_1:vault.write:1"
    request_hash = tool_call.idempotency_fingerprint
    corrupt_records = (
        InvocationRecord(
            "ws_other:run_1:run_1:vault.write:1",
            tool_call.idempotency_key,
            request_hash,
            JournalState.COMPLETED,
            NOW,
            NOW,
            success(tool_call.tool_call_id, 1),
        ),
        InvocationRecord(
            scope,
            tool_call.idempotency_key,
            request_hash,
            JournalState.COMPLETED,
            NOW,
            NOW,
            success("call_other", 1),
        ),
        InvocationRecord(
            scope,
            tool_call.idempotency_key,
            request_hash,
            JournalState.COMPLETED,
            NOW,
            NOW,
            success(tool_call.tool_call_id, "invalid"),
        ),
        InvocationRecord(
            scope,
            tool_call.idempotency_key,
            request_hash,
            JournalState.STARTED,
            NOW,
            NOW,
            success(tool_call.tool_call_id, 1),
        ),
    )

    for record in corrupt_records:
        executor = ExactAttemptExecutor(())
        executions = await kernel((tool,), executor, CorruptReadJournal(record)).execute_batch(
            (tool_call,),
            ManualCancellationToken(),
        )
        assert executions[0].result.status is ToolResultStatus.UNKNOWN_OUTCOME
        assert executions[0].result.error is not None
        assert executions[0].result.error.code == "journal_corrupt"
        assert executor.calls == []


@pytest.mark.asyncio
async def test_idempotency_key_is_bound_to_args_hash_in_production_kernel() -> None:
    tool = definition("workspace.read")
    first_call = call(tool, 1, idempotency_key="same-key")
    changed_call = call(tool, 2, idempotency_key="same-key")
    executor = ExactAttemptExecutor((Attempt(first_call, success("call_1", 1)),))
    journal = MemoryJournal()
    tool_kernel = kernel((tool,), executor, journal)

    await tool_kernel.execute_batch((first_call,), ManualCancellationToken())
    changed = await tool_kernel.execute_batch((changed_call,), ManualCancellationToken())
    assert changed[0].result.status is ToolResultStatus.FAILED
    assert changed[0].result.error is not None and changed[0].result.error.code == "idempotency_conflict"
    assert executor.calls == [first_call]


@pytest.mark.asyncio
async def test_retryable_failure_retries_inside_one_journal_claim_and_finalizes_once() -> None:
    tool = definition("workspace.read")
    tool_call = call(tool, 1)
    executor = ExactAttemptExecutor(
        (
            Attempt(tool_call, retryable_failure("call_1")),
            Attempt(tool_call, success("call_1", 1)),
        )
    )
    journal = MemoryJournal()
    execution = await kernel(
        (tool,),
        executor,
        journal,
        retry_policy=RetryPolicy(max_attempts=2, delays_seconds=(0,)),
    ).execute_batch((tool_call,), ManualCancellationToken())
    assert execution[0].result.status is ToolResultStatus.SUCCEEDED
    assert executor.calls == [tool_call, tool_call]
    assert next(iter(journal.records.values())).state is JournalState.COMPLETED


class AckLostClient:
    def __init__(self, result: ToolResult, *, queryable: bool) -> None:
        self.result = result
        self.queryable = queryable
        self.invocations: list[ClientToolInvocation] = []
        self.committed: dict[str, ToolResult] = {}

    async def invoke(self, invocation: ClientToolInvocation, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        self.invocations.append(invocation)
        self.committed[invocation.invocation_id] = self.result
        raise InvocationAcknowledgementLost()

    async def lookup_result(self, invocation_id: str, *, run_id: str | None = None) -> ToolResult | None:
        del run_id
        return self.committed.get(invocation_id) if self.queryable else None

    async def cancel(self, invocation_id: str, reason: str) -> None:
        del invocation_id, reason


def client_kernel(
    tool: ToolDefinition,
    client: AckLostClient,
    journal: MemoryJournal,
) -> UnifiedToolKernel:
    context = make_context((tool,), PermissionMode.BYPASS)
    clock = ManualClock(NOW)
    return UnifiedToolKernel(
        registry=ToolRegistry("snapshot_1", (tool,)),
        validator=ToolValidator(),
        policy=RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink()),
        policy_context=lambda _: context,
        scheduler=ToolScheduler(clock=clock, max_parallel_reads=1),
        dispatcher=ToolDispatcher(clock=clock, client=client),
        journal=journal,
        clock=clock,
        ids=DeterministicIdGenerator(),
    )


@pytest.mark.asyncio
async def test_client_ack_loss_queries_result_and_journals_without_reexecution() -> None:
    tool = definition(
        "vault.write",
        location=ExecutorLocation.CLIENT,
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        concurrent=False,
    )
    tool_call = call(tool, 1)
    client = AckLostClient(success("call_1", 1), queryable=True)
    journal = MemoryJournal()
    tool_kernel = client_kernel(tool, client, journal)

    assert (await tool_kernel.execute_batch((tool_call,), ManualCancellationToken()))[0].result.status is (
        ToolResultStatus.SUCCEEDED
    )
    assert (await tool_kernel.execute_batch((tool_call,), ManualCancellationToken()))[0].result.status is (
        ToolResultStatus.SUCCEEDED
    )
    assert len(client.invocations) == 1


@pytest.mark.asyncio
async def test_unqueryable_effectful_ack_loss_becomes_unknown_and_is_never_replayed() -> None:
    tool = definition(
        "vault.write",
        location=ExecutorLocation.CLIENT,
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        concurrent=False,
    )
    tool_call = call(tool, 1)
    client = AckLostClient(success("call_1", 1), queryable=False)
    journal = MemoryJournal()
    tool_kernel = client_kernel(tool, client, journal)

    first = await tool_kernel.execute_batch((tool_call,), ManualCancellationToken())
    second = await tool_kernel.execute_batch((tool_call,), ManualCancellationToken())
    assert first[0].result.status is ToolResultStatus.UNKNOWN_OUTCOME
    assert second[0].result.status is ToolResultStatus.UNKNOWN_OUTCOME
    assert len(client.invocations) == 1
    assert next(iter(journal.records.values())).state is JournalState.UNKNOWN


class HookFake:
    def __init__(self, outcomes: Mapping[HookEvent, HookOutcome]) -> None:
        self.outcomes = dict(outcomes)
        self.invocations: list[HookInvocation] = []

    async def invoke(self, invocation: HookInvocation, cancellation: CancellationToken) -> HookOutcome:
        cancellation.checkpoint()
        self.invocations.append(invocation)
        return self.outcomes.get(invocation.event, HookOutcome.continue_without_hooks())


class CollectingExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        self.calls.append(tool_call)
        value = tool_call.arguments["value"]
        assert isinstance(value, int)
        return success(tool_call.tool_call_id, value)


def mutation_outcome(value: int, *, decision: HookDecision = HookDecision.CONTINUE) -> HookOutcome:
    arguments = {"value": value}
    return HookOutcome(
        decision,
        (),
        (),
        ("mutator",),
        (),
        arguments,
        canonical_json_sha256(arguments),
    )


@pytest.mark.asyncio
async def test_pre_tool_mutation_revalidates_schema_and_policy_before_dispatch() -> None:
    tool = definition("workspace.read")
    tool_call = call(tool, 1)
    deny_mutated_value = PolicyRule(
        "deny-two",
        RuleEffect.DENY,
        tool_names=frozenset({tool.name}),
        argument_schema={
            "type": "object",
            "properties": {"value": {"const": 2}},
            "required": ["value"],
        },
        reason="mutated value is forbidden",
    )
    executor = CollectingExecutor()
    hooks = HookFake({HookEvent.PRE_TOOL_USE: mutation_outcome(2)})

    execution = await kernel(
        (tool,),
        executor,
        MemoryJournal(),
        rules=(deny_mutated_value,),
        hooks=hooks,
    ).execute_batch((tool_call,), ManualCancellationToken())

    assert execution[0].result.status is ToolResultStatus.DENIED
    assert execution[0].result.error is not None
    assert execution[0].result.error.code == "hook_mutation_policy_denied"
    assert executor.calls == []


@pytest.mark.asyncio
async def test_mutation_derives_new_journal_identity_and_never_reuses_old_receipt() -> None:
    tool = definition("workspace.read")
    original = call(tool, 1)
    journal = MemoryJournal()
    original_executor = CollectingExecutor()
    first = await kernel((tool,), original_executor, journal).execute_batch(
        (original,),
        ManualCancellationToken(),
    )
    assert first[0].result.data == {"value": 1}

    mutated_executor = CollectingExecutor()
    hooks = HookFake({HookEvent.PRE_TOOL_USE: mutation_outcome(9)})
    second = await kernel((tool,), mutated_executor, journal, hooks=hooks).execute_batch(
        (original,),
        ManualCancellationToken(),
    )

    mutated_call = second[0].call
    assert second[0].result.data == {"value": 9}
    assert mutated_call.args_hash != original.args_hash
    assert mutated_call.idempotency_key != original.idempotency_key
    assert mutated_executor.calls == [mutated_call]
    assert len(journal.records) == 2


@pytest.mark.asyncio
async def test_hook_ask_approval_binds_only_mutated_arguments() -> None:
    tool = definition("workspace.read")
    original = call(tool, 1)
    executor = CollectingExecutor()
    approvals = ApprovalFake()
    hooks = HookFake({HookEvent.PRE_TOOL_USE: mutation_outcome(3, decision=HookDecision.ASK)})

    execution = await kernel(
        (tool,),
        executor,
        MemoryJournal(),
        approvals=approvals,
        hooks=hooks,
    ).execute_batch((original,), ManualCancellationToken())

    mutated_call = execution[0].call
    assert execution[0].result.status is ToolResultStatus.SUCCEEDED
    assert approvals.requests[0].binding.args_hash == mutated_call.args_hash
    assert approvals.requests[0].binding.args_hash != original.args_hash
    assert approvals.requests[0].approval_id == approval_id_for(
        mutated_call.tool_call_id, approvals.requests[0].binding
    )


@pytest.mark.asyncio
async def test_approval_required_hook_can_deny_before_approval_port() -> None:
    tool = definition("workspace.read")
    tool_call = call(tool, 1)
    approvals = ApprovalFake()
    hooks = HookFake(
        {
            HookEvent.PRE_TOOL_USE: HookOutcome(HookDecision.ASK, (), (), ("asker",), ()),
            HookEvent.APPROVAL_REQUIRED: HookOutcome(HookDecision.DENY, (), (), ("enterprise-deny",), ()),
        }
    )

    execution = await kernel(
        (tool,),
        CollectingExecutor(),
        MemoryJournal(),
        approvals=approvals,
        hooks=hooks,
    ).execute_batch((tool_call,), ManualCancellationToken())

    assert execution[0].result.status is ToolResultStatus.DENIED
    assert execution[0].result.error is not None
    assert execution[0].result.error.code == "approval_hook_denied"
    assert approvals.requests == []


class ApprovalFake:
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
            approval_id=approval.approval_id,
            state=ApprovalState.APPROVED,
            scope=ApprovalScope.ONCE,
            resolved_at=NOW,
            resolver_id="user_1",
            include_descendants=False,
        )
        if observer is not None:
            await observer.resolved(approval, resolution)
        return ApprovalDecisionReceipt(approval, resolution)

    async def cancel(self, approval_id: str, reason: str) -> None:
        del approval_id, reason

    async def pending(self, approval_id: str) -> ApprovalRequest | None:
        del approval_id
        return None


class RecordingToolObservability:
    def __init__(self) -> None:
        self.results: list[tuple[ToolCall, ToolDefinition, ToolResult, PolicyContext, int]] = []
        self.approvals: list[tuple[ToolCall, PolicyContext, int]] = []

    async def result_recorded(
        self,
        call: ToolCall,
        definition: ToolDefinition,
        result: ToolResult,
        context: PolicyContext,
        elapsed_ms: int,
    ) -> None:
        self.results.append((call, definition, result, context, elapsed_ms))

    async def approval_wait_recorded(
        self,
        call: ToolCall,
        context: PolicyContext,
        elapsed_ms: int,
    ) -> None:
        self.approvals.append((call, context, elapsed_ms))


@pytest.mark.asyncio
async def test_tool_kernel_records_terminal_latency_and_approval_wait_without_arguments() -> None:
    tool = definition(
        "vault.write",
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        concurrent=False,
    )
    tool_call = call(tool, 1)
    clock = ManualClock(NOW)
    telemetry = RecordingToolObservability()

    class TimedApproval(ApprovalFake):
        async def request(
            self,
            approval: ApprovalRequest,
            cancellation: CancellationToken,
            observer: ApprovalObserver | None = None,
        ) -> ApprovalDecisionReceipt:
            clock.advance(timedelta(milliseconds=1250))
            return await super().request(approval, cancellation, observer)

    class TimedExecutor:
        async def execute(self, received: ToolCall, cancellation: CancellationToken) -> ToolResult:
            assert received == tool_call
            cancellation.checkpoint()
            clock.advance(timedelta(milliseconds=750))
            return success(received.tool_call_id, 1)

    execution = await kernel(
        (tool,),
        TimedExecutor(),
        MemoryJournal(),
        mode=PermissionMode.NORMAL,
        approvals=TimedApproval(),
        clock=clock,
        observability=telemetry,
    ).execute_batch((tool_call,), ManualCancellationToken())

    assert execution[0].result.status is ToolResultStatus.SUCCEEDED
    assert len(telemetry.approvals) == 1
    assert telemetry.approvals[0][0] == tool_call
    assert telemetry.approvals[0][2] == 1250
    assert len(telemetry.results) == 1
    assert telemetry.results[0][:3] == (tool_call, tool, execution[0].result)
    assert telemetry.results[0][4] == 2000


class ContextMutatingApproval(ApprovalFake):
    def __init__(self, mutate: Callable[[], None]) -> None:
        super().__init__()
        self._mutate = mutate

    async def request(
        self,
        approval: ApprovalRequest,
        cancellation: CancellationToken,
        observer: ApprovalObserver | None = None,
    ) -> ApprovalDecisionReceipt:
        self._mutate()
        return await super().request(approval, cancellation, observer)


@pytest.mark.asyncio
async def test_write_in_normal_mode_requires_real_approval_before_dispatch() -> None:
    tool = definition(
        "vault.write",
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        concurrent=False,
    )
    tool_call = call(tool, 1)
    executor = ExactAttemptExecutor((Attempt(tool_call, success("call_1", 1)),))
    approval = ApprovalFake()
    result = await kernel(
        (tool,),
        executor,
        MemoryJournal(),
        mode=PermissionMode.NORMAL,
        approvals=approval,
    ).execute_batch((tool_call,), ManualCancellationToken())
    assert result[0].result.status is ToolResultStatus.SUCCEEDED
    assert len(approval.requests) == 1


@pytest.mark.asyncio
async def test_approval_is_rejected_when_session_principal_changes_while_user_decides() -> None:
    tool = definition(
        "vault.write",
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        concurrent=False,
    )
    tool_call = call(tool, 1)
    executor = ExactAttemptExecutor(())
    original = make_context((tool,), PermissionMode.NORMAL)
    current = [original]
    approval = ContextMutatingApproval(lambda: current.__setitem__(0, replace(original, session_id="session_other")))

    result = await kernel(
        (tool,),
        executor,
        MemoryJournal(),
        mode=PermissionMode.NORMAL,
        approvals=approval,
        policy_context_factory=lambda _: current[0],
    ).execute_batch((tool_call,), ManualCancellationToken())

    assert result[0].result.status is ToolResultStatus.DENIED
    assert result[0].result.error is not None
    assert result[0].result.error.code == "approval_stale_context"
    assert executor.calls == []


@pytest.mark.asyncio
async def test_restart_retry_cannot_extend_persisted_approval_ttl_or_replace_old_evidence() -> None:
    tool = definition(
        "vault.write",
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        concurrent=False,
    )
    tool_call = call(tool, 1)
    old_now = NOW - timedelta(minutes=10)
    old_context = replace(make_context((tool,), PermissionMode.NORMAL), now=old_now)
    old_policy = RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink())
    old_decision = await old_policy.evaluate(tool, tool_call, old_context)
    assert old_decision.approval_binding is not None
    old_request = ApprovalRequest(
        approval_id=approval_id_for(tool_call.tool_call_id, old_decision.approval_binding),
        tool_call_id=tool_call.tool_call_id,
        binding=old_decision.approval_binding,
        risk=tool.risk,
        summary="persisted old approval",
        diff_artifact_ids=("art_old_evidence",),
    )
    approval_clock = ManualClock(old_now)
    manager = ApprovalManager(unit_of_work=InMemoryUnitOfWorkFactory(), clock=approval_clock)
    await manager._ensure_pending(old_request)
    approved = ApprovalResolution(
        approval_id=old_request.approval_id,
        state=ApprovalState.APPROVED,
        scope=ApprovalScope.ONCE,
        resolved_at=old_now,
        resolver_id="principal_1",
        include_descendants=False,
    )
    assert await manager.resolve(approved) == approved
    approval_clock.advance(timedelta(minutes=10))

    executor = ExactAttemptExecutor(())
    result = await kernel(
        (tool,),
        executor,
        MemoryJournal(),
        mode=PermissionMode.NORMAL,
        approvals=manager,
    ).execute_batch((tool_call,), ManualCancellationToken())

    assert result[0].result.status is ToolResultStatus.DENIED
    assert result[0].result.error is not None
    assert result[0].result.error.code == "approval_expired"
    assert executor.calls == []
    stored = await manager.find_pending(tool_call.tool_call_id, old_request.binding)
    assert stored is None


@pytest.mark.asyncio
async def test_queued_restart_retry_revalidates_persisted_ttl_inside_execution_gate() -> None:
    tool = definition(
        "vault.write",
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        concurrent=False,
    )
    calls = (call(tool, 1), call(tool, 2))

    class BlockingFirstExecutor:
        def __init__(self) -> None:
            self.calls: list[ToolCall] = []
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, tool_call: ToolCall, cancellation: CancellationToken) -> ToolResult:
            cancellation.checkpoint()
            self.calls.append(tool_call)
            if len(self.calls) == 1:
                self.entered.set()
                await self.release.wait()
            return success(tool_call.tool_call_id, int(tool_call.arguments["value"]))

    runtime_clock = ManualClock(NOW)
    uow = InMemoryUnitOfWorkFactory()
    manager = ApprovalManager(unit_of_work=uow, clock=runtime_clock)
    policy = RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink())
    context = make_context((tool,), PermissionMode.NORMAL)
    old_requests: list[ApprovalRequest] = []
    for tool_call in calls:
        decision = await policy.evaluate(tool, tool_call, context)
        assert decision.approval_binding is not None
        old_binding = replace(decision.approval_binding, expires_at=NOW + timedelta(minutes=1))
        old_request = ApprovalRequest(
            approval_id=approval_id_for(tool_call.tool_call_id, old_binding),
            tool_call_id=tool_call.tool_call_id,
            binding=old_binding,
            risk=tool.risk,
            summary="persisted approval before restart",
            diff_artifact_ids=(f"art_old_{tool_call.tool_call_id}",),
        )
        old_requests.append(old_request)
        await manager._ensure_pending(old_request)
        approved = ApprovalResolution(
            approval_id=old_request.approval_id,
            state=ApprovalState.APPROVED,
            scope=ApprovalScope.ONCE,
            resolved_at=NOW,
            resolver_id="principal_1",
            include_descendants=False,
        )
        assert await manager.resolve(approved) == approved

    executor = BlockingFirstExecutor()
    tool_kernel = kernel(
        (tool,),
        executor,
        MemoryJournal(),
        mode=PermissionMode.NORMAL,
        approvals=manager,
        clock=runtime_clock,
    )
    running = asyncio.create_task(tool_kernel.execute_batch(calls, ManualCancellationToken()))
    await executor.entered.wait()
    runtime_clock.advance(timedelta(minutes=2))
    executor.release.set()
    result = await running

    assert result[0].result.status is ToolResultStatus.SUCCEEDED
    assert result[1].result.status is ToolResultStatus.DENIED
    assert result[1].result.error is not None
    assert result[1].result.error.code == "approval_expired"
    assert executor.calls == [calls[0]]
    stored_second = await uow.get_entity("approvals", old_requests[1].approval_id)
    assert isinstance(stored_second, ApprovalRecord)
    assert stored_second.request.diff_artifact_ids == old_requests[1].diff_artifact_ids
    assert stored_second.request.binding.expires_at == NOW + timedelta(minutes=1)


class MemoryArtifactStore:
    def __init__(self) -> None:
        self.items: dict[str, tuple[ArtifactMetadata, bytes]] = {}

    async def put(
        self,
        metadata: ArtifactMetadata,
        content: bytes,
        *,
        idempotency_key: str,
    ) -> ArtifactMetadata:
        del idempotency_key
        self.items[metadata.artifact_id] = (metadata, content)
        return metadata

    async def metadata(self, artifact_id: str) -> ArtifactMetadata | None:
        item = self.items.get(artifact_id)
        return None if item is None else item[0]

    async def read(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> AsyncIterator[bytes]:
        content = self.items[artifact_id][1][offset:]
        yield content if limit is None else content[:limit]


@pytest.mark.asyncio
@pytest.mark.parametrize("result_sensitivity", [ResultSensitivity.WORKSPACE, ResultSensitivity.PRIVATE])
async def test_schema_valid_oversized_output_preserves_sensitivity_in_artifact(
    result_sensitivity: ResultSensitivity,
) -> None:
    tool = replace(
        definition("workspace.read", output_limit=12),
        result_sensitivity=result_sensitivity,
    )
    tool_call = call(tool, 1)
    executor = ExactAttemptExecutor((Attempt(tool_call, success("call_1", "x" * 100)),))
    store = MemoryArtifactStore()
    clock = ManualClock(NOW)
    budget = BudgetLedger(
        RunBudget(
            max_model_rounds=10,
            max_tool_calls=10,
            max_parallel_reads=2,
            max_wall_seconds=60,
            max_input_tokens=10_000,
            max_output_tokens=10_000,
            max_cost=Decimal("10"),
            max_artifact_bytes=10_000,
            max_subagents=1,
            max_subagent_depth=1,
        ),
        started_at=NOW,
    )
    manager = ToolArtifactManager(store, clock, DeterministicIdGenerator(), budget)
    result = await kernel(
        (tool,),
        executor,
        MemoryJournal(),
        artifacts=manager,
    ).execute_batch((tool_call,), ManualCancellationToken())
    assert result[0].result.status is ToolResultStatus.SUCCEEDED
    assert result[0].result.data is None
    assert result[0].result.artifact_ids == ("artifact_0001",)
    assert len(store.items) == 1
    assert next(iter(store.items.values()))[0].sensitivity.value == result_sensitivity.value
    snapshot = await budget.snapshot(now=NOW)
    assert snapshot.used.artifact_bytes == len(next(iter(store.items.values()))[1])
    assert snapshot.reserved.artifact_bytes == 0


@pytest.mark.asyncio
async def test_effectful_invalid_output_becomes_unknown_and_is_not_replayed() -> None:
    tool = definition(
        "vault.write",
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        concurrent=False,
        output_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    tool_call = call(tool, 1)
    executor = ExactAttemptExecutor((Attempt(tool_call, success("call_1", "invalid")),))
    journal = MemoryJournal()
    tool_kernel = kernel((tool,), executor, journal)

    first = await tool_kernel.execute_batch((tool_call,), ManualCancellationToken())
    second = await tool_kernel.execute_batch((tool_call,), ManualCancellationToken())
    assert first[0].result.status is ToolResultStatus.UNKNOWN_OUTCOME
    assert second[0].result.status is ToolResultStatus.UNKNOWN_OUTCOME
    assert executor.calls == [tool_call]


@pytest.mark.asyncio
async def test_policy_deny_and_unknown_tool_are_sibling_results_not_batch_exceptions() -> None:
    tool = definition("workspace.read")
    denied_call = call(tool, 1)
    unknown_arguments = {"value": 2}
    unknown_call = ToolCall(
        "call_2",
        "run_1",
        "ws_1",
        "unknown.tool",
        "1",
        unknown_arguments,
        canonical_json_sha256(unknown_arguments),
        "idem_2",
        None,
        AgentLineage.root("run_1"),
        canonical_json_sha256({"name": "unknown.tool", "version": "1", "unavailable": True}),
        tool.result_sensitivity,
    )
    executor = ExactAttemptExecutor(())
    result = await kernel(
        (tool,),
        executor,
        MemoryJournal(),
        mode=PermissionMode.NORMAL,
        rules=(PolicyRule("deny", RuleEffect.DENY, tool_names=frozenset({tool.name})),),
    ).execute_batch((denied_call, unknown_call), ManualCancellationToken())
    assert [execution.result.status for execution in result] == [ToolResultStatus.DENIED, ToolResultStatus.FAILED]
    assert result[1].definition.result_sensitivity is ResultSensitivity.WORKSPACE
    assert result[1].result.error is not None and result[1].result.error.code == "tool_unavailable"
    assert executor.calls == []
