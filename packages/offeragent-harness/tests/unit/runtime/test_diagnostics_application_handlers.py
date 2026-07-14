from __future__ import annotations

from datetime import datetime, timezone

import pytest

from offeragent_harness.observability import (
    DiagnosticBundlePreview,
    DiagnosticProcess,
    DiagnosticSnapshot,
    DiagnosticsService,
)
from offeragent_harness.ports import (
    ApplicationCommandContext,
    ArtifactMetadata,
    ArtifactState,
    CancellationToken,
    Sensitivity,
)
from offeragent_harness.protocol.messages import (
    DiagnosticsExportParams,
    DiagnosticsExportPreviewParams,
    DiagnosticsExportPreviewResult,
    DiagnosticsExportResult,
    DiagnosticsGetParams,
    DiagnosticsGetResult,
    DiagnosticsSnapshotParams,
    DiagnosticsSnapshotResult,
)
from offeragent_harness.runtime.application_handlers import diagnostics_command_handlers
from offeragent_harness.testing import ManualCancellationToken

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class OwnerRuns:
    async def authorize(self, context: ApplicationCommandContext, owner_run_id: str) -> str:
        assert context.client_id == "client_1"
        return owner_run_id


class Service(DiagnosticsService):
    def __init__(self) -> None:
        self.exports: list[str] = []

    async def snapshot(self, *, include_recent_errors: bool = True) -> DiagnosticSnapshot:
        return DiagnosticSnapshot(
            NOW,
            {"ready": True},
            (DiagnosticProcess("worker", 123, "running", True),),
            ({"event": "failed"},) if include_recent_errors else (),
            ({"name": "runs.active", "current": 1},),
        )

    async def preview_export(self, *, include_recent_errors: bool = True) -> DiagnosticBundlePreview:
        files = ("runtime.json", "logs.jsonl") if include_recent_errors else ("runtime.json",)
        return DiagnosticBundlePreview(files, 1024, False, False)

    async def export(
        self,
        *,
        owner_run_id: str,
        include_recent_errors: bool,
        cancellation: CancellationToken,
    ) -> ArtifactMetadata:
        del include_recent_errors
        cancellation.checkpoint()
        self.exports.append(owner_run_id)
        return ArtifactMetadata(
            artifact_id="art_diag",
            workspace_id="ws_1",
            owner_run_id=owner_run_id,
            mime_type="application/vnd.offeragent.diagnostics+json",
            byte_length=10,
            sha256="sha256:" + "a" * 64,
            sensitivity=Sensitivity.PRIVATE,
            state=ArtifactState.COMPLETE,
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_diagnostics_handlers_delegate_to_single_local_service() -> None:
    service = Service()
    handlers = diagnostics_command_handlers(service=service, owner_runs=OwnerRuns())
    context = ApplicationCommandContext(transport="loopback-http", client_id="client_1")
    cancellation = ManualCancellationToken()

    result = await handlers["diagnostics/get"](DiagnosticsGetParams(), cancellation, context)
    snapshot = await handlers["diagnostics/snapshot"](DiagnosticsSnapshotParams(), cancellation, context)
    preview = await handlers["diagnostics/export-preview"](
        DiagnosticsExportPreviewParams(include_recent_errors=False),
        cancellation,
        context,
    )
    exported = await handlers["diagnostics/export"](
        DiagnosticsExportParams(owner_run_id="run_1", include_recent_errors=True),
        cancellation,
        context,
    )

    assert isinstance(result, DiagnosticsGetResult)
    assert isinstance(snapshot, DiagnosticsSnapshotResult)
    assert isinstance(preview, DiagnosticsExportPreviewResult)
    assert isinstance(exported, DiagnosticsExportResult)
    assert result.runtime == snapshot.runtime == {"ready": True}
    assert preview.upload_destination is None and not preview.contains_paths
    assert exported.artifact.sensitivity.value == "private" and not exported.uploaded
    assert service.exports == ["run_1"]
