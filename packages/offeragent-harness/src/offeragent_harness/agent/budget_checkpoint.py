"""Strict durable checkpoint for one Run's complete budget ledger."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .budgets import BudgetDelta, BudgetLedger, RunBudget


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_nonnegative_decimal(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


def _validate_delta(delta: BudgetDelta, name: str) -> None:
    integer_fields = (
        ("model_rounds", delta.model_rounds),
        ("tool_calls", delta.tool_calls),
        ("input_tokens", delta.input_tokens),
        ("output_tokens", delta.output_tokens),
        ("artifact_bytes", delta.artifact_bytes),
        ("subagents", delta.subagents),
    )
    for field_name, value in integer_fields:
        if type(value) is not int:
            raise TypeError(f"{name}.{field_name} must be an integer")
        if value < 0:
            raise ValueError(f"{name}.{field_name} cannot be negative")
    _require_nonnegative_decimal(delta.cost, f"{name}.cost")


def _validate_budget(budget: RunBudget) -> None:
    positive_integer_fields = (
        ("max_model_rounds", budget.max_model_rounds),
        ("max_tool_calls", budget.max_tool_calls),
        ("max_parallel_reads", budget.max_parallel_reads),
        ("max_input_tokens", budget.max_input_tokens),
        ("max_output_tokens", budget.max_output_tokens),
        ("max_artifact_bytes", budget.max_artifact_bytes),
        ("max_subagents", budget.max_subagents),
    )
    for name, value in positive_integer_fields:
        if type(value) is not int:
            raise TypeError(f"budget.{name} must be an integer")
        if value < 1:
            raise ValueError(f"budget.{name} must be positive")
    if isinstance(budget.max_wall_seconds, bool) or not isinstance(budget.max_wall_seconds, (int, float)):
        raise TypeError("budget.max_wall_seconds must be a number")
    if not math.isfinite(float(budget.max_wall_seconds)) or budget.max_wall_seconds <= 0:
        raise ValueError("budget.max_wall_seconds must be finite and positive")
    _require_nonnegative_decimal(budget.max_cost, "budget.max_cost")


def _validate_ceiling(budget: RunBudget, used: BudgetDelta, reserved: BudgetDelta) -> None:
    total = used + reserved
    dimensions: tuple[tuple[str, int | Decimal, int | Decimal], ...] = (
        ("model_rounds", budget.max_model_rounds, total.model_rounds),
        ("tool_calls", budget.max_tool_calls, total.tool_calls),
        ("input_tokens", budget.max_input_tokens, total.input_tokens),
        ("output_tokens", budget.max_output_tokens, total.output_tokens),
        ("cost", budget.max_cost, total.cost),
        ("artifact_bytes", budget.max_artifact_bytes, total.artifact_bytes),
        ("subagents", budget.max_subagents, total.subagents),
    )
    for name, limit, value in dimensions:
        if value > limit:
            raise ValueError(f"used + reserved {name} exceeds the persisted budget limit")


@dataclass(frozen=True, slots=True)
class BudgetCheckpoint:
    """A lossless point-in-time budget ledger suitable for crash recovery."""

    budget: RunBudget
    started_at: datetime
    used: BudgetDelta
    reserved: BudgetDelta
    captured_at: datetime
    elapsed_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.budget, RunBudget):
            raise TypeError("budget must be a RunBudget")
        if not isinstance(self.used, BudgetDelta) or not isinstance(self.reserved, BudgetDelta):
            raise TypeError("used and reserved must be BudgetDelta values")
        _validate_budget(self.budget)
        _validate_delta(self.used, "used")
        _validate_delta(self.reserved, "reserved")
        _validate_ceiling(self.budget, self.used, self.reserved)
        _require_aware(self.started_at, "started_at")
        _require_aware(self.captured_at, "captured_at")
        if self.captured_at < self.started_at:
            raise ValueError("captured_at cannot precede started_at")
        if isinstance(self.elapsed_seconds, bool) or not isinstance(self.elapsed_seconds, (int, float)):
            raise TypeError("elapsed_seconds must be a number")
        elapsed = float(self.elapsed_seconds)
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("elapsed_seconds must be finite and non-negative")
        expected = (self.captured_at - self.started_at).total_seconds()
        if not math.isclose(elapsed, expected, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("elapsed_seconds must match captured_at - started_at")
        object.__setattr__(self, "elapsed_seconds", elapsed)

    @classmethod
    async def capture(cls, ledger: BudgetLedger, *, now: datetime) -> BudgetCheckpoint:
        snapshot = await ledger.snapshot(now=now)
        return cls(
            budget=ledger.budget,
            started_at=ledger.started_at,
            used=snapshot.used,
            reserved=snapshot.reserved,
            captured_at=now,
            elapsed_seconds=snapshot.elapsed_seconds,
        )

    def restore_ledger(self) -> BudgetLedger:
        return BudgetLedger.restore(
            self.budget,
            started_at=self.started_at,
            used=self.used,
            reserved=self.reserved,
        )


__all__ = ["BudgetCheckpoint"]
