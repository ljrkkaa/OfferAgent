"""Production unified Tool Kernel used by the single Agent Loop."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING

from offeragent_harness.hooks import (
    HookDecision,
    HookEvent,
    HookExecutionContext,
    HookInvocation,
    HookOutcome,
    HookToolInput,
)
from offeragent_harness.permissions import (
    ApprovalBinding,
    ApprovalRequest,
    ApprovalState,
    PolicyContext,
    PolicyDecision,
    PolicyDisposition,
    RiskClass,
    approval_id_for,
)
from offeragent_harness.ports import (
    ApprovalPort,
    CancellationToken,
    Clock,
    HookLifecyclePort,
    IdGenerator,
    InvocationJournal,
    InvocationJournalConflict,
    InvocationRecord,
    JournalState,
    PolicyEvaluator,
    ToolLifecycleObserver,
    ToolObservabilitySink,
)

from .artifacts import ToolArtifactManager
from .canonical import canonical_json_sha256
from .definitions import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffectClass,
    ToolCall,
    ToolDefinition,
)
from .dispatcher import ToolDispatcher, ToolDispatchError
from .preflight import (
    PreflightConflict,
    PreflightEvidence,
    PreflightProvider,
    PreflightProviderUnavailable,
    PreflightRegistry,
)
from .recovery_contract import (
    invocation_journal_scope,
    invocation_request_fingerprint,
    is_safe_crash_replay,
    is_side_effect_free,
)
from .registry import ToolCapabilityUnavailable, ToolNotFound, ToolRegistry, ToolVersionUnavailable
from .results import SideEffect, SideEffectKind, SideEffectState, ToolError, ToolResult, ToolResultStatus
from .scheduler import ScheduledInvocation, ToolScheduler, abort_scheduled_invocations
from .validator import ToolValidationError, ToolValidator

if TYPE_CHECKING:
    from offeragent_harness.agent.loop import ToolExecution

PolicyContextFactory = Callable[[ToolCall], PolicyContext]


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    references: int


@dataclass
class _PreparedPreflight:
    provider: PreflightProvider
    evidence: PreflightEvidence
    _release_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def release(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        """Complete provider-local state exactly once and resist caller cancel.

        The provider runs in its own task so cancelling the Run cannot interrupt
        plan release halfway through.  Provider failures remain cleanup-only;
        a cancellation delivered to the caller is re-raised after release so it
        cannot be mistaken for a successful tool completion.
        """

        task = self._release_task
        if task is None:
            task = asyncio.create_task(self.provider.complete(definition, call, self.evidence, result))
            self._release_task = task
        interrupted: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if interrupted is None:
                    interrupted = error
                continue
            except BaseException:
                break
        if task.done() and not task.cancelled():
            try:
                task.result()
            except BaseException:
                pass
        if interrupted is not None:
            raise interrupted


class KeyedLockPool:
    """Cancellation-safe fair locks shared by all kernels in one Worker.

    ``asyncio.Lock`` grants queued acquisitions fairly.  Reference tracking
    keeps a key alive while any holder or waiter can still observe it, and the
    cancellation race releases a just-granted lock before propagating cancel.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, ...], _LockEntry] = {}
        self._metadata_lock = asyncio.Lock()

    @asynccontextmanager
    async def hold(
        self,
        key: tuple[str, ...],
        cancellation: CancellationToken,
    ) -> AsyncIterator[None]:
        async with self._metadata_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry(asyncio.Lock(), 0)
                self._entries[key] = entry
            entry.references += 1
        acquired = False
        try:
            await self._acquire(entry.lock, cancellation)
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._metadata_lock:
                entry.references -= 1
                if entry.references == 0 and self._entries.get(key) is entry:
                    self._entries.pop(key)

    @staticmethod
    async def _acquire(lock: asyncio.Lock, cancellation: CancellationToken) -> None:
        cancellation.checkpoint()
        acquire = asyncio.create_task(lock.acquire())
        cancel_wait = asyncio.create_task(cancellation.wait())
        acquired = False
        try:
            done, _ = await asyncio.wait((acquire, cancel_wait), return_when=asyncio.FIRST_COMPLETED)
            if acquire in done:
                acquired = acquire.result()
            if cancel_wait in done:
                cancellation.checkpoint()
            if not acquired:
                acquired = await acquire
            cancellation.checkpoint()
        except BaseException:
            if not acquire.done():
                acquire.cancel()
                await asyncio.gather(acquire, return_exceptions=True)
            elif not acquired and not acquire.cancelled():
                acquired = acquire.result()
            if acquired:
                lock.release()
            raise
        finally:
            if not cancel_wait.done():
                cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)


