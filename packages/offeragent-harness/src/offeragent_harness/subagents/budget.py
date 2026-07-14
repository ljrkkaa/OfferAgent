"""Root-wide Subagent budget reservations backed by the authoritative BudgetLedger."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from offeragent_harness.agent import BudgetDelta, BudgetLedger
from offeragent_harness.agent.budgets import BudgetReservation

from .models import AgentBudget, AgentUsage


class SubagentBudgetError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ChildBudgetReservation:
    def __init__(self, reservation: BudgetReservation, limit: AgentBudget) -> None:
        self._reservation = reservation
        self.limit = limit
        self._closed = False

    async def settle(self, usage: AgentUsage) -> None:
        if self._closed:
            return
        if not usage.fits_within(self.limit):
            raise SubagentBudgetError("usage_exceeded_reservation", "child usage exceeds its budget reservation")
        await self._reservation.consume(_usage_delta(usage))
        self._closed = True

    async def release(self) -> None:
        if self._closed:
            return
        await self._reservation.release()
        self._closed = True


class SubagentBudgetTree:
    def __init__(
        self,
        ledger: BudgetLedger | Callable[[str], BudgetLedger],
        *,
        retained_final_budget: AgentBudget,
        hard_max_depth: int = 3,
    ) -> None:
        if not 1 <= hard_max_depth <= 3:
            raise ValueError("hard Subagent depth must be 1..3")
        self._ledger = ledger if isinstance(ledger, BudgetLedger) else None
        self._ledger_for_root = ledger if callable(ledger) else None
        self._retained = retained_final_budget
        self._hard_depth = hard_max_depth

    async def reserve(
        self,
        requested: AgentBudget,
        *,
        parent_remaining: AgentBudget,
        child_depth: int,
        root_run_id: str | None = None,
    ) -> ChildBudgetReservation:
        if child_depth > self._hard_depth:
            raise SubagentBudgetError("depth_limit", "Subagent hard depth limit exceeded")
        if not requested.fits_within(parent_remaining):
            raise SubagentBudgetError("parent_budget", "requested child budget exceeds parent remaining budget")
        if not _retains(parent_remaining, requested, self._retained):
            raise SubagentBudgetError("final_budget", "spawn would consume the retained final-compose/error budget")
        reservation = await self._resolve_ledger(root_run_id).reserve(_budget_delta(requested))
        return ChildBudgetReservation(reservation, requested)

    async def adopt(
        self,
        requested: AgentBudget,
        *,
        root_run_id: str | None = None,
    ) -> ChildBudgetReservation:
        reservation = await self._resolve_ledger(root_run_id).adopt_reservation(_budget_delta(requested))
        return ChildBudgetReservation(reservation, requested)

    def _resolve_ledger(self, root_run_id: str | None) -> BudgetLedger:
        if self._ledger is not None:
            return self._ledger
        if root_run_id is None or not root_run_id:
            raise SubagentBudgetError(
                "root_ledger_unavailable",
                "root Run identity is required for a routed Subagent budget reservation",
            )
        resolver = self._ledger_for_root
        assert resolver is not None
        try:
            ledger = resolver(root_run_id)
        except (KeyError, LookupError) as error:
            raise SubagentBudgetError(
                "root_ledger_unavailable",
                "authoritative root Run budget ledger is unavailable",
            ) from error
        if not isinstance(ledger, BudgetLedger):
            raise SubagentBudgetError(
                "root_ledger_unavailable",
                "root Run budget resolver returned an invalid ledger",
            )
        return ledger


def _retains(remaining: AgentBudget, requested: AgentBudget, retained: AgentBudget) -> bool:
    return all(
        maximum - value >= reserve
        for maximum, value, reserve in zip(
            remaining.as_tuple(),
            requested.as_tuple(),
            retained.as_tuple(),
            strict=True,
        )
    )


def _budget_delta(value: AgentBudget) -> BudgetDelta:
    return BudgetDelta(
        model_rounds=value.model_calls,
        tool_calls=value.tool_calls,
        input_tokens=value.input_tokens,
        output_tokens=value.output_tokens,
        cost=Decimal(value.cost_micros) / Decimal(1_000_000),
        artifact_bytes=value.artifact_bytes,
        subagents=1,
    )


def _usage_delta(value: AgentUsage) -> BudgetDelta:
    return BudgetDelta(
        model_rounds=value.model_calls,
        tool_calls=value.tool_calls,
        input_tokens=value.input_tokens,
        output_tokens=value.output_tokens,
        cost=Decimal(value.cost_micros) / Decimal(1_000_000),
        artifact_bytes=value.artifact_bytes,
        subagents=1,
    )


__all__ = ["ChildBudgetReservation", "SubagentBudgetError", "SubagentBudgetTree"]
