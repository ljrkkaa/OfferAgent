"""Content-addressed local Artifact Store outside the Vault.

The filesystem objects are immutable, while a small Artifact-specific SQLite
journal is the visibility boundary.  This is intentionally separate from the
Tool invocation journal: it only arbitrates Artifact IDs and idempotency keys.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import stat as stat_module
import threading
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, cast

from offeragent_harness.error_codes import ResourceConflictCause
from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.ports import ArtifactMetadata, ArtifactState, CancellationToken, Sensitivity

_SHA256 = re.compile(r"^sha256:([0-9a-f]{64})$")
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_METADATA_SCHEMA_VERSION = 1
_JOURNAL_SCHEMA_VERSION = 1
_MAX_METADATA_BYTES = 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_DATABASE_NAME = ".artifact-store.sqlite3"
_MAX_ARTIFACT_ID_CHARS = 128
_MAX_CONTENT_HASH_CHARS = len("sha256:") + 64
_MAX_DIGEST_CHARS = 64
_MAX_STATE_CHARS = 16
_DATABASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifact_store_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO artifact_store_schema(singleton, version) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS artifact_entries (
    artifact_id TEXT PRIMARY KEY COLLATE BINARY,
    metadata_digest TEXT NOT NULL UNIQUE,
    metadata_json BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility = 'visible')
);

CREATE TABLE IF NOT EXISTS artifact_idempotency (
    key_digest TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    artifact_id TEXT NOT NULL COLLATE BINARY,
    content_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'complete'))
);
"""

SecurityHook = Callable[[str, Path], None]
CommitHook = Callable[[Path], None]
FileIdentity = tuple[int, int]


@dataclass(frozen=True)
class _OpenFileSnapshot:
    identity: FileIdentity
    file_type: int
    byte_length: int
    modified_ns: int
    changed_ns: int
    link_count: int


@dataclass(frozen=True)
class _VerifiedObjectHandle:
    path: Path
    stream: BinaryIO
    snapshot: _OpenFileSnapshot


class ArtifactStoreError(RuntimeError):
    pass


class ArtifactConflict(ArtifactStoreError, ResourceConflictCause):
    pass


class ArtifactCorrupt(ArtifactStoreError):
    pass


class ArtifactSecurityError(ArtifactStoreError):
    pass


class ArtifactTooLarge(ArtifactStoreError):
    pass


