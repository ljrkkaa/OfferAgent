from __future__ import annotations

import asyncio

import pytest

from offeragent_harness.runtime.cancellation import CancellationCode, CancellationReason, CancellationScope
from offeragent_harness.runtime.turn_manager import SessionRunConflict, TurnManager


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
