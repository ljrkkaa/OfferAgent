"""Current-user local JSONL logger with bounded retention and no raw content API."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from offeragent_harness.models.json_types import JsonValue

from .models import DataClass, LogField, LogLevel, TraceCorrelation
from .redaction import RedactionPolicy, sanitize_fields

_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class RecentError:
    timestamp: datetime
    event: str
    message: str
    correlation: TraceCorrelation
    fields: Mapping[str, JsonValue]


class LocalJsonLogger:
    def __init__(
        self,
        directory: Path,
        *,
        workspace_instance_id: str,
        max_file_bytes: int = 8 * 1024 * 1024,
        max_files: int = 7,
        retention: timedelta = timedelta(days=14),
        max_recent_errors: int = 1000,
        allowed_root: Path | None = None,
    ) -> None:
        self._directory = directory.resolve()
        if allowed_root is not None and not self._directory.is_relative_to(allowed_root.resolve()):
            raise ValueError("logger directory escapes its allowed Runtime state root")
        if not workspace_instance_id or any(item in workspace_instance_id for item in ("/", "\\", "\0")):
            raise ValueError("invalid logger Workspace instance identity")
        if max_file_bytes < 64 * 1024 or max_files < 1 or retention <= timedelta(0) or max_recent_errors < 1:
            raise ValueError("logger retention bounds are invalid")
        self._workspace_instance_id = workspace_instance_id
        self._max_file_bytes = max_file_bytes
        self._max_files = max_files
        self._retention = retention
        self._recent_errors: deque[RecentError] = deque(maxlen=max_recent_errors)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._directory / f"{self._workspace_instance_id}.jsonl"

    async def emit(
        self,
        level: LogLevel,
        event: str,
        message: str,
        correlation: TraceCorrelation,
        fields: Mapping[str, LogField] | None = None,
        *,
        occurred_at: datetime | None = None,
    ) -> None:
        if _EVENT_NAME.fullmatch(event) is None:
            raise ValueError("structured log event name is invalid")
        if not message or len(message) > 4096:
            raise ValueError("structured log message is empty or too large")
        timestamp = occurred_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("structured log timestamp must be timezone-aware")
        sanitized = sanitize_fields(fields or {}, RedactionPolicy(include_paths=False))
        safe_message = sanitize_fields({"message": LogField(message, DataClass.PUBLIC)})["message"]
        assert isinstance(safe_message, str)
        record: dict[str, JsonValue] = {
            "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
            "level": level.value,
            "event": event,
            "message": safe_message,
            **correlation.to_wire(),
            "fields": sanitized,
        }
        payload = (
            json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        if len(payload) > 64 * 1024:
            raise ValueError("structured log record exceeds 64 KiB")
        async with self._lock:
            await asyncio.to_thread(self._append_sync, payload)
            if level in {LogLevel.ERROR, LogLevel.CRITICAL}:
                self._recent_errors.append(RecentError(timestamp, event, safe_message, correlation, sanitized))

    async def recent_errors(self, *, limit: int = 1000) -> tuple[RecentError, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("recent error limit must be 1..1000")
        async with self._lock:
            return tuple(list(self._recent_errors)[-limit:])

    async def sanitized_log_tail(self, *, max_bytes: int = 1_048_576) -> bytes:
        if not 1 <= max_bytes <= 16 * 1024 * 1024:
            raise ValueError("log tail bound is invalid")
        async with self._lock:
            return await asyncio.to_thread(self._tail_sync, max_bytes)

    def _append_sync(self, payload: bytes) -> None:
        self._prepare_directory()
        self._prune_sync(datetime.now(timezone.utc))
        if self.path.exists() and self.path.stat().st_size + len(payload) > self._max_file_bytes:
            self._rotate_sync()
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(self.path, flags, 0o600)
        try:
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written < 1:
                    raise OSError("structured log append made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._protect_path(self.path, directory=False)

    def _prepare_directory(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._directory.is_symlink() or (self.path.exists() and self.path.is_symlink()):
            raise RuntimeError("log path cannot be a symlink")
        self._protect_path(self._directory, directory=True)

    def _rotate_sync(self) -> None:
        oldest = self.path.with_suffix(f".jsonl.{self._max_files}")
        oldest.unlink(missing_ok=True)
        for index in range(self._max_files - 1, 0, -1):
            source = self.path.with_suffix(f".jsonl.{index}")
            if source.exists():
                os.replace(source, self.path.with_suffix(f".jsonl.{index + 1}"))
        if self.path.exists():
            os.replace(self.path, self.path.with_suffix(".jsonl.1"))

    def _prune_sync(self, now: datetime) -> None:
        cutoff = now.timestamp() - self._retention.total_seconds()
        for candidate in self._directory.glob(f"{self._workspace_instance_id}.jsonl.*"):
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink(missing_ok=True)
            except OSError:
                continue

    def _tail_sync(self, max_bytes: int) -> bytes:
        try:
            with self.path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - max_bytes))
                data = stream.read(max_bytes)
        except FileNotFoundError:
            return b""
        if size > max_bytes:
            newline = data.find(b"\n")
            data = data[newline + 1 :] if newline >= 0 else b""
        return data

    @staticmethod
    def _protect_path(path: Path, *, directory: bool) -> None:
        if os.name == "nt":
            from offeragent_harness.runtime.windows_security import protect_current_user_path

            protect_current_user_path(path, directory=directory)
            return
        path.chmod(0o700 if directory else 0o600)


__all__ = ["LocalJsonLogger", "RecentError"]
