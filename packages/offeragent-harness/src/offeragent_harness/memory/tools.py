"""Tool Kernel surface for source-bound personal memory."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any

from offeragent_harness.models import thaw_json
from offeragent_harness.permissions import RiskClass
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
    MemoryItem,
    MemoryKind,
    MemoryScope,
    memory_event_to_json,
    memory_item_to_json,
)
from .store import MemoryRepository, MemoryStoreError

MEMORY_TOOL_VERSION = "1"
_OUTPUT_LIMIT = 256 * 1024
_MEMORY_ID = {"type": "string", "pattern": r"^memory_[A-Za-z0-9_-]{4,128}$"}
_KEY = {"type": "string", "pattern": r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*){0,7}$"}


def _object(properties: Mapping[str, Any], required: tuple[str, ...]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_SOURCE = _object(
    {
        "sessionId": {"type": "string", "minLength": 1, "maxLength": 256},
        "turnId": {"type": "string", "minLength": 1, "maxLength": 256},
        "runId": {"type": "string", "minLength": 1, "maxLength": 256},
        "quote": {"type": "string", "minLength": 1, "maxLength": 4096},
    },
    ("sessionId", "turnId", "runId", "quote"),
)
_MEMORY = _object(
    {
        "schemaVersion": {"const": 1},
        "memoryId": _MEMORY_ID,
        "workspaceId": {"type": "string", "minLength": 1, "maxLength": 256},
        "profileId": {"type": "string", "minLength": 1, "maxLength": 256},
        "sessionId": {"anyOf": [{"type": "string", "minLength": 1, "maxLength": 256}, {"type": "null"}]},
        "key": _KEY,
        "kind": {"enum": [item.value for item in MemoryKind]},
        "scope": {"enum": [item.value for item in MemoryScope]},
        "status": {"enum": ["proposed", "confirmed", "superseded", "forgotten", "expired"]},
        "content": {"type": "string", "minLength": 1, "maxLength": 16_384},
        "pinned": {"type": "boolean"},
        "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        "source": _SOURCE,
        "createdAt": {"type": "string", "minLength": 20, "maxLength": 64},
        "updatedAt": {"type": "string", "minLength": 20, "maxLength": 64},
        "expiresAt": {"anyOf": [{"type": "string", "minLength": 20, "maxLength": 64}, {"type": "null"}]},
        "supersedesId": {"anyOf": [_MEMORY_ID, {"type": "null"}]},
        "contentHash": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
        "revision": {"type": "integer", "minimum": 1},
    },
    (
        "schemaVersion",
        "memoryId",
        "workspaceId",
        "profileId",
        "sessionId",
        "key",
        "kind",
        "scope",
        "status",
        "content",
        "pinned",
        "importance",
        "source",
        "createdAt",
        "updatedAt",
        "expiresAt",
        "supersedesId",
        "contentHash",
        "revision",
    ),
)
_WRITE_PROPERTIES = {
    "memoryKey": _KEY,
    "kind": {"enum": [item.value for item in MemoryKind]},
    "scope": {"enum": [item.value for item in MemoryScope]},
    "content": {"type": "string", "minLength": 1, "maxLength": 16_384},
    "evidenceQuote": {"type": "string", "minLength": 1, "maxLength": 4096},
    "pinned": {"type": "boolean", "default": False},
    "importance": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
    "expiresAt": {"anyOf": [{"type": "string", "minLength": 20, "maxLength": 64}, {"type": "null"}]},
}


def _definition(
    *,
    name: str,
    description: str,
    input_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any],
    write: bool,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version=MEMORY_TOOL_VERSION,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.WRITE if write else RiskClass.READ,
        side_effect_class=SideEffectClass.WRITE if write else SideEffectClass.READ,
        required_capabilities=frozenset({"memory.write" if write else "memory.read"}),
        concurrency_safe=not write,
        idempotent=not write,
        retryable=not write,
        timeout_ms=15_000,
        output_limit_bytes=_OUTPUT_LIMIT,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    )


_DEFINITIONS = (
    _definition(
        name="memory.search",
        description=(
            "Search only confirmed, unexpired personal memories in the active profile/Session scope. "
            "Returns source-bound records; never searches document knowledge."
        ),
        input_schema=_object(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 4096},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            },
            ("query",),
        ),
        output_schema=_object(
            {
                "memories": {
                    "type": "array",
                    "maxItems": 20,
                    "items": _object(
                        {"memory": _MEMORY, "score": {"type": "number", "minimum": 0, "maximum": 1}},
                        ("memory", "score"),
                    ),
                }
            },
            ("memories",),
        ),
        write=False,
    ),
    _definition(
        name="memory.get",
        description="Read one personal-memory record by ID after enforcing Workspace, profile, and Session scope.",
        input_schema=_object({"memoryId": _MEMORY_ID}, ("memoryId",)),
        output_schema=_object({"memory": _MEMORY}, ("memory",)),
        write=False,
    ),
    _definition(
        name="memory.pending",
        description=(
            "List unexpired personal-memory proposals in the active profile/Session scope so a later explicit "
            "user confirmation can reference the correct memory ID. Proposed content remains excluded from recall."
        ),
        input_schema=_object(
            {"limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}},
            (),
        ),
        output_schema=_object(
            {
                "memories": {
                    "type": "array",
                    "maxItems": 20,
                    "items": _MEMORY,
                }
            },
            ("memories",),
        ),
        write=False,
    ),
    _definition(
        name="memory.remember",
        description=(
            "Persist a confirmed memory only when content is an exact span of evidenceQuote and evidenceQuote is an "
            "exact span of the current root user's explicit instruction. A confirmed item with the same key/scope "
            "is revisioned to superseded; credentials are rejected."
        ),
        input_schema=_object(
            _WRITE_PROPERTIES,
            ("memoryKey", "kind", "scope", "content", "evidenceQuote"),
        ),
        output_schema=_object({"memory": _MEMORY}, ("memory",)),
        write=True,
    ),
    _definition(
        name="memory.propose",
        description=(
            "Store an unconfirmed memory proposal supported by an exact quote from the current root user input. "
            "Use for model inferences; proposed items are excluded from recall until explicitly confirmed."
        ),
        input_schema=_object(
            _WRITE_PROPERTIES,
            ("memoryKey", "kind", "scope", "content", "evidenceQuote"),
        ),
        output_schema=_object({"memory": _MEMORY}, ("memory",)),
        write=True,
    ),
    _definition(
        name="memory.confirm",
        description=(
            "Confirm one proposed memory using an exact quote from the current root user input. Conflicting "
            "confirmed memory with the same key/scope becomes superseded."
        ),
        input_schema=_object(
            {
                "memoryId": _MEMORY_ID,
                "evidenceQuote": {"type": "string", "minLength": 1, "maxLength": 4096},
            },
            ("memoryId", "evidenceQuote"),
        ),
        output_schema=_object({"memory": _MEMORY}, ("memory",)),
        write=True,
    ),
    _definition(
        name="memory.forget",
        description=(
            "Tombstone one active personal memory after an exact current-user deletion quote. Forgotten content "
            "is excluded from all future recall but its lifecycle audit remains."
        ),
        input_schema=_object(
            {
                "memoryId": _MEMORY_ID,
                "evidenceQuote": {"type": "string", "minLength": 1, "maxLength": 4096},
            },
            ("memoryId", "evidenceQuote"),
        ),
        output_schema=_object({"memory": _MEMORY}, ("memory",)),
        write=True,
    ),
    _definition(
        name="memory.history",
        description="Read the append-only lifecycle events for one in-scope personal memory.",
        input_schema=_object({"memoryId": _MEMORY_ID}, ("memoryId",)),
        output_schema=_object(
            {
                "events": {
                    "type": "array",
                    "maxItems": 256,
                    "items": _object(
                        {
                            "schemaVersion": {"const": 1},
                            "eventId": {"type": "string", "minLength": 1, "maxLength": 256},
                            "memoryId": _MEMORY_ID,
                            "workspaceId": {"type": "string", "minLength": 1, "maxLength": 256},
                            "profileId": {"type": "string", "minLength": 1, "maxLength": 256},
                            "eventType": {"enum": ["proposed", "confirmed", "superseded", "forgotten"]},
                            "status": {"enum": ["proposed", "confirmed", "superseded", "forgotten", "expired"]},
                            "occurredAt": {"type": "string", "minLength": 20, "maxLength": 64},
                            "actorRunId": {"type": "string", "minLength": 1, "maxLength": 256},
                            "sourceTurnId": {"type": "string", "minLength": 1, "maxLength": 256},
                            "revision": {"type": "integer", "minimum": 1},
                        },
                        (
                            "schemaVersion",
                            "eventId",
                            "memoryId",
                            "workspaceId",
                            "profileId",
                            "eventType",
                            "status",
                            "occurredAt",
                            "actorRunId",
                            "sourceTurnId",
                            "revision",
                        ),
                    ),
                }
            },
            ("events",),
        ),
        write=False,
    ),
)


def memory_tool_definitions() -> tuple[ToolDefinition, ...]:
    return _DEFINITIONS


class MemoryToolExecutor:
    def __init__(self, repository: MemoryRepository) -> None:
        self._repository = repository
        handlers = (
            self._search,
            self._get,
            self._pending,
            self._remember,
            self._propose,
            self._confirm,
            self._forget,
            self._history,
        )
        self._operations = MappingProxyType(
            {
                (definition.name, definition.version): (definition, handler)
                for definition, handler in zip(_DEFINITIONS, handlers, strict=True)
            }
        )

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return _DEFINITIONS

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        operation = self._operations.get((call.name, call.version))
        if operation is None or operation[0].fingerprint != call.definition_fingerprint:
            return _failure(call, "memory_tool_unavailable", "Memory operation is not registered in this Run")
        try:
            arguments = thaw_json(call.arguments)
            if not isinstance(arguments, Mapping):
                raise ValueError("memory arguments must be an object")
            return await operation[1](call, arguments, cancellation)
        except OperationCancelled:
            raise
        except (KeyError, TypeError, ValueError, MemoryStoreError) as error:
            return _failure(call, "memory_validation_failed", str(error))
        except OSError:
            return _failure(call, "memory_io_failed", "Memory storage operation failed safely")

    async def _search(
        self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken
    ) -> ToolResult:
        query = _string(arguments, "query")
        limit = _integer(arguments.get("limit", 8), "limit")
        hits = await self._repository.search(call, query=query, limit=limit, cancellation=cancellation)
        data = {"memories": [{"memory": memory_item_to_json(hit.item), "score": hit.score} for hit in hits]}
        refs = tuple(_memory_ref(hit.item) for hit in hits)
        return _success(call, data, refs, "已检索已确认的个人记忆")

    async def _get(self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken) -> ToolResult:
        item = await self._repository.get(call, _string(arguments, "memoryId"), cancellation)
        return _success(call, {"memory": memory_item_to_json(item)}, (_memory_ref(item),), "已读取个人记忆")

    async def _pending(
        self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken
    ) -> ToolResult:
        items = await self._repository.pending(
            call,
            limit=_integer(arguments.get("limit", 8), "limit"),
            cancellation=cancellation,
        )
        return _success(
            call,
            {"memories": [memory_item_to_json(item) for item in items]},
            tuple(_memory_ref(item) for item in items),
            "已读取待确认的个人记忆提案",
        )

    async def _remember(
        self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken
    ) -> ToolResult:
        item = await self._repository.remember(
            call,
            **_write_arguments(arguments),
            cancellation=cancellation,
        )
        return _write_success(call, item, "已保存经用户原文确认的个人记忆")

    async def _propose(
        self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken
    ) -> ToolResult:
        item = await self._repository.propose(
            call,
            **_write_arguments(arguments),
            cancellation=cancellation,
        )
        return _write_success(call, item, "已保存待确认的个人记忆提案")

    async def _confirm(
        self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken
    ) -> ToolResult:
        item = await self._repository.confirm(
            call,
            memory_id=_string(arguments, "memoryId"),
            evidence_quote=_string(arguments, "evidenceQuote"),
            cancellation=cancellation,
        )
        return _write_success(call, item, "已确认个人记忆")

    async def _forget(
        self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken
    ) -> ToolResult:
        item = await self._repository.forget(
            call,
            memory_id=_string(arguments, "memoryId"),
            evidence_quote=_string(arguments, "evidenceQuote"),
            cancellation=cancellation,
        )
        return _write_success(call, item, "已遗忘指定个人记忆")

    async def _history(
        self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken
    ) -> ToolResult:
        events = await self._repository.history(
            call,
            _string(arguments, "memoryId"),
            cancellation,
        )
        refs = tuple(f"memory:{event.memory_id}:event:{event.event_id}" for event in events)
        return _success(
            call,
            {"events": [memory_event_to_json(event) for event in events]},
            refs,
            "已读取个人记忆生命周期",
        )


def _write_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "key": _string(arguments, "memoryKey"),
        "kind": MemoryKind(_string(arguments, "kind")),
        "scope": MemoryScope(_string(arguments, "scope")),
        "content": _string(arguments, "content"),
        "evidence_quote": _string(arguments, "evidenceQuote"),
        "pinned": _boolean(arguments.get("pinned", False), "pinned"),
        "importance": _integer(arguments.get("importance", 3), "importance"),
        "expires_at": _optional_datetime(arguments.get("expiresAt"), "expiresAt"),
    }


def _success(
    call: ToolCall,
    data: Mapping[str, Any],
    refs: tuple[str, ...],
    summary: str,
) -> ToolResult:
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data=data,
        user_visible_summary=summary,
        artifact_ids=(),
        source_refs=refs,
        side_effects=(
            SideEffect(
                SideEffectKind.READ,
                SideEffectState.OBSERVED,
                f"memory-query:{call.tool_call_id}",
                None,
                {"sourceCount": len(refs)},
                {"backend": "workspace-sqlite"},
            ),
        ),
        retryable=False,
        before_state=None,
        after_state=None,
        error=None,
    )


def _write_success(call: ToolCall, item: MemoryItem, summary: str) -> ToolResult:
    data = {"memory": memory_item_to_json(item)}
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data=data,
        user_visible_summary=summary,
        artifact_ids=(),
        source_refs=(_memory_ref(item), _source_ref(item)),
        side_effects=(
            SideEffect(
                SideEffectKind.EXTERNAL_SYSTEM,
                SideEffectState.COMMITTED,
                _memory_ref(item),
                None,
                {"status": item.status.value, "revision": item.revision},
                {"backend": "workspace-sqlite", "localOnly": True},
            ),
        ),
        retryable=False,
        before_state=None,
        after_state=data,
        error=None,
    )


def _failure(call: ToolCall, code: str, message: str) -> ToolResult:
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.FAILED,
        data=None,
        user_visible_summary="个人记忆操作未执行",
        artifact_ids=(),
        source_refs=(),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state=None,
        error=ToolError(code, message, retryable=False, cancelled=False),
    )


def _memory_ref(item: MemoryItem) -> str:
    return f"memory:{item.memory_id}"


def _source_ref(item: MemoryItem) -> str:
    return f"session:{item.source.session_id}:turn:{item.source.turn_id}:run:{item.source.run_id}"


def _string(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value or value.strip() != value or "\x00" in value:
        raise ValueError(f"{key} must be canonical non-empty text")
    return value


def _integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _optional_datetime(value: Any, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 timestamp or null")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return result


__all__ = ["MEMORY_TOOL_VERSION", "MemoryToolExecutor", "memory_tool_definitions"]
