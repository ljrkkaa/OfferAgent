"""Strict Draft 2020-12 contract for the local ``vault.transaction`` tool."""

from __future__ import annotations

from typing import Any

from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.permissions import RiskClass
from offeragent_harness.tools.definitions import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffectClass,
    ToolDefinition,
)

ABSENT_HASH = "absent"
VAULT_TRANSACTION_PREFLIGHT_PROVIDER = "vault.transaction.v1"

_PATH = {"type": "string", "minLength": 1, "maxLength": 1024}
_HASH = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
_EXPECTED = {"oneOf": [{"const": ABSENT_HASH}, _HASH]}
_CONTENT = {"type": "string", "maxLength": 1_048_576}


def _operation(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_CREATE = _operation(
    {
        "op": {"const": "create"},
        "path": _PATH,
        "content": _CONTENT,
        "expectedHash": {"const": ABSENT_HASH},
    },
    ["op", "path", "content", "expectedHash"],
)
_APPEND = _operation(
    {
        "op": {"const": "append"},
        "path": _PATH,
        "content": _CONTENT,
        "expectedHash": _HASH,
    },
    ["op", "path", "content", "expectedHash"],
)
_REPLACE = _operation(
    {
        "op": {"const": "replace"},
        "path": _PATH,
        "find": {"type": "string", "minLength": 1, "maxLength": 1_048_576},
        "replace": _CONTENT,
        "expectedHash": _HASH,
    },
    ["op", "path", "find", "replace", "expectedHash"],
)
_PATCH = _operation(
    {
        "op": {"const": "patch"},
        "path": _PATH,
        "edits": {
            "type": "array",
            "minItems": 1,
            "maxItems": 256,
            "items": _operation(
                {
                    "startLine": {"type": "integer", "minimum": 1},
                    "endLine": {"type": "integer", "minimum": 1},
                    "replacement": _CONTENT,
                },
                ["startLine", "endLine", "replacement"],
            ),
        },
        "expectedHash": _HASH,
    },
    ["op", "path", "edits", "expectedHash"],
)
_RENAME = _operation(
    {
        "op": {"const": "rename"},
        "path": _PATH,
        "destination": _PATH,
        "expectedHash": _HASH,
        "expectedDestinationHash": _EXPECTED,
    },
    ["op", "path", "destination", "expectedHash", "expectedDestinationHash"],
)
_TRASH = _operation(
    {
        "op": {"const": "trash"},
        "path": _PATH,
        "expectedHash": _HASH,
    },
    ["op", "path", "expectedHash"],
)


def _transaction_schema(*, operations: list[dict[str, Any]], max_items: int) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_items,
                "items": {"oneOf": operations},
            }
        },
        "required": ["operations"],
        "additionalProperties": False,
    }


# This is the only model-facing write contract.  One invocation can mutate one
# logical/physical path, which is the unit covered by the durable crash manifest.
VAULT_TRANSACTION_SCHEMA: dict[str, Any] = _transaction_schema(
    operations=[_CREATE, _APPEND, _REPLACE, _PATCH],
    max_items=1,
)

# Rename/trash and multi-path planning remain domain capabilities for migration
# and coordinator tests.  This schema must never be registered in a model-facing
# Tool Registry.
INTERNAL_VAULT_TRANSACTION_SCHEMA: dict[str, Any] = _transaction_schema(
    operations=[_CREATE, _APPEND, _REPLACE, _PATCH, _RENAME, _TRASH],
    max_items=20,
)

VAULT_TRANSACTION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
        "stateHash": _HASH,
        "recoveryArtifactIds": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 16,
        },
    },
    "required": ["paths", "stateHash", "recoveryArtifactIds"],
    "additionalProperties": False,
}


