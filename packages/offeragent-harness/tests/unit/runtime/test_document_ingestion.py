from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import offeragent_harness.runtime.document_ingestion as document_ingestion
from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.agent.state import RunState
from offeragent_harness.config import HarnessConfig
from offeragent_harness.documents import decode_canonical_request
from offeragent_harness.foundation.canonical import canonical_json_bytes
from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.ports import (
    ArtifactMetadata,
    CancellationToken,
    ProcessLifecycleState,
    ProcessOwnerKind,
    ProcessSupervisor,
    SupervisedProcessRequest,
    SupervisedProcessResult,
    VaultEntry,
    VaultEntryKind,
    VaultRead,
)
from offeragent_harness.protocol.content import (
    DocumentContentBlock,
    DocumentFileRef,
    DocumentMediaType,
)
from offeragent_harness.protocol.documents import DocumentExtractionFailureCode
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.runtime.cancellation import CancellationScope
from offeragent_harness.runtime.document_ingestion import (
    DocumentIngestionBatchError,
    DocumentIngestionConfig,
    DocumentIngestionRecoveryError,
    DocumentIngestionService,
    FailedDocument,
    IndexedDocumentContent,
    PreparedDocumentIngestion,
    document_id_for_input,
)
from offeragent_harness.runtime.harness_service import StartTurnCommand
from offeragent_harness.runtime.production_worker_composition import ProductionRunComponentsFactory
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
)
from offeragent_harness.tools import canonical_json_sha256

_NOW = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
_PARSER_FINGERPRINT = "sha256:" + "c" * 64


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _directory_is_empty(path: Path) -> bool:
    return not any(path.iterdir())


