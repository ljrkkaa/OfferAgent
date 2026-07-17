"""Stable local workspace identity without storing runtime state in the Vault."""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from offeragent_harness.error_codes import ResourceConflictCause

_REGISTRY_VERSION = 1
_LOCK_RETRY_SECONDS = 0.01
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class CanonicalRootIdentity:
    canonical_path: str
    volume_id: str
    filesystem_id: str
    identity_hash: str


@dataclass(frozen=True)
class WorkspaceInstanceRecord:
    workspace_instance_id: str
    root_identity: CanonicalRootIdentity
    portable_workspace_id: str | None
    created_at: str
    last_seen_at: str


class WorkspaceRegistryCorrupt(RuntimeError):
    pass


class WorkspaceRegistryConflict(RuntimeError, ResourceConflictCause):
    pass


def identify_workspace_root(root: Path) -> CanonicalRootIdentity:
    canonical = root.expanduser().resolve(strict=True)
    if not canonical.is_dir():
        raise ValueError(f"workspace root is not a directory: {canonical}")
    stat = canonical.stat()
    canonical_path = os.path.normcase(os.path.normpath(str(canonical)))
    volume_id = f"{stat.st_dev:x}"
    filesystem_id = f"{stat.st_ino:x}"
    digest = _identity_digest(canonical_path, volume_id, filesystem_id)
    return CanonicalRootIdentity(
        canonical_path=canonical_path,
        volume_id=volume_id,
        filesystem_id=filesystem_id,
        identity_hash=f"sha256:{digest}",
    )


