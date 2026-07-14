"""Versioned plain-JSON codecs for durable Subagent entities."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.permissions import CapabilityScope, PermissionMode, RiskClass

from .models import (
    AgentBudget,
    AgentUsage,
    ContextForkMode,
    ContextSnapshot,
    EffectiveToolScope,
    ExecutionPriority,
    SubagentLifetime,
    SubagentResult,
    SubagentRunRecord,
    SubagentRunStatus,
)


def budget_to_value(value: AgentBudget) -> dict[str, int | float]:
    return {
        "inputTokens": value.input_tokens,
        "outputTokens": value.output_tokens,
        "modelCalls": value.model_calls,
        "toolCalls": value.tool_calls,
        "wallTimeSeconds": value.wall_time_seconds,
        "artifactBytes": value.artifact_bytes,
        "childCount": value.child_count,
        "costMicros": value.cost_micros,
    }


def budget_from_value(value: Any) -> AgentBudget:
    raw = _exact(
        value,
        {
            "inputTokens",
            "outputTokens",
            "modelCalls",
            "toolCalls",
            "wallTimeSeconds",
            "artifactBytes",
            "childCount",
            "costMicros",
        },
        "AgentBudget",
    )
    return AgentBudget(
        _int(raw["inputTokens"]),
        _int(raw["outputTokens"]),
        _int(raw["modelCalls"]),
        _int(raw["toolCalls"]),
        _number(raw["wallTimeSeconds"]),
        _int(raw["artifactBytes"]),
        _int(raw["childCount"]),
        _int(raw["costMicros"]),
    )


def usage_to_value(value: AgentUsage) -> dict[str, int | float]:
    return {
        "inputTokens": value.input_tokens,
        "outputTokens": value.output_tokens,
        "modelCalls": value.model_calls,
        "toolCalls": value.tool_calls,
        "wallTimeSeconds": value.wall_time_seconds,
        "artifactBytes": value.artifact_bytes,
        "childCount": value.child_count,
        "costMicros": value.cost_micros,
    }


def usage_from_value(value: Any) -> AgentUsage:
    budget = budget_from_value(value)
    return AgentUsage(*budget.as_tuple())  # type: ignore[arg-type]


def scope_to_value(value: CapabilityScope) -> dict[str, Any]:
    return {
        "allowedTools": sorted(value.allowed_tools),
        "deniedTools": sorted(value.denied_tools),
        "allowedRisks": sorted(item.value for item in value.allowed_risks),
        "rootCapabilities": sorted(value.root_capabilities),
        "allowNetwork": value.allow_network,
        "allowSecretHandles": value.allow_secret_handles,
    }


def scope_from_value(value: Any) -> CapabilityScope:
    raw = _exact(
        value,
        {
            "allowedTools",
            "deniedTools",
            "allowedRisks",
            "rootCapabilities",
            "allowNetwork",
            "allowSecretHandles",
        },
        "CapabilityScope",
    )
    return CapabilityScope(
        frozenset(_strings(raw["allowedTools"])),
        frozenset(_strings(raw["deniedTools"])),
        frozenset(RiskClass(item) for item in _strings(raw["allowedRisks"])),
        frozenset(_strings(raw["rootCapabilities"])),
        _bool(raw["allowNetwork"]),
        _bool(raw["allowSecretHandles"]),
    )


def tool_scope_to_value(value: EffectiveToolScope) -> dict[str, Any]:
    return {
        "allowedVersions": thaw_json(value.allowed_versions),
        "argumentConstraints": thaw_json(value.argument_constraints),
        "registrySnapshotHash": value.registry_snapshot_hash,
    }


def tool_scope_from_value(value: Any) -> EffectiveToolScope:
    raw = _exact(value, {"allowedVersions", "argumentConstraints", "registrySnapshotHash"}, "EffectiveToolScope")
    versions_raw = _mapping(raw["allowedVersions"], "allowedVersions")
    constraints_raw = _mapping(raw["argumentConstraints"], "argumentConstraints")
    versions = {str(name): tuple(_strings(items)) for name, items in versions_raw.items()}
    constraints = {str(name): _mapping(item, f"argumentConstraints.{name}") for name, item in constraints_raw.items()}
    return EffectiveToolScope(versions, constraints, _str(raw["registrySnapshotHash"]))


def context_to_value(value: ContextSnapshot) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "snapshotId": value.snapshot_id,
        "workspaceId": value.workspace_id,
        "parentRunId": value.parent_run_id,
        "mode": value.mode.value,
        "content": thaw_json(value.content),
        "contentHash": value.content_hash,
        "createdAt": value.created_at.isoformat(),
    }


def context_from_value(value: Any) -> ContextSnapshot:
    raw = _exact(
        value,
        {"schemaVersion", "snapshotId", "workspaceId", "parentRunId", "mode", "content", "contentHash", "createdAt"},
        "ContextSnapshot",
    )
    if raw["schemaVersion"] != 1:
        raise ValueError("ContextSnapshot schema version is unsupported")
    return ContextSnapshot(
        _str(raw["snapshotId"]),
        _str(raw["workspaceId"]),
        _str(raw["parentRunId"]),
        ContextForkMode(_str(raw["mode"])),
        _mapping(raw["content"], "content"),
        _str(raw["contentHash"]),
        _datetime(raw["createdAt"]),
    )


def run_record_to_value(value: SubagentRunRecord) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "runId": value.run_id,
        "rootRunId": value.root_run_id,
        "parentRunId": value.parent_run_id,
        "ancestorRunIds": list(value.ancestor_run_ids),
        "sessionId": value.session_id,
        "turnId": value.turn_id,
        "workspaceId": value.workspace_id,
        "traceId": value.trace_id,
        "spawnCallId": value.spawn_call_id,
        "agentName": value.agent_name,
        "agentVersion": value.agent_version,
        "task": value.task,
        "taskFingerprint": value.task_fingerprint,
        "depth": value.depth,
        "lifetime": value.lifetime.value,
        "contextSnapshotId": value.context_snapshot_id,
        "permissionMode": value.permission_mode.value,
        "effectiveScope": scope_to_value(value.effective_scope),
        "toolScope": tool_scope_to_value(value.tool_scope),
        "budgetLimit": budget_to_value(value.budget_limit),
        "budgetUsed": usage_to_value(value.budget_used),
        "deadlineAt": value.deadline_at.isoformat(),
        "resultSchema": thaw_json(value.result_schema),
        "status": value.status.value,
        "phase": value.phase,
        "priority": value.priority.value,
        "createdAt": value.created_at.isoformat(),
        "updatedAt": value.updated_at.isoformat(),
        "leaseOwner": value.lease_owner,
        "leaseExpiresAt": None if value.lease_expires_at is None else value.lease_expires_at.isoformat(),
        "safeCheckpoint": value.safe_checkpoint,
        "resultArtifactId": value.result_artifact_id,
        "revision": value.revision,
    }


def run_record_from_value(value: Any) -> SubagentRunRecord:
    fields = {
        "schemaVersion",
        "runId",
        "rootRunId",
        "parentRunId",
        "ancestorRunIds",
        "sessionId",
        "turnId",
        "workspaceId",
        "traceId",
        "spawnCallId",
        "agentName",
        "agentVersion",
        "task",
        "taskFingerprint",
        "depth",
        "lifetime",
        "contextSnapshotId",
        "permissionMode",
        "effectiveScope",
        "toolScope",
        "budgetLimit",
        "budgetUsed",
        "deadlineAt",
        "resultSchema",
        "status",
        "phase",
        "priority",
        "createdAt",
        "updatedAt",
        "leaseOwner",
        "leaseExpiresAt",
        "safeCheckpoint",
        "resultArtifactId",
        "revision",
    }
    raw = _exact(value, fields, "SubagentRunRecord")
    if raw["schemaVersion"] != 1:
        raise ValueError("SubagentRunRecord schema version is unsupported")
    return SubagentRunRecord(
        run_id=_str(raw["runId"]),
        root_run_id=_str(raw["rootRunId"]),
        parent_run_id=_str(raw["parentRunId"]),
        ancestor_run_ids=tuple(_strings(raw["ancestorRunIds"])),
        session_id=_str(raw["sessionId"]),
        turn_id=_str(raw["turnId"]),
        workspace_id=_str(raw["workspaceId"]),
        trace_id=_str(raw["traceId"]),
        spawn_call_id=_str(raw["spawnCallId"]),
        agent_name=_str(raw["agentName"]),
        agent_version=_str(raw["agentVersion"]),
        task=_str(raw["task"]),
        task_fingerprint=_str(raw["taskFingerprint"]),
        depth=_int(raw["depth"]),
        lifetime=SubagentLifetime(_str(raw["lifetime"])),
        context_snapshot_id=_str(raw["contextSnapshotId"]),
        permission_mode=PermissionMode(_str(raw["permissionMode"])),
        effective_scope=scope_from_value(raw["effectiveScope"]),
        tool_scope=tool_scope_from_value(raw["toolScope"]),
        budget_limit=budget_from_value(raw["budgetLimit"]),
        budget_used=usage_from_value(raw["budgetUsed"]),
        deadline_at=_datetime(raw["deadlineAt"]),
        result_schema=_mapping(raw["resultSchema"], "resultSchema"),
        status=SubagentRunStatus(_str(raw["status"])),
        phase=_str(raw["phase"]),
        priority=ExecutionPriority(_str(raw["priority"])),
        created_at=_datetime(raw["createdAt"]),
        updated_at=_datetime(raw["updatedAt"]),
        lease_owner=None if raw["leaseOwner"] is None else _str(raw["leaseOwner"]),
        lease_expires_at=None if raw["leaseExpiresAt"] is None else _datetime(raw["leaseExpiresAt"]),
        safe_checkpoint=_bool(raw["safeCheckpoint"]),
        result_artifact_id=None if raw["resultArtifactId"] is None else _str(raw["resultArtifactId"]),
        revision=_int(raw["revision"]),
    )


def result_to_value(value: SubagentResult) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "runId": value.run_id,
        "status": value.status,
        "summary": value.summary,
        "findings": thaw_json(value.findings),
        "evidence": thaw_json(value.evidence),
        "artifactIds": list(value.artifact_ids),
        "proposedActions": thaw_json(value.proposed_actions),
        "unresolvedQuestions": list(value.unresolved_questions),
        "usage": thaw_json(value.usage),
        "error": None if value.error is None else thaw_json(value.error),
    }


def result_from_value(value: Any) -> SubagentResult:
    raw = _exact(
        value,
        {
            "schemaVersion",
            "runId",
            "status",
            "summary",
            "findings",
            "evidence",
            "artifactIds",
            "proposedActions",
            "unresolvedQuestions",
            "usage",
            "error",
        },
        "SubagentResult",
    )
    if raw["schemaVersion"] != 1:
        raise ValueError("SubagentResult schema version is unsupported")
    return SubagentResult(
        _str(raw["runId"]),
        _str(raw["status"]),
        _str(raw["summary"]),
        tuple(_mappings(raw["findings"], "findings")),
        tuple(_mappings(raw["evidence"], "evidence")),
        tuple(_strings(raw["artifactIds"])),
        tuple(_mappings(raw["proposedActions"], "proposedActions")),
        tuple(_strings(raw["unresolvedQuestions"])),
        _mapping(raw["usage"], "usage"),
        None if raw["error"] is None else _mapping(raw["error"], "error"),
    )


def _exact(value: Any, fields: set[str], name: str) -> Mapping[str, Any]:
    raw = _mapping(value, name)
    if set(raw) != fields:
        raise ValueError(f"{name} fields are corrupt")
    return raw


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return value


def _mappings(value: Any, name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an object array")
    return [_mapping(item, name) for item in value]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("persisted value must be a string array")
    return [str(item) for item in value]


def _str(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("persisted value must be a non-empty string")
    return value


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("persisted value must be an integer")
    return int(value)


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("persisted value must be numeric")
    return float(value)


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("persisted value must be a boolean")
    return value


def _datetime(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(_str(value))
    except ValueError as error:
        raise ValueError("persisted datetime is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted datetime must be timezone-aware")
    return parsed


__all__ = [
    "budget_from_value",
    "budget_to_value",
    "context_from_value",
    "context_to_value",
    "result_from_value",
    "result_to_value",
    "run_record_from_value",
    "run_record_to_value",
    "scope_from_value",
    "scope_to_value",
    "tool_scope_from_value",
    "tool_scope_to_value",
    "usage_from_value",
    "usage_to_value",
]