class _Clock:
    def utcnow(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 1.0

    async def sleep_until(self, deadline: datetime) -> None:
        del deadline


class _SourceReader:
    workspace_id = "ws_main"

    def __init__(
        self,
        content: bytes,
        *,
        entry_hash: str | None = None,
        truncated: bool = False,
        path: str = ".offeragent/attachments/source.pdf",
    ) -> None:
        self.content = content
        self.entry_hash = _sha256(content) if entry_hash is None else entry_hash
        self.truncated = truncated
        self.path = path
        self.read_limits: list[int] = []
        self.available = True

    async def read_bounded(
        self,
        relative_path: str,
        max_bytes: int,
        cancellation: CancellationToken,
    ) -> VaultRead:
        cancellation.checkpoint()
        if not self.available:
            raise FileNotFoundError(relative_path)
        self.read_limits.append(max_bytes)
        return VaultRead(
            entry=VaultEntry(
                resource_id=f"vault:ws_main:{self.path}",
                relative_path=self.path,
                kind=VaultEntryKind.FILE,
                size=len(self.content),
                modified_at=_NOW,
                content_hash=self.entry_hash,
                workspace_revision=3,
            ),
            content=self.content,
            truncated=self.truncated,
        )


class _Artifacts:
    def __init__(self, *, mismatch: bool = False) -> None:
        self.mismatch = mismatch
        self.calls: list[tuple[ArtifactMetadata, bytes, str]] = []

    async def put(
        self,
        metadata: ArtifactMetadata,
        content: bytes,
        *,
        idempotency_key: str,
    ) -> ArtifactMetadata:
        assert metadata.byte_length == len(content)
        assert metadata.sha256 == _sha256(content)
        self.calls.append((metadata, bytes(content), idempotency_key))
        if self.mismatch:
            return replace(metadata, owner_run_id="run_wrong")
        return metadata

    async def metadata(self, artifact_id: str) -> ArtifactMetadata | None:
        return next((metadata for metadata, _, _ in self.calls if metadata.artifact_id == artifact_id), None)

    def read(self, artifact_id: str, *, offset: int = 0, limit: int | None = None):  # type: ignore[no-untyped-def]
        del artifact_id, offset, limit
        raise NotImplementedError


class _Process(ProcessSupervisor):
    def __init__(self, *, mode: str = "success") -> None:
        self.mode = mode
        self.requests: list[SupervisedProcessRequest] = []
        self.staged_paths: list[Path] = []

    async def execute(
        self,
        request: SupervisedProcessRequest,
        cancellation: CancellationToken,
    ) -> SupervisedProcessResult:
        cancellation.checkpoint()
        self.requests.append(request)
        parsed = decode_canonical_request(request.stdin)
        self.staged_paths.append(parsed.source.absolute_path)
        assert parsed.source.absolute_path.read_bytes().startswith(b"%PDF-")
        if self.mode == "cancel":
            assert isinstance(cancellation, ManualCancellationToken)
            cancellation.cancel()
            cancellation.checkpoint()
        response = _success_response(request)
        if self.mode == "invalid_json":
            response = b'{"ok":true,"ok":false}'
        elif self.mode == "request_drift":
            value = json.loads(response)
            value["requestId"] = "req_wrong"
            response = canonical_json_bytes(value)
        elif self.mode == "config_drift":
            value = json.loads(response)
            value["result"]["parserConfigFingerprint"] = "sha256:" + "d" * 64
            response = canonical_json_bytes(value)
        if self.mode == "process_failure":
            return SupervisedProcessResult(exit_code=17, stdout=b"", stderr=b"safe")
        if self.mode == "truncated":
            return SupervisedProcessResult(exit_code=0, stdout=response, stderr=b"", output_truncated=True)
        if self.mode == "deadline":
            return SupervisedProcessResult(
                exit_code=1,
                stdout=b"",
                stderr=b"",
                timed_out=True,
                lifecycle_state=ProcessLifecycleState.TIMED_OUT,
            )
        return SupervisedProcessResult(exit_code=0, stdout=response, stderr=b"")


def _success_response(request: SupervisedProcessRequest) -> bytes:
    parsed = decode_canonical_request(request.stdin)
    source = parsed.source.absolute_path.read_bytes()
    first = "第一页面试题"
    second = "second page"
    first_end = len(first.encode("utf-8"))
    second_start = first_end + 2
    second_end = second_start + len(second.encode("utf-8"))
    text = f"{first}\n\n{second}"
    backend = {"name": "fixture", "version": "1.0.0"}
    pages = [
        {
            "provenance": {
                "sourceId": parsed.source.source_id,
                "sourceSha256": parsed.source.expected_sha256,
                "pageNumber": 1,
                "inputBackend": backend,
                "extractionBackend": backend,
            },
            "extractionMethod": "embedded_text",
            "text": first,
            "characterCount": len(first),
            "utf8ByteCount": first_end,
            "ocrRegions": [],
            "utf8StartByte": 0,
            "utf8EndByte": first_end,
        },
        {
            "provenance": {
                "sourceId": parsed.source.source_id,
                "sourceSha256": parsed.source.expected_sha256,
                "pageNumber": 2,
                "inputBackend": backend,
                "extractionBackend": backend,
            },
            "extractionMethod": "ocr",
            "text": second,
            "characterCount": len(second),
            "utf8ByteCount": len(second.encode("utf-8")),
            "ocrRegions": [
                {
                    "polygon": [
                        {"x": 0.0, "y": 0.0},
                        {"x": 1.0, "y": 0.0},
                        {"x": 1.0, "y": 1.0},
                        {"x": 0.0, "y": 1.0},
                    ],
                    "text": second,
                    "confidence": 0.9,
                }
            ],
            "utf8StartByte": second_start,
            "utf8EndByte": second_end,
        },
    ]
    return canonical_json_bytes(
        {
            "schemaVersion": 1,
            "requestId": parsed.request_id,
            "ok": True,
            "result": {
                "source": {
                    "sourceId": parsed.source.source_id,
                    "sha256": parsed.source.expected_sha256,
                    "mediaType": parsed.source.declared_media_type.value,
                    "byteSize": len(source),
                },
                "parserConfigFingerprint": _PARSER_FINGERPRINT,
                "text": text,
                "pageCount": 2,
                "totalCharacters": len(text),
                "totalTextUtf8Bytes": len(text.encode("utf-8")),
                "pages": pages,
            },
        }
    )


def _block(content: bytes, *, media_type: DocumentMediaType = DocumentMediaType.PDF) -> IndexedDocumentContent:
    return IndexedDocumentContent(
        input_block_index=1,
        block=DocumentContentBlock(
            type="document",
            file=DocumentFileRef(
                workspace_id="ws_main",
                path=".offeragent/attachments/source.pdf",
                content_hash=_sha256(content),
            ),
            media_type=media_type,
        ),
    )


def _service(
    tmp_path: Path,
    source: _SourceReader,
    process: _Process,
    artifacts: _Artifacts,
    **limits: Any,
) -> DocumentIngestionService:
    config = DocumentIngestionConfig(parser_config_fingerprint=_PARSER_FINGERPRINT, **limits)
    return DocumentIngestionService(
        workspace_id="ws_main",
        source_reader=source,
        process_supervisor=process,
        artifacts=artifacts,
        scratch_working_root=tmp_path,
        clock=_Clock(),
        config=config,
    )


def _production_factory(
    *,
    clock: ManualClock,
    artifacts: LocalArtifactStore,
    document_service: DocumentIngestionService,
) -> ProductionRunComponentsFactory:
    config = HarnessConfig()
    factory = ProductionRunComponentsFactory(
        workspace_id="ws_main",
        clock=clock,
        ids=DeterministicIdGenerator(),
        gateway_factory=lambda _settings: object(),  # type: ignore[arg-type]
        bootstrap_max_parallel_reads=config.budgets.max_parallel_reads,
        approvals=ApprovalManager(unit_of_work=InMemoryUnitOfWorkFactory(), clock=clock),
        policy_audit=object(),  # type: ignore[arg-type]
        journal=object(),
        artifacts=artifacts,
        local_read=SimpleNamespace(definitions=()),  # type: ignore[arg-type]
        local_transaction=SimpleNamespace(provider_id="vault.transaction"),  # type: ignore[arg-type]
        parent_authorities=object(),  # type: ignore[arg-type]
        document_ingestion=document_service,
    )
    factory.bind_worker_read_limit(config.budgets.max_parallel_reads)
    return factory


def _root_command_and_state(content: bytes) -> tuple[StartTurnCommand, RunState]:
    effective_config = HarnessConfig()
    command = StartTurnCommand(
        workspace_id="ws_main",
        session_id="ses_main",
        turn_id="turn_main",
        idempotency_key="idem-document-recovery",
        input_blocks=(_block(content).block.to_wire(),),
        run_config={
            "provider": "codex",
            "model": "gpt-test",
            "permissionMode": "read-only",
        },
        effective_config=effective_config,
        effective_config_fingerprint=canonical_json_sha256(effective_config.model_dump(mode="json")),
    )
    return command, RunState(
        workspace_id="ws_main",
        session_id="ses_main",
        turn_id="turn_main",
        run_id="run_main",
        lineage=AgentLineage.root("run_main"),
    )


@dataclass(frozen=True, slots=True)
class _PersistedDocumentCase:
    content: bytes
    source: _SourceReader
    process: _Process
    artifacts: LocalArtifactStore
    scratch: Path
    clock: ManualClock
    config: DocumentIngestionConfig
    command: StartTurnCommand
    state: RunState
    snapshot: dict[str, Any]


async def _persist_document_case(tmp_path: Path) -> _PersistedDocumentCase:
    content = b"%PDF-fixture"
    source = _SourceReader(content)
    process = _Process()
    artifacts = LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_main")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    clock = ManualClock(_NOW)
    config = DocumentIngestionConfig(parser_config_fingerprint=_PARSER_FINGERPRINT)
    service = DocumentIngestionService(
        workspace_id="ws_main",
        source_reader=source,
        process_supervisor=process,
        artifacts=artifacts,
        scratch_working_root=scratch,
        clock=clock,
        config=config,
    )
    command, state = _root_command_and_state(content)
    first = await _production_factory(
        clock=clock,
        artifacts=artifacts,
        document_service=service,
    ).prepare_root(command, state, CancellationScope(), None)
    snapshot = thaw_json(first.durable_snapshot)
    if not isinstance(snapshot, dict):
        raise TypeError("test fixture requires an object capability snapshot")
    return _PersistedDocumentCase(
        content,
        source,
        process,
        artifacts,
        scratch,
        clock,
        config,
        command,
        state,
        snapshot,
    )


async def _ingest(
    service: DocumentIngestionService,
    *documents: IndexedDocumentContent,
    token: ManualCancellationToken | None = None,
) -> PreparedDocumentIngestion:
    return await service.ingest(
        run_id="run_main",
        documents=documents,
        created_at=_NOW,
        cancellation=token or ManualCancellationToken(),
    )


@pytest.mark.asyncio
async def test_success_persists_canonical_artifacts_and_full_provenance_then_cleans_scratch(tmp_path: Path) -> None:
    content = b"%PDF-fixture"
    source = _SourceReader(content)
    process = _Process()
    artifacts = _Artifacts()
    service = _service(tmp_path, source, process, artifacts, max_context_fragment_bytes=8)

    prepared = await _ingest(service, _block(content))

    assert len(prepared.documents) == 1
    document = prepared.documents[0]
    assert document.document_id == document_id_for_input("run_main", 1, _sha256(content))
    assert document.text_artifact.owner_run_id == "run_main"
    assert document.provenance_artifact.owner_run_id == "run_main"
    assert len(artifacts.calls) == 2
    assert b"first" not in artifacts.calls[1][1]
    provenance = json.loads(artifacts.calls[1][1])
    assert [page["pageNumber"] for page in provenance["pages"]] == [1, 2]
    assert provenance["pages"][1]["utf8StartByte"] > provenance["pages"][0]["utf8EndByte"]
    assert document.completed_payload.page_count == 2
    assert [item.locator.page_start for item in document.completed_payload.page_provenance] == [1, 2]
    assert "".join(fragment.text for fragment in document.context_fragments) == "第一页面试题second page"
    assert all(fragment.layer.value == "user_input" for fragment in document.context_fragments)
    assert process.requests[0].owner_kind is ProcessOwnerKind.PARSER
    assert process.requests[0].allow_network is True
    assert process.requests[0].arguments == ("document-extract",)
    assert process.requests[0].cwd.startswith("working/document-ingestion-")
    assert not process.staged_paths[0].exists()
    assert _directory_is_empty(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "reader_kwargs", "media_type", "expected"),
    [
        (b"GIF89a", {}, DocumentMediaType.PDF, DocumentExtractionFailureCode.UNSUPPORTED_MEDIA_TYPE),
        (
            b"%PDF-fixture",
            {"entry_hash": "sha256:" + "a" * 64},
            DocumentMediaType.PDF,
            DocumentExtractionFailureCode.SOURCE_INTEGRITY_MISMATCH,
        ),
        (
            b"%PDF-fixture",
            {"truncated": True},
            DocumentMediaType.PDF,
            DocumentExtractionFailureCode.SOURCE_INTEGRITY_MISMATCH,
        ),
    ],
)
async def test_source_magic_hash_and_complete_read_fail_closed(
    tmp_path: Path,
    content: bytes,
    reader_kwargs: dict[str, object],
    media_type: DocumentMediaType,
    expected: DocumentExtractionFailureCode,
) -> None:
    source = _SourceReader(content, **reader_kwargs)  # type: ignore[arg-type]
    process = _Process()
    service = _service(tmp_path, source, process, _Artifacts())

    with pytest.raises(DocumentIngestionBatchError) as raised:
        await _ingest(service, _block(content, media_type=media_type))

    assert raised.value.failed_payloads[0].failure.code is expected
    assert process.requests == []
    assert _directory_is_empty(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("process_failure", DocumentExtractionFailureCode.PARSER_FAILED),
        ("truncated", DocumentExtractionFailureCode.OUTPUT_INTEGRITY_MISMATCH),
        ("deadline", DocumentExtractionFailureCode.DEADLINE_EXCEEDED),
        ("invalid_json", DocumentExtractionFailureCode.OUTPUT_INTEGRITY_MISMATCH),
        ("request_drift", DocumentExtractionFailureCode.OUTPUT_INTEGRITY_MISMATCH),
        ("config_drift", DocumentExtractionFailureCode.OUTPUT_INTEGRITY_MISMATCH),
        ("cancel", DocumentExtractionFailureCode.CANCELLED),
    ],
)
async def test_process_failures_have_stable_safe_codes_and_clean_scratch(
    tmp_path: Path,
    mode: str,
    expected: DocumentExtractionFailureCode,
) -> None:
    content = b"%PDF-fixture"
    process = _Process(mode=mode)
    service = _service(tmp_path, _SourceReader(content), process, _Artifacts())

    with pytest.raises(DocumentIngestionBatchError) as raised:
        await _ingest(service, _block(content))

    payload = raised.value.failed_payloads[0]
    assert payload.failure.code is expected
    assert ".offeragent" not in payload.failure.user_visible_message
    assert "%PDF" not in payload.failure.user_visible_message
    assert _directory_is_empty(tmp_path)


