"""The two read-only tools that expose lazy Skills to a Run."""

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
from .models import SkillAuthority, SkillError, SkillErrorCode, SkillLayer

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
_SUMMARY = {
    "type": "object",
    "properties": {
        "rootId": {"type": "string", "minLength": 1, "maxLength": 64},
        "packagePath": {"type": "string", "minLength": 1, "maxLength": 2048},
        "name": {"type": "string", "minLength": 1, "maxLength": 64},
        "description": {"type": "string", "minLength": 1, "maxLength": 2048},
        "layer": {"enum": ["builtin", "user", "workspace"]},
        "metadataHash": _HASH,
        "allowedTools": {"type": "array", "items": {"type": "string"}, "maxItems": 256},
        "enabled": {"type": "boolean"},
        "trustState": {"enum": ["verified", "confirmed", "confirmation_required"]},
    },
    "required": [
        "rootId",
        "packagePath",
        "name",
        "description",
        "layer",
        "metadataHash",
        "allowedTools",
        "enabled",
        "trustState",
    ],
    "additionalProperties": False,
}
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
        name="skill.list",
        version=SKILL_TOOL_VERSION,
        description="List discovered local Skill metadata without reading any Skill instruction body.",
        input_schema=_object({"includeShadowed": {"type": "boolean", "default": True}}, []),
        output_schema=_object(
            {
                "workspaceId": {"type": "string", "minLength": 1},
                "revision": {"type": "integer", "minimum": 0},
                "snapshotHash": _HASH,
                "partial": {"type": "boolean"},
                "skills": {"type": "array", "items": _SUMMARY, "maxItems": 1000},
            },
            ["workspaceId", "revision", "snapshotHash", "partial", "skills"],
        ),
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.READ,
        side_effect_class=SideEffectClass.READ,
        required_capabilities=frozenset({"skills.read"}),
        concurrency_safe=True,
        idempotent=True,
        retryable=True,
        timeout_ms=15_000,
        output_limit_bytes=SKILL_TOOL_OUTPUT_LIMIT_BYTES,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    ),
    ToolDefinition(
        name="skill.read",
        version=SKILL_TOOL_VERSION,
        description="Read one trusted Skill body on demand from the active catalog revision.",
        input_schema=_object(
            {
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "expectedRevision": {"type": "integer", "minimum": 0},
            },
            ["name", "expectedRevision"],
        ),
        output_schema=_object(
            {
                "workspaceId": {"type": "string", "minLength": 1},
                "revision": {"type": "integer", "minimum": 0},
                "snapshotHash": _HASH,
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "metadataHash": _HASH,
                "instruction": _INSTRUCTION,
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
                for definition, handler in zip(_DEFINITIONS, (self._list, self._read), strict=True)
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

    async def _list(self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken) -> ToolResult:
        include_shadowed = arguments.get("includeShadowed", True)
        if not isinstance(include_shadowed, bool):
            raise ValueError("includeShadowed must be a boolean")
        authority = await self._authority_provider.authority_for(call)
        snapshot = self._catalog.snapshot
        skills = self._catalog.list(include_shadowed=include_shadowed)
        data: dict[str, Any] = {
            "workspaceId": self._workspace_id,
            "revision": snapshot.revision,
            "snapshotHash": snapshot.snapshot_hash,
            "partial": self._catalog.status().partial,
            "skills": [
                {
                    "rootId": item.root_id,
                    "packagePath": item.package_path,
                    "name": item.name,
                    "description": item.description,
                    "layer": item.layer.value,
                    "metadataHash": item.content_hash,
                    "allowedTools": sorted(item.allowed_tools),
                    "enabled": item.enabled
                    and (authority.enabled_skills is None or item.name in authority.enabled_skills)
                    and (authority.workspace_trusted or item.layer is not SkillLayer.WORKSPACE),
                    "trustState": item.trust_state.value,
                }
                for item in skills
                if authority.enabled_skills is None or item.name in authority.enabled_skills
            ],
        }
        return _success(
            call,
            data,
            (f"skill-catalog:{self._workspace_id}:{snapshot.revision}",),
            f"列出 {len(data['skills'])} 个 Skill",
        )

    async def _read(self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken) -> ToolResult:
        name = arguments.get("name")
        expected_revision = arguments.get("expectedRevision")
        if not isinstance(name, str) or not name:
            raise ValueError("name must be a non-empty string")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expectedRevision must be an integer")
        snapshot = self._catalog.snapshot
        if expected_revision != snapshot.revision:
            raise SkillError(SkillErrorCode.CAS_CONFLICT, "Skill catalog revision changed before read")
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
            "effectiveAllowedTools": sorted(selection.effective_allowed_tools),
            "unavailableDeclaredTools": sorted(selection.unavailable_declared_tools),
            "totalBytes": loaded.total_bytes,
        }
        return _success(call, data, (instruction.source_ref,), f"已按需读取 Skill {descriptor.name!r}")


def _effects(workspace_id: str, refs: tuple[str, ...]) -> tuple[SideEffect, ...]:
    return tuple(
        SideEffect(SideEffectKind.READ, SideEffectState.OBSERVED, ref, None, None, {"workspaceId": workspace_id})
        for ref in refs
    )


def _success(call: ToolCall, data: Mapping[str, Any], refs: tuple[str, ...], summary: str) -> ToolResult:
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data=dict(data),
        user_visible_summary=summary,
        artifact_ids=(),
        source_refs=refs,
        side_effects=_effects(call.workspace_id, refs),
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
