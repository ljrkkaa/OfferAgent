"""Side-effect-aware deterministic Tool Scheduler."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import CancellationToken, Clock, OperationCancelled

from .definitions import SideEffectClass, ToolCall, ToolDefinition
from .dispatcher import ToolDispatchError
from .results import (
    SideEffect,
    SideEffectKind,
    SideEffectState,
    ToolError,
    ToolResult,
    ToolResultStatus,
)

PrepareInvocation = Callable[[], Awaitable[ToolResult | None]]
ExecuteAttempt = Callable[[CancellationToken], Awaitable[ToolResult]]
FinalizeInvocation = Callable[[ToolResult], Awaitable[ToolResult]]
AbortInvocation = Callable[[], Awaitable[None]]
GuardFactory = Callable[[CancellationToken], AbstractAsyncContextManager[None]]
RetrySleep = Callable[[float, CancellationToken], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    delays_seconds: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if len(self.delays_seconds) != self.max_attempts - 1:
            raise ValueError("retry policy requires exactly one delay for every possible retry")
        if any(delay < 0 for delay in self.delays_seconds):
            raise ValueError("retry delays cannot be negative")

    def delay_before_attempt(self, attempt: int) -> float:
        if attempt <= 1:
            return 0
        return self.delays_seconds[attempt - 2]


NO_RETRY = RetryPolicy()


@dataclass(frozen=True)
class ScheduledInvocation:
    call: ToolCall
    definition: ToolDefinition
    guard: GuardFactory | None
    prepare: PrepareInvocation | None
    execute_attempt: ExecuteAttempt | None
    finalize: FinalizeInvocation | None
    precomputed_result: ToolResult | None = None
    abort: AbortInvocation | None = None

    def __post_init__(self) -> None:
        executable_fields = (self.guard, self.prepare, self.execute_attempt, self.finalize)
        if self.precomputed_result is None and any(field is None for field in executable_fields):
            raise ValueError("executable scheduled invocation requires guard/prepare/attempt/finalize")
        if self.precomputed_result is not None and any(field is not None for field in executable_fields):
            raise ValueError("precomputed invocation cannot also be executable")
        if self.precomputed_result is not None and self.abort is not None:
            raise ValueError("precomputed invocation cannot retain abort cleanup")
        if self.precomputed_result is not None and self.precomputed_result.tool_call_id != self.call.tool_call_id:
            raise ValueError("precomputed result must match its ToolCall")


ResultCallback = Callable[[ScheduledInvocation, ToolResult], Awaitable[None]]


async def abort_scheduled_invocations(invocations: Sequence[ScheduledInvocation]) -> None:
    """Release prepared invocation state without manufacturing a ToolResult.

    Abort callbacks are deliberately separate from journal finalization.  They
    only release state that was prepared before the scheduler took ownership.
    Cleanup runs in a shielded child task, in reverse preparation order, and is
    best-effort: cancellation or a cleanup failure must not replace the
    exception that caused the batch to abort.
    """

    for invocation in reversed(invocations):
        if invocation.abort is None:
            continue
        try:
            cleanup: asyncio.Future[None] = asyncio.ensure_future(invocation.abort())
        except BaseException:
            continue
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # The caller is already unwinding an earlier cancellation.
                # Keep the provider cleanup alive and preserve that original
                # signal when control returns to the surrounding ``except``.
                continue
            except BaseException:
                break
        if cleanup.done() and not cleanup.cancelled():
            try:
                cleanup.result()
            except BaseException:
                pass


@dataclass
class _EffectWaiter:
    safe_read: bool
    ready: asyncio.Future[None]
    granted: bool = False


class FairEffectGate:
    """Fair Worker-wide reader/writer gate shared by every Run scheduler.

    A scheduler may impose a narrower per-Run read limit, while this gate is
    the authoritative ceiling for all sessions and root/child Runs owned by
    one Worker.  FIFO waiter ordering prevents a continuous stream of readers
    from starving an already-queued effectful invocation.
    """

    def __init__(self, max_readers: int) -> None:
        if type(max_readers) is not int or not 1 <= max_readers <= 256:
            raise ValueError("max_readers must be between 1 and 256")
        self._max_readers = max_readers
        self._state_lock = asyncio.Lock()
        self._active_readers = 0
        self._writer_active = False
        self._waiters: deque[_EffectWaiter] = deque()

    @property
    def max_readers(self) -> int:
        return self._max_readers

    def configure_max_readers(self, max_readers: int) -> None:
        """Bind the Worker ceiling before the gate is exposed to schedulers.

        Production composition is created before persisted Workspace config is
        reconciled.  The first effective Run snapshot may therefore replace the
        bootstrap default.  Reconfiguration after acquisition or queueing would
        make an in-flight Worker's concurrency semantics ambiguous, so fail
        closed instead.
        """

        if type(max_readers) is not int or not 1 <= max_readers <= 256:
            raise ValueError("max_readers must be between 1 and 256")
        if self._active_readers or self._writer_active or self._waiters:
            raise RuntimeError("cannot reconfigure an active effect gate")
        self._max_readers = max_readers

    @asynccontextmanager
    async def hold(self, *, safe_read: bool, cancellation: CancellationToken) -> AsyncIterator[None]:
        cancellation.checkpoint()
        waiter: _EffectWaiter | None = None
        acquired = False
        async with self._state_lock:
            if self._can_enter_immediately(safe_read):
                self._mark_acquired(safe_read)
                acquired = True
            else:
                waiter = _EffectWaiter(
                    safe_read=safe_read,
                    ready=asyncio.get_running_loop().create_future(),
                )
                self._waiters.append(waiter)
                self._grant_waiters_locked()
        if waiter is not None:
            try:
                await _wait_for_signal_or_cancellation(waiter.ready, cancellation)
                acquired = True
            except BaseException:
                async with self._state_lock:
                    if waiter.granted:
                        self._mark_released(waiter.safe_read)
                    else:
                        try:
                            self._waiters.remove(waiter)
                        except ValueError:
                            pass
                    self._grant_waiters_locked()
                raise
        try:
            cancellation.checkpoint()
            yield
        finally:
            if acquired:
                async with self._state_lock:
                    self._mark_released(safe_read)
                    self._grant_waiters_locked()

    def _can_enter_immediately(self, safe_read: bool) -> bool:
        if self._waiters or self._writer_active:
            return False
        if safe_read:
            return self._active_readers < self._max_readers
        return self._active_readers == 0

    def _mark_acquired(self, safe_read: bool) -> None:
        if safe_read:
            self._active_readers += 1
        else:
            self._writer_active = True

    def _mark_released(self, safe_read: bool) -> None:
        if safe_read:
            self._active_readers -= 1
        else:
            self._writer_active = False

    def _grant_waiters_locked(self) -> None:
        if self._writer_active or not self._waiters:
            return
        first = self._waiters[0]
        if not first.safe_read:
            if self._active_readers == 0:
                self._waiters.popleft()
                first.granted = True
                self._writer_active = True
                first.ready.set_result(None)
            return
        while (
            self._waiters
            and self._waiters[0].safe_read
            and self._active_readers < self._max_readers
            and not self._writer_active
        ):
            reader = self._waiters.popleft()
            reader.granted = True
            self._active_readers += 1
            reader.ready.set_result(None)


async def _wait_for_signal_or_cancellation(
    signal: asyncio.Future[None],
    cancellation: CancellationToken,
) -> None:
    cancellation.checkpoint()
    signal_wait = asyncio.ensure_future(asyncio.shield(signal))
    cancel_wait = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait((signal_wait, cancel_wait), return_when=asyncio.FIRST_COMPLETED)
        if cancel_wait in done:
            cancellation.checkpoint()
        await signal_wait
        cancellation.checkpoint()
    finally:
        for task in (signal_wait, cancel_wait):
            if not task.done():
                task.cancel()
        await asyncio.gather(signal_wait, cancel_wait, return_exceptions=True)


async def _default_retry_sleep(delay: float, cancellation: CancellationToken) -> None:
    cancellation.checkpoint()
    if delay == 0:
        await asyncio.sleep(0)
        cancellation.checkpoint()
        return
    sleep_task = asyncio.create_task(asyncio.sleep(delay))
    cancel_task = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait((sleep_task, cancel_task), return_when=asyncio.FIRST_COMPLETED)
        if cancel_task in done:
            cancellation.checkpoint()
        await sleep_task
        cancellation.checkpoint()
    finally:
        for task in (sleep_task, cancel_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(sleep_task, cancel_task, return_exceptions=True)


class ToolScheduler:
    def __init__(
        self,
        *,
        clock: Clock,
        max_parallel_reads: int,
        effect_gate: FairEffectGate | None = None,
        retry_policy: RetryPolicy = NO_RETRY,
        retry_sleep: RetrySleep = _default_retry_sleep,
        termination_grace_seconds: float = 0.1,
    ) -> None:
        if type(max_parallel_reads) is not int or not 1 <= max_parallel_reads <= 256:
            raise ValueError("max_parallel_reads must be between 1 and 256")
        if termination_grace_seconds <= 0:
            raise ValueError("termination_grace_seconds must be positive")
        self._clock = clock
        self._max_parallel_reads = max_parallel_reads
        self._retry_policy = retry_policy
        self._retry_sleep = retry_sleep
        self._termination_grace_seconds = termination_grace_seconds
        self._stragglers: set[asyncio.Future[ToolResult]] = set()
        self._effect_gate = effect_gate or FairEffectGate(max_parallel_reads)

    async def execute_batch(
        self,
        invocations: Sequence[ScheduledInvocation],
        cancellation: CancellationToken,
        on_result: ResultCallback | None = None,
    ) -> tuple[ToolResult, ...]:
        try:
            results: list[ToolResult] = []
            index = 0
            while index < len(invocations):
                cancellation.checkpoint()
                current = invocations[index]
                if not self._parallel_safe_read(current.definition):
                    results.append(await self._execute_one_and_notify(current, cancellation, on_result))
                    index += 1
                    continue
                end = index
                while end < len(invocations) and self._parallel_safe_read(invocations[end].definition):
                    end += 1
                group = invocations[index:end]
                for offset in range(0, len(group), self._max_parallel_reads):
                    chunk = group[offset : offset + self._max_parallel_reads]
                    results.extend(await self._execute_parallel(chunk, cancellation, on_result))
                index = end
            return tuple(results)
        except BaseException:
            await abort_scheduled_invocations(invocations)
            raise

    async def _execute_parallel(
        self,
        invocations: Sequence[ScheduledInvocation],
        cancellation: CancellationToken,
        on_result: ResultCallback | None,
    ) -> tuple[ToolResult, ...]:
        tasks = [
            asyncio.create_task(self._execute_one_and_notify(invocation, cancellation, on_result))
            for invocation in invocations
        ]
        try:
            values = await asyncio.gather(*tasks)
            return tuple(values)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _execute_one_and_notify(
        self,
        invocation: ScheduledInvocation,
        cancellation: CancellationToken,
        on_result: ResultCallback | None,
    ) -> ToolResult:
        result = await self._execute_one(invocation, cancellation)
        if on_result is not None:
            await on_result(invocation, result)
        return result

    async def _execute_one(
        self,
        invocation: ScheduledInvocation,
        cancellation: CancellationToken,
    ) -> ToolResult:
        if invocation.precomputed_result is not None:
            return invocation.precomputed_result
        async with self._effect_gate.hold(
            safe_read=self._parallel_safe_read(invocation.definition),
            cancellation=cancellation,
        ):
            return await self._execute_guarded(invocation, cancellation)

    async def _execute_guarded(
        self,
        invocation: ScheduledInvocation,
        cancellation: CancellationToken,
    ) -> ToolResult:
        assert invocation.guard is not None
        assert invocation.prepare is not None
        assert invocation.execute_attempt is not None
        assert invocation.finalize is not None
        async with invocation.guard(cancellation):
            try:
                # The two guards may have queued independently.  Do not create a
                # STARTED journal entry after cancellation won either wait.
                cancellation.checkpoint()
                replay = await invocation.prepare()
                if replay is not None:
                    return replay
                final_result: ToolResult | None = None
                for attempt in range(1, self._retry_policy.max_attempts + 1):
                    cancellation.checkpoint()
                    delay = self._retry_policy.delay_before_attempt(attempt)
                    if delay:
                        await self._retry_sleep(delay, cancellation)
                    try:
                        final_result = await self._attempt_with_controls(invocation, cancellation)
                    except OperationCancelled:
                        raise
                    except Exception as error:
                        final_result = self._exception_result(invocation.call, invocation.definition, error)
                    if not self._can_retry(invocation.definition, final_result, attempt):
                        break
                assert final_result is not None
                return await invocation.finalize(final_result)
            except OperationCancelled:
                raise
            except Exception as error:
                result = self._exception_result(invocation.call, invocation.definition, error)
                try:
                    return await invocation.finalize(result)
                except Exception:
                    return result

    async def _attempt_with_controls(
        self,
        invocation: ScheduledInvocation,
        cancellation: CancellationToken,
    ) -> ToolResult:
        assert invocation.execute_attempt is not None
        remaining = invocation.definition.timeout_seconds
        if invocation.call.deadline is not None:
            remaining = min(remaining, (invocation.call.deadline - self._clock.utcnow()).total_seconds())
        if remaining <= 0:
            return self._timeout_result(invocation.call, invocation.definition, "deadline_expired")
        execution: asyncio.Future[ToolResult] = asyncio.ensure_future(invocation.execute_attempt(cancellation))
        cancel_wait = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (execution, cancel_wait),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_wait in done:
                await self._cancel_execution_bounded(execution)
                committed = self._completed_effect_result(invocation.definition, execution)
                if committed is not None:
                    # Cancellation loses once an effectful executor reports a
                    # committed, partial, or explicitly unknown outcome.  The
                    # caller must journal that result instead of manufacturing
                    # a cancellation that contradicts durable side effects.
                    return committed
                cancellation.checkpoint()
                raise AssertionError("cancelled token checkpoint returned")
            if execution in done:
                return await execution
            terminated = await self._cancel_execution_bounded(execution)
            committed = self._completed_effect_result(invocation.definition, execution)
            if committed is not None:
                # A timeout can race the executor's durable commit boundary.
                # Preserve the completed effect result so finalize/journal and
                # filesystem recovery cannot disagree about the outcome.
                return committed
            if not terminated and invocation.definition.side_effect_class not in {
                SideEffectClass.NONE,
                SideEffectClass.READ,
            }:
                return self._unknown_result(
                    invocation.call,
                    "tool_termination_unconfirmed",
                    "工具超时且执行器未在终止宽限期内确认退出, 副作用结果未知。",
                )
            return self._timeout_result(invocation.call, invocation.definition, "tool_timeout")
        except asyncio.CancelledError:
            await self._cancel_execution_bounded(execution)
            raise
        finally:
            if not cancel_wait.done():
                cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)

    async def _cancel_execution_bounded(self, execution: asyncio.Future[ToolResult]) -> bool:
        if execution.done():
            await asyncio.gather(execution, return_exceptions=True)
            return True
        execution.cancel()
        done, _ = await asyncio.wait((execution,), timeout=self._termination_grace_seconds)
        if execution in done:
            await asyncio.gather(execution, return_exceptions=True)
            return True
        self._stragglers.add(execution)
        execution.add_done_callback(self._discard_straggler)
        return False

    @staticmethod
    def _completed_effect_result(
        definition: ToolDefinition,
        execution: asyncio.Future[ToolResult],
    ) -> ToolResult | None:
        if definition.side_effect_class in {SideEffectClass.NONE, SideEffectClass.READ}:
            return None
        if not execution.done() or execution.cancelled():
            return None
        try:
            result = execution.result()
        except BaseException:
            return None
        if result.status not in {
            ToolResultStatus.SUCCEEDED,
            ToolResultStatus.PARTIAL,
            ToolResultStatus.UNKNOWN_OUTCOME,
        }:
            return None
        return result

    def _discard_straggler(self, execution: asyncio.Future[ToolResult]) -> None:
        self._stragglers.discard(execution)
        if not execution.cancelled():
            execution.exception()

    def _can_retry(self, definition: ToolDefinition, result: ToolResult, attempt: int) -> bool:
        if attempt >= self._retry_policy.max_attempts:
            return False
        if not (definition.idempotent and definition.retryable and result.retryable):
            return False
        if result.status not in {
            ToolResultStatus.FAILED,
            ToolResultStatus.TIMED_OUT,
            ToolResultStatus.CONFLICTED,
        }:
            return False
        unsafe_states = {SideEffectState.COMMITTED, SideEffectState.PARTIAL, SideEffectState.UNKNOWN}
        return not any(effect.state in unsafe_states for effect in result.side_effects)

    @staticmethod
    def _parallel_safe_read(definition: ToolDefinition) -> bool:
        return (
            definition.risk is RiskClass.READ
            and definition.side_effect_class in {SideEffectClass.NONE, SideEffectClass.READ}
            and definition.concurrency_safe
        )

    @staticmethod
    def _timeout_result(call: ToolCall, definition: ToolDefinition, code: str) -> ToolResult:
        if definition.side_effect_class in {SideEffectClass.NONE, SideEffectClass.READ}:
            retryable = definition.idempotent and definition.retryable
            return ToolResult(
                tool_call_id=call.tool_call_id,
                status=ToolResultStatus.TIMED_OUT,
                data=None,
                user_visible_summary="工具在 deadline/timeout 前未完成。",
                artifact_ids=(),
                source_refs=(),
                side_effects=(),
                retryable=retryable,
                before_state=None,
                after_state=None,
                error=ToolError(code=code, message="tool execution timed out", retryable=retryable, cancelled=False),
            )
        return ToolScheduler._unknown_result(call, code, "工具超时, 无法确认副作用是否已经提交。")

    @staticmethod
    def _exception_result(call: ToolCall, definition: ToolDefinition, error: Exception) -> ToolResult:
        if isinstance(error, ToolDispatchError):
            if error.side_effect_possible and definition.side_effect_class not in {
                SideEffectClass.NONE,
                SideEffectClass.READ,
            }:
                return ToolScheduler._unknown_result(call, error.code, str(error))
            retryable = error.retryable and definition.idempotent and definition.retryable
            code = error.code
        else:
            if definition.side_effect_class not in {SideEffectClass.NONE, SideEffectClass.READ}:
                return ToolScheduler._unknown_result(call, "executor_exception", str(error))
            retryable = False
            code = "executor_exception"
        return ToolResult(
            tool_call_id=call.tool_call_id,
            status=ToolResultStatus.FAILED,
            data=None,
            user_visible_summary=f"工具执行失败: {error}",
            artifact_ids=(),
            source_refs=(),
            side_effects=(),
            retryable=retryable,
            before_state=None,
            after_state=None,
            error=ToolError(code=code, message=str(error), retryable=retryable, cancelled=False),
        )

    @staticmethod
    def _unknown_result(call: ToolCall, code: str, message: str) -> ToolResult:
        effect = SideEffect(
            kind=SideEffectKind.EXTERNAL_SYSTEM,
            state=SideEffectState.UNKNOWN,
            resource_id=f"tool:{call.name}:{call.args_hash}",
            before_state=None,
            after_state=None,
            metadata={"toolCallId": call.tool_call_id},
        )
        return ToolResult(
            tool_call_id=call.tool_call_id,
            status=ToolResultStatus.UNKNOWN_OUTCOME,
            data=None,
            user_visible_summary=message,
            artifact_ids=(),
            source_refs=(),
            side_effects=(effect,),
            retryable=False,
            before_state=None,
            after_state=None,
            error=ToolError(code=code, message=message, retryable=False, cancelled=False),
        )


__all__ = ["NO_RETRY", "FairEffectGate", "RetryPolicy", "ScheduledInvocation", "ToolScheduler"]