@pytest.mark.asyncio
async def test_deadline_closes_every_started_document_without_running_later_items(tmp_path: Path) -> None:
    content = b"%PDF-fixture"
    process = _Process(mode="deadline")
    service = _service(tmp_path, _SourceReader(content), process, _Artifacts())
    first = _block(content)
    second = IndexedDocumentContent(input_block_index=2, block=first.block)

    with pytest.raises(DocumentIngestionBatchError) as raised:
        await _ingest(service, first, second)

    assert len(raised.value.started_payloads) == 2
    assert len(raised.value.outcomes) == 2
    assert all(isinstance(outcome, FailedDocument) for outcome in raised.value.outcomes)
    assert [payload.failure.code for payload in raised.value.failed_payloads] == [
        DocumentExtractionFailureCode.DEADLINE_EXCEEDED,
        DocumentExtractionFailureCode.DEADLINE_EXCEEDED,
    ]
    assert len(process.requests) == 1


@pytest.mark.asyncio
async def test_artifact_metadata_drift_fails_after_verification_and_is_not_exposed(tmp_path: Path) -> None:
    content = b"%PDF-fixture"
    artifacts = _Artifacts(mismatch=True)
    service = _service(tmp_path, _SourceReader(content), _Process(), artifacts)

    with pytest.raises(DocumentIngestionBatchError) as raised:
        await _ingest(service, _block(content))

    assert raised.value.failed_payloads[0].failure.code is DocumentExtractionFailureCode.OUTPUT_INTEGRITY_MISMATCH
    assert len(artifacts.calls) == 1
    assert _directory_is_empty(tmp_path)


