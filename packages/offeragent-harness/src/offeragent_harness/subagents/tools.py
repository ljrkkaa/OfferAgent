"""All model-facing Subagent operations routed through the single Tool Kernel."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any

from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.permissions import CapabilityScope, PermissionMode, RiskClass
from offeragent_harness.ports import CancellationToken, OperationCancelled
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffect,
    SideEffectClass,
    SideEffectKind,
    SideEffectState,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
)

from .models import (
    AgentBudget,
    AgentCancelCommand,
    AgentSendCommand,
    AgentSpawnCommand,
    AgentWaitCommand,
    ContextForkMode,
    ExecutionPriority,
    MailboxMode,
    SubagentLifetime,
    WaitMode,
)
from .service import SubagentService, SubagentServiceError

SUBAGENT_TOOL_VERSION = "1"
_OUTPUT_LIMIT = 2 * 1024 * 1024


def _object(properties: Mapping[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": dict(properties),
        "required": required,
        "additionalProperties": False,
    }


_STRINGS = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 4096}, "maxItems": 256}
_BUDGET_REQUIRED = [
    "inputTokens",
    "outputTokens",
    "modelCalls",
    "toolCalls",
    "wallTimeSeconds",
    "artifactBytes",
    "childCount",
    "costMicros",
]
_BUDGET = {
    "type": "object",
    "properties": {
        "inputTokens": {"type": "integer", "minimum": 0},
        "outputTokens": {"type": "integer", "minimum": 0},
        "modelCalls": {"type": "integer", "minimum": 1},
        "toolCalls": {"type": "integer", "minimum": 0},
        "wallTimeSeconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 604800},
        "artifactBytes": {"type": "integer", "minimum": 1024},
        "childCount": {"type": "integer", "minimum": 0},
        "costMicros": {"type": "integer", "minimum": 0},
    },
    "required": _BUDGET_REQUIRED,
    "additionalProperties": False,
}
_USAGE = {
    "type": "object",
    "properties": {
        "inputTokens": {"type": "integer", "minimum": 0},
        "outputTokens": {"type": "integer", "minimum": 0},
        "modelCalls": {"type": "integer", "minimum": 0},
        "toolCalls": {"type": "integer", "minimum": 0},
        "wallTimeSeconds": {"type": "number", "minimum": 0, "maximum": 604800},
        "artifactBytes": {"type": "integer", "minimum": 0},
        "childCount": {"type": "integer", "minimum": 0},
        "costMicros": {"type": "integer", "minimum": 0},
    },
    "required": _BUDGET_REQUIRED,
    "additionalProperties": False,
}
_SCOPE = {
    "type": "object",
    "properties": {
        "allowedTools": _STRINGS,
        "deniedTools": _STRINGS,
        "allowedRisks": {"type": "array", "items": {"enum": [item.value for item in RiskClass]}, "maxItems": 7},
        "rootCapabilities": _STRINGS,
        "allowNetwork": {"type": "boolean"},
        "allowSecretHandles": {"type": "boolean"},
        "permissionMode": {"enum": [item.value for item in PermissionMode if item is not PermissionMode.BYPASS]},
    },
    "required": [
        "allowedTools",
        "deniedTools",
        "allowedRisks",
        "rootCapabilities",
        "allowNetwork",
        "allowSecretHandles",
        "permissionMode",
    ],
    "additionalProperties": False,
}
_RUN_ID = {"type": "string", "minLength": 1, "maxLength": 256}


def _definition(
    name: str,
    description: str,
    input_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any],
    *,
    risk: RiskClass,
    effect: SideEffectClass,
    capability: str,
    concurrent: bool,
    retryable: bool,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version=SUBAGENT_TOOL_VERSION,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        executor_location=ExecutorLocation.LOCAL,
        risk=risk,
        side_effect_class=effect,
        required_capabilities=frozenset({capability}),
        concurrency_safe=concurrent,
        idempotent=True,
        retryable=retryable,
        timeout_ms=310_000 if name == "agent.wait" else 45_000,
        output_limit_bytes=_OUTPUT_LIMIT,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    )


_DEFINITIONS = (
    _definition(
        "agent.spawn",
        "Create a budgeted child AgentRun in this Worker's existing Harness Agent Loop.",
        _object(
            {
                "task": {"type": "string", "minLength": 1, "maxLength": 32768},
                "profile": {"type": "string", "pattern": r"^[a-z][a-z0-9_-]{0,63}$"},
                "context": {
                    "type": "object",
                    "properties": {
                        "mode": {"enum": [item.value for item in ContextForkMode]},
                        "messageIds": _STRINGS,
                        "artifactIds": _STRINGS,
                    },
                    "required": ["mode", "messageIds", "artifactIds"],
                    "additionalProperties": False,
                },
                "scope": _SCOPE,
                "toolVersions": {"type": "object", "additionalProperties": _STRINGS, "maxProperties": 256},
                "toolConstraints": {"type": "object", "additionalProperties": {"type": "object"}, "maxProperties": 256},
                "budget": _BUDGET,
                "execution": {
                    "type": "object",
                    "properties": {
                        "lifetime": {"enum": [item.value for item in SubagentLifetime]},
                        "priority": {"enum": [item.value for item in ExecutionPriority]},
                        "deadlineAt": {"oneOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]},
                    },
                    "required": ["lifetime", "priority", "deadlineAt"],
                    "additionalProperties": False,
                },
            },
            ["task", "profile", "context", "scope", "toolVersions", "toolConstraints", "budget", "execution"],
        ),
        _object(
            {"runId": _RUN_ID, "parentRunId": _RUN_ID, "status": {"const": "queued"}, "queuedAt": {"type": "string"}},
            ["runId", "parentRunId", "status", "queuedAt"],
        ),
        risk=RiskClass.EXECUTE,
        effect=SideEffectClass.EXECUTE,
        capability="subagent.spawn",
        concurrent=False,
        retryable=True,
    ),
    _definition(
        "agent.send",
        "Append or safely steer one managed descendant through its durable mailbox.",
        _object(
            {
                "runId": _RUN_ID,
                "mode": {"enum": [item.value for item in MailboxMode]},
                "message": {"type": "string", "minLength": 1, "maxLength": 262144},
                "artifactIds": _STRINGS,
                "messageId": {"type": "string", "pattern": r"^msg_[A-Za-z0-9][A-Za-z0-9_-]{0,123}$"},
            },
            ["runId", "mode", "message", "artifactIds", "messageId"],
        ),
        _object(
            {
                "runId": _RUN_ID,
                "messageId": _RUN_ID,
                "sequence": {"type": "integer", "minimum": 1},
                "duplicate": {"type": "boolean"},
            },
            ["runId", "messageId", "sequence", "duplicate"],
        ),
        risk=RiskClass.WRITE,
        effect=SideEffectClass.WRITE,
        capability="subagent.manage",
        concurrent=False,
        retryable=True,
    ),
    _definition(
        "agent.wait",
        "Wait for any/all managed descendant Runs without changing child state on timeout.",
        _object(
            {
                "runIds": {"type": "array", "items": _RUN_ID, "minItems": 1, "maxItems": 256, "uniqueItems": True},
                "mode": {"enum": [item.value for item in WaitMode]},
                "timeoutMs": {"type": "integer", "minimum": 0, "maximum": 300000},
            },
            ["runIds", "mode", "timeoutMs"],
        ),
        _object(
            {
                "completedRunIds": {"type": "array", "items": _RUN_ID, "maxItems": 256},
                "pendingRunIds": {"type": "array", "items": _RUN_ID, "maxItems": 256},
                "timedOut": {"type": "boolean"},
            },
            ["completedRunIds", "pendingRunIds", "timedOut"],
        ),
        risk=RiskClass.READ,
        effect=SideEffectClass.READ,
        capability="subagent.read",
        concurrent=True,
        retryable=True,
    ),
    _definition(
        "agent.status",
        "Read bounded status, phase, child IDs and budget progress for a managed descendant.",
        _object({"runId": _RUN_ID}, ["runId"]),
        _object(
            {
                "runId": _RUN_ID,
                "rootRunId": _RUN_ID,
                "parentRunId": _RUN_ID,
                "agentName": {"type": "string"},
                "status": {"type": "string"},
                "phase": {"type": "string"},
                "depth": {"type": "integer", "minimum": 1},
                "budgetLimit": _BUDGET,
                "budgetUsed": _USAGE,
                "deadlineAt": {"type": "string"},
                "childRunIds": {"type": "array", "items": _RUN_ID},
                "updatedAt": {"type": "string"},
            },
            [
                "runId",
                "rootRunId",
                "parentRunId",
                "agentName",
                "status",
                "phase",
                "depth",
                "budgetLimit",
                "budgetUsed",
                "deadlineAt",
                "childRunIds",
                "updatedAt",
            ],
        ),
        risk=RiskClass.READ,
        effect=SideEffectClass.READ,
        capability="subagent.read",
        concurrent=True,
        retryable=True,
    ),
    _definition(
        "agent.result",
        "Read a structured descendant result and Artifact references without injecting its transcript.",
        _object(
            {
                "runId": _RUN_ID,
                "include": {
                    "type": "array",
                    "items": {"enum": ["summary", "findings", "evidence", "artifacts", "proposedActions", "usage"]},
                    "maxItems": 6,
                    "uniqueItems": True,
                },
            },
            ["runId", "include"],
        ),
        _object(
            {
                "runId": _RUN_ID,
                "status": {"type": "string"},
                "summary": {"type": "string"},
                "findings": {"type": "array", "items": {"type": "object"}},
                "evidence": {"type": "array", "items": {"type": "object"}},
                "artifactIds": {"type": "array", "items": _RUN_ID},
                "proposedActions": {"type": "array", "items": {"type": "object"}},
                "unresolvedQuestions": {"type": "array", "items": {"type": "string"}},
                "usage": {"type": "object"},
                "error": {"oneOf": [{"type": "object"}, {"type": "null"}]},
            },
            [
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
            ],
        ),
        risk=RiskClass.READ,
        effect=SideEffectClass.READ,
        capability="subagent.read",
        concurrent=True,
        retryable=True,
    ),
    _definition(
        "agent.cancel",
        "Request cancellation of one managed descendant and optionally its descendant tree.",
        _object(
            {
                "runId": _RUN_ID,
                "reason": {"type": "string", "minLength": 1, "maxLength": 4096},
                "cascade": {"type": "boolean"},
            },
            ["runId", "reason", "cascade"],
        ),
        _object(
            {
                "runId": _RUN_ID,
                "accepted": {"type": "boolean"},
                "descendantRunIds": {"type": "array", "items": _RUN_ID},
            },
            ["runId", "accepted", "descendantRunIds"],
        ),
        risk=RiskClass.EXECUTE,
        effect=SideEffectClass.EXECUTE,
        capability="subagent.manage",
        concurrent=False,
        retryable=True,
    ),
)


def subagent_tool_definitions() -> tuple[ToolDefinition, ...]:
    return _DEFINITIONS


class SubagentToolExecutor:
    def __init__(self, workspace_id: str, service: SubagentService) -> None:
        if not workspace_id or service.workspace_id != workspace_id:
            raise ValueError("Subagent Tool executor/Service Workspace mismatch")
        self._workspace_id = workspace_id
        self._service = service
        self._definitions = MappingProxyType({(item.name, item.version): item for item in _DEFINITIONS})

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions.values())

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        if call.workspace_id != self._workspace_id:
            return _failure(call, "subagent_workspace_mismatch", "Subagent Tool belongs to another Workspace")
        definition = self._definitions.get((call.name, call.version))
        if definition is None or definition.fingerprint != call.definition_fingerprint:
            return _failure(call, "subagent_tool_unavailable", "Subagent Tool is not in this Registry snapshot")
        arguments = thaw_json(call.arguments)
        try:
            if call.name == "agent.spawn":
                return await self._spawn(call, arguments, cancellation)
            if call.name == "agent.send":
                return await self._send(call, arguments, cancellation)
            if call.name == "agent.wait":
                return await self._wait(call, arguments, cancellation)
            if call.name == "agent.status":
                return await self._status(call, arguments)
            if call.name == "agent.result":
                return await self._result(call, arguments)
            return await self._cancel(call, arguments, cancellation)
        except OperationCancelled:
            raise
        except SubagentServiceError as error:
            return _failure(call, error.code, str(error), retryable=error.retryable)
        except (KeyError, TypeError, ValueError) as error:
            return _failure(call, "subagent_invalid_arguments", str(error))

    async def _spawn(self, call: ToolCall, value: Mapping[str, Any], cancellation: CancellationToken) -> ToolResult:
        context = _mapping(value["context"])
        scope_raw = _mapping(value["scope"])
        execution = _mapping(value["execution"])
        command = AgentSpawnCommand(
            call.run_id,
            call.tool_call_id,
            _string(value["task"]),
            _string(value["profile"]),
            ContextForkMode(_string(context["mode"])),
            tuple(_strings(context["messageIds"])),
            tuple(_strings(context["artifactIds"])),
            _scope(scope_raw),
            PermissionMode(_string(scope_raw["permissionMode"])),
            {name: tuple(_strings(items)) for name, items in _mapping(value["toolVersions"]).items()},
            {name: _mapping(item) for name, item in _mapping(value["toolConstraints"]).items()},
            _budget(_mapping(value["budget"])),
            SubagentLifetime(_string(execution["lifetime"])),
            ExecutionPriority(_string(execution["priority"])),
            _optional_datetime(execution["deadlineAt"]),
        )
        handle = await self._service.spawn(command, cancellation)
        data = {
            "runId": handle.run_id,
            "parentRunId": handle.parent_run_id,
            "status": handle.status.value,
            "queuedAt": handle.queued_at.isoformat(),
        }
        return _success(call, data, f"Subagent {handle.run_id} 已进入队列", handle.run_id)

    async def _send(self, call: ToolCall, value: Mapping[str, Any], cancellation: CancellationToken) -> ToolResult:
        receipt = await self._service.send(
            AgentSendCommand(
                call.run_id,
                _string(value["runId"]),
                MailboxMode(_string(value["mode"])),
                _string(value["message"]),
                tuple(_strings(value["artifactIds"])),
                _string(value["messageId"]),
            ),
            cancellation,
        )
        return _success(
            call,
            {
                "runId": receipt.run_id,
                "messageId": receipt.message_id,
                "sequence": receipt.sequence,
                "duplicate": receipt.duplicate,
            },
            f"消息已写入 Subagent {receipt.run_id} 邮箱",
            receipt.run_id,
        )

    async def _wait(self, call: ToolCall, value: Mapping[str, Any], cancellation: CancellationToken) -> ToolResult:
        result = await self._service.wait(
            AgentWaitCommand(
                call.run_id,
                tuple(_strings(value["runIds"])),
                WaitMode(_string(value["mode"])),
                _integer(value["timeoutMs"]),
            ),
            cancellation,
        )
        return _success(
            call,
            {
                "completedRunIds": list(result.completed_run_ids),
                "pendingRunIds": list(result.pending_run_ids),
                "timedOut": result.timed_out,
            },
            "Subagent wait 已结束",
            ",".join((*result.completed_run_ids, *result.pending_run_ids)),
        )

    async def _status(self, call: ToolCall, value: Mapping[str, Any]) -> ToolResult:
        status = await self._service.status(call.run_id, _string(value["runId"]))
        return _success(
            call,
            {
                "runId": status.run_id,
                "rootRunId": status.root_run_id,
                "parentRunId": status.parent_run_id,
                "agentName": status.agent_name,
                "status": status.status.value,
                "phase": status.phase,
                "depth": status.depth,
                "budgetLimit": _budget_value(status.budget_limit),
                "budgetUsed": _usage_as_budget(status.budget_used),
                "deadlineAt": status.deadline_at.isoformat(),
                "childRunIds": list(status.child_run_ids),
                "updatedAt": status.updated_at.isoformat(),
            },
            f"Subagent {status.run_id} 状态为 {status.status.value}",
            status.run_id,
        )

    async def _result(self, call: ToolCall, value: Mapping[str, Any]) -> ToolResult:
        result = await self._service.result(
            call.run_id,
            _string(value["runId"]),
            frozenset(_strings(value["include"])),
        )
        return _success(
            call,
            {
                "runId": result.run_id,
                "status": result.status,
                "summary": result.summary,
                "findings": thaw_json(result.findings),
                "evidence": thaw_json(result.evidence),
                "artifactIds": list(result.artifact_ids),
                "proposedActions": thaw_json(result.proposed_actions),
                "unresolvedQuestions": list(result.unresolved_questions),
                "usage": thaw_json(result.usage),
                "error": None if result.error is None else thaw_json(result.error),
            },
            f"已读取 Subagent {result.run_id} 结构化结果",
            result.run_id,
            result.artifact_ids,
        )

    async def _cancel(self, call: ToolCall, value: Mapping[str, Any], cancellation: CancellationToken) -> ToolResult:
        result = await self._service.cancel(
            AgentCancelCommand(
                call.run_id,
                _string(value["runId"]),
                _string(value["reason"]),
                _boolean(value["cascade"]),
            ),
            cancellation,
        )
        return _success(
            call,
            {
                "runId": result.run_id,
                "accepted": result.accepted,
                "descendantRunIds": list(result.descendant_run_ids),
            },
            f"Subagent {result.run_id} 取消请求已处理",
            result.run_id,
        )


def _success(
    call: ToolCall,
    data: Mapping[str, Any],
    summary: str,
    resource: str,
    artifact_ids: tuple[str, ...] = (),
) -> ToolResult:
    return ToolResult(
        call.tool_call_id,
        ToolResultStatus.SUCCEEDED,
        data,
        summary,
        artifact_ids,
        (),
        (
            SideEffect(
                SideEffectKind.EXTERNAL_SYSTEM,
                SideEffectState.OBSERVED,
                f"subagent:{resource or call.run_id}",
                None,
                None,
                {"workspaceId": call.workspace_id, "parentRunId": call.run_id},
            ),
        ),
        False,
        None,
        None,
        None,
    )


def _failure(call: ToolCall, code: str, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(
        call.tool_call_id,
        ToolResultStatus.FAILED,
        None,
        message,
        (),
        (),
        (),
        retryable,
        None,
        None,
        ToolError(code, message, retryable, False),
    )


def _scope(value: Mapping[str, Any]) -> CapabilityScope:
    return CapabilityScope(
        frozenset(_strings(value["allowedTools"])),
        frozenset(_strings(value["deniedTools"])),
        frozenset(RiskClass(item) for item in _strings(value["allowedRisks"])),
        frozenset(_strings(value["rootCapabilities"])),
        _boolean(value["allowNetwork"]),
        _boolean(value["allowSecretHandles"]),
    )


def _budget(value: Mapping[str, Any]) -> AgentBudget:
    return AgentBudget(
        _integer(value["inputTokens"]),
        _integer(value["outputTokens"]),
        _integer(value["modelCalls"]),
        _integer(value["toolCalls"]),
        _number(value["wallTimeSeconds"]),
        _integer(value["artifactBytes"]),
        _integer(value["childCount"]),
        _integer(value["costMicros"]),
    )


def _budget_value(value: AgentBudget) -> dict[str, Any]:
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


def _usage_as_budget(value: Any) -> dict[str, Any]:
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


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("argument must be an object")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("argument must be a non-empty string")
    return value


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("argument must be a string array")
    return [str(item) for item in value]


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("argument must be an integer")
    return int(value)


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("argument must be numeric")
    return float(value)


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("argument must be a boolean")
    return value


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(_string(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("deadlineAt must be timezone-aware")
    return parsed


__all__ = ["SUBAGENT_TOOL_VERSION", "SubagentToolExecutor", "subagent_tool_definitions"]
