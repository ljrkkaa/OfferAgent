"""Local-only diagnostics snapshots and previewable Artifact exports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from offeragent_harness.models.json_types import JsonValue
from offeragent_harness.ports import (
    ArtifactMetadata,
    ArtifactState,
    ArtifactStore,
    CancellationToken,
    Clock,
    IdGenerator,
    Sensitivity,
)

from .logger import LocalJsonLogger
from .metrics import MetricsRegistry
from .models import LogField
from .redaction import RedactionPolicy, sanitize_fields


@dataclass(frozen=True, slots=True)
class DiagnosticProcess:
    role: str
    pid: int
    state: str
    owned: bool

    def __post_init__(self) -> None:
        if (
            self.role not in {"host", "worker", "shell", "parser", "hook"}
            or self.pid < 1
            or self.state
            not in {"starting", "running", "stopping", "stopped", "failed", "restarting", "unresponsive", "orphaned"}
        ):
            raise ValueError("invalid diagnostic process snapshot")


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    generated_at: datetime
    runtime: Mapping[str, JsonValue]
    processes: tuple[DiagnosticProcess, ...]
    recent_errors: tuple[Mapping[str, JsonValue], ...]
    metrics: tuple[Mapping[str, JsonValue], ...]


@dataclass(frozen=True, slots=True)
class DiagnosticBundlePreview:
    files: tuple[str, ...]
    estimated_bytes: int
    contains_paths: bool
    contains_content: bool
    upload_destination: None = None


class RuntimeDiagnosticsProvider(Protocol):
    async def snapshot(self) -> Mapping[str, LogField]: ...


class ProcessDiagnosticsProvider(Protocol):
    async def processes(self) -> Sequence[DiagnosticProcess]: ...


class DiagnosticsService:
    def __init__(
        self,
        *,
        workspace_id: str,
        runtime: RuntimeDiagnosticsProvider,
        processes: ProcessDiagnosticsProvider,
        logger: LocalJsonLogger,
        metrics: MetricsRegistry,
        artifacts: ArtifactStore,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        if not workspace_id:
            raise ValueError("DiagnosticsService requires a Workspace")
        self._workspace_id = workspace_id
        self._runtime = runtime
        self._processes = processes
        self._logger = logger
        self._metrics = metrics
        self._artifacts = artifacts
        self._clock = clock
        self._ids = ids

    async def snapshot(self, *, include_recent_errors: bool = True) -> DiagnosticSnapshot:
        runtime = sanitize_fields(await self._runtime.snapshot(), RedactionPolicy(include_paths=False))
        processes = tuple(await self._processes.processes())
        recent = await self._logger.recent_errors() if include_recent_errors else ()
        errors: tuple[Mapping[str, JsonValue], ...] = tuple(
            {
                "timestamp": item.timestamp.astimezone(timezone.utc).isoformat(),
                "event": item.event,
                "message": item.message,
                **item.correlation.to_wire(),
                "fields": dict(item.fields),
            }
            for item in recent
        )
        metric_rows: tuple[Mapping[str, JsonValue], ...] = tuple(
            {
                "name": item.name.value,
                "kind": item.kind,
                "count": item.count,
                "total": item.total,
                "current": item.current,
                "p50": item.p50,
                "p95": item.p95,
                "p99": item.p99,
            }
            for item in self._metrics.snapshots()
        )
        return DiagnosticSnapshot(self._clock.utcnow(), runtime, processes, errors, metric_rows)

    async def preview_export(self, *, include_recent_errors: bool = True) -> DiagnosticBundlePreview:
        snapshot = await self.snapshot(include_recent_errors=include_recent_errors)
        payload = self._encode(snapshot, log_tail=b"")
        files: tuple[str, ...] = ("manifest.json", "runtime.json", "processes.json", "metrics.json")
        if include_recent_errors:
            files += ("recent-errors.json", "logs.jsonl")
        return DiagnosticBundlePreview(files, len(payload) + 1_048_576, False, False)

    async def export(
        self,
        *,
        owner_run_id: str,
        include_recent_errors: bool,
        cancellation: CancellationToken,
    ) -> ArtifactMetadata:
        cancellation.checkpoint()
        snapshot = await self.snapshot(include_recent_errors=include_recent_errors)
        log_tail = await self._logger.sanitized_log_tail() if include_recent_errors else b""
        payload = self._encode(snapshot, log_tail=log_tail)
        cancellation.checkpoint()
        metadata = ArtifactMetadata(
            artifact_id=self._ids.new_id("artifact"),
            workspace_id=self._workspace_id,
            owner_run_id=owner_run_id,
            mime_type="application/vnd.offeragent.diagnostics+json",
            byte_length=len(payload),
            sha256=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            sensitivity=Sensitivity.PRIVATE,
            state=ArtifactState.COMPLETE,
            created_at=self._clock.utcnow(),
            attributes={
                "kind": "diagnostic_bundle",
                "containsPaths": False,
                "containsContent": False,
                "telemetryUploaded": False,
            },
        )
        return await self._artifacts.put(
            metadata,
            payload,
            idempotency_key=f"diagnostics:{self._workspace_id}:{metadata.sha256}",
        )

    @staticmethod
    def _encode(snapshot: DiagnosticSnapshot, *, log_tail: bytes) -> bytes:
        try:
            log_text = log_tail.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("sanitized log tail is not UTF-8") from error
        value: dict[str, Any] = {
            "manifest": {
                "formatVersion": 1,
                "generatedAt": snapshot.generated_at.astimezone(timezone.utc).isoformat(),
                "containsPaths": False,
                "containsContent": False,
                "uploadDestination": None,
                "files": ["runtime", "processes", "metrics", "recentErrors", "sanitizedLogTail"],
            },
            "runtime": dict(snapshot.runtime),
            "processes": [asdict(item) for item in snapshot.processes],
            "metrics": list(snapshot.metrics),
            "recentErrors": list(snapshot.recent_errors),
            "sanitizedLogTail": log_text,
        }
        return (
            json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()


__all__ = [
    "DiagnosticBundlePreview",
    "DiagnosticProcess",
    "DiagnosticSnapshot",
    "DiagnosticsService",
    "ProcessDiagnosticsProvider",
    "RuntimeDiagnosticsProvider",
]
