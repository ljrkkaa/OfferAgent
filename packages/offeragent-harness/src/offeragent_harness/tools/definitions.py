"""Fail-closed tool definition and normalized call types."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from jsonschema import Draft202012Validator

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json
from offeragent_harness.permissions import RiskClass
from offeragent_harness.sessions import AgentLineage

from .canonical import canonical_json_sha256

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*$")


def _reject_external_references(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            child_path = f"{path}/{key}"
            if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                raise ToolDefinitionError(f"external schema reference is forbidden at {child_path}")
            _reject_external_references(item, child_path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_external_references(item, f"{path}/{index}")


class ExecutorLocation(str, Enum):
    LOCAL = "local"
    CLIENT = "client"
    MCP = "mcp"
    SUBAGENT = "subagent"


class SideEffectClass(str, Enum):
    NONE = "none"
    READ = "read"
    NETWORK = "network"
    WRITE = "write"
    EXECUTE = "execute"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


class ToolDefinitionError(ValueError):
    pass


@dataclass(frozen=True)
class ToolDefinition:
    """Immutable tool metadata; every execution-safety field is mandatory."""

    name: str
    version: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    executor_location: ExecutorLocation
    risk: RiskClass
    side_effect_class: SideEffectClass
    required_capabilities: frozenset[str]
    concurrency_safe: bool
    idempotent: bool
    retryable: bool
    timeout_ms: int
    output_limit_bytes: int

    def __post_init__(self) -> None:
        if not _TOOL_NAME.fullmatch(self.name):
            raise ToolDefinitionError(f"invalid tool name: {self.name!r}")
        if not self.version or not self.description:
            raise ToolDefinitionError("tool version and description must not be empty")
        if self.timeout_ms <= 0 or self.output_limit_bytes <= 0:
            raise ToolDefinitionError("timeout and output limit must be positive")
        if not self.required_capabilities or any(not capability for capability in self.required_capabilities):
            raise ToolDefinitionError("tools require at least one non-empty capability")
        if self.retryable and not self.idempotent:
            raise ToolDefinitionError("retryable tools must also be explicitly idempotent")
        if self.concurrency_safe and self.side_effect_class in {
            SideEffectClass.WRITE,
            SideEffectClass.EXECUTE,
            SideEffectClass.DESTRUCTIVE,
            SideEffectClass.UNKNOWN,
        }:
            raise ToolDefinitionError("effectful tools cannot declare concurrent execution safe")
        if self.side_effect_class is SideEffectClass.UNKNOWN:
            if self.concurrency_safe or self.idempotent or self.retryable:
                raise ToolDefinitionError(
                    "unknown side effects must fail closed: serial, non-idempotent and non-retryable"
                )
        try:
            Draft202012Validator.check_schema(dict(self.input_schema))
            Draft202012Validator.check_schema(dict(self.output_schema))
        except Exception as error:
            raise ToolDefinitionError(f"invalid Draft 2020-12 schema: {error}") from error
        _reject_external_references(self.input_schema)
        _reject_external_references(self.output_schema)
        input_schema = dict(self.input_schema)
        if input_schema.get("type") != "object":
            raise ToolDefinitionError("tool input_schema root type must be object")
        is_closed = (
            input_schema.get("additionalProperties") is False or input_schema.get("unevaluatedProperties") is False
        )
        if not is_closed:
            raise ToolDefinitionError(
                "tool input_schema must fail closed with additionalProperties=false or unevaluatedProperties=false"
            )
        frozen_input = freeze_json(self.input_schema)
        frozen_output = freeze_json(self.output_schema)
        if not isinstance(frozen_input, FrozenJsonObject) or not isinstance(frozen_output, FrozenJsonObject):
            raise ToolDefinitionError("tool schemas must be JSON objects")
        object.__setattr__(self, "input_schema", frozen_input)
        object.__setattr__(self, "output_schema", frozen_output)

    @property
    def timeout_seconds(self) -> float:
        return self.timeout_ms / 1000


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str
    run_id: str
    workspace_id: str
    name: str
    version: str
    arguments: Mapping[str, Any]
    args_hash: str
    idempotency_key: str
    deadline: datetime | None
    lineage: AgentLineage

    def __post_init__(self) -> None:
        required = (
            self.tool_call_id,
            self.run_id,
            self.workspace_id,
            self.name,
            self.version,
            self.args_hash,
            self.idempotency_key,
        )
        if any(not value for value in required):
            raise ValueError("tool call identity fields must not be empty")
        if self.run_id != self.lineage.run_id:
            raise ValueError("tool call run_id must match its agent lineage")
        if self.deadline is not None and (self.deadline.tzinfo is None or self.deadline.utcoffset() is None):
            raise ValueError("tool call deadline must be timezone-aware")
        frozen = freeze_json(self.arguments)
        if not isinstance(frozen, FrozenJsonObject):
            raise TypeError("tool arguments must be a JSON object")
        if canonical_json_sha256(frozen) != self.args_hash:
            raise ValueError("args_hash does not match canonical arguments")
        object.__setattr__(self, "arguments", frozen)

    @property
    def idempotency_fingerprint(self) -> str:
        return canonical_json_sha256(
            {
                "workspaceId": self.workspace_id,
                "rootRunId": self.lineage.root_run_id,
                "runId": self.run_id,
                "name": self.name,
                "version": self.version,
                "argsHash": self.args_hash,
            }
        )


__all__ = ["ExecutorLocation", "SideEffectClass", "ToolCall", "ToolDefinition", "ToolDefinitionError"]
