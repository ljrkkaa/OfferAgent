"""Deterministically reduce a child transcript tail into its typed result envelope."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator

from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.tools import ToolResult

from .models import SubagentResult, SubagentRunRecord


class ResultReducer:
    """Reduce child output without injecting the detailed transcript into its parent."""

    def reduce(
        self,
        record: SubagentRunRecord,
        *,
        status: str,
        assistant_text: str,
        tool_results: Sequence[ToolResult],
        usage: Mapping[str, Any],
    ) -> SubagentResult:
        text = assistant_text.strip()
        parsed = _json_object(text)
        candidate = parsed if parsed is not None else _fallback_candidate(text, tool_results)
        schema_valid = Draft202012Validator(thaw_json(record.result_schema)).is_valid(candidate)

        summary = candidate.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = text or f"Child Agent ended in {status}"
        findings = _objects(candidate.get("findings"))
        evidence = _objects(candidate.get("evidence"))
        proposed = _objects(candidate.get("proposedActions"))
        unresolved = _strings(candidate.get("unresolvedQuestions"))
        artifact_ids = tuple(
            dict.fromkeys(artifact_id for result in tool_results for artifact_id in result.artifact_ids)
        )
        error: Mapping[str, Any] | None = None
        effective_status = status
        if not schema_valid:
            effective_status = "failed"
            error = {"code": "child_result_schema", "message": "Agent result did not match its schema"}
        elif status != "completed":
            error = {"code": f"child_{status}"}
        return SubagentResult(
            record.run_id,
            effective_status,
            summary[:16_384],
            findings[:512],
            evidence[:512],
            artifact_ids[:512],
            proposed[:256],
            unresolved[:256],
            usage,
            error,
        )


def _json_object(text: str) -> dict[str, Any] | None:
    candidate = text
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1])
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _fallback_candidate(text: str, tool_results: Sequence[ToolResult]) -> dict[str, Any]:
    evidence = [
        {"toolCallId": result.tool_call_id, "sourceRefs": list(result.source_refs)}
        for result in tool_results
        if result.source_refs
    ]
    findings = [{"title": "Child Agent conclusion", "summary": text}] if text else []
    return {
        "summary": text or "Child Agent returned no assistant text",
        "findings": findings,
        "evidence": evidence,
        "proposedActions": [],
        "unresolvedQuestions": [],
    }


def _objects(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


__all__ = ["ResultReducer"]
