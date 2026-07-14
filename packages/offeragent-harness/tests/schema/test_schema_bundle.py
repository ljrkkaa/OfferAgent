from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator

from offeragent_harness.protocol.events import EVENT_REGISTRY
from offeragent_harness.protocol.jsonrpc import (
    EventNotification,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    RpcCancelNotification,
    parse_jsonrpc_message,
    validate_request,
    validate_response,
)
from offeragent_harness.protocol.messages import ALL_METHOD_REGISTRY, COMMAND_REGISTRY, REVERSE_REQUEST_REGISTRY
from offeragent_harness.protocol.schemas import (
    BUNDLE_FILENAME,
    DEFAULT_SCHEMA_DIR,
    EXAMPLE_METHODS,
    MANIFEST_FILENAME,
    build_examples,
    build_schema_bundle,
    check_schema_artifacts,
    generate_schema_artifacts,
    generated_artifacts,
    main,
    schema_bundle_bytes,
    schema_hash,
)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast(dict[str, object], value)


def test_committed_schema_artifacts_are_current() -> None:
    assert check_schema_artifacts(DEFAULT_SCHEMA_DIR) == []
    assert main(["check", "--output", str(DEFAULT_SCHEMA_DIR)]) == 0


def test_generation_is_byte_for_byte_reproducible_across_directories(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_artifacts = generate_schema_artifacts(first)
    second_artifacts = generate_schema_artifacts(second)
    assert first_artifacts == second_artifacts == generated_artifacts()
    for relative_name in generated_artifacts():
        assert (first / relative_name).read_bytes() == (second / relative_name).read_bytes()


def test_check_cli_detects_tampering_without_rewriting(tmp_path: Path) -> None:
    generate_schema_artifacts(tmp_path)
    target = tmp_path / BUNDLE_FILENAME
    target.write_bytes(target.read_bytes() + b" ")
    before = target.read_bytes()
    mismatches = check_schema_artifacts(tmp_path)
    assert len(mismatches) == 1
    assert mismatches[0].startswith(f"changed: {BUNDLE_FILENAME}")
    assert main(["check", "--output", str(tmp_path)]) == 1
    assert target.read_bytes() == before


def test_check_detects_stale_generated_json_file(tmp_path: Path) -> None:
    generate_schema_artifacts(tmp_path)
    stale = tmp_path / "examples" / "removed-command.json"
    stale.write_text("{}\n", encoding="utf-8")
    assert check_schema_artifacts(tmp_path) == ["unexpected: examples/removed-command.json"]


def test_manifest_hashes_exact_generated_bytes() -> None:
    manifest = json.loads((DEFAULT_SCHEMA_DIR / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    bundle = (DEFAULT_SCHEMA_DIR / BUNDLE_FILENAME).read_bytes()
    assert manifest["schemaHash"] == schema_hash() == _digest(bundle)
    assert bundle == schema_bundle_bytes()
    for relative_name, metadata in manifest["files"].items():
        payload = (DEFAULT_SCHEMA_DIR / relative_name).read_bytes()
        assert metadata == {"sha256": _digest(payload), "sizeBytes": len(payload)}


def test_bundle_registries_are_complete_and_use_refs_to_closed_models() -> None:
    bundle = build_schema_bundle()
    commands = _object(bundle["commands"])
    reverse_requests = _object(bundle["reverseRequests"])
    events = _object(bundle["events"])
    assert set(commands) == set(COMMAND_REGISTRY)
    assert set(reverse_requests) == set(REVERSE_REQUEST_REGISTRY)
    assert set(events) == {event.value for event in EVENT_REGISTRY}

    definitions = _object(bundle["$defs"])
    for spec in ALL_METHOD_REGISTRY.values():
        for model in (spec.params_model, spec.result_model):
            definition = _object(definitions[model.__name__])
            assert definition["type"] == "object"
            assert definition["additionalProperties"] is False
    for payload in EVENT_REGISTRY.values():
        definition = _object(definitions[payload.__name__])
        assert definition["type"] == "object"
        assert definition["additionalProperties"] is False


def test_transport_cancel_notification_has_a_stable_closed_schema_and_manifest_example() -> None:
    bundle = build_schema_bundle()
    envelopes = _object(bundle["envelopes"])
    definitions = _object(bundle["$defs"])
    cancel_ref = _object(envelopes["rpcCancelNotification"])["$ref"]
    assert cancel_ref == "#/$defs/RpcCancelNotification"
    cancel_definition = _object(definitions["RpcCancelNotification"])
    params_definition = _object(definitions["RpcCancelParams"])
    assert cancel_definition["additionalProperties"] is False
    assert params_definition["additionalProperties"] is False
    assert EXAMPLE_METHODS["rpc-cancel.notification.json"] == ("notification", "rpc/cancel")


def test_all_committed_examples_validate_against_dto_and_json_schema() -> None:
    bundle = build_schema_bundle()
    definitions = _object(bundle["$defs"])
    commands = _object(bundle["commands"])
    reverse_requests = _object(bundle["reverseRequests"])
    envelopes = _object(bundle["envelopes"])
    examples = build_examples()
    assert set(examples) == set(EXAMPLE_METHODS)

    for name, example in examples.items():
        message = parse_jsonrpc_message(example)
        kind, method = EXAMPLE_METHODS[name]
        if kind == "request":
            assert isinstance(message, JsonRpcRequest)
            validated = validate_request(message)
            registry = commands if method in COMMAND_REGISTRY else reverse_requests
            method_schema = _object(registry[method])
            params_schema = _object(method_schema["params"])
            schema_ref = params_schema["$ref"]
            instance = validated.params.to_wire()
        elif kind == "response":
            assert isinstance(message, JsonRpcSuccessResponse)
            validated_response = validate_response(method, message)
            registry = commands if method in COMMAND_REGISTRY else reverse_requests
            method_schema = _object(registry[method])
            result_schema = _object(method_schema["result"])
            schema_ref = result_schema["$ref"]
            instance = validated_response.result.to_wire()
        elif kind == "event":
            assert isinstance(message, EventNotification)
            event_schema = _object(envelopes["event"])
            schema_ref = event_schema["$ref"]
            instance = message.params.to_wire()
        else:
            assert kind == "notification"
            assert isinstance(message, RpcCancelNotification)
            cancel_schema = _object(envelopes["rpcCancelNotification"])
            schema_ref = cancel_schema["$ref"]
            instance = message.to_wire()
        assert isinstance(schema_ref, str)
        validator = Draft202012Validator({"$ref": schema_ref, "$defs": definitions})
        assert list(validator.iter_errors(instance)) == [], name


def test_schema_artifacts_do_not_embed_build_time_or_machine_path() -> None:
    artifacts = generated_artifacts()
    combined = b"\n".join(artifacts.values())
    assert b"E:\\Projects" not in combined
    manifest = json.loads(artifacts[MANIFEST_FILENAME])
    assert "generatedAt" not in manifest
    assert b"2026-07-12T" in combined  # fixed protocol example, not generation time
