"""Trusted Shell command profiles.

Profiles are installed/configured by trusted runtime composition.  The model
only sees one ToolDefinition per profile and must provide the complete argv;
there is no model-controlled executable or raw command string.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from offeragent_harness.permissions import RiskClass
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffectClass,
    ToolDefinition,
    canonical_json_sha256,
)

_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_ARGUMENT_PATTERN = r"^[^\x00-\x1f\x7f&|<>^;`]{0,4096}$"
_FORBIDDEN_SWITCH_PATTERN = (
    r"^(?:-|/)(?:[cC]|[cC][oO][mM][mM][aA][nN][dD]|"
    r"[eE][nN][cC]|[eE][nN][cC][oO][dD][eE][dD][cC][oO][mM][mM][aA][nN][dD])$"
)
_FORBIDDEN_SWITCH = re.compile(_FORBIDDEN_SWITCH_PATTERN)
_RELATIVE_CWD_PATTERN = (
    r"^(?!\.{1,2}(?:/|$))(?!.*\/\.{1,2}(?:/|$))"
    r"(?:$|[^\x00-\x1f<>:\"|?*\\/]+(?:/[^\x00-\x1f<>:\"|?*\\/]+)*)$"
)
_SENSITIVE_ENVIRONMENT_FRAGMENTS = (
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "KEY",
    "PASSWORD",
    "PROXY",
    "SECRET",
    "TOKEN",
)
_DANGEROUS_ENVIRONMENT_NAMES = frozenset(
    {
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "PROMPT",
        "PSMODULEPATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "NODE_OPTIONS",
    }
)


@dataclass(frozen=True, slots=True)
class ShellCommandProfile:
    profile_id: str
    description: str
    executable_id: str
    executable_profile_fingerprint: str
    fixed_arguments: tuple[str, ...]
    risk: RiskClass
    side_effect_class: SideEffectClass
    cwd_root_id: str = "vault"
    environment_profile_id: str = "minimal"
    environment_allowlist: frozenset[str] = frozenset()
    environment: Mapping[str, str] = field(default_factory=dict)
    minimum_variable_arguments: int = 0
    maximum_variable_arguments: int = 16
    variable_argument_pattern: str = _ARGUMENT_PATTERN
    timeout_ms: int = 30_000
    inline_output_limit_bytes: int = 64 * 1024
    artifact_output_limit_bytes: int = 16 * 1024 * 1024
    allow_network: bool = False
    concurrency_safe: bool = False
    idempotent: bool = False
    retryable: bool = False
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        if not _PROFILE_ID.fullmatch(self.profile_id):
            raise ValueError("invalid Shell profile ID")
        if (
            not self.description
            or len(self.description) > 512
            or any(ord(character) < 32 for character in self.description)
            or not _PROFILE_ID.fullmatch(self.executable_id)
        ):
            raise ValueError("Shell profile description and executable ID are required")
        if not _SHA256.fullmatch(self.executable_profile_fingerprint):
            raise ValueError("Shell profile requires the captured executable profile fingerprint")
        if not _PROFILE_ID.fullmatch(self.cwd_root_id) or not _PROFILE_ID.fullmatch(self.environment_profile_id):
            raise ValueError("invalid Shell capability profile ID")
        if not 0 <= self.minimum_variable_arguments <= self.maximum_variable_arguments <= 128:
            raise ValueError("invalid Shell argument count limits")
        try:
            re.compile(self.variable_argument_pattern)
        except re.error as error:
            raise ValueError("invalid Shell argument pattern") from error
        if self.timeout_ms < 1 or self.inline_output_limit_bytes < 1:
            raise ValueError("Shell timeout and output limit must be positive")
        if self.artifact_output_limit_bytes < self.inline_output_limit_bytes:
            raise ValueError("Shell Artifact output limit must cover inline output")
        if self.retryable and not self.idempotent:
            raise ValueError("retryable Shell profiles must be idempotent")
        if any(
            re.fullmatch(_ARGUMENT_PATTERN, argument) is None or _FORBIDDEN_SWITCH.fullmatch(argument) is not None
            for argument in self.fixed_arguments
        ):
            raise ValueError("fixed Shell argv contains control or command-parser metacharacters")
        if self.concurrency_safe and self.side_effect_class not in {
            SideEffectClass.NONE,
            SideEffectClass.READ,
        }:
            raise ValueError("only read-only Shell profiles can be concurrent")
        environment = dict(self.environment)
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items()):
            raise TypeError("Shell profile environment must be text")
        allowlist = frozenset(name.upper() for name in self.environment_allowlist)
        if any(not name or "=" in name or "\x00" in name or _environment_name_is_sensitive(name) for name in allowlist):
            raise ValueError("Shell environment allowlist contains a secret or dangerous variable")
        if any(
            not key
            or key.upper() not in allowlist
            or _environment_name_is_sensitive(key)
            or "\x00" in value
            or len(value) > 32 * 1024
            for key, value in environment.items()
        ):
            raise ValueError("Shell fixed environment exceeds its non-secret allowlist")
        if not _VERSION.fullmatch(self.version):
            raise ValueError("invalid Shell profile version")
        object.__setattr__(self, "fixed_arguments", tuple(self.fixed_arguments))
        object.__setattr__(self, "environment_allowlist", allowlist)
        object.__setattr__(self, "environment", MappingProxyType(environment))

    @property
    def tool_name(self) -> str:
        return f"shell.{self.profile_id}"

    @property
    def definition(self) -> ToolDefinition:
        binding_digest = canonical_json_sha256(
            {
                "executableProfileFingerprint": self.executable_profile_fingerprint,
                "environment": {key: self.environment[key] for key in sorted(self.environment)},
            }
        )
        binding_capability = "shell.binding." + binding_digest.removeprefix("sha256:")
        return ToolDefinition(
            name=self.tool_name,
            version=self.version,
            description=self.description,
            input_schema=self.input_schema,
            output_schema=_OUTPUT_SCHEMA,
            executor_location=ExecutorLocation.LOCAL,
            risk=self.risk,
            side_effect_class=self.side_effect_class,
            required_capabilities=frozenset(
                {
                    "process.execute",
                    f"process.executable.{self.executable_id}",
                    f"shell.profile.{self.profile_id}",
                    binding_capability,
                }
            ),
            concurrency_safe=self.concurrency_safe,
            idempotent=self.idempotent,
            retryable=self.retryable,
            timeout_ms=self.timeout_ms,
            output_limit_bytes=self.inline_output_limit_bytes,
            preflight_mode=PreflightMode.NONE,
            preflight_provider=None,
            approval_evidence=ApprovalEvidence.NONE,
            result_sensitivity=ResultSensitivity.PRIVATE,
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        fixed_count = len(self.fixed_arguments)
        schema: dict[str, Any] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["argv", "cwd", "environment", "timeoutMs"],
            "properties": {
                "argv": {
                    "type": "array",
                    "prefixItems": [{"const": argument} for argument in self.fixed_arguments],
                    "items": {
                        "type": "string",
                        "maxLength": 4096,
                        "allOf": [
                            {"pattern": _ARGUMENT_PATTERN},
                            {"not": {"pattern": _FORBIDDEN_SWITCH_PATTERN}},
                            {"pattern": self.variable_argument_pattern},
                        ],
                    },
                    "minItems": fixed_count + self.minimum_variable_arguments,
                    "maxItems": fixed_count + self.maximum_variable_arguments,
                },
                "cwd": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["rootId", "path"],
                    "properties": {
                        "rootId": {"const": self.cwd_root_id},
                        "path": {"type": "string", "pattern": _RELATIVE_CWD_PATTERN, "maxLength": 1024},
                    },
                },
                "environment": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["profileId"],
                    "properties": {"profileId": {"const": self.environment_profile_id}},
                },
                "timeoutMs": {"type": "integer", "minimum": 1, "maximum": self.timeout_ms},
            },
        }
        Draft202012Validator.check_schema(schema)
        return schema


_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "exitCode",
        "lifecycleState",
        "stdout",
        "stderr",
        "stdoutBytes",
        "stderrBytes",
        "outputTruncated",
    ],
    "properties": {
        "exitCode": {"type": "integer"},
        "lifecycleState": {"type": "string"},
        "stdout": {"type": "object"},
        "stderr": {"type": "object"},
        "stdoutBytes": {"type": "integer", "minimum": 0},
        "stderrBytes": {"type": "integer", "minimum": 0},
        "outputTruncated": {"type": "boolean"},
    },
}


def _environment_name_is_sensitive(name: str) -> bool:
    folded = name.upper()
    return folded in _DANGEROUS_ENVIRONMENT_NAMES or any(
        fragment in folded for fragment in _SENSITIVE_ENVIRONMENT_FRAGMENTS
    )


__all__ = ["ShellCommandProfile"]
