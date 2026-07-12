"""Subagent scheduling and result value objects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json
from offeragent_harness.permissions import CapabilityScope
from offeragent_harness.sessions import AgentLineage


class ContextForkMode(str, Enum):
    NONE = "none"
    SUMMARY = "summary"
    SELECTED = "selected"
    FULL = "full"


class SubagentLifetime(str, Enum):
    PARENT = "parent"
    TURN = "turn"
    SESSION = "session"


@dataclass(frozen=True)
class AgentBudget:
    input_tokens: int
    output_tokens: int
    model_calls: int
    tool_calls: int
    wall_time_seconds: float
    artifact_bytes: int
    child_count: int

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.model_calls,
                self.tool_calls,
                self.wall_time_seconds,
                self.artifact_bytes,
                self.child_count,
            )
        ):
            raise ValueError("agent budgets cannot be negative")


@dataclass(frozen=True)
class SubagentSpawnRequest:
    spawn_call_id: str
    parent_lineage: AgentLineage
    child_run_id: str
    task: str
    profile: str
    context_mode: ContextForkMode
    selected_message_ids: tuple[str, ...]
    selected_artifact_ids: tuple[str, ...]
    requested_scope: CapabilityScope
    budget: AgentBudget
    lifetime: SubagentLifetime
    deadline_at: datetime

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("subagent task must not be empty")
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise ValueError("subagent deadline must be timezone-aware")


@dataclass(frozen=True)
class SubagentResult:
    run_id: str
    status: str
    summary: str
    findings: tuple[Mapping[str, Any], ...]
    evidence: tuple[Mapping[str, Any], ...]
    artifact_ids: tuple[str, ...]
    proposed_actions: tuple[Mapping[str, Any], ...]
    unresolved_questions: tuple[str, ...]
    usage: Mapping[str, Any]
    error: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(freeze_json(item) for item in self.findings))
        object.__setattr__(self, "evidence", tuple(freeze_json(item) for item in self.evidence))
        object.__setattr__(self, "proposed_actions", tuple(freeze_json(item) for item in self.proposed_actions))
        usage = freeze_json(self.usage)
        if not isinstance(usage, FrozenJsonObject):
            raise TypeError("subagent usage must be a JSON object")
        object.__setattr__(self, "usage", usage)
        if self.error is not None:
            error = freeze_json(self.error)
            if not isinstance(error, FrozenJsonObject):
                raise TypeError("subagent error must be a JSON object")
            object.__setattr__(self, "error", error)


__all__ = [
    "AgentBudget",
    "ContextForkMode",
    "SubagentLifetime",
    "SubagentResult",
    "SubagentSpawnRequest",
]
