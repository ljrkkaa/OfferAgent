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
async def test_artifact_bytes_are_reserved_before_write_and_committed_exactly_once() -> None:
    started = datetime.now(timezone.utc)
    ledger = BudgetLedger(budget(), started_at=started)
    reservation = await ledger.reserve_artifact_bytes(900_000)

    with pytest.raises(BudgetExceeded, match="artifact_bytes"):
        await ledger.reserve_artifact_bytes(100_001)

    await reservation.commit()
    snapshot = await ledger.snapshot(now=started)
    assert snapshot.used.artifact_bytes == 900_000
    assert snapshot.reserved.artifact_bytes == 0


@pytest.mark.asyncio
async def test_restart_restores_usage_and_adopts_each_persisted_reservation_once() -> None:
    started = datetime.now(timezone.utc)
    restored = BudgetLedger.restore(
        budget(),
        started_at=started,
        used=BudgetDelta(model_rounds=2, tool_calls=3),
        reserved=BudgetDelta(model_rounds=1),
    )

    model_round = await restored.adopt_reservation(BudgetDelta(model_rounds=1))
    with pytest.raises(ValueError, match="does not contain"):
        await restored.adopt_reservation(BudgetDelta(model_rounds=1))
    await model_round.consume(BudgetDelta(model_rounds=1))

    snapshot = await restored.snapshot(now=started + timedelta(seconds=7))
    assert snapshot.used == BudgetDelta(model_rounds=3, tool_calls=3)
    assert snapshot.reserved == BudgetDelta()
    assert snapshot.elapsed_seconds == 7


@pytest.mark.asyncio
async def test_wall_time_is_checked_before_new_work() -> None:
    started = datetime.now(timezone.utc)
    ledger = BudgetLedger(budget(), started_at=started)
    with pytest.raises(BudgetExceeded) as error:
        await ledger.enforce_wall_time(now=started + timedelta(seconds=61))
    assert error.value.dimension == "wall_seconds"
