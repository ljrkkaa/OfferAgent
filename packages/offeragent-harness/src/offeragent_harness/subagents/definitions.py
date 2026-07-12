"""Versioned Harness-owned subagent definitions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jsonschema import Draft202012Validator

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json
from offeragent_harness.permissions import CapabilityScope, PermissionMode

_AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


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

    def __post_init__(self) -> None:
        if not _AGENT_NAME.fullmatch(self.name):
            raise ValueError("invalid agent definition name")
        if not self.version or not self.description:
            raise ValueError("agent version and description must not be empty")
        if self.model_policy is ModelPolicy.FIXED and not self.model:
            raise ValueError("fixed model policy requires a model")
        if self.model_policy is not ModelPolicy.FIXED and self.model is not None:
            raise ValueError("only fixed model policy accepts a model")
        if self.tool_allow & self.tool_deny:
            raise ValueError("the same tool cannot be both allowed and denied")
        if self.max_depth < 0 or self.max_tool_calls < 0:
            raise ValueError("agent limits cannot be negative")
        Draft202012Validator.check_schema(dict(self.result_schema))
        schema = freeze_json(self.result_schema)
        metadata = freeze_json(self.metadata)
        if not isinstance(schema, FrozenJsonObject) or not isinstance(metadata, FrozenJsonObject):
            raise TypeError("result_schema and metadata must be JSON objects")
        object.__setattr__(self, "result_schema", schema)
        object.__setattr__(self, "metadata", metadata)


__all__ = ["AgentDefinition", "ModelPolicy"]
