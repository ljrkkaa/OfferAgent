"""Reproducible JSON Schema bundle, examples, manifest, and check CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic.json_schema import models_json_schema

from ._base import WireModel
from .content import (
    ArtifactContentBlock,
    ArtifactRef,
    ArtifactSourceRef,
    FileContentBlock,
    FileRef,
    ImageContentBlock,
    TextContentBlock,
    VaultSourceRef,
)
from .errors import ErrorEnvelope
from .events import EVENT_REGISTRY, EventEnvelope
from .jsonrpc import (
    EventNotification,
    JsonRpcError,
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    RpcCancelNotification,
    RpcCancelParams,
)
from .messages import ALL_METHOD_REGISTRY

PROTOCOL_VERSION = "1.0"
SCHEMA_VERSION = "1"
BUNDLE_FILENAME = "offeragent-protocol-v1.schema.json"
MANIFEST_FILENAME = "protocol-manifest.json"
GENERATOR_VERSION = "1"


def _default_schema_dir() -> Path:
    # .../offeragent-harness/src/offeragent_harness/protocol/schemas.py
    module_path = Path(__file__).resolve()
    source_schema = module_path.parents[3] / "schema"
    if source_schema.is_dir():
        return source_schema
    return module_path.parents[1] / "_schema"


DEFAULT_SCHEMA_DIR = _default_schema_dir()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _schema_models() -> list[type[WireModel]]:
    models: list[type[WireModel]] = []
    for spec in ALL_METHOD_REGISTRY.values():
        models.extend((spec.params_model, spec.result_model))
    models.extend(EVENT_REGISTRY.values())
    models.extend(
        (
            EventEnvelope,
            ErrorEnvelope,
            JsonRpcError,
            JsonRpcRequest,
            JsonRpcNotification,
            EventNotification,
            RpcCancelParams,
            RpcCancelNotification,
            JsonRpcSuccessResponse,
            JsonRpcErrorResponse,
            ArtifactRef,
            FileRef,
            TextContentBlock,
            FileContentBlock,
            ImageContentBlock,
            ArtifactContentBlock,
            VaultSourceRef,
            ArtifactSourceRef,
        )
    )
    # Preserve first occurrence so generated references and traversal are stable.
    return list(dict.fromkeys(models))


def build_schema_bundle() -> dict[str, object]:
    models = _schema_models()
    model_schemas, definitions_root = models_json_schema(
        [(model, "validation") for model in models],
        ref_template="#/$defs/{model}",
    )

    def model_ref(model: type[WireModel]) -> dict[str, str]:
        schema = model_schemas[(model, "validation")]
        ref = schema.get("$ref")
        if not isinstance(ref, str):
            raise RuntimeError(f"schema for {model.__name__} did not produce a stable $ref")
        return {"$ref": ref}

    commands: dict[str, object] = {}
    for method, spec in ALL_METHOD_REGISTRY.items():
        entry: dict[str, object] = {
            "params": model_ref(spec.params_model),
            "result": model_ref(spec.result_model),
        }
        if spec.required_capability is not None:
            entry["requiredCapability"] = spec.required_capability.value
        commands[method] = entry

    events = {
        event_type.value: {"payload": model_ref(payload_model)} for event_type, payload_model in EVENT_REGISTRY.items()
    }
    definitions = definitions_root.get("$defs")
    if not isinstance(definitions, dict):
        raise RuntimeError("Pydantic did not generate shared schema definitions")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:offeragent:protocol:1.0",
        "title": "OfferAgent Local Harness Protocol v1",
        "description": (
            "Canonical DTO schema shared by the Windows Named Pipe and loopback adapters. "
            "All object schemas are closed and wire fields use camelCase."
        ),
        "protocolVersion": PROTOCOL_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "commands": commands,
        "events": events,
        "envelopes": {
            "request": model_ref(JsonRpcRequest),
            "notification": model_ref(JsonRpcNotification),
            "eventNotification": model_ref(EventNotification),
            "rpcCancelNotification": model_ref(RpcCancelNotification),
            "successResponse": model_ref(JsonRpcSuccessResponse),
            "errorResponse": model_ref(JsonRpcErrorResponse),
            "event": model_ref(EventEnvelope),
            "error": model_ref(ErrorEnvelope),
        },
        "$defs": definitions,
    }


def schema_bundle_bytes() -> bytes:
    return _canonical_json(build_schema_bundle())


def schema_hash() -> str:
    return _sha256(schema_bundle_bytes())


def build_examples(bundle_hash: str | None = None) -> dict[str, dict[str, object]]:
    digest = bundle_hash or schema_hash()
    h_a = "sha256:" + "a" * 64
    return {
        "initialize.request.json": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "1.0",
                "clientVersion": "2.0.0",
                "workspaceId": "ws_xxx",
                "capabilities": {
                    "eventReplay": True,
                    "multiSession": True,
                    "subagents": True,
                },
            },
        },
        "initialize.response.json": {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "1.0",
                "supportedProtocolRange": {"minimum": "1.0", "maximum": "1.0"},
                "runtimeVersion": "2.0.0",
                "coreVersion": "2.0.0",
                "schemaHash": digest,
                "workspaceId": "ws_xxx",
                "workspaceInstanceId": "wsi_xxx",
                "hostPid": 12000,
                "workerPid": 12042,
                "transport": "windows-named-pipe",
                "runtimeArch": "win-x64",
                "buildCommit": "0123456789abcdef0123456789abcdef01234567",
                "capabilities": {
                    "eventReplay": True,
                    "multiSession": True,
                    "approvals": True,
                    "shell": True,
                    "subagents": True,
                    "artifacts": True,
                    "contentBlocks": True,
                    "cancellation": True,
                    "diagnostics": True,
                },
            },
        },
        "rpc-cancel.notification.json": {
            "jsonrpc": "2.0",
            "method": "rpc/cancel",
            "params": {"requestId": "rpc_cancelled_01"},
        },
        "turn-start.request.json": {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "turn/start",
            "params": {
                "sessionId": "ses_01",
                "turnId": "turn_01",
                "idempotencyKey": "turn_01",
                "input": [{"type": "text", "text": "整理当前笔记并补充相关链接"}],
                "runConfig": {
                    "model": "gpt-5.5",
                    "reasoningEffort": "high",
                    "permissionMode": "normal",
                },
            },
        },
        "turn-start.response.json": {
            "jsonrpc": "2.0",
            "id": 12,
            "result": {
                "sessionId": "ses_01",
                "turnId": "turn_01",
                "runId": "run_01",
                "accepted": True,
            },
        },
        "tool-completed.event.json": {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "protocolVersion": "1.0",
                "schemaVersion": "1",
                "eventId": "evt_17",
                "sequence": 17,
                "timestamp": "2026-07-12T10:00:00Z",
                "traceId": "trace_17",
                "workspaceId": "ws_xxx",
                "sessionId": "ses_01",
                "turnId": "turn_01",
                "runId": "run_01",
                "rootRunId": "run_01",
                "parentRunId": None,
                "type": "tool.completed",
                "payload": {
                    "result": {
                        "toolCallId": "call_04",
                        "status": "succeeded",
                        "summary": "已读取目标笔记。",
                        "data": {"path": "raw/xxx.md"},
                        "sourceRefs": [
                            {
                                "type": "vault",
                                "file": {
                                    "workspaceId": "ws_xxx",
                                    "path": "raw/xxx.md",
                                    "contentHash": h_a,
                                },
                                "workspaceRevision": 103,
                                "freshness": "fresh",
                            }
                        ],
                    }
                },
            },
        },
    }


EXAMPLE_METHODS: Mapping[str, tuple[str, str]] = {
    "initialize.request.json": ("request", "initialize"),
    "initialize.response.json": ("response", "initialize"),
    "rpc-cancel.notification.json": ("notification", "rpc/cancel"),
    "turn-start.request.json": ("request", "turn/start"),
    "turn-start.response.json": ("response", "turn/start"),
    "tool-completed.event.json": ("event", "event"),
}


def generated_artifacts() -> dict[str, bytes]:
    bundle = schema_bundle_bytes()
    bundle_hash = _sha256(bundle)
    artifacts: dict[str, bytes] = {BUNDLE_FILENAME: bundle}
    for name, value in build_examples(bundle_hash).items():
        artifacts[f"examples/{name}"] = _canonical_json(value)

    file_entries = {
        name: {"sha256": _sha256(payload), "sizeBytes": len(payload)} for name, payload in sorted(artifacts.items())
    }
    manifest = {
        "manifestVersion": 1,
        "generatorVersion": GENERATOR_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "schemaBundle": BUNDLE_FILENAME,
        "schemaHash": bundle_hash,
        "files": file_entries,
        "examples": {
            f"examples/{name}": {"kind": kind, "method": method}
            for name, (kind, method) in sorted(EXAMPLE_METHODS.items())
        },
    }
    artifacts[MANIFEST_FILENAME] = _canonical_json(manifest)
    return artifacts


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def generate_schema_artifacts(output_dir: Path = DEFAULT_SCHEMA_DIR) -> dict[str, bytes]:
    artifacts = generated_artifacts()
    for relative_name, payload in sorted(artifacts.items()):
        _atomic_write(output_dir / relative_name, payload)
    return artifacts


def check_schema_artifacts(output_dir: Path = DEFAULT_SCHEMA_DIR) -> list[str]:
    expected = generated_artifacts()
    mismatches: list[str] = []
    for relative_name, payload in sorted(expected.items()):
        path = output_dir / relative_name
        if not path.is_file():
            mismatches.append(f"missing: {relative_name}")
            continue
        actual = path.read_bytes()
        if actual != payload:
            mismatches.append(f"changed: {relative_name} (expected {_sha256(payload)}, got {_sha256(actual)})")
    expected_names = set(expected)
    if output_dir.is_dir():
        for path in sorted(output_dir.rglob("*.json")):
            relative_name = path.relative_to(output_dir).as_posix()
            if relative_name not in expected_names:
                mismatches.append(f"unexpected: {relative_name}")
    return mismatches


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("generate", "check", "hash"),
        help="generate artifacts, verify committed artifacts, or print the schema hash",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_SCHEMA_DIR,
        help=f"schema artifact directory (default: {DEFAULT_SCHEMA_DIR})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "hash":
        print(schema_hash())
        return 0
    if args.command == "generate":
        artifacts = generate_schema_artifacts(args.output)
        print(f"generated {len(artifacts)} deterministic protocol artifacts in {args.output}")
        print(schema_hash())
        return 0
    mismatches = check_schema_artifacts(args.output)
    if mismatches:
        for mismatch in mismatches:
            print(mismatch, file=sys.stderr)
        print("run the schema generator and review the resulting diff", file=sys.stderr)
        return 1
    print(f"protocol schema artifacts are reproducible: {schema_hash()}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI subprocess test
    raise SystemExit(main())


__all__ = [
    "BUNDLE_FILENAME",
    "DEFAULT_SCHEMA_DIR",
    "EXAMPLE_METHODS",
    "GENERATOR_VERSION",
    "MANIFEST_FILENAME",
    "PROTOCOL_VERSION",
    "SCHEMA_VERSION",
    "build_examples",
    "build_schema_bundle",
    "check_schema_artifacts",
    "generate_schema_artifacts",
    "generated_artifacts",
    "main",
    "schema_bundle_bytes",
    "schema_hash",
]
