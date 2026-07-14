from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import CancellationToken, ClientToolInvocation
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import ControlledBarrier, FakeRunCancelled, ManualCancellationToken, ManualClock
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffectClass,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
)
from offeragent_harness.tools.dispatcher import DispatcherUnavailable, ToolDispatcher
from offeragent_harness.tools.scheduler import FairEffectGate, RetryPolicy, ScheduledInvocation, ToolScheduler

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def definition(
    name: str,
    *,
    location: ExecutorLocation = ExecutorLocation.LOCAL,
    effect: SideEffectClass = SideEffectClass.READ,
    risk: RiskClass = RiskClass.READ,
    concurrent: bool = True,
    idempotent: bool = True,
    retryable: bool = True,
    timeout_ms: int = 1_000,
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
        output_schema={"type": "object"},
        executor_location=location,
        risk=risk,
        side_effect_class=effect,
        required_capabilities=frozenset({name}),
        concurrency_safe=concurrent,
        idempotent=idempotent,
        retryable=retryable,
        timeout_ms=timeout_ms,
        output_limit_bytes=1_024,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def call(tool: ToolDefinition, number: int, *, deadline: datetime | None = None) -> ToolCall:
    arguments = {"value": number}
    return ToolCall(
        tool_call_id=f"call_{number}",
        run_id="run_1",
        workspace_id="ws_1",
        name=tool.name,
        version=tool.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=f"idem_{number}",
        deadline=deadline,
        lineage=AgentLineage.root("run_1"),
        definition_fingerprint=tool.fingerprint,
        result_sensitivity=tool.result_sensitivity,
    )


def success(tool_call_id: str, value: int) -> ToolResult:
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


@asynccontextmanager
async def guard(_: CancellationToken) -> AsyncIterator[None]:
    yield


def scheduled(
    tool_call: ToolCall,
    tool: ToolDefinition,
    attempt: Callable[[CancellationToken], Awaitable[ToolResult]],
) -> ScheduledInvocation:
    async def prepare() -> ToolResult | None:
        return None

    async def execute(token: CancellationToken) -> ToolResult:
        return await attempt(token)

    async def finalize(result: ToolResult) -> ToolResult:
        return result

    return ScheduledInvocation(tool_call, tool, guard, prepare, execute, finalize)


@pytest.mark.asyncio
async def test_only_contiguous_explicit_safe_reads_run_in_parallel_and_write_is_a_barrier() -> None:
    clock = ManualClock(NOW)
    scheduler = ToolScheduler(clock=clock, max_parallel_reads=4)
    read = definition("workspace.read")
    write = definition(
        "vault.write",
        effect=SideEffectClass.WRITE,
        risk=RiskClass.WRITE,
        concurrent=False,
    )
    first_gate = ControlledBarrier("first-read-run")
    write_gate = ControlledBarrier("write-run")
    last_gate = ControlledBarrier("last-read-run")

    def gated(gate: ControlledBarrier, result: ToolResult) -> Callable[[CancellationToken], Awaitable[ToolResult]]:
        async def run(token: CancellationToken) -> ToolResult:
            await gate.arrive_and_wait(token)
            return result

        return run

    invocations = (
        scheduled(call(read, 1), read, gated(first_gate, success("call_1", 1))),
        scheduled(call(read, 2), read, gated(first_gate, success("call_2", 2))),
        scheduled(call(write, 3), write, gated(write_gate, success("call_3", 3))),
        scheduled(call(read, 4), read, gated(last_gate, success("call_4", 4))),
    )
    run = asyncio.create_task(scheduler.execute_batch(invocations, ManualCancellationToken()))
    await first_gate.wait_for_arrivals(2)
    assert write_gate.arrivals == 0 and last_gate.arrivals == 0
    first_gate.release()
    await write_gate.wait_for_arrivals(1)
    assert last_gate.arrivals == 0
    write_gate.release()
    await last_gate.wait_for_arrivals(1)
    last_gate.release()
    results = await run
    assert [result.tool_call_id for result in results] == ["call_1", "call_2", "call_3", "call_4"]


@pytest.mark.asyncio
async def test_effect_gate_serializes_writes_and_blocks_reads_across_concurrent_batches() -> None:
    scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=3)
    write = definition(
        "vault.write",
        effect=SideEffectClass.WRITE,
        risk=RiskClass.WRITE,
        concurrent=False,
    )
    read = definition("workspace.read")
    first_write = ControlledBarrier("first-write")
    second_write = ControlledBarrier("second-write")
    later_read = ControlledBarrier("later-read")

    async def gated(
        barrier: ControlledBarrier,
        result: ToolResult,
        token: CancellationToken,
    ) -> ToolResult:
        await barrier.arrive_and_wait(token)
        return result

    token = ManualCancellationToken()
    first = asyncio.create_task(
        scheduler.execute_batch(
            (scheduled(call(write, 1), write, lambda current: gated(first_write, success("call_1", 1), current)),),
            token,
        )
    )
    await first_write.wait_for_arrivals(1)
    second = asyncio.create_task(
        scheduler.execute_batch(
            (scheduled(call(write, 2), write, lambda current: gated(second_write, success("call_2", 2), current)),),
            token,
        )
    )
    reader = asyncio.create_task(
        scheduler.execute_batch(
            (scheduled(call(read, 3), read, lambda current: gated(later_read, success("call_3", 3), current)),),
            token,
        )
    )
    await asyncio.sleep(0)
    assert second_write.arrivals == 0
    assert later_read.arrivals == 0

    first_write.release()
    await second_write.wait_for_arrivals(1)
    assert later_read.arrivals == 0
    second_write.release()
    await later_read.wait_for_arrivals(1)
    later_read.release()
    await asyncio.gather(first, second, reader)


