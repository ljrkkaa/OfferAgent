"""Plugin-owned tool execution broker for the direct Worker connection."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import cast

from offeragent_harness.models import thaw_json
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import ApplicationCommandContext, CancellationToken
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.common import ToolCallStatus, ToolResultDescriptor
from offeragent_harness.protocol.content import ArtifactSourceRef, ProjectSourceRef, VaultSourceRef
from offeragent_harness.protocol.messages import PluginToolCompleteParams, PluginToolCompleteResult
from offeragent_harness.runtime.application_dispatcher import ApplicationCommandHandler
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffectClass,
    SideEffectKind,
    SideEffectState,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
)
from offeragent_harness.tools import (
    SideEffect as DomainSideEffect,
)


class PluginToolExecutionError(RuntimeError):
    pass


class PluginToolNotPending(PluginToolExecutionError):
    pass


class PluginToolBindingMismatch(PluginToolExecutionError):
    pass


class PluginToolCompletionDisposition(str, Enum):
    ACCEPTED = "accepted"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class PluginToolCompletion:
    workspace_id: str
    run_id: str
    tool_call_id: str
    definition_fingerprint: str
    args_hash: str
    idempotency_key: str
    result: ToolResult

    @classmethod
    def from_call(cls, call: ToolCall, result: ToolResult) -> PluginToolCompletion:
        return cls(
            workspace_id=call.workspace_id,
            run_id=call.run_id,
            tool_call_id=call.tool_call_id,
            definition_fingerprint=call.definition_fingerprint,
            args_hash=call.args_hash,
            idempotency_key=call.idempotency_key,
            result=result,
        )


@dataclass(slots=True)
class _PendingCompletion:
    call: ToolCall
    future: asyncio.Future[ToolResult]


class PluginToolExecutor:
    """Await a result from the plugin that owns the requested capability."""

    def __init__(
        self,
        *,
        registration_timeout_seconds: float = 5.0,
        max_completed_receipts: int = 10_000,
    ) -> None:
        if registration_timeout_seconds <= 0:
            raise ValueError("registration_timeout_seconds must be positive")
        if max_completed_receipts <= 0:
            raise ValueError("max_completed_receipts must be positive")
        self._lock = asyncio.Lock()
        self._registered = asyncio.Condition(self._lock)
        self._pending: dict[str, _PendingCompletion] = {}
        self._completed: OrderedDict[str, PluginToolCompletion] = OrderedDict()
        self._registration_timeout_seconds = registration_timeout_seconds
        self._max_completed_receipts = max_completed_receipts

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        future: asyncio.Future[ToolResult] = asyncio.get_running_loop().create_future()
        async with self._registered:
            if call.tool_call_id in self._pending:
                raise PluginToolExecutionError(f"plugin tool call {call.tool_call_id!r} is already pending")
            prior = self._completed.get(call.tool_call_id)
            if prior is not None:
                if prior != PluginToolCompletion.from_call(call, prior.result):
                    raise PluginToolBindingMismatch(call.tool_call_id)
                self._completed.move_to_end(call.tool_call_id)
                return prior.result
            self._pending[call.tool_call_id] = _PendingCompletion(call, future)
            self._registered.notify_all()
        cancel_wait = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait((future, cancel_wait), return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait in done:
                cancellation.checkpoint()
            return await asyncio.shield(future)
        finally:
            if not cancel_wait.done():
                cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)
            async with self._lock:
                current = self._pending.get(call.tool_call_id)
                if current is not None and current.future is future:
                    self._pending.pop(call.tool_call_id, None)

    async def complete(self, completion: PluginToolCompletion) -> PluginToolCompletionDisposition:
        async with self._registered:
            prior = self._completed.get(completion.tool_call_id)
            if prior is not None:
                if prior != completion:
                    raise PluginToolBindingMismatch(completion.tool_call_id)
                self._completed.move_to_end(completion.tool_call_id)
                return PluginToolCompletionDisposition.REPLAYED
            try:
                await asyncio.wait_for(
                    self._registered.wait_for(
                        lambda: completion.tool_call_id in self._pending or completion.tool_call_id in self._completed
                    ),
                    timeout=self._registration_timeout_seconds,
                )
            except TimeoutError:
                raise PluginToolNotPending(completion.tool_call_id) from None
            prior = self._completed.get(completion.tool_call_id)
            if prior is not None:
                if prior != completion:
                    raise PluginToolBindingMismatch(completion.tool_call_id)
                self._completed.move_to_end(completion.tool_call_id)
                return PluginToolCompletionDisposition.REPLAYED
            pending = self._pending.get(completion.tool_call_id)
            assert pending is not None
            call = pending.call
            binding = (
                completion.workspace_id,
                completion.run_id,
                completion.tool_call_id,
                completion.definition_fingerprint,
                completion.args_hash,
                completion.idempotency_key,
                completion.result.tool_call_id,
            )
            expected = (
                call.workspace_id,
                call.run_id,
                call.tool_call_id,
                call.definition_fingerprint,
                call.args_hash,
                call.idempotency_key,
                call.tool_call_id,
            )
            if binding != expected:
                raise PluginToolBindingMismatch(completion.tool_call_id)
            if not pending.future.done():
                pending.future.set_result(completion.result)
            self._completed[completion.tool_call_id] = completion
            self._completed.move_to_end(completion.tool_call_id)
            while len(self._completed) > self._max_completed_receipts:
                self._completed.popitem(last=False)
            self._registered.notify_all()
            return PluginToolCompletionDisposition.ACCEPTED


_RESULT_STATUS = {
    ToolCallStatus.SUCCEEDED: ToolResultStatus.SUCCEEDED,
    ToolCallStatus.FAILED: ToolResultStatus.FAILED,
    ToolCallStatus.DENIED: ToolResultStatus.DENIED,
    ToolCallStatus.CANCELLED: ToolResultStatus.CANCELLED,
    ToolCallStatus.TIMED_OUT: ToolResultStatus.TIMED_OUT,
    ToolCallStatus.CONFLICT: ToolResultStatus.CONFLICTED,
    ToolCallStatus.PARTIAL: ToolResultStatus.PARTIAL,
    ToolCallStatus.UNKNOWN_OUTCOME: ToolResultStatus.UNKNOWN_OUTCOME,
}

_SIDE_EFFECT_KIND = {
    "file_created": SideEffectKind.FILE_WRITE,
    "file_modified": SideEffectKind.FILE_WRITE,
    "file_renamed": SideEffectKind.FILE_RENAME,
    "file_trashed": SideEffectKind.FILE_TRASH,
    "process": SideEffectKind.PROCESS,
    "network": SideEffectKind.NETWORK,
}


def _source_reference_id(reference: VaultSourceRef | ArtifactSourceRef | ProjectSourceRef) -> str:
    if isinstance(reference, VaultSourceRef):
        revision = reference.file.content_hash or "current"
        return f"vault:{reference.file.workspace_id}:{reference.file.path}:{revision}"
    if isinstance(reference, ProjectSourceRef):
        return f"project:{reference.project_id}:{reference.path}:{reference.content_hash}"
    return f"artifact:{reference.artifact.artifact_id}"


def _domain_tool_result(descriptor: ToolResultDescriptor) -> ToolResult:
    status = _RESULT_STATUS[descriptor.status]
    error = None
    if descriptor.error is not None:
        error = ToolError(
            code=descriptor.error.code.value,
            message=descriptor.error.user_visible_message,
            retryable=descriptor.error.retryable,
            cancelled=descriptor.error.cancelled,
            details=thaw_json(descriptor.error.details),
        )
    side_effects = tuple(
        DomainSideEffect(
            kind=_SIDE_EFFECT_KIND[item.kind],
            state=(
                SideEffectState.UNKNOWN
                if status is ToolResultStatus.UNKNOWN_OUTCOME
                else SideEffectState.COMMITTED
                if item.confirmed
                else SideEffectState.ATTEMPTED
            ),
            resource_id=item.resource,
            before_state=None if item.before_hash is None else {"contentHash": item.before_hash},
            after_state=None if item.after_hash is None else {"contentHash": item.after_hash},
            metadata={"protocolKind": item.kind},
        )
        for item in descriptor.side_effects
    )
    return ToolResult(
        tool_call_id=descriptor.tool_call_id,
        status=status,
        data=thaw_json(descriptor.data),
        user_visible_summary=descriptor.summary,
        artifact_ids=tuple(item.artifact_id for item in descriptor.artifact_refs),
        source_refs=tuple(_source_reference_id(item) for item in descriptor.source_refs),
        side_effects=side_effects,
        retryable=descriptor.retryable,
        before_state=None,
        after_state=None,
        error=error,
        source_references=tuple(item.to_wire() for item in descriptor.source_refs),
    )


def plugin_tool_completion_handlers(*, executor: PluginToolExecutor) -> Mapping[str, ApplicationCommandHandler]:
    """Expose the single plugin-to-Worker completion command."""

    async def complete(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        if context.transport != "stdio":
            raise ValueError("plugin Tool completions are accepted only from the direct stdio peer")
        cancellation.checkpoint()
        params = cast(PluginToolCompleteParams, raw)
        result = _domain_tool_result(params.result)
        disposition = await executor.complete(
            PluginToolCompletion(
                workspace_id=params.workspace_id,
                run_id=params.run_id,
                tool_call_id=result.tool_call_id,
                definition_fingerprint=params.definition_fingerprint,
                args_hash=params.args_hash,
                idempotency_key=params.idempotency_key,
                result=result,
            )
        )
        return PluginToolCompleteResult(
            accepted=True,
            replayed=disposition is PluginToolCompletionDisposition.REPLAYED,
        )

    return {"plugin-tools/complete": complete}


def plugin_tool_definitions() -> tuple[ToolDefinition, ...]:
    """Definitions implemented only by the connected Obsidian plugin."""

    return (
        _plugin_read_definition(
            name="agent_contract.read",
            required_capability="agent_contract.read",
            description="Read the current Vault's agent.md Agent Contract through the Obsidian Vault API.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "contentHash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                },
                "required": ["path", "content", "contentHash"],
                "additionalProperties": False,
            },
            output_limit_bytes=65_536,
        ),
        _plugin_read_definition(
            name="skill.read",
            required_capability="skill.read",
            description=(
                "Read SKILL.md or one directly referenced resource from an explicitly selected "
                "Vault-local Skill; paths cannot traverse outside that Skill."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]*$",
                    },
                    "resource": {"type": "string", "minLength": 1, "maxLength": 512},
                },
                "required": ["skill"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "skill": {"type": "string"},
                    "resource": {"type": "string"},
                    "path": {"type": "string"},
                    "modifiedVersion": {"type": "string"},
                    "contentHash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    "content": {"type": "string", "maxLength": 32_768},
                },
                "required": [
                    "skill",
                    "resource",
                    "path",
                    "modifiedVersion",
                    "contentHash",
                    "content",
                ],
                "additionalProperties": False,
            },
            output_limit_bytes=65_536,
        ),
        _plugin_read_definition(
            name="daily_note.context",
            required_capability="daily_note.read",
            description=(
                "Resolve the current Vault's Daily Notes configuration, local date, existing note, "
                "and template without creating or modifying a note."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "resolvedDate": {"type": "string"},
                    "dateFormat": {"type": "string"},
                    "targetPath": {"type": "string"},
                    "targetExists": {"type": "boolean"},
                    "targetContent": {"type": ["string", "null"], "maxLength": 32_768},
                    "targetModifiedVersion": {"type": ["string", "null"]},
                    "targetContentHash": {
                        "type": ["string", "null"],
                        "pattern": "^sha256:[0-9a-f]{64}$",
                    },
                    "templatePath": {"type": ["string", "null"]},
                    "templateContent": {"type": ["string", "null"], "maxLength": 32_768},
                    "templateModifiedVersion": {"type": ["string", "null"]},
                    "templateContentHash": {
                        "type": ["string", "null"],
                        "pattern": "^sha256:[0-9a-f]{64}$",
                    },
                },
                "required": [
                    "resolvedDate",
                    "dateFormat",
                    "targetPath",
                    "targetExists",
                    "targetContent",
                    "targetModifiedVersion",
                    "targetContentHash",
                    "templatePath",
                    "templateContent",
                    "templateModifiedVersion",
                    "templateContentHash",
                ],
                "additionalProperties": False,
            },
            output_limit_bytes=65_536,
        ),
        _plugin_read_definition(
            name="vault.list",
            description="List bounded current Markdown and text files from the Obsidian Vault.",
            input_schema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "maxLength": 512},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "entries": {"type": "array", "maxItems": 100, "items": {"type": "object"}},
                    "truncated": {"type": "boolean"},
                },
                "required": ["entries", "truncated"],
                "additionalProperties": False,
            },
            output_limit_bytes=131_072,
        ),
        _plugin_read_definition(
            name="vault.search",
            description="Locate bounded current Vault candidates by keyword; follow with vault.read before answering.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 512},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "snippetsPerFile": {"type": "integer", "minimum": 1, "maximum": 3},
                    "snippetMaxBytes": {"type": "integer", "minimum": 16, "maximum": 512},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "entries": {"type": "array", "maxItems": 20, "items": {"type": "object"}},
                    "truncated": {"type": "boolean"},
                },
                "required": ["entries", "truncated"],
                "additionalProperties": False,
            },
            output_limit_bytes=65_536,
        ),
        _plugin_read_definition(
            name="vault.read",
            description="Read an exact bounded line range from current Vault evidence with optional version binding.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 512},
                    "lineStart": {"type": "integer", "minimum": 1},
                    "lineEnd": {"type": "integer", "minimum": 1},
                    "expectedContentHash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    "expectedModifiedVersion": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "lineStart": {"type": "integer", "minimum": 1},
                    "lineEnd": {"type": "integer", "minimum": 1},
                    "modifiedVersion": {"type": "string"},
                    "contentHash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    "content": {"type": "string", "maxLength": 32_768},
                    "truncated": {"type": "boolean"},
                },
                "required": [
                    "path",
                    "lineStart",
                    "lineEnd",
                    "modifiedVersion",
                    "contentHash",
                    "content",
                    "truncated",
                ],
                "additionalProperties": False,
            },
            output_limit_bytes=65_536,
        ),
        _plugin_read_definition(
            name="project.list",
            required_capability="project.read",
            description="List bounded text sources from a Vault-registered external Project root.",
            input_schema={
                "type": "object",
                "properties": {
                    "projectId": {"type": "string", "minLength": 1, "maxLength": 64},
                    "directory": {"type": "string", "maxLength": 512},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["projectId"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "projectId": {"type": "string"},
                    "entries": {"type": "array", "maxItems": 100, "items": {"type": "object"}},
                    "truncated": {"type": "boolean"},
                },
                "required": ["projectId", "entries", "truncated"],
                "additionalProperties": False,
            },
            output_limit_bytes=131_072,
        ),
        _plugin_read_definition(
            name="project.search",
            required_capability="project.read",
            description=(
                "Locate bounded candidates in a Vault-registered Project; follow with project.read "
                "using the returned version before answering."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "projectId": {"type": "string", "minLength": 1, "maxLength": 64},
                    "query": {"type": "string", "minLength": 1, "maxLength": 512},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "snippetMaxBytes": {"type": "integer", "minimum": 16, "maximum": 512},
                },
                "required": ["projectId", "query"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "projectId": {"type": "string"},
                    "entries": {"type": "array", "maxItems": 20, "items": {"type": "object"}},
                    "truncated": {"type": "boolean"},
                },
                "required": ["projectId", "entries", "truncated"],
                "additionalProperties": False,
            },
            output_limit_bytes=65_536,
        ),
        _plugin_read_definition(
            name="project.read",
            required_capability="project.read",
            description="Read an exact bounded line range from a registered Project with optional version binding.",
            input_schema={
                "type": "object",
                "properties": {
                    "projectId": {"type": "string", "minLength": 1, "maxLength": 64},
                    "path": {"type": "string", "minLength": 1, "maxLength": 512},
                    "lineStart": {"type": "integer", "minimum": 1},
                    "lineEnd": {"type": "integer", "minimum": 1},
                    "expectedContentHash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    "expectedModifiedVersion": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "required": ["projectId", "path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "projectId": {"type": "string"},
                    "path": {"type": "string"},
                    "lineStart": {"type": "integer", "minimum": 1},
                    "lineEnd": {"type": "integer", "minimum": 1},
                    "modifiedVersion": {"type": "string"},
                    "contentHash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    "content": {"type": "string", "maxLength": 32_768},
                    "truncated": {"type": "boolean"},
                },
                "required": [
                    "projectId",
                    "path",
                    "lineStart",
                    "lineEnd",
                    "modifiedVersion",
                    "contentHash",
                    "content",
                    "truncated",
                ],
                "additionalProperties": False,
            },
            output_limit_bytes=65_536,
        ),
        _plugin_write_definition(),
    )


def _plugin_write_definition() -> ToolDefinition:
    path = {"type": "string", "minLength": 1, "maxLength": 512}
    digest = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
    content = {"type": "string", "maxLength": 262_144}

    def operation(properties: Mapping[str, object], required: list[str]) -> dict[str, object]:
        return {
            "type": "object",
            "properties": dict(properties),
            "required": required,
            "additionalProperties": False,
        }

    operations = (
        operation(
            {"op": {"const": "create"}, "path": path, "content": content, "expectedContentHash": {"const": "absent"}},
            ["op", "path", "content", "expectedContentHash"],
        ),
        operation(
            {"op": {"const": "append"}, "path": path, "content": content, "expectedContentHash": digest},
            ["op", "path", "content", "expectedContentHash"],
        ),
        operation(
            {
                "op": {"const": "replace"},
                "path": path,
                "find": {"type": "string", "minLength": 1, "maxLength": 262_144},
                "replacement": content,
                "expectedContentHash": digest,
            },
            ["op", "path", "find", "replacement", "expectedContentHash"],
        ),
        operation(
            {
                "op": {"const": "patch"},
                "path": path,
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 256,
                    "items": operation(
                        {
                            "startLine": {"type": "integer", "minimum": 1},
                            "endLine": {"type": "integer", "minimum": 1},
                            "replacement": content,
                        },
                        ["startLine", "endLine", "replacement"],
                    ),
                },
                "expectedContentHash": digest,
            },
            ["op", "path", "edits", "expectedContentHash"],
        ),
        operation(
            {"op": {"const": "delete"}, "path": path, "expectedContentHash": digest},
            ["op", "path", "expectedContentHash"],
        ),
    )
    return ToolDefinition(
        name="vault.changes.apply",
        version="1",
        description=(
            "Apply one logical, hash-bound Vault Change Batch through the Obsidian plugin. "
            "Delete is restricted by the plugin to Planning Memory topics."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "batchId": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]*$",
                },
                "task": {"type": "string", "minLength": 1, "maxLength": 512},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {"oneOf": list(operations)},
                },
            },
            "required": ["batchId", "task", "operations"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "batchId": {"type": "string"},
                "state": {"enum": ["applied", "rolled_back", "undone"]},
                "checkpointRef": {"type": "string", "minLength": 1, "maxLength": 512},
                "paths": {"type": "array", "minItems": 1, "maxItems": 20, "items": path},
                "beforeStateHash": digest,
                "afterStateHash": digest,
                "undoAvailable": {"type": "boolean"},
            },
            "required": [
                "batchId",
                "state",
                "checkpointRef",
                "paths",
                "beforeStateHash",
                "afterStateHash",
                "undoAvailable",
            ],
            "additionalProperties": False,
        },
        executor_location=ExecutorLocation.PLUGIN,
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        required_capabilities=frozenset({"vault.write"}),
        concurrency_safe=False,
        idempotent=True,
        retryable=False,
        timeout_ms=300_000,
        output_limit_bytes=131_072,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    )


def _plugin_read_definition(
    *,
    name: str,
    description: str,
    input_schema: Mapping[str, object],
    output_schema: Mapping[str, object],
    output_limit_bytes: int,
    required_capability: str = "vault.read",
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version="1",
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        executor_location=ExecutorLocation.PLUGIN,
        risk=RiskClass.READ,
        side_effect_class=SideEffectClass.READ,
        required_capabilities=frozenset({required_capability}),
        concurrency_safe=True,
        idempotent=True,
        retryable=True,
        timeout_ms=10_000,
        output_limit_bytes=output_limit_bytes,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    )


__all__ = [
    "PluginToolBindingMismatch",
    "PluginToolCompletion",
    "PluginToolCompletionDisposition",
    "PluginToolExecutionError",
    "PluginToolExecutor",
    "PluginToolNotPending",
    "plugin_tool_completion_handlers",
    "plugin_tool_definitions",
]
