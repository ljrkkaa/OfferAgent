"""The single model-facing tool that invokes a discovered Skill lazily."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports.cancellation import CancellationToken, OperationCancelled
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

from .catalog import SkillCatalog
from .models import SkillAuthority, SkillError

SKILL_TOOL_VERSION = "1"
SKILL_TOOL_OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024


@runtime_checkable
class SkillAuthorityProvider(Protocol):
    async def authority_for(self, call: ToolCall) -> SkillAuthority: ...


def _object(properties: Mapping[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": dict(properties),
        "required": required,
        "additionalProperties": False,
    }


_HASH = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
_INSTRUCTION = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "maxLength": 1_000_000},
        "sourceRef": {"type": "string", "minLength": 1, "maxLength": 2048},
        "metadataHash": _HASH,
        "precedence": {"const": "untrusted_skill"},
    },
    "required": ["text", "sourceRef", "metadataHash", "precedence"],
    "additionalProperties": False,
}

_DEFINITIONS = (
    ToolDefinition(
        name="skill",
        version=SKILL_TOOL_VERSION,
        description=(
            "Invoke one available Skill by name. Use this before any other tool when the user's request "
            "matches an available Skill description; the complete Skill instructions are loaded only by this call."
        ),
        input_schema=_object(
            {
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "arguments": {"type": "string", "maxLength": 16_384, "default": ""},
            },
            ["name"],
        ),
        output_schema=_object(
            {
                "workspaceId": {"type": "string", "minLength": 1},
                "revision": {"type": "integer", "minimum": 0},
                "snapshotHash": _HASH,
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "metadataHash": _HASH,
                "instruction": _INSTRUCTION,
                "arguments": {"type": "string", "maxLength": 16_384},
                "effectiveAllowedTools": {"type": "array", "items": {"type": "string"}, "maxItems": 256},
                "unavailableDeclaredTools": {"type": "array", "items": {"type": "string"}, "maxItems": 256},
                "totalBytes": {"type": "integer", "minimum": 0},
            },
            [
                "workspaceId",
                "revision",
                "snapshotHash",
                "name",
                "metadataHash",
                "instruction",
                "arguments",
                "effectiveAllowedTools",
                "unavailableDeclaredTools",
                "totalBytes",
            ],
        ),
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.READ,
        side_effect_class=SideEffectClass.READ,
        required_capabilities=frozenset({"skills.read"}),
        concurrency_safe=True,
        idempotent=True,
        retryable=True,
        timeout_ms=30_000,
        output_limit_bytes=SKILL_TOOL_OUTPUT_LIMIT_BYTES,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    ),
)


def skill_tool_definitions() -> tuple[ToolDefinition, ...]:
    return _DEFINITIONS


SkillHandler = Callable[[ToolCall, Mapping[str, Any], CancellationToken], Awaitable[ToolResult]]


class SkillToolExecutor:
    def __init__(self, *, workspace_id: str, catalog: SkillCatalog, authority_provider: SkillAuthorityProvider) -> None:
        if not workspace_id or catalog.workspace_id != workspace_id:
            raise ValueError("Skill executor/catalog Workspace mismatch")
        self._workspace_id = workspace_id
        self._catalog = catalog
        self._authority_provider = authority_provider
        self._operations = MappingProxyType(
            {
                (definition.name, definition.version): (definition, handler)
                for definition, handler in zip(_DEFINITIONS, (self._invoke,), strict=True)
            }
        )

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(item[0] for item in self._operations.values())

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        if call.workspace_id != self._workspace_id:
            return _failure(call, "skill_workspace_mismatch", "Skill tool belongs to another Workspace")
        operation = self._operations.get((call.name, call.version))
        if operation is None or operation[0].fingerprint != call.definition_fingerprint:
            return _failure(call, "skill_tool_unavailable", "Skill operation is not registered in this Run")
        try:
            return await operation[1](call, thaw_json(call.arguments), cancellation)
        except OperationCancelled:
            raise
        except SkillError as error:
            return _failure(call, f"skill_{error.code.value}", str(error), path=error.path)
        except (KeyError, TypeError, ValueError) as error:
            return _failure(call, "skill_invalid_arguments", str(error))

    async def _invoke(
        self,
        call: ToolCall,
        arguments: Mapping[str, Any],
        cancellation: CancellationToken,
    ) -> ToolResult:
        name = arguments.get("name")
        invocation_arguments = arguments.get("arguments", "")
        if not isinstance(name, str) or not name:
            raise ValueError("name must be a non-empty string")
        if not isinstance(invocation_arguments, str):
            raise ValueError("arguments must be a string")
        snapshot = self._catalog.snapshot
        selection = self._catalog.select(name=name, authority=await self._authority_provider.authority_for(call))
        loaded = await self._catalog.load(selection, cancellation)
        descriptor = selection.descriptor
        instruction = loaded.instruction
        data = {
            "workspaceId": self._workspace_id,
            "revision": snapshot.revision,
            "snapshotHash": snapshot.snapshot_hash,
            "name": descriptor.name,
            "metadataHash": descriptor.content_hash,
            "instruction": {
                "text": instruction.text,
                "sourceRef": instruction.source_ref,
                "metadataHash": instruction.content_hash,
                "precedence": instruction.precedence,
            },
            "arguments": invocation_arguments,
            "effectiveAllowedTools": sorted(selection.effective_allowed_tools),
            "unavailableDeclaredTools": sorted(selection.unavailable_declared_tools),
            "totalBytes": loaded.total_bytes,
        }
        return _success(call, data, (instruction.source_ref,), f"已调用 Skill {descriptor.name!r}")


def _effects(call: ToolCall, refs: tuple[str, ...]) -> tuple[SideEffect, ...]:
    """Record one catalog access, never one pseudo-effect per discovered file."""

    return (
        SideEffect(
            SideEffectKind.READ,
            SideEffectState.OBSERVED,
            f"workspace:{call.workspace_id}:skills",
            None,
            None,
            {
                "toolCallId": call.tool_call_id,
                "toolName": call.name,
                "argsHash": call.args_hash,
                "sourceCount": len(refs),
            },
        ),
    )


def _success(call: ToolCall, data: Mapping[str, Any], refs: tuple[str, ...], summary: str) -> ToolResult:
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data=dict(data),
        user_visible_summary=summary,
        artifact_ids=(),
        source_refs=refs,
        side_effects=_effects(call, refs),
        retryable=False,
        before_state=None,
        after_state=None,
        error=None,
        source_references=(),
    )


def _failure(call: ToolCall, code: str, message: str, *, path: str | None = None) -> ToolResult:
    details: dict[str, object] = {} if path is None else {"path": path}
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.FAILED,
        data=None,
        user_visible_summary=message,
        artifact_ids=(),
        source_refs=(),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state=None,
        error=ToolError(code, message, False, False, details),
        source_references=(),
    )


__all__ = ["SKILL_TOOL_VERSION", "SkillAuthorityProvider", "SkillToolExecutor", "skill_tool_definitions"]
