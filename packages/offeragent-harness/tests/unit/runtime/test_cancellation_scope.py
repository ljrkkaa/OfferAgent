from __future__ import annotations

import asyncio

import pytest

from offeragent_harness.runtime.cancellation import (
    CancellationCode,
    CancellationReason,
    CancellationScope,
    RunCancelled,
)


@pytest.mark.asyncio
async def test_parent_cancellation_reaches_existing_and_future_children() -> None:
    parent = CancellationScope(name="root")
    child = parent.child("tool")
    reason = CancellationReason.now(CancellationCode.USER, "stop")

    assert await parent.cancel(reason) is True
    assert await parent.cancel(reason) is False
    assert child.cancelled
    assert child.reason is not None and child.reason.code is CancellationCode.PARENT

    future_child = parent.child("late")
    assert future_child.cancelled
    with pytest.raises(RunCancelled):
        future_child.checkpoint()


@pytest.mark.asyncio
async def test_cancellation_callback_failure_does_not_block_siblings() -> None:
    scope = CancellationScope(name="root")
    seen: list[str] = []

    async def broken(_: CancellationReason) -> None:
        raise RuntimeError("cleanup failed")

    async def healthy(_: CancellationReason) -> None:
        seen.append("healthy")

    scope.add_callback(broken)
    scope.add_callback(healthy)
    await scope.cancel(CancellationReason.now(CancellationCode.SHUTDOWN, "shutdown"))
    assert seen == ["healthy"]


@pytest.mark.asyncio
async def test_scheduled_deadline_cancels_scope() -> None:
    scope = CancellationScope(name="model")
    deadline = scope.schedule_deadline(0.01)
    reason = await asyncio.wait_for(scope.wait(), timeout=1)
    await deadline
    assert reason.code is CancellationCode.DEADLINE