class WorkspaceRegistry:
    """Atomic registry mapping a canonical local Vault to a random instance ID.

    Every read/modify/write cycle is protected by both a process-local lock and
    an operating-system byte-range lock. Concurrent local processes therefore
    cannot lose a workspace registration.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        new_uuid: Callable[[], uuid.UUID] | None = None,
    ) -> None:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if path is None and not local_app_data:
            raise RuntimeError("LOCALAPPDATA is required when no workspace registry path is supplied")
        self._path = path or Path(local_app_data or "") / "OfferAgent" / "workspace-registry.json"
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._new_uuid = new_uuid or uuid.uuid4
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def register(self, root: Path, *, portable_workspace_id: str | None = None) -> WorkspaceInstanceRecord:
        identity = identify_workspace_root(root)
        timestamp = _rfc3339(self._now())
        with self._lock, _registry_file_lock(self._path):
            records = self._read()
            current = records.get(identity.identity_hash)
            if current is None:
                instance_id = f"wsi_{self._new_uuid()}"
                if any(record.workspace_instance_id == instance_id for record in records.values()):
                    raise WorkspaceRegistryConflict(f"generated workspace instance ID already exists: {instance_id}")
                current = WorkspaceInstanceRecord(
                    workspace_instance_id=instance_id,
                    root_identity=identity,
                    portable_workspace_id=portable_workspace_id,
                    created_at=timestamp,
                    last_seen_at=timestamp,
                )
            else:
                current = WorkspaceInstanceRecord(
                    workspace_instance_id=current.workspace_instance_id,
                    root_identity=identity,
                    portable_workspace_id=portable_workspace_id or current.portable_workspace_id,
                    created_at=current.created_at,
                    last_seen_at=timestamp,
                )
            records[identity.identity_hash] = current
            self._write(records)
            return current

    def list(self) -> tuple[WorkspaceInstanceRecord, ...]:
        with self._lock, _registry_file_lock(self._path):
            return tuple(sorted(self._read().values(), key=lambda item: item.workspace_instance_id))

    def lookup(self, root: Path) -> WorkspaceInstanceRecord | None:
        identity = identify_workspace_root(root)
        with self._lock, _registry_file_lock(self._path):
            return self._read().get(identity.identity_hash)

    def _read(self) -> dict[str, WorkspaceInstanceRecord]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != _REGISTRY_VERSION:
                raise ValueError("unsupported or missing registry version")
            raw_records = payload.get("workspaces")
            if not isinstance(raw_records, list):
                raise ValueError("workspaces must be a list")
            records: dict[str, WorkspaceInstanceRecord] = {}
            instance_ids: set[str] = set()
            for value in raw_records:
                record = _parse_record(value)
                key = record.root_identity.identity_hash
                if key in records:
                    raise ValueError(f"duplicate root identity {key}")
                if record.workspace_instance_id in instance_ids:
                    raise ValueError(f"duplicate workspace instance ID {record.workspace_instance_id}")
                records[key] = record
                instance_ids.add(record.workspace_instance_id)
            return records
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise WorkspaceRegistryCorrupt(
                f"workspace registry is invalid and was not overwritten: {self._path}"
            ) from error

    def _write(self, records: dict[str, WorkspaceInstanceRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
        payload = {
            "version": _REGISTRY_VERSION,
            "workspaces": [_record_to_json(records[key]) for key in sorted(records)],
        }
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _parse_record(value: Any) -> WorkspaceInstanceRecord:
    if not isinstance(value, dict):
        raise TypeError("workspace record must be an object")
    raw_identity = value["root_identity"]
    if not isinstance(raw_identity, dict):
        raise TypeError("root_identity must be an object")
    identity = CanonicalRootIdentity(
        canonical_path=str(raw_identity["canonical_path"]),
        volume_id=str(raw_identity["volume_id"]),
        filesystem_id=str(raw_identity["filesystem_id"]),
        identity_hash=str(raw_identity["identity_hash"]),
    )
    expected_hash = f"sha256:{_identity_digest(identity.canonical_path, identity.volume_id, identity.filesystem_id)}"
    if identity.identity_hash != expected_hash:
        raise ValueError("root identity hash does not match its canonical fields")
    raw_instance_id = value["workspace_instance_id"]
    if not isinstance(raw_instance_id, str) or not raw_instance_id.startswith("wsi_"):
        raise ValueError("workspace_instance_id must use the wsi_<uuid> protocol format")
    parsed_uuid = uuid.UUID(raw_instance_id.removeprefix("wsi_"))
    instance_id = f"wsi_{parsed_uuid}"
    if instance_id != raw_instance_id:
        raise ValueError("workspace_instance_id must contain a canonical lowercase UUID")
    portable = value.get("portable_workspace_id")
    if portable is not None and not isinstance(portable, str):
        raise TypeError("portable_workspace_id must be a string or null")
    created_at = str(value["created_at"])
    last_seen_at = str(value["last_seen_at"])
    _parse_rfc3339(created_at)
    _parse_rfc3339(last_seen_at)
    return WorkspaceInstanceRecord(instance_id, identity, portable, created_at, last_seen_at)


def _record_to_json(record: WorkspaceInstanceRecord) -> dict[str, Any]:
    return {
        "workspace_instance_id": record.workspace_instance_id,
        "root_identity": asdict(record.root_identity),
        "portable_workspace_id": record.portable_workspace_id,
        "created_at": record.created_at,
        "last_seen_at": record.last_seen_at,
    }


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("registry clock must return timezone-aware datetimes")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _identity_digest(canonical_path: str, volume_id: str, filesystem_id: str) -> str:
    return hashlib.sha256(
        f"v1\0{canonical_path}\0{volume_id}\0{filesystem_id}".encode("utf-8", errors="strict")
    ).hexdigest()


def _parse_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _process_lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


@contextmanager
def _registry_file_lock(registry_path: Path) -> Iterator[None]:
    """Hold a crash-safe OS lock for one registry read/modify/write cycle."""

    lock_path = registry_path.with_name(f"{registry_path.name}.lock")
    process_lock = _process_lock_for(lock_path)
    with process_lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        acquired = False
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            _acquire_os_lock(descriptor)
            acquired = True
            yield
        finally:
            try:
                if acquired:
                    _release_os_lock(descriptor)
            finally:
                os.close(descriptor)


def _acquire_os_lock(descriptor: int) -> None:
    if os.name != "nt":
        fcntl = importlib.import_module("fcntl")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return

    msvcrt = importlib.import_module("msvcrt")
    while True:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            # Windows byte-range locks are released automatically when a
            # process exits, so retrying cannot inherit a stale lock file.
            time.sleep(_LOCK_RETRY_SECONDS)


def _release_os_lock(descriptor: int) -> None:
    if os.name != "nt":
        fcntl = importlib.import_module("fcntl")
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    msvcrt = importlib.import_module("msvcrt")
    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


__all__ = [
    "CanonicalRootIdentity",
    "WorkspaceInstanceRecord",
    "WorkspaceRegistry",
    "WorkspaceRegistryConflict",
    "WorkspaceRegistryCorrupt",
    "identify_workspace_root",
]
