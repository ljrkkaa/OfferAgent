from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offeragent_harness.agent.budgets import BudgetDelta, BudgetExceeded, BudgetLedger, RunBudget


def budget() -> RunBudget:
    return RunBudget(
        max_model_rounds=8,
        max_tool_calls=20,
        max_parallel_reads=4,
        max_wall_seconds=60,
        max_input_tokens=10_000,
        max_output_tokens=4_000,
        max_cost=Decimal("5"),
        max_artifact_bytes=1_000_000,
        max_subagents=4,
        max_subagent_depth=2,
    )


@pytest.mark.asyncio
async def test_usage_and_reservations_share_one_atomic_ceiling() -> None:
    started = datetime.now(timezone.utc)
    ledger = BudgetLedger(budget(), started_at=started)
    await ledger.consume(BudgetDelta(model_rounds=1, output_tokens=500))
    reservation = await ledger.reserve(BudgetDelta(model_rounds=2, output_tokens=1_000, subagents=1))

    with pytest.raises(BudgetExceeded, match="output_tokens"):
        await ledger.consume(BudgetDelta(output_tokens=3_000))

    await reservation.consume(BudgetDelta(model_rounds=1, output_tokens=600, subagents=1))
    snapshot = await ledger.snapshot(now=started)
    assert snapshot.used == BudgetDelta(model_rounds=2, output_tokens=1_100, subagents=1)
    assert snapshot.reserved == BudgetDelta()


@pytest.mark.asyncio
async def test_unused_child_reservation_is_returned() -> None:
    started = datetime.now(timezone.utc)
    ledger = BudgetLedger(budget(), started_at=started)
    reservation = await ledger.reserve(BudgetDelta(tool_calls=10, subagents=1))
    await reservation.release()
    assert (await ledger.snapshot(now=started)).reserved == BudgetDelta()


@pytest.mark.asyncio
async def test_wall_time_is_checked_before_new_work() -> None:
    started = datetime.now(timezone.utc)
    ledger = BudgetLedger(budget(), started_at=started)
    with pytest.raises(BudgetExceeded) as error:
        await ledger.enforce_wall_time(now=started + timedelta(seconds=61))
    assert error.value.dimension == "wall_seconds"