@pytest.mark.asyncio
async def test_worker_shared_effect_gate_bounds_reads_across_independent_run_schedulers() -> None:
    gate = FairEffectGate(2)
    first_scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=2, effect_gate=gate)
    second_scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=2, effect_gate=gate)
    read = definition("workspace.read")
    release = asyncio.Event()
    two_started = asyncio.Event()
    active = 0
    peak = 0

    async def blocked(tool_call_id: str, token: CancellationToken) -> ToolResult:
        nonlocal active, peak
        token.checkpoint()
        active += 1
        peak = max(peak, active)
        if active == 2:
            two_started.set()
        try:
            await release.wait()
            token.checkpoint()
            return success(tool_call_id, 1)
        finally:
            active -= 1

    def blocked_attempt(number: int) -> Callable[[CancellationToken], Awaitable[ToolResult]]:
        async def attempt(token: CancellationToken) -> ToolResult:
            return await blocked(f"call_{number}", token)

        return attempt

    first = asyncio.create_task(
        first_scheduler.execute_batch(
            tuple(scheduled(call(read, number), read, blocked_attempt(number)) for number in (1, 2)),
            ManualCancellationToken(),
        )
    )
    second = asyncio.create_task(
        second_scheduler.execute_batch(
            tuple(scheduled(call(read, number), read, blocked_attempt(number)) for number in (3, 4)),
            ManualCancellationToken(),
        )
    )

    await asyncio.wait_for(two_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert active == peak == 2

    release.set()
    await asyncio.gather(first, second)
    assert peak == 2


@pytest.mark.asyncio
async def test_worker_shared_effect_gate_never_overlaps_effectful_calls_from_independent_runs() -> None:
    gate = FairEffectGate(4)
    first_scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=4, effect_gate=gate)
    second_scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=4, effect_gate=gate)
    write = definition(
        "vault.write",
        effect=SideEffectClass.WRITE,
        risk=RiskClass.WRITE,
        concurrent=False,
    )
    first_barrier = ControlledBarrier("first-write")
    second_barrier = ControlledBarrier("second-write")

    async def gated(
        barrier: ControlledBarrier,
        result: ToolResult,
        token: CancellationToken,
    ) -> ToolResult:
        await barrier.arrive_and_wait(token)
        return result

    first = asyncio.create_task(
        first_scheduler.execute_batch(
            (scheduled(call(write, 1), write, lambda token: gated(first_barrier, success("call_1", 1), token)),),
            ManualCancellationToken(),
        )
    )
    await first_barrier.wait_for_arrivals(1)
    second = asyncio.create_task(
        second_scheduler.execute_batch(
            (scheduled(call(write, 2), write, lambda token: gated(second_barrier, success("call_2", 2), token)),),
            ManualCancellationToken(),
        )
    )
    await asyncio.sleep(0)
    assert second_barrier.arrivals == 0

    first_barrier.release()
    await second_barrier.wait_for_arrivals(1)
    second_barrier.release()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_worker_shared_effect_gate_is_fifo_and_later_reader_cannot_starve_writer() -> None:
    gate = FairEffectGate(2)
    first_scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=2, effect_gate=gate)
    writer_scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=2, effect_gate=gate)
    later_scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=2, effect_gate=gate)
    read = definition("workspace.read")
    write = definition(
        "vault.write",
        effect=SideEffectClass.WRITE,
        risk=RiskClass.WRITE,
        concurrent=False,
    )
    active_read = ControlledBarrier("active-read")
    queued_write = ControlledBarrier("queued-write")
    later_read = ControlledBarrier("later-read")

    async def gated(
        barrier: ControlledBarrier,
        result: ToolResult,
        token: CancellationToken,
    ) -> ToolResult:
        await barrier.arrive_and_wait(token)
        return result

    first = asyncio.create_task(
        first_scheduler.execute_batch(
            (scheduled(call(read, 1), read, lambda token: gated(active_read, success("call_1", 1), token)),),
            ManualCancellationToken(),
        )
    )
    await active_read.wait_for_arrivals(1)
    writer = asyncio.create_task(
        writer_scheduler.execute_batch(
            (scheduled(call(write, 2), write, lambda token: gated(queued_write, success("call_2", 2), token)),),
            ManualCancellationToken(),
        )
    )
    await asyncio.sleep(0)
    late = asyncio.create_task(
        later_scheduler.execute_batch(
            (scheduled(call(read, 3), read, lambda token: gated(later_read, success("call_3", 3), token)),),
            ManualCancellationToken(),
        )
    )
    await asyncio.sleep(0)
    assert queued_write.arrivals == 0 and later_read.arrivals == 0

    active_read.release()
    await queued_write.wait_for_arrivals(1)
    assert later_read.arrivals == 0
    queued_write.release()
    await later_read.wait_for_arrivals(1)
    later_read.release()
    await asyncio.gather(first, writer, late)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_worker_shared_effect_gate_capacity() -> None:
    gate = FairEffectGate(1)
    active_scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=1, effect_gate=gate)
    waiting_scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=1, effect_gate=gate)
    replacement_scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=1, effect_gate=gate)
    write = definition(
        "vault.write",
        effect=SideEffectClass.WRITE,
        risk=RiskClass.WRITE,
        concurrent=False,
    )
    active = ControlledBarrier("active")
    replacement = ControlledBarrier("replacement")

    async def active_attempt(token: CancellationToken) -> ToolResult:
        await active.arrive_and_wait(token)
        return success("call_1", 1)

    holder = asyncio.create_task(
        active_scheduler.execute_batch(
            (scheduled(call(write, 1), write, active_attempt),),
            ManualCancellationToken(),
        )
    )
    await active.wait_for_arrivals(1)

    waiting_token = ManualCancellationToken()
    waiting = asyncio.create_task(
        waiting_scheduler.execute_batch(
            (
                scheduled(
                    call(write, 2),
                    write,
                    lambda _token: asyncio.sleep(0, result=success("call_2", 2)),
                ),
            ),
            waiting_token,
        )
    )
    await asyncio.sleep(0)
    waiting_token.cancel()
    with pytest.raises(FakeRunCancelled):
        await asyncio.wait_for(waiting, timeout=1)

    async def replacement_attempt(token: CancellationToken) -> ToolResult:
        await replacement.arrive_and_wait(token)
        return success("call_3", 3)

    replacement_task = asyncio.create_task(
        replacement_scheduler.execute_batch(
            (
                scheduled(
                    call(write, 3),
                    write,
                    replacement_attempt,
                ),
            ),
            ManualCancellationToken(),
        )
    )
    active.release()
    await replacement.wait_for_arrivals(1)
    replacement.release()
    await asyncio.gather(holder, replacement_task)


