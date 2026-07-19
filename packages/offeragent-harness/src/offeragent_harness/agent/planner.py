from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from offeragent_harness.models import ModelCitation, ModelContinuation, ModelUsage
from offeragent_harness.ports import CancellationToken
from offeragent_harness.tools import ToolCall

from .state import RunState


class PlanningAttemptOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    INVALID = "invalid"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PlanningAttempt:
    request_id: str
    repair_index: int
    outcome: PlanningAttemptOutcome
    usage: ModelUsage | None
    error_code: str | None = None
    violations: tuple[str, ...] = ()
    retry_of_request_id: str | None = None
    projection: str | None = None
    projection_hash: str | None = None
    omitted_context_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("planning attempt request_id must not be empty")
        if self.repair_index < 0:
            raise ValueError("planning repair_index cannot be negative")
        if self.retry_of_request_id == self.request_id:
            raise ValueError("planning retry request must have a new request_id")
        if len(self.omitted_context_ids) != len(set(self.omitted_context_ids)):
            raise ValueError("planning omitted context IDs must be unique")
        if self.projection_hash is not None and (
            len(self.projection_hash) != 71
            or not self.projection_hash.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in self.projection_hash[7:])
        ):
            raise ValueError("planning projection hash must be a canonical sha256 digest")
        if self.outcome is PlanningAttemptOutcome.SUCCEEDED:
            if self.usage is None or self.error_code is not None or self.violations:
                raise ValueError("successful planning attempts require usage and no error fields")
        elif self.outcome is PlanningAttemptOutcome.INVALID:
            if self.usage is None or not self.violations:
                raise ValueError("invalid planning attempts require usage and schema violations")
        elif self.error_code is None:
            raise ValueError("failed planning attempts require an error_code")


@dataclass(frozen=True, slots=True)
class PlanningStep:
    calls: tuple[ToolCall, ...]
    requires_write_outcome: bool
    final_response: str | None
    attempts: tuple[PlanningAttempt, ...] = ()
    citations: tuple[ModelCitation, ...] = ()
    agent_step_id: str | None = None
    continuation: ModelContinuation | None = None

    def __post_init__(self) -> None:
        call_ids = [call.tool_call_id for call in self.calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("planning step contains duplicate tool_call_id values")
        if self.calls and self.final_response is not None:
            raise ValueError("a planning step with calls cannot also contain a final response")
        if not self.calls and (self.final_response is None or not self.final_response.strip()):
            raise ValueError("a planning step without calls requires a non-empty final response")
        if self.attempts:
            request_ids = [attempt.request_id for attempt in self.attempts]
            if len(request_ids) != len(set(request_ids)):
                raise ValueError("planning attempts contain duplicate request IDs")
            if self.attempts[-1].outcome is not PlanningAttemptOutcome.SUCCEEDED:
                raise ValueError("a successful PlanningStep must end in a successful attempt")
            if tuple(attempt.repair_index for attempt in self.attempts) != tuple(range(len(self.attempts))):
                raise ValueError("planning repair indexes must be contiguous from zero")
        if len(self.citations) > 256 or len(self.citations) != len(set(self.citations)):
            raise ValueError("planning citations must be bounded and unique")
        if (self.agent_step_id is None) != (self.continuation is None):
            raise ValueError("planning step identity and Provider continuation must be present together")
        if self.agent_step_id is not None and (
            not self.agent_step_id or len(self.agent_step_id) > 256 or "\x00" in self.agent_step_id
        ):
            raise ValueError("planning AgentStep identity must be bounded non-empty text")


@runtime_checkable
class AuditedPlanningFailure(Protocol):
    @property
    def planning_attempts(self) -> tuple[PlanningAttempt, ...]: ...


class Planner(Protocol):
    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep: ...


__all__ = [
    "AuditedPlanningFailure",
    "Planner",
    "PlanningAttempt",
    "PlanningAttemptOutcome",
    "PlanningStep",
]