@pytest.mark.asyncio
async def test_scratch_cleanup_failure_fails_closed_and_final_cleanup_is_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"%PDF-fixture"
    original = document_ingestion._remove_secure_tree
    calls = 0

    def fail_first_cleanup(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected cleanup failure")
        original(path)

    monkeypatch.setattr(document_ingestion, "_remove_secure_tree", fail_first_cleanup)
    service = _service(tmp_path, _SourceReader(content), _Process(), _Artifacts())

    with pytest.raises(DocumentIngestionBatchError) as raised:
        await _ingest(service, _block(content))

    assert raised.value.failed_payloads[0].failure.code is DocumentExtractionFailureCode.PARSER_FAILED
    assert calls >= 2
    assert _directory_is_empty(tmp_path)


@pytest.mark.asyncio
async def test_text_or_context_limit_never_returns_a_truncated_success(tmp_path: Path) -> None:
    content = b"%PDF-fixture"
    service = _service(
        tmp_path,
        _SourceReader(content),
        _Process(),
        _Artifacts(),
        max_text_bytes_per_document=16,
        max_total_text_bytes=16,
        max_total_context_bytes=16,
        max_context_fragment_bytes=8,
    )

    with pytest.raises(DocumentIngestionBatchError) as raised:
        await _ingest(service, _block(content))

    assert raised.value.failed_payloads[0].failure.code is DocumentExtractionFailureCode.DOCUMENT_LIMIT_EXCEEDED
    assert raised.value.partial.documents == ()


def test_document_id_is_deterministic_protocol_identity() -> None:
    source_hash = "sha256:" + "1" * 64

    first = document_id_for_input("run_main", 7, source_hash)

    assert first == document_id_for_input("run_main", 7, source_hash)
    assert first.startswith("doc_")
    assert len(first) == 68
    assert first != document_id_for_input("run_main", 8, source_hash)


@pytest.mark.asyncio
async def test_production_recovery_uses_durable_document_artifacts_after_source_is_deleted(
    tmp_path: Path,
) -> None:
    case = await _persist_document_case(tmp_path)
    case.source.available = False
    case.clock.advance(timedelta(hours=1))
    recovered_service = DocumentIngestionService(
        workspace_id="ws_main",
        source_reader=case.source,
        process_supervisor=case.process,
        artifacts=case.artifacts,
        scratch_working_root=case.scratch,
        clock=case.clock,
        config=case.config,
    )
    recovered_factory = _production_factory(
        clock=case.clock,
        artifacts=case.artifacts,
        document_service=recovered_service,
    )

    recovered = await recovered_factory.prepare_root(
        case.command,
        case.state,
        CancellationScope(),
        case.snapshot,
    )

    assert thaw_json(recovered.durable_snapshot) == case.snapshot
    assert len(case.process.requests) == 1
    assert case.source.read_limits == [case.config.max_source_bytes_per_document]


@pytest.mark.asyncio
async def test_production_recovery_rejects_document_snapshot_for_a_drifted_explicit_block(
    tmp_path: Path,
) -> None:
    case = await _persist_document_case(tmp_path)
    case.source.available = False
    original = _block(case.content).block
    drifted = DocumentContentBlock(
        type="document",
        file=DocumentFileRef(
            workspace_id=original.file.workspace_id,
            path="OfferAgent/Attachments/renamed.pdf",
            content_hash=original.file.content_hash,
        ),
        media_type=original.media_type,
    )
    drifted_command = replace(case.command, input_blocks=(drifted.to_wire(),))
    service = DocumentIngestionService(
        workspace_id="ws_main",
        source_reader=case.source,
        process_supervisor=case.process,
        artifacts=case.artifacts,
        scratch_working_root=case.scratch,
        clock=case.clock,
        config=case.config,
    )

    with pytest.raises(DocumentIngestionRecoveryError):
        await _production_factory(
            clock=case.clock,
            artifacts=case.artifacts,
            document_service=service,
        ).prepare_root(drifted_command, case.state, CancellationScope(), case.snapshot)

    assert len(case.process.requests) == 1
    assert len(case.source.read_limits) == 1


@pytest.mark.asyncio
async def test_production_recovery_rejects_document_snapshot_bound_to_another_run(tmp_path: Path) -> None:
    case = await _persist_document_case(tmp_path)
    case.source.available = False
    other_state = replace(case.state, run_id="run_other", lineage=AgentLineage.root("run_other"))
    service = DocumentIngestionService(
        workspace_id="ws_main",
        source_reader=case.source,
        process_supervisor=case.process,
        artifacts=case.artifacts,
        scratch_working_root=case.scratch,
        clock=case.clock,
        config=case.config,
    )

    with pytest.raises(DocumentIngestionRecoveryError):
        await _production_factory(
            clock=case.clock,
            artifacts=case.artifacts,
            document_service=service,
        ).prepare_root(case.command, other_state, CancellationScope(), case.snapshot)

    assert len(case.process.requests) == 1
    assert len(case.source.read_limits) == 1


@pytest.mark.asyncio
async def test_production_recovery_rejects_a_missing_document_capability_snapshot(tmp_path: Path) -> None:
    case = await _persist_document_case(tmp_path)
    missing = dict(case.snapshot)
    missing["documents"] = None
    service = DocumentIngestionService(
        workspace_id="ws_main",
        source_reader=case.source,
        process_supervisor=case.process,
        artifacts=case.artifacts,
        scratch_working_root=case.scratch,
        clock=case.clock,
        config=case.config,
    )

    with pytest.raises(ValueError, match="snapshot is missing"):
        await _production_factory(
            clock=case.clock,
            artifacts=case.artifacts,
            document_service=service,
        ).prepare_root(case.command, case.state, CancellationScope(), missing)

    assert len(case.process.requests) == 1
    assert len(case.source.read_limits) == 1
