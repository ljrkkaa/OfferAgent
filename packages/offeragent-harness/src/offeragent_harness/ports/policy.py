"""Pure policy evaluation boundary."""

from typing import Protocol, runtime_checkable

from offeragent_harness.permissions import PolicyContext, PolicyDecision
from offeragent_harness.tools import ToolCall, ToolDefinition


@runtime_checkable
class PolicyEvaluator(Protocol):
    async def evaluate(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
    ) -> PolicyDecision: ...


__all__ = ["PolicyEvaluator"]