@pytest.mark.asyncio
async def test_cancelling_waiting_writer_wakes_readers_without_releasing_active_read() -> None:
    scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=2)
    read = definition("workspace.read")
    write = definition(
        "vault.write",
        effect=SideEffectClass.WRITE,
        risk=RiskClass.WRITE,
        concurrent=False,
    )
    active_read = ControlledBarrier("active-read")
    waiting_write = ControlledBarrier("waiting-write")
    later_read = ControlledBarrier("later-read")
    token = ManualCancellationToken()

    async def gated(
        barrier: ControlledBarrier,
        result: ToolResult,
        current: CancellationToken,
    ) -> ToolResult:
        await barrier.arrive_and_wait(current)
        return result

    first = asyncio.create_task(
        scheduler.execute_batch(
            (scheduled(call(read, 1), read, lambda current: gated(active_read, success("call_1", 1), current)),),
            token,
        )
    )
    await active_read.wait_for_arrivals(1)
    writer = asyncio.create_task(
        scheduler.execute_batch(
            (scheduled(call(write, 2), write, lambda current: gated(waiting_write, success("call_2", 2), current)),),
            token,
        )
    )
    await asyncio.sleep(0)
    reader = asyncio.create_task(
        scheduler.execute_batch(
            (scheduled(call(read, 3), read, lambda current: gated(later_read, success("call_3", 3), current)),),
            token,
        )
    )
    await asyncio.sleep(0)
    assert waiting_write.arrivals == 0 and later_read.arrivals == 0

    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer
    await asyncio.wait_for(later_read.wait_for_arrivals(1), timeout=1)
    later_read.release()
    active_read.release()
    await asyncio.gather(first, reader)


