from __future__ import annotations

import asyncio
import hashlib
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from offeragent_harness.documents import (
    CanonicalParseFailure,
    CanonicalParseSuccess,
    DocumentMediaType,
    DocumentParseError,
    DocumentParseRequest,
    DocumentSource,
    decode_canonical_response,
    encode_canonical_request,
)
from offeragent_harness.knowledge import (
    KnowledgeCancellation,
    KnowledgeParseResult,
    KnowledgePreparationError,
    PageEvidence,
    SourceRecord,
)
from offeragent_harness.ports import (
    CancellationToken,
    Clock,
    ProcessLifecycleState,
    ProcessOutputEncoding,
    ProcessOwnerKind,
    ProcessStdinMode,
    ProcessSupervisor,
    SupervisedProcessRequest,
)


@dataclass(frozen=True, slots=True)
class KnowledgeProcessParserConfig:
    parser_config_fingerprint: str
    executable_profile_fingerprint: str
    maximum_source_bytes: int = 64 * 1024 * 1024
    maximum_pages: int = 256
    maximum_response_bytes: int = 12 * 1024 * 1024
    maximum_stderr_bytes: int = 64 * 1024
    timeout_seconds: float = 180.0

    def __post_init__(self) -> None:
        hashes = (self.parser_config_fingerprint, self.executable_profile_fingerprint)
        if any(not _is_sha256(value) for value in hashes):
            raise ValueError("knowledge process parser fingerprints are invalid")
        limits = (
            self.maximum_source_bytes,
            self.maximum_pages,
            self.maximum_response_bytes,
            self.maximum_stderr_bytes,
        )
        if (
            any(isinstance(value, bool) or value < 1 for value in limits)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("knowledge process parser limits must be positive")


class KnowledgeProcessParser:
    """Adapter to the same hash-pinned PyMuPDF/RapidOCR parser host used by attachments."""

    def __init__(
        self,
        *,
        workspace_id: str,
        process_supervisor: ProcessSupervisor,
        scratch_working_root: Path,
        clock: Clock,
        config: KnowledgeProcessParserConfig,
    ) -> None:
        if not workspace_id or not scratch_working_root.is_absolute():
            raise ValueError("knowledge process parser configuration is invalid")
        self._workspace_id = workspace_id
        self._processes = process_supervisor
        self._scratch = scratch_working_root
        self._clock = clock
        self._config = config

    @property
    def parser_fingerprint(self) -> str:
        return self._config.parser_config_fingerprint

    async def parse(
        self,
        *,
        source: SourceRecord,
        absolute_path: Path,
        cancellation: KnowledgeCancellation,
    ) -> KnowledgeParseResult:
        if not isinstance(cancellation, CancellationToken):
            raise TypeError("knowledge process parser requires a complete cancellation token")
        token = cancellation
        token.checkpoint()
        media_type = _media_type(source.media_type)
        job = await asyncio.to_thread(_stage_job, self._scratch, absolute_path, source, self._config)
        request_id = f"knowledge-{source.content_hash.removeprefix('sha256:')[:32]}"
        process_id = f"knowledge-{source.content_hash.removeprefix('sha256:')[:24]}"
        payload = encode_canonical_request(
            DocumentParseRequest(
                request_id,
                DocumentSource(source.source_id, job / "source.bin", media_type, source.content_hash),
            )
        )
        failure: BaseException | None = None
        parsed: KnowledgeParseResult | None = None
        try:
            result = await self._processes.execute(
                SupervisedProcessRequest(
                    process_id=process_id,
                    executable_id="document-extract",
                    arguments=("document-extract",),
                    stdin=payload,
                    environment={},
                    deadline=self._clock.utcnow() + timedelta(seconds=self._config.timeout_seconds),
                    stdout_limit_bytes=self._config.maximum_response_bytes,
                    stderr_limit_bytes=self._config.maximum_stderr_bytes,
                    allow_network=True,
                    owner_kind=ProcessOwnerKind.PARSER,
                    owner_run_id=f"knowledge:{source.source_id}",
                    workspace_id=self._workspace_id,
                    cwd_root_id="process-scratch",
                    cwd=f"working/{job.name}",
                    environment_profile_id="minimal",
                    stdin_mode=ProcessStdinMode.FIXED_PAYLOAD,
                    artifact_limit_bytes=max(
                        self._config.maximum_response_bytes,
                        self._config.maximum_stderr_bytes,
                    ),
                    allow_artifact_spill=False,
                    executable_profile_fingerprint=self._config.executable_profile_fingerprint,
                ),
                token,
            )
            parsed = self._decode_result(result, request_id=request_id, source=source)
        except BaseException as error:
            failure = error
        try:
            await asyncio.to_thread(_remove_job, self._scratch, job)
        except BaseException as error:
            failure = KnowledgePreparationError("knowledge parser scratch cleanup failed")
            failure.__cause__ = error
        if failure is not None:
            raise failure
        assert parsed is not None
        return parsed

    def _decode_result(self, result: object, *, request_id: str, source: SourceRecord) -> KnowledgeParseResult:
        from offeragent_harness.ports import SupervisedProcessResult

        if not isinstance(result, SupervisedProcessResult):
            raise KnowledgePreparationError("knowledge parser returned an invalid process result")
        if result.timed_out or result.lifecycle_state is ProcessLifecycleState.TIMED_OUT:
            raise KnowledgePreparationError("knowledge parser exceeded its deadline")
        if (
            result.exit_code != 0
            or result.output_truncated
            or result.stdout_artifact_id is not None
            or result.stderr_artifact_id is not None
            or result.stdout_encoding is not ProcessOutputEncoding.UTF8
            or result.stdout_total_bytes != len(result.stdout)
            or result.stderr_total_bytes != len(result.stderr)
            or len(result.stdout) > self._config.maximum_response_bytes
            or len(result.stderr) > self._config.maximum_stderr_bytes
        ):
            raise KnowledgePreparationError("knowledge parser process output failed integrity checks")
        try:
            response = decode_canonical_response(result.stdout)
        except DocumentParseError as error:
            raise KnowledgePreparationError("knowledge parser response is invalid") from error
        if response.request_id != request_id:
            raise KnowledgePreparationError("knowledge parser response identity mismatch")
        if isinstance(response, CanonicalParseFailure):
            raise KnowledgePreparationError(f"knowledge parser rejected the source: {response.code.value}")
        assert isinstance(response, CanonicalParseSuccess)
        document = response.result
        if (
            document.source_id != source.source_id
            or document.source_sha256 != source.content_hash
            or document.media_type.value != source.media_type
            or document.source_byte_size != source.byte_size
            or document.parser_config_fingerprint != self._config.parser_config_fingerprint
            or not document.pages
            or len(document.pages) > self._config.maximum_pages
        ):
            raise KnowledgePreparationError("knowledge parser response drifted from its request")
        pages = tuple(
            PageEvidence(
                page.page.provenance.page_number,
                page.page.text,
                _sha256(page.page.text.encode("utf-8", errors="strict")),
            )
            for page in document.pages
        )
        return KnowledgeParseResult(source.source_id, source.content_hash, pages, document.parser_config_fingerprint)


def _stage_job(
    working_root: Path,
    source_path: Path,
    source: SourceRecord,
    config: KnowledgeProcessParserConfig,
) -> Path:
    if not working_root.is_dir() or working_root.is_symlink():
        raise KnowledgePreparationError("knowledge parser scratch root is unavailable")
    if not source_path.is_absolute() or not source_path.is_file() or source_path.is_symlink():
        raise KnowledgePreparationError("knowledge parser source path is invalid")
    content = source_path.read_bytes()
    if (
        len(content) != source.byte_size
        or len(content) > config.maximum_source_bytes
        or _sha256(content) != source.content_hash
    ):
        raise KnowledgePreparationError("knowledge source changed before parser staging")
    job = Path(tempfile.mkdtemp(prefix="knowledge-", dir=working_root))
    try:
        staged = job / "source.bin"
        with staged.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return job
    except BaseException:
        shutil.rmtree(job, ignore_errors=True)
        raise


def _remove_job(working_root: Path, job: Path) -> None:
    if job.parent != working_root or not job.name.startswith("knowledge-"):
        raise ValueError("knowledge parser scratch path escaped its root")
    entries = tuple(job.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise OSError("knowledge parser scratch contains a symbolic link")
    shutil.rmtree(job)


def _media_type(value: str) -> DocumentMediaType:
    try:
        return DocumentMediaType(value)
    except ValueError as error:
        raise KnowledgePreparationError("knowledge source media type is unsupported by the parser") from error


def _is_sha256(value: str) -> bool:
    return (
        len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


__all__ = ["KnowledgeProcessParser", "KnowledgeProcessParserConfig"]
