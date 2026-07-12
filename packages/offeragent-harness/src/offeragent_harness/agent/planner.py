from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from offeragent_harness.models import ModelUsage
from offeragent_harness.ports import CancellationToken
from offeragent_harness.tools import ToolCall

from .state import RunState


@dataclass(frozen=True, slots=True)
class PlanningStep:
    calls: tuple[ToolCall, ...]
    requires_write_outcome: bool
    stop_reason: str | None
    usage: ModelUsage | None = None

    def __post_init__(self) -> None:
        call_ids = [call.tool_call_id for call in self.calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("planning step contains duplicate tool_call_id values")
        if self.calls and self.stop_reason is not None:
            raise ValueError("a planning step with calls cannot also declare stop_reason")


class Planner(Protocol):
    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep: ...


__all__ = ["Planner", "PlanningStep"]
