"""Typed tool outcomes and observable side effects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from offeragent_harness.foundation import MAX_AUDIT_EFFECTS, MAX_SOURCE_REFERENCES
from offeragent_harness.models.json_types import FrozenJsonObject, JsonValue, freeze_json

MAX_TOOL_RESULT_SOURCE_REFERENCES = MAX_SOURCE_REFERENCES


class ToolResultStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CONFLICTED = "conflicted"
    PARTIAL = "partial"
    UNKNOWN_OUTCOME = "unknown_outcome"


class SideEffectKind(str, Enum):
    READ = "read"
    NETWORK = "network"
    FILE_WRITE = "file_write"
    FILE_RENAME = "file_rename"
    FILE_TRASH = "file_trash"
    PROCESS = "process"
    EXTERNAL_SYSTEM = "external_system"
    SECRET_HANDLE_USE = "secret_handle_use"


class SideEffectState(str, Enum):
    OBSERVED = "observed"
    ATTEMPTED = "attempted"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SideEffect:
    kind: SideEffectKind
    state: SideEffectState
    resource_id: str
    before_state: JsonValue | None
    after_state: JsonValue | None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.resource_id:
            raise ValueError("side effects require a canonical resource_id")
        if self.before_state is not None:
            object.__setattr__(self, "before_state", freeze_json(self.before_state))
        if self.after_state is not None:
            object.__setattr__(self, "after_state", freeze_json(self.after_state))
        metadata = freeze_json(self.metadata)
        if not isinstance(metadata, FrozenJsonObject):
            raise TypeError("side effect metadata must be a JSON object")
        object.__setattr__(self, "metadata", metadata)


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    retryable: bool
    cancelled: bool
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("tool errors require code and message")
        details = freeze_json(self.details)
        if not isinstance(details, FrozenJsonObject):
            raise TypeError("tool error details must be a JSON object")
        object.__setattr__(self, "details", details)


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    status: ToolResultStatus
    data: JsonValue | None
    user_visible_summary: str
    artifact_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    side_effects: tuple[SideEffect, ...]
    retryable: bool
    before_state: JsonValue | None
    after_state: JsonValue | None
    error: ToolError | None
    # Protocol-shaped, user-displayable provenance. ``source_refs`` above is
    # intentionally retained as the opaque identifier set consumed by
    # Context/Memory/Compaction; it is not safe to project directly into UI.
    source_references: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.tool_call_id or not self.user_visible_summary:
            raise ValueError("tool result identity and summary must not be empty")
        if self.data is not None:
            object.__setattr__(self, "data", freeze_json(self.data))
        if self.before_state is not None:
            object.__setattr__(self, "before_state", freeze_json(self.before_state))
        if self.after_state is not None:
            object.__setattr__(self, "after_state", freeze_json(self.after_state))
        if len(self.source_references) > MAX_TOOL_RESULT_SOURCE_REFERENCES:
            raise ValueError(
                f"tool result cannot expose more than {MAX_TOOL_RESULT_SOURCE_REFERENCES} source references"
            )
        if len(self.side_effects) > MAX_AUDIT_EFFECTS:
            raise ValueError(f"tool result cannot expose more than {MAX_AUDIT_EFFECTS} audit effects")
        source_references: list[FrozenJsonObject] = []
        for reference in self.source_references:
            frozen = freeze_json(reference)
            if not isinstance(frozen, FrozenJsonObject):
                raise TypeError("tool result source references must be JSON objects")
            source_references.append(frozen)
        object.__setattr__(self, "source_references", tuple(source_references))
        if self.status is ToolResultStatus.SUCCEEDED and self.error is not None:
            raise ValueError("successful tool result cannot contain an error")
        if self.status is not ToolResultStatus.SUCCEEDED and self.error is None:
            raise ValueError("non-success tool result requires a typed error")
        if self.error is not None and self.error.retryable != self.retryable:
            raise ValueError("result retryability must agree with its typed error")
        if self.status is ToolResultStatus.UNKNOWN_OUTCOME and not any(
            effect.state is SideEffectState.UNKNOWN for effect in self.side_effects
        ):
            raise ValueError("unknown outcome must identify at least one unknown side effect")


__all__ = [
    "MAX_TOOL_RESULT_SOURCE_REFERENCES",
    "SideEffect",
    "SideEffectKind",
    "SideEffectState",
    "ToolError",
    "ToolResult",
    "ToolResultStatus",
]