class UnifiedToolKernel:
    """Registry -> Schema -> Policy/Approval -> Schedule -> Dispatch -> Journal."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        validator: ToolValidator,
        policy: PolicyEvaluator,
        policy_context: PolicyContextFactory,
        scheduler: ToolScheduler,
        dispatcher: ToolDispatcher,
        journal: InvocationJournal,
        clock: Clock,
        ids: IdGenerator,
        approvals: ApprovalPort | None = None,
        artifacts: ToolArtifactManager | None = None,
        preflights: PreflightRegistry | None = None,
        hooks: HookLifecyclePort | None = None,
        managed_hook_owner_id: str = "system",
        hook_approval_ttl: timedelta = timedelta(minutes=5),
        observability: ToolObservabilitySink | None = None,
        lock_pool: KeyedLockPool | None = None,
    ) -> None:
        if not managed_hook_owner_id:
            raise ValueError("managed_hook_owner_id must not be empty")
        if hook_approval_ttl.total_seconds() <= 0:
            raise ValueError("hook_approval_ttl must be positive")
        self._registry = registry
        self._validator = validator
        self._policy = policy
        self._policy_context = policy_context
        self._scheduler = scheduler
        self._dispatcher = dispatcher
        self._journal = journal
        self._clock = clock
        self._ids = ids
        self._approvals = approvals
        self._artifacts = artifacts
        self._preflights = preflights or PreflightRegistry(())
        self._hooks = hooks
        self._managed_hook_owner_id = managed_hook_owner_id
        self._hook_approval_ttl = hook_approval_ttl
        self._observability = observability
        self._locks = lock_pool or KeyedLockPool()

    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        cancellation.checkpoint()
        executions: list[ToolExecution] = []
        index = 0
        while index < len(calls):
            end = index + 1
            if self._parallel_preparation_safe(calls[index]):
                while end < len(calls) and self._parallel_preparation_safe(calls[end]):
                    end += 1
            group = await self._execute_prepared_group(calls[index:end], cancellation, observer)
            executions.extend(group)
            index = end
            if (
                len(group) == 1
                and not self._parallel_preparation_safe(group[0].call)
                and group[0].result.status is not ToolResultStatus.SUCCEEDED
            ):
                for call in calls[index:]:
                    executions.append(await self._abort_remaining_batch_call(call, observer))
                break
        return tuple(executions)

    async def _abort_remaining_batch_call(
        self,
        call: ToolCall,
        observer: ToolLifecycleObserver | None,
    ) -> ToolExecution:
        try:
            definition = self._registry.get(call.name, call.version)
        except (ToolNotFound, ToolVersionUnavailable):
            definition = self._unavailable_definition(call)
        result = self._cancelled(
            call,
            "prior_serial_call_failed",
            "前一个串行工具调用未成功, 后续调用未执行。",
        )
        if observer is not None:
            await observer.result_available(call, definition, result)
        from offeragent_harness.agent.loop import ToolExecution

        return ToolExecution(call=call, definition=definition, result=result)

    def _parallel_preparation_safe(self, call: ToolCall) -> bool:
        try:
            definition = self._registry.get(call.name, call.version)
        except (ToolNotFound, ToolVersionUnavailable):
            return False
        return (
            definition.risk is RiskClass.READ
            and definition.side_effect_class in {SideEffectClass.NONE, SideEffectClass.READ}
            and definition.concurrency_safe
        )

    async def _execute_prepared_group(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None,
    ) -> tuple[ToolExecution, ...]:
        started: dict[str, float] = {}
        prepared: list[ScheduledInvocation] = []
        try:
            for call in calls:
                started[call.tool_call_id] = self._clock.monotonic()
                prepared.append(await self._prepare_scheduled(call, cancellation, observer))
            scheduled = tuple(
                invocation
                if observer is None or invocation.precomputed_result is not None
                else replace(
                    invocation,
                    on_started=lambda started_invocation: observer.execution_started(
                        started_invocation.call,
                        started_invocation.definition,
                    ),
                )
                for invocation in prepared
            )
        except BaseException:
            await abort_scheduled_invocations(prepared)
            raise

        async def on_result(invocation: ScheduledInvocation, result: ToolResult) -> None:
            if self._observability is not None:
                context = replace(self._policy_context(invocation.call), now=self._clock.utcnow())
                elapsed_ms = _elapsed_ms(started[invocation.call.tool_call_id], self._clock.monotonic())
                try:
                    await self._observability.result_recorded(
                        invocation.call,
                        invocation.definition,
                        result,
                        context,
                        elapsed_ms,
                    )
                except Exception:
                    # Local telemetry is diagnostic-only and cannot rewrite a
                    # real ToolResult or make the sole Tool Kernel unavailable.
                    pass
            if observer is not None:
                await observer.result_available(invocation.call, invocation.definition, result)

        try:
            results = await self._scheduler.execute_batch(scheduled, cancellation, on_result)
        except BaseException:
            # The scheduler already releases every handed-off preflight.  This
            # second boundary also covers custom scheduler implementations and
            # is harmless because provider release is once-only.
            await abort_scheduled_invocations(scheduled)
            raise
        from offeragent_harness.agent.loop import ToolExecution

        return tuple(
            ToolExecution(call=invocation.call, definition=invocation.definition, result=result)
            for invocation, result in zip(scheduled, results, strict=True)
        )

    async def _prepare_scheduled(
        self,
        call: ToolCall,
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None,
    ) -> ScheduledInvocation:
        cancellation.checkpoint()
        try:
            definition = self._registry.get(call.name, call.version)
        except (ToolNotFound, ToolVersionUnavailable) as error:
            definition = self._unavailable_definition(call)
            return self._precomputed(call, definition, self._failed(call, "tool_unavailable", str(error)))
        if (
            call.result_sensitivity is ResultSensitivity.UNKNOWN
            or call.result_sensitivity is not definition.result_sensitivity
        ):
            return self._precomputed(
                call,
                definition,
                self._denied(
                    call,
                    "result_sensitivity_snapshot_mismatch",
                    "ToolCall 缺少结果敏感度快照; 或与当前工具定义不一致。",
                ),
            )
        if call.definition_fingerprint != definition.fingerprint:
            return self._precomputed(
                call,
                definition,
                self._denied(
                    call,
                    "definition_snapshot_mismatch",
                    "ToolCall 绑定的工具定义快照与当前 Registry 不一致。",
                ),
            )

        try:
            validated = self._validator.validate_arguments(definition, call.arguments)
            if validated.args_hash != call.args_hash:
                raise ValueError("validated canonical args hash differs from ToolCall.args_hash")
        except (ToolValidationError, ValueError) as error:
            return self._precomputed(call, definition, self._failed(call, "invalid_arguments", str(error)))

        context = replace(self._policy_context(call), now=self._clock.utcnow())
        context_denial = self._validate_policy_context(definition, call, context)
        if context_denial is not None:
            return self._precomputed(call, definition, context_denial)
        try:
            decision = await self._policy.evaluate(definition, call, context)
        except Exception as error:
            return self._precomputed(
                call,
                definition,
                self._denied(call, "policy_unavailable", f"Policy 评估失败: {type(error).__name__}"),
            )
        if decision.disposition is PolicyDisposition.DENY:
            return self._precomputed(
                call,
                definition,
                self._denied(call, decision.reason_code, decision.user_message),
            )

        original_call = call
        hook_ask = False
        if self._hooks is not None:
            try:
                hook_outcome = await self._invoke_pre_tool_hook(definition, call, context, cancellation)
            except Exception as error:
                return self._precomputed(
                    call,
                    definition,
                    self._denied(call, "pre_tool_hook_failed", f"PreTool Hook 失败: {type(error).__name__}"),
                )
            if hook_outcome.decision is HookDecision.DENY:
                return self._precomputed(
                    call,
                    definition,
                    self._denied(call, "pre_tool_hook_denied", "PreTool Hook 拒绝了工具调用。"),
                )
            hook_ask = hook_outcome.decision is HookDecision.ASK
            if hook_outcome.mutated_arguments is not None and hook_outcome.mutated_args_hash != call.args_hash:
                try:
                    mutated = self._validator.validate_arguments(definition, hook_outcome.mutated_arguments)
                except (ToolValidationError, ValueError) as error:
                    return self._precomputed(
                        call,
                        definition,
                        self._denied(call, "pre_tool_hook_invalid_arguments", str(error)),
                    )
                call = replace(
                    call,
                    arguments=mutated.arguments,
                    args_hash=mutated.args_hash,
                    idempotency_key=self._mutated_idempotency_key(call, mutated.args_hash),
                )
                if observer is not None:
                    try:
                        await observer.call_replaced(original_call, call)
                    except Exception as error:
                        return self._precomputed(
                            original_call,
                            definition,
                            self._denied(
                                original_call,
                                "hook_state_sync_failed",
                                f"Hook 参数快照同步失败: {type(error).__name__}",
                            ),
                        )
                context = replace(self._policy_context(call), now=self._clock.utcnow())
                context_denial = self._validate_policy_context(definition, call, context)
                if context_denial is not None:
                    return self._precomputed(call, definition, context_denial)
                try:
                    decision = await self._policy.evaluate(definition, call, context)
                except Exception as error:
                    return self._precomputed(
                        call,
                        definition,
                        self._denied(
                            call,
                            "hook_mutation_policy_unavailable",
                            f"Hook 修改参数后的 Policy 复验失败: {type(error).__name__}",
                        ),
                    )
                if decision.disposition is PolicyDisposition.DENY:
                    return self._precomputed(
                        call,
                        definition,
                        self._denied(call, "hook_mutation_policy_denied", decision.user_message),
                    )

        if hook_ask and decision.disposition is PolicyDisposition.ALLOW:
            decision = self._hook_ask_decision(definition, call, context, decision)

        scope = invocation_journal_scope(call, definition)
        completed_replay = await self._completed_journal_replay(scope, definition, call)
        if completed_replay is not None:
            return self._precomputed(call, definition, completed_replay)

        prepared_preflight: _PreparedPreflight | None
        try:
            prepared_preflight = await self._prepare_preflight(definition, call, cancellation)
        except PreflightConflict as error:
            return self._precomputed(
                call, definition, self._conflicted(call, "preflight_conflict", str(error), error.details)
            )
        except Exception as error:
            return self._precomputed(
                call,
                definition,
                self._failed(call, "preflight_failed", f"工具预检失败: {type(error).__name__}: {error}"),
            )

        decision = self._bind_preflight_evidence(decision, prepared_preflight)
        if decision.disposition is PolicyDisposition.ASK:
            try:
                approval_result, durable_binding = await self._resolve_approval(
                    definition,
                    call,
                    context,
                    decision,
                    cancellation,
                    observer,
                    prepared_preflight,
                )
            except BaseException:
                await self._abort_preflight(definition, call, prepared_preflight)
                raise
            if approval_result is not None:
                await self._complete_preflight(definition, call, prepared_preflight, approval_result)
                return self._precomputed(call, definition, approval_result)
            if durable_binding is None:
                raise RuntimeError("approved tool invocation omitted its durable approval binding")
            decision = replace(decision, approval_binding=durable_binding)

        key = ("journal", call.workspace_id, scope, call.idempotency_key)

        async def prepare() -> ToolResult | None:
            conflict = await self._revalidate_preflight(definition, call, prepared_preflight, cancellation)
            if conflict is not None:
                await self._complete_preflight(definition, call, prepared_preflight, conflict)
                return conflict
            denied = await self._revalidate_before_execution(
                definition,
                call,
                decision,
                cancellation,
                observer,
                prepared_preflight,
            )
            if denied is not None:
                await self._complete_preflight(definition, call, prepared_preflight, denied)
                return denied
            replay = await self._prepare_journal(scope, definition, call)
            if replay is not None:
                await self._complete_preflight(definition, call, prepared_preflight, replay)
            return replay

        async def execute_attempt(token: CancellationToken) -> ToolResult:
            return await self._execute_attempt(definition, call, token)

        async def finalize(result: ToolResult) -> ToolResult:
            try:
                finalized = await self._finalize_journal(scope, call, result)
            except Exception as error:
                finalized = self._journal_finalize_failure(definition, call, result, error)
            await self._complete_preflight(definition, call, prepared_preflight, finalized)
            if self._hooks is not None:
                try:
                    await self._invoke_post_tool_hook(definition, call, context, finalized, cancellation)
                except Exception:
                    # PostToolUse is observational: HookService records a warning,
                    # and an unavailable adapter cannot rewrite a real ToolResult.
                    pass
            return finalized

        return ScheduledInvocation(
            call=call,
            definition=definition,
            guard=lambda token: self._hold_invocation_locks(
                key,
                call.workspace_id,
                prepared_preflight,
                token,
            ),
            prepare=prepare,
            execute_attempt=execute_attempt,
            finalize=finalize,
            precomputed_result=None,
            abort=(
                None
                if prepared_preflight is None
                else lambda: self._abort_preflight(definition, call, prepared_preflight)
            ),
        )

    def _validate_policy_context(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
    ) -> ToolResult | None:
        if context.workspace_id != call.workspace_id or context.run_id != call.run_id:
            return self._denied(
                call,
                "policy_context_identity_mismatch",
                "PolicyContext 与 ToolCall 的 Workspace/Run 身份不一致。",
            )
        try:
            current = self._registry.resolve(call.name, call.version, context.effective_scope.root_capabilities)
        except ToolCapabilityUnavailable as error:
            return self._denied(
                call,
                "capability_unavailable",
                f"缺少 capability: {sorted(error.missing_capabilities)!r}",
            )
        if current != definition or current.fingerprint != call.definition_fingerprint:
            return self._denied(call, "definition_snapshot_mismatch", "工具定义或 capability 快照已变化。")
        return None

    async def _invoke_pre_tool_hook(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
        cancellation: CancellationToken,
    ) -> HookOutcome:
        assert self._hooks is not None
        return await self._hooks.invoke(
            HookInvocation(
                invocation_id=f"pre-tool:{call.tool_call_id}:{call.args_hash}",
                chain_id=f"tool:{call.run_id}:{call.tool_call_id}",
                event=HookEvent.PRE_TOOL_USE,
                context=self._hook_context(context),
                run_id=call.run_id,
                facts={
                    "risk": definition.risk.value,
                    "executorLocation": definition.executor_location.value,
                    "sideEffectClass": definition.side_effect_class.value,
                },
                tool=self._hook_tool_input(call),
            ),
            cancellation,
        )

    async def _invoke_post_tool_hook(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
        result: ToolResult,
        cancellation: CancellationToken,
    ) -> HookOutcome:
        assert self._hooks is not None
        result_hash = canonical_json_sha256(
            {
                "status": result.status.value,
                "data": result.data,
                "artifactIds": list(result.artifact_ids),
                "sourceRefs": list(result.source_refs),
                "beforeState": result.before_state,
                "afterState": result.after_state,
                "errorCode": None if result.error is None else result.error.code,
            }
        )
        return await self._hooks.invoke(
            HookInvocation(
                invocation_id=f"post-tool:{call.tool_call_id}:{result_hash}",
                chain_id=f"tool:{call.run_id}:{call.tool_call_id}",
                event=HookEvent.POST_TOOL_USE,
                context=self._hook_context(context),
                run_id=call.run_id,
                facts={
                    "status": result.status.value,
                    "resultHash": result_hash,
                    "artifactIds": list(result.artifact_ids),
                    "sourceRefs": list(result.source_refs),
                    "errorCode": None if result.error is None else result.error.code,
                },
                tool=self._hook_tool_input(call),
            ),
            cancellation,
        )

    def _hook_ask_decision(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
        decision: PolicyDecision,
    ) -> PolicyDecision:
        expires_at = context.now + self._hook_approval_ttl
        if call.deadline is not None and call.deadline < expires_at:
            expires_at = call.deadline
        expected_hash = call.arguments.get("expectedHash")
        if not isinstance(expected_hash, str) or (
            expected_hash != "absent" and re.fullmatch(r"sha256:[0-9a-f]{64}", expected_hash) is None
        ):
            expected_hash = None
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
            expected_state_hash=expected_hash,
            expires_at=expires_at,
        )
        return PolicyDecision(
            disposition=PolicyDisposition.ASK,
            risk=definition.risk,
            reason_code="pre_tool_hook_approval_required",
            user_message="PreTool Hook 要求用户审批此工具调用。",
            audit_facts={**dict(decision.audit_facts), "hookRequiredApproval": True},
            approval_binding=binding,
        )

    def _hook_context(self, context: PolicyContext) -> HookExecutionContext:
        return HookExecutionContext(
            managed_owner_id=self._managed_hook_owner_id,
            principal_id=context.principal_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            workspace_trusted=context.workspace_trusted,
        )

    @staticmethod
    def _hook_tool_input(call: ToolCall) -> HookToolInput:
        return HookToolInput(
            tool_call_id=call.tool_call_id,
            name=call.name,
            version=call.version,
            definition_fingerprint=call.definition_fingerprint,
            arguments=call.arguments,
            args_hash=call.args_hash,
            idempotency_key=call.idempotency_key,
        )

    @staticmethod
    def _mutated_idempotency_key(call: ToolCall, args_hash: str) -> str:
        encoded = f"{call.idempotency_key}\0{call.args_hash}\0{args_hash}".encode()
        return f"hookv1-{hashlib.sha256(encoded).hexdigest()}"

    async def _prepare_preflight(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        cancellation: CancellationToken,
    ) -> _PreparedPreflight | None:
        provider = self._preflights.resolve(definition)
        if provider is None:
            return None
        evidence = await provider.prepare(definition, call, cancellation)
        prepared = _PreparedPreflight(provider, evidence)
        try:
            if evidence.provider_id != provider.provider_id:
                raise PreflightProviderUnavailable("preflight evidence provider identity mismatch")
            if definition.approval_evidence is ApprovalEvidence.DIFF and not evidence.artifact_ids:
                raise PreflightProviderUnavailable("diff approval evidence did not produce an Artifact")
            return prepared
        except BaseException:
            await self._abort_preflight(definition, call, prepared)
            raise

    @staticmethod
    def _bind_preflight_evidence(
        decision: PolicyDecision,
        prepared: _PreparedPreflight | None,
    ) -> PolicyDecision:
        if decision.disposition is not PolicyDisposition.ASK or prepared is None:
            return decision
        binding = decision.approval_binding
        assert binding is not None
        return replace(
            decision,
            approval_binding=replace(binding, expected_state_hash=prepared.evidence.state_hash),
        )

    async def _revalidate_preflight(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        prepared: _PreparedPreflight | None,
        cancellation: CancellationToken,
    ) -> ToolResult | None:
        if prepared is None:
            return None
        try:
            await prepared.provider.revalidate(definition, call, prepared.evidence, cancellation)
        except PreflightConflict as error:
            return self._conflicted(call, "preflight_state_changed", str(error), error.details)
        except Exception as error:
            return self._failed(
                call,
                "preflight_revalidation_failed",
                f"执行前预检复验失败: {type(error).__name__}: {error}",
            )
        return None

    @staticmethod
    async def _complete_preflight(
        definition: ToolDefinition,
        call: ToolCall,
        prepared: _PreparedPreflight | None,
        result: ToolResult,
    ) -> None:
        if prepared is None:
            return
        await prepared.release(definition, call, result)

    async def _abort_preflight(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        prepared: _PreparedPreflight | None,
    ) -> None:
        if prepared is None:
            return
        result = self._cancelled(
            call,
            "preflight_released_after_abort",
            "工具调用未完成, 已释放预检状态。",
        )
        try:
            await prepared.release(definition, call, result)
        except BaseException:
            # Abort cleanup is subordinate to the cancellation/failure already
            # in flight.  ``release`` has nevertheless waited for the provider
            # task, so a repeated task cancellation cannot strand its plan.
            return

    @asynccontextmanager
    async def _hold_invocation_locks(
        self,
        journal_key: tuple[str, ...],
        workspace_id: str,
        prepared: _PreparedPreflight | None,
        cancellation: CancellationToken,
    ) -> AsyncIterator[None]:
        keys = [journal_key]
        if prepared is not None:
            keys.extend(("resource", workspace_id, key.casefold()) for key in prepared.evidence.lock_keys)
        ordered = sorted(set(keys))
        async with AsyncExitStack() as stack:
            for key in ordered:
                await stack.enter_async_context(self._locks.hold(key, cancellation))
            yield

    async def _resolve_approval(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
        decision: PolicyDecision,
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None,
        prepared_preflight: _PreparedPreflight | None,
    ) -> tuple[ToolResult | None, ApprovalBinding | None]:
        binding = decision.approval_binding
        assert binding is not None
        request = ApprovalRequest(
            approval_id=approval_id_for(call.tool_call_id, binding),
            tool_call_id=call.tool_call_id,
            binding=binding,
            risk=definition.risk,
            summary=decision.user_message,
            diff_artifact_ids=(
                prepared_preflight.evidence.artifact_ids
                if definition.approval_evidence is ApprovalEvidence.DIFF and prepared_preflight is not None
                else ()
            ),
        )
        if self._hooks is not None:
            try:
                hook_outcome = await self._invoke_approval_required_hook(
                    definition,
                    call,
                    context,
                    request,
                    cancellation,
                )
            except Exception as error:
                return (
                    self._denied(call, "approval_hook_failed", f"ApprovalRequired Hook 失败: {type(error).__name__}"),
                    None,
                )
            if hook_outcome.decision is HookDecision.DENY:
                return self._denied(call, "approval_hook_denied", "ApprovalRequired Hook 拒绝了审批请求。"), None
        if self._approvals is None:
            return self._denied(call, "approval_port_unavailable", "需要审批, 但当前没有可用审批客户端。"), None
        approval_started = self._clock.monotonic()
        try:
            receipt = await self._approvals.request(request, cancellation, observer)
        finally:
            if self._observability is not None:
                elapsed_ms = _elapsed_ms(approval_started, self._clock.monotonic())
                try:
                    await self._observability.approval_wait_recorded(call, context, elapsed_ms)
                except Exception:
                    pass
        resolution = receipt.resolution
        cancellation.checkpoint()
        if (
            resolution.approval_id != request.approval_id
            or receipt.request.approval_id != request.approval_id
            or receipt.request.tool_call_id != call.tool_call_id
            or not receipt.request.binding.same_recovery_identity(binding)
        ):
            return self._denied(call, "approval_identity_mismatch", "审批结果不属于当前 Approval 请求。"), None
        # The durable request is authoritative for TTL and evidence after a
        # Worker restart. A retry-generated binding must never extend it.
        binding = receipt.request.binding
        if resolution.state is not ApprovalState.APPROVED:
            return (
                self._denied(
                    call,
                    f"approval_{resolution.state.value}",
                    resolution.reason or "用户未批准工具调用。",
                ),
                None,
            )
        now = self._clock.utcnow()
        if now >= binding.expires_at or resolution.resolved_at >= binding.expires_at:
            return self._denied(call, "approval_expired", "审批已过期。"), None
        fresh = replace(self._policy_context(call), now=now)
        try:
            current = self._registry.resolve(call.name, call.version, fresh.effective_scope.root_capabilities)
        except (ToolCapabilityUnavailable, ToolNotFound, ToolVersionUnavailable):
            return self._denied(call, "approval_stale_scope", "审批后 capability 或 Registry 已变化。"), None
        if (
            current != definition
            or current.fingerprint != call.definition_fingerprint
            or fresh.workspace_id != binding.workspace_id
            or fresh.session_id != binding.session_id
            or fresh.principal_id != binding.principal_id
            or fresh.run_id != binding.run_id
        ):
            return self._denied(call, "approval_stale_context", "审批绑定的 Workspace/Run 已变化。"), None
        if (
            binding.tool_name != definition.name
            or binding.tool_version != definition.version
            or binding.definition_fingerprint != definition.fingerprint
        ):
            return self._denied(call, "approval_stale_tool", "审批绑定的工具名称或版本不一致。"), None
        if (
            call.args_hash != binding.args_hash
            or call.lineage.root_run_id != binding.root_run_id
            or call.lineage.run_id != binding.run_id
            or call.lineage.agent_name != binding.agent_name
            or call.lineage.ancestor_run_ids != binding.ancestor_run_ids
        ):
            return self._denied(call, "approval_stale_arguments", "审批绑定的参数或 Agent lineage 已变化。"), None
        evidence_hash = None if prepared_preflight is None else prepared_preflight.evidence.state_hash
        if binding.expected_state_hash is not None and evidence_hash != binding.expected_state_hash:
            return self._denied(call, "approval_stale_state", "审批绑定的 expectedHash 已变化。"), None
        try:
            revalidated = await self._policy.evaluate(definition, call, fresh)
        except Exception as error:
            return (
                self._denied(call, "approval_revalidation_failed", f"审批后 Policy 复验失败: {type(error).__name__}"),
                None,
            )
        if revalidated.disposition is PolicyDisposition.DENY:
            return self._denied(call, "approval_revalidation_denied", revalidated.user_message), None
        if revalidated.disposition is PolicyDisposition.ASK:
            revalidated = self._bind_preflight_evidence(revalidated, prepared_preflight)
            fresh_binding = revalidated.approval_binding
            if (
                fresh_binding is None
                or not self._same_approval_identity(fresh_binding, binding)
                or now >= fresh_binding.expires_at
            ):
                return self._denied(call, "approval_revalidation_changed", "审批后 Policy 绑定条件已变化。"), None
        return None, binding

    async def _invoke_approval_required_hook(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
        request: ApprovalRequest,
        cancellation: CancellationToken,
    ) -> HookOutcome:
        assert self._hooks is not None
        return await self._hooks.invoke(
            HookInvocation(
                invocation_id=f"approval:{request.approval_id}",
                chain_id=f"tool:{call.run_id}:{call.tool_call_id}",
                event=HookEvent.APPROVAL_REQUIRED,
                context=self._hook_context(context),
                run_id=call.run_id,
                facts={
                    "approvalId": request.approval_id,
                    "risk": definition.risk.value,
                    "expectedStateHash": request.binding.expected_state_hash,
                    "diffArtifactIds": list(request.diff_artifact_ids),
                },
                tool=self._hook_tool_input(call),
            ),
            cancellation,
        )

    async def _revalidate_before_execution(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        original_decision: PolicyDecision,
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None,
        prepared_preflight: _PreparedPreflight | None,
    ) -> ToolResult | None:
        cancellation.checkpoint()
        fresh = replace(self._policy_context(call), now=self._clock.utcnow())
        if fresh.workspace_id != call.workspace_id or fresh.run_id != call.run_id:
            return self._denied(
                call,
                "policy_context_identity_mismatch",
                "执行前 PolicyContext 与 ToolCall 身份不一致。",
            )
        try:
            current = self._registry.resolve(
                call.name,
                call.version,
                fresh.effective_scope.root_capabilities,
            )
        except (ToolCapabilityUnavailable, ToolNotFound, ToolVersionUnavailable):
            return self._denied(call, "execution_scope_changed", "执行前 capability 或 Registry 已变化。")
        if current != definition or current.fingerprint != call.definition_fingerprint:
            return self._denied(call, "execution_definition_changed", "执行前工具定义已变化。")
        try:
            decision = await self._policy.evaluate(definition, call, fresh)
        except Exception as error:
            return self._denied(call, "execution_policy_unavailable", f"执行前 Policy 复验失败: {type(error).__name__}")
        if decision.disposition is PolicyDisposition.DENY:
            return self._denied(call, "execution_policy_denied", decision.user_message)
        if decision.disposition is PolicyDisposition.ALLOW:
            return None

        decision = self._bind_preflight_evidence(decision, prepared_preflight)
        binding = decision.approval_binding
        original_binding = original_decision.approval_binding
        if (
            original_decision.disposition is PolicyDisposition.ASK
            and binding is not None
            and original_binding is not None
            and self._same_approval_identity(binding, original_binding)
            and fresh.now < binding.expires_at
            and fresh.now < original_binding.expires_at
        ):
            return None
        result, _durable_binding = await self._resolve_approval(
            definition,
            call,
            fresh,
            decision,
            cancellation,
            observer,
            prepared_preflight,
        )
        return result

    @staticmethod
    def _same_approval_identity(left: ApprovalBinding, right: ApprovalBinding) -> bool:
        return (
            left.tool_name,
            left.tool_version,
            left.definition_fingerprint,
            left.args_hash,
            left.workspace_id,
            left.root_run_id,
            left.run_id,
            left.session_id,
            left.principal_id,
            left.agent_name,
            left.ancestor_run_ids,
            left.expected_state_hash,
        ) == (
            right.tool_name,
            right.tool_version,
            right.definition_fingerprint,
            right.args_hash,
            right.workspace_id,
            right.root_run_id,
            right.run_id,
            right.session_id,
            right.principal_id,
            right.agent_name,
            right.ancestor_run_ids,
            right.expected_state_hash,
        )

    async def _prepare_journal(
        self,
        scope: str,
        definition: ToolDefinition,
        call: ToolCall,
    ) -> ToolResult | None:
        try:
            record = await self._journal.get(scope, call.idempotency_key)
            if record is None:
                await self._journal.start(
                    scope,
                    call.idempotency_key,
                    invocation_request_fingerprint(call),
                    self._clock.utcnow(),
                )
                return None
            binding_corruption = self._journal_record_binding_corruption(record, scope, call)
            if binding_corruption is not None:
                return self._journal_corrupt(definition, call, binding_corruption)
            if record.request_hash != invocation_request_fingerprint(call):
                return self._failed(call, "idempotency_conflict", "幂等键已绑定到不同参数。")
            corruption = self._journal_record_corruption(record, scope, call)
            if corruption is not None:
                return self._journal_corrupt(definition, call, corruption)
            if record.state is JournalState.COMPLETED:
                return self._validated_completed_replay(definition, call, record)
            if record.state is JournalState.UNKNOWN:
                return self._unknown(call, "journal_unknown_outcome", "此前调用结果未知, 禁止自动重放。")
            if not is_side_effect_free(definition):
                await self._journal.mark_unknown(
                    scope,
                    call.idempotency_key,
                    invocation_request_fingerprint(call),
                    self._clock.utcnow(),
                )
                return self._unknown(call, "unconfirmed_prior_invocation", "发现未确认的既有副作用, 禁止重放。")
            if not is_safe_crash_replay(definition):
                result = self._failed(call, "prior_invocation_not_retryable", "此前读取未完成且未声明可重试。")
                await self._journal.complete(
                    scope,
                    call.idempotency_key,
                    invocation_request_fingerprint(call),
                    result,
                    self._clock.utcnow(),
                )
                return result
            return None
        except InvocationJournalConflict as error:
            return self._failed(call, "idempotency_conflict", str(error))
        except ToolDispatchError:
            if not is_side_effect_free(definition):
                try:
                    await self._journal.mark_unknown(
                        scope,
                        call.idempotency_key,
                        invocation_request_fingerprint(call),
                        self._clock.utcnow(),
                    )
                except Exception:
                    pass
                return self._unknown(call, "invocation_lookup_failed", "无法查询既有副作用, 禁止重放。")
            if not is_safe_crash_replay(definition):
                result = self._failed(call, "invocation_lookup_failed", "无法查询既有读取, 且工具未声明可重试。")
                try:
                    await self._journal.complete(
                        scope,
                        call.idempotency_key,
                        invocation_request_fingerprint(call),
                        result,
                        self._clock.utcnow(),
                    )
                except Exception:
                    pass
                return result
            return None
        except Exception as error:
            return self._failed(call, "journal_unavailable", f"Invocation Journal 不可用: {type(error).__name__}")

    async def _completed_journal_replay(
        self,
        scope: str,
        definition: ToolDefinition,
        call: ToolCall,
    ) -> ToolResult | None:
        try:
            record = await self._journal.get(scope, call.idempotency_key)
        except Exception:
            return None
        if record is None:
            return None
        binding_corruption = self._journal_record_binding_corruption(record, scope, call)
        if binding_corruption is not None:
            return self._journal_corrupt(definition, call, binding_corruption)
        assert isinstance(record, InvocationRecord)
        if record.request_hash != invocation_request_fingerprint(call):
            return self._failed(call, "idempotency_conflict", "幂等键已绑定到不同参数。")
        corruption = self._journal_record_corruption(record, scope, call)
        if corruption is not None:
            return self._journal_corrupt(definition, call, corruption)
        if record.state is not JournalState.COMPLETED:
            return None
        return self._validated_completed_replay(definition, call, record)

    @staticmethod
    def _journal_record_binding_corruption(
        record: object,
        scope: str,
        call: ToolCall,
    ) -> str | None:
        if not isinstance(record, InvocationRecord):
            return "Invocation Journal 返回了错误的记录类型。"
        if record.scope != scope or record.idempotency_key != call.idempotency_key:
            return "Invocation Journal 返回了其他 scope 或幂等键的记录。"
        return None

    @staticmethod
    def _journal_record_corruption(
        record: object,
        scope: str,
        call: ToolCall,
    ) -> str | None:
        binding_corruption = UnifiedToolKernel._journal_record_binding_corruption(record, scope, call)
        if binding_corruption is not None:
            return binding_corruption
        assert isinstance(record, InvocationRecord)
        if record.state is JournalState.STARTED:
            if record.completed_at is not None or record.result is not None:
                return "STARTED Invocation Journal 记录携带了完成字段。"
            return None
        if record.state is JournalState.UNKNOWN:
            if record.completed_at is None or record.result is not None:
                return "UNKNOWN Invocation Journal 记录形状无效。"
            return None
        if record.state is JournalState.COMPLETED:
            if record.completed_at is None or record.result is None:
                return "COMPLETED Invocation Journal 记录缺少完成时间或结果。"
            if record.result.tool_call_id != call.tool_call_id:
                return "COMPLETED Invocation Journal 结果属于其他 ToolCall。"
            if record.result.status is ToolResultStatus.UNKNOWN_OUTCOME:
                return "COMPLETED Invocation Journal 不得携带 UNKNOWN_OUTCOME。"
            return None
        return "Invocation Journal 记录状态无效。"

    def _validated_completed_replay(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        record: object,
    ) -> ToolResult:
        assert isinstance(record, InvocationRecord)
        assert record.result is not None
        result = record.result
        if result.status is ToolResultStatus.SUCCEEDED:
            try:
                validated = self._validator.validate_output(definition, result.data)
            except ToolValidationError as error:
                return self._journal_corrupt(
                    definition,
                    call,
                    f"持久化的成功结果不符合当前输出 Schema: {error}",
                )
            result = replace(result, data=validated)
        return result

    def _journal_corrupt(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        message: str,
    ) -> ToolResult:
        detail = f"Invocation Journal 损坏或违反 Port 合约: {message}"
        if is_side_effect_free(definition):
            return self._failed(call, "journal_corrupt", detail)
        return self._unknown(call, "journal_corrupt", detail)

    async def _execute_attempt(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        cancellation: CancellationToken,
    ) -> ToolResult:
        result = await self._dispatcher.execute(definition, call, cancellation)
        return await self._normalize_result(definition, call, result)

    async def _normalize_result(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        if result.tool_call_id != call.tool_call_id:
            if is_side_effect_free(definition):
                return self._failed(call, "result_identity_mismatch", "Executor 返回了其他 ToolCall 的结果。")
            return self._unknown(call, "result_identity_mismatch", "Executor 结果身份错误, 副作用无法确认。")
        if result.status is not ToolResultStatus.SUCCEEDED:
            return result
        try:
            validated = self._validator.validate_output(definition, result.data)
            return replace(result, data=validated)
        except ToolValidationError as error:
            if error.issues and all(issue.keyword == "maxBytes" for issue in error.issues):
                if self._artifacts is not None:
                    try:
                        return await self._artifacts.externalize(call, result)
                    except Exception as artifact_error:
                        return self._partial(
                            call,
                            result,
                            "artifact_store_failed",
                            f"工具已执行, 但 Artifact 保存失败: {type(artifact_error).__name__}",
                        )
                return self._partial(call, result, "artifact_required", "工具输出超限且未配置 Artifact Store。")
            if is_side_effect_free(definition):
                return self._failed(call, "output_schema_mismatch", str(error))
            return self._unknown(call, "output_schema_mismatch", "副作用可能已提交, 但结果不符合 Schema。")

    async def _finalize_journal(self, scope: str, call: ToolCall, result: ToolResult) -> ToolResult:
        if result.status is ToolResultStatus.UNKNOWN_OUTCOME:
            await self._journal.mark_unknown(
                scope,
                call.idempotency_key,
                invocation_request_fingerprint(call),
                self._clock.utcnow(),
            )
            return result
        await self._journal.complete(
            scope,
            call.idempotency_key,
            invocation_request_fingerprint(call),
            result,
            self._clock.utcnow(),
        )
        return result

    def _journal_finalize_failure(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        result: ToolResult,
        error: Exception,
    ) -> ToolResult:
        if is_side_effect_free(definition) or result.status in {
            ToolResultStatus.DENIED,
            ToolResultStatus.CANCELLED,
            ToolResultStatus.FAILED,
            ToolResultStatus.TIMED_OUT,
        }:
            return result
        message = f"工具结果已返回, 但 Invocation Journal 持久化失败: {type(error).__name__}"
        return ToolResult(
            tool_call_id=call.tool_call_id,
            status=ToolResultStatus.PARTIAL,
            data=result.data,
            user_visible_summary=message,
            artifact_ids=result.artifact_ids,
            source_refs=result.source_refs,
            side_effects=result.side_effects,
            retryable=False,
            before_state=result.before_state,
            after_state=result.after_state,
            error=ToolError("journal_finalize_failed", message, False, False),
            source_references=result.source_references,
        )

    @staticmethod
    def _precomputed(call: ToolCall, definition: ToolDefinition, result: ToolResult) -> ScheduledInvocation:
        return ScheduledInvocation(call, definition, None, None, None, None, result)

    @staticmethod
    def _unavailable_definition(call: ToolCall) -> ToolDefinition:
        return ToolDefinition(
            name=call.name,
            version=call.version,
            description="Unregistered tool sentinel; never executable",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={},
            executor_location=ExecutorLocation.LOCAL,
            risk=RiskClass.READ,
            side_effect_class=SideEffectClass.NONE,
            required_capabilities=frozenset({"kernel.validation"}),
            concurrency_safe=False,
            idempotent=False,
            retryable=False,
            timeout_ms=1,
            output_limit_bytes=1,
            preflight_mode=PreflightMode.NONE,
            preflight_provider=None,
            approval_evidence=ApprovalEvidence.NONE,
            result_sensitivity=call.result_sensitivity,
        )

    @staticmethod
    def _failed(call: ToolCall, code: str, message: str) -> ToolResult:
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.FAILED,
            None,
            message,
            (),
            (),
            (),
            False,
            None,
            None,
            ToolError(code, message, False, False),
        )

    @staticmethod
    def _denied(call: ToolCall, code: str, message: str) -> ToolResult:
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.DENIED,
            None,
            message,
            (),
            (),
            (),
            False,
            None,
            None,
            ToolError(code, message, False, False),
        )

    @staticmethod
    def _cancelled(call: ToolCall, code: str, message: str) -> ToolResult:
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.CANCELLED,
            None,
            message,
            (),
            (),
            (),
            False,
            None,
            None,
            ToolError(code, message, False, True),
        )

    @staticmethod
    def _conflicted(
        call: ToolCall,
        code: str,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> ToolResult:
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.CONFLICTED,
            None,
            message,
            (),
            (),
            (),
            False,
            None,
            None,
            ToolError(code, message, False, False, dict(details or {})),
        )

    @staticmethod
    def _unknown(call: ToolCall, code: str, message: str) -> ToolResult:
        effect = SideEffect(
            SideEffectKind.EXTERNAL_SYSTEM,
            SideEffectState.UNKNOWN,
            f"tool:{call.name}:{call.args_hash}",
            None,
            None,
            {"toolCallId": call.tool_call_id},
        )
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.UNKNOWN_OUTCOME,
            None,
            message,
            (),
            (),
            (effect,),
            False,
            None,
            None,
            ToolError(code, message, False, False),
        )

    @staticmethod
    def _partial(call: ToolCall, original: ToolResult, code: str, message: str) -> ToolResult:
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.PARTIAL,
            None,
            message,
            original.artifact_ids,
            original.source_refs,
            original.side_effects,
            False,
            original.before_state,
            original.after_state,
            ToolError(code, message, False, False),
            original.source_references,
        )


ProductionToolKernel = UnifiedToolKernel


def _elapsed_ms(started: float, finished: float) -> int:
    return max(0, round((finished - started) * 1_000))


__all__ = ["KeyedLockPool", "ProductionToolKernel", "UnifiedToolKernel"]
