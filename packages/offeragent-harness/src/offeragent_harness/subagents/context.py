"""Immutable Context fork and monotonic Subagent authority derivation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.permissions import CapabilityScope, PermissionMode, RiskClass
from offeragent_harness.ports import Clock, IdGenerator
from offeragent_harness.ports.subagents import ParentRunAuthority
from offeragent_harness.tools import ToolDefinition
from offeragent_harness.tools.canonical import canonical_json_sha256

from .definitions import AgentDefinition
from .models import AgentSpawnCommand, ContextForkMode, ContextSnapshot, EffectiveToolScope

_FORBIDDEN_KEYS = (
    "secret",
    "token",
    "password",
    "credential",
    "cookie",
    "authorization",
    "refresh",
    "filehandle",
    "file_handle",
    "pluginobject",
    "plugin_object",
)


class ContextForkError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ContextForker:
    def __init__(self, ids: IdGenerator, clock: Clock, *, max_bytes: int = 2 * 1024 * 1024) -> None:
        if not 4_096 <= max_bytes <= 32 * 1024 * 1024:
            raise ValueError("Context fork limit is invalid")
        self._ids = ids
        self._clock = clock
        self._max_bytes = max_bytes

    def fork(
        self,
        authority: ParentRunAuthority,
        command: AgentSpawnCommand,
        definition: AgentDefinition,
    ) -> ContextSnapshot:
        parent = _sanitize(thaw_json(authority.context))
        if not isinstance(parent, dict):
            raise ContextForkError("context_corrupt", "parent context must be an object")
        content: dict[str, Any] = {
            "workspaceId": authority.workspace_id,
            "parentRunId": authority.lineage.run_id,
            "rootRunId": authority.lineage.root_run_id,
            "task": command.task.strip(),
            "agent": {"name": definition.name, "version": definition.version},
            "mode": command.context_mode.value,
            "systemPolicy": parent.get("systemPolicy", {}),
            "parentContextHash": canonical_json_sha256(parent),
        }
        if definition.instructions:
            content["agentInstructions"] = definition.instructions
        content["agentSkills"] = list(definition.skills)
        if command.context_mode is ContextForkMode.SUMMARY:
            content["summary"] = parent.get("summary", "")
            content["memories"] = _bounded_array(parent.get("memories", []), 256)
        elif command.context_mode is ContextForkMode.SELECTED:
            content["messages"] = _select_by_id(parent.get("messages", []), command.selected_message_ids, "message")
            content["artifacts"] = _select_artifacts(parent.get("artifacts", []), command.selected_artifact_ids)
            content["fileRefs"] = _bounded_array(parent.get("fileRefs", []), 256)
        elif command.context_mode is ContextForkMode.FULL:
            for key in ("summary", "messages", "artifacts", "fileRefs", "memories", "references", "compactions"):
                if key in parent:
                    content[key] = parent[key]
        encoded = json.dumps(
            content, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
        mode_limit = self._max_bytes if command.context_mode is ContextForkMode.FULL else self._max_bytes // 2
        if len(encoded) > mode_limit:
            raise ContextForkError("context_limit", "filtered child context exceeds its mode limit")
        snapshot_id = self._ids.new_id("ctx")
        return ContextSnapshot(
            snapshot_id,
            authority.workspace_id,
            authority.lineage.run_id,
            command.context_mode,
            content,
            canonical_json_sha256(content),
            self._clock.utcnow(),
        )


@dataclass(frozen=True, slots=True)
class DerivedAuthority:
    permission_mode: PermissionMode
    capability_scope: CapabilityScope
    tool_scope: EffectiveToolScope


class ScopeDeriver:
    def __init__(
        self,
        system_scope: CapabilityScope,
        *,
        coordinated_write_tools: frozenset[str] = frozenset(),
    ) -> None:
        self._system = system_scope
        self._coordinated_write_tools = coordinated_write_tools

    def derive(
        self,
        authority: ParentRunAuthority,
        definition: AgentDefinition,
        command: AgentSpawnCommand,
    ) -> DerivedAuthority:
        if command.requested_permission_mode is PermissionMode.BYPASS:
            raise ContextForkError("scope_widening", "Subagents cannot request bypass permission")
        permission = _narrow_permission(
            authority.permission_mode,
            definition.permission_ceiling,
            command.requested_permission_mode,
        )
        base = self._system.intersect(authority.effective_scope).intersect(command.requested_scope)
        explicit_capabilities = definition.metadata.get("capabilityCeilingExplicit") is True
        root_capabilities = (
            base.root_capabilities & definition.capability_ceiling.root_capabilities
            if explicit_capabilities
            else base.root_capabilities
        )
        allowed_tools = base.allowed_tools & definition.tool_allow
        denied_tools = base.denied_tools | definition.tool_deny
        allowed_risks = base.allowed_risks & definition.capability_ceiling.allowed_risks
        if permission in {PermissionMode.READ_ONLY, PermissionMode.PLAN}:
            allowed_risks &= {RiskClass.READ}
        capability_scope = CapabilityScope(
            allowed_tools=allowed_tools - denied_tools,
            denied_tools=denied_tools,
            allowed_risks=frozenset(allowed_risks),
            root_capabilities=root_capabilities,
            allow_network=(
                base.allow_network
                and definition.capability_ceiling.allow_network
                and permission not in {PermissionMode.READ_ONLY, PermissionMode.PLAN}
            ),
            allow_secret_handles=base.allow_secret_handles and definition.capability_ceiling.allow_secret_handles,
        )
        tool_scope = _derive_tools(
            authority.tool_definitions,
            capability_scope,
            command.requested_tool_versions,
            command.requested_tool_constraints,
            definition,
            authority.registry_snapshot_hash,
            self._coordinated_write_tools,
        )
        return DerivedAuthority(permission, capability_scope, tool_scope)


def _derive_tools(
    definitions: Sequence[ToolDefinition],
    scope: CapabilityScope,
    requested_versions: Mapping[str, tuple[str, ...]],
    requested_constraints: Mapping[str, Mapping[str, Any]],
    profile: AgentDefinition,
    registry_hash: str,
    coordinated_write_tools: frozenset[str],
) -> EffectiveToolScope:
    by_name: dict[str, list[str]] = {}
    for definition in definitions:
        if (
            definition.name in scope.allowed_tools
            and definition.name not in scope.denied_tools
            and definition.risk in scope.allowed_risks
            and definition.required_capabilities <= scope.root_capabilities
            and (
                definition.name.startswith("agent.")
                or definition.risk not in {RiskClass.WRITE, RiskClass.DESTRUCTIVE, RiskClass.EXTERNAL_PATH}
                or definition.name in coordinated_write_tools
            )
        ):
            by_name.setdefault(definition.name, []).append(definition.version)
    profile_versions = profile.metadata.get("toolVersions", {})
    if not isinstance(profile_versions, Mapping):
        raise ContextForkError("definition_scope", "Agent Definition toolVersions is invalid")
    allowed_versions: dict[str, tuple[str, ...]] = {}
    constraints: dict[str, Mapping[str, Any]] = {}
    profile_constraints = profile.metadata.get("toolConstraints", {})
    if not isinstance(profile_constraints, Mapping):
        raise ContextForkError("definition_scope", "Agent Definition toolConstraints is invalid")
    for name, available in sorted(by_name.items()):
        selected = set(available)
        requested = requested_versions.get(name)
        if requested:
            selected &= set(requested)
        configured = profile_versions.get(name)
        if configured is not None:
            if not isinstance(configured, (list, tuple)) or any(not isinstance(item, str) for item in configured):
                raise ContextForkError("definition_scope", f"Agent Definition versions for {name} are invalid")
            selected &= set(configured)
        if not selected:
            continue
        allowed_versions[name] = tuple(sorted(selected))
        schemas: list[Mapping[str, Any]] = []
        for candidate in (profile_constraints.get(name), requested_constraints.get(name)):
            if candidate is None:
                continue
            if not isinstance(candidate, Mapping):
                raise ContextForkError("tool_constraint", f"Tool constraint for {name} is invalid")
            Draft202012Validator.check_schema(dict(candidate))
            _reject_external_refs(candidate)
            schemas.append(candidate)
        if schemas:
            constraints[name] = schemas[0] if len(schemas) == 1 else {"allOf": schemas}
    return EffectiveToolScope(allowed_versions, constraints, registry_hash)


def _narrow_permission(*modes: PermissionMode) -> PermissionMode:
    if PermissionMode.PLAN in modes:
        return PermissionMode.PLAN
    if PermissionMode.READ_ONLY in modes:
        return PermissionMode.READ_ONLY
    if PermissionMode.BYPASS in modes:
        # Bypass is never inherited into a child.  The remaining scopes still
        # undergo exact capability intersection.
        modes = tuple(PermissionMode.NORMAL if item is PermissionMode.BYPASS else item for item in modes)
    if all(item is PermissionMode.TRUSTED_WORKSPACE for item in modes):
        return PermissionMode.TRUSTED_WORKSPACE
    return PermissionMode.NORMAL


def _sanitize(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    if depth > 32:
        raise ContextForkError("context_depth", "parent context nesting exceeds limit")
    if key is not None and any(fragment in key.casefold().replace("-", "_") for fragment in _FORBIDDEN_KEYS):
        return None
    if isinstance(value, Mapping):
        return {
            str(item_key): cleaned
            for item_key, item in value.items()
            if (cleaned := _sanitize(item, key=str(item_key), depth=depth + 1)) is not None
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth=depth + 1) for item in value[:10_000]]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return None


def _select_by_id(value: Any, identifiers: tuple[str, ...], label: str) -> list[Any]:
    if not identifiers:
        return []
    if not isinstance(value, list):
        raise ContextForkError("context_selection", f"parent {label} collection is invalid")
    wanted = set(identifiers)
    selected = [item for item in value if isinstance(item, Mapping) and item.get("id") in wanted]
    found = {str(item["id"]) for item in selected}
    if found != wanted:
        raise ContextForkError("context_selection", f"selected {label} is missing or unauthorized")
    return selected


def _select_artifacts(value: Any, identifiers: tuple[str, ...]) -> list[Any]:
    selected = _select_by_id(value, identifiers, "artifact")
    if any(item.get("authorized") is not True for item in selected if isinstance(item, Mapping)):
        raise ContextForkError("artifact_scope", "selected Artifact is not authorized for the child")
    return selected


def _bounded_array(value: Any, limit: int) -> list[Any]:
    return value[:limit] if isinstance(value, list) else []


def _reject_external_refs(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                raise ContextForkError("tool_constraint", "external Tool constraint reference is forbidden")
            _reject_external_refs(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_external_refs(item)


__all__ = ["ContextForkError", "ContextForker", "DerivedAuthority", "ScopeDeriver"]
