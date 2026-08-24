"""Explicitly classified, correlation-complete observability values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from offeragent_harness.models.json_types import JsonValue, freeze_json

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class DataClass(str, Enum):
    PUBLIC = "public"
    IDENTIFIER = "identifier"
    METRIC = "metric"
    PATH = "path"
    CONTENT = "content"
    SECRET = "secret"


@dataclass(frozen=True, slots=True)
class LogField:
    value: Any
    classification: DataClass

    def __post_init__(self) -> None:
        if self.classification in {DataClass.PUBLIC, DataClass.IDENTIFIER, DataClass.METRIC}:
            freeze_json(self.value)


@dataclass(frozen=True, slots=True)
class TraceCorrelation:
    trace_id: str
    workspace_id: str
    session_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "trace_id",
            "workspace_id",
            "session_id",
            "turn_id",
            "run_id",
            "parent_run_id",
            "tool_call_id",
        ):
            value = getattr(self, name)
            if value is not None and _ID.fullmatch(value) is None:
                raise ValueError(f"invalid observability correlation {name}")
        if self.parent_run_id is not None and self.run_id is None:
            raise ValueError("parent Run correlation requires run_id")
        if self.tool_call_id is not None and self.run_id is None:
            raise ValueError("tool correlation requires run_id")

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            "traceId": self.trace_id,
            "workspaceId": self.workspace_id,
            "sessionId": self.session_id,
            "turnId": self.turn_id,
            "runId": self.run_id,
            "parentRunId": self.parent_run_id,
            "toolCallId": self.tool_call_id,
        }


__all__ = ["DataClass", "LogField", "LogLevel", "TraceCorrelation"]
