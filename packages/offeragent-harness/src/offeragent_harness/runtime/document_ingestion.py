"""Fail-closed Run preparation for hashed PDF and image attachments.

The original :class:`~offeragent_harness.protocol.DocumentContentBlock` is the
authoritative source reference.  This module verifies that immutable reference,
stages bytes inside the Run's scratch directory, invokes the fixed parser-host
profile as the current Windows user, and persists derived text/provenance
Artifacts.  It does not infer intent from filenames or user text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable

from offeragent_harness.agent.context_manager import ContextFragment, ContextLayer
from offeragent_harness.documents import (
    CanonicalDocumentResult,
    CanonicalParseFailure,
    CanonicalParseSuccess,
    DocumentErrorCode,
    DocumentParseError,
    DocumentParseRequest,
    DocumentSource,
    decode_canonical_response,
    encode_canonical_request,
)
from offeragent_harness.documents import (
    DocumentMediaType as ParserDocumentMediaType,
)
from offeragent_harness.foundation.canonical import canonical_json_bytes
from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json, thaw_json
from offeragent_harness.ports import (
    ArtifactMetadata,
    ArtifactState,
    ArtifactStore,
    CancellationToken,
    Clock,
    OperationCancelled,
    ProcessLifecycleState,
    ProcessOutputEncoding,
    ProcessOwnerKind,
    ProcessStdinMode,
    ProcessSupervisor,
    Sensitivity,
    SupervisedProcessRequest,
    VaultEntryKind,
    VaultRead,
)
from offeragent_harness.protocol.content import DocumentContentBlock, DocumentMediaType, DocumentPageLocator
from offeragent_harness.protocol.documents import (
    DocumentExtractionCompletedPayload,
    DocumentExtractionFailedPayload,
    DocumentExtractionFailure,
    DocumentExtractionFailureCode,
    DocumentExtractionMethod,
    DocumentExtractionStartedPayload,
    DocumentExtractionWarning,
    DocumentExtractionWarningCode,
    DocumentPageProvenance,
)

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROFILE_SHA256 = _SHA256
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_PROCESS_EXECUTABLE_ID = "document-extract"
_PROCESS_ARGUMENTS = ("document-extract",)
_PROCESS_CWD_ROOT_ID = "process-scratch"
_PROCESS_CWD_PREFIX = "working"
_PROCESS_ENVIRONMENT_PROFILE_ID = "minimal"
_ARTIFACT_SCHEMA_VERSION = 1
_SNAPSHOT_SCHEMA_VERSION = 1


@runtime_checkable
class DocumentSourceReader(Protocol):
    """Read-only, workspace-bound source boundary used by ingestion."""

    @property
    def workspace_id(self) -> str: ...

    async def read_bounded(
        self,
        relative_path: str,
        max_bytes: int,
        cancellation: CancellationToken,
    ) -> VaultRead: ...


@dataclass(frozen=True, slots=True)
class IndexedDocumentContent:
    input_block_index: int
    block: DocumentContentBlock

    def __post_init__(self) -> None:
        if isinstance(self.input_block_index, bool) or not 0 <= self.input_block_index <= 255:
            raise ValueError("document input block index must be between zero and 255")


@dataclass(slots=True)
class _AggregateBudget:
    remaining_source_bytes: int
    remaining_text_bytes: int
    remaining_context_bytes: int
    remaining_context_fragments: int

    @classmethod
    def from_config(cls, config: DocumentIngestionConfig) -> _AggregateBudget:
        return cls(
            remaining_source_bytes=config.max_total_source_bytes,
            remaining_text_bytes=config.max_total_text_bytes,
            remaining_context_bytes=config.max_total_context_bytes,
            remaining_context_fragments=config.max_context_fragments,
        )

    def consume_source(self, byte_length: int) -> bool:
        if byte_length > self.remaining_source_bytes:
            return False
        self.remaining_source_bytes -= byte_length
        return True

    def consume_text(self, byte_length: int) -> bool:
        if byte_length > self.remaining_text_bytes:
            return False
        self.remaining_text_bytes -= byte_length
        return True

    def consume_context(self, byte_length: int, fragment_count: int) -> bool:
        if byte_length > self.remaining_context_bytes or fragment_count > self.remaining_context_fragments:
            return False
        self.remaining_context_bytes -= byte_length
        self.remaining_context_fragments -= fragment_count
        return True


@dataclass(frozen=True, slots=True)
class DocumentIngestionConfig:
    """All parser-host, source, Artifact, and context ceilings for one Run."""

    parser_config_fingerprint: str
    max_documents: int = 16
    max_source_bytes_per_document: int = 32 * 1024 * 1024
    max_total_source_bytes: int = 64 * 1024 * 1024
    max_pages_per_document: int = 10_000
    max_text_bytes_per_document: int = 4 * 1024 * 1024
    max_total_text_bytes: int = 8 * 1024 * 1024
    max_provenance_bytes_per_document: int = 4 * 1024 * 1024
    max_context_fragment_bytes: int = 64 * 1024
    max_context_fragments: int = 512
    max_total_context_bytes: int = 8 * 1024 * 1024
    max_parser_response_bytes: int = 12 * 1024 * 1024
    max_parser_stderr_bytes: int = 64 * 1024
    parser_timeout_seconds: float = 120.0
    executable_profile_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if _PROFILE_SHA256.fullmatch(self.parser_config_fingerprint) is None:
            raise ValueError("parser_config_fingerprint must be a canonical SHA-256 identity")
        if (
            self.executable_profile_fingerprint is not None
            and _PROFILE_SHA256.fullmatch(self.executable_profile_fingerprint) is None
        ):
            raise ValueError("executable_profile_fingerprint must be a canonical SHA-256 identity")
        limits = {
            "max_documents": self.max_documents,
            "max_source_bytes_per_document": self.max_source_bytes_per_document,
            "max_total_source_bytes": self.max_total_source_bytes,
            "max_pages_per_document": self.max_pages_per_document,
            "max_text_bytes_per_document": self.max_text_bytes_per_document,
            "max_total_text_bytes": self.max_total_text_bytes,
            "max_provenance_bytes_per_document": self.max_provenance_bytes_per_document,
            "max_context_fragment_bytes": self.max_context_fragment_bytes,
            "max_context_fragments": self.max_context_fragments,
            "max_total_context_bytes": self.max_total_context_bytes,
            "max_parser_response_bytes": self.max_parser_response_bytes,
            "max_parser_stderr_bytes": self.max_parser_stderr_bytes,
        }
        invalid = [name for name, value in limits.items() if isinstance(value, bool) or value < 1]
        if invalid:
            raise ValueError(f"document ingestion limits must be positive integers: {', '.join(invalid)}")
        if self.max_documents > 256:
            raise ValueError("max_documents cannot exceed the turn input-block protocol limit")
        if self.max_pages_per_document > 10_000:
            raise ValueError("max_pages_per_document cannot exceed the durable event protocol limit")
        if self.max_source_bytes_per_document > self.max_total_source_bytes:
            raise ValueError("per-document source limit cannot exceed the aggregate source limit")
        if self.max_text_bytes_per_document > self.max_total_text_bytes:
            raise ValueError("per-document text limit cannot exceed the aggregate text limit")
        if self.max_total_context_bytes > self.max_total_text_bytes:
            raise ValueError("context bytes cannot exceed extracted text bytes")
        if self.max_context_fragment_bytes > self.max_total_context_bytes:
            raise ValueError("one context fragment cannot exceed the aggregate context limit")
        if self.max_context_fragment_bytes < 4:
            raise ValueError("context fragment limit must fit one UTF-8 scalar value")
        if not math.isfinite(self.parser_timeout_seconds) or self.parser_timeout_seconds <= 0:
            raise ValueError("parser timeout must be finite and positive")


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    document_id: str
    input_block_index: int
    source_byte_size: int
    text_artifact: ArtifactMetadata
    provenance_artifact: ArtifactMetadata
    context_fragments: tuple[ContextFragment, ...]
    completed_payload: DocumentExtractionCompletedPayload
    snapshot: FrozenJsonObject


@dataclass(frozen=True, slots=True)
class FailedDocument:
    document_id: str
    input_block_index: int
    failed_payload: DocumentExtractionFailedPayload


DocumentIngestionOutcome: TypeAlias = PreparedDocument | FailedDocument


@dataclass(frozen=True, slots=True)
class PreparedDocumentIngestion:
    documents: tuple[PreparedDocument, ...]
    context_fragments: tuple[ContextFragment, ...]
    snapshot: FrozenJsonObject
    started_payloads: tuple[DocumentExtractionStartedPayload, ...]
    completed_payloads: tuple[DocumentExtractionCompletedPayload, ...]
    outcomes: tuple[DocumentIngestionOutcome, ...]


class DocumentIngestionError(RuntimeError):
    """One safe terminal document failure; never contains source text or paths."""

    def __init__(
        self,
        *,
        document_id: str,
        input_block_index: int,
        failure: DocumentExtractionFailure,
    ) -> None:
        self.document_id = document_id
        self.input_block_index = input_block_index
        self.failure = failure
        super().__init__(failure.user_visible_message)

    def failure_payload(self) -> DocumentExtractionFailedPayload:
        return DocumentExtractionFailedPayload(document_id=self.document_id, attempt=1, failure=self.failure)


class DocumentIngestionBatchError(RuntimeError):
    """A closed batch containing exactly one terminal outcome per input."""

    def __init__(
        self,
        *,
        outcomes: Sequence[DocumentIngestionOutcome],
        started_payloads: Sequence[DocumentExtractionStartedPayload],
        partial: PreparedDocumentIngestion,
        infrastructure_failure: DocumentExtractionFailure | None = None,
    ) -> None:
        self.outcomes = tuple(outcomes)
        self.started_payloads = tuple(started_payloads)
        self.partial = partial
        self.infrastructure_failure = infrastructure_failure
        self.completed_payloads = tuple(
            outcome.completed_payload for outcome in self.outcomes if isinstance(outcome, PreparedDocument)
        )
        self.failed_payloads = tuple(
            outcome.failed_payload for outcome in self.outcomes if isinstance(outcome, FailedDocument)
        )
        if len(self.outcomes) != len(self.started_payloads):
            raise ValueError("document batch failures require one closed outcome per started input")
        if not self.failed_payloads and infrastructure_failure is None:
            raise ValueError("document batch error requires a document or infrastructure failure")
        super().__init__(
            infrastructure_failure.user_visible_message
            if infrastructure_failure is not None
            else "One or more attached documents could not be processed safely."
        )


class DocumentIngestionRecoveryError(RuntimeError):
    """Sanitized fail-closed rejection of a persisted extraction snapshot."""


def document_id_for_input(run_id: str, input_block_index: int, source_sha256: str) -> str:
    """Return the sole deterministic, protocol-valid document identity."""

    if not run_id or "\x00" in run_id or len(run_id) > 1024:
        raise ValueError("run_id must be a bounded non-empty identifier")
    if isinstance(input_block_index, bool) or not 0 <= input_block_index <= 255:
        raise ValueError("input_block_index must be between zero and 255")
    if _SHA256.fullmatch(source_sha256) is None:
        raise ValueError("source_sha256 must be a canonical SHA-256 identity")
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "inputBlockIndex": input_block_index,
                "runId": run_id,
                "sourceSha256": source_sha256,
            }
        )
    ).hexdigest()
    return f"doc_{digest}"


class DocumentIngestionService:
    def __init__(
        self,
        *,
        workspace_id: str,
        source_reader: DocumentSourceReader,
        process_supervisor: ProcessSupervisor,
        artifacts: ArtifactStore,
        scratch_working_root: Path,
        clock: Clock,
        config: DocumentIngestionConfig,
    ) -> None:
        if not workspace_id or "\x00" in workspace_id:
            raise ValueError("workspace_id must not be empty")
        if source_reader.workspace_id != workspace_id:
            raise ValueError("document source reader belongs to another workspace")
        if not scratch_working_root.is_absolute():
            raise ValueError("document scratch working root must be absolute")
        self._workspace_id = workspace_id
        self._source_reader = source_reader
        self._process_supervisor = process_supervisor
        self._artifacts = artifacts
        self._scratch_working_root = scratch_working_root
        self._clock = clock
        self._config = config

    async def ingest(
        self,
        *,
        run_id: str,
        documents: Sequence[IndexedDocumentContent],
        created_at: datetime,
        cancellation: CancellationToken,
    ) -> PreparedDocumentIngestion:
        """Extract every declared document and close every started outcome."""

        selected = tuple(documents)
        _validate_ingestion_call(run_id, selected, created_at)
        started = tuple(
            DocumentExtractionStartedPayload(
                document_id=document_id_for_input(
                    run_id,
                    item.input_block_index,
                    item.block.file.content_hash,
                ),
                input_block_index=item.input_block_index,
                attempt=1,
            )
            for item in selected
        )
        if not selected:
            return _prepared_batch(
                run_id=run_id,
                workspace_id=self._workspace_id,
                started=(),
                outcomes=(),
            )

        outcomes: list[DocumentIngestionOutcome] = []
        terminal_batch_failure: DocumentExtractionFailure | None = None
        root_created = False
        run_scratch = self._scratch_working_root / _run_scratch_name(run_id)
        budget = _AggregateBudget.from_config(self._config)
        infrastructure_failure: DocumentExtractionFailure | None = None

        try:
            await asyncio.to_thread(_create_run_scratch, self._scratch_working_root, run_scratch)
            root_created = True
            for ordinal, (item, started_payload) in enumerate(zip(selected, started, strict=True)):
                if terminal_batch_failure is not None:
                    outcomes.append(
                        _failed_document(
                            started_payload,
                            item.input_block_index,
                            terminal_batch_failure,
                        )
                    )
                    continue
                if ordinal >= self._config.max_documents:
                    outcomes.append(
                        _failed_document(
                            started_payload,
                            item.input_block_index,
                            _safe_failure(DocumentExtractionFailureCode.DOCUMENT_LIMIT_EXCEEDED),
                        )
                    )
                    continue
                remaining_source = budget.remaining_source_bytes
                if remaining_source < 1:
                    outcomes.append(
                        _failed_document(
                            started_payload,
                            item.input_block_index,
                            _safe_failure(DocumentExtractionFailureCode.DOCUMENT_LIMIT_EXCEEDED),
                        )
                    )
                    continue
                try:
                    prepared = await self._ingest_one(
                        run_id=run_id,
                        item=item,
                        document_id=started_payload.document_id,
                        ordinal=ordinal,
                        run_scratch=run_scratch,
                        created_at=created_at,
                        maximum_source_bytes=min(
                            self._config.max_source_bytes_per_document,
                            remaining_source,
                        ),
                        budget=budget,
                        cancellation=cancellation,
                    )
                except DocumentIngestionError as error:
                    failed = FailedDocument(
                        document_id=error.document_id,
                        input_block_index=error.input_block_index,
                        failed_payload=error.failure_payload(),
                    )
                    outcomes.append(failed)
                    if error.failure.code in {
                        DocumentExtractionFailureCode.CANCELLED,
                        DocumentExtractionFailureCode.DEADLINE_EXCEEDED,
                    }:
                        terminal_batch_failure = error.failure
                    continue
                outcomes.append(prepared)
        except OperationCancelled:
            terminal_batch_failure = _safe_failure(DocumentExtractionFailureCode.CANCELLED)
        except BaseException:
            infrastructure_failure = _safe_failure(DocumentExtractionFailureCode.PARSER_FAILED)
        finally:
            if root_created:
                try:
                    await asyncio.to_thread(_remove_secure_tree, run_scratch)
                except BaseException:
                    infrastructure_failure = _safe_failure(DocumentExtractionFailureCode.PARSER_FAILED)

        if len(outcomes) < len(selected):
            failure = (
                terminal_batch_failure
                or infrastructure_failure
                or _safe_failure(DocumentExtractionFailureCode.PARSER_FAILED)
            )
            for item, started_payload in zip(selected[len(outcomes) :], started[len(outcomes) :], strict=True):
                outcomes.append(_failed_document(started_payload, item.input_block_index, failure))

        prepared_batch = _prepared_batch(
            run_id=run_id,
            workspace_id=self._workspace_id,
            started=started,
            outcomes=outcomes,
        )
        has_failed_outcome = any(isinstance(outcome, FailedDocument) for outcome in outcomes)
        if has_failed_outcome or infrastructure_failure is not None:
            raise DocumentIngestionBatchError(
                outcomes=outcomes,
                started_payloads=started,
                partial=prepared_batch,
                infrastructure_failure=infrastructure_failure,
            )
        return prepared_batch

    async def restore(
        self,
        *,
        run_id: str,
        documents: Sequence[IndexedDocumentContent],
        snapshot: Mapping[str, Any],
        cancellation: CancellationToken,
    ) -> PreparedDocumentIngestion:
        """Rebuild prepared context only from a durable proof and immutable Artifacts."""

        selected = tuple(documents)
        _validate_ingestion_call(run_id, selected, self._clock.utcnow())
        cancellation.checkpoint()
        try:
            materialized = thaw_json(snapshot)
            root = _expect_exact_object(
                materialized,
                {"schemaVersion", "runId", "workspaceId", "documents"},
            )
            if (
                root["schemaVersion"] != _SNAPSHOT_SCHEMA_VERSION
                or root["runId"] != run_id
                or root["workspaceId"] != self._workspace_id
            ):
                raise ValueError("document recovery root identity drifted")
            document_snapshots = _expect_list(root["documents"])
            if len(document_snapshots) != len(selected) or len(selected) > self._config.max_documents:
                raise ValueError("document recovery input cardinality drifted")

            started = tuple(
                DocumentExtractionStartedPayload(
                    document_id=document_id_for_input(
                        run_id,
                        item.input_block_index,
                        item.block.file.content_hash,
                    ),
                    input_block_index=item.input_block_index,
                    attempt=1,
                )
                for item in selected
            )
            budget = _AggregateBudget.from_config(self._config)
            recovered: list[PreparedDocument] = []
            for item, started_payload, document_snapshot in zip(
                selected,
                started,
                document_snapshots,
                strict=True,
            ):
                cancellation.checkpoint()
                recovered.append(
                    await self._restore_one(
                        run_id=run_id,
                        item=item,
                        document_id=started_payload.document_id,
                        snapshot=document_snapshot,
                        budget=budget,
                        cancellation=cancellation,
                    )
                )
            prepared = _prepared_batch(
                run_id=run_id,
                workspace_id=self._workspace_id,
                started=started,
                outcomes=recovered,
            )
            if thaw_json(prepared.snapshot) != root:
                raise ValueError("document recovery proof changed during reconstruction")
            cancellation.checkpoint()
            return prepared
        except OperationCancelled:
            raise
        except DocumentIngestionRecoveryError:
            raise
        except Exception as error:
            raise DocumentIngestionRecoveryError(
                "Persisted document extraction could not be restored safely."
            ) from error

    async def _restore_one(
        self,
        *,
        run_id: str,
        item: IndexedDocumentContent,
        document_id: str,
        snapshot: object,
        budget: _AggregateBudget,
        cancellation: CancellationToken,
    ) -> PreparedDocument:
        value = _expect_exact_object(
            snapshot,
            {
                "schemaVersion",
                "documentId",
                "inputBlockIndex",
                "source",
                "sourceByteSize",
                "parserConfigFingerprint",
                "pageCount",
                "textArtifactId",
                "textSha256",
                "provenanceArtifactId",
                "provenanceSha256",
                "contextFragmentIds",
            },
        )
        expected_text_id = _artifact_id(run_id, document_id, "text")
        expected_provenance_id = _artifact_id(run_id, document_id, "provenance")
        source = _expect_exact_object(value["source"], {"workspaceId", "path", "sha256", "mediaType"})
        source_size = _expect_integer(value["sourceByteSize"], minimum=1)
        page_count = _expect_integer(value["pageCount"], minimum=1)
        expected_source = {
            "workspaceId": item.block.file.workspace_id,
            "path": item.block.file.path,
            "sha256": item.block.file.content_hash,
            "mediaType": item.block.media_type.value,
        }
        if (
            value["schemaVersion"] != _SNAPSHOT_SCHEMA_VERSION
            or value["documentId"] != document_id
            or value["inputBlockIndex"] != item.input_block_index
            or source != expected_source
            or value["parserConfigFingerprint"] != self._config.parser_config_fingerprint
            or value["textArtifactId"] != expected_text_id
            or value["provenanceArtifactId"] != expected_provenance_id
            or _SHA256.fullmatch(_expect_string(value["textSha256"])) is None
            or _SHA256.fullmatch(_expect_string(value["provenanceSha256"])) is None
            or source_size > self._config.max_source_bytes_per_document
            or page_count > self._config.max_pages_per_document
            or not budget.consume_source(source_size)
        ):
            raise ValueError("document recovery snapshot drifted from its explicit input")

        text_metadata = await self._artifacts.metadata(expected_text_id)
        provenance_metadata = await self._artifacts.metadata(expected_provenance_id)
        if text_metadata is None or provenance_metadata is None:
            raise ValueError("document recovery Artifact is missing")
        _validate_recovered_artifact_metadata(
            text_metadata,
            artifact_id=expected_text_id,
            workspace_id=self._workspace_id,
            run_id=run_id,
            mime_type="text/plain; charset=utf-8",
            sha256=_expect_string(value["textSha256"]),
            maximum_bytes=self._config.max_text_bytes_per_document,
            attributes={
                "schemaVersion": _ARTIFACT_SCHEMA_VERSION,
                "kind": "document_extracted_text",
                "documentId": document_id,
                "sourceSha256": item.block.file.content_hash,
                "parserConfigFingerprint": self._config.parser_config_fingerprint,
                "provenanceArtifactId": expected_provenance_id,
            },
        )
        _validate_recovered_artifact_metadata(
            provenance_metadata,
            artifact_id=expected_provenance_id,
            workspace_id=self._workspace_id,
            run_id=run_id,
            mime_type="application/vnd.offeragent.document-provenance+json",
            sha256=_expect_string(value["provenanceSha256"]),
            maximum_bytes=self._config.max_provenance_bytes_per_document,
            attributes={
                "schemaVersion": _ARTIFACT_SCHEMA_VERSION,
                "kind": "document_extraction_provenance",
                "documentId": document_id,
                "sourceSha256": item.block.file.content_hash,
                "parserConfigFingerprint": self._config.parser_config_fingerprint,
                "textArtifactId": expected_text_id,
            },
        )
        if not budget.consume_text(text_metadata.byte_length):
            raise ValueError("document recovery text budget drifted")
        text_bytes = await _read_complete_artifact(
            self._artifacts,
            text_metadata,
            maximum_bytes=self._config.max_text_bytes_per_document,
            cancellation=cancellation,
        )
        provenance_bytes = await _read_complete_artifact(
            self._artifacts,
            provenance_metadata,
            maximum_bytes=self._config.max_provenance_bytes_per_document,
            cancellation=cancellation,
        )
        fragments, completed_payload = _restore_provenance_and_context(
            document_id=document_id,
            block=item.block,
            source_byte_size=source_size,
            page_count=page_count,
            text_bytes=text_bytes,
            provenance_bytes=provenance_bytes,
            artifact_ids=(expected_text_id, expected_provenance_id),
            maximum_fragment_bytes=self._config.max_context_fragment_bytes,
            parser_config_fingerprint=self._config.parser_config_fingerprint,
        )
        context_bytes = sum(len(fragment.text.encode("utf-8", errors="strict")) for fragment in fragments)
        if not budget.consume_context(context_bytes, len(fragments)):
            raise ValueError("document recovery context budget drifted")
        fragment_ids = [_expect_string(item) for item in _expect_list(value["contextFragmentIds"])]
        if fragment_ids != [fragment.fragment_id for fragment in fragments]:
            raise ValueError("document recovery context identities drifted")
        frozen_snapshot = freeze_json(value)
        if not isinstance(frozen_snapshot, FrozenJsonObject):
            raise TypeError("document recovery snapshot must be an object")
        return PreparedDocument(
            document_id=document_id,
            input_block_index=item.input_block_index,
            source_byte_size=source_size,
            text_artifact=text_metadata,
            provenance_artifact=provenance_metadata,
            context_fragments=fragments,
            completed_payload=completed_payload,
            snapshot=frozen_snapshot,
        )

    async def _ingest_one(
        self,
        *,
        run_id: str,
        item: IndexedDocumentContent,
        document_id: str,
        ordinal: int,
        run_scratch: Path,
        created_at: datetime,
        maximum_source_bytes: int,
        budget: _AggregateBudget,
        cancellation: CancellationToken,
    ) -> PreparedDocument:
        if item.block.file.workspace_id != self._workspace_id:
            raise self._error(item, document_id, DocumentExtractionFailureCode.SOURCE_INTEGRITY_MISMATCH)
        try:
            cancellation.checkpoint()
            source = await self._source_reader.read_bounded(
                item.block.file.path,
                maximum_source_bytes,
                cancellation,
            )
        except OperationCancelled as error:
            raise self._error(item, document_id, DocumentExtractionFailureCode.CANCELLED) from error
        except FileNotFoundError as error:
            raise self._error(item, document_id, DocumentExtractionFailureCode.SOURCE_NOT_FOUND) from error
        except BaseException as error:
            raise self._error(item, document_id, DocumentExtractionFailureCode.SOURCE_UNREADABLE) from error
        try:
            source_bytes = _verify_source(
                source,
                expected_path=item.block.file.path,
                expected_hash=item.block.file.content_hash,
                media_type=item.block.media_type,
                maximum_bytes=maximum_source_bytes,
            )
        except _SourceLimitError as error:
            raise self._error(item, document_id, DocumentExtractionFailureCode.DOCUMENT_LIMIT_EXCEEDED) from error
        except _UnsupportedSourceError as error:
            raise self._error(item, document_id, DocumentExtractionFailureCode.UNSUPPORTED_MEDIA_TYPE) from error
        except BaseException as error:
            raise self._error(item, document_id, DocumentExtractionFailureCode.SOURCE_INTEGRITY_MISMATCH) from error
        if not budget.consume_source(len(source_bytes)):
            raise self._error(item, document_id, DocumentExtractionFailureCode.DOCUMENT_LIMIT_EXCEEDED)

        document_scratch = run_scratch / f"document-{ordinal:03d}"
        staged_source = document_scratch / "source.bin"
        decoded: CanonicalDocumentResult | None = None
        ingestion_error: DocumentIngestionError | None = None
        try:
            await asyncio.to_thread(_stage_source, document_scratch, staged_source, source_bytes)
            decoded = await self._execute_parser(
                run_id=run_id,
                document_id=document_id,
                item=item,
                staged_source=staged_source,
                run_scratch=run_scratch,
                source_byte_size=len(source_bytes),
                cancellation=cancellation,
            )
        except DocumentIngestionError as error:
            ingestion_error = error
        except OperationCancelled:
            ingestion_error = self._error(item, document_id, DocumentExtractionFailureCode.CANCELLED)
        except BaseException:
            ingestion_error = self._error(item, document_id, DocumentExtractionFailureCode.PARSER_FAILED)
        finally:
            try:
                if _path_exists_no_follow(document_scratch):
                    await asyncio.to_thread(_remove_secure_tree, document_scratch)
            except BaseException:
                ingestion_error = self._error(item, document_id, DocumentExtractionFailureCode.PARSER_FAILED)
        if ingestion_error is not None:
            raise ingestion_error
        assert decoded is not None

        text_bytes = decoded.text.encode("utf-8", errors="strict")
        if not any(page.page.text for page in decoded.pages):
            raise self._error(item, document_id, DocumentExtractionFailureCode.NO_EXTRACTABLE_TEXT)
        if len(text_bytes) > self._config.max_text_bytes_per_document or not budget.consume_text(len(text_bytes)):
            raise self._error(item, document_id, DocumentExtractionFailureCode.DOCUMENT_LIMIT_EXCEEDED)
        if len(decoded.pages) > self._config.max_pages_per_document:
            raise self._error(item, document_id, DocumentExtractionFailureCode.DOCUMENT_LIMIT_EXCEEDED)

        text_artifact_id = _artifact_id(run_id, document_id, "text")
        provenance_artifact_id = _artifact_id(run_id, document_id, "provenance")
        provenance_bytes, completed_payload = _provenance_and_completed(
            document_id=document_id,
            result=decoded,
            block=item.block,
            text_artifact_id=text_artifact_id,
        )
        if len(provenance_bytes) > self._config.max_provenance_bytes_per_document:
            raise self._error(item, document_id, DocumentExtractionFailureCode.DOCUMENT_LIMIT_EXCEEDED)

        fragments = _context_fragments(
            document_id=document_id,
            result=decoded,
            artifact_ids=(text_artifact_id, provenance_artifact_id),
            maximum_fragment_bytes=self._config.max_context_fragment_bytes,
        )
        context_bytes = sum(len(fragment.text.encode("utf-8", errors="strict")) for fragment in fragments)
        if context_bytes != sum(
            len(page.page.text.encode("utf-8", errors="strict")) for page in decoded.pages
        ) or not budget.consume_context(context_bytes, len(fragments)):
            raise self._error(item, document_id, DocumentExtractionFailureCode.DOCUMENT_LIMIT_EXCEEDED)
        text_metadata = ArtifactMetadata(
            artifact_id=text_artifact_id,
            workspace_id=self._workspace_id,
            owner_run_id=run_id,
            mime_type="text/plain; charset=utf-8",
            byte_length=len(text_bytes),
            sha256=_sha256(text_bytes),
            sensitivity=Sensitivity.WORKSPACE,
            state=ArtifactState.COMPLETE,
            created_at=created_at,
            attributes={
                "schemaVersion": _ARTIFACT_SCHEMA_VERSION,
                "kind": "document_extracted_text",
                "documentId": document_id,
                "sourceSha256": decoded.source_sha256,
                "parserConfigFingerprint": decoded.parser_config_fingerprint,
                "provenanceArtifactId": provenance_artifact_id,
            },
        )
        provenance_metadata = ArtifactMetadata(
            artifact_id=provenance_artifact_id,
            workspace_id=self._workspace_id,
            owner_run_id=run_id,
            mime_type="application/vnd.offeragent.document-provenance+json",
            byte_length=len(provenance_bytes),
            sha256=_sha256(provenance_bytes),
            sensitivity=Sensitivity.WORKSPACE,
            state=ArtifactState.COMPLETE,
            created_at=created_at,
            attributes={
                "schemaVersion": _ARTIFACT_SCHEMA_VERSION,
                "kind": "document_extraction_provenance",
                "documentId": document_id,
                "sourceSha256": decoded.source_sha256,
                "parserConfigFingerprint": decoded.parser_config_fingerprint,
                "textArtifactId": text_artifact_id,
            },
        )
        try:
            cancellation.checkpoint()
            stored_text = await self._artifacts.put(
                text_metadata,
                text_bytes,
                idempotency_key=_artifact_idempotency_key(run_id, document_id, "text"),
            )
            if stored_text != text_metadata:
                raise ValueError("Artifact Store returned different text metadata")
            cancellation.checkpoint()
            stored_provenance = await self._artifacts.put(
                provenance_metadata,
                provenance_bytes,
                idempotency_key=_artifact_idempotency_key(run_id, document_id, "provenance"),
            )
            if stored_provenance != provenance_metadata:
                raise ValueError("Artifact Store returned different provenance metadata")
        except OperationCancelled as error:
            raise self._error(item, document_id, DocumentExtractionFailureCode.CANCELLED) from error
        except BaseException as error:
            raise self._error(item, document_id, DocumentExtractionFailureCode.OUTPUT_INTEGRITY_MISMATCH) from error

        snapshot_value = freeze_json(
            {
                "schemaVersion": _SNAPSHOT_SCHEMA_VERSION,
                "documentId": document_id,
                "inputBlockIndex": item.input_block_index,
                "source": {
                    "workspaceId": item.block.file.workspace_id,
                    "path": item.block.file.path,
                    "sha256": item.block.file.content_hash,
                    "mediaType": item.block.media_type.value,
                },
                "sourceByteSize": decoded.source_byte_size,
                "parserConfigFingerprint": decoded.parser_config_fingerprint,
                "pageCount": len(decoded.pages),
                "textArtifactId": text_artifact_id,
                "textSha256": text_metadata.sha256,
                "provenanceArtifactId": provenance_artifact_id,
                "provenanceSha256": provenance_metadata.sha256,
                "contextFragmentIds": [fragment.fragment_id for fragment in fragments],
            }
        )
        assert isinstance(snapshot_value, FrozenJsonObject)
        canonical_json_bytes(snapshot_value)
        return PreparedDocument(
            document_id=document_id,
            input_block_index=item.input_block_index,
            source_byte_size=decoded.source_byte_size,
            text_artifact=stored_text,
            provenance_artifact=stored_provenance,
            context_fragments=fragments,
            completed_payload=completed_payload,
            snapshot=snapshot_value,
        )

    async def _execute_parser(
        self,
        *,
        run_id: str,
        document_id: str,
        item: IndexedDocumentContent,
        staged_source: Path,
        run_scratch: Path,
        source_byte_size: int,
        cancellation: CancellationToken,
    ) -> CanonicalDocumentResult:
        request_id = _request_id(run_id, document_id)
        request_payload = encode_canonical_request(
            DocumentParseRequest(
                request_id=request_id,
                source=DocumentSource(
                    source_id=document_id,
                    absolute_path=staged_source,
                    declared_media_type=ParserDocumentMediaType(item.block.media_type.value),
                    expected_sha256=item.block.file.content_hash,
                ),
            )
        )
        cwd = f"{_PROCESS_CWD_PREFIX}/{run_scratch.name}"
        try:
            result = await self._process_supervisor.execute(
                SupervisedProcessRequest(
                    process_id=_process_id(run_id, document_id),
                    executable_id=_PROCESS_EXECUTABLE_ID,
                    arguments=_PROCESS_ARGUMENTS,
                    stdin=request_payload,
                    environment={},
                    deadline=self._clock.utcnow() + timedelta(seconds=self._config.parser_timeout_seconds),
                    stdout_limit_bytes=self._config.max_parser_response_bytes,
                    stderr_limit_bytes=self._config.max_parser_stderr_bytes,
                    # This personal local build runs the fixed, hash-pinned parser
                    # as the current Windows user.  It deliberately does not use
                    # the AppContainer path or expose a configuration switch.
                    allow_network=True,
                    owner_kind=ProcessOwnerKind.PARSER,
                    owner_run_id=run_id,
                    workspace_id=self._workspace_id,
                    cwd_root_id=_PROCESS_CWD_ROOT_ID,
                    cwd=cwd,
                    environment_profile_id=_PROCESS_ENVIRONMENT_PROFILE_ID,
                    stdin_mode=ProcessStdinMode.FIXED_PAYLOAD,
                    artifact_limit_bytes=max(
                        self._config.max_parser_response_bytes,
                        self._config.max_parser_stderr_bytes,
                    ),
                    allow_artifact_spill=False,
                    executable_profile_fingerprint=self._config.executable_profile_fingerprint,
                ),
                cancellation,
            )
        except OperationCancelled as error:
            raise self._error(item, document_id, DocumentExtractionFailureCode.CANCELLED) from error
        except BaseException as error:
            raise self._error(item, document_id, DocumentExtractionFailureCode.PARSER_UNAVAILABLE) from error
        if result.timed_out or result.lifecycle_state is ProcessLifecycleState.TIMED_OUT:
            raise self._error(item, document_id, DocumentExtractionFailureCode.DEADLINE_EXCEEDED)
        if (
            result.output_truncated
            or result.stdout_artifact_id is not None
            or result.stderr_artifact_id is not None
            or result.stdout_encoding is not ProcessOutputEncoding.UTF8
            or result.stdout_total_bytes != len(result.stdout)
            or result.stderr_total_bytes != len(result.stderr)
            or len(result.stdout) > self._config.max_parser_response_bytes
            or len(result.stderr) > self._config.max_parser_stderr_bytes
        ):
            raise self._error(item, document_id, DocumentExtractionFailureCode.OUTPUT_INTEGRITY_MISMATCH)
        if result.exit_code != 0:
            raise self._error(item, document_id, DocumentExtractionFailureCode.PARSER_FAILED)
        try:
            response = decode_canonical_response(result.stdout)
        except DocumentParseError as error:
            raise self._error(item, document_id, DocumentExtractionFailureCode.OUTPUT_INTEGRITY_MISMATCH) from error
        if response.request_id != request_id:
            raise self._error(item, document_id, DocumentExtractionFailureCode.OUTPUT_INTEGRITY_MISMATCH)
        if isinstance(response, CanonicalParseFailure):
            raise self._error(item, document_id, _map_parser_failure(response.code))
        assert isinstance(response, CanonicalParseSuccess)
        parsed = response.result
        if (
            parsed.source_id != document_id
            or parsed.source_sha256 != item.block.file.content_hash
            or parsed.media_type.value != item.block.media_type.value
            or parsed.source_byte_size != source_byte_size
            or parsed.parser_config_fingerprint != self._config.parser_config_fingerprint
        ):
            raise self._error(item, document_id, DocumentExtractionFailureCode.OUTPUT_INTEGRITY_MISMATCH)
        if len(parsed.pages) > self._config.max_pages_per_document:
            raise self._error(item, document_id, DocumentExtractionFailureCode.DOCUMENT_LIMIT_EXCEEDED)
        return parsed

    @staticmethod
    def _error(
        item: IndexedDocumentContent,
        document_id: str,
        code: DocumentExtractionFailureCode,
    ) -> DocumentIngestionError:
        return DocumentIngestionError(
            document_id=document_id,
            input_block_index=item.input_block_index,
            failure=_safe_failure(code),
        )


class _SourceLimitError(ValueError):
    pass


class _UnsupportedSourceError(ValueError):
    pass


def _validate_ingestion_call(
    run_id: str,
    documents: Sequence[IndexedDocumentContent],
    created_at: datetime,
) -> None:
    if not run_id or "\x00" in run_id or len(run_id) > 1024:
        raise ValueError("run_id must be a bounded non-empty identifier")
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    indices = [item.input_block_index for item in documents]
    if len(indices) != len(set(indices)):
        raise ValueError("document input block indices must be unique")


def _expect_exact_object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("document recovery object shape is invalid")
    return dict(value)


def _expect_list(value: object) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("document recovery array is invalid")
    return list(value)


def _expect_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("document recovery string is invalid")
    return value


def _expect_integer(value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("document recovery integer is invalid")
    return value


def _validate_recovered_artifact_metadata(
    metadata: ArtifactMetadata,
    *,
    artifact_id: str,
    workspace_id: str,
    run_id: str,
    mime_type: str,
    sha256: str,
    maximum_bytes: int,
    attributes: Mapping[str, Any],
) -> None:
    if (
        metadata.artifact_id != artifact_id
        or metadata.workspace_id != workspace_id
        or metadata.owner_run_id != run_id
        or metadata.mime_type != mime_type
        or metadata.byte_length < 1
        or metadata.byte_length > maximum_bytes
        or metadata.sha256 != sha256
        or metadata.sensitivity is not Sensitivity.WORKSPACE
        or metadata.state is not ArtifactState.COMPLETE
        or thaw_json(metadata.attributes) != dict(attributes)
    ):
        raise ValueError("document recovery Artifact metadata drifted")


async def _read_complete_artifact(
    artifacts: ArtifactStore,
    metadata: ArtifactMetadata,
    *,
    maximum_bytes: int,
    cancellation: CancellationToken,
) -> bytes:
    if metadata.byte_length > maximum_bytes:
        raise ValueError("document recovery Artifact exceeds its configured limit")
    chunks: list[bytes] = []
    byte_length = 0
    async for chunk in artifacts.read(metadata.artifact_id, limit=metadata.byte_length):
        cancellation.checkpoint()
        if not isinstance(chunk, bytes) or byte_length + len(chunk) > metadata.byte_length:
            raise ValueError("document recovery Artifact stream is invalid")
        byte_length += len(chunk)
        chunks.append(chunk)
    payload = b"".join(chunks)
    if byte_length != metadata.byte_length or _sha256(payload) != metadata.sha256:
        raise ValueError("document recovery Artifact integrity check failed")
    cancellation.checkpoint()
    return payload


def _restore_provenance_and_context(
    *,
    document_id: str,
    block: DocumentContentBlock,
    source_byte_size: int,
    page_count: int,
    text_bytes: bytes,
    provenance_bytes: bytes,
    artifact_ids: tuple[str, str],
    maximum_fragment_bytes: int,
    parser_config_fingerprint: str,
) -> tuple[tuple[ContextFragment, ...], DocumentExtractionCompletedPayload]:
    try:
        text_bytes.decode("utf-8", errors="strict")
        decoded = json.loads(provenance_bytes.decode("utf-8", errors="strict"))
        if canonical_json_bytes(decoded) != provenance_bytes:
            raise ValueError("document recovery provenance is not canonical JSON")
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("document recovery provenance is invalid") from error
    provenance = _expect_exact_object(
        decoded,
        {
            "schemaVersion",
            "documentId",
            "source",
            "parserConfigFingerprint",
            "textSha256",
            "pageCount",
            "pages",
        },
    )
    source = _expect_exact_object(
        provenance["source"],
        {"workspaceId", "path", "sha256", "mediaType", "byteSize"},
    )
    if (
        provenance["schemaVersion"] != _ARTIFACT_SCHEMA_VERSION
        or provenance["documentId"] != document_id
        or provenance["parserConfigFingerprint"] != parser_config_fingerprint
        or provenance["textSha256"] != _sha256(text_bytes)
        or provenance["pageCount"] != page_count
        or source
        != {
            "workspaceId": block.file.workspace_id,
            "path": block.file.path,
            "sha256": block.file.content_hash,
            "mediaType": block.media_type.value,
            "byteSize": source_byte_size,
        }
    ):
        raise ValueError("document recovery provenance identity drifted")
    pages = _expect_list(provenance["pages"])
    if len(pages) != page_count:
        raise ValueError("document recovery provenance page count drifted")

    fragments: list[ContextFragment] = []
    event_pages: list[DocumentPageProvenance] = []
    warnings: list[DocumentExtractionWarning] = []
    cursor = 0
    for index, raw_page in enumerate(pages, start=1):
        page = _expect_exact_object(
            raw_page,
            {
                "pageNumber",
                "utf8StartByte",
                "utf8EndByte",
                "extractionMethod",
                "inputBackend",
                "extractionBackend",
                "ocrRegions",
            },
        )
        if index > 1:
            if text_bytes[cursor : cursor + 2] != b"\n\n":
                raise ValueError("document recovery page separator drifted")
            cursor += 2
        start = _expect_integer(page["utf8StartByte"])
        end = _expect_integer(page["utf8EndByte"])
        if page["pageNumber"] != index or start != cursor or end < start or end > len(text_bytes):
            raise ValueError("document recovery page span drifted")
        try:
            page_text = text_bytes[start:end].decode("utf-8", errors="strict")
            extraction_method = DocumentExtractionMethod(_expect_string(page["extractionMethod"]))
        except (UnicodeError, ValueError) as error:
            raise ValueError("document recovery page is invalid") from error
        cursor = end
        locator = DocumentPageLocator(type="page", page_start=index, page_end=index)
        if page_text:
            event_pages.append(
                DocumentPageProvenance(
                    locator=locator,
                    extraction_method=extraction_method,
                    utf8_start_byte=start,
                    utf8_end_byte=end,
                    confidence=None,
                )
            )
        else:
            warnings.append(
                DocumentExtractionWarning(
                    code=DocumentExtractionWarningCode.PAGE_NO_TEXT,
                    user_visible_message="No text was extracted from this page.",
                    locator=locator,
                )
            )
        for chunk_index, chunk in enumerate(_utf8_chunks(page_text, maximum_fragment_bytes), start=1):
            fragments.append(
                ContextFragment(
                    fragment_id=f"attachment-{document_id}-p{index}-c{chunk_index}",
                    layer=ContextLayer.USER_INPUT,
                    text=chunk,
                    sensitivity=Sensitivity.WORKSPACE,
                    source_refs=(f"document:{document_id}:page:{index}",),
                    artifact_ids=artifact_ids,
                    content_hash=_sha256(chunk.encode("utf-8", errors="strict")),
                )
            )
    if cursor != len(text_bytes) or not event_pages:
        raise ValueError("document recovery text does not match page provenance")
    completed = DocumentExtractionCompletedPayload(
        document_id=document_id,
        attempt=1,
        text_artifact_id=artifact_ids[0],
        page_count=page_count,
        page_provenance=event_pages,
        warnings=warnings,
    )
    return tuple(fragments), completed


def _verify_source(
    source: VaultRead,
    *,
    expected_path: str,
    expected_hash: str,
    media_type: DocumentMediaType,
    maximum_bytes: int,
) -> bytes:
    if source.entry.kind is not VaultEntryKind.FILE or source.entry.relative_path != expected_path:
        raise ValueError("source entry identity drifted")
    if source.entry.size > maximum_bytes:
        raise _SourceLimitError("source exceeds configured limit")
    if source.truncated or source.entry.size != len(source.content) or len(source.content) > maximum_bytes:
        raise ValueError("source read was not complete")
    digest = _sha256(source.content)
    if source.entry.content_hash != expected_hash or digest != expected_hash:
        raise ValueError("source hash does not match immutable reference")
    detected = _media_type_from_magic(source.content)
    if detected is not media_type:
        raise _UnsupportedSourceError("declared media type does not match file signature")
    return bytes(source.content)


def _media_type_from_magic(content: bytes) -> DocumentMediaType:
    if content.startswith(b"%PDF-"):
        return DocumentMediaType.PDF
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return DocumentMediaType.PNG
    if content.startswith(b"\xff\xd8\xff"):
        return DocumentMediaType.JPEG
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return DocumentMediaType.WEBP
    raise _UnsupportedSourceError("source signature is unsupported")


def _provenance_and_completed(
    *,
    document_id: str,
    result: CanonicalDocumentResult,
    block: DocumentContentBlock,
    text_artifact_id: str,
) -> tuple[bytes, DocumentExtractionCompletedPayload]:
    pages: list[dict[str, object]] = []
    event_pages: list[DocumentPageProvenance] = []
    warnings: list[DocumentExtractionWarning] = []
    for item in result.pages:
        page = item.page
        regions = [
            {
                "polygon": [point.to_json() for point in region.polygon],
                "confidence": region.confidence,
            }
            for region in page.ocr_regions
        ]
        pages.append(
            {
                "pageNumber": page.provenance.page_number,
                "utf8StartByte": item.utf8_start_byte,
                "utf8EndByte": item.utf8_end_byte,
                "extractionMethod": page.extraction_method.value,
                "inputBackend": page.provenance.input_backend.to_json(),
                "extractionBackend": page.provenance.extraction_backend.to_json(),
                "ocrRegions": regions,
            }
        )
        locator = DocumentPageLocator(
            type="page",
            page_start=page.provenance.page_number,
            page_end=page.provenance.page_number,
        )
        if item.utf8_end_byte == item.utf8_start_byte:
            warnings.append(
                DocumentExtractionWarning(
                    code=DocumentExtractionWarningCode.PAGE_NO_TEXT,
                    user_visible_message="No text was extracted from this page.",
                    locator=locator,
                )
            )
        else:
            event_pages.append(
                DocumentPageProvenance(
                    locator=locator,
                    extraction_method=DocumentExtractionMethod(page.extraction_method.value),
                    utf8_start_byte=item.utf8_start_byte,
                    utf8_end_byte=item.utf8_end_byte,
                    confidence=None,
                )
            )
    if not event_pages:
        raise ValueError("document has no non-empty provenance spans")
    provenance = canonical_json_bytes(
        {
            "schemaVersion": _ARTIFACT_SCHEMA_VERSION,
            "documentId": document_id,
            "source": {
                "workspaceId": block.file.workspace_id,
                "path": block.file.path,
                "sha256": result.source_sha256,
                "mediaType": result.media_type.value,
                "byteSize": result.source_byte_size,
            },
            "parserConfigFingerprint": result.parser_config_fingerprint,
            "textSha256": _sha256(result.text.encode("utf-8", errors="strict")),
            "pageCount": len(result.pages),
            "pages": pages,
        }
    )
    completed = DocumentExtractionCompletedPayload(
        document_id=document_id,
        attempt=1,
        text_artifact_id=text_artifact_id,
        page_count=len(result.pages),
        page_provenance=event_pages,
        warnings=warnings,
    )
    return provenance, completed


def _context_fragments(
    *,
    document_id: str,
    result: CanonicalDocumentResult,
    artifact_ids: tuple[str, str],
    maximum_fragment_bytes: int,
) -> tuple[ContextFragment, ...]:
    fragments: list[ContextFragment] = []
    for page_item in result.pages:
        page = page_item.page
        chunks = _utf8_chunks(page.text, maximum_fragment_bytes)
        for chunk_index, chunk in enumerate(chunks, start=1):
            chunk_bytes = chunk.encode("utf-8", errors="strict")
            fragments.append(
                ContextFragment(
                    fragment_id=f"attachment-{document_id}-p{page.provenance.page_number}-c{chunk_index}",
                    layer=ContextLayer.USER_INPUT,
                    text=chunk,
                    sensitivity=Sensitivity.WORKSPACE,
                    source_refs=(f"document:{document_id}:page:{page.provenance.page_number}",),
                    artifact_ids=artifact_ids,
                    content_hash=_sha256(chunk_bytes),
                )
            )
    return tuple(fragments)


def _utf8_chunks(text: str, maximum_bytes: int) -> tuple[str, ...]:
    if not text:
        return ()
    chunks: list[str] = []
    characters: list[str] = []
    used = 0
    for character in text:
        encoded = character.encode("utf-8", errors="strict")
        if characters and used + len(encoded) > maximum_bytes:
            chunks.append("".join(characters))
            characters = []
            used = 0
        if len(encoded) > maximum_bytes:
            raise ValueError("one UTF-8 scalar exceeds the configured context fragment limit")
        characters.append(character)
        used += len(encoded)
    if characters:
        chunks.append("".join(characters))
    if "".join(chunks) != text:
        raise AssertionError("context chunking must preserve the full source text")
    return tuple(chunks)


def _prepared_batch(
    *,
    run_id: str,
    workspace_id: str,
    started: Sequence[DocumentExtractionStartedPayload],
    outcomes: Sequence[DocumentIngestionOutcome],
) -> PreparedDocumentIngestion:
    prepared = tuple(outcome for outcome in outcomes if isinstance(outcome, PreparedDocument))
    fragments = tuple(fragment for document in prepared for fragment in document.context_fragments)
    snapshot = freeze_json(
        {
            "schemaVersion": _SNAPSHOT_SCHEMA_VERSION,
            "runId": run_id,
            "workspaceId": workspace_id,
            "documents": [dict(document.snapshot) for document in prepared],
        }
    )
    assert isinstance(snapshot, FrozenJsonObject)
    canonical_json_bytes(snapshot)
    return PreparedDocumentIngestion(
        documents=prepared,
        context_fragments=fragments,
        snapshot=snapshot,
        started_payloads=tuple(started),
        completed_payloads=tuple(document.completed_payload for document in prepared),
        outcomes=tuple(outcomes),
    )


def _failed_document(
    started: DocumentExtractionStartedPayload,
    input_block_index: int,
    failure: DocumentExtractionFailure,
) -> FailedDocument:
    return FailedDocument(
        document_id=started.document_id,
        input_block_index=input_block_index,
        failed_payload=DocumentExtractionFailedPayload(
            document_id=started.document_id,
            attempt=started.attempt,
            failure=failure,
        ),
    )


def _safe_failure(code: DocumentExtractionFailureCode) -> DocumentExtractionFailure:
    message, retryable = _SAFE_FAILURES[code]
    return DocumentExtractionFailure(
        code=code,
        retryable=retryable,
        user_visible_message=message,
        details={},
    )


_SAFE_FAILURES: Mapping[DocumentExtractionFailureCode, tuple[str, bool]] = {
    DocumentExtractionFailureCode.SOURCE_NOT_FOUND: ("The attached document is no longer available.", False),
    DocumentExtractionFailureCode.SOURCE_UNREADABLE: ("The attached document could not be read safely.", True),
    DocumentExtractionFailureCode.SOURCE_INTEGRITY_MISMATCH: (
        "The attached document changed or failed its integrity check.",
        False,
    ),
    DocumentExtractionFailureCode.UNSUPPORTED_MEDIA_TYPE: (
        "The attached file is not a supported PDF, PNG, JPEG, or WebP document.",
        False,
    ),
    DocumentExtractionFailureCode.DOCUMENT_ENCRYPTED: ("Encrypted PDF documents are not supported.", False),
    DocumentExtractionFailureCode.DOCUMENT_LIMIT_EXCEEDED: (
        "The attached document exceeds the configured processing limits.",
        False,
    ),
    DocumentExtractionFailureCode.NO_EXTRACTABLE_TEXT: (
        "No extractable text was found in the attached document.",
        False,
    ),
    DocumentExtractionFailureCode.PARSER_UNAVAILABLE: (
        "The local document parser is unavailable.",
        True,
    ),
    DocumentExtractionFailureCode.PARSER_FAILED: ("The local document parser failed safely.", True),
    DocumentExtractionFailureCode.OUTPUT_INTEGRITY_MISMATCH: (
        "The document parser returned an invalid or incomplete result.",
        True,
    ),
    DocumentExtractionFailureCode.DEADLINE_EXCEEDED: ("Document extraction exceeded its deadline.", True),
    DocumentExtractionFailureCode.CANCELLED: ("Document extraction was cancelled.", False),
}


def _map_parser_failure(code: DocumentErrorCode) -> DocumentExtractionFailureCode:
    if code is DocumentErrorCode.SOURCE_NOT_FOUND:
        return DocumentExtractionFailureCode.SOURCE_NOT_FOUND
    if code in {
        DocumentErrorCode.SOURCE_NOT_REGULAR,
        DocumentErrorCode.SOURCE_CHANGED,
        DocumentErrorCode.SOURCE_HASH_MISMATCH,
    }:
        return DocumentExtractionFailureCode.SOURCE_INTEGRITY_MISMATCH
    if code in {DocumentErrorCode.UNSUPPORTED_MEDIA_TYPE, DocumentErrorCode.MIME_MISMATCH}:
        return DocumentExtractionFailureCode.UNSUPPORTED_MEDIA_TYPE
    if code is DocumentErrorCode.PDF_ENCRYPTED:
        return DocumentExtractionFailureCode.DOCUMENT_ENCRYPTED
    if code in {
        DocumentErrorCode.FILE_TOO_LARGE,
        DocumentErrorCode.PDF_PAGE_LIMIT_EXCEEDED,
        DocumentErrorCode.IMAGE_FRAME_LIMIT_EXCEEDED,
        DocumentErrorCode.RASTER_LIMIT_EXCEEDED,
        DocumentErrorCode.PAGE_TEXT_LIMIT_EXCEEDED,
        DocumentErrorCode.TOTAL_TEXT_LIMIT_EXCEEDED,
        DocumentErrorCode.OCR_REGION_LIMIT_EXCEEDED,
        DocumentErrorCode.OUTPUT_LIMIT_EXCEEDED,
    }:
        return DocumentExtractionFailureCode.DOCUMENT_LIMIT_EXCEEDED
    if code is DocumentErrorCode.BACKEND_UNAVAILABLE:
        return DocumentExtractionFailureCode.PARSER_UNAVAILABLE
    if code is DocumentErrorCode.CANCELLED:
        return DocumentExtractionFailureCode.CANCELLED
    if code in {DocumentErrorCode.INVALID_REQUEST, DocumentErrorCode.NON_CANONICAL_REQUEST}:
        return DocumentExtractionFailureCode.OUTPUT_INTEGRITY_MISMATCH
    return DocumentExtractionFailureCode.PARSER_FAILED


def _artifact_id(run_id: str, document_id: str, kind: str) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes({"documentId": document_id, "kind": kind, "runId": run_id})
    ).hexdigest()
    return f"art_document_{kind}_{digest}"


def _artifact_idempotency_key(run_id: str, document_id: str, kind: str) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "documentId": document_id,
                "kind": kind,
                "runId": run_id,
                "schemaVersion": _ARTIFACT_SCHEMA_VERSION,
            }
        )
    ).hexdigest()
    return f"document-ingestion:{kind}:{digest}"


def _request_id(run_id: str, document_id: str) -> str:
    digest = hashlib.sha256(canonical_json_bytes({"documentId": document_id, "runId": run_id})).hexdigest()
    return f"req_document_{digest}"


def _process_id(run_id: str, document_id: str) -> str:
    digest = hashlib.sha256(canonical_json_bytes({"documentId": document_id, "runId": run_id})).hexdigest()
    return f"document-parser-{digest}"


def _run_scratch_name(run_id: str) -> str:
    return "document-ingestion-" + hashlib.sha256(run_id.encode("utf-8", errors="strict")).hexdigest()


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _create_run_scratch(working_root: Path, run_scratch: Path) -> None:
    _validate_directory(working_root)
    if run_scratch.parent != working_root:
        raise ValueError("run scratch escaped its configured working root")
    os.mkdir(run_scratch, 0o700)
    try:
        _validate_directory(working_root)
        _validate_directory(run_scratch)
    except BaseException:
        try:
            os.rmdir(run_scratch)
        except OSError:
            pass
        raise


def _stage_source(document_scratch: Path, staged_source: Path, content: bytes) -> None:
    if staged_source.parent != document_scratch or staged_source.name != "source.bin":
        raise ValueError("staged source path is not canonical")
    os.mkdir(document_scratch, 0o700)
    _validate_directory(document_scratch)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    descriptor = os.open(staged_source, flags, 0o600)
    try:
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("staged source write made no progress")
            offset += written
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
        _validate_regular_file_stat(opened)
        if opened.st_size != len(content):
            raise OSError("staged source size drifted")
    finally:
        os.close(descriptor)
    _validate_directory(document_scratch)
    staged = os.lstat(staged_source)
    _validate_regular_file_stat(staged)
    if staged.st_size != len(content):
        raise OSError("staged source size drifted after close")


def _remove_secure_tree(path: Path) -> None:
    if not _path_exists_no_follow(path):
        return
    _validate_directory(path)
    with os.scandir(path) as entries:
        children = tuple(entries)
    for entry in children:
        child = path / entry.name
        snapshot = os.lstat(child)
        _reject_reparse(snapshot)
        if stat.S_ISDIR(snapshot.st_mode):
            _remove_secure_tree(child)
        elif stat.S_ISREG(snapshot.st_mode):
            _validate_regular_file_stat(snapshot)
            os.unlink(child)
        else:
            raise OSError("scratch contains an unsupported filesystem entry")
    _validate_directory(path)
    os.rmdir(path)


def _validate_directory(path: Path) -> None:
    snapshot = os.lstat(path)
    _reject_reparse(snapshot)
    if not stat.S_ISDIR(snapshot.st_mode):
        raise OSError("scratch boundary is not a directory")


def _validate_regular_file_stat(snapshot: os.stat_result) -> None:
    _reject_reparse(snapshot)
    if not stat.S_ISREG(snapshot.st_mode) or snapshot.st_nlink != 1:
        raise OSError("scratch file is not an unlinked regular file")


def _reject_reparse(snapshot: os.stat_result) -> None:
    attributes = int(getattr(snapshot, "st_file_attributes", 0))
    if stat.S_ISLNK(snapshot.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError("scratch reparse points are forbidden")


def _path_exists_no_follow(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


__all__ = [
    "DocumentIngestionBatchError",
    "DocumentIngestionConfig",
    "DocumentIngestionError",
    "DocumentIngestionOutcome",
    "DocumentIngestionRecoveryError",
    "DocumentIngestionService",
    "DocumentSourceReader",
    "FailedDocument",
    "IndexedDocumentContent",
    "PreparedDocument",
    "PreparedDocumentIngestion",
    "document_id_for_input",
]
