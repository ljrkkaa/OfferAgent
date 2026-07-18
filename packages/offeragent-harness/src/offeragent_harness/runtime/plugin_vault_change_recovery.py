"""Read-only recovery lookup for plugin-owned Interview Submission journals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from offeragent_harness.tools import (
    ExecutorLocation,
    SideEffect,
    SideEffectKind,
    SideEffectState,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
)

_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECKPOINT = re.compile(r"^refs/offeragent/checkpoints/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MAX_RECORD_BYTES = 256 * 1024
_MAX_MARKER_BYTES = 256 * 1024
_MAX_TARGETS = 20
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_RECORD_KEYS = {
    "version",
    "batchId",
    "toolCallId",
    "workspaceId",
    "runId",
    "rootRunId",
    "changeKind",
    "reviewHash",
    "argsHash",
    "idempotencyKey",
    "state",
    "checkpointRef",
    "targets",
    "appliedPaths",
    "manualReviewPaths",
}
_TARGET_KEYS = {
    "operation",
    "path",
    "beforeHash",
    "afterHash",
    "beforeModifiedVersion",
    "afterModifiedVersion",
}
_STATES = {
    "prepared",
    "applying",
    "applied",
    "undoing",
    "rolled_back",
    "rejected",
    "undone",
    "recovery_failed",
}
_MANUAL_STATES = {"prepared", "applying", "undoing", "undone", "recovery_failed"}


class PluginVaultChangeRecoveryError(RuntimeError):
    """The plugin recovery evidence is unavailable, malformed, or conflicts."""


class PluginVaultChangeRecoveryLookup:
    """Reconstruct definite plugin results without executing or mutating a Vault Tool."""

    def __init__(self, vault_root: Path, journal_directory: Path, recovery_token: str) -> None:
        if _TOKEN.fullmatch(recovery_token) is None:
            raise ValueError("plugin recovery token is invalid")
        try:
            root = vault_root.expanduser().resolve(strict=True)
        except OSError as error:
            raise ValueError("Vault root is unavailable") from error
        if not root.is_dir() or _unsafe_file_type(root):
            raise ValueError("Vault root must be a real directory")
        directory = journal_directory.expanduser().resolve(strict=False)
        try:
            relative = directory.relative_to(root)
        except ValueError as error:
            raise ValueError("plugin journal directory escapes the Vault") from error
        parts = relative.parts
        if len(parts) < 3 or parts[-2:] != ("offeragent", "vault-change-journal") or not parts[0].startswith("."):
            raise ValueError("plugin journal directory is outside the plugin configuration tree")
        self._vault_root = root
        self._journal_directory = directory
        self._recovery_token = recovery_token

    async def lookup_result(self, definition: ToolDefinition, call: ToolCall) -> ToolResult | None:
        if not _is_interview_apply(definition, call):
            return None
        batch_id = call.arguments.get("batchId")
        if not isinstance(batch_id, str) or _BATCH_ID.fullmatch(batch_id) is None:
            raise PluginVaultChangeRecoveryError("plugin journal batch binding is invalid")
        return await asyncio.to_thread(self._lookup, call, batch_id)

    def _lookup(self, call: ToolCall, batch_id: str) -> ToolResult | None:
        self._validate_current_recovery_marker()
        path = self._journal_directory / f"{batch_id}.json"
        seal_path = self._journal_directory / ".recovery-seals" / "current" / _recovery_seal_name(batch_id)
        record_exists = _entry_exists(path, "Vault Change journal")
        seal_exists = _entry_exists(seal_path, "plugin recovery seal")
        if not record_exists:
            if seal_exists:
                raise PluginVaultChangeRecoveryError("sealed record is unavailable after plugin recovery")
            return _not_applied(call, batch_id, "The plugin Journal confirms that this batch was not applied.")
        if not seal_exists:
            raise PluginVaultChangeRecoveryError("unsealed record appeared after plugin recovery")
        seal = _load_object(seal_path, "plugin recovery seal", _MAX_MARKER_BYTES)
        if (
            set(seal) != {"schemaVersion", "recoveryToken", "batchId", "contentHash", "byteLength"}
            or seal.get("schemaVersion") != 1
            or seal.get("recoveryToken") != self._recovery_token
            or seal.get("batchId") != batch_id
            or not isinstance(seal.get("contentHash"), str)
            or _DIGEST.fullmatch(seal["contentHash"]) is None
            or not isinstance(seal.get("byteLength"), int)
            or isinstance(seal.get("byteLength"), bool)
            or not 2 <= seal["byteLength"] <= _MAX_RECORD_BYTES
        ):
            raise PluginVaultChangeRecoveryError("plugin recovery seal is malformed")
        raw = _read_stable_file(path, "Vault Change journal", _MAX_RECORD_BYTES)
        if len(raw) != seal["byteLength"] or f"sha256:{hashlib.sha256(raw).hexdigest()}" != seal["contentHash"]:
            raise PluginVaultChangeRecoveryError("Vault Change journal changed after plugin recovery")
        record = _decode_object(raw, "Vault Change journal")
        targets, state = _validate_record(record, call, batch_id)
        if state in _MANUAL_STATES:
            return None
        if state == "applied":
            return _applied(call, record, targets)
        if state == "rejected":
            return _rejected(call, batch_id)
        if state == "rolled_back":
            return _not_applied(call, batch_id, "The plugin Journal confirms that this batch was rolled back.")
        raise PluginVaultChangeRecoveryError("Vault Change journal state is malformed")

    def _validate_current_recovery_marker(self) -> None:
        _validate_real_ancestry(self._vault_root, self._journal_directory)
        marker = _load_object(
            self._journal_directory / ".recovery-ready.json",
            "plugin recovery marker",
            _MAX_MARKER_BYTES,
        )
        if set(marker) != {"schemaVersion", "recoveryToken"} or marker.get("schemaVersion") != 2:
            raise PluginVaultChangeRecoveryError("plugin recovery marker is malformed")
        if marker.get("recoveryToken") != self._recovery_token:
            raise PluginVaultChangeRecoveryError("plugin recovery token does not match this Worker launch")
        seal_directory = self._journal_directory / ".recovery-seals" / "current"
        _validate_real_ancestry(self._vault_root, seal_directory)


def _entry_exists(path: Path, label: str) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise PluginVaultChangeRecoveryError(f"{label} is unavailable") from error
    return True


def _recovery_seal_name(batch_id: str) -> str:
    return f"{hashlib.sha256(batch_id.encode('utf-8')).hexdigest()}.json"


def _is_interview_apply(definition: ToolDefinition, call: ToolCall) -> bool:
    return (
        definition.name == "vault.changes.apply"
        and definition.version == "1"
        and definition.executor_location is ExecutorLocation.PLUGIN
        and call.name == definition.name
        and call.version == definition.version
        and call.definition_fingerprint == definition.fingerprint
        and call.arguments.get("changeKind") == "interview_submission"
    )


def _load_object(path: Path, label: str, maximum_bytes: int) -> dict[str, Any]:
    try:
        raw = _read_stable_file(path, label, maximum_bytes)
        return _decode_object(raw, label)
    except PluginVaultChangeRecoveryError:
        raise
    except OSError as error:
        raise PluginVaultChangeRecoveryError(f"{label} is malformed") from error


def _decode_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_unique_object)
    except PluginVaultChangeRecoveryError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise PluginVaultChangeRecoveryError(f"{label} is malformed") from error
    if not isinstance(value, dict):
        raise PluginVaultChangeRecoveryError(f"{label} is malformed")
    return value


def _read_stable_file(path: Path, label: str, maximum_bytes: int) -> bytes:
    try:
        before = path.lstat()
        _require_regular_file(before, label, maximum_bytes)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            _require_regular_file(opened, label, maximum_bytes)
            if _file_identity(before) != _file_identity(opened):
                raise PluginVaultChangeRecoveryError(f"{label} changed before it was opened")
            chunks: list[bytes] = []
            length = 0
            while length <= maximum_bytes:
                chunk = os.read(descriptor, min(16_384, maximum_bytes + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
            after_read = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = path.lstat()
        _require_regular_file(current, label, maximum_bytes)
    except PluginVaultChangeRecoveryError:
        raise
    except OSError as error:
        raise PluginVaultChangeRecoveryError(f"{label} is unavailable") from error
    if length > maximum_bytes:
        raise PluginVaultChangeRecoveryError(f"{label} size is malformed")
    if (
        _file_snapshot(before) != _file_snapshot(opened)
        or _file_snapshot(opened) != _file_snapshot(after_read)
        or _file_snapshot(after_read) != _file_snapshot(current)
    ):
        raise PluginVaultChangeRecoveryError(f"{label} changed while reading")
    return b"".join(chunks)


def _require_regular_file(info: os.stat_result, label: str, maximum_bytes: int) -> None:
    if not stat.S_ISREG(info.st_mode) or _unsafe_stat(info):
        raise PluginVaultChangeRecoveryError(f"{label} is not a real file")
    if info.st_size < 2 or info.st_size > maximum_bytes:
        raise PluginVaultChangeRecoveryError(f"{label} size is malformed")


def _file_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _file_snapshot(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        int(getattr(info, "st_file_attributes", 0)),
    )


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _validate_record(
    record: Mapping[str, Any],
    call: ToolCall,
    batch_id: str,
) -> tuple[tuple[Mapping[str, Any], ...], str]:
    if set(record) != _RECORD_KEYS or record.get("version") != 2:
        raise PluginVaultChangeRecoveryError("Vault Change journal is malformed")
    binding = (
        record.get("batchId"),
        record.get("toolCallId"),
        record.get("workspaceId"),
        record.get("runId"),
        record.get("rootRunId"),
        record.get("changeKind"),
        record.get("argsHash"),
        record.get("idempotencyKey"),
    )
    expected = (
        batch_id,
        call.tool_call_id,
        call.workspace_id,
        call.run_id,
        call.lineage.root_run_id,
        "interview_submission",
        call.args_hash,
        call.idempotency_key,
    )
    if binding != expected:
        raise PluginVaultChangeRecoveryError("Vault Change journal binding conflicts with the original ToolCall")
    review_hash = record.get("reviewHash")
    state = record.get("state")
    if not isinstance(review_hash, str) or _DIGEST.fullmatch(review_hash) is None or state not in _STATES:
        raise PluginVaultChangeRecoveryError("Vault Change journal is malformed")
    checkpoint = record.get("checkpointRef")
    if checkpoint is not None and (not isinstance(checkpoint, str) or _CHECKPOINT.fullmatch(checkpoint) is None):
        raise PluginVaultChangeRecoveryError("Vault Change journal is malformed")
    raw_targets = record.get("targets")
    if not isinstance(raw_targets, list) or not 1 <= len(raw_targets) <= _MAX_TARGETS:
        raise PluginVaultChangeRecoveryError("Vault Change journal is malformed")
    targets = tuple(_validate_target(item) for item in raw_targets)
    _validate_target_binding(targets, call)
    paths = tuple(str(item["path"]) for item in targets)
    if len({path.lower() for path in paths}) != len(paths):
        raise PluginVaultChangeRecoveryError("Vault Change journal is malformed")
    applied_paths = _journal_paths(record.get("appliedPaths"), paths)
    _journal_paths(record.get("manualReviewPaths"), paths)
    if state in {"applying", "applied", "undoing", "undone"} and checkpoint is None:
        raise PluginVaultChangeRecoveryError("Vault Change journal is malformed")
    if state == "applied" and (
        applied_paths != paths
        or checkpoint is None
        or any(item["afterModifiedVersion"] is None for item in targets)
    ):
        raise PluginVaultChangeRecoveryError("Vault Change journal applied state is malformed")
    return targets, str(state)


def _validate_target_binding(targets: tuple[Mapping[str, Any], ...], call: ToolCall) -> None:
    operations = call.arguments.get("operations")
    if (
        not isinstance(operations, Sequence)
        or isinstance(operations, (str, bytes, bytearray))
        or len(operations) != len(targets)
    ):
        raise PluginVaultChangeRecoveryError("Vault Change journal target binding conflicts with the original ToolCall")
    for operation, target in zip(operations, targets, strict=True):
        if not isinstance(operation, Mapping) or (
            operation.get("op"),
            operation.get("path"),
            operation.get("expectedContentHash"),
            operation.get("expectedModifiedVersion"),
        ) != (
            target["operation"],
            target["path"],
            target["beforeHash"],
            target["beforeModifiedVersion"],
        ):
            raise PluginVaultChangeRecoveryError(
                "Vault Change journal target binding conflicts with the original ToolCall"
            )
        if operation.get("op") == "create":
            content = operation.get("content")
            expected_after_hash = (
                f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"
                if isinstance(content, str)
                else None
            )
            if target["afterHash"] != expected_after_hash:
                raise PluginVaultChangeRecoveryError(
                    "Vault Change journal target binding conflicts with the original ToolCall"
                )


def _validate_target(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != _TARGET_KEYS:
        raise PluginVaultChangeRecoveryError("Vault Change journal target is malformed")
    operation = value.get("operation")
    path = value.get("path")
    before_hash = value.get("beforeHash")
    after_hash = value.get("afterHash")
    before_version = value.get("beforeModifiedVersion")
    after_version = value.get("afterModifiedVersion")
    if operation not in {"create", "append", "replace", "patch", "delete"} or not _safe_vault_path(path):
        raise PluginVaultChangeRecoveryError("Vault Change journal target is malformed")
    if not _state_identity(before_hash) or not _state_identity(after_hash):
        raise PluginVaultChangeRecoveryError("Vault Change journal target is malformed")
    if not _modified_version(before_version) or (after_version is not None and not _modified_version(after_version)):
        raise PluginVaultChangeRecoveryError("Vault Change journal target is malformed")
    if operation == "create" and (before_hash != "absent" or after_hash == "absent" or before_version != "missing"):
        raise PluginVaultChangeRecoveryError("Vault Change journal target is malformed")
    if operation == "delete" and (before_hash == "absent" or after_hash != "absent"):
        raise PluginVaultChangeRecoveryError("Vault Change journal target is malformed")
    if operation not in {"create", "delete"} and (before_hash == "absent" or after_hash == "absent"):
        raise PluginVaultChangeRecoveryError("Vault Change journal target is malformed")
    return value


def _journal_paths(value: Any, targets: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not _safe_vault_path(item) for item in value):
        raise PluginVaultChangeRecoveryError("Vault Change journal paths are malformed")
    paths = tuple(value)
    if len({path.lower() for path in paths}) != len(paths) or any(path not in targets for path in paths):
        raise PluginVaultChangeRecoveryError("Vault Change journal paths are malformed")
    return paths


def _safe_vault_path(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(item in value for item in "\\:\x00")
    ):
        return False
    path = PurePosixPath(value)
    parts = path.parts
    if path.is_absolute() or str(path) != value or any(part in {"", ".", ".."} for part in parts):
        return False
    lowered_parts = tuple(part.lower() for part in parts)
    if any(part in {".git", "node_modules"} for part in lowered_parts):
        return False
    lowered = value.lower()
    if lowered.startswith(".obsidian/plugins/offeragent"):
        return False
    if any(part.startswith(".") for part in parts) and not lowered.startswith((".codex/", ".obsidian/")):
        return False
    suffix = path.suffix.lower()
    return suffix in {".md", ".txt"} or (
        lowered.startswith(".obsidian/") and suffix in {".json", ".css"}
    )


def _state_identity(value: Any) -> bool:
    return value == "absent" or (isinstance(value, str) and _DIGEST.fullmatch(value) is not None)


def _modified_version(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and not any(item in value for item in "\x00\r\n")


def _applied(
    call: ToolCall,
    record: Mapping[str, Any],
    targets: tuple[Mapping[str, Any], ...],
) -> ToolResult:
    paths = [str(item["path"]) for item in targets]
    side_effects = tuple(
        SideEffect(
            kind=SideEffectKind.FILE_TRASH if item["operation"] == "delete" else SideEffectKind.FILE_WRITE,
            state=SideEffectState.COMMITTED,
            resource_id=str(item["path"]),
            before_state=None if item["beforeHash"] == "absent" else {"contentHash": item["beforeHash"]},
            after_state=None if item["afterHash"] == "absent" else {"contentHash": item["afterHash"]},
            metadata={
                "protocolKind": (
                    "file_created"
                    if item["operation"] == "create"
                    else "file_trashed"
                    if item["operation"] == "delete"
                    else "file_modified"
                ),
                "reconciledFrom": "plugin_vault_change_journal",
            },
        )
        for item in targets
    )
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data={
            "batchId": record["batchId"],
            "state": "applied",
            "checkpointRef": record["checkpointRef"],
            "paths": paths,
            "beforeStateHash": _state_hash(targets, "beforeHash"),
            "afterStateHash": _state_hash(targets, "afterHash"),
            "undoAvailable": True,
        },
        user_visible_summary=f"Reconciled applied Vault Change Batch '{record['batchId']}'.",
        artifact_ids=(),
        source_refs=(),
        side_effects=side_effects,
        retryable=False,
        before_state=None,
        after_state=None,
        error=None,
    )


def _state_hash(targets: tuple[Mapping[str, Any], ...], key: str) -> str:
    content = "\n".join(f"{item['path']}\0{item[key]}" for item in sorted(targets, key=lambda item: str(item["path"])))
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _not_applied(call: ToolCall, batch_id: str, summary: str) -> ToolResult:
    return _failure(call, batch_id, ToolResultStatus.CONFLICTED, "resource.conflict", summary)


def _rejected(call: ToolCall, batch_id: str) -> ToolResult:
    return _failure(
        call,
        batch_id,
        ToolResultStatus.DENIED,
        "policy.denied",
        "The plugin Journal confirms that this batch was rejected.",
    )


def _failure(
    call: ToolCall,
    batch_id: str,
    status: ToolResultStatus,
    code: str,
    summary: str,
) -> ToolResult:
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=status,
        data={"batchId": batch_id, "state": "not_applied"},
        user_visible_summary=summary,
        artifact_ids=(),
        source_refs=(),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state=None,
        error=ToolError(code=code, message=summary, retryable=False, cancelled=False),
    )


def _validate_real_ancestry(root: Path, directory: Path) -> None:
    current = root
    for part in directory.relative_to(root).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise PluginVaultChangeRecoveryError("plugin recovery directory is unavailable") from error
        if not stat.S_ISDIR(info.st_mode) or _unsafe_stat(info):
            raise PluginVaultChangeRecoveryError("plugin recovery directory ancestry is unsafe")


def _unsafe_file_type(path: Path) -> bool:
    return _unsafe_stat(path.lstat())


def _unsafe_stat(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


__all__ = ["PluginVaultChangeRecoveryError", "PluginVaultChangeRecoveryLookup"]
