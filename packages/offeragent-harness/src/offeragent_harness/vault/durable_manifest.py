"""Durable, content-free transaction manifests for local Vault CAS recovery.

The manifest lives in the per-workspace runtime state tree, never in the
Vault.  It records identities and hashes only: note contents remain in the
Vault and in the already-approved in-memory plan.  Every update is written to
a canonical, fsync'd pending file before an atomic promotion.  A pending file
left by process termination is itself a complete recovery record and is
promoted on the next open.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import stat
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_MANIFESTS = 256
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^(?:0|[1-9a-f][0-9a-f]*):(?:0|[1-9a-f][0-9a-f]*)$")
_MANIFEST_FILE = re.compile(r"^(sha256_[0-9a-f]{64})\.json$")
_PENDING_FILE = re.compile(r"^(sha256_[0-9a-f]{64})\.pending$")
_OPERATIONS = frozenset({"create", "append", "replace", "patch"})
_EXPECTED_KEYS = frozenset(
    {
        "schemaVersion",
        "manifestId",
        "workspaceId",
        "rootIdentity",
        "journalScope",
        "idempotencyKey",
        "requestHash",
        "toolCallId",
        "runId",
        "rootRunId",
        "definitionFingerprint",
        "planToken",
        "stateHash",
        "operation",
        "path",
        "beforeHash",
        "afterHash",
        "originalIdentity",
        "approvedParents",
        "activeParents",
        "backupPath",
        "temporaryPath",
        "rollbackPath",
        "temporaryIdentity",
        "state",
        "manualReason",
    }
)


class DurableManifestError(RuntimeError):
    """A manifest cannot be trusted or safely transitioned."""


class DurableManifestState(str, Enum):
    PREPARED = "prepared"
    STAGED = "staged"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True, slots=True)
class DurableVaultTransactionManifest:
    manifest_id: str
    workspace_id: str
    root_identity: str
    journal_scope: str
    idempotency_key: str
    request_hash: str
    tool_call_id: str
    run_id: str
    root_run_id: str
    definition_fingerprint: str
    plan_token: str
    state_hash: str
    operation: str
    path: str
    before_hash: str
    after_hash: str
    original_identity: str | None
    approved_parents: Mapping[str, str | None]
    active_parents: Mapping[str, str | None]
    backup_path: str | None
    temporary_path: str
    rollback_path: str
    temporary_identity: str | None
    state: DurableManifestState
    manual_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "approved_parents", MappingProxyType(dict(self.approved_parents)))
        object.__setattr__(self, "active_parents", MappingProxyType(dict(self.active_parents)))
        _validate_manifest(self)

    def transition(
        self,
        state: DurableManifestState,
        *,
        active_parents: Mapping[str, str | None] | None = None,
        temporary_identity: str | None | object = ...,
        manual_reason: str | None = None,
    ) -> DurableVaultTransactionManifest:
        identity = self.temporary_identity if temporary_identity is ... else temporary_identity
        assert identity is None or isinstance(identity, str)
        return replace(
            self,
            state=state,
            active_parents=self.active_parents if active_parents is None else active_parents,
            temporary_identity=identity,
            manual_reason=manual_reason,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "manifestId": self.manifest_id,
            "workspaceId": self.workspace_id,
            "rootIdentity": self.root_identity,
            "journalScope": self.journal_scope,
            "idempotencyKey": self.idempotency_key,
            "requestHash": self.request_hash,
            "toolCallId": self.tool_call_id,
            "runId": self.run_id,
            "rootRunId": self.root_run_id,
            "definitionFingerprint": self.definition_fingerprint,
            "planToken": self.plan_token,
            "stateHash": self.state_hash,
            "operation": self.operation,
            "path": self.path,
            "beforeHash": self.before_hash,
            "afterHash": self.after_hash,
            "originalIdentity": self.original_identity,
            "approvedParents": dict(self.approved_parents),
            "activeParents": dict(self.active_parents),
            "backupPath": self.backup_path,
            "temporaryPath": self.temporary_path,
            "rollbackPath": self.rollback_path,
            "temporaryIdentity": self.temporary_identity,
            "state": self.state.value,
            "manualReason": self.manual_reason,
        }


class DurableVaultManifestStore:
    """Strict bounded manifest storage under one runtime-state directory."""

    def __init__(
        self,
        directory: Path,
        *,
        vault_root: Path,
        workspace_id: str,
        trusted_state_root: Path | None = None,
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id cannot be empty")
        vault = vault_root.resolve(strict=True)
        target = Path(os.path.abspath(os.fspath(directory.expanduser())))
        selected_state_root = directory.parent if trusted_state_root is None else trusted_state_root
        state_root = Path(os.path.abspath(os.fspath(selected_state_root.expanduser())))
        if _path_key(target.parent) != _path_key(state_root):
            raise ValueError("durable manifest directory must be a direct child of the trusted state root")
        if _path_within(target, vault) or _path_within(state_root, vault):
            raise ValueError("durable Vault manifests must live outside the Vault")
        self._directory = target
        self._state_root = state_root
        self._workspace_id = workspace_id
        self._lock = threading.RLock()
        self._ensure_directory()
        self._directory_chain = (
            (self._state_root, _identity(self._state_root.lstat())),
            (self._directory, _identity(self._directory.lstat())),
        )

    @property
    def directory(self) -> Path:
        return self._directory

    def create(self, manifest: DurableVaultTransactionManifest) -> None:
        with self._lock:
            self._verify_directory_chain()
            self._require_workspace(manifest)
            if len(self.list()) >= _MAX_MANIFESTS:
                raise DurableManifestError("too many unresolved durable Vault transactions")
            final = self._path(manifest.manifest_id)
            pending = self._pending_path(manifest.manifest_id)
            if final.exists() or pending.exists():
                raise DurableManifestError("durable Vault transaction manifest already exists")
            self._write_pending(pending, manifest)
            self._promote(pending, final)
            self._verify_directory_chain()

    def save(self, manifest: DurableVaultTransactionManifest) -> None:
        with self._lock:
            self._verify_directory_chain()
            self._require_workspace(manifest)
            final = self._path(manifest.manifest_id)
            pending = self._pending_path(manifest.manifest_id)
            if pending.exists():
                raise DurableManifestError("manifest has an unresolved pending transition")
            current = self._read(final)
            self._validate_transition(current, manifest)
            self._write_pending(pending, manifest)
            self._promote(pending, final)
            self._verify_directory_chain()

    def get(self, manifest_id: str) -> DurableVaultTransactionManifest | None:
        with self._lock:
            self._verify_directory_chain()
            self._reconcile_pending_id(manifest_id)
            path = self._path(manifest_id)
            try:
                result = self._read(path)
            except FileNotFoundError:
                result = None
            self._verify_directory_chain()
            return result

    def list(self) -> tuple[DurableVaultTransactionManifest, ...]:
        with self._lock:
            self._verify_directory_chain()
            entries = tuple(self._directory.iterdir())
            if len(entries) > _MAX_MANIFESTS * 2:
                raise DurableManifestError("durable Vault transaction manifest directory exceeds its bound")
            pending_ids: set[str] = set()
            for path in entries:
                match = _PENDING_FILE.fullmatch(path.name)
                if match is not None:
                    pending_ids.add(match.group(1))
                    continue
                if _MANIFEST_FILE.fullmatch(path.name) is None:
                    raise DurableManifestError(f"unexpected file in durable manifest directory: {path.name}")
            for manifest_id in sorted(pending_ids):
                self._reconcile_pending_id(manifest_id)
            manifests = tuple(
                self._read(path)
                for path in sorted(self._directory.iterdir(), key=lambda item: item.name)
                if _MANIFEST_FILE.fullmatch(path.name) is not None
            )
            if len(manifests) > _MAX_MANIFESTS:
                raise DurableManifestError("too many unresolved durable Vault transactions")
            self._verify_directory_chain()
            return manifests

    def delete(self, manifest_id: str) -> None:
        with self._lock:
            self._verify_directory_chain()
            self._reconcile_pending_id(manifest_id)
            path = self._path(manifest_id)
            current = self._read(path)
            self._require_workspace(current)
            path.unlink()
            _fsync_directory(self._directory)
            self._verify_directory_chain()

    def assert_paths_available(self, paths: Sequence[str]) -> None:
        with self._lock:
            requested = {path.casefold() for path in paths}
            for manifest in self.list():
                if manifest.path.casefold() in requested:
                    raise DurableManifestError(
                        f"Vault path is blocked by unresolved transaction {manifest.manifest_id}: {manifest.path}"
                    )

    def _reconcile_pending_id(self, manifest_id: str) -> None:
        pending = self._pending_path(manifest_id)
        try:
            candidate = self._read(pending)
        except FileNotFoundError:
            return
        if candidate.manifest_id != manifest_id:
            raise DurableManifestError("pending manifest filename and identity differ")
        final = self._path(manifest_id)
        try:
            current = self._read(final)
        except FileNotFoundError:
            current = None
        if current is not None:
            self._validate_transition(current, candidate)
        self._promote(pending, final)

    def _write_pending(self, path: Path, manifest: DurableVaultTransactionManifest) -> None:
        content = _canonical_bytes(manifest)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise

    def _promote(self, pending: Path, final: Path) -> None:
        if os.name == "nt":
            _windows_move_write_through(pending, final)
            return
        os.replace(pending, final)
        _fsync_directory(self._directory)

    def _read(self, path: Path) -> DurableVaultTransactionManifest:
        info = path.lstat()
        _validate_regular_file(info, path.name)
        if info.st_size < 2 or info.st_size > _MAX_MANIFEST_BYTES:
            raise DurableManifestError(f"manifest size is invalid: {path.name}")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            _validate_regular_file(opened, path.name)
            if _identity(info) != _identity(opened):
                raise DurableManifestError(f"manifest changed before open: {path.name}")
            raw = os.read(descriptor, _MAX_MANIFEST_BYTES + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            current = path.lstat()
        except OSError as error:
            raise DurableManifestError(f"manifest path changed while reading: {path.name}") from error
        _validate_regular_file(current, path.name)
        if (
            len(raw) > _MAX_MANIFEST_BYTES
            or _version(opened) != _version(after)
            or _identity(after) != _identity(current)
        ):
            raise DurableManifestError(f"manifest changed while reading: {path.name}")
        manifest = _parse_manifest(raw)
        if _canonical_bytes(manifest) != raw:
            raise DurableManifestError(f"manifest is not canonical: {path.name}")
        expected_name = f"{manifest.manifest_id}.json"
        expected_pending = f"{manifest.manifest_id}.pending"
        if path.name not in {expected_name, expected_pending}:
            raise DurableManifestError("manifest filename and identity differ")
        self._require_workspace(manifest)
        return manifest

    def _path(self, manifest_id: str) -> Path:
        _require_manifest_id(manifest_id)
        return self._directory / f"{manifest_id}.json"

    def _pending_path(self, manifest_id: str) -> Path:
        _require_manifest_id(manifest_id)
        return self._directory / f"{manifest_id}.pending"

    def _require_workspace(self, manifest: DurableVaultTransactionManifest) -> None:
        if manifest.workspace_id != self._workspace_id:
            raise DurableManifestError("manifest belongs to another workspace")

    def _ensure_directory(self) -> None:
        root_info = self._state_root.lstat()
        _validate_directory(root_info, "trusted state root")
        self._directory.mkdir(exist_ok=True, mode=0o700)
        info = self._directory.lstat()
        _validate_directory(info, "durable manifest directory")

    def _verify_directory_chain(self) -> None:
        # The local runtime owns and ACL-hardens ``trusted_state_root``.  The
        # store pins that root plus its one manifest child by identity on every
        # operation; it never follows a tree-internal reparse point.  Recovery
        # paths inside the Vault have an additional WorkspacePathPolicy and
        # handle-bound AtomicVaultCas authorization layer.
        for path, expected in self._directory_chain:
            try:
                info = path.lstat()
            except OSError as error:
                raise DurableManifestError(f"durable manifest directory chain is unavailable: {path.name}") from error
            _validate_directory(info, path.name)
            if _identity(info) != expected:
                raise DurableManifestError(f"durable manifest directory identity changed: {path.name}")

    @staticmethod
    def _validate_transition(
        current: DurableVaultTransactionManifest,
        candidate: DurableVaultTransactionManifest,
    ) -> None:
        if _immutable_binding(current) != _immutable_binding(candidate):
            raise DurableManifestError("durable manifest binding changed")
        allowed = {
            DurableManifestState.PREPARED: {
                DurableManifestState.STAGED,
                DurableManifestState.ROLLED_BACK,
                DurableManifestState.MANUAL_REVIEW,
            },
            DurableManifestState.STAGED: {
                DurableManifestState.COMMITTED,
                DurableManifestState.ROLLED_BACK,
                DurableManifestState.MANUAL_REVIEW,
            },
            DurableManifestState.COMMITTED: {
                DurableManifestState.COMMITTED,
                DurableManifestState.MANUAL_REVIEW,
            },
            DurableManifestState.ROLLED_BACK: {
                DurableManifestState.ROLLED_BACK,
                DurableManifestState.MANUAL_REVIEW,
            },
            DurableManifestState.MANUAL_REVIEW: {DurableManifestState.MANUAL_REVIEW},
        }
        if candidate.state not in allowed[current.state]:
            raise DurableManifestError(
                f"invalid durable manifest transition {current.state.value}->{candidate.state.value}"
            )
        if current.state is not DurableManifestState.PREPARED and (
            current.active_parents != candidate.active_parents
            or current.temporary_identity != candidate.temporary_identity
        ):
            raise DurableManifestError("staged manifest identities are immutable")


def manifest_identity_token(identity: tuple[int, int]) -> str:
    return f"{identity[0]:x}:{identity[1]:x}"


def parse_manifest_identity(value: str) -> tuple[int, int]:
    if _IDENTITY.fullmatch(value) is None:
        raise DurableManifestError("manifest filesystem identity is invalid")
    volume, file_id = value.split(":", 1)
    identity = int(volume, 16), int(file_id, 16)
    if manifest_identity_token(identity) != value:
        raise DurableManifestError("manifest filesystem identity is not canonical")
    return identity


def durable_manifest_id(workspace_id: str, plan_token: str) -> str:
    import hashlib

    digest = hashlib.sha256(f"{workspace_id}:{plan_token}".encode()).hexdigest()
    return f"sha256_{digest}"


def _parse_manifest(raw: bytes) -> DurableVaultTransactionManifest:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, DurableManifestError) as error:
        raise DurableManifestError("durable manifest JSON is invalid") from error
    if not isinstance(value, dict) or frozenset(value) != _EXPECTED_KEYS or value.get("schemaVersion") != 1:
        raise DurableManifestError("durable manifest schema is invalid")
    parents = _parse_parents(value["approvedParents"], "approvedParents")
    active = _parse_parents(value["activeParents"], "activeParents")
    try:
        state = DurableManifestState(_require_string(value["state"], "state", 32))
    except ValueError as error:
        raise DurableManifestError("durable manifest state is invalid") from error
    return DurableVaultTransactionManifest(
        manifest_id=_require_string(value["manifestId"], "manifestId", 80),
        workspace_id=_require_string(value["workspaceId"], "workspaceId", 256),
        root_identity=_require_string(value["rootIdentity"], "rootIdentity", 128),
        journal_scope=_require_string(value["journalScope"], "journalScope", 2048),
        idempotency_key=_require_string(value["idempotencyKey"], "idempotencyKey", 512),
        request_hash=_require_string(value["requestHash"], "requestHash", 80),
        tool_call_id=_require_string(value["toolCallId"], "toolCallId", 256),
        run_id=_require_string(value["runId"], "runId", 256),
        root_run_id=_require_string(value["rootRunId"], "rootRunId", 256),
        definition_fingerprint=_require_string(value["definitionFingerprint"], "definitionFingerprint", 80),
        plan_token=_require_string(value["planToken"], "planToken", 80),
        state_hash=_require_string(value["stateHash"], "stateHash", 80),
        operation=_require_string(value["operation"], "operation", 16),
        path=_require_string(value["path"], "path", 1024),
        before_hash=_require_string(value["beforeHash"], "beforeHash", 80),
        after_hash=_require_string(value["afterHash"], "afterHash", 80),
        original_identity=_optional_string(value["originalIdentity"], "originalIdentity", 128),
        approved_parents=parents,
        active_parents=active,
        backup_path=_optional_string(value["backupPath"], "backupPath", 1024),
        temporary_path=_require_string(value["temporaryPath"], "temporaryPath", 1024),
        rollback_path=_require_string(value["rollbackPath"], "rollbackPath", 1024),
        temporary_identity=_optional_string(value["temporaryIdentity"], "temporaryIdentity", 128),
        state=state,
        manual_reason=_optional_string(value["manualReason"], "manualReason", 1024),
    )


def _validate_manifest(manifest: DurableVaultTransactionManifest) -> None:
    _require_manifest_id(manifest.manifest_id)
    for value, label, maximum in (
        (manifest.workspace_id, "workspace_id", 256),
        (manifest.journal_scope, "journal_scope", 2048),
        (manifest.idempotency_key, "idempotency_key", 512),
        (manifest.tool_call_id, "tool_call_id", 256),
        (manifest.run_id, "run_id", 256),
        (manifest.root_run_id, "root_run_id", 256),
    ):
        _require_bounded_text(value, label, maximum)
    for value, label in (
        (manifest.request_hash, "request_hash"),
        (manifest.definition_fingerprint, "definition_fingerprint"),
        (manifest.plan_token, "plan_token"),
        (manifest.state_hash, "state_hash"),
        (manifest.after_hash, "after_hash"),
    ):
        if _SHA256.fullmatch(value) is None:
            raise DurableManifestError(f"{label} is not a sha256 digest")
    if manifest.manifest_id != durable_manifest_id(manifest.workspace_id, manifest.plan_token):
        raise DurableManifestError("manifest identity is not bound to its workspace and plan")
    expected_scope = ":".join(
        (
            manifest.workspace_id,
            manifest.root_run_id,
            manifest.run_id,
            "vault.transaction",
            "1",
        )
    )
    if manifest.journal_scope != expected_scope:
        raise DurableManifestError("manifest Invocation Journal scope is not identity-bound")
    if manifest.before_hash != "absent" and _SHA256.fullmatch(manifest.before_hash) is None:
        raise DurableManifestError("before_hash is invalid")
    if manifest.after_hash == "absent" or manifest.before_hash == manifest.after_hash:
        raise DurableManifestError("public durable transaction hashes must describe a real file write")
    if manifest.operation not in _OPERATIONS:
        raise DurableManifestError("durable manifest operation is not model-facing")
    _validate_path(manifest.path, hidden=False)
    _validate_path(manifest.temporary_path, hidden=True)
    _validate_path(manifest.rollback_path, hidden=True)
    if manifest.backup_path is not None:
        _validate_path(manifest.backup_path, hidden=True)
    if (manifest.before_hash == "absent") != (manifest.original_identity is None):
        raise DurableManifestError("manifest original identity does not match before-hash presence")
    if (manifest.before_hash == "absent") != (manifest.backup_path is None):
        raise DurableManifestError("manifest backup path does not match before-hash presence")
    parse_manifest_identity(manifest.root_identity)
    if manifest.original_identity is not None:
        parse_manifest_identity(manifest.original_identity)
    if manifest.temporary_identity is not None:
        parse_manifest_identity(manifest.temporary_identity)
    if frozenset(manifest.approved_parents) != frozenset(manifest.active_parents):
        raise DurableManifestError("manifest parent identity key sets differ")
    for mapping in (manifest.approved_parents, manifest.active_parents):
        if len(mapping) > 64:
            raise DurableManifestError("manifest parent identity map exceeds its bound")
        for path, identity in mapping.items():
            _validate_path(path, hidden=True)
            if identity is not None:
                parse_manifest_identity(identity)
    if manifest.state in {DurableManifestState.STAGED, DurableManifestState.COMMITTED}:
        if manifest.temporary_identity is None or any(value is None for value in manifest.active_parents.values()):
            raise DurableManifestError("staged manifest is missing active filesystem identities")
    if manifest.state is DurableManifestState.PREPARED and manifest.temporary_identity is not None:
        raise DurableManifestError("prepared manifest cannot claim a temporary identity")
    if manifest.state is DurableManifestState.MANUAL_REVIEW:
        _require_bounded_text(manifest.manual_reason or "", "manual_reason", 1024)
    elif manifest.manual_reason is not None:
        raise DurableManifestError("non-manual manifest cannot carry a manual reason")
    _validate_internal_names(manifest)


def _validate_internal_names(manifest: DurableVaultTransactionManifest) -> None:
    target = PurePosixPath(manifest.path)
    parent = target.parent
    prefix = manifest.plan_token.removeprefix("sha256:")[:20]
    expected_temporary = parent / f".offeragent-tx-{prefix}-{target.name}.tmp"
    expected_backup = parent / f".offeragent-tx-{prefix}-{target.name}.bak"
    expected_rollback = parent / f".offeragent-rollback-{prefix}-{target.name}"
    if manifest.temporary_path != expected_temporary.as_posix():
        raise DurableManifestError("manifest temporary path is not transaction-bound")
    if manifest.rollback_path != expected_rollback.as_posix():
        raise DurableManifestError("manifest rollback path is not transaction-bound")
    if manifest.backup_path is not None and manifest.backup_path != expected_backup.as_posix():
        raise DurableManifestError("manifest backup path is not transaction-bound")


def _immutable_binding(manifest: DurableVaultTransactionManifest) -> tuple[object, ...]:
    return (
        manifest.manifest_id,
        manifest.workspace_id,
        manifest.root_identity,
        manifest.journal_scope,
        manifest.idempotency_key,
        manifest.request_hash,
        manifest.tool_call_id,
        manifest.run_id,
        manifest.root_run_id,
        manifest.definition_fingerprint,
        manifest.plan_token,
        manifest.state_hash,
        manifest.operation,
        manifest.path,
        manifest.before_hash,
        manifest.after_hash,
        manifest.original_identity,
        tuple(sorted(manifest.approved_parents.items())),
        manifest.backup_path,
        manifest.temporary_path,
        manifest.rollback_path,
    )


def _parse_parents(value: object, label: str) -> Mapping[str, str | None]:
    if not isinstance(value, dict):
        raise DurableManifestError(f"{label} must be an object")
    result: dict[str, str | None] = {}
    for key, raw_identity in value.items():
        if not isinstance(key, str):
            raise DurableManifestError(f"{label} keys must be strings")
        result[key] = _optional_string(raw_identity, f"{label}.{key}", 128)
    return result


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DurableManifestError(f"duplicate manifest JSON key: {key}")
        result[key] = value
    return result


def _canonical_bytes(manifest: DurableVaultTransactionManifest) -> bytes:
    return (json.dumps(manifest.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _require_string(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise DurableManifestError(f"{label} must be a string")
    _require_bounded_text(value, label, maximum)
    return value


def _optional_string(value: object, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _require_string(value, label, maximum)


def _require_bounded_text(value: str, label: str, maximum: int) -> None:
    if not value or len(value) > maximum or any(ord(character) < 0x20 for character in value):
        raise DurableManifestError(f"{label} is empty, oversized, or contains control characters")


def _require_manifest_id(value: str) -> None:
    if re.fullmatch(r"sha256_[0-9a-f]{64}", value) is None:
        raise DurableManifestError("manifest identity is invalid")


def _validate_path(value: str, *, hidden: bool) -> None:
    _require_bounded_text(value, "path", 1024)
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise DurableManifestError("manifest path is not canonical")
    if "\\" in value or "\x00" in value:
        raise DurableManifestError("manifest path contains a forbidden separator")
    if not hidden and any(part.startswith(".") for part in path.parts):
        raise DurableManifestError("manifest target path cannot be hidden")


def _validate_regular_file(info: os.stat_result, label: str) -> None:
    attributes = int(getattr(info, "st_file_attributes", 0))
    if stat.S_ISLNK(info.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise DurableManifestError(f"manifest file is a reparse point: {label}")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DurableManifestError(f"manifest file is not a private regular file: {label}")


def _validate_directory(info: os.stat_result, label: str) -> None:
    attributes = int(getattr(info, "st_file_attributes", 0))
    if stat.S_ISLNK(info.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise DurableManifestError(f"durable state directory is a reparse point: {label}")
    if not stat.S_ISDIR(info.st_mode):
        raise DurableManifestError(f"durable state path is not a directory: {label}")


def _identity(info: os.stat_result) -> tuple[int, int]:
    return int(info.st_dev), int(info.st_ino)


def _version(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns), int(info.st_ctime_ns)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _path_within(path: Path, root: Path) -> bool:
    path_key = _path_key(path)
    root_key = _path_key(root)
    try:
        return os.path.commonpath((path_key, root_key)) == root_key
    except ValueError:
        return False


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _windows_move_write_through(source: Path, destination: Path) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel32.MoveFileExW
    move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    move.restype = ctypes.c_int
    # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH.  Replacing is safe
    # here because the destination is the verified state-store record, never a
    # Vault path.
    if move(str(source), str(destination), 0x1 | 0x8):
        return
    error = ctypes.get_last_error()
    raise DurableManifestError(f"manifest atomic promotion failed (Win32 {error})")


__all__ = [
    "DurableManifestError",
    "DurableManifestState",
    "DurableVaultManifestStore",
    "DurableVaultTransactionManifest",
    "durable_manifest_id",
    "manifest_identity_token",
    "parse_manifest_identity",
]
