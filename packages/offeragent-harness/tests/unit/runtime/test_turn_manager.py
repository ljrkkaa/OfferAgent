from __future__ import annotations

import asyncio
import gc
from datetime import timedelta

import pytest

from offeragent_harness.observability import MetricName, MetricsRegistry, ProductionRunObservability
from offeragent_harness.runtime.cancellation import CancellationCode, CancellationReason, CancellationScope
from offeragent_harness.runtime.turn_manager import SessionRunConflict, TurnManager
from offeragent_harness.testing import ManualClock


@pytest.mark.asyncio
async def test_one_root_run_per_session_and_cancel_propagation() -> None:
    manager = TurnManager()
    entered = asyncio.Event()

    async def run(scope: CancellationScope) -> None:
        entered.set()
        await scope.wait()
        scope.checkpoint()

    first = await manager.start(session_id="s1", run_id="r1", factory=run)
    await entered.wait()
    with pytest.raises(SessionRunConflict):
        await manager.start(session_id="s1", run_id="r2", factory=run)

    await manager.cancel("r1", CancellationReason.now(CancellationCode.USER, "cancel"))
    with pytest.raises(asyncio.CancelledError):
        await first.task


@pytest.mark.asyncio
async def test_different_sessions_share_bounded_scheduler() -> None:
    manager = TurnManager(max_active_runs=1)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_run(_scope: CancellationScope) -> None:
        first_entered.set()
        await release_first.wait()

    async def second_run(_scope: CancellationScope) -> None:
        second_entered.set()

    one = await manager.start(session_id="s1", run_id="r1", factory=first_run)
    two = await manager.start(session_id="s2", run_id="r2", factory=second_run)
    await first_entered.wait()
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    release_first.set()
    await one.task
    await two.task
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_settle_terminal_waits_for_cleanup_but_never_waits_for_a_live_run() -> None:
    manager = TurnManager()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def finishing(_scope: CancellationScope) -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    first = await manager.start(session_id="s1", run_id="r1", factory=finishing)
    await cleanup_started.wait()

    async def not_terminal(_run_id: str) -> bool:
        return False

    assert not await manager.settle_terminal("s1", not_terminal)
    with pytest.raises(SessionRunConflict):
        await manager.start(session_id="s1", run_id="r2", factory=finishing)

    observed: list[str] = []

    async def terminal(run_id: str) -> bool:
        observed.append(run_id)
        return True

    settling = asyncio.create_task(manager.settle_terminal("s1", terminal))
    await asyncio.sleep(0)
    assert not settling.done()
    release_cleanup.set()
    assert await settling
    assert observed == ["r1"]
    await first.task

    async def next_run(_scope: CancellationScope) -> None:
        return None

    second = await manager.start(session_id="s1", run_id="r2", factory=next_run)
    await second.task


@pytest.mark.asyncio
async def test_turn_manager_observer_records_active_count_and_cleanup_complete_cancel_latency() -> None:
    clock = ManualClock()
    metrics = MetricsRegistry()
    observer = ProductionRunObservability(clock=clock, metrics=metrics)
    manager = TurnManager(observer=observer)
    entered = asyncio.Event()

    async def run(scope: CancellationScope) -> None:
        entered.set()
        await scope.wait()
        clock.advance(timedelta(milliseconds=275))

    active = await manager.start(session_id="s1", run_id="r1", factory=run)
    await entered.wait()
    assert await manager.cancel("r1", CancellationReason.now(CancellationCode.USER, "cancel"))
    await active.task
    for _ in range(10):
        if not await manager.active_runs():
            break
        await asyncio.sleep(0)

    rows = {item.name: item for item in metrics.snapshots()}
    assert rows[MetricName.CANCELLATION_LATENCY_MS].p50 == 275
    assert rows[MetricName.ACTIVE_RUNS].current == 0

    observer.run_depth_registered(1)
    observer.run_depth_registered(3)
    observer.run_depth_registered(2)
    assert {item.name: item for item in metrics.snapshots()}[MetricName.SUBAGENT_DEPTH].current == 3


@pytest.mark.asyncio
async def test_failed_background_run_is_observed_before_manager_releases_it() -> None:
    manager = TurnManager()
    reported: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:

        async def fails(_scope: CancellationScope) -> None:
            raise RuntimeError("expected run failure")

        active = await manager.start(session_id="s1", run_id="r1", factory=fails)
        for _ in range(10):
            if not await manager.active_runs():
                break
            await asyncio.sleep(0)
        del active
        gc.collect()
        await asyncio.sleep(0)
        assert reported == []
    finally:
        loop.set_exception_handler(previous_handler)