def vault_transaction_definition(
    *,
    executor_location: ExecutorLocation = ExecutorLocation.LOCAL,
) -> ToolDefinition:
    """Return the one model-facing Vault write definition for a Run.

    Production composition always selects ``LOCAL``: Obsidian contributes a
    read-only live-editor proof, while the Worker's one transaction coordinator
    owns every durable mutation.  ``CLIENT`` remains accepted only so recovery
    can recognize journals written by pre-migration development builds.
    """

    if executor_location not in {ExecutorLocation.LOCAL, ExecutorLocation.CLIENT}:
        raise ValueError("vault.transaction can execute only in the local Worker or authenticated client")
    return ToolDefinition(
        name="vault.transaction",
        version="1",
        description="Apply an approved, hash-bound local Vault transaction.",
        input_schema=VAULT_TRANSACTION_SCHEMA,
        output_schema=VAULT_TRANSACTION_OUTPUT_SCHEMA,
        executor_location=executor_location,
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        required_capabilities=frozenset({"workspace.read", "vault.write"}),
        concurrency_safe=False,
        idempotent=True,
        retryable=False,
        timeout_ms=120_000,
        output_limit_bytes=64 * 1024,
        preflight_mode=PreflightMode.REQUIRED,
        preflight_provider=VAULT_TRANSACTION_PREFLIGHT_PROVIDER,
        approval_evidence=ApprovalEvidence.DIFF,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    )


def legacy_vault_transaction_definition(
    *,
    executor_location: ExecutorLocation = ExecutorLocation.LOCAL,
) -> ToolDefinition:
    """Recognize the retired broad fingerprint during fail-closed recovery.

    Callers may place this definition in a fingerprint resolver only.  It is
    intentionally distinct from :func:`vault_transaction_definition` and must
    never be inserted into an active Tool Registry.
    """

    if executor_location not in {ExecutorLocation.LOCAL, ExecutorLocation.CLIENT}:
        raise ValueError("vault.transaction can execute only in the local Worker or authenticated client")
    return ToolDefinition(
        name="vault.transaction",
        version="1",
        description="Apply an approved, hash-bound local Vault transaction.",
        input_schema=INTERNAL_VAULT_TRANSACTION_SCHEMA,
        output_schema=VAULT_TRANSACTION_OUTPUT_SCHEMA,
        executor_location=executor_location,
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        required_capabilities=frozenset({"workspace.read", "vault.write"}),
        concurrency_safe=False,
        idempotent=True,
        retryable=False,
        timeout_ms=120_000,
        output_limit_bytes=64 * 1024,
        preflight_mode=PreflightMode.REQUIRED,
        preflight_provider=VAULT_TRANSACTION_PREFLIGHT_PROVIDER,
        approval_evidence=ApprovalEvidence.DIFF,
        result_sensitivity=ResultSensitivity.UNKNOWN,
    )


def legacy_public_vault_transaction_definition(
    *,
    executor_location: ExecutorLocation = ExecutorLocation.LOCAL,
) -> ToolDefinition:
    """Recognize the pre-classification public-schema fingerprint only.

    Recovery may resolve this immutable definition to explain an old snapshot,
    but UNKNOWN prevents insertion into an active Registry or silent replay.
    """

    if executor_location not in {ExecutorLocation.LOCAL, ExecutorLocation.CLIENT}:
        raise ValueError("vault.transaction can execute only in the local Worker or authenticated client")
    definition = vault_transaction_definition(executor_location=executor_location)
    return ToolDefinition(
        name=definition.name,
        version=definition.version,
        description=definition.description,
        input_schema=thaw_json(definition.input_schema),
        output_schema=thaw_json(definition.output_schema),
        executor_location=definition.executor_location,
        risk=definition.risk,
        side_effect_class=definition.side_effect_class,
        required_capabilities=definition.required_capabilities,
        concurrency_safe=definition.concurrency_safe,
        idempotent=definition.idempotent,
        retryable=definition.retryable,
        timeout_ms=definition.timeout_ms,
        output_limit_bytes=definition.output_limit_bytes,
        preflight_mode=definition.preflight_mode,
        preflight_provider=definition.preflight_provider,
        approval_evidence=definition.approval_evidence,
        result_sensitivity=ResultSensitivity.UNKNOWN,
    )


def client_vault_transaction_definition() -> ToolDefinition:
    """Return the legacy CLIENT fingerprint for fail-closed journal recovery."""

    return legacy_public_vault_transaction_definition(executor_location=ExecutorLocation.CLIENT)


__all__ = [
    "ABSENT_HASH",
    "INTERNAL_VAULT_TRANSACTION_SCHEMA",
    "VAULT_TRANSACTION_OUTPUT_SCHEMA",
    "VAULT_TRANSACTION_PREFLIGHT_PROVIDER",
    "VAULT_TRANSACTION_SCHEMA",
    "client_vault_transaction_definition",
    "legacy_public_vault_transaction_definition",
    "legacy_vault_transaction_definition",
    "vault_transaction_definition",
]
