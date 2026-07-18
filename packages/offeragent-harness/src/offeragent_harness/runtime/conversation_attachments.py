"""Crash-safe Conversation-owned image attachments outside the Vault."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import sqlite3
import threading
import warnings
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

from offeragent_harness.ports import CancellationToken, Clock, IdGenerator
from offeragent_harness.protocol.content import ArtifactRef, ArtifactSensitivity, ArtifactState

from .attachment_errors import AttachmentError

_SESSION_ID = re.compile(r"^ses_[A-Za-z0-9][A-Za-z0-9_-]{0,123}$")
_TURN_ID = re.compile(r"^turn_[A-Za-z0-9][A-Za-z0-9_-]{0,122}$")
_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9][A-Za-z0-9_-]{0,123}$")
_UPLOAD_ID = re.compile(r"^upload_[A-Za-z0-9][A-Za-z0-9_-]{0,120}$")
_ARTIFACT_ID = re.compile(r"^art_[A-Za-z0-9][A-Za-z0-9_-]{0,123}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_PIL_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
}
_MAX_IMAGE_PIXELS = 40_000_000
_MAX_DECODE_ATTESTATIONS = 256
_FILE_NAME_FORBIDDEN = frozenset("/\\\x00\r\n")


@dataclass(frozen=True, slots=True)
class AttachmentLimits:
    max_image_bytes: int = 10 * 1024 * 1024
    max_submission_images: int = 20
    max_submission_bytes: int = 50 * 1024 * 1024
    max_conversation_bytes: int = 250 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_chunk_bytes: int = 64 * 1024
    staging_ttl: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        numeric = (
            self.max_image_bytes,
            self.max_submission_images,
            self.max_submission_bytes,
            self.max_conversation_bytes,
            self.max_total_bytes,
            self.max_chunk_bytes,
        )
        if min(numeric) < 1 or self.staging_ttl <= timedelta(0):
            raise ValueError("attachment limits must be positive")
        if self.max_image_bytes > self.max_submission_bytes:
            raise ValueError("one image cannot exceed the submission limit")
        if self.max_submission_bytes > self.max_conversation_bytes:
            raise ValueError("one submission cannot exceed the Conversation limit")
        if self.max_conversation_bytes > self.max_total_bytes:
            raise ValueError("one Conversation cannot exceed the global attachment limit")


@dataclass(frozen=True, slots=True)
class AttachmentUploadRequest:
    session_id: str
    client_request_id: str
    file_name: str
    media_type: str
    byte_length: int
    content_hash: str

    def __post_init__(self) -> None:
        _require_match(_SESSION_ID, self.session_id, "Session ID")
        _require_match(_REQUEST_ID, self.client_request_id, "client request ID")
        if (
            not self.file_name
            or len(self.file_name) > 512
            or self.file_name.strip() != self.file_name
            or any(character in _FILE_NAME_FORBIDDEN or ord(character) < 0x20 for character in self.file_name)
        ):
            raise ValueError("attachment file name is unsafe")
        if self.media_type not in _MEDIA_TYPES:
            raise ValueError("attachment media type is unsupported")
        if self.byte_length < 1:
            raise ValueError("attachment byte length must be positive")
        _require_match(_SHA256, self.content_hash, "attachment content hash")


@dataclass(frozen=True, slots=True)
class AttachmentBeginReceipt:
    upload_id: str
    artifact_id: str
    next_offset: int
    duplicate: bool


@dataclass(frozen=True, slots=True)
class AttachmentChunkReceipt:
    upload_id: str
    next_offset: int
    duplicate: bool


@dataclass(frozen=True, slots=True)
class AttachmentCommitReceipt:
    upload_id: str
    artifact: ArtifactRef
    duplicate: bool


@dataclass(frozen=True, slots=True)
class AttachmentReadReceipt:
    artifact: ArtifactRef
    offset: int
    next_offset: int
    content: bytes
    complete: bool


@dataclass(frozen=True, slots=True)
class AttachmentClaim:
    artifact_id: str
    order: int
    content_hash: str
    media_type: str
    byte_length: int

    def __post_init__(self) -> None:
        _require_match(_ARTIFACT_ID, self.artifact_id, "Artifact ID")
        if self.order < 0:
            raise ValueError("attachment claim order cannot be negative")
        _require_match(_SHA256, self.content_hash, "attachment claim hash")
        if self.media_type not in _MEDIA_TYPES or self.byte_length < 1:
            raise ValueError("attachment claim metadata is invalid")


@dataclass(frozen=True, slots=True)
class ClaimedAttachment:
    artifact_id: str
    order: int
    file_name: str
    media_type: str
    byte_length: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class AttachmentClaimReceipt:
    attachments: tuple[ClaimedAttachment, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class AttachmentRecoveryReport:
    removed_artifact_ids: tuple[str, ...]
    deleted_session_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _StoredAttachment:
    upload_id: str
    artifact_id: str
    client_request_id: str
    session_id: str
    file_name: str
    media_type: str
    byte_length: int
    content_hash: str
    status: str
    received_bytes: int
    created_at: datetime
    updated_at: datetime


class ConversationAttachmentStore:
    """Own one Workspace's upload journal, immutable bytes, and Turn claims.

    The store is deliberately independent from Vault and replay events.  A
    durable Turn records only bounded Artifact metadata; the corresponding
    bytes remain in this Workspace-scoped directory until Session deletion.
    """

    def __init__(
        self,
        root: Path,
        *,
        workspace_id: str,
        clock: Clock,
        ids: IdGenerator,
        limits: AttachmentLimits | None = None,
    ) -> None:
        if not workspace_id or workspace_id.strip() != workspace_id or "\x00" in workspace_id:
            raise ValueError("attachment Workspace ID is invalid")
        self._root = Path(os.path.abspath(os.fspath(root.expanduser())))
        self._objects = self._root / "objects"
        self._database = self._root / "attachments.sqlite3"
        self._workspace_id = workspace_id
        self._clock = clock
        self._ids = ids
        self._limits = limits or AttachmentLimits()
        self._lock = threading.RLock()
        self._verified_files: OrderedDict[str, str] = OrderedDict()
        self._ensure_layout()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    async def begin(
        self,
        request: AttachmentUploadRequest,
        cancellation: CancellationToken,
    ) -> AttachmentBeginReceipt:
        cancellation.checkpoint()
        result = await asyncio.to_thread(self._begin_sync, request)
        cancellation.checkpoint()
        return result

    async def append(
        self,
        upload_id: str,
        offset: int,
        content: bytes,
        cancellation: CancellationToken,
        *,
        session_id: str | None = None,
    ) -> AttachmentChunkReceipt:
        cancellation.checkpoint()
        if offset < 0:
            raise ValueError("attachment chunk offset cannot be negative")
        payload = bytes(content)
        if not payload or len(payload) > self._limits.max_chunk_bytes:
            raise AttachmentError("invalid_chunk", "Attachment chunk must be non-empty and within the 64 KiB limit")
        result = await asyncio.to_thread(self._append_sync, upload_id, offset, payload, session_id)
        cancellation.checkpoint()
        return result

    async def commit(
        self,
        upload_id: str,
        cancellation: CancellationToken,
        *,
        session_id: str | None = None,
    ) -> AttachmentCommitReceipt:
        cancellation.checkpoint()
        result = await asyncio.to_thread(self._commit_sync, upload_id, session_id)
        cancellation.checkpoint()
        return result

    async def abort(
        self,
        upload_id: str,
        cancellation: CancellationToken,
        *,
        session_id: str | None = None,
    ) -> None:
        cancellation.checkpoint()
        await asyncio.to_thread(self._abort_sync, upload_id, session_id)
        cancellation.checkpoint()

    async def read(
        self,
        artifact_id: str,
        offset: int,
        max_bytes: int,
        cancellation: CancellationToken,
    ) -> AttachmentReadReceipt:
        cancellation.checkpoint()
        if offset < 0 or max_bytes < 1 or max_bytes > self._limits.max_chunk_bytes:
            raise ValueError("attachment read range is invalid")
        result = await asyncio.to_thread(self._read_sync, artifact_id, offset, max_bytes)
        cancellation.checkpoint()
        return result

    async def read_for_conversation(
        self,
        session_id: str,
        artifact_id: str,
        offset: int,
        max_bytes: int,
        cancellation: CancellationToken,
    ) -> AttachmentReadReceipt:
        cancellation.checkpoint()
        _require_match(_SESSION_ID, session_id, "Session ID")
        if offset < 0 or max_bytes < 1 or max_bytes > self._limits.max_chunk_bytes:
            raise ValueError("attachment read range is invalid")
        result = await asyncio.to_thread(self._read_sync, artifact_id, offset, max_bytes, session_id)
        cancellation.checkpoint()
        return result

    async def read_all_for_conversation(
        self,
        session_id: str,
        artifact_id: str,
        cancellation: CancellationToken,
    ) -> AttachmentReadReceipt:
        """Materialize one owned immutable image with exactly one integrity/decode pass."""

        cancellation.checkpoint()
        _require_match(_SESSION_ID, session_id, "Session ID")
        result = await asyncio.to_thread(self._read_all_sync, artifact_id, session_id)
        cancellation.checkpoint()
        return result

    async def claim_submission(
        self,
        session_id: str,
        turn_id: str,
        claims: Sequence[AttachmentClaim],
        cancellation: CancellationToken,
    ) -> tuple[ClaimedAttachment, ...]:
        receipt = await self.claim_submission_with_receipt(session_id, turn_id, claims, cancellation)
        return receipt.attachments

    async def claim_submission_with_receipt(
        self,
        session_id: str,
        turn_id: str,
        claims: Sequence[AttachmentClaim],
        cancellation: CancellationToken,
    ) -> AttachmentClaimReceipt:
        cancellation.checkpoint()
        result = await asyncio.to_thread(self._claim_submission_sync, session_id, turn_id, tuple(claims))
        cancellation.checkpoint()
        return result

    async def release_turn_claim(self, turn_id: str, cancellation: CancellationToken) -> None:
        cancellation.checkpoint()
        _require_match(_TURN_ID, turn_id, "Turn ID")
        await asyncio.to_thread(self._release_turn_claim_sync, turn_id)
        cancellation.checkpoint()

    async def mark_conversation_deleting(self, session_id: str, cancellation: CancellationToken) -> None:
        cancellation.checkpoint()
        _require_match(_SESSION_ID, session_id, "Session ID")
        await asyncio.to_thread(self._mark_conversation_deleting_sync, session_id)
        cancellation.checkpoint()

    async def delete_conversation(self, session_id: str, cancellation: CancellationToken) -> None:
        await self.mark_conversation_deleting(session_id, cancellation)
        await asyncio.to_thread(self._finish_conversation_deletion_sync, session_id)
        cancellation.checkpoint()

    async def recover(
        self,
        deleted_session_ids: Sequence[str],
        cancellation: CancellationToken,
        *,
        existing_turn_ids: Sequence[str] | None = None,
    ) -> AttachmentRecoveryReport:
        cancellation.checkpoint()
        result = await asyncio.to_thread(
            self._recover_sync,
            tuple(deleted_session_ids),
            None if existing_turn_ids is None else tuple(existing_turn_ids),
        )
        cancellation.checkpoint()
        return result

    def _begin_sync(self, request: AttachmentUploadRequest) -> AttachmentBeginReceipt:
        if request.byte_length > self._limits.max_image_bytes:
            raise AttachmentError("attachment_too_large", "Image exceeds the 10 MiB image limit")
        with self._lock, self._connect() as connection:
            existing = self._by_client_request(connection, request.client_request_id)
            if existing is not None:
                if _request_identity(existing) != _request_identity(request):
                    raise AttachmentError(
                        "idempotency_conflict",
                        "Attachment client request ID is already bound to different metadata",
                    )
                self._reconcile_staging_length(existing)
                return AttachmentBeginReceipt(
                    existing.upload_id,
                    existing.artifact_id,
                    existing.received_bytes,
                    True,
                )
            conversation_bytes = self._reserved_bytes(connection, session_id=request.session_id)
            total_bytes = self._reserved_bytes(connection, session_id=None)
            if conversation_bytes + request.byte_length > self._limits.max_conversation_bytes:
                raise AttachmentError(
                    "capacity_exceeded",
                    "Conversation attachment capacity is full; delete this Conversation's old images "
                    "or start a new Conversation",
                )
            if total_bytes + request.byte_length > self._limits.max_total_bytes:
                raise AttachmentError(
                    "capacity_exceeded",
                    "Attachment storage is full; delete or clean up Conversations before adding another image",
                )
            upload_id = self._ids.new_id("upload")
            artifact_id = self._ids.new_id("art")
            _require_match(_UPLOAD_ID, upload_id, "Upload ID")
            _require_match(_ARTIFACT_ID, artifact_id, "Artifact ID")
            staging = self._staging_path(artifact_id)
            try:
                with staging.open("xb") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                now = self._clock.utcnow()
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO attachments("
                    "upload_id, artifact_id, client_request_id, session_id, file_name, media_type, "
                    "byte_length, content_hash, status, received_bytes, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staging', 0, ?, ?)",
                    (
                        upload_id,
                        artifact_id,
                        request.client_request_id,
                        request.session_id,
                        request.file_name,
                        request.media_type,
                        request.byte_length,
                        request.content_hash,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.commit()
            except BaseException:
                staging.unlink(missing_ok=True)
                raise
        return AttachmentBeginReceipt(upload_id, artifact_id, 0, False)

    def _append_sync(
        self,
        upload_id: str,
        offset: int,
        content: bytes,
        session_id: str | None = None,
    ) -> AttachmentChunkReceipt:
        _require_match(_UPLOAD_ID, upload_id, "Upload ID")
        chunk_hash = hashlib.sha256(content).hexdigest()
        with self._lock, self._connect() as connection:
            record = self._by_upload(connection, upload_id)
            if record is None or record.status == "deleting":
                raise AttachmentError("attachment_unavailable", "Attachment upload is unavailable")
            self._require_conversation(record, session_id)
            chunk = connection.execute(
                "SELECT byte_length, content_hash FROM chunks WHERE upload_id = ? AND chunk_offset = ?",
                (upload_id, offset),
            ).fetchone()
            if chunk is not None:
                if int(chunk[0]) != len(content) or str(chunk[1]) != chunk_hash:
                    raise AttachmentError("chunk_conflict", "Attachment chunk offset is bound to different bytes")
                path = self._content_path(record)
                if not path.exists() or self._read_range(path, offset, len(content)) != content:
                    raise AttachmentError(
                        "attachment_corrupt",
                        "Committed attachment chunk no longer matches its journal",
                    )
                return AttachmentChunkReceipt(upload_id, record.received_bytes, True)
            if record.status != "staging":
                raise AttachmentError("attachment_committed", "Attachment upload is already committed")
            self._reconcile_staging_length(record)
            if offset != record.received_bytes:
                raise AttachmentError(
                    "chunk_offset_mismatch",
                    f"Attachment chunk offset {offset} does not match next offset {record.received_bytes}",
                )
            if offset + len(content) > record.byte_length:
                raise AttachmentError("invalid_chunk", "Attachment chunk exceeds the declared byte length")
            path = self._staging_path(record.artifact_id)
            with path.open("r+b") as stream:
                stream.seek(offset)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            next_offset = offset + len(content)
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE attachments SET received_bytes = ?, updated_at = ? "
                "WHERE upload_id = ? AND status = 'staging' AND received_bytes = ?",
                (next_offset, self._clock.utcnow().isoformat(), upload_id, offset),
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise AttachmentError("chunk_conflict", "Attachment upload changed while appending a chunk")
            connection.execute(
                "INSERT INTO chunks(upload_id, chunk_offset, byte_length, content_hash) VALUES (?, ?, ?, ?)",
                (upload_id, offset, len(content), chunk_hash),
            )
            connection.commit()
        return AttachmentChunkReceipt(upload_id, next_offset, False)

    def _commit_sync(self, upload_id: str, session_id: str | None = None) -> AttachmentCommitReceipt:
        _require_match(_UPLOAD_ID, upload_id, "Upload ID")
        with self._lock, self._connect() as connection:
            record = self._by_upload(connection, upload_id)
            if record is None or record.status == "deleting":
                raise AttachmentError("attachment_unavailable", "Attachment upload is unavailable")
            self._require_conversation(record, session_id)
            if record.status == "ready":
                self._verify_ready(record)
                return AttachmentCommitReceipt(upload_id, _artifact_ref(record), True)
            final = self._final_path(record.artifact_id)
            staging = self._staging_path(record.artifact_id)
            if staging.exists():
                self._reconcile_staging_length(record)
            elif not final.exists():
                raise AttachmentError("attachment_corrupt", "Attachment staging bytes are missing")
            if record.received_bytes != record.byte_length:
                raise AttachmentError(
                    "upload_incomplete",
                    f"Attachment upload is incomplete at offset {record.received_bytes}",
                )
            source = staging if staging.exists() else final
            self._verify_image(source, record, hash_error_code="invalid_image")
            if staging.exists():
                if final.exists():
                    self._verify_image(final, record)
                    staging.unlink()
                else:
                    os.replace(staging, final)
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE attachments SET status = 'ready', updated_at = ? "
                "WHERE upload_id = ? AND status = 'staging' AND received_bytes = byte_length",
                (self._clock.utcnow().isoformat(), upload_id),
            )
            if updated.rowcount != 1:
                connection.rollback()
                current = self._by_upload(connection, upload_id)
                if current is None or current.status != "ready":
                    raise AttachmentError("commit_conflict", "Attachment changed while committing")
                record = current
            else:
                connection.commit()
                record = self._by_upload(connection, upload_id)
                if record is None:
                    raise AttachmentError("attachment_corrupt", "Committed attachment metadata disappeared")
            self._remember_verified(record)
        return AttachmentCommitReceipt(upload_id, _artifact_ref(record), False)

    def _abort_sync(self, upload_id: str, session_id: str | None = None) -> None:
        _require_match(_UPLOAD_ID, upload_id, "Upload ID")
        with self._lock, self._connect() as connection:
            record = self._by_upload(connection, upload_id)
            if record is None:
                raise AttachmentError("attachment_unavailable", "Attachment upload is unavailable")
            self._require_conversation(record, session_id)
            claimed = int(
                connection.execute(
                    "SELECT count(*) FROM claims WHERE artifact_id = ?",
                    (record.artifact_id,),
                ).fetchone()[0]
            )
            if claimed:
                raise AttachmentError("attachment_claimed", "A claimed Conversation attachment cannot be aborted")
            for path in self._possible_paths(record.artifact_id):
                path.unlink(missing_ok=True)
            self._verified_files.pop(record.artifact_id, None)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM attachments WHERE upload_id = ?", (upload_id,))
            connection.commit()

    def _read_sync(
        self,
        artifact_id: str,
        offset: int,
        max_bytes: int,
        session_id: str | None = None,
    ) -> AttachmentReadReceipt:
        _require_match(_ARTIFACT_ID, artifact_id, "Artifact ID")
        with self._lock, self._connect() as connection:
            record = self._by_artifact(connection, artifact_id)
            if record is None or record.status != "ready":
                raise AttachmentError("attachment_unavailable", "Conversation attachment is unavailable")
            self._require_conversation(record, session_id)
            verified_content = self._verify_ready(record)
            if offset > record.byte_length:
                raise AttachmentError("invalid_range", "Attachment read offset exceeds its byte length")
            length = min(max_bytes, record.byte_length - offset)
            content = verified_content[offset : offset + length]
            next_offset = offset + len(content)
            return AttachmentReadReceipt(
                _artifact_ref(record),
                offset,
                next_offset,
                content,
                next_offset == record.byte_length,
            )

    def _read_all_sync(self, artifact_id: str, session_id: str) -> AttachmentReadReceipt:
        _require_match(_ARTIFACT_ID, artifact_id, "Artifact ID")
        with self._lock, self._connect() as connection:
            record = self._by_artifact(connection, artifact_id)
            if record is None or record.status != "ready":
                raise AttachmentError("attachment_unavailable", "Conversation attachment is unavailable")
            self._require_conversation(record, session_id)
            content = self._verify_ready(record)
            return AttachmentReadReceipt(
                _artifact_ref(record),
                0,
                record.byte_length,
                content,
                True,
            )

    def _claim_submission_sync(
        self,
        session_id: str,
        turn_id: str,
        claims: tuple[AttachmentClaim, ...],
    ) -> AttachmentClaimReceipt:
        _require_match(_SESSION_ID, session_id, "Session ID")
        _require_match(_TURN_ID, turn_id, "Turn ID")
        if not claims:
            return AttachmentClaimReceipt((), False)
        if len(claims) > self._limits.max_submission_images:
            raise AttachmentError("submission_too_large", "One submission cannot contain more than 20 images")
        if [item.order for item in claims] != list(range(len(claims))):
            raise AttachmentError("invalid_order", "Attachment submission order must be contiguous from zero")
        if len({item.artifact_id for item in claims}) != len(claims):
            raise AttachmentError("invalid_order", "Attachment submission cannot repeat an Artifact")
        if sum(item.byte_length for item in claims) > self._limits.max_submission_bytes:
            raise AttachmentError("submission_too_large", "Ordered image submission exceeds the 50 MiB total limit")
        with self._lock, self._connect() as connection:
            records: list[_StoredAttachment] = []
            for claim in claims:
                record = self._by_artifact(connection, claim.artifact_id)
                if record is None or record.status != "ready":
                    raise AttachmentError("attachment_unavailable", "Conversation attachment is unavailable")
                if record.session_id != session_id:
                    raise AttachmentError("ownership_mismatch", "Attachment belongs to another Conversation")
                if (
                    record.content_hash != claim.content_hash
                    or record.media_type != claim.media_type
                    or record.byte_length != claim.byte_length
                ):
                    raise AttachmentError("metadata_conflict", "Attachment claim metadata differs from committed bytes")
                self._verify_ready(record)
                records.append(record)
            existing = connection.execute(
                "SELECT artifact_id, item_order FROM claims WHERE turn_id = ? ORDER BY item_order",
                (turn_id,),
            ).fetchall()
            expected = [(item.artifact_id, item.order) for item in claims]
            if existing:
                observed = [(str(row[0]), int(row[1])) for row in existing]
                if observed != expected:
                    raise AttachmentError("claim_conflict", "Turn is already bound to a different attachment order")
                created = False
            else:
                now = self._clock.utcnow().isoformat()
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "INSERT INTO claims(artifact_id, turn_id, item_order, claimed_at) VALUES (?, ?, ?, ?)",
                    [(item.artifact_id, turn_id, item.order, now) for item in claims],
                )
                connection.commit()
                created = True
            return AttachmentClaimReceipt(
                tuple(
                    ClaimedAttachment(
                        record.artifact_id,
                        claim.order,
                        record.file_name,
                        record.media_type,
                        record.byte_length,
                        record.content_hash,
                    )
                    for record, claim in zip(records, claims, strict=True)
                ),
                created,
            )

    def _release_turn_claim_sync(self, turn_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM claims WHERE turn_id = ?", (turn_id,))
            connection.commit()

    def _mark_conversation_deleting_sync(self, session_id: str) -> None:
        with self._lock, self._connect() as connection:
            records = self._for_session(connection, session_id)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE attachments SET status = 'deleting', updated_at = ? WHERE session_id = ?",
                (self._clock.utcnow().isoformat(), session_id),
            )
            connection.commit()
            for record in records:
                self._verified_files.pop(record.artifact_id, None)
                tombstone = self._deleting_path(record.artifact_id)
                if tombstone.exists():
                    continue
                final = self._final_path(record.artifact_id)
                staging = self._staging_path(record.artifact_id)
                source = final if final.exists() else staging
                if source.exists():
                    os.replace(source, tombstone)

    def _finish_conversation_deletion_sync(self, session_id: str) -> None:
        with self._lock, self._connect() as connection:
            records = self._for_session(connection, session_id)
            for record in records:
                for path in self._possible_paths(record.artifact_id):
                    path.unlink(missing_ok=True)
                self._verified_files.pop(record.artifact_id, None)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM attachments WHERE session_id = ?", (session_id,))
            connection.commit()

    def _recover_sync(
        self,
        deleted_session_ids: tuple[str, ...],
        existing_turn_ids: tuple[str, ...] | None,
    ) -> AttachmentRecoveryReport:
        removed: list[str] = []
        deleted: list[str] = []
        for session_id in dict.fromkeys(deleted_session_ids):
            _require_match(_SESSION_ID, session_id, "Session ID")
            self._mark_conversation_deleting_sync(session_id)
        with self._lock, self._connect() as connection:
            deleting = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT session_id FROM attachments WHERE status = 'deleting' ORDER BY session_id"
                ).fetchall()
            )
        for session_id in deleting:
            self._finish_conversation_deletion_sync(session_id)
            deleted.append(session_id)
        if existing_turn_ids is not None:
            if any(_TURN_ID.fullmatch(turn_id) is None for turn_id in existing_turn_ids):
                raise ValueError("existing Turn IDs contain an invalid identity")
            retained = set(existing_turn_ids)
            with self._lock, self._connect() as connection:
                orphaned = tuple(
                    str(row[0])
                    for row in connection.execute("SELECT DISTINCT turn_id FROM claims").fetchall()
                    if str(row[0]) not in retained
                )
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "DELETE FROM claims WHERE turn_id = ?",
                    ((turn_id,) for turn_id in orphaned),
                )
                connection.commit()
        cutoff = self._clock.utcnow() - self._limits.staging_ttl
        with self._lock, self._connect() as connection:
            expiring = tuple(
                self._row(row)
                for row in connection.execute(
                    "SELECT a.* FROM attachments a LEFT JOIN claims c ON c.artifact_id = a.artifact_id "
                    "WHERE c.artifact_id IS NULL AND a.updated_at < ? AND a.status IN ('staging', 'ready') "
                    "ORDER BY a.artifact_id",
                    (cutoff.isoformat(),),
                ).fetchall()
            )
        for record in expiring:
            self._abort_sync(record.upload_id)
            removed.append(record.artifact_id)
        with self._lock, self._connect() as connection:
            records = tuple(
                self._row(row)
                for row in connection.execute("SELECT * FROM attachments ORDER BY artifact_id").fetchall()
            )
        for record in records:
            if record.status == "staging":
                if self._final_path(record.artifact_id).exists() and record.received_bytes == record.byte_length:
                    self._commit_sync(record.upload_id)
                else:
                    self._reconcile_staging_length(record)
        self._remove_orphan_files()
        return AttachmentRecoveryReport(tuple(removed), tuple(deleted))

    def _ensure_layout(self) -> None:
        for path in (self._root, self._objects):
            if path.exists() and path.is_symlink():
                raise AttachmentError("unsafe_storage", "Attachment storage cannot use a symlink")
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise AttachmentError("unsafe_storage", "Attachment storage path is not a directory")
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS attachments(
                    upload_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL UNIQUE,
                    client_request_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    byte_length INTEGER NOT NULL CHECK(byte_length > 0),
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('staging', 'ready', 'deleting')),
                    received_bytes INTEGER NOT NULL CHECK(received_bytes >= 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attachments_session ON attachments(session_id, artifact_id);
                CREATE TABLE IF NOT EXISTS chunks(
                    upload_id TEXT NOT NULL REFERENCES attachments(upload_id) ON DELETE CASCADE,
                    chunk_offset INTEGER NOT NULL CHECK(chunk_offset >= 0),
                    byte_length INTEGER NOT NULL CHECK(byte_length > 0),
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY(upload_id, chunk_offset)
                );
                CREATE TABLE IF NOT EXISTS claims(
                    artifact_id TEXT NOT NULL REFERENCES attachments(artifact_id) ON DELETE CASCADE,
                    turn_id TEXT NOT NULL,
                    item_order INTEGER NOT NULL CHECK(item_order >= 0),
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY(turn_id, item_order),
                    UNIQUE(turn_id, artifact_id)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _by_upload(self, connection: sqlite3.Connection, upload_id: str) -> _StoredAttachment | None:
        row = connection.execute("SELECT * FROM attachments WHERE upload_id = ?", (upload_id,)).fetchone()
        return None if row is None else self._row(row)

    def _by_artifact(self, connection: sqlite3.Connection, artifact_id: str) -> _StoredAttachment | None:
        row = connection.execute("SELECT * FROM attachments WHERE artifact_id = ?", (artifact_id,)).fetchone()
        return None if row is None else self._row(row)

    def _by_client_request(
        self,
        connection: sqlite3.Connection,
        client_request_id: str,
    ) -> _StoredAttachment | None:
        row = connection.execute(
            "SELECT * FROM attachments WHERE client_request_id = ?", (client_request_id,)
        ).fetchone()
        return None if row is None else self._row(row)

    def _for_session(self, connection: sqlite3.Connection, session_id: str) -> tuple[_StoredAttachment, ...]:
        return tuple(
            self._row(row)
            for row in connection.execute(
                "SELECT * FROM attachments WHERE session_id = ? ORDER BY artifact_id", (session_id,)
            ).fetchall()
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> _StoredAttachment:
        try:
            return _StoredAttachment(
                upload_id=str(row["upload_id"]),
                artifact_id=str(row["artifact_id"]),
                client_request_id=str(row["client_request_id"]),
                session_id=str(row["session_id"]),
                file_name=str(row["file_name"]),
                media_type=str(row["media_type"]),
                byte_length=int(row["byte_length"]),
                content_hash=str(row["content_hash"]),
                status=str(row["status"]),
                received_bytes=int(row["received_bytes"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise AttachmentError("attachment_corrupt", "Attachment metadata journal is corrupt") from error

    def _reserved_bytes(self, connection: sqlite3.Connection, *, session_id: str | None) -> int:
        if session_id is None:
            row = connection.execute("SELECT coalesce(sum(byte_length), 0) FROM attachments").fetchone()
        else:
            row = connection.execute(
                "SELECT coalesce(sum(byte_length), 0) FROM attachments WHERE session_id = ?", (session_id,)
            ).fetchone()
        return int(row[0])

    def _reconcile_staging_length(self, record: _StoredAttachment) -> None:
        if record.status != "staging":
            return
        path = self._staging_path(record.artifact_id)
        if not path.exists():
            if self._final_path(record.artifact_id).exists() and record.received_bytes == record.byte_length:
                return
            raise AttachmentError("attachment_corrupt", "Attachment staging file is missing")
        observed = path.stat().st_size
        if observed < record.received_bytes:
            raise AttachmentError("attachment_corrupt", "Attachment staging file is shorter than its journal")
        if observed > record.received_bytes:
            with path.open("r+b") as stream:
                stream.truncate(record.received_bytes)
                stream.flush()
                os.fsync(stream.fileno())

    def _verify_ready(self, record: _StoredAttachment) -> bytes:
        content = self._verify_image(
            self._final_path(record.artifact_id),
            record,
            decode=self._verified_files.get(record.artifact_id) != record.content_hash,
        )
        self._remember_verified(record)
        return content

    def _remember_verified(self, record: _StoredAttachment) -> None:
        self._verified_files[record.artifact_id] = record.content_hash
        self._verified_files.move_to_end(record.artifact_id)
        while len(self._verified_files) > _MAX_DECODE_ATTESTATIONS:
            self._verified_files.popitem(last=False)

    @staticmethod
    def _verify_image(
        path: Path,
        record: _StoredAttachment,
        *,
        hash_error_code: str = "attachment_corrupt",
        decode: bool = True,
    ) -> bytes:
        try:
            content = path.read_bytes()
        except FileNotFoundError as error:
            raise AttachmentError("attachment_corrupt", "Attachment bytes are missing") from error
        if len(content) != record.byte_length:
            raise AttachmentError("attachment_corrupt", "Attachment byte length differs from committed metadata")
        actual_hash = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if not _has_image_signature(content, record.media_type):
            raise AttachmentError("invalid_image", "Attachment bytes do not match the claimed image signature")
        if actual_hash != record.content_hash:
            raise AttachmentError(hash_error_code, "Attachment content hash differs from committed metadata")
        if decode:
            _verify_decodable_static_image(content, record.media_type)
        return content

    @staticmethod
    def _read_range(path: Path, offset: int, length: int) -> bytes:
        with path.open("rb") as stream:
            stream.seek(offset)
            content = stream.read(length)
        if len(content) != length:
            raise AttachmentError("attachment_corrupt", "Attachment range ended before its committed boundary")
        return content

    def _content_path(self, record: _StoredAttachment) -> Path:
        if record.status == "ready":
            return self._final_path(record.artifact_id)
        if record.status == "deleting":
            return self._deleting_path(record.artifact_id)
        staging = self._staging_path(record.artifact_id)
        return staging if staging.exists() else self._final_path(record.artifact_id)

    @staticmethod
    def _require_conversation(record: _StoredAttachment, session_id: str | None) -> None:
        if session_id is not None and record.session_id != session_id:
            raise AttachmentError(
                "attachment_unavailable",
                "Attachment does not belong to this Conversation",
            )

    def _possible_paths(self, artifact_id: str) -> tuple[Path, Path, Path]:
        return self._staging_path(artifact_id), self._final_path(artifact_id), self._deleting_path(artifact_id)

    def _staging_path(self, artifact_id: str) -> Path:
        _require_match(_ARTIFACT_ID, artifact_id, "Artifact ID")
        return self._objects / f"{artifact_id}.part"

    def _final_path(self, artifact_id: str) -> Path:
        _require_match(_ARTIFACT_ID, artifact_id, "Artifact ID")
        return self._objects / artifact_id

    def _deleting_path(self, artifact_id: str) -> Path:
        _require_match(_ARTIFACT_ID, artifact_id, "Artifact ID")
        return self._objects / f"{artifact_id}.deleting"

    def _remove_orphan_files(self) -> None:
        with self._lock, self._connect() as connection:
            known = {
                path.name
                for row in connection.execute("SELECT artifact_id, status FROM attachments").fetchall()
                for path in self._known_paths(str(row[0]), str(row[1]))
            }
        for path in self._objects.iterdir():
            if path.is_symlink() or not path.is_file():
                raise AttachmentError("unsafe_storage", "Attachment object directory contains an unsafe entry")
            if path.name not in known:
                path.unlink()

    def _known_paths(self, artifact_id: str, status: str) -> tuple[Path, ...]:
        if status == "staging":
            return self._staging_path(artifact_id), self._final_path(artifact_id)
        if status == "ready":
            return (self._final_path(artifact_id),)
        return (self._deleting_path(artifact_id),)


def _request_identity(value: _StoredAttachment | AttachmentUploadRequest) -> tuple[object, ...]:
    return (
        value.session_id,
        value.client_request_id,
        value.file_name,
        value.media_type,
        value.byte_length,
        value.content_hash,
    )


def _artifact_ref(record: _StoredAttachment) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=record.artifact_id,
        content_hash=record.content_hash,
        media_type=record.media_type,
        size_bytes=record.byte_length,
        sensitivity=ArtifactSensitivity.PRIVATE,
        state=ArtifactState.COMPLETE,
        title=record.file_name,
    )


def _has_image_signature(content: bytes, media_type: str) -> bool:
    if media_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9")
    if media_type == "image/gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if media_type == "image/webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    return False


def _verify_decodable_static_image(content: bytes, media_type: str) -> None:
    expected_format = _PIL_FORMATS[media_type]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.format != expected_format:
                    raise AttachmentError("invalid_image", "Attachment container does not match its media type")
                width, height = image.size
                if width < 1 or height < 1 or width * height > _MAX_IMAGE_PIXELS:
                    raise AttachmentError("invalid_image", "Attachment image dimensions exceed the safe decode limit")
                if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
                    raise AttachmentError("invalid_image", "Animated image attachments are not supported")
                image.verify()
            with Image.open(io.BytesIO(content)) as decoded:
                decoded.load()
    except AttachmentError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise AttachmentError("invalid_image", "Attachment image dimensions exceed the safe decode limit") from error
    except (OSError, SyntaxError, ValueError) as error:
        raise AttachmentError("invalid_image", "Attachment image format cannot be decoded safely") from error


def _require_match(pattern: re.Pattern[str], value: str, label: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


__all__ = [
    "AttachmentBeginReceipt",
    "AttachmentChunkReceipt",
    "AttachmentClaim",
    "AttachmentClaimReceipt",
    "AttachmentCommitReceipt",
    "AttachmentError",
    "AttachmentLimits",
    "AttachmentReadReceipt",
    "AttachmentRecoveryReport",
    "AttachmentUploadRequest",
    "ClaimedAttachment",
    "ConversationAttachmentStore",
]
