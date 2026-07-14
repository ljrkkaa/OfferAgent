from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offeragent_harness.agent.budget_checkpoint import BudgetCheckpoint
from offeragent_harness.agent.budgets import BudgetDelta, BudgetLedger, RunBudget
from offeragent_harness.agent.state import RunState
from offeragent_harness.sessions import AgentLineage


def budget() -> RunBudget:
    return RunBudget(
        max_model_rounds=6,
        max_tool_calls=12,
        max_parallel_reads=3,
        max_wall_seconds=90.5,
        max_input_tokens=10_000,
        max_output_tokens=5_000,
        max_cost=Decimal("12.50"),
        max_artifact_bytes=2_000_000,
        max_subagents=4,
        max_subagent_depth=2,
    )


@pytest.mark.asyncio
async def test_capture_and_restore_preserve_usage_reservations_and_absolute_wall_clock() -> None:
    started = datetime(2026, 7, 13, 1, 2, 3, tzinfo=timezone.utc)
    ledger = BudgetLedger(budget(), started_at=started)
    await ledger.consume(
        BudgetDelta(
            model_rounds=2,
            tool_calls=3,
            input_tokens=1_200,
            output_tokens=400,
            cost=Decimal("1.25"),
            artifact_bytes=2_048,
        )
    )
    await ledger.reserve(
        BudgetDelta(
            model_rounds=1,
            output_tokens=800,
            cost=Decimal("0.75"),
            subagents=1,
        )
    )

    checkpoint = await BudgetCheckpoint.capture(ledger, now=started + timedelta(seconds=7.25))
    assert checkpoint.budget == budget()
    assert checkpoint.started_at == started
    assert checkpoint.elapsed_seconds == 7.25
    assert checkpoint.used.cost == Decimal("1.25")
    assert checkpoint.reserved.cost == Decimal("0.75")

    restored = checkpoint.restore_ledger()
    restored_snapshot = await restored.snapshot(now=started + timedelta(seconds=12))
    assert restored_snapshot.used == checkpoint.used
    assert restored_snapshot.reserved == checkpoint.reserved
    assert restored_snapshot.elapsed_seconds == 12

    adopted = await restored.adopt_reservation(checkpoint.reserved)
    await adopted.consume(BudgetDelta(model_rounds=1, output_tokens=700, cost=Decimal("0.50"), subagents=1))
    settled = await restored.snapshot(now=started + timedelta(seconds=13))
    assert settled.reserved == BudgetDelta()
    assert settled.used.model_rounds == 3
    assert settled.used.cost == Decimal("1.75")


def test_checkpoint_rejects_budget_overflow_and_inconsistent_time() -> None:
    started = datetime(2026, 7, 13, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match=r"used \+ reserved model_rounds"):
        BudgetCheckpoint(
            budget=budget(),
            started_at=started,
            used=BudgetDelta(model_rounds=6),
            reserved=BudgetDelta(model_rounds=1),
            captured_at=started,
            elapsed_seconds=0,
        )

    with pytest.raises(ValueError, match="must match"):
        BudgetCheckpoint(
            budget=budget(),
            started_at=started,
            used=BudgetDelta(),
            reserved=BudgetDelta(),
            captured_at=started + timedelta(seconds=5),
            elapsed_seconds=4,
        )

    with pytest.raises(ValueError, match="cannot precede"):
        BudgetCheckpoint(
            budget=budget(),
            started_at=started,
            used=BudgetDelta(),
            reserved=BudgetDelta(),
            captured_at=started - timedelta(microseconds=1),
            elapsed_seconds=0,
        )


@pytest.mark.parametrize(
    ("invalid_budget", "invalid_delta", "expected"),
    [
        (
            RunBudget(6, 12, 3, float("inf"), 10_000, 5_000, Decimal("1"), 100, 2, 1),
            BudgetDelta(),
            "max_wall_seconds",
        ),
        (
            RunBudget(6, 12, 3, 10, 10_000, 5_000, 1, 100, 2, 1),  # type: ignore[arg-type]
            BudgetDelta(),
            "max_cost must be Decimal",
        ),
        (
            budget(),
            BudgetDelta(cost=0),  # type: ignore[arg-type]
            "used.cost must be Decimal",
        ),
        (
            budget(),
            BudgetDelta(model_rounds=True),
            "used.model_rounds must be an integer",
        ),
    ],
)
def test_checkpoint_enforces_finite_typed_limits_and_deltas(
    invalid_budget: RunBudget,
    invalid_delta: BudgetDelta,
    expected: str,
) -> None:
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    with pytest.raises((TypeError, ValueError), match=expected):
        BudgetCheckpoint(invalid_budget, now, invalid_delta, BudgetDelta(), now, 0)


def test_checkpoint_requires_aware_timestamps_and_run_state_marks_missing_checkpoint() -> None:
    aware = datetime(2026, 7, 13, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="started_at must be timezone-aware"):
        BudgetCheckpoint(budget(), aware.replace(tzinfo=None), BudgetDelta(), BudgetDelta(), aware, 0)

    state = RunState("ws", "session", "turn", "run", AgentLineage.root("run"))
    assert state.budget_checkpoint is None