@pytest.mark.asyncio
async def test_token_cancellation_while_waiting_for_effect_gate_never_prepares_invocation() -> None:
    scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=1)
    write = definition(
        "vault.write",
        effect=SideEffectClass.WRITE,
        risk=RiskClass.WRITE,
        concurrent=False,
    )
    active = ControlledBarrier("active-write")
    active_token = ManualCancellationToken()

    async def blocked(current: CancellationToken) -> ToolResult:
        await active.arrive_and_wait(current)
        return success("call_1", 1)

    first = asyncio.create_task(scheduler.execute_batch((scheduled(call(write, 1), write, blocked),), active_token))
    await active.wait_for_arrivals(1)

    prepared = 0

    async def prepare() -> ToolResult | None:
        nonlocal prepared
        prepared += 1
        return None

    async def should_not_execute(_: CancellationToken) -> ToolResult:
        raise AssertionError("cancelled queued invocation must not execute")

    async def finalize(result: ToolResult) -> ToolResult:
        return result

    aborted = 0

    async def abort() -> None:
        nonlocal aborted
        aborted += 1

    queued_call = call(write, 2)
    queued = ScheduledInvocation(
        queued_call,
        write,
        guard,
        prepare,
        should_not_execute,
        finalize,
        abort=abort,
    )
    queued_token = ManualCancellationToken()
    waiting = asyncio.create_task(scheduler.execute_batch((queued,), queued_token))
    await asyncio.sleep(0)
    queued_token.cancel()

    with pytest.raises(FakeRunCancelled):
        await asyncio.wait_for(waiting, timeout=1)
    assert prepared == 0
    assert aborted == 1

    active.release()
    await first


