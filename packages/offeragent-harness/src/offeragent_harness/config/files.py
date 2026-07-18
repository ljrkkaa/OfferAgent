"""Crash-safe config bootstrap files with migration and corruption isolation."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .migrations import (
    project_legacy_codex_config_with_report,
    project_previous_codex_config_with_report,
    validate_current_codex_config,
)
from .models import ConfigLayer, ConfigPatch, ConfigScope

_FILE_VERSION = 6
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class ConfigFileError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ConfigFileLoad:
    layer: ConfigLayer
    safe_mode: bool
    backup_path: Path | None
    migrated_from: int | None = None
    retired_fields: tuple[str, ...] = ()
    retired_provider_ids: tuple[str, ...] = ()


class ConfigFileStore:
    def __init__(
        self,
        path: Path,
        *,
        scope: ConfigScope,
        owner_id: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if scope not in {ConfigScope.MANAGED, ConfigScope.USER, ConfigScope.WORKSPACE}:
            raise ValueError("bootstrap files only support managed, user, and workspace scopes")
        if not path.is_absolute():
            raise ValueError("configuration path must be absolute")
        self.path = path
        self.scope = scope
        self.owner_id = owner_id
        self._now = now or (lambda: datetime.now(timezone.utc))

    def load(self) -> ConfigFileLoad:
        with _file_lock(self.path):
            if not self.path.exists():
                return ConfigFileLoad(ConfigLayer(self.scope, self.owner_id, 0, ConfigPatch()), False, None)
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, Mapping):
                    raise ValueError("config file must be an object")
                version = raw.get("version")
                if type(version) is int and 1 <= version <= 5:
                    return self._migrate_versioned(raw, version=version)
                if version != _FILE_VERSION:
                    raise ValueError("unsupported config file version")
                return ConfigFileLoad(self._decode_current(raw), False, None)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                backup = self._isolate_corrupt()
                safe = ConfigLayer(self.scope, self.owner_id, 0, ConfigPatch())
                return ConfigFileLoad(safe, True, backup)

    def _decode_current(self, raw: Mapping[str, Any]) -> ConfigLayer:
        if set(raw) != {"version", "scope", "ownerId", "revision", "config"}:
            raise ValueError("config file fields are incompatible")
        if raw["scope"] != self.scope.value or raw["ownerId"] != self.owner_id:
            raise ValueError("config file scope or owner mismatch")
        revision = raw["revision"]
        if type(revision) is not int or revision < 0:
            raise ValueError("config file revision is invalid")
        return ConfigLayer(self.scope, self.owner_id, revision, validate_current_codex_config(raw["config"]))

    def _migrate_versioned(self, raw: Mapping[str, Any], *, version: int) -> ConfigFileLoad:
        legacy_v1 = version == 1
        expected = (
            {"version", "revision", "settings"}
            if legacy_v1
            else {
                "version",
                "scope",
                "ownerId",
                "revision",
                "config",
            }
        )
        if set(raw) != expected:
            raise ValueError(f"v{version} config fields are incompatible")
        if not legacy_v1 and (raw["scope"] != self.scope.value or raw["ownerId"] != self.owner_id):
            raise ValueError(f"v{version} config scope or owner mismatch")
        revision = raw["revision"]
        if type(revision) is not int or revision < 0:
            raise ValueError(f"v{version} config revision is invalid")
        payload = raw["settings"] if legacy_v1 else raw["config"]
        projection = (
            project_previous_codex_config_with_report(payload)
            if version == 5
            else project_legacy_codex_config_with_report(payload)
        )
        layer = ConfigLayer(self.scope, self.owner_id, revision, projection.patch)
        backup = self.path.with_name(f"{self.path.name}.v{version}.{_stamp(self._now())}.bak")
        shutil.copy2(self.path, backup)
        try:
            _atomic_write(self.path, _encode(layer))
        except BaseException as error:
            os.replace(backup, self.path)
            raise ConfigFileError("configuration migration failed and was rolled back") from error
        return ConfigFileLoad(
            layer,
            False,
            backup,
            migrated_from=version,
            retired_fields=projection.retired_fields,
            retired_provider_ids=projection.retired_provider_ids,
        )

    def _isolate_corrupt(self) -> Path:
        backup = self.path.with_name(f"{self.path.name}.corrupt.{_stamp(self._now())}.bak")
        os.replace(self.path, backup)
        return backup


def _encode(layer: ConfigLayer) -> bytes:
    payload = {
        "version": _FILE_VERSION,
        "scope": layer.scope.value,
        "ownerId": layer.owner_id,
        "revision": layer.revision,
        "config": layer.patch.payload(),
    }
    return (json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("config clock must return timezone-aware values")
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        lock_path = path.with_name(f"{path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl = importlib.import_module("fcntl")
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


__all__ = ["ConfigFileError", "ConfigFileLoad", "ConfigFileStore"]
