"""Lossless JSON serialization for local SQLite domain records."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from offeragent_harness.models.json_types import freeze_json, thaw_json
from offeragent_harness.ports import InvocationRecord, JournalState, StoredEvent
from offeragent_harness.tools import (
    SideEffect,
    SideEffectKind,
    SideEffectState,
    ToolError,
    ToolResult,
    ToolResultStatus,
)


def dump_json(value: Any) -> str:
    """Validate a value as JSON and encode it canonically."""

    frozen = freeze_json(value)
    return json.dumps(
        thaw_json(frozen),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def load_json(value: str) -> Any:
    return json.loads(value)


def dump_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def load_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted timestamp is not timezone-aware")
    return parsed


def tool_result_to_value(result: ToolResult) -> dict[str, Any]:
    return {
        "toolCallId": result.tool_call_id,
        "status": result.status.value,
        "data": thaw_json(result.data),
        "userVisibleSummary": result.user_visible_summary,
        "artifactIds": list(result.artifact_ids),
        "sourceRefs": list(result.source_refs),
        "sourceReferences": [thaw_json(reference) for reference in result.source_references],
        "contextActivations": list(result.context_activations),
        "sideEffects": [
            {
                "kind": effect.kind.value,
                "state": effect.state.value,
                "resourceId": effect.resource_id,
                "beforeState": thaw_json(effect.before_state),
                "afterState": thaw_json(effect.after_state),
                "metadata": thaw_json(effect.metadata),
            }
            for effect in result.side_effects
        ],
        "retryable": result.retryable,
        "beforeState": thaw_json(result.before_state),
        "afterState": thaw_json(result.after_state),
        "error": (
            None
            if result.error is None
            else {
                "code": result.error.code,
                "message": result.error.message,
                "retryable": result.error.retryable,
                "cancelled": result.error.cancelled,
                "details": thaw_json(result.error.details),
            }
        ),
    }


def tool_result_from_value(raw: Any) -> ToolResult:
    if not isinstance(raw, dict):
        raise ValueError("persisted tool result must be a JSON object")
    raw_effects = raw.get("sideEffects")
    if not isinstance(raw_effects, list):
        raise ValueError("persisted tool result sideEffects must be an array")
    effects = tuple(
        SideEffect(
            kind=SideEffectKind(effect["kind"]),
            state=SideEffectState(effect["state"]),
            resource_id=effect["resourceId"],
            before_state=effect.get("beforeState"),
            after_state=effect.get("afterState"),
            metadata=effect.get("metadata", {}),
        )
        for effect in raw_effects
    )
    raw_error = raw.get("error")
    error = (
        None
        if raw_error is None
        else ToolError(
            code=raw_error["code"],
            message=raw_error["message"],
            retryable=raw_error["retryable"],
            cancelled=raw_error["cancelled"],
            details=raw_error.get("details", {}),
        )
    )
    return ToolResult(
        tool_call_id=raw["toolCallId"],
        status=ToolResultStatus(raw["status"]),
        data=raw.get("data"),
        user_visible_summary=raw["userVisibleSummary"],
        artifact_ids=tuple(raw.get("artifactIds", ())),
        source_refs=tuple(raw.get("sourceRefs", ())),
        side_effects=effects,
        retryable=raw["retryable"],
        before_state=raw.get("beforeState"),
        after_state=raw.get("afterState"),
        error=error,
        source_references=tuple(raw.get("sourceReferences", ())),
        context_activations=tuple(raw["contextActivations"]),
    )


def dump_tool_result(result: ToolResult) -> str:
    return dump_json(tool_result_to_value(result))


def load_tool_result(value: str) -> ToolResult:
    return tool_result_from_value(load_json(value))


def stored_event_from_row(row: Any) -> StoredEvent:
    return StoredEvent(
        stream_id=row["stream_id"],
        sequence=row["sequence"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        payload=load_json(row["payload_json"]),
        occurred_at=load_datetime(row["occurred_at"]),
        terminal=bool(row["terminal"]),
        idempotency_key=row["idempotency_key"],
    )


def invocation_record_from_row(row: Any) -> InvocationRecord:
    result_json = row["result_json"]
    return InvocationRecord(
        scope=row["scope"],
        idempotency_key=row["idempotency_key"],
        request_hash=row["request_hash"],
        state=JournalState(row["state"]),
        started_at=load_datetime(row["started_at"]),
        completed_at=None if row["completed_at"] is None else load_datetime(row["completed_at"]),
        result=None if result_json is None else load_tool_result(result_json),
    )


__all__ = [
    "dump_datetime",
    "dump_json",
    "dump_tool_result",
    "invocation_record_from_row",
    "load_datetime",
    "load_json",
    "load_tool_result",
    "stored_event_from_row",
    "tool_result_from_value",
    "tool_result_to_value",
]
