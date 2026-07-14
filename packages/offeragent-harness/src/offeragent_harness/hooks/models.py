"""Typed, immutable Hook definitions and lifecycle outcomes.

Hooks receive snapshots only.  They never receive stores, policy evaluators or
tool executors, so an extension cannot mutate authoritative state directly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json, thaw_json
from offeragent_harness.tools.canonical import canonical_json_bytes, canonical_json_sha256

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_AUDIT_TAG = re.compile(r"[a-z][a-z0-9_.:-]{0,63}")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_PROFILE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_SAFE_ARGUMENT = re.compile(r"[^\x00-\x1f\x7f&|<>^;`]{0,4096}")
_FORBIDDEN_COMMAND_SWITCH = re.compile(r"(?:-|/)(?:c|command|enc|encodedcommand)", re.IGNORECASE)
_MAX_PATCH_BYTES = 256 * 1024
_MAX_HINTS = 16
_MAX_HINT_CHARS = 4096


class HookEvent(str, Enum):
    SESSION_START = "SessionStart"
    TURN_START = "TurnStart"
    BEFORE_MODEL = "BeforeModel"
    AFTER_MODEL = "AfterModel"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    APPROVAL_REQUIRED = "ApprovalRequired"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    BEFORE_COMPACT = "BeforeCompact"
    TURN_STOP = "TurnStop"
    RUNTIME_SHUTDOWN = "RuntimeShutdown"


class HookDecision(str, Enum):
    CONTINUE = "continue"
    ASK = "ask"
    DENY = "deny"


class HookScope(str, Enum):
    MANAGED = "managed"
    USER = "user"
    WORKSPACE = "workspace"
    SESSION = "session"


class HookImplementation(str, Enum):
    BUILTIN = "builtin"
    COMMAND = "command"


class HookFailureMode(str, Enum):
    FAIL_CLOSED = "fail_closed"
    WARN = "warn"


@dataclass(frozen=True, slots=True)
class HookExecutionContext:
    managed_owner_id: str
    principal_id: str
    workspace_id: str
    session_id: str
    workspace_trusted: bool

    def __post_init__(self) -> None:
        for value in (self.managed_owner_id, self.principal_id, self.workspace_id, self.session_id):
            _require_identity(value, "Hook execution context identity")


@dataclass(frozen=True, slots=True)
class HookToolInput:
    tool_call_id: str
    name: str
    version: str
    definition_fingerprint: str
    arguments: Mapping[str, Any]
    args_hash: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for value in (self.tool_call_id, self.name, self.version, self.idempotency_key):
            _require_identity(value, "Hook tool identity")
        _require_hash(self.definition_fingerprint, "definition_fingerprint")
        _require_hash(self.args_hash, "args_hash")
        frozen = freeze_json(self.arguments)
        if not isinstance(frozen, FrozenJsonObject):
            raise TypeError("Hook tool arguments must be a JSON object")
        if canonical_json_sha256(frozen) != self.args_hash:
            raise ValueError("Hook tool args_hash does not match canonical arguments")
        object.__setattr__(self, "arguments", frozen)


@dataclass(frozen=True, slots=True)
class HookInvocation:
    invocation_id: str
    chain_id: str
    event: HookEvent
    context: HookExecutionContext
    run_id: str | None
    facts: Mapping[str, Any] = field(default_factory=dict)
    tool: HookToolInput | None = None

    def __post_init__(self) -> None:
        _require_identity(self.invocation_id, "Hook invocation_id")
        _require_identity(self.chain_id, "Hook chain_id")
        if self.run_id is not None:
            _require_identity(self.run_id, "Hook run_id")
        frozen = freeze_json(self.facts)
        if not isinstance(frozen, FrozenJsonObject):
            raise TypeError("Hook facts must be a JSON object")
        object.__setattr__(self, "facts", frozen)
        if self.event in {HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE, HookEvent.APPROVAL_REQUIRED}:
            if self.tool is None:
                raise ValueError(f"{self.event.value} requires a typed HookToolInput")
        elif self.tool is not None:
            raise ValueError(f"{self.event.value} cannot carry HookToolInput")

    @property
    def request_hash(self) -> str:
        return canonical_json_sha256(
            {
                "invocationId": self.invocation_id,
                "chainId": self.chain_id,
                "event": self.event.value,
                "managedOwnerId": self.context.managed_owner_id,
                "principalId": self.context.principal_id,
                "workspaceId": self.context.workspace_id,
                "sessionId": self.context.session_id,
                "workspaceTrusted": self.context.workspace_trusted,
                "runId": self.run_id,
                "factsHash": canonical_json_sha256(self.facts),
                "tool": (
                    None
                    if self.tool is None
                    else {
                        "toolCallId": self.tool.tool_call_id,
                        "name": self.tool.name,
                        "version": self.tool.version,
                        "definitionFingerprint": self.tool.definition_fingerprint,
                        "argsHash": self.tool.args_hash,
                        "idempotencyKey": self.tool.idempotency_key,
                    }
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class HookCommandSpec:
    """A supervisor-owned executable profile, never a shell command string."""

    executable_id: str
    arguments: tuple[str, ...] = ()
    allowed_environment: frozenset[str] = frozenset()
    executable_profile_fingerprint: str | None = None
    cwd_root_id: str = "vault"
    cwd: str = ""
    environment_profile_id: str = "minimal"
    artifact_output_limit_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        _require_identity(self.executable_id, "Hook executable_id")
        arguments = tuple(self.arguments)
        if len(arguments) > 64 or any(
            not isinstance(item, str)
            or _SAFE_ARGUMENT.fullmatch(item) is None
            or _FORBIDDEN_COMMAND_SWITCH.fullmatch(item) is not None
            for item in arguments
        ):
            raise ValueError("Hook command arguments exceed their structural limit")
        environment = frozenset(self.allowed_environment)
        if any(_ENVIRONMENT_NAME.fullmatch(name) is None for name in environment):
            raise ValueError("Hook environment names must be portable identifiers")
        if (
            self.executable_profile_fingerprint is not None
            and _SHA256.fullmatch(self.executable_profile_fingerprint) is None
        ):
            raise ValueError("Hook executable profile fingerprint must be canonical SHA-256")
        if (
            _PROFILE_ID.fullmatch(self.cwd_root_id) is None
            or _PROFILE_ID.fullmatch(self.environment_profile_id) is None
        ):
            raise ValueError("Hook cwd/environment capability IDs are invalid")
        segments = self.cwd.split("/") if self.cwd else []
        if (
            "\\" in self.cwd
            or self.cwd.startswith("/")
            or any(segment in {"", ".", ".."} for segment in segments)
            or any(ord(character) < 32 or character in '<>:"|?*' for character in self.cwd)
        ):
            raise ValueError("Hook cwd must be a safe relative capability path")
        if not 1 <= self.artifact_output_limit_bytes <= 16 * 1024 * 1024:
            raise ValueError("Hook Artifact output limit is invalid")
        object.__setattr__(self, "arguments", arguments)
        object.__setattr__(self, "allowed_environment", environment)


@dataclass(frozen=True, slots=True)
class HookDefinition:
    hook_id: str
    scope: HookScope
    owner_id: str
    event: HookEvent
    implementation: HookImplementation
    priority: int = 0
    timeout_ms: int = 5_000
    output_limit_bytes: int = 64 * 1024
    enabled: bool = True
    handler_id: str | None = None
    command: HookCommandSpec | None = None

    def __post_init__(self) -> None:
        _require_identity(self.hook_id, "Hook hook_id")
        _require_identity(self.owner_id, "Hook owner_id")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise TypeError("Hook priority must be an integer")
        if not 1 <= self.timeout_ms <= 300_000:
            raise ValueError("Hook timeout_ms must be between 1 and 300000")
        if not 1 <= self.output_limit_bytes <= 1024 * 1024:
            raise ValueError("Hook output_limit_bytes must be between 1 and 1048576")
        if self.implementation is HookImplementation.BUILTIN:
            if self.handler_id is None or self.command is not None:
                raise ValueError("builtin Hook requires handler_id and forbids command")
            _require_identity(self.handler_id, "Hook handler_id")
        elif self.command is None or self.handler_id is not None:
            raise ValueError("command Hook requires command and forbids handler_id")


@dataclass(frozen=True, slots=True)
class HookLayer:
    scope: HookScope
    owner_id: str
    revision: int
    hooks: tuple[HookDefinition, ...] = ()
    denied_events: frozenset[HookEvent] = frozenset()
    denied_hook_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _require_identity(self.owner_id, "Hook layer owner_id")
        if self.revision < 0:
            raise ValueError("Hook layer revision cannot be negative")
        hooks = tuple(self.hooks)
        if len({item.hook_id for item in hooks}) != len(hooks):
            raise ValueError("Hook layer contains duplicate hook_id values")
        if any(item.scope is not self.scope or item.owner_id != self.owner_id for item in hooks):
            raise ValueError("Hook definition scope/owner must match its layer")
        if self.scope is not HookScope.MANAGED and (self.denied_events or self.denied_hook_ids):
            raise ValueError("only the managed Hook layer may define non-overridable denials")
        denied_hook_ids = frozenset(self.denied_hook_ids)
        if any(_IDENTITY.fullmatch(item) is None for item in denied_hook_ids):
            raise ValueError("managed denied Hook IDs are invalid")
        object.__setattr__(self, "hooks", hooks)
        object.__setattr__(self, "denied_events", frozenset(self.denied_events))
        object.__setattr__(self, "denied_hook_ids", denied_hook_ids)


@dataclass(frozen=True, slots=True)
class ResolvedHookPlan:
    hooks: tuple[HookDefinition, ...]
    managed_denied: bool
    denied_hook_ids: frozenset[str]
    layer_revisions: Mapping[str, int]

    def __post_init__(self) -> None:
        revisions = dict(self.layer_revisions)
        object.__setattr__(self, "layer_revisions", revisions)


@dataclass(frozen=True, slots=True)
class HookOutput:
    decision: HookDecision = HookDecision.CONTINUE
    audit_tags: tuple[str, ...] = ()
    argument_patch: Mapping[str, Any] | None = None
    context_hints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        tags = tuple(self.audit_tags)
        if len(tags) > 32 or len(set(tags)) != len(tags) or any(_AUDIT_TAG.fullmatch(tag) is None for tag in tags):
            raise ValueError("Hook audit tags must be unique bounded identifiers")
        hints = tuple(self.context_hints)
        if len(hints) > _MAX_HINTS or any(not hint or len(hint) > _MAX_HINT_CHARS for hint in hints):
            raise ValueError("Hook context hints exceed their count or size limit")
        patch = self.argument_patch
        if patch is not None:
            frozen = freeze_json(patch)
            if not isinstance(frozen, FrozenJsonObject):
                raise TypeError("Hook argument_patch must be a JSON object")
            if len(canonical_json_bytes(frozen)) > _MAX_PATCH_BYTES:
                raise ValueError("Hook argument_patch exceeds its canonical byte limit")
            patch = frozen
        object.__setattr__(self, "audit_tags", tags)
        object.__setattr__(self, "argument_patch", patch)
        object.__setattr__(self, "context_hints", hints)


@dataclass(frozen=True, slots=True)
class HookOutcome:
    decision: HookDecision
    audit_tags: tuple[str, ...]
    context_hints: tuple[str, ...]
    applied_hook_ids: tuple[str, ...]
    warning_codes: tuple[str, ...]
    mutated_arguments: Mapping[str, Any] | None = None
    mutated_args_hash: str | None = None
    replayed: bool = False

    def __post_init__(self) -> None:
        arguments = self.mutated_arguments
        if arguments is None:
            if self.mutated_args_hash is not None:
                raise ValueError("mutated_args_hash requires mutated_arguments")
        else:
            frozen = freeze_json(arguments)
            if not isinstance(frozen, FrozenJsonObject):
                raise TypeError("Hook mutated_arguments must be a JSON object")
            actual = canonical_json_sha256(frozen)
            if self.mutated_args_hash != actual:
                raise ValueError("Hook mutated_args_hash does not match mutated_arguments")
            arguments = frozen
        object.__setattr__(self, "mutated_arguments", arguments)
        object.__setattr__(self, "audit_tags", tuple(self.audit_tags))
        object.__setattr__(self, "context_hints", tuple(self.context_hints))
        object.__setattr__(self, "applied_hook_ids", tuple(self.applied_hook_ids))
        object.__setattr__(self, "warning_codes", tuple(self.warning_codes))

    @classmethod
    def continue_without_hooks(cls) -> HookOutcome:
        return cls(HookDecision.CONTINUE, (), (), (), ())


@dataclass(frozen=True, slots=True)
class HookAuditRecord:
    audit_id: str
    invocation_id: str
    event: HookEvent
    request_hash: str
    decision: HookDecision
    applied_hook_ids: tuple[str, ...]
    audit_tags: tuple[str, ...]
    warning_codes: tuple[str, ...]
    original_args_hash: str | None
    mutated_args_hash: str | None
    workspace_id_hash: str
    run_id_hash: str | None

    def payload(self) -> dict[str, Any]:
        """Return the redacted persistence/event form; no raw Hook payloads."""

        return {
            "schemaVersion": 1,
            "auditId": self.audit_id,
            "invocationId": self.invocation_id,
            "event": self.event.value,
            "requestHash": self.request_hash,
            "decision": self.decision.value,
            "appliedHookIds": list(self.applied_hook_ids),
            "auditTags": list(self.audit_tags),
            "warningCodes": list(self.warning_codes),
            "originalArgsHash": self.original_args_hash,
            "mutatedArgsHash": self.mutated_args_hash,
            "workspaceIdHash": self.workspace_id_hash,
            "runIdHash": self.run_id_hash,
        }


def resolve_hook_plan(layers: Sequence[HookLayer], invocation: HookInvocation) -> ResolvedHookPlan:
    """Select the four exact owners and apply managed denials before ordering."""

    expected = {
        HookScope.MANAGED: invocation.context.managed_owner_id,
        HookScope.USER: invocation.context.principal_id,
        HookScope.WORKSPACE: invocation.context.workspace_id,
        HookScope.SESSION: invocation.context.session_id,
    }
    selected = [layer for layer in layers if expected[layer.scope] == layer.owner_id]
    keys = [(layer.scope, layer.owner_id) for layer in selected]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate Hook layers for one effective owner")
    managed = next((layer for layer in selected if layer.scope is HookScope.MANAGED), None)
    denied_ids = frozenset() if managed is None else managed.denied_hook_ids
    denied_event = managed is not None and invocation.event in managed.denied_events
    order = {
        HookScope.MANAGED: 0,
        HookScope.USER: 1,
        HookScope.WORKSPACE: 2,
        HookScope.SESSION: 3,
    }
    hooks = tuple(
        sorted(
            (
                hook
                for layer in selected
                for hook in layer.hooks
                if hook.enabled and hook.event is invocation.event and hook.hook_id not in denied_ids
            ),
            key=lambda hook: (order[hook.scope], -hook.priority, hook.hook_id),
        )
    )
    return ResolvedHookPlan(
        hooks,
        denied_event,
        denied_ids,
        {f"{layer.scope.value}:{layer.owner_id}": layer.revision for layer in selected},
    )


def merge_argument_patch(arguments: Mapping[str, Any], patch: Mapping[str, Any]) -> FrozenJsonObject:
    """Apply RFC 7396 object merge-patch; the ToolValidator remains authoritative."""

    mutable = thaw_json(freeze_json(arguments))
    assert isinstance(mutable, dict)
    patch_value = thaw_json(freeze_json(patch))
    assert isinstance(patch_value, dict)
    _merge_patch(mutable, patch_value)
    frozen = freeze_json(mutable)
    assert isinstance(frozen, FrozenJsonObject)
    return frozen


def _merge_patch(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, Mapping):
            current = target.get(key)
            if not isinstance(current, dict):
                current = {}
                target[key] = current
            _merge_patch(current, value)
        else:
            target[key] = value


def _require_identity(value: str, label: str) -> None:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_hash(value: str, label: str) -> None:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} must be a canonical sha256 digest")


__all__ = [
    "HookAuditRecord",
    "HookCommandSpec",
    "HookDecision",
    "HookDefinition",
    "HookEvent",
    "HookExecutionContext",
    "HookFailureMode",
    "HookImplementation",
    "HookInvocation",
    "HookLayer",
    "HookOutcome",
    "HookOutput",
    "HookScope",
    "HookToolInput",
    "ResolvedHookPlan",
    "merge_argument_patch",
    "resolve_hook_plan",
]