@pytest.mark.asyncio
async def test_sibling_exception_is_typed_and_does_not_cancel_other_read() -> None:
    read = definition("workspace.read")
    scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=2)

    async def fail(_: CancellationToken) -> ToolResult:
        raise RuntimeError("one sibling failed")

    async def succeed(_: CancellationToken) -> ToolResult:
        return success("call_2", 2)

    results = await scheduler.execute_batch(
        (
            scheduled(call(read, 1), read, fail),
            scheduled(call(read, 2), read, succeed),
        ),
        ManualCancellationToken(),
    )
    assert results[0].status is ToolResultStatus.FAILED
    assert results[1].status is ToolResultStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_effectful_result_callback_runs_before_the_next_write_can_start() -> None:
    write = definition(
        "vault.write",
        effect=SideEffectClass.WRITE,
        risk=RiskClass.WRITE,
        concurrent=False,
    )
    scheduler = ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=1)
    attempted: list[str] = []
    persisted: list[str] = []

    async def execute(current: CancellationToken, tool_call_id: str) -> ToolResult:
        current.checkpoint()
        attempted.append(tool_call_id)
        return success(tool_call_id, 1)

    invocations = (
        scheduled(call(write, 1), write, lambda token: execute(token, "call_1")),
        scheduled(call(write, 2), write, lambda token: execute(token, "call_2")),
    )

    async def persist(invocation: ScheduledInvocation, result: ToolResult) -> None:
        assert invocation.call.tool_call_id == result.tool_call_id
        persisted.append(result.tool_call_id)
        raise OSError("simulated result persistence failure")

    with pytest.raises(OSError, match="persistence"):
        await scheduler.execute_batch(
            invocations,
            ManualCancellationToken(),
            persist,
        )

    assert attempted == ["call_1"]
    assert persisted == ["call_1"]


@pytest.mark.asyncio
async def test_retry_requires_definition_idempotent_retryable_and_retryable_result() -> None:
    retry_tool = definition("workspace.read")
    scheduler = ToolScheduler(
        clock=ManualClock(NOW),
        max_parallel_reads=1,
        retry_policy=RetryPolicy(max_attempts=2, delays_seconds=(0,)),
    )
    attempts = 0

    async def flaky(_: CancellationToken) -> ToolResult:
        nonlocal attempts
        attempts += 1
        return retryable_failure("call_1") if attempts == 1 else success("call_1", 1)

    result = await scheduler.execute_batch(
        (scheduled(call(retry_tool, 1), retry_tool, flaky),),
        ManualCancellationToken(),
    )
    assert result[0].status is ToolResultStatus.SUCCEEDED
    assert attempts == 2

    no_retry = definition("vault.once", retryable=False)
    no_retry_attempts = 0

    async def always_retryable(_: CancellationToken) -> ToolResult:
        nonlocal no_retry_attempts
        no_retry_attempts += 1
        return retryable_failure("call_2")

    result = await scheduler.execute_batch(
        (scheduled(call(no_retry, 2), no_retry, always_retryable),),
        ManualCancellationToken(),
    )
    assert result[0].status is ToolResultStatus.FAILED
    assert no_retry_attempts == 1


@pytest.mark.asyncio
async def test_expired_deadline_never_invokes_and_effectful_timeout_is_unknown() -> None:
    write = definition(
        "vault.write",
        effect=SideEffectClass.WRITE,
        risk=RiskClass.WRITE,
        concurrent=False,
    )
    attempts = 0

    async def should_not_run(_: CancellationToken) -> ToolResult:
        nonlocal attempts
        attempts += 1
        return success("call_1", 1)

    result = await ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=1).execute_batch(
        (scheduled(call(write, 1, deadline=NOW), write, should_not_run),),
        ManualCancellationToken(),
    )
    assert attempts == 0
    assert result[0].status is ToolResultStatus.UNKNOWN_OUTCOME


