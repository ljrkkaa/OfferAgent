"""Canonical invocation-journal and crash-replay safety rules.

Both the live Tool Kernel and startup RecoveryCoordinator depend on these pure
functions.  Keeping them outside either orchestrator prevents recovery behavior
from drifting away from normal exactly-once execution semantics.
"""

from __future__ import annotations

from .definitions import SideEffectClass, ToolCall, ToolDefinition


def invocation_journal_scope(call: ToolCall, definition: ToolDefinition) -> str:
    if (call.name, call.version) != (definition.name, definition.version):
        raise ValueError("ToolCall and ToolDefinition identities do not match")
    if call.definition_fingerprint != definition.fingerprint:
        raise ValueError("ToolCall and ToolDefinition fingerprints do not match")
    return ":".join((call.workspace_id, call.lineage.root_run_id, call.run_id, definition.name, definition.version))


def invocation_request_fingerprint(call: ToolCall) -> str:
    return call.idempotency_fingerprint


def is_side_effect_free(definition: ToolDefinition) -> bool:
    return definition.side_effect_class in {SideEffectClass.NONE, SideEffectClass.READ}


def is_safe_crash_replay(definition: ToolDefinition) -> bool:
    return is_side_effect_free(definition) and definition.idempotent and definition.retryable


__all__ = [
    "invocation_journal_scope",
    "invocation_request_fingerprint",
    "is_safe_crash_replay",
    "is_side_effect_free",
]
