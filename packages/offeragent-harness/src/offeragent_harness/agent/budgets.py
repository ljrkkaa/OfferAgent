from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RunBudget:
    max_model_rounds: int
    max_tool_calls: int
    max_parallel_reads: int
    max_wall_seconds: float
    max_input_tokens: int
    max_output_tokens: int
    max_cost: Decimal
    max_artifact_bytes: int
    max_subagents: int

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_model_rounds,
            self.max_tool_calls,
            self.max_parallel_reads,
            self.max_input_tokens,
            self.max_output_tokens,
            self.max_artifact_bytes,
            self.max_subagents,
        )
        if any(value < 1 for value in integer_limits):
            raise ValueError("run budget limits must be positive")
        if self.max_parallel_reads > 256:
            raise ValueError("max_parallel_reads cannot exceed 256")
        if self.max_wall_seconds <= 0 or self.max_cost < 0:
            raise ValueError("wall time must be positive and cost cannot be negative")


@dataclass(frozen=True, slots=True)
class BudgetDelta:
    model_rounds: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal("0")
    artifact_bytes: int = 0
    subagents: int = 0

    def __post_init__(self) -> None:
        values = (
            self.model_rounds,
            self.tool_calls,
            self.input_tokens,
            self.output_tokens,
            self.artifact_bytes,
            self.subagents,
        )
        if any(value < 0 for value in values) or self.cost < 0:
            raise ValueError("budget deltas cannot be negative")

    def __add__(self, other: BudgetDelta) -> BudgetDelta:
        return BudgetDelta(
            model_rounds=self.model_rounds + other.model_rounds,
            tool_calls=self.tool_calls + other.tool_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost=self.cost + other.cost,
            artifact_bytes=self.artifact_bytes + other.artifact_bytes,
            subagents=self.subagents + other.subagents,
        )

    def subtract(self, other: BudgetDelta) -> BudgetDelta:
        result = BudgetDelta(
            model_rounds=self.model_rounds - other.model_rounds,
            tool_calls=self.tool_calls - other.tool_calls,
            input_tokens=self.input_tokens - other.input_tokens,
            output_tokens=self.output_tokens - other.output_tokens,
            cost=self.cost - other.cost,
            artifact_bytes=self.artifact_bytes - other.artifact_bytes,
            subagents=self.subagents - other.subagents,
        )
        return result


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    used: BudgetDelta
    reserved: BudgetDelta
    elapsed_seconds: float


class BudgetExceeded(RuntimeError):
    def __init__(self, dimension: str, *, limit: int | float | Decimal, attempted: int | float | Decimal) -> None:
        super().__init__(f"{dimension} budget exceeded: limit={limit}, attempted={attempted}")
        self.dimension = dimension
        self.limit = limit
        self.attempted = attempted


class BudgetReservation:
    def __init__(self, ledger: BudgetLedger, amount: BudgetDelta, *, adopted: bool = False) -> None:
        self._ledger = ledger
        self.amount = amount
        self._adopted = adopted
        self._closed = False

    async def consume(self, actual: BudgetDelta) -> None:
        if self._closed:
            raise RuntimeError("reservation is already closed")
        if not _fits(actual, self.amount):
            raise ValueError("actual usage exceeds reservation")
        await self._ledger._settle_reservation(self.amount, actual, adopted=self._adopted)
        self._closed = True

    async def release(self) -> None:
        if self._closed:
            return
        await self._ledger._settle_reservation(self.amount, BudgetDelta(), adopted=self._adopted)
        self._closed = True


class ArtifactByteReservation:
    """Narrow reservation facade exposed to Artifact-producing components."""

    def __init__(self, reservation: BudgetReservation, byte_length: int) -> None:
        self._reservation = reservation
        self._byte_length = byte_length

    async def commit(self) -> None:
        await self._reservation.consume(BudgetDelta(artifact_bytes=self._byte_length))

    async def release(self) -> None:
        await self._reservation.release()