@pytest.mark.asyncio
async def test_runtime_timeout_returns_typed_timeout_for_safe_read() -> None:
    read = definition("workspace.read", timeout_ms=20)
    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocked(_: CancellationToken) -> ToolResult:
        entered.set()
        await never.wait()
        return success("call_1", 1)

    run = asyncio.create_task(
        ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=1).execute_batch(
            (scheduled(call(read, 1), read, blocked),),
            ManualCancellationToken(),
        )
    )
    await entered.wait()
    result = await run
    assert result[0].status is ToolResultStatus.TIMED_OUT
    assert result[0].error is not None and result[0].error.code == "tool_timeout"


@pytest.mark.asyncio
async def test_timeout_does_not_wait_unbounded_for_executor_that_suppresses_task_cancel() -> None:
    read = definition("workspace.read", timeout_ms=10)
    started = asyncio.Event()
    cancel_seen = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def stubborn(_: CancellationToken) -> ToolResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_seen.set()
            await release.wait()
        finally:
            finished.set()
        return success("call_1", 1)

    run = asyncio.create_task(
        ToolScheduler(
            clock=ManualClock(NOW),
            max_parallel_reads=1,
            termination_grace_seconds=0.01,
        ).execute_batch(
            (scheduled(call(read, 1), read, stubborn),),
            ManualCancellationToken(),
        )
    )
    await started.wait()
    result = await asyncio.wait_for(run, timeout=1)
    assert result[0].status is ToolResultStatus.TIMED_OUT
    assert cancel_seen.is_set()
    assert not finished.is_set()

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_cancellation_interrupts_a_blocked_tool_without_waiting_for_timeout() -> None:
    read = definition("workspace.read", timeout_ms=60_000)
    barrier = ControlledBarrier("blocked-read")

    async def blocked(token: CancellationToken) -> ToolResult:
        await barrier.arrive_and_wait(token)
        return success("call_1", 1)

    token = ManualCancellationToken()
    task = asyncio.create_task(
        ToolScheduler(clock=ManualClock(NOW), max_parallel_reads=1).execute_batch(
            (scheduled(call(read, 1), read, blocked),),
            token,
        )
    )
    await barrier.wait_for_arrivals(1)
    token.cancel()
    with pytest.raises(FakeRunCancelled):
        await task


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        self.calls.append(tool_call)
        return success(tool_call.tool_call_id, 1)


class RecordingClient:
    def __init__(self) -> None:
        self.invocations: list[ClientToolInvocation] = []

    async def invoke(self, invocation: ClientToolInvocation, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        self.invocations.append(invocation)
        return success(invocation.call.tool_call_id, 1)

    async def cancel(self, invocation_id: str, reason: str) -> None:
        del invocation_id, reason

    async def lookup_result(self, invocation_id: str, *, run_id: str | None = None) -> ToolResult | None:
        del invocation_id, run_id
        return None


@pytest.mark.asyncio
async def test_dispatcher_routes_all_locations_and_client_invocation_identity_is_stable() -> None:
    local = RecordingExecutor()
    subagent = RecordingExecutor()
    client = RecordingClient()
    dispatcher = ToolDispatcher(clock=ManualClock(NOW), local=local, client=client, subagent=subagent)
    token = ManualCancellationToken()
    definitions = (
        definition("local.read", location=ExecutorLocation.LOCAL),
        definition("client.read", location=ExecutorLocation.CLIENT),
        definition("agent.read", location=ExecutorLocation.SUBAGENT),
    )
    calls = tuple(call(tool, index) for index, tool in enumerate(definitions, start=1))
    for tool, tool_call in zip(definitions, calls, strict=True):
        assert (await dispatcher.execute(tool, tool_call, token)).status is ToolResultStatus.SUCCEEDED

    assert local.calls == [calls[0]]
    assert subagent.calls == [calls[2]]
    assert client.invocations[0].invocation_id == dispatcher.client_invocation_id(calls[1])
    assert client.invocations[0].deadline == NOW + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_dispatcher_missing_route_fails_before_side_effect() -> None:
    tool = definition("workspace.read")
    with pytest.raises(DispatcherUnavailable) as error:
        await ToolDispatcher(clock=ManualClock(NOW)).execute(tool, call(tool, 1), ManualCancellationToken())
    assert error.value.side_effect_possible is False
