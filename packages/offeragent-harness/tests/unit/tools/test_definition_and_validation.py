from __future__ import annotations

import inspect
import math
from collections.abc import Mapping
from typing import Any

import pytest

from offeragent_harness.models import thaw_json
from offeragent_harness.permissions import RiskClass
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools import (
    CanonicalJsonError,
    ExecutorLocation,
    SideEffectClass,
    ToolCall,
    ToolDefinition,
    ToolDefinitionError,
    ToolValidationError,
    ToolValidator,
    canonical_json_bytes,
    canonical_json_sha256,
)


def definition(
    *,
    input_schema: Mapping[str, Any] | None = None,
    side_effect_class: SideEffectClass = SideEffectClass.WRITE,
    required_capabilities: frozenset[str] = frozenset({"vault.write", "obsidian.bridge"}),
    concurrency_safe: bool = False,
    idempotent: bool = True,
    retryable: bool = True,
    output_limit_bytes: int = 1024,
) -> ToolDefinition:
    default_input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "mode": {"enum": ["read", "write"]},
            "expectedHash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            "labels": {
                "type": "array",
                "prefixItems": [{"const": "offeragent"}],
                "items": {"type": "string"},
            },
        },
        "required": ["path", "mode"],
        "allOf": [
            {
                "if": {"properties": {"mode": {"const": "write"}}, "required": ["mode"]},
                "then": {"required": ["expectedHash"]},
            }
        ],
        "unevaluatedProperties": False,
    }
    return ToolDefinition(
        name="vault.patch",
        version="1",
        description="Patch a Vault note with an expected hash",
        input_schema=default_input_schema if input_schema is None else input_schema,
        output_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        executor_location=ExecutorLocation.CLIENT,
        risk=RiskClass.WRITE,
        side_effect_class=side_effect_class,
        required_capabilities=required_capabilities,
        concurrency_safe=concurrency_safe,
        idempotent=idempotent,
        retryable=retryable,
        timeout_ms=30_000,
        output_limit_bytes=output_limit_bytes,
    )


def test_every_security_field_is_explicitly_required() -> None:
    parameters = inspect.signature(ToolDefinition).parameters
    for name in (
        "executor_location",
        "risk",
        "side_effect_class",
        "required_capabilities",
        "concurrency_safe",
        "idempotent",
        "retryable",
        "timeout_ms",
        "output_limit_bytes",
    ):
        assert parameters[name].default is inspect.Parameter.empty


def test_unknown_side_effects_fail_closed() -> None:
    with pytest.raises(ToolDefinitionError, match="fail closed"):
        definition(side_effect_class=SideEffectClass.UNKNOWN, idempotent=True)


def test_retry_and_concurrency_flags_cannot_contradict_side_effect_safety() -> None:
    with pytest.raises(ToolDefinitionError, match="idempotent"):
        definition(idempotent=False, retryable=True)
    with pytest.raises(ToolDefinitionError, match="concurrent"):
        definition(concurrency_safe=True)
    with pytest.raises(ToolDefinitionError, match="capability"):
        definition(required_capabilities=frozenset())


def test_invalid_or_open_input_schema_is_rejected_at_registration() -> None:
    with pytest.raises(ToolDefinitionError, match="Draft 2020-12"):
        definition(input_schema={"type": "not-a-real-type", "additionalProperties": False})
    with pytest.raises(ToolDefinitionError, match="fail closed"):
        definition(input_schema={"type": "object", "properties": {"path": {"type": "string"}}})
    with pytest.raises(ToolDefinitionError, match="external schema reference"):
        definition(
            input_schema={
                "type": "object",
                "properties": {"path": {"$ref": "https://attacker.invalid/schema.json"}},
                "additionalProperties": False,
            }
        )


def test_draft_2020_12_validation_checks_conditionals_prefix_items_and_unknown_fields() -> None:
    validator = ToolValidator()
    valid = {
        "mode": "write",
        "path": "notes/中文 空格.md",
        "expectedHash": "sha256:" + "a" * 64,
        "labels": ["offeragent", "safe"],
    }
    normalized = validator.validate_arguments(definition(), valid)
    assert normalized.args_hash == canonical_json_sha256(valid)

    with pytest.raises(ToolValidationError) as error:
        validator.validate_arguments(
            definition(),
            {"mode": "write", "path": "a.md", "labels": ["wrong"], "surprise": True},
        )
    keywords = {issue.keyword for issue in error.value.issues}
    assert {"required", "const", "unevaluatedProperties"} <= keywords


def test_canonical_hash_is_key_order_independent_and_rejects_non_json_numbers() -> None:
    left = {"z": [1, {"中文": "值"}], "a": True}
    right = {"a": True, "z": [1, {"中文": "值"}]}
    assert canonical_json_sha256(left) == canonical_json_sha256(right)
    with pytest.raises(CanonicalJsonError):
        canonical_json_sha256({"bad": math.nan})


def test_canonical_json_matches_ecmascript_number_rules_used_by_typescript() -> None:
    assert canonical_json_bytes({"a": 1.0, "b": -0.0, "c": 1e-6, "d": 1e-7, "e": 1e20}) == (
        b'{"a":1,"b":0,"c":0.000001,"d":1e-7,"e":100000000000000000000}'
    )
    assert canonical_json_sha256({"value": 1}) == canonical_json_sha256({"value": 1.0})
    with pytest.raises(CanonicalJsonError, match="safe range"):
        canonical_json_bytes({"unsafe": 2**53})


def test_tool_call_recomputes_and_verifies_args_hash() -> None:
    arguments = {"path": "notes/a.md", "mode": "read"}
    call = ToolCall(
        tool_call_id="call_1",
        run_id="run_1",
        workspace_id="ws_1",
        name="vault.patch",
        version="1",
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="idem_1",
        deadline=None,
        lineage=AgentLineage.root("run_1"),
    )
    assert call.args_hash == canonical_json_sha256(arguments)
    with pytest.raises(ValueError, match="args_hash"):
        ToolCall(
            tool_call_id="call_2",
            run_id="run_1",
            workspace_id="ws_1",
            name="vault.patch",
            version="1",
            arguments=arguments,
            args_hash="sha256:" + "0" * 64,
            idempotency_key="idem_2",
            deadline=None,
            lineage=AgentLineage.root("run_1"),
        )


def test_output_schema_and_canonical_byte_limit_are_enforced() -> None:
    validator = ToolValidator()
    assert thaw_json(validator.validate_output(definition(), {"ok": True})) == {"ok": True}
    with pytest.raises(ToolValidationError):
        validator.validate_output(definition(), {"ok": "yes"})
    with pytest.raises(ToolValidationError, match="bytes"):
        validator.validate_output(definition(output_limit_bytes=5), {"ok": True})
