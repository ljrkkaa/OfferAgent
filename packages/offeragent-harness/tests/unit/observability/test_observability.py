from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from offeragent_harness.observability import (
    DataClass,
    DiagnosticProcess,
    DiagnosticsService,
    LocalJsonLogger,
    LogField,
    LogLevel,
    MetricName,
    MetricsRegistry,
    RedactionPolicy,
    TraceCorrelation,
    sanitize_fields,
)
from offeragent_harness.ports import ArtifactMetadata
from offeragent_harness.runtime.windows_security import (
    current_windows_identity,
    kernel_handle_is_inheritable,
    kernel_object_security_sddl,
)
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    FakeRunCancelled,
    ManualCancellationToken,
    ManualClock,
)

NOW = datetime(2032, 4, 5, 6, 7, 8, tzinfo=timezone.utc)
CORRELATION = TraceCorrelation(
    trace_id="trace_1",
    workspace_id="workspace_1",
    session_id="session_1",
    turn_id="turn_1",
    run_id="run_1",
    tool_call_id="tool_1",
)


class MemoryArtifactStore:
    def __init__(self) -> None:
        self.metadata_by_id: dict[str, ArtifactMetadata] = {}
        self.content_by_id: dict[str, bytes] = {}
        self.keys: dict[str, str] = {}

    async def put(
        self,
        metadata: ArtifactMetadata,
        content: bytes,
        *,
        idempotency_key: str,
    ) -> ArtifactMetadata:
        assert metadata.byte_length == len(content)
        assert metadata.sha256 == f"sha256:{hashlib.sha256(content).hexdigest()}"
        prior = self.keys.get(idempotency_key)
        if prior is not None:
            assert self.content_by_id[prior] == content
            return self.metadata_by_id[prior]
        self.keys[idempotency_key] = metadata.artifact_id
        self.metadata_by_id[metadata.artifact_id] = metadata
        self.content_by_id[metadata.artifact_id] = content
        return metadata

    async def metadata(self, artifact_id: str) -> ArtifactMetadata | None:
        return self.metadata_by_id.get(artifact_id)

    async def read(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> AsyncIterator[bytes]:
        content = self.content_by_id[artifact_id]
        yield content[offset : None if limit is None else offset + limit]


class RuntimeProbe:
    async def snapshot(self) -> Mapping[str, LogField]:
        return {
            "version": LogField("1.2.3", DataClass.PUBLIC),
            "vaultRoot": LogField(r"E:\private-vault", DataClass.PATH),
            "providerToken": LogField("do-not-export", DataClass.SECRET),
            "queueDepth": LogField(3, DataClass.METRIC),
        }


class ProcessProbe:
    async def processes(self) -> Sequence[DiagnosticProcess]:
        return (DiagnosticProcess("worker", 1234, "running", True),)


def _logger(tmp_path: Path, **overrides: object) -> LocalJsonLogger:
    values: dict[str, object] = {
        "workspace_instance_id": "workspace_1",
        "allowed_root": tmp_path,
    }
    values.update(overrides)
    return LocalJsonLogger(tmp_path / "logs", **values)  # type: ignore[arg-type]


def test_classified_redaction_is_bounded_and_fails_closed() -> None:
    sanitized = sanitize_fields(
        {
            "secret": LogField("sk-live-secret", DataClass.SECRET),
            "content": LogField("private interview note", DataClass.CONTENT),
            "path": LogField(r"E:\sensitive-vault\notes\private.md", DataClass.PATH),
            "public": LogField(r"api_key=also-secret at E:\vault\note.md", DataClass.PUBLIC),
            "metric": LogField(42, DataClass.METRIC),
        }
    )

    encoded = json.dumps(sanitized, ensure_ascii=False)
    assert "sk-live-secret" not in encoded
    assert "private interview note" not in encoded
    assert "sensitive-vault" not in encoded
    assert "also-secret" not in encoded
    assert sanitized["metric"] == 42
    assert str(sanitized["content"]).startswith("<content:redacted bytes=")

    with pytest.raises(TypeError, match="explicit LogField"):
        sanitize_fields({"unsafe": "not-classified"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="item limit"):
        sanitize_fields(
            {"items": LogField([1, 2], DataClass.PUBLIC)},
            RedactionPolicy(max_collection_items=1),
        )


def test_trace_correlation_requires_valid_parent_and_tool_run_links() -> None:
    assert CORRELATION.to_wire()["toolCallId"] == "tool_1"
    with pytest.raises(ValueError, match="requires run_id"):
        TraceCorrelation("trace_1", "workspace_1", parent_run_id="parent_1")
    with pytest.raises(ValueError, match="invalid observability"):
        TraceCorrelation("contains spaces", "workspace_1")


def test_metrics_are_bounded_typed_and_report_exact_percentiles() -> None:
    metrics = MetricsRegistry(reservoir_size=16)
    for value in range(1, 21):
        metrics.observe(MetricName.MODEL_LATENCY_MS, value)
    metrics.increment(MetricName.MODEL_INPUT_TOKENS, 25)
    metrics.set_gauge(MetricName.EVENT_QUEUE_LENGTH, 7)

    rows = {item.name: item for item in metrics.snapshots()}
    latency = rows[MetricName.MODEL_LATENCY_MS]
    assert latency.count == 16
    assert latency.p50 == 12
    assert latency.p95 == 20
    assert latency.p99 == 20
    assert rows[MetricName.MODEL_INPUT_TOKENS].total == 25
    assert rows[MetricName.EVENT_QUEUE_LENGTH].current == 7

    with pytest.raises(ValueError, match="not a histogram"):
        metrics.observe(MetricName.TOOL_ERRORS, 1)
    with pytest.raises(TypeError, match="finite"):
        metrics.increment(MetricName.MODEL_OUTPUT_TOKENS, float("nan"))
    with pytest.raises(ValueError, match="non-negative"):
        metrics.set_gauge(MetricName.SUBAGENT_DEPTH, -1)


async def test_logger_serializes_concurrent_writes_and_never_persists_sensitive_values(tmp_path: Path) -> None:
    logger = _logger(tmp_path)

    await asyncio.gather(
        *(
            logger.emit(
                LogLevel.ERROR if index % 3 == 0 else LogLevel.INFO,
                "tool.completed",
                r"api_key=message-secret E:\private\note.md",
                CORRELATION,
                {
                    "ordinal": LogField(index, DataClass.METRIC),
                    "body": LogField(f"private-body-{index}", DataClass.CONTENT),
                    "credential": LogField(f"token-{index}", DataClass.SECRET),
                },
                occurred_at=NOW + timedelta(microseconds=index),
            )
            for index in range(30)
        )
    )

    lines = logger.path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    persisted = logger.path.read_text(encoding="utf-8")
    assert len(records) == 30
    assert {item["fields"]["ordinal"] for item in records} == set(range(30))
    assert "message-secret" not in persisted
    assert "private-body" not in persisted
    assert "token-" not in persisted
    assert r"E:\private" not in persisted
    assert len(await logger.recent_errors()) == 10


async def test_logger_rotates_prunes_and_enforces_state_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        LocalJsonLogger(tmp_path.parent / "outside", workspace_instance_id="workspace_1", allowed_root=tmp_path)

    logger = _logger(tmp_path, max_file_bytes=64 * 1024, max_files=2, retention=timedelta(seconds=1))
    old = logger.path.with_suffix(".jsonl.1")
    logger.path.parent.mkdir(parents=True)
    old.write_text("old", encoding="utf-8")
    stale = datetime.now(timezone.utc).timestamp() - 10
    os.utime(old, (stale, stale))
    message = "x" * 3500
    for _index in range(25):
        await logger.emit(LogLevel.INFO, "stress.record", message, CORRELATION, occurred_at=NOW)

    assert not old.exists() or old.read_text(encoding="utf-8") != "old"
    assert logger.path.exists()
    assert logger.path.with_suffix(".jsonl.1").exists()
    assert not logger.path.with_suffix(".jsonl.3").exists()


async def test_diagnostics_preview_and_private_artifact_export_are_local_and_redacted(tmp_path: Path) -> None:
    logger = _logger(tmp_path)
    metrics = MetricsRegistry()
    metrics.observe(MetricName.TOOL_LATENCY_MS, 25)
    await logger.emit(
        LogLevel.ERROR,
        "worker.failed",
        "token=diagnostic-secret",
        CORRELATION,
        {"note": LogField("confidential user content", DataClass.CONTENT)},
        occurred_at=NOW,
    )
    artifacts = MemoryArtifactStore()
    service = DiagnosticsService(
        workspace_id="workspace_1",
        runtime=RuntimeProbe(),
        processes=ProcessProbe(),
        logger=logger,
        metrics=metrics,
        artifacts=artifacts,
        clock=ManualClock(NOW),
        ids=DeterministicIdGenerator(),
    )

    preview = await service.preview_export()
    assert preview.upload_destination is None
    assert not preview.contains_paths
    assert not preview.contains_content
    assert "logs.jsonl" in preview.files

    exported = await service.export(
        owner_run_id="run_1",
        include_recent_errors=True,
        cancellation=ManualCancellationToken(),
    )
    payload = artifacts.content_by_id[exported.artifact_id]
    decoded = json.loads(payload)
    text = payload.decode()
    assert exported.artifact_id == "artifact_0001"
    assert exported.sensitivity.value == "private"
    assert exported.attributes["telemetryUploaded"] is False
    assert decoded["manifest"]["uploadDestination"] is None
    assert decoded["runtime"]["vaultRoot"] == "<path:redacted>"
    assert decoded["runtime"]["providerToken"] == "<secret:redacted>"
    assert "private-vault" not in text
    assert "diagnostic-secret" not in text
    assert "confidential user content" not in text


async def test_diagnostics_cancellation_prevents_artifact_write(tmp_path: Path) -> None:
    artifacts = MemoryArtifactStore()
    token = ManualCancellationToken()
    token.cancel()
    service = DiagnosticsService(
        workspace_id="workspace_1",
        runtime=RuntimeProbe(),
        processes=ProcessProbe(),
        logger=_logger(tmp_path),
        metrics=MetricsRegistry(),
        artifacts=artifacts,
        clock=ManualClock(NOW),
        ids=DeterministicIdGenerator(),
    )

    with pytest.raises(FakeRunCancelled):
        await service.export(owner_run_id="run_1", include_recent_errors=False, cancellation=token)
    assert artifacts.content_by_id == {}


def test_diagnostic_process_rejects_unbounded_or_unknown_state_text() -> None:
    with pytest.raises(ValueError, match="invalid diagnostic process"):
        DiagnosticProcess("worker", 1, r"failed at E:\private\note.md", True)
    with pytest.raises(ValueError, match="invalid diagnostic process"):
        DiagnosticProcess("browser", 1, "running", True)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows filesystem ACLs")
async def test_logger_directory_and_file_have_current_sid_only_dacl(tmp_path: Path) -> None:
    logger = _logger(tmp_path)
    await logger.emit(LogLevel.INFO, "runtime.ready", "Worker ready", CORRELATION, occurred_at=NOW)
    expected_sid = current_windows_identity().sid

    for path, directory in ((logger.path.parent, True), (logger.path, False)):
        handle = _open_security_handle(path, directory=directory)
        try:
            sddl = kernel_object_security_sddl(handle)
            trustees = re.findall(r"\([^)]*;;;([^)]+)\)", sddl)
            assert trustees == [expected_sid]
            assert not kernel_handle_is_inheritable(handle)
        finally:
            _close_handle(handle)


def _open_security_handle(path: Path, *, directory: bool) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    flags = 0x02000000 if directory else 0
    handle = kernel32.CreateFileW(str(path), 0x00020000, 0x7, None, 3, flags, None)
    if not handle or int(handle) == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code))
    return int(handle)


def _close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(wintypes.HANDLE(handle)):
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code))
