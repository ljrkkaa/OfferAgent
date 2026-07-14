"""Strict layered Agent Definition catalog with hash-bound trust."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from offeragent_harness.permissions import CapabilityScope, PermissionMode, RiskClass
from offeragent_harness.tools.canonical import canonical_json_sha256

from .definitions import AgentDefinition, ModelPolicy, _structured_result_schema

_MAX_DEFINITION_BYTES = 256 * 1024
_MAX_DEFINITIONS = 512


class AgentDefinitionLayer(str, Enum):
    BUILTIN = "builtin"
    USER = "user"
    WORKSPACE = "workspace"

    @property
    def priority(self) -> int:
        return {self.BUILTIN: 100, self.USER: 200, self.WORKSPACE: 300}[self]


class AgentDefinitionTrust(str, Enum):
    VERIFIED = "verified"
    CONFIRMED = "confirmed"
    CONFIRMATION_REQUIRED = "confirmation_required"
    WORKSPACE_UNTRUSTED = "workspace_untrusted"

    @property
    def enabled(self) -> bool:
        return self in {self.VERIFIED, self.CONFIRMED}


class AgentDefinitionError(RuntimeError):
    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        self.code = code
        self.path = path
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AgentDefinitionRoot:
    root_id: str
    layer: AgentDefinitionLayer
    path: Path | None
    workspace_trusted: bool

    def __post_init__(self) -> None:
        if not self.root_id or (self.layer is AgentDefinitionLayer.BUILTIN) != (self.path is None):
            raise ValueError("Agent Definition root identity/layer is invalid")
        if self.layer is not AgentDefinitionLayer.WORKSPACE and not self.workspace_trusted:
            raise ValueError("non-Workspace Agent Definition roots must be trusted")


@dataclass(frozen=True, slots=True)
class AgentDefinitionDescriptor:
    definition: AgentDefinition
    root_id: str
    layer: AgentDefinitionLayer
    relative_path: str | None
    content_hash: str
    trust: AgentDefinitionTrust


class AgentDefinitionCatalog:
    def __init__(
        self,
        *,
        workspace_id: str,
        builtins: Sequence[AgentDefinition],
        roots: Sequence[AgentDefinitionRoot] = (),
        system_denied_tools: frozenset[str] = frozenset(),
    ) -> None:
        if not workspace_id:
            raise ValueError("Agent Definition catalog requires a Workspace")
        if len(roots) > 16 or len({item.root_id for item in roots}) != len(roots):
            raise ValueError("Agent Definition roots are invalid/duplicated")
        self.workspace_id = workspace_id
        self._builtins = tuple(builtins)
        self._roots = tuple(roots)
        self._system_denied = system_denied_tools
        self._by_name: Mapping[str, AgentDefinitionDescriptor] = MappingProxyType({})
        self._descriptors: tuple[AgentDefinitionDescriptor, ...] = ()
        self._revision = 0
        self._snapshot_hash = canonical_json_sha256({"workspaceId": workspace_id, "agents": []})

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def snapshot_hash(self) -> str:
        return self._snapshot_hash

    @property
    def descriptors(self) -> tuple[AgentDefinitionDescriptor, ...]:
        return self._descriptors

    def rescan(self, *, expected_revision: int) -> None:
        if expected_revision != self._revision:
            raise AgentDefinitionError("catalog_revision_conflict", "Agent Definition catalog revision changed")
        descriptors: list[AgentDefinitionDescriptor] = []
        for definition in self._builtins:
            content_hash = canonical_json_sha256(_definition_identity(definition))
            descriptors.append(
                AgentDefinitionDescriptor(
                    definition,
                    "builtin",
                    AgentDefinitionLayer.BUILTIN,
                    None,
                    content_hash,
                    AgentDefinitionTrust.VERIFIED,
                )
            )
        for root in self._roots:
            if root.path is None:
                continue
            descriptors.extend(self._scan_root(root))
        if len(descriptors) > _MAX_DEFINITIONS:
            raise AgentDefinitionError("definition_limit", "Agent Definition count exceeds limit")
        effective: dict[str, AgentDefinitionDescriptor] = {}
        seen_origin: set[tuple[AgentDefinitionLayer, str]] = set()
        for descriptor in sorted(descriptors, key=lambda item: (item.layer.priority, item.definition.name)):
            origin = (descriptor.layer, descriptor.definition.name)
            if origin in seen_origin:
                raise AgentDefinitionError(
                    "definition_collision",
                    f"duplicate Agent Definition {descriptor.definition.name!r} in one layer",
                    path=descriptor.relative_path,
                )
            seen_origin.add(origin)
            current = effective.get(descriptor.definition.name)
            if descriptor.trust.enabled and (current is None or descriptor.layer.priority > current.layer.priority):
                effective[descriptor.definition.name] = descriptor
        self._revision += 1
        self._descriptors = tuple(
            sorted(descriptors, key=lambda item: (item.definition.name, -item.layer.priority, item.relative_path or ""))
        )
        self._by_name = MappingProxyType(dict(sorted(effective.items())))
        self._snapshot_hash = canonical_json_sha256(
            {
                "workspaceId": self.workspace_id,
                "revision": self._revision,
                "definitions": [
                    {
                        "name": item.definition.name,
                        "version": item.definition.version,
                        "layer": item.layer.value,
                        "hash": item.content_hash,
                        "trust": item.trust.value,
                    }
                    for item in self._descriptors
                ],
            }
        )

    def resolve(self, name: str) -> AgentDefinitionDescriptor:
        descriptor = self._by_name.get(name)
        if descriptor is None:
            discovered = [item for item in self._descriptors if item.definition.name == name]
            if discovered:
                raise AgentDefinitionError("definition_untrusted", f"Agent Definition {name!r} is not trusted")
            raise AgentDefinitionError("definition_not_found", f"Agent Definition {name!r} is unavailable")
        if descriptor.relative_path is not None:
            root = next(item for item in self._roots if item.root_id == descriptor.root_id)
            assert root.path is not None
            raw = _secure_read(root.path, descriptor.relative_path)
            digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
            if digest != descriptor.content_hash:
                raise AgentDefinitionError(
                    "definition_hash_drift",
                    "Agent Definition changed after catalog snapshot",
                    path=descriptor.relative_path,
                )
        return descriptor

    def _scan_root(self, root: AgentDefinitionRoot) -> list[AgentDefinitionDescriptor]:
        assert root.path is not None
        if not root.path.exists():
            return []
        base = root.path.resolve(strict=True)
        if base.is_symlink() or not base.is_dir():
            raise AgentDefinitionError("definition_root", "Agent Definition root is invalid")
        paths = sorted(
            (item for item in base.rglob("*.md") if item.is_file()), key=lambda item: item.as_posix().casefold()
        )
        if len(paths) > _MAX_DEFINITIONS:
            raise AgentDefinitionError("definition_limit", "Agent Definition root exceeds file limit")
        if len({item.name.casefold() for item in paths}) != len(paths):
            raise AgentDefinitionError("definition_collision", "Agent Definition filenames collide on Windows")
        output: list[AgentDefinitionDescriptor] = []
        for path in paths:
            relative = path.relative_to(base).as_posix()
            raw = _secure_read(base, relative)
            content_hash = f"sha256:{hashlib.sha256(raw).hexdigest()}"
            trust = AgentDefinitionTrust.CONFIRMATION_REQUIRED
            if root.layer is AgentDefinitionLayer.WORKSPACE and not root.workspace_trusted:
                trust = AgentDefinitionTrust.WORKSPACE_UNTRUSTED
            value, instructions = _parse_document(raw)
            definition = _definition_from_mapping(
                value,
                instructions=instructions,
                version=content_hash.removeprefix("sha256:"),
                system_denied=self._system_denied,
            )
            if root.layer is AgentDefinitionLayer.USER:
                trust = AgentDefinitionTrust.CONFIRMED
            elif root.layer is AgentDefinitionLayer.WORKSPACE and root.workspace_trusted:
                trust = AgentDefinitionTrust.CONFIRMED
            output.append(
                AgentDefinitionDescriptor(definition, root.root_id, root.layer, relative, content_hash, trust)
            )
        return output


def _secure_read(root: Path, relative_path: str) -> bytes:
    if not relative_path or "\\" in relative_path or relative_path.startswith("/") or ".." in relative_path.split("/"):
        raise AgentDefinitionError("definition_path", "Agent Definition relative path is unsafe", path=relative_path)
    candidate = root.joinpath(*relative_path.split("/"))
    current = root
    for part in relative_path.split("/"):
        current = current / part
        try:
            info = os.stat(current, follow_symlinks=False)
        except OSError as error:
            raise AgentDefinitionError(
                "definition_path", "Agent Definition path is unavailable", path=relative_path
            ) from error
        if os.path.islink(current) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise AgentDefinitionError(
                "definition_reparse", "Agent Definition path is a symlink/junction", path=relative_path
            )
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise AgentDefinitionError(
            "definition_escape", "Agent Definition escaped its root", path=relative_path
        ) from error
    if candidate.stat().st_size > _MAX_DEFINITION_BYTES:
        raise AgentDefinitionError("definition_size", "Agent Definition exceeds size limit", path=relative_path)
    try:
        raw = candidate.read_bytes()
    except OSError as error:
        raise AgentDefinitionError("definition_read", "Agent Definition cannot be read", path=relative_path) from error
    if b"\x00" in raw:
        raise AgentDefinitionError("definition_encoding", "Agent Definition contains NUL", path=relative_path)
    return raw


def _parse_document(raw: bytes) -> tuple[Mapping[str, Any], str]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AgentDefinitionError("definition_encoding", "Agent Definition must be UTF-8") from error
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise AgentDefinitionError("definition_parse", "Agent Definition must start with YAML frontmatter")
    closing = next((index for index, line in enumerate(lines[1:], start=1) if line.rstrip("\r\n") == "---"), None)
    if closing is None:
        raise AgentDefinitionError("definition_parse", "Agent Definition frontmatter is not closed")
    try:
        value = _simple_yaml("".join(lines[1:closing]))
    except (ValueError, SyntaxError) as error:
        raise AgentDefinitionError("definition_parse", "Agent Definition syntax is invalid") from error
    if not isinstance(value, Mapping):
        raise AgentDefinitionError("definition_parse", "Agent Definition root must be an object")
    return value, "".join(lines[closing + 1 :])


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate key: {key}")
        output[key] = value
    return output


def _simple_yaml(text: str) -> Mapping[str, Any]:
    """Parse the intentionally small, tag/anchor-free Agent YAML subset."""

    if any(token in text for token in ("!!", "&", "*", "<<:")):
        raise ValueError("YAML tags, anchors, aliases and merges are forbidden")
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if "\t" in raw_line:
            raise ValueError("tabs are forbidden in Agent YAML")
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent % 2:
            raise ValueError("Agent YAML indentation must use two spaces")
        line = raw_line.strip()
        if ":" not in line or line.startswith("-"):
            raise ValueError("Agent YAML only supports mappings and inline arrays")
        key, raw_value = (part.strip() for part in line.split(":", maxsplit=1))
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
            raise ValueError("Agent YAML key is invalid")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError("Agent YAML indentation is invalid")
        parent = stack[-1][1]
        if key in parent:
            raise ValueError(f"duplicate Agent YAML key: {key}")
        if not raw_value:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _yaml_scalar(raw_value)
    return root


def _yaml_scalar(value: str) -> Any:
    if value in {"null", "~"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith("[") and value.endswith("]"):
        body = value[1:-1].strip()
        return [] if not body else [_yaml_scalar(item.strip()) for item in body.split(",")]
    if value.startswith("{") and value.endswith("}"):
        return json.loads(value, object_pairs_hook=_reject_duplicate_pairs)
    if value[:1] in {'"', "'"}:
        parsed = ast.literal_eval(value)
        if not isinstance(parsed, str):
            raise ValueError("quoted YAML scalar must be a string")
        return parsed
    if re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    return value


_FIELDS = frozenset(
    {
        "name",
        "description",
        "model",
        "tools",
        "disallowedTools",
        "skills",
        "permissionMode",
    }
)


def _definition_from_mapping(
    value: Mapping[str, Any],
    *,
    instructions: str,
    version: str,
    system_denied: frozenset[str],
) -> AgentDefinition:
    unknown = set(value) - _FIELDS
    if unknown:
        raise AgentDefinitionError("definition_unknown_field", f"unknown Agent Definition fields: {sorted(unknown)!r}")
    required = {"name", "description"}
    if set(value) < required:
        raise AgentDefinitionError(
            "definition_missing_field", f"missing Agent Definition fields: {sorted(required - set(value))!r}"
        )
    allow = frozenset(_string_list(value.get("tools", []), "tools")) - system_denied
    deny = frozenset(_string_list(value.get("disallowedTools", []), "disallowedTools")) | system_denied
    mode = _optional_string(value.get("permissionMode"), "permissionMode")
    permission = {
        None: PermissionMode.READ_ONLY,
        "default": PermissionMode.NORMAL,
        "plan": PermissionMode.PLAN,
        "read-only": PermissionMode.READ_ONLY,
    }.get(mode)
    if permission is None:
        raise AgentDefinitionError("definition_permission", "Agent permissionMode is unsupported")
    model = _optional_string(value.get("model"), "model")
    model_policy = ModelPolicy.INHERIT if model is None or model == "inherit" else ModelPolicy.FIXED
    if model == "inherit":
        model = None
    return AgentDefinition(
        name=_string(value["name"], "name"),
        version=version,
        description=_string(value["description"], "description"),
        model_policy=model_policy,
        model=model,
        reasoning_effort=None,
        tool_allow=allow,
        tool_deny=deny,
        skills=tuple(_string_list(value.get("skills", []), "skills")),
        permission_ceiling=permission,
        capability_ceiling=CapabilityScope(
            allow,
            deny,
            frozenset({RiskClass.READ}),
            frozenset(),
            False,
            False,
        ),
        can_spawn_children=False,
        max_depth=0,
        max_tool_calls=0,
        result_schema=_structured_result_schema(),
        metadata={"capabilityCeilingExplicit": True, "source": "claude-agent-markdown"},
        instructions=instructions,
    )


def _risks_for_permission(permission: PermissionMode, network: bool) -> frozenset[RiskClass]:
    if permission in {PermissionMode.READ_ONLY, PermissionMode.PLAN}:
        risks = {RiskClass.READ}
    else:
        risks = set(RiskClass) - {RiskClass.SECRET_ACCESS}
    if not network:
        risks.discard(RiskClass.NETWORK)
    return frozenset(risks)


def _definition_identity(value: AgentDefinition) -> dict[str, Any]:
    return {
        "name": value.name,
        "version": value.version,
        "description": value.description,
        "modelPolicy": value.model_policy.value,
        "model": value.model,
        "reasoningEffort": value.reasoning_effort,
        "tools": {"allow": sorted(value.tool_allow), "deny": sorted(value.tool_deny)},
        "skills": list(value.skills),
        "permissionCeiling": value.permission_ceiling.value,
        "capabilityCeiling": {
            "allowedTools": sorted(value.capability_ceiling.allowed_tools),
            "deniedTools": sorted(value.capability_ceiling.denied_tools),
            "allowedRisks": sorted(item.value for item in value.capability_ceiling.allowed_risks),
            "rootCapabilities": sorted(value.capability_ceiling.root_capabilities),
            "network": value.capability_ceiling.allow_network,
            "secretHandles": value.capability_ceiling.allow_secret_handles,
        },
        "canSpawnChildren": value.can_spawn_children,
        "maxDepth": value.max_depth,
        "maxToolCalls": value.max_tool_calls,
        "resultSchema": value.result_schema,
        "metadata": value.metadata,
        "instructions": value.instructions,
    }


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AgentDefinitionError("definition_type", f"{name} must be an object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AgentDefinitionError("definition_type", f"{name} must be a non-empty string")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise AgentDefinitionError("definition_type", f"{name} must be a string array")
    if len(value) != len(set(value)) or len(value) > 512:
        raise AgentDefinitionError("definition_limit", f"{name} must contain unique bounded values")
    return [str(item) for item in value]


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise AgentDefinitionError("definition_type", f"{name} must be a boolean")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentDefinitionError("definition_type", f"{name} must be an integer")
    return int(value)


__all__ = [
    "AgentDefinitionCatalog",
    "AgentDefinitionDescriptor",
    "AgentDefinitionError",
    "AgentDefinitionLayer",
    "AgentDefinitionRoot",
    "AgentDefinitionTrust",
]