class LocalArtifactStore:
    """Workspace-isolated Artifact Store with crash-safe process-wide idempotency."""

    def __init__(
        self,
        root: Path,
        *,
        workspace_id: str,
        chunk_size: int = 64 * 1024,
        _security_hook: SecurityHook | None = None,
        _commit_hook: CommitHook | None = None,
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        # resolve() would hide a root symlink/junction before it can be rejected.
        self._root = Path(os.path.abspath(os.fspath(root.expanduser())))
        self._workspace_id = workspace_id
        self._chunk_size = chunk_size
        self._security_hook = _security_hook
        self._commit_hook = _commit_hook
        self._root_identity: FileIdentity | None = None
        self._identity_lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    async def put(
        self,
        metadata: ArtifactMetadata,
        content: bytes,
        *,
        idempotency_key: str,
    ) -> ArtifactMetadata:
        """Convenience API for already-materialized, relatively small content."""

        self._validate_request(metadata, idempotency_key)
        payload = content if isinstance(content, bytes) else await asyncio.to_thread(bytes, content)
        if metadata.byte_length != len(payload):
            raise ArtifactConflict("artifact metadata length/hash does not match content")
        await asyncio.to_thread(self._ensure_layout)
        staging_path: Path | None = None
        try:
            created_path, byte_length, digest = await asyncio.to_thread(self._stage_bytes, payload)
            staging_path = created_path
            self._validate_content_summary(metadata, byte_length, digest)
            return await asyncio.to_thread(
                self._commit_staged,
                metadata,
                created_path,
                idempotency_key,
                None,
            )
        finally:
            if staging_path is not None:
                await asyncio.to_thread(self._cleanup_staging, staging_path)

    async def put_stream(
        self,
        metadata: ArtifactMetadata,
        content: AsyncIterable[bytes],
        *,
        idempotency_key: str,
        max_bytes: int,
        cancellation: CancellationToken,
    ) -> ArtifactMetadata:
        """Stage a bounded async byte stream without hashing or file I/O on the event loop."""

        self._validate_request(metadata, idempotency_key)
        if max_bytes < 0:
            raise ValueError("max_bytes cannot be negative")
        if metadata.byte_length > max_bytes:
            raise ArtifactTooLarge("artifact declared length exceeds max_bytes")
        cancellation.checkpoint()
        await asyncio.to_thread(self._ensure_layout, cancellation)
        staging_path: Path | None = None
        stream: BinaryIO | None = None
        digest = hashlib.sha256()
        byte_length = 0
        try:
            created_path, writable = await asyncio.to_thread(self._create_staging_file)
            staging_path = created_path
            stream = writable
            async for chunk in content:
                cancellation.checkpoint()
                if not isinstance(chunk, bytes):
                    raise TypeError("artifact stream chunks must be bytes")
                if byte_length + len(chunk) > max_bytes:
                    raise ArtifactTooLarge("artifact stream exceeded max_bytes")
                await asyncio.to_thread(_write_and_hash, writable, digest, chunk)
                byte_length += len(chunk)
                cancellation.checkpoint()
            await asyncio.to_thread(_finish_writable_stream, writable)
            stream = None
            actual_digest = f"sha256:{digest.hexdigest()}"
            self._validate_content_summary(metadata, byte_length, actual_digest)
            cancellation.checkpoint()
            return await asyncio.to_thread(
                self._commit_staged,
                metadata,
                created_path,
                idempotency_key,
                cancellation,
            )
        finally:
            if stream is not None:
                await asyncio.to_thread(_close_quietly, stream)
            if staging_path is not None:
                await asyncio.to_thread(self._cleanup_staging, staging_path)

    async def metadata(self, artifact_id: str) -> ArtifactMetadata | None:
        _validate_artifact_id(artifact_id)
        return await asyncio.to_thread(self._metadata_sync, artifact_id)

    def read(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> AsyncIterator[bytes]:
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("artifact offset and limit cannot be negative")
        _validate_artifact_id(artifact_id)

        async def generate() -> AsyncIterator[bytes]:
            metadata = await self.metadata(artifact_id)
            if metadata is None:
                raise FileNotFoundError(f"artifact {artifact_id!r} does not exist")
            if offset > metadata.byte_length:
                raise ValueError("artifact offset exceeds its byte length")
            opened = await asyncio.to_thread(self._open_verified_object, metadata, offset, None)
            remaining = max(0, metadata.byte_length - offset)
            if limit is not None:
                remaining = min(remaining, limit)
            try:
                while remaining > 0:
                    chunk = await asyncio.to_thread(
                        self._read_verified_chunk,
                        opened,
                        min(self._chunk_size, remaining),
                    )
                    if not chunk:
                        raise ArtifactCorrupt(f"artifact object ended before the requested range: {artifact_id}")
                    remaining -= len(chunk)
                    yield chunk
            finally:
                try:
                    await asyncio.to_thread(self._validate_verified_handle, opened)
                finally:
                    await asyncio.to_thread(_close_quietly, opened.stream)

        return generate()

    def _stage_bytes(self, content: bytes) -> tuple[Path, int, str]:
        path, stream = self._create_staging_file()
        digest = hashlib.sha256()
        try:
            view = memoryview(content)
            for start in range(0, len(content), self._chunk_size):
                chunk = view[start : start + self._chunk_size]
                stream.write(chunk)
                digest.update(chunk)
            _finish_writable_stream(stream)
        except BaseException:
            _close_quietly(stream)
            self._cleanup_staging(path)
            raise
        return path, len(content), f"sha256:{digest.hexdigest()}"

    def _create_staging_file(self) -> tuple[Path, BinaryIO]:
        staging_directory = self._root / ".staging"
        self._validate_path(staging_directory, final_kind="directory")
        path = staging_directory / f"{uuid.uuid4().hex}.tmp"
        self._validate_path(path, final_kind="file", allow_missing=True)
        try:
            stream = _open_new_binary(path)
        except FileExistsError:
            return self._create_staging_file()
        try:
            opened = os.fstat(stream.fileno())
            _reject_reparse_or_wrong_kind(path, opened, "file")
            self._validate_path(staging_directory, final_kind="directory")
            self._validate_root_identity()
        except BaseException:
            _close_quietly(stream)
            self._cleanup_staging(path)
            raise
        return path, stream

    def _commit_staged(
        self,
        metadata: ArtifactMetadata,
        staging_path: Path,
        idempotency_key: str,
        cancellation: CancellationToken | None,
    ) -> ArtifactMetadata:
        self._ensure_layout(cancellation)
        metadata_bytes = _canonical_json_bytes(_metadata_to_json(metadata))
        if len(metadata_bytes) > _MAX_METADATA_BYTES:
            raise ArtifactTooLarge("artifact metadata exceeds its hard limit")
        request_digest = hashlib.sha256(metadata_bytes).hexdigest()
        key_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        transaction_started = False
        connection = self._connect_database()
        try:
            self._begin_immediate(connection, cancellation)
            transaction_started = True
            row = self._read_idempotency_row(connection, key_digest)
            if row is not None:
                stored_request, stored_artifact, stored_hash, state = row
                if state != "complete":
                    raise ArtifactCorrupt("artifact idempotency reservation was committed in an incomplete state")
                if (
                    stored_request != request_digest
                    or stored_artifact != metadata.artifact_id
                    or stored_hash != metadata.sha256
                ):
                    raise ArtifactConflict("artifact idempotency key is bound to a different request")
                existing = self._metadata_with_connection(connection, metadata.artifact_id)
                if existing != metadata:
                    raise ArtifactConflict("idempotent artifact metadata differs from the original")
                self._verify_object(existing, cancellation)
                connection.commit()
                transaction_started = False
                self._after_commit_hook()
                return existing

            connection.execute(
                "INSERT INTO artifact_idempotency"
                "(key_digest, request_digest, artifact_id, content_hash, state) VALUES (?, ?, ?, ?, 'preparing')",
                (key_digest, request_digest, metadata.artifact_id, metadata.sha256),
            )
            existing = self._metadata_with_connection(connection, metadata.artifact_id)
            if existing is not None:
                if existing != metadata:
                    raise ArtifactConflict(f"artifact ID {metadata.artifact_id!r} already has different metadata")
                self._verify_object(existing, cancellation)
            else:
                self._promote_staging(staging_path, metadata, cancellation)
                self._write_metadata_file(metadata.artifact_id, metadata_bytes)
                connection.execute(
                    "INSERT INTO artifact_entries"
                    "(artifact_id, metadata_digest, metadata_json, content_hash, byte_length, visibility) "
                    "VALUES (?, ?, ?, ?, ?, 'visible')",
                    (
                        metadata.artifact_id,
                        _artifact_id_digest(metadata.artifact_id),
                        metadata_bytes,
                        metadata.sha256,
                        metadata.byte_length,
                    ),
                )
            updated = connection.execute(
                "UPDATE artifact_idempotency SET state = 'complete' WHERE key_digest = ? AND state = 'preparing'",
                (key_digest,),
            )
            if updated.rowcount != 1:
                raise ArtifactCorrupt("artifact idempotency reservation could not be completed exactly once")
            connection.commit()
            transaction_started = False
            self._after_commit_hook()
            return metadata
        except sqlite3.DatabaseError as error:
            if transaction_started:
                _rollback_quietly(connection)
            # The commit result can be indeterminate after an I/O error.  Object
            # and metadata files are immutable and intentionally remain as safe
            # orphans so an identical retry can reconcile them without overwrite.
            raise ArtifactCorrupt("Artifact visibility journal failed during commit") from error
        except BaseException:
            if transaction_started:
                _rollback_quietly(connection)
            raise
        finally:
            connection.close()

    def _read_idempotency_row(
        self,
        connection: sqlite3.Connection,
        key_digest: str,
    ) -> tuple[str, str, str, str] | None:
        row = cast(
            tuple[object, ...] | None,
            connection.execute(
                "SELECT "
                "length(request_digest), substr(request_digest, 1, ?), "
                "length(artifact_id), substr(artifact_id, 1, ?), "
                "length(content_hash), substr(content_hash, 1, ?), "
                "length(state), substr(state, 1, ?) "
                "FROM artifact_idempotency WHERE key_digest = ?",
                (
                    _MAX_DIGEST_CHARS + 1,
                    _MAX_ARTIFACT_ID_CHARS + 1,
                    _MAX_CONTENT_HASH_CHARS + 1,
                    _MAX_STATE_CHARS + 1,
                    key_digest,
                ),
            ).fetchone(),
        )
        if row is None:
            return None
        stored_request = _expect_bounded_sql_text(row[1], row[0], _MAX_DIGEST_CHARS, "idempotency request digest")
        stored_artifact = _expect_bounded_sql_text(row[3], row[2], _MAX_ARTIFACT_ID_CHARS, "idempotency artifact ID")
        stored_hash = _expect_bounded_sql_text(row[5], row[4], _MAX_CONTENT_HASH_CHARS, "idempotency content hash")
        state = _expect_bounded_sql_text(row[7], row[6], _MAX_STATE_CHARS, "idempotency state")
        if re.fullmatch(r"[0-9a-f]{64}", stored_request) is None:
            raise ArtifactCorrupt("Artifact journal contains an invalid idempotency request digest")
        if _ARTIFACT_ID.fullmatch(stored_artifact) is None:
            raise ArtifactCorrupt("Artifact journal contains an unsafe idempotency artifact ID")
        if _SHA256.fullmatch(stored_hash) is None:
            raise ArtifactCorrupt("Artifact journal contains an invalid idempotency content hash")
        if state not in {"preparing", "complete"}:
            raise ArtifactCorrupt("Artifact journal contains an invalid idempotency state")
        return stored_request, stored_artifact, stored_hash, state

    def _after_commit_hook(self) -> None:
        if self._commit_hook is not None:
            self._commit_hook(self._root / _DATABASE_NAME)

    def _promote_staging(
        self,
        staging_path: Path,
        metadata: ArtifactMetadata,
        cancellation: CancellationToken | None,
    ) -> None:
        stage_stream = self._open_validated_file(staging_path)
        try:
            verified_stage = self._verify_open_stream(stage_stream, metadata, cancellation)
            target = self._object_path(metadata.sha256)
            self._secure_mkdir(target.parent)
            self._validate_path(target, final_kind="file", allow_missing=True)
            self._assert_stream_snapshot(stage_stream, verified_stage, metadata.artifact_id)
            try:
                os.link(staging_path, target, follow_symlinks=False)
            except FileExistsError:
                self._verify_object(metadata, cancellation)
                return
            target_stat = self._validate_path(target, final_kind="file")
            if target_stat is None or _file_identity(target_stat) != _file_identity(os.fstat(stage_stream.fileno())):
                raise ArtifactSecurityError("promoted Artifact object does not match the verified staging handle")
            self._validate_root_identity()
        finally:
            _close_quietly(stage_stream)

    def _write_metadata_file(self, artifact_id: str, content: bytes) -> bool:
        target = self._metadata_path(artifact_id)
        self._validate_path(target.parent, final_kind="directory")
        self._validate_path(target, final_kind="file", allow_missing=True)
        if _path_exists_no_follow(target):
            if self._read_small_file(target) != content:
                raise ArtifactCorrupt("artifact metadata digest collision or orphan corruption")
            return False
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        stream: BinaryIO | None = None
        try:
            stream = _open_new_binary(temporary)
            stream.write(content)
            _finish_writable_stream(stream)
            stream = None
            self._validate_path(temporary, final_kind="file")
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                if self._read_small_file(target) != content:
                    raise ArtifactCorrupt("concurrent artifact metadata digest collision") from None
                return False
            self._validate_path(target, final_kind="file")
            return True
        finally:
            if stream is not None:
                _close_quietly(stream)
            self._unlink_temporary(temporary)

    def _metadata_sync(self, artifact_id: str) -> ArtifactMetadata | None:
        self._ensure_layout()
        connection = self._connect_database()
        try:
            return self._metadata_with_connection(connection, artifact_id)
        except sqlite3.DatabaseError as error:
            raise ArtifactCorrupt("Artifact visibility journal is corrupt") from error
        finally:
            connection.close()

    def _metadata_with_connection(
        self,
        connection: sqlite3.Connection,
        artifact_id: str,
    ) -> ArtifactMetadata | None:
        row = cast(
            tuple[object, ...] | None,
            connection.execute(
                "SELECT "
                "length(metadata_digest), substr(metadata_digest, 1, ?), "
                "typeof(metadata_json), length(metadata_json), substr(metadata_json, 1, ?), "
                "length(content_hash), substr(content_hash, 1, ?), "
                "byte_length, length(visibility), substr(visibility, 1, ?) "
                "FROM artifact_entries WHERE artifact_id = ? COLLATE BINARY",
                (
                    _MAX_DIGEST_CHARS + 1,
                    _MAX_METADATA_BYTES + 1,
                    _MAX_CONTENT_HASH_CHARS + 1,
                    _MAX_STATE_CHARS + 1,
                    artifact_id,
                ),
            ).fetchone(),
        )
        if row is None:
            return None
        metadata_digest = _expect_bounded_sql_text(row[1], row[0], _MAX_DIGEST_CHARS, "metadata digest")
        metadata_kind = _expect_text(row[2])
        if len(metadata_kind) > 8:
            raise ArtifactCorrupt("Artifact journal contains an invalid metadata storage type")
        metadata_length = _expect_integer(row[3])
        if metadata_kind != "blob":
            raise ArtifactCorrupt(f"artifact metadata has an invalid SQLite storage type: {artifact_id}")
        persisted_json = _expect_blob(row[4])
        content_hash = _expect_bounded_sql_text(row[6], row[5], _MAX_CONTENT_HASH_CHARS, "content hash")
        byte_length = _expect_integer(row[7])
        visibility = _expect_bounded_sql_text(row[9], row[8], _MAX_STATE_CHARS, "visibility")
        if visibility != "visible" or metadata_digest != _artifact_id_digest(artifact_id):
            raise ArtifactCorrupt(f"artifact visibility record is corrupt: {artifact_id}")
        if metadata_length < 0 or metadata_length > _MAX_METADATA_BYTES or len(persisted_json) != metadata_length:
            raise ArtifactCorrupt(f"artifact metadata exceeds its hard limit: {artifact_id}")
        if _SHA256.fullmatch(content_hash) is None or byte_length < 0:
            raise ArtifactCorrupt(f"artifact content record is corrupt: {artifact_id}")
        path = self._metadata_path(artifact_id)
        try:
            disk_json = self._read_small_file(path)
        except FileNotFoundError as error:
            raise ArtifactCorrupt(f"artifact metadata file is missing: {artifact_id}") from error
        if disk_json != persisted_json:
            raise ArtifactCorrupt(f"artifact metadata file does not match its committed record: {artifact_id}")
        try:
            metadata = _metadata_from_json(_load_json_bytes(persisted_json))
            canonical_metadata = _canonical_json_bytes(_metadata_to_json(metadata))
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ArtifactCorrupt(f"artifact metadata is corrupt: {artifact_id}") from error
        if canonical_metadata != persisted_json:
            raise ArtifactCorrupt(f"artifact metadata is not canonical: {artifact_id}")
        if (
            metadata.artifact_id != artifact_id
            or metadata.workspace_id != self._workspace_id
            or metadata.sha256 != content_hash
            or metadata.byte_length != byte_length
        ):
            raise ArtifactCorrupt(f"artifact metadata crossed its workspace/identity boundary: {artifact_id}")
        if metadata.sensitivity is Sensitivity.SECRET:
            raise ArtifactCorrupt("secret material was persisted in ArtifactStore")
        return metadata

    def _verify_object(
        self,
        metadata: ArtifactMetadata,
        cancellation: CancellationToken | None,
    ) -> None:
        opened = self._open_verified_object(metadata, 0, cancellation)
        _close_quietly(opened.stream)

    def _open_verified_object(
        self,
        metadata: ArtifactMetadata,
        offset: int,
        cancellation: CancellationToken | None,
    ) -> _VerifiedObjectHandle:
        path = self._object_path(metadata.sha256)
        try:
            self._validate_path(path, final_kind="file")
            if self._security_hook is not None:
                self._security_hook("before_object_open", path)
            stream = _open_binary_read(path)
        except FileNotFoundError as error:
            raise ArtifactCorrupt(f"artifact content object is missing: {metadata.artifact_id}") from error
        try:
            self._validate_open_file(path, stream)
            snapshot = self._verify_open_stream(stream, metadata, cancellation)
            self._validate_open_file(path, stream)
            self._assert_stream_snapshot(stream, snapshot, metadata.artifact_id)
            stream.seek(offset)
            self._assert_stream_snapshot(stream, snapshot, metadata.artifact_id)
            self._validate_root_identity()
            return _VerifiedObjectHandle(path=path, stream=stream, snapshot=snapshot)
        except BaseException:
            _close_quietly(stream)
            raise

    def _verify_open_stream(
        self,
        stream: BinaryIO,
        metadata: ArtifactMetadata,
        cancellation: CancellationToken | None,
    ) -> _OpenFileSnapshot:
        try:
            opened_before = _open_file_snapshot(os.fstat(stream.fileno()))
            stream.seek(0)
        except OSError as error:
            raise ArtifactCorrupt(f"artifact content object could not be inspected: {metadata.artifact_id}") from error
        try:
            first_digest, first_length = self._bounded_stream_digest(stream, metadata, cancellation)
            _checkpoint(cancellation)
            opened_after_first_pass = _open_file_snapshot(os.fstat(stream.fileno()))
            stream.seek(0)
            second_digest, second_length = self._bounded_stream_digest(stream, metadata, cancellation)
            _checkpoint(cancellation)
            opened_after = _open_file_snapshot(os.fstat(stream.fileno()))
        except OSError as error:
            raise ArtifactCorrupt(
                f"artifact content object changed while being verified: {metadata.artifact_id}"
            ) from error
        if (
            opened_before != opened_after_first_pass
            or opened_after_first_pass != opened_after
            or opened_before.byte_length != metadata.byte_length
            or opened_after.byte_length != metadata.byte_length
            or first_length != metadata.byte_length
            or second_length != metadata.byte_length
            or first_digest != metadata.sha256
            or second_digest != metadata.sha256
        ):
            raise ArtifactCorrupt(f"artifact content object failed integrity verification: {metadata.artifact_id}")
        return opened_after

    def _bounded_stream_digest(
        self,
        stream: BinaryIO,
        metadata: ArtifactMetadata,
        cancellation: CancellationToken | None,
    ) -> tuple[str, int]:
        """Hash at most the declared content length plus one overflow byte.

        Two identical bounded passes are required by ``_verify_open_stream``.
        File timestamps are not a portable content-version primitive, so matching
        handle snapshots alone cannot prove that bytes read earlier in the pass
        were not overwritten before verification completed.
        """

        digest = hashlib.sha256()
        observed_length = 0
        probe_limit = metadata.byte_length + 1
        while observed_length < probe_limit:
            _checkpoint(cancellation)
            requested = min(self._chunk_size, probe_limit - observed_length)
            chunk = stream.read(requested)
            _checkpoint(cancellation)
            if not chunk:
                break
            if len(chunk) > requested:
                raise ArtifactCorrupt(f"artifact content object exceeded its bounded read: {metadata.artifact_id}")
            digest.update(chunk)
            observed_length += len(chunk)
        return f"sha256:{digest.hexdigest()}", observed_length

    def _read_verified_chunk(self, opened: _VerifiedObjectHandle, byte_length: int) -> bytes:
        self._validate_verified_handle(opened)
        try:
            chunk = opened.stream.read(byte_length)
        except OSError as error:
            raise ArtifactCorrupt("artifact content object failed during range read") from error
        self._validate_verified_handle(opened)
        return chunk

    def _validate_verified_handle(self, opened: _VerifiedObjectHandle) -> None:
        self._assert_stream_snapshot(opened.stream, opened.snapshot, opened.path.name)
        self._validate_open_file(opened.path, opened.stream)
        self._assert_stream_snapshot(opened.stream, opened.snapshot, opened.path.name)
        self._validate_root_identity()

    @staticmethod
    def _assert_stream_snapshot(
        stream: BinaryIO,
        expected: _OpenFileSnapshot,
        artifact_identity: str,
    ) -> None:
        try:
            current = _open_file_snapshot(os.fstat(stream.fileno()))
        except OSError as error:
            raise ArtifactCorrupt(f"artifact content object handle failed validation: {artifact_identity}") from error
        if current != expected:
            raise ArtifactCorrupt(f"artifact content object changed while open: {artifact_identity}")

    def _open_validated_file(self, path: Path) -> BinaryIO:
        self._validate_path(path, final_kind="file")
        stream = _open_binary_read(path)
        try:
            self._validate_open_file(path, stream)
            return stream
        except BaseException:
            _close_quietly(stream)
            raise

    def _validate_open_file(self, path: Path, stream: BinaryIO) -> None:
        opened = os.fstat(stream.fileno())
        _reject_reparse_or_wrong_kind(path, opened, "file")
        try:
            current = self._validate_path(path, final_kind="file")
        except FileNotFoundError as error:
            raise ArtifactSecurityError(f"Artifact path disappeared while it was open: {path.name}") from error
        if current is None or _file_identity(opened) != _file_identity(current):
            raise ArtifactSecurityError(f"Artifact path changed while it was being opened: {path.name}")

    def _read_small_file(self, path: Path) -> bytes:
        stream = self._open_validated_file(path)
        try:
            before = _open_file_snapshot(os.fstat(stream.fileno()))
            if before.byte_length > _MAX_METADATA_BYTES:
                raise ArtifactCorrupt(f"Artifact metadata exceeds {_MAX_METADATA_BYTES} bytes")
            content = stream.read(_MAX_METADATA_BYTES + 1)
            after = _open_file_snapshot(os.fstat(stream.fileno()))
            self._validate_open_file(path, stream)
            final = _open_file_snapshot(os.fstat(stream.fileno()))
            if (
                before != after
                or after != final
                or len(content) != before.byte_length
                or len(content) > _MAX_METADATA_BYTES
            ):
                raise ArtifactCorrupt("Artifact metadata changed while being read")
            return content
        except OSError as error:
            raise ArtifactCorrupt("Artifact metadata failed bounded read") from error
        finally:
            _close_quietly(stream)

    def _ensure_layout(self, cancellation: CancellationToken | None = None) -> None:
        _checkpoint(cancellation)
        self._root.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._root.mkdir()
        except FileExistsError:
            pass
        self._capture_or_validate_root()
        for name in ("objects", "metadata", ".staging"):
            _checkpoint(cancellation)
            self._secure_mkdir(self._root / name)
        self._initialize_database(cancellation)

    def _capture_or_validate_root(self) -> None:
        current = _secure_directory_stat(self._root)
        identity = _file_identity(current)
        with self._identity_lock:
            if self._root_identity is None:
                self._root_identity = identity
            elif self._root_identity != identity:
                raise ArtifactSecurityError("Artifact root identity changed after Store initialization")

    def _validate_root_identity(self) -> None:
        with self._identity_lock:
            expected = self._root_identity
        if expected is None:
            raise ArtifactSecurityError("Artifact root identity has not been established")
        current = _secure_directory_stat(self._root)
        if _file_identity(current) != expected:
            raise ArtifactSecurityError("Artifact root identity changed during an operation")

    def _validate_path(
        self,
        path: Path,
        *,
        final_kind: str,
        allow_missing: bool = False,
    ) -> os.stat_result | None:
        self._validate_root_identity()
        try:
            relative = path.relative_to(self._root)
        except ValueError as error:
            raise ArtifactSecurityError("Artifact path escaped its root") from error
        current = self._root
        final_stat: os.stat_result | None = None
        parts = relative.parts
        for index, part in enumerate(parts):
            current /= part
            is_final = index == len(parts) - 1
            try:
                item_stat = os.lstat(current)
            except FileNotFoundError:
                if allow_missing and is_final:
                    self._validate_root_identity()
                    return None
                raise
            kind = final_kind if is_final else "directory"
            _reject_reparse_or_wrong_kind(current, item_stat, kind)
            if is_final:
                final_stat = item_stat
        self._validate_root_identity()
        return final_stat

    def _secure_mkdir(self, path: Path) -> None:
        self._validate_root_identity()
        try:
            relative = path.relative_to(self._root)
        except ValueError as error:
            raise ArtifactSecurityError("Artifact directory escaped its root") from error
        current = self._root
        for part in relative.parts:
            current /= part
            try:
                os.mkdir(current)
            except FileExistsError:
                pass
            item_stat = os.lstat(current)
            _reject_reparse_or_wrong_kind(current, item_stat, "directory")
        self._validate_root_identity()

    def _initialize_database(self, cancellation: CancellationToken | None) -> None:
        deadline = time.monotonic() + 30.0
        while True:
            _checkpoint(cancellation)
            connection = self._connect_database()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(_DATABASE_SCHEMA)
                row = cast(
                    tuple[object, ...] | None,
                    connection.execute("SELECT version FROM artifact_store_schema WHERE singleton = 1").fetchone(),
                )
                if row is None or _expect_integer(row[0]) != _JOURNAL_SCHEMA_VERSION:
                    raise ArtifactCorrupt("unsupported Artifact visibility journal schema")
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise
                _checkpoint(cancellation)
                time.sleep(0.01)
            finally:
                connection.close()

    def _connect_database(self) -> sqlite3.Connection:
        path = self._root / _DATABASE_NAME
        self._validate_path(path, final_kind="file", allow_missing=True)
        connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 100")
            connection.execute("PRAGMA foreign_keys = ON")
            self._validate_path(path, final_kind="file")
            return connection
        except BaseException:
            connection.close()
            raise

    def _begin_immediate(
        self,
        connection: sqlite3.Connection,
        cancellation: CancellationToken | None,
    ) -> None:
        deadline = time.monotonic() + 30.0
        while True:
            _checkpoint(cancellation)
            try:
                connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def _object_path(self, digest: str) -> Path:
        match = _SHA256.fullmatch(digest)
        if match is None:
            raise ArtifactCorrupt("artifact digest must be canonical sha256")
        value = match.group(1)
        return self._root / "objects" / value[:2] / value

    def _metadata_path(self, artifact_id: str) -> Path:
        _validate_artifact_id(artifact_id)
        return self._root / "metadata" / f"{_artifact_id_digest(artifact_id)}.json"

    def _validate_request(self, metadata: ArtifactMetadata, idempotency_key: str) -> None:
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        _validate_artifact_id(metadata.artifact_id)
        if metadata.workspace_id != self._workspace_id:
            raise ArtifactConflict("artifact belongs to a different workspace")
        if metadata.sensitivity is Sensitivity.SECRET:
            raise ArtifactConflict("secret material must use SecretStore and cannot enter ArtifactStore")
        if _SHA256.fullmatch(metadata.sha256) is None:
            raise ArtifactConflict("artifact metadata digest must be canonical sha256")

    @staticmethod
    def _validate_content_summary(metadata: ArtifactMetadata, byte_length: int, digest: str) -> None:
        if metadata.byte_length != byte_length or metadata.sha256 != digest:
            raise ArtifactConflict("artifact metadata length/hash does not match content")

    def _cleanup_staging(self, path: Path) -> None:
        try:
            self._validate_path(path.parent, final_kind="directory")
            if _path_exists_no_follow(path):
                self._validate_path(path, final_kind="file")
                os.unlink(path)
        except (FileNotFoundError, ArtifactSecurityError):
            # Never follow a replaced staging directory merely to clean up.
            return

    def _unlink_temporary(self, path: Path) -> None:
        try:
            if _path_exists_no_follow(path):
                self._validate_path(path, final_kind="file")
                os.unlink(path)
        except (FileNotFoundError, ArtifactSecurityError):
            return


def _checkpoint(cancellation: CancellationToken | None) -> None:
    if cancellation is not None:
        cancellation.checkpoint()


def _validate_artifact_id(artifact_id: str) -> None:
    if not _ARTIFACT_ID.fullmatch(artifact_id):
        raise ValueError("artifact_id is not filesystem-safe")


def _artifact_id_digest(artifact_id: str) -> str:
    return hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()


def _metadata_to_json(metadata: ArtifactMetadata) -> dict[str, Any]:
    return {
        "schemaVersion": _METADATA_SCHEMA_VERSION,
        "artifactId": metadata.artifact_id,
        "workspaceId": metadata.workspace_id,
        "ownerRunId": metadata.owner_run_id,
        "mimeType": metadata.mime_type,
        "byteLength": metadata.byte_length,
        "sha256": metadata.sha256,
        "sensitivity": metadata.sensitivity.value,
        "state": metadata.state.value,
        "createdAt": metadata.created_at.isoformat(),
        "attributes": thaw_json(metadata.attributes),
    }


def _metadata_from_json(value: Mapping[str, Any]) -> ArtifactMetadata:
    expected = {
        "schemaVersion",
        "artifactId",
        "workspaceId",
        "ownerRunId",
        "mimeType",
        "byteLength",
        "sha256",
        "sensitivity",
        "state",
        "createdAt",
        "attributes",
    }
    if set(value) != expected or value["schemaVersion"] != _METADATA_SCHEMA_VERSION:
        raise ValueError("unsupported artifact metadata schema")
    attributes = value["attributes"]
    if not isinstance(attributes, Mapping):
        raise TypeError("artifact attributes must be an object")
    return ArtifactMetadata(
        artifact_id=_expect_text(value["artifactId"]),
        workspace_id=_expect_text(value["workspaceId"]),
        owner_run_id=_expect_text(value["ownerRunId"]),
        mime_type=_expect_text(value["mimeType"]),
        byte_length=_expect_integer(value["byteLength"]),
        sha256=_expect_text(value["sha256"]),
        sensitivity=Sensitivity(value["sensitivity"]),
        state=ArtifactState(value["state"]),
        created_at=datetime.fromisoformat(_expect_text(value["createdAt"])),
        attributes=dict(attributes),
    )


def _load_json_bytes(content: bytes) -> dict[str, Any]:
    value: object = json.loads(content.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("persisted JSON must be an object")
    return cast(dict[str, Any], value)


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _expect_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactCorrupt("Artifact journal contains an invalid text value")
    return value


def _expect_integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ArtifactCorrupt("Artifact journal contains an invalid integer value")
    return value


def _expect_blob(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, memoryview):
        return value.tobytes()
    raise ArtifactCorrupt("Artifact journal contains an invalid binary value")


def _expect_bounded_sql_text(
    value: object,
    declared_length: object,
    max_chars: int,
    label: str,
) -> str:
    text = _expect_text(value)
    length = _expect_integer(declared_length)
    if length < 1 or length > max_chars or len(text) != length:
        raise ArtifactCorrupt(f"Artifact journal contains an invalid {label}")
    return text


def _file_identity(value: os.stat_result) -> FileIdentity:
    identity = (int(value.st_dev), int(value.st_ino))
    if identity[1] == 0:
        raise ArtifactSecurityError("filesystem did not provide a stable file identity")
    return identity


def _open_file_snapshot(value: os.stat_result) -> _OpenFileSnapshot:
    return _OpenFileSnapshot(
        identity=_file_identity(value),
        file_type=stat_module.S_IFMT(value.st_mode),
        byte_length=int(value.st_size),
        modified_ns=int(value.st_mtime_ns),
        changed_ns=int(value.st_ctime_ns),
        link_count=int(value.st_nlink),
    )


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    return stat_module.S_ISLNK(value.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _reject_reparse_or_wrong_kind(path: Path, value: os.stat_result, expected_kind: str) -> None:
    if _is_reparse(value):
        raise ArtifactSecurityError(f"Artifact path contains a reparse point: {path}")
    if expected_kind == "directory" and not stat_module.S_ISDIR(value.st_mode):
        raise ArtifactSecurityError(f"Artifact path component is not a directory: {path}")
    if expected_kind == "file" and not stat_module.S_ISREG(value.st_mode):
        raise ArtifactSecurityError(f"Artifact path is not a regular file: {path}")


def _secure_directory_stat(path: Path) -> os.stat_result:
    try:
        before = os.lstat(path)
        _reject_reparse_or_wrong_kind(path, before, "directory")
        descriptor = _open_directory_descriptor(path)
    except FileNotFoundError as error:
        raise ArtifactSecurityError(f"Artifact directory disappeared: {path}") from error
    try:
        opened = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    _reject_reparse_or_wrong_kind(path, opened, "directory")
    after = os.lstat(path)
    _reject_reparse_or_wrong_kind(path, after, "directory")
    if _file_identity(before) != _file_identity(opened) or _file_identity(opened) != _file_identity(after):
        raise ArtifactSecurityError(f"Artifact directory changed while validating identity: {path}")
    return opened


def _open_directory_descriptor(path: Path) -> int:
    if os.name == "nt":
        import _winapi
        import msvcrt

        handle = _winapi.CreateFile(
            _windows_extended_path(path),
            0,
            0x00000001 | 0x00000002 | 0x00000004,
            0,
            3,
            0x02000000 | 0x00200000,
            0,
        )
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _open_binary_read(path: Path) -> BinaryIO:
    if os.name == "nt":
        import _winapi
        import msvcrt

        # FILE_SHARE_READ only: writers and delete/rename cannot race the verified handle.
        handle = _winapi.CreateFile(
            _windows_extended_path(path),
            0x80000000,
            0x00000001,
            0,
            3,
            0x00200000 | 0x08000000,
            0,
        )
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise
        return os.fdopen(descriptor, "rb", buffering=0)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags), "rb", buffering=0)


def _open_new_binary(path: Path) -> BinaryIO:
    if os.name == "nt":
        import _winapi
        import msvcrt

        handle = _winapi.CreateFile(
            _windows_extended_path(path),
            0x40000000,
            0,
            0,
            1,
            0x00000080 | 0x00200000,
            0,
        )
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise
        return os.fdopen(descriptor, "wb", buffering=0)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb", buffering=0)


def _windows_extended_path(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _path_exists_no_follow(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _write_and_hash(stream: BinaryIO, digest: Any, chunk: bytes) -> None:
    stream.write(chunk)
    digest.update(chunk)


def _finish_writable_stream(stream: BinaryIO) -> None:
    stream.flush()
    os.fsync(stream.fileno())
    stream.close()


def _close_quietly(stream: BinaryIO) -> None:
    try:
        stream.close()
    except OSError:
        pass


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except sqlite3.DatabaseError:
        pass


__all__ = [
    "ArtifactConflict",
    "ArtifactCorrupt",
    "ArtifactSecurityError",
    "ArtifactStoreError",
    "ArtifactTooLarge",
    "LocalArtifactStore",
]
