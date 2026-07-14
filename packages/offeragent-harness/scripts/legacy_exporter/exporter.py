"""Canonical JSONL exporter for use in the retired server environment.

Only the supplied row source is read.  The default CLI accepts an offline JSONL
file and never creates a server, database, HTTP, or model connection.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

FORMAT_NAME = "offeragent.legacy-migration"
SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
RECORDS_NAME = "records.jsonl"
RECORD_TYPES = ("conversation", "message", "memory", "vault_action_audit")

_TYPE_ALIASES = {
    "conversation": "conversation",
    "Conversation": "conversation",
    "message": "message",
    "Message": "message",
    "memory": "memory",
    "Memory": "memory",
    "vault_action": "vault_action_audit",
    "VaultAction": "vault_action_audit",
    "vault_action_audit": "vault_action_audit",
}
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:api[_-]?key|token|cookie|authorization|credential|password|secret|tool[_-]?credentials|"
    r"server[_-]?url|base[_-]?url|api[_-]?url|auth[_-]?json)"
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)
_RFC3339 = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_WORKSPACE_ID = re.compile(r"^ws_[A-Za-z0-9][A-Za-z0-9_-]*$")
_PROFILE_ID = re.compile(r"^profile_[A-Za-z0-9][A-Za-z0-9_-]*$")


class ExportError(ValueError):
    pass


class LegacyRowSource(Protocol):
    """Abstract read-only source; implementations must not mutate returned rows."""

    def iter_rows(self) -> Iterable[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class JsonlRowSource:
    path: Path
    max_line_bytes: int = 2 * 1024 * 1024

    def iter_rows(self) -> Iterator[Mapping[str, Any]]:
        source = self.path.expanduser()
        if source.is_symlink():
            raise ExportError("offline input must be a regular non-symlink file")
        source = source.resolve(strict=True)
        if not source.is_file():
            raise ExportError("offline input must be a regular non-symlink file")
        with source.open("rb") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip() or len(raw) > self.max_line_bytes:
                    raise ExportError(f"input line {line_number} is blank or too large")
                try:
                    value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ExportError(f"input line {line_number} is not strict UTF-8 JSON") from error
                if not isinstance(value, dict):
                    raise ExportError(f"input line {line_number} must be an object")
                yield value


@dataclass(frozen=True)
class ExportReport:
    output_directory: Path
    export_id: str
    manifest_hash: str
    record_count: int
    record_type_counts: dict[str, int]
    redacted_values: int


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ExportError("legacy row is not JSON serializable") from error


def digest(value: bytes) -> str:
    return f"sha256:{sha256(value).hexdigest()}"


def export_bundle(
    source: LegacyRowSource,
    output_directory: str | Path,
    *,
    target_workspace_id: str,
    target_profile_id: str,
    export_id: str | None = None,
    created_at: str | None = None,
) -> ExportReport:
    output = Path(output_directory).expanduser()
    if output.is_symlink():
        raise ExportError("output directory must not be a symlink")
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve(strict=True)
    if _WORKSPACE_ID.fullmatch(target_workspace_id) is None or _PROFILE_ID.fullmatch(target_profile_id) is None:
        raise ExportError("target Workspace/Profile identifiers are not canonical")
    export_time = created_at or datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    _timestamp(export_time, "createdAt")
    resolved_export_id = export_id or f"export_{sha256(export_time.encode()).hexdigest()}"
    _bounded_text(resolved_export_id, "exportId", 256)

    counts: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    redacted_count = 0
    records_chunks: list[bytes] = []
    for original in source.iter_rows():
        sanitized, changed = _redact(original)
        redacted_count += changed
        if not isinstance(sanitized, Mapping):
            raise ExportError("redacting a legacy row must preserve its object shape")
        row = _normalize_row(sanitized)
        identity = (row["recordType"], row["legacyId"])
        if identity in seen:
            raise ExportError("duplicate legacy record identity")
        seen.add(identity)
        envelope = {
            "schemaVersion": SCHEMA_VERSION,
            "recordType": row["recordType"],
            "legacyId": row["legacyId"],
            "occurredAt": row["occurredAt"],
            "targetWorkspaceId": target_workspace_id,
            "targetProfileId": target_profile_id,
            "payload": row["payload"],
        }
        envelope["recordHash"] = digest(canonical_json_bytes(envelope))
        records_chunks.append(canonical_json_bytes(envelope) + b"\n")
        counts[row["recordType"]] += 1

    records_raw = b"".join(records_chunks)
    type_counts = {record_type: counts[record_type] for record_type in RECORD_TYPES}
    manifest: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "format": FORMAT_NAME,
        "exportId": resolved_export_id,
        "createdAt": export_time,
        "targetWorkspaceId": target_workspace_id,
        "targetProfileId": target_profile_id,
        "recordsFile": RECORDS_NAME,
        "recordCount": len(records_chunks),
        "recordTypeCounts": type_counts,
        "recordsBytes": len(records_raw),
        "recordsSha256": digest(records_raw),
    }
    manifest["manifestHash"] = digest(canonical_json_bytes(manifest))
    manifest_raw = canonical_json_bytes(manifest) + b"\n"
    _atomic_write(output / RECORDS_NAME, records_raw)
    _atomic_write(output / MANIFEST_NAME, manifest_raw)
    return ExportReport(
        output_directory=output,
        export_id=resolved_export_id,
        manifest_hash=manifest["manifestHash"],
        record_count=len(records_chunks),
        record_type_counts=type_counts,
        redacted_values=redacted_count,
    )


def _normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    raw_type = row.get("recordType", row.get("type"))
    if raw_type not in _TYPE_ALIASES:
        raise ExportError("legacy row type is unsupported")
    record_type = _TYPE_ALIASES[raw_type]
    legacy_id = _bounded_text(row.get("legacyId", row.get("id")), "legacyId", 256)
    occurred_at = _bounded_text(
        row.get("occurredAt", row.get("createdAt", row.get("created_at"))),
        "occurredAt",
        40,
    )
    _timestamp(occurred_at, "occurredAt")
    nested = row.get("payload")
    payload = nested if isinstance(nested, Mapping) else row
    if record_type == "conversation":
        normalized = {
            "title": _bounded_text(payload.get("title", "Imported conversation"), "conversation title", 512),
            "archived": _boolean(payload.get("archived", False), "conversation archived"),
        }
    elif record_type == "message":
        role = _bounded_text(payload.get("role", "user"), "message role", 32)
        if role not in {"user", "assistant", "system", "tool"}:
            raise ExportError("message role is unsupported")
        process_status = payload.get("toolProcessStatus", payload.get("tool_process_status"))
        if process_status is None:
            process_status = "legacy_missing" if role == "tool" else "not_applicable"
        if process_status not in {"complete", "legacy_missing", "not_applicable"}:
            process_status = "legacy_missing"
        normalized = {
            "conversationLegacyId": _bounded_text(
                payload.get("conversationLegacyId", payload.get("conversation_id")),
                "message conversationLegacyId",
                256,
            ),
            "role": role,
            "content": _bounded_text(payload.get("content", payload.get("text")), "message content", 1_048_576),
            "toolProcessStatus": process_status,
        }
    elif record_type == "memory":
        scope = _bounded_text(payload.get("scope", "workspace"), "memory scope", 32)
        if scope not in {"session", "workspace", "profile"}:
            raise ExportError("memory scope is unsupported")
        confidence = payload.get("confidence", 0.5)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
            raise ExportError("memory confidence must be between zero and one")
        conversation_id = payload.get("conversationLegacyId", payload.get("conversation_id"))
        if conversation_id is not None:
            conversation_id = _bounded_text(conversation_id, "memory conversationLegacyId", 256)
        if (scope == "session") != (conversation_id is not None):
            raise ExportError("Session Memory requires exactly one conversation reference")
        normalized = {
            "scope": scope,
            "content": _bounded_text(payload.get("content", payload.get("text")), "memory content", 262_144),
            "confidence": float(confidence),
            "conversationLegacyId": conversation_id,
        }
    else:
        conversation_id = payload.get("conversationLegacyId", payload.get("conversation_id"))
        if conversation_id is not None:
            conversation_id = _bounded_text(conversation_id, "VaultAction conversationLegacyId", 256)
        normalized = {
            "conversationLegacyId": conversation_id,
            "action": _bounded_text(payload.get("action", payload.get("name", "legacy_vault_action")), "action", 256),
            "outcome": _bounded_text(payload.get("outcome", payload.get("status", "unknown")), "outcome", 256),
            "summary": _bounded_text(payload.get("summary", "Legacy VaultAction audit"), "summary", 8192),
        }
    return {"recordType": record_type, "legacyId": legacy_id, "occurredAt": occurred_at, "payload": normalized}


def _redact(value: object) -> tuple[object, int]:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        count = 0
        for key, item in value.items():
            rendered_key = str(key)
            if _SENSITIVE_KEY.search(rendered_key):
                result[rendered_key] = "[REDACTED]"
                count += 1
            else:
                result[rendered_key], changed = _redact(item)
                count += changed
        return result, count
    if isinstance(value, (list, tuple)):
        result_list: list[object] = []
        count = 0
        for item in value:
            sanitized, changed = _redact(item)
            result_list.append(sanitized)
            count += changed
        return result_list, count
    if isinstance(value, str):
        redacted_text = value
        count = 0
        for pattern in _SECRET_PATTERNS:
            redacted_text, substitutions = pattern.subn("[REDACTED]", redacted_text)
            count += substitutions
        return redacted_text, count
    return value, 0


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value.encode("utf-8")) > maximum:
        raise ExportError(f"{label} must be bounded non-empty text")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ExportError(f"{label} must be a boolean")
    return value


def _timestamp(value: str, label: str) -> None:
    if _RFC3339.fullmatch(value) is None:
        raise ExportError(f"{label} must be RFC 3339 with an offset")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise ExportError(f"{label} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExportError(f"{label} must include an offset")


def _reject_constant(value: str) -> object:
    raise ExportError(f"non-finite JSON constant {value!r} is forbidden")


def _atomic_write(path: Path, value: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/legacy_exporter",
        description="Export supplied offline legacy rows to a canonical, checksummed migration bundle.",
    )
    parser.add_argument("--input-jsonl", type=Path, required=True, help="offline read-only legacy rows")
    parser.add_argument("--output", type=Path, required=True, help="bundle output directory")
    parser.add_argument("--workspace-id", required=True, help="target ws_ identifier")
    parser.add_argument("--profile-id", required=True, help="target profile_ identifier")
    parser.add_argument("--export-id", help="stable one-shot export identity")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = export_bundle(
        JsonlRowSource(args.input_jsonl),
        args.output,
        target_workspace_id=args.workspace_id,
        target_profile_id=args.profile_id,
        export_id=args.export_id,
    )
    print(
        json.dumps(
            {
                "outputDirectory": str(report.output_directory),
                "exportId": report.export_id,
                "manifestHash": report.manifest_hash,
                "recordCount": report.record_count,
                "recordTypeCounts": report.record_type_counts,
                "redactedValues": report.redacted_values,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "ExportError",
    "ExportReport",
    "JsonlRowSource",
    "LegacyRowSource",
    "build_parser",
    "canonical_json_bytes",
    "export_bundle",
    "main",
]
