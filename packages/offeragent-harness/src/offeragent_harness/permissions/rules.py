"""Immutable exact-match policy rules with Draft 2020-12 argument constraints."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from offeragent_harness.models import FrozenJsonObject, freeze_json, thaw_json
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools.definitions import ExecutorLocation, SideEffectClass, ToolCall, ToolDefinition

from .policy import PolicyContext
from .risk import PermissionMode, RiskClass


class RuleEffect(str, Enum):
    DENY = "deny"
    ALLOW = "allow"
    ASK = "ask"


def _reject_external_refs(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}/{key}"
            if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                raise ValueError(f"external policy schema reference is forbidden at {child}")
            _reject_external_refs(item, child)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_external_refs(item, f"{path}/{index}")


@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    effect: RuleEffect
    tool_names: frozenset[str] = frozenset()
    tool_versions: frozenset[str] = frozenset()
    risks: frozenset[RiskClass] = frozenset()
    side_effects: frozenset[SideEffectClass] = frozenset()
    executor_locations: frozenset[ExecutorLocation] = frozenset()
    permission_modes: frozenset[PermissionMode] = frozenset()
    agent_names: frozenset[str] = frozenset()
    workspace_trusted: bool | None = None
    argument_schema: Mapping[str, Any] | None = None
    reason: str = "configured policy rule"
    audit_tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.rule_id or not self.reason:
            raise ValueError("policy rule ID and reason must not be empty")
        for attribute in (
            "tool_names",
            "tool_versions",
            "risks",
            "side_effects",
            "executor_locations",
            "permission_modes",
            "agent_names",
            "audit_tags",
        ):
            object.__setattr__(self, attribute, frozenset(getattr(self, attribute)))
        if self.argument_schema is not None:
            Draft202012Validator.check_schema(dict(self.argument_schema))
            _reject_external_refs(self.argument_schema)
            frozen = freeze_json(self.argument_schema)
            if not isinstance(frozen, FrozenJsonObject):
                raise TypeError("policy argument_schema must be a JSON object")
            object.__setattr__(self, "argument_schema", frozen)

    def matches(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
        lineage: AgentLineage,
    ) -> bool:
        checks = (
            not self.tool_names or definition.name in self.tool_names,
            not self.tool_versions or definition.version in self.tool_versions,
            not self.risks or definition.risk in self.risks,
            not self.side_effects or definition.side_effect_class in self.side_effects,
            not self.executor_locations or definition.executor_location in self.executor_locations,
            not self.permission_modes or context.permission_mode in self.permission_modes,
            not self.agent_names or lineage.agent_name in self.agent_names,
            self.workspace_trusted is None or self.workspace_trusted is context.workspace_trusted,
        )
        if not all(checks):
            return False
        if self.argument_schema is None:
            return True
        return bool(
            Draft202012Validator(
                thaw_json(self.argument_schema),
                format_checker=FormatChecker(),
            ).is_valid(thaw_json(call.arguments))
        )


@dataclass(frozen=True)
class PolicyRuleSet:
    rules: tuple[PolicyRule, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        values = tuple(self.rules)
        identifiers = [rule.rule_id for rule in values]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("policy rule IDs must be unique")
        object.__setattr__(self, "rules", tuple(sorted(values, key=lambda rule: rule.rule_id)))

    @classmethod
    def from_iterable(cls, rules: Iterable[PolicyRule]) -> PolicyRuleSet:
        return cls(tuple(rules))

    def matching(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
    ) -> tuple[PolicyRule, ...]:
        return tuple(rule for rule in self.rules if rule.matches(definition, call, context, call.lineage))


__all__ = ["PolicyRule", "PolicyRuleSet", "RuleEffect"]
