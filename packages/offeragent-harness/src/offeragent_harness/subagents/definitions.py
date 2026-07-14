"""Versioned Harness-owned subagent definitions."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jsonschema import Draft202012Validator

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json
from offeragent_harness.permissions import CapabilityScope, PermissionMode, RiskClass

_AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*$")
_REASONING = frozenset({"low", "medium", "high"})
_RESULT_FIELDS = frozenset({"summary", "findings", "evidence", "proposedActions", "unresolvedQuestions"})


class ModelPolicy(str, Enum):
    INHERIT = "inherit"
    FIXED = "fixed"
    LOCAL_ONLY = "local_only"


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    version: str
    description: str
    model_policy: ModelPolicy
    model: str | None
    reasoning_effort: str | None
    tool_allow: frozenset[str]
    tool_deny: frozenset[str]
    skills: tuple[str, ...]
    permission_ceiling: PermissionMode
    capability_ceiling: CapabilityScope
    can_spawn_children: bool
    max_depth: int
    max_tool_calls: int
    result_schema: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    instructions: str = ""

    def __post_init__(self) -> None:
        if not _AGENT_NAME.fullmatch(self.name):
            raise ValueError("invalid agent definition name")
        if not self.version or not self.description:
            raise ValueError("agent version and description must not be empty")
        if "\x00" in self.instructions or len(self.instructions) > 1_000_000:
            raise ValueError("agent instructions are invalid")
        if self.model_policy is ModelPolicy.FIXED and not self.model:
            raise ValueError("fixed model policy requires a model")
        if self.model_policy is not ModelPolicy.FIXED and self.model is not None:
            raise ValueError("only fixed model policy accepts a model")
        if self.tool_allow & self.tool_deny:
            raise ValueError("the same tool cannot be both allowed and denied")
        if any(_TOOL_NAME.fullmatch(item) is None for item in (*self.tool_allow, *self.tool_deny)):
            raise ValueError("agent Tool allow/deny contains an invalid canonical name")
        if self.capability_ceiling.allowed_tools - self.tool_allow:
            raise ValueError("agent capability ceiling cannot allow Tools outside tool_allow")
        if self.permission_ceiling is PermissionMode.BYPASS:
            raise ValueError("agent definitions cannot grant bypass permission")
        if self.reasoning_effort is not None and self.reasoning_effort not in _REASONING:
            raise ValueError("agent reasoning_effort is invalid")
        if not 0 <= self.max_depth <= 3 or not 0 <= self.max_tool_calls <= 10_000:
            raise ValueError("agent limits exceed the hard Subagent ceiling")
        if len(self.skills) > 256 or len(self.skills) != len(set(self.skills)) or any(not item for item in self.skills):
            raise ValueError("agent skills must be unique non-empty names")
        Draft202012Validator.check_schema(dict(self.result_schema))
        _reject_external_references(self.result_schema)
        if self.result_schema.get("type") != "object":
            raise ValueError("agent result_schema root must be an object")
        if not (
            self.result_schema.get("additionalProperties") is False
            or self.result_schema.get("unevaluatedProperties") is False
        ):
            raise ValueError("agent result_schema must be closed")
        properties = self.result_schema.get("properties")
        required = self.result_schema.get("required", ())
        if (
            not isinstance(properties, Mapping)
            or set(properties) != _RESULT_FIELDS
            or not isinstance(required, (list, tuple))
            or "summary" not in required
            or not set(required) <= _RESULT_FIELDS
        ):
            raise ValueError("agent result_schema must use the fixed SubagentResult envelope")
        schema = freeze_json(self.result_schema)
        metadata = freeze_json(self.metadata)
        if not isinstance(schema, FrozenJsonObject) or not isinstance(metadata, FrozenJsonObject):
            raise TypeError("result_schema and metadata must be JSON objects")
        object.__setattr__(self, "result_schema", schema)
        object.__setattr__(self, "metadata", metadata)


def _reject_external_references(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}/{key}"
            if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                raise ValueError(f"external Agent result-schema reference is forbidden at {child}")
            _reject_external_references(item, child)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_external_references(item, f"{path}/{index}")


def builtin_agent_definitions(
    *,
    available_tools: frozenset[str],
    root_capabilities: frozenset[str],
) -> tuple[AgentDefinition, ...]:
    """Create the signed Runtime profiles against the composed capability set."""

    profiles = (
        (
            "researcher",
            "Collect bounded evidence from local workspace files.",
            frozenset(
                {
                    "glob",
                    "grep",
                    "read",
                }
            ),
            frozenset({RiskClass.READ}),
            False,
        ),
        (
            "reviewer",
            "Review implementation read-only, find counterexamples and cite evidence.",
            frozenset({"glob", "grep", "read", "shell.powershell"}),
            frozenset({RiskClass.READ}),
            False,
        ),
        (
            "test-runner",
            "Run only the preconfigured test command profile and report structured results.",
            frozenset({"read", "shell.powershell"}),
            frozenset({RiskClass.READ, RiskClass.EXECUTE}),
            False,
        ),
        (
            "workspace-editor",
            "Read Workspace content and return ProposedAction/Patch Artifacts without applying writes.",
            frozenset({"glob", "grep", "read"}),
            frozenset({RiskClass.READ}),
            False,
        ),
        (
            "general",
            "Perform a bounded child task with an authority intersection that never exceeds the parent.",
            available_tools,
            frozenset({RiskClass.READ}),
            False,
        ),
    )
    output: list[AgentDefinition] = []
    for name, description, requested_tools, risks, network in profiles:
        tools = (requested_tools & available_tools) - {"vault.transaction"}
        definition_revision = hashlib.sha256(
            "\x1f".join(
                (
                    name,
                    description,
                    *sorted(tools),
                    *sorted(risk.value for risk in risks),
                    str(network),
                )
            ).encode("utf-8")
        ).hexdigest()
        output.append(
            AgentDefinition(
                name=name,
                version=definition_revision,
                description=description,
                model_policy=ModelPolicy.INHERIT,
                model=None,
                reasoning_effort="high" if name == "reviewer" else "medium",
                tool_allow=tools,
                tool_deny=frozenset({"vault.transaction"}),
                skills=(),
                permission_ceiling=PermissionMode.NORMAL if risks - {RiskClass.READ} else PermissionMode.READ_ONLY,
                capability_ceiling=CapabilityScope(
                    tools,
                    frozenset({"vault.transaction"}),
                    risks,
                    root_capabilities,
                    network,
                    False,
                ),
                can_spawn_children=False,
                max_depth=1,
                max_tool_calls=20,
                result_schema=_structured_result_schema(),
                metadata={"builtin": True, "capabilityCeilingExplicit": True},
                instructions=description,
            )
        )
    return tuple(output)


def _structured_result_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "summary": {"type": "string", "minLength": 1, "maxLength": 16_384},
            "findings": {"type": "array", "items": {"type": "object"}, "maxItems": 512},
            "evidence": {"type": "array", "items": {"type": "object"}, "maxItems": 512},
            "proposedActions": {"type": "array", "items": {"type": "object"}, "maxItems": 256},
            "unresolvedQuestions": {"type": "array", "items": {"type": "string"}, "maxItems": 256},
        },
        "required": ["summary", "findings", "evidence", "proposedActions", "unresolvedQuestions"],
        "additionalProperties": False,
    }


__all__ = ["AgentDefinition", "ModelPolicy", "builtin_agent_definitions"]
