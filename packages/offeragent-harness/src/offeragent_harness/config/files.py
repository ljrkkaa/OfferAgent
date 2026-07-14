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

from .models import ConfigLayer, ConfigPatch, ConfigScope

_FILE_VERSION = 3
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
                if version == 1:
                    return self._migrate_v1(raw)
                if version == 2:
                    return self._migrate_v2(raw)
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
        return ConfigLayer(self.scope, self.owner_id, revision, ConfigPatch.model_validate(raw["config"]))

    def _migrate_v1(self, raw: Mapping[str, Any]) -> ConfigFileLoad:
        if set(raw) != {"version", "revision", "settings"}:
            raise ValueError("v1 config fields are incompatible")
        revision = raw["revision"]
        if type(revision) is not int or revision < 0:
            raise ValueError("v1 config revision is invalid")
        patch = ConfigPatch.model_validate(raw["settings"])
        _reject_unsafe_legacy_local(patch)
        layer = ConfigLayer(self.scope, self.owner_id, revision, patch)
        backup = self.path.with_name(f"{self.path.name}.v1.{_stamp(self._now())}.bak")
        shutil.copy2(self.path, backup)
        try:
            _atomic_write(self.path, _encode(layer))
        except BaseException as error:
            os.replace(backup, self.path)
            raise ConfigFileError("configuration migration failed and was rolled back") from error
        return ConfigFileLoad(layer, False, backup, migrated_from=1)

    def _migrate_v2(self, raw: Mapping[str, Any]) -> ConfigFileLoad:
        if set(raw) != {"version", "scope", "ownerId", "revision", "config"}:
            raise ValueError("v2 config fields are incompatible")
        if raw["scope"] != self.scope.value or raw["ownerId"] != self.owner_id:
            raise ValueError("v2 config scope or owner mismatch")
        revision = raw["revision"]
        if type(revision) is not int or revision < 0:
            raise ValueError("v2 config revision is invalid")
        patch = ConfigPatch.model_validate(raw["config"])
        _reject_unsafe_legacy_local(patch)
        layer = ConfigLayer(self.scope, self.owner_id, revision, patch)
        backup = self.path.with_name(f"{self.path.name}.v2.{_stamp(self._now())}.bak")
        shutil.copy2(self.path, backup)
        try:
            _atomic_write(self.path, _encode(layer))
        except BaseException as error:
            os.replace(backup, self.path)
            raise ConfigFileError("configuration migration failed and was rolled back") from error
        return ConfigFileLoad(layer, False, backup, migrated_from=2)

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


def _reject_unsafe_legacy_local(patch: ConfigPatch) -> None:
    model = patch.model
    if model is not None and model.provider is not None and model.provider.value == "local" and not model.base_url:
        raise ValueError("legacy local provider omitted its required base_url")


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