class BudgetLedger:
    """Atomic root budget usage and child reservation ledger."""

    def __init__(self, budget: RunBudget, *, started_at: datetime) -> None:
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        self.budget = budget
        self.started_at = started_at
        self._used = BudgetDelta()
        self._reserved = BudgetDelta()
        self._adopted = BudgetDelta()
        self._lock = asyncio.Lock()

    @classmethod
    def restore(
        cls,
        budget: RunBudget,
        *,
        started_at: datetime,
        used: BudgetDelta,
        reserved: BudgetDelta,
    ) -> BudgetLedger:
        ledger = cls(budget, started_at=started_at)
        ledger._enforce(used + reserved)
        ledger._used = used
        ledger._reserved = reserved
        return ledger

    async def consume(self, delta: BudgetDelta) -> BudgetSnapshot:
        async with self._lock:
            attempted = self._used + self._reserved + delta
            self._enforce(attempted)
            self._used = self._used + delta
            return BudgetSnapshot(self._used, self._reserved, 0)

    async def reserve(self, amount: BudgetDelta) -> BudgetReservation:
        async with self._lock:
            attempted = self._used + self._reserved + amount
            self._enforce(attempted)
            self._reserved = self._reserved + amount
        return BudgetReservation(self, amount)

    async def adopt_reservation(self, amount: BudgetDelta) -> BudgetReservation:
        """Reclaim an already-persisted reservation after Worker recovery."""

        async with self._lock:
            available = self._reserved.subtract(self._adopted)
            if not _fits(amount, available):
                raise ValueError("persisted budget does not contain the requested reservation")
            self._adopted = self._adopted + amount
        return BudgetReservation(self, amount, adopted=True)

    async def reserve_artifact_bytes(self, byte_length: int) -> ArtifactByteReservation:
        if byte_length < 0:
            raise ValueError("artifact byte length cannot be negative")
        reservation = await self.reserve(BudgetDelta(artifact_bytes=byte_length))
        return ArtifactByteReservation(reservation, byte_length)

    async def _settle_reservation(
        self,
        reserved: BudgetDelta,
        actual: BudgetDelta,
        *,
        adopted: bool,
    ) -> None:
        async with self._lock:
            self._reserved = self._reserved.subtract(reserved)
            if adopted:
                self._adopted = self._adopted.subtract(reserved)
            self._used = self._used + actual
            self._enforce(self._used + self._reserved)

    async def snapshot(self, *, now: datetime) -> BudgetSnapshot:
        async with self._lock:
            elapsed = self._elapsed(now)
            return BudgetSnapshot(self._used, self._reserved, elapsed)

    async def enforce_wall_time(self, *, now: datetime) -> None:
        elapsed = self._elapsed(now)
        if elapsed > self.budget.max_wall_seconds:
            raise BudgetExceeded("wall_seconds", limit=self.budget.max_wall_seconds, attempted=elapsed)

    def _elapsed(self, now: datetime) -> float:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return max(0.0, (now - self.started_at).total_seconds())

    def _enforce(self, attempted: BudgetDelta) -> None:
        limits: tuple[tuple[str, int | Decimal, int | Decimal], ...] = (
            ("model_rounds", self.budget.max_model_rounds, attempted.model_rounds),
            ("tool_calls", self.budget.max_tool_calls, attempted.tool_calls),
            ("input_tokens", self.budget.max_input_tokens, attempted.input_tokens),
            ("output_tokens", self.budget.max_output_tokens, attempted.output_tokens),
            ("cost", self.budget.max_cost, attempted.cost),
            ("artifact_bytes", self.budget.max_artifact_bytes, attempted.artifact_bytes),
            ("subagents", self.budget.max_subagents, attempted.subagents),
        )
        for dimension, limit, value in limits:
            if value > limit:
                raise BudgetExceeded(dimension, limit=limit, attempted=value)


def _fits(value: BudgetDelta, ceiling: BudgetDelta) -> bool:
    return all(
        current <= maximum
        for current, maximum in (
            (value.model_rounds, ceiling.model_rounds),
            (value.tool_calls, ceiling.tool_calls),
            (value.input_tokens, ceiling.input_tokens),
            (value.output_tokens, ceiling.output_tokens),
            (value.cost, ceiling.cost),
            (value.artifact_bytes, ceiling.artifact_bytes),
            (value.subagents, ceiling.subagents),
        )
    )
