"""Strict, crash-safe Session lifecycle application service.

This module owns the one durable meaning of Session creation and mutation.  It
does not run an Agent Loop, copy conversation content, or own Artifact/Vault
implementations.  All authoritative state, audit events, and idempotency
receipts are committed through one Unit of Work.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

from pydantic import TypeAdapter

from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.error_codes import ResourceConflictCause, ResourceNotFoundCause
from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.permissions import ApprovalState
from offeragent_harness.ports import (
    Clock,
    EntityRecord,
    EntityStore,
    EventSink,
    IdGenerator,
    NewEvent,
    StoredEvent,
    UnitOfWork,
    UnitOfWorkFactory,
)
from offeragent_harness.protocol._base import validate_wire
from offeragent_harness.protocol.common import SessionSummary
from offeragent_harness.protocol.events import (
    EventType,
    SessionUpdatedPayload,
    make_domain_event_record,
    parse_persisted_domain_event,
)
from offeragent_harness.protocol.ids import ProfileId, RunId, SessionId, TurnId, WorkspaceId
from offeragent_harness.sessions import Run, RunKind, Session, SessionStatus, Turn, TurnStatus
from offeragent_harness.tools import canonical_json_sha256

from .approval_manager import ApprovalConflict, ApprovalManager, ApprovalRecord
from .cancellation import CancellationCode, CancellationReason
from .event_bus import DeliveryFailure
from .turn_manager import TurnManager

SESSION_LIFECYCLE_COLLECTION = "session_lifecycle"
SESSION_OPERATION_COLLECTION = "session_operations"
SESSION_MUTATION_COLLECTION = "session_mutations"
SESSION_CREATE_IDEMPOTENCY_COLLECTION = "session_idempotency"

_WORKSPACE_ID: TypeAdapter[str] = TypeAdapter(WorkspaceId)
_PROFILE_ID: TypeAdapter[str] = TypeAdapter(ProfileId)
_SESSION_ID: TypeAdapter[str] = TypeAdapter(SessionId)
_TURN_ID: TypeAdapter[str] = TypeAdapter(TurnId)
_RUN_ID: TypeAdapter[str] = TypeAdapter(RunId)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_ARTIFACT_LINK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_PAGE_SCHEMA_VERSION = 1
_LIFECYCLE_SCHEMA_VERSION = 1
_RECEIPT_SCHEMA_VERSION = 1
_MAX_SCAN_RECORDS = 100_000


class SessionLifecycleError(RuntimeError):
    """Base class for strict Session lifecycle failures."""


class SessionNotFound(SessionLifecycleError, ResourceNotFoundCause):
    pass


class SessionDeleted(SessionLifecycleError, ResourceConflictCause):
    pass


class SessionRevisionConflict(SessionLifecycleError, ResourceConflictCause):
    pass


class SessionIdempotencyConflict(SessionLifecycleError, ResourceConflictCause):
    pass


class SessionProjectionCorrupt(SessionLifecycleError):
    pass


class SessionOperationConflict(SessionLifecycleError, ResourceConflictCause):
    pass


class SessionActiveEffectfulRun(SessionLifecycleError, ResourceConflictCause):
    pass


class SessionActiveRunUnavailable(SessionLifecycleError, ResourceConflictCause):
    pass


class SessionForkRejected(SessionLifecycleError, ResourceConflictCause):
    pass


class SessionCreateState(str, Enum):
    """Durable activation state for one idempotent Session create request."""

    PENDING = "pending"
    ACTIVE = "active"
    ABORTED = "aborted"


def _validate_idempotency_key(value: str) -> None:
    if _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ValueError("idempotency_key must be a 1-256 character canonical opaque key")


def _validate_connection_id(value: str) -> None:
    if _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ValueError("connection_id must be a 1-256 character canonical opaque key")


def _validate_hook_reason_code(value: str) -> None:
    if _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ValueError("Hook reason code must be a 1-256 character canonical opaque key")


def _validate_title(value: str) -> None:
    if not 1 <= len(value) <= 512 or not value.strip() or "\x00" in value:
        raise ValueError("session title must contain 1-512 non-NUL characters and not be blank")


def _validate_expected_revision(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("expected_revision must be a positive integer")


@dataclass(frozen=True, slots=True)
class SessionCreateCommand:
    workspace_id: str
    profile_id: str
    title: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _WORKSPACE_ID.validate_python(self.workspace_id, strict=True)
        _PROFILE_ID.validate_python(self.profile_id, strict=True)
        _validate_title(self.title)
        _validate_idempotency_key(self.idempotency_key)


@dataclass(frozen=True, slots=True)
class SessionGetCommand:
    workspace_id: str
    session_id: str
    include_deleted: bool = False

    def __post_init__(self) -> None:
        _WORKSPACE_ID.validate_python(self.workspace_id, strict=True)
        _SESSION_ID.validate_python(self.session_id, strict=True)
        if type(self.include_deleted) is not bool:
            raise TypeError("include_deleted must be a boolean")


@dataclass(frozen=True, slots=True)
class SessionListCommand:
    workspace_id: str
    cursor: str | None = None
    limit: int = 50
    include_deleted: bool = False

    def __post_init__(self) -> None:
        _WORKSPACE_ID.validate_python(self.workspace_id, strict=True)
        if self.cursor is not None and not 1 <= len(self.cursor) <= 1024:
            raise ValueError("cursor must contain between 1 and 1024 characters")
        if type(self.limit) is not int or not 1 <= self.limit <= 500:
            raise ValueError("limit must be an integer between 1 and 500")
        if type(self.include_deleted) is not bool:
            raise TypeError("include_deleted must be a boolean")


@dataclass(frozen=True, slots=True)
class SessionRenameCommand:
    workspace_id: str
    session_id: str
    title: str
    expected_revision: int
    idempotency_key: str

    def __post_init__(self) -> None:
        _WORKSPACE_ID.validate_python(self.workspace_id, strict=True)
        _SESSION_ID.validate_python(self.session_id, strict=True)
        _validate_title(self.title)
        _validate_expected_revision(self.expected_revision)
        _validate_idempotency_key(self.idempotency_key)


@dataclass(frozen=True, slots=True)
class SessionDeleteCommand:
    workspace_id: str
    session_id: str
    expected_revision: int
    idempotency_key: str

    def __post_init__(self) -> None:
        _WORKSPACE_ID.validate_python(self.workspace_id, strict=True)
        _SESSION_ID.validate_python(self.session_id, strict=True)
        _validate_expected_revision(self.expected_revision)
        _validate_idempotency_key(self.idempotency_key)


@dataclass(frozen=True, slots=True)
class SessionForkCommand:
    workspace_id: str
    source_session_id: str
    fork_turn_id: str
    expected_revision: int
    idempotency_key: str
    fork_run_id: str | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        _WORKSPACE_ID.validate_python(self.workspace_id, strict=True)
        _SESSION_ID.validate_python(self.source_session_id, strict=True)
        _TURN_ID.validate_python(self.fork_turn_id, strict=True)
        if self.fork_run_id is not None:
            _RUN_ID.validate_python(self.fork_run_id, strict=True)
        if self.title is not None:
            _validate_title(self.title)
        _validate_expected_revision(self.expected_revision)
        _validate_idempotency_key(self.idempotency_key)


@dataclass(frozen=True, slots=True)
class SessionForkReference:
    source_workspace_id: str
    source_session_id: str
    source_turn_id: str
    source_run_id: str
    source_event_sequence: int
    artifact_link_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _WORKSPACE_ID.validate_python(self.source_workspace_id, strict=True)
        _SESSION_ID.validate_python(self.source_session_id, strict=True)
        _TURN_ID.validate_python(self.source_turn_id, strict=True)
        _RUN_ID.validate_python(self.source_run_id, strict=True)
        if type(self.source_event_sequence) is not int or self.source_event_sequence < 1:
            raise ValueError("source_event_sequence must be a positive integer")
        if len(self.artifact_link_ids) > 10_000:
            raise ValueError("fork reference cannot contain more than 10000 Artifact link IDs")
        if any(_ARTIFACT_LINK_ID.fullmatch(item) is None for item in self.artifact_link_ids):
            raise ValueError("Artifact link IDs must be canonical opaque IDs")
        if tuple(sorted(set(self.artifact_link_ids))) != self.artifact_link_ids:
            raise ValueError("Artifact link IDs must be sorted and unique")


@dataclass(frozen=True, slots=True)
class SessionCreateResult:
    session: SessionSummary
    revision: int
    created: bool


@dataclass(frozen=True, slots=True)
class SessionCreatePreparation:
    """Opaque, durable preparation returned before SessionStart is evaluated.

    A pending preparation deliberately has no row in the public ``sessions``
    projection.  The application layer may therefore run a veto-capable Hook
    without briefly exposing an active Session.
    """

    command: SessionCreateCommand
    receipt_id: str
    request_hash: str
    session_id: str
    connection_id: str
    created_at: datetime
    state: SessionCreateState
    result: SessionCreateResult | None = None
    hook_decision: str | None = None
    hook_reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.state is SessionCreateState.ACTIVE:
            if self.result is None or self.hook_decision != "continue" or self.hook_reason_code is None:
                raise ValueError("active Session creation requires a continue outcome and result")
        elif self.state is SessionCreateState.ABORTED:
            if self.result is not None or self.hook_decision not in {"ask", "deny"} or self.hook_reason_code is None:
                raise ValueError("aborted Session creation requires a durable ask/deny outcome")
        elif self.result is not None or self.hook_decision is not None or self.hook_reason_code is not None:
            raise ValueError("pending Session creation cannot already contain a Hook outcome")


@dataclass(frozen=True, slots=True)
class SessionGetResult:
    session: SessionSummary
    revision: int
    fork_reference: SessionForkReference | None


@dataclass(frozen=True, slots=True)
class SessionListResult:
    sessions: tuple[SessionSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class SessionRenameResult:
    session: SessionSummary
    revision: int
    updated: bool


@dataclass(frozen=True, slots=True)
class SessionDeleteResult:
    session_id: str
    workspace_id: str
    revision: int
    deleted: bool
    deleted_at: datetime
    purge_after: datetime
    active_runs_cancel_requested: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionForkResult:
    session: SessionSummary
    revision: int
    source_session_id: str
    fork_turn_id: str
    fork_reference: SessionForkReference
    created: bool


@dataclass(slots=True)
class SessionLifecycleDiagnostics:
    delivery_failures: list[DeliveryFailure] = field(default_factory=list)


class ArtifactLinkResolver(Protocol):
    """Optional read-only boundary; the Session service never owns Artifacts."""

    def __call__(
        self,
        workspace_id: str,
        source_session_id: str,
        source_turn_id: str,
        source_run_id: str,
    ) -> Awaitable[Sequence[str]]: ...


@dataclass(frozen=True, slots=True)
class _Lifecycle:
    session_id: str
    workspace_id: str
    event_sequence: int
    deleted_at: datetime | None
    purge_after: datetime | None
    fork_reference: SessionForkReference | None


@dataclass(frozen=True, slots=True)
class _ProjectedSession:
    record: EntityRecord
    lifecycle_record: EntityRecord
    session: Session
    lifecycle: _Lifecycle
    summary: SessionSummary
    turns: tuple[Turn, ...]
    runs: tuple[Run, ...]


@dataclass(frozen=True, slots=True)
class _Projection:
    sessions: Mapping[str, _ProjectedSession]


@dataclass(frozen=True, slots=True)
class _DecodedCreateOperation:
    state: SessionCreateState
    session_id: str
    workspace_id: str
    profile_id: str | None
    title: str | None
    connection_id: str | None
    created_at: datetime | None
    hook_decision: str | None
    hook_reason_code: str | None


class SessionLifecycleService:
    """Authoritative Session create/list/get/rename/delete/fork service."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        event_sink: EventSink,
        clock: Clock,
        ids: IdGenerator,
        turn_manager: TurnManager,
        approval_manager: ApprovalManager,
        retention_period: timedelta = timedelta(days=30),
        artifact_link_resolver: ArtifactLinkResolver | None = None,
        delivery_failures: list[DeliveryFailure] | None = None,
    ) -> None:
        if retention_period.total_seconds() <= 0:
            raise ValueError("retention_period must be positive")
        self._unit_of_work = unit_of_work
        self._event_sink = event_sink
        self._clock = clock
        self._ids = ids
        self._turn_manager = turn_manager
        self._approval_manager = approval_manager
        self._retention_period = retention_period
        self._artifact_link_resolver = artifact_link_resolver
        self.diagnostics = SessionLifecycleDiagnostics(
            delivery_failures=delivery_failures if delivery_failures is not None else []
        )

    async def create(self, command: SessionCreateCommand) -> SessionCreateResult:
        """Create without an external activation gate.

        Transport-facing composition uses :meth:`begin_create` followed by a
        durable Hook outcome and :meth:`activate_create`/``abort_create``.
        Keeping this convenience method preserves the domain service API for
        callers that intentionally have no SessionStart Hook provider.
        """

        prepared = await self.begin_create(command, connection_id="direct")
        if prepared.state is SessionCreateState.ACTIVE:
            assert prepared.result is not None
            return prepared.result
        if prepared.state is SessionCreateState.ABORTED:
            raise SessionOperationConflict("Session creation was durably aborted by its activation gate")
        return await self.activate_create(prepared, hook_reason_code="session_start_not_configured")

    async def begin_create(
        self,
        command: SessionCreateCommand,
        *,
        connection_id: str,
    ) -> SessionCreatePreparation:
        """Persist an invisible pending Session before invoking SessionStart.

        The first connection identity is bound to the idempotency key.  A
        retry after reconnect/restart therefore reuses the same Hook request
        identity instead of evaluating a second Hook chain.
        """

        _validate_connection_id(connection_id)
        request_hash = canonical_json_sha256(
            {
                "workspaceId": command.workspace_id,
                "profileId": command.profile_id,
                "title": command.title,
            }
        )
        receipt_id = f"{command.workspace_id}:{command.profile_id}:{command.idempotency_key}"
        now = self._clock.utcnow()
        session_id = self._ids.new_id("ses")
        pending = SessionCreatePreparation(
            command=command,
            receipt_id=receipt_id,
            request_hash=request_hash,
            session_id=session_id,
            connection_id=connection_id,
            created_at=now,
            state=SessionCreateState.PENDING,
        )
        try:
            async with self._unit_of_work.begin() as uow:
                existing = await uow.entities.get(SESSION_CREATE_IDEMPOTENCY_COLLECTION, receipt_id)
                if existing is not None:
                    return await self._decode_create_preparation_in_uow(
                        uow,
                        command=command,
                        receipt_id=receipt_id,
                        request_hash=request_hash,
                        value=existing,
                        fallback_connection_id=connection_id,
                    )
                await uow.entities.put(
                    SESSION_CREATE_IDEMPOTENCY_COLLECTION,
                    receipt_id,
                    _encode_create_operation(pending),
                    expected_revision=0,
                )
                await uow.commit()
        except BaseException as error:
            recovered = await self._recover_create_preparation(
                command,
                receipt_id,
                request_hash,
                connection_id,
            )
            if recovered is None:
                raise
            if not isinstance(error, Exception):
                raise
            return recovered
        return pending

    async def activate_create(
        self,
        prepared: SessionCreatePreparation,
        *,
        hook_reason_code: str,
    ) -> SessionCreateResult:
        """Atomically publish one active Session and the durable allow outcome."""

        _validate_hook_reason_code(hook_reason_code)
        if prepared.state is SessionCreateState.ACTIVE:
            assert prepared.result is not None
            return prepared.result
        if prepared.state is SessionCreateState.ABORTED:
            raise SessionOperationConflict("aborted Session creation cannot be activated")

        stored: tuple[StoredEvent, ...] = ()
        result: SessionCreateResult
        try:
            async with self._unit_of_work.begin() as uow:
                existing = await uow.entities.get(
                    SESSION_CREATE_IDEMPOTENCY_COLLECTION,
                    prepared.receipt_id,
                )
                if existing is None:
                    raise SessionProjectionCorrupt("pending Session creation receipt disappeared")
                current = await self._decode_create_preparation_in_uow(
                    uow,
                    command=prepared.command,
                    receipt_id=prepared.receipt_id,
                    request_hash=prepared.request_hash,
                    value=existing,
                    fallback_connection_id=prepared.connection_id,
                )
                _require_same_create_preparation(prepared, current)
                if current.state is SessionCreateState.ACTIVE:
                    assert current.result is not None
                    return current.result
                if current.state is SessionCreateState.ABORTED:
                    raise SessionOperationConflict("aborted Session creation cannot be activated")

                session = Session(
                    session_id=current.session_id,
                    workspace_id=current.command.workspace_id,
                    profile_id=current.command.profile_id,
                    title=current.command.title,
                    status=SessionStatus.ACTIVE,
                    created_at=current.created_at,
                    updated_at=current.created_at,
                    revision=1,
                )
                lifecycle = _Lifecycle(current.session_id, current.command.workspace_id, 1, None, None, None)
                summary = _make_summary(session, turn_count=0, active_run_id=None)
                result = SessionCreateResult(summary, 1, True)
                event = _session_updated_event(
                    operation="create",
                    request_hash=current.request_hash,
                    workspace_id=current.command.workspace_id,
                    session=summary,
                    changed_fields=("created", "title", "updatedAt"),
                    state_revision=1,
                    occurred_at=current.created_at,
                )
                await uow.entities.put("sessions", current.session_id, session, expected_revision=0)
                await uow.entities.put(
                    SESSION_LIFECYCLE_COLLECTION,
                    current.session_id,
                    _encode_lifecycle(lifecycle),
                    expected_revision=0,
                )
                stored = await uow.events.append(current.session_id, 0, (event,))
                completed = replace(
                    current,
                    state=SessionCreateState.ACTIVE,
                    result=result,
                    hook_decision="continue",
                    hook_reason_code=hook_reason_code,
                )
                await uow.entities.put(
                    SESSION_CREATE_IDEMPOTENCY_COLLECTION,
                    current.receipt_id,
                    _encode_create_operation(completed),
                    expected_revision=1,
                )
                await uow.commit()
        except BaseException as error:
            recovered = await self._recover_create_preparation(
                prepared.command,
                prepared.receipt_id,
                prepared.request_hash,
                prepared.connection_id,
            )
            if recovered is None or recovered.state is not SessionCreateState.ACTIVE:
                raise
            if not isinstance(error, Exception):
                raise
            assert recovered.result is not None
            result = replace(recovered.result, created=True)
            async with self._unit_of_work.begin() as uow:
                events = await uow.events.read(recovered.session_id)
                stored = (_find_mutation_event(events, "create", prepared.request_hash),)
        await self._publish(stored)
        return result

    async def abort_create(
        self,
        prepared: SessionCreatePreparation,
        *,
        hook_decision: str,
        hook_reason_code: str,
    ) -> SessionCreatePreparation:
        """Soft-abort one pending create and retain its veto for exact replay."""

        if hook_decision not in {"ask", "deny"}:
            raise ValueError("SessionStart abort decision must be ask or deny")
        _validate_hook_reason_code(hook_reason_code)
        if prepared.state is SessionCreateState.ACTIVE:
            raise SessionOperationConflict("active Session creation cannot be aborted")
        if prepared.state is SessionCreateState.ABORTED:
            return prepared

        aborted: SessionCreatePreparation
        try:
            async with self._unit_of_work.begin() as uow:
                existing = await uow.entities.get(
                    SESSION_CREATE_IDEMPOTENCY_COLLECTION,
                    prepared.receipt_id,
                )
                if existing is None:
                    raise SessionProjectionCorrupt("pending Session creation receipt disappeared")
                current = await self._decode_create_preparation_in_uow(
                    uow,
                    command=prepared.command,
                    receipt_id=prepared.receipt_id,
                    request_hash=prepared.request_hash,
                    value=existing,
                    fallback_connection_id=prepared.connection_id,
                )
                _require_same_create_preparation(prepared, current)
                if current.state is SessionCreateState.ACTIVE:
                    raise SessionOperationConflict("active Session creation cannot be aborted")
                if current.state is SessionCreateState.ABORTED:
                    return current
                aborted = replace(
                    current,
                    state=SessionCreateState.ABORTED,
                    hook_decision=hook_decision,
                    hook_reason_code=hook_reason_code,
                )
                await uow.entities.put(
                    SESSION_CREATE_IDEMPOTENCY_COLLECTION,
                    current.receipt_id,
                    _encode_create_operation(aborted),
                    expected_revision=1,
                )
                await uow.commit()
        except BaseException as error:
            recovered = await self._recover_create_preparation(
                prepared.command,
                prepared.receipt_id,
                prepared.request_hash,
                prepared.connection_id,
            )
            if recovered is None or recovered.state is not SessionCreateState.ABORTED:
                raise
            if not isinstance(error, Exception):
                raise
            aborted = recovered
        return aborted

    async def get(self, command: SessionGetCommand) -> SessionGetResult:
        async with self._unit_of_work.begin() as uow:
            projection = await _load_projection(uow)
            projected = _select_session(projection, command.workspace_id, command.session_id)
            if projected.session.status is SessionStatus.DELETED and not command.include_deleted:
                raise SessionDeleted(f"session {command.session_id!r} is deleted")
            return SessionGetResult(
                projected.summary,
                projected.session.revision,
                projected.lifecycle.fork_reference,
            )

    async def list(self, command: SessionListCommand) -> SessionListResult:
        after_id = _decode_cursor(command.cursor, command.workspace_id, command.include_deleted)
        async with self._unit_of_work.begin() as uow:
            projection = await _load_projection(uow)
            candidates = tuple(
                projected
                for session_id, projected in sorted(projection.sessions.items())
                if session_id > (after_id or "")
                and projected.session.workspace_id == command.workspace_id
                and (command.include_deleted or projected.session.status is not SessionStatus.DELETED)
            )
        selected = candidates[: command.limit]
        has_more = len(candidates) > command.limit
        next_cursor = None
        if has_more and selected:
            next_cursor = _encode_cursor(command.workspace_id, command.include_deleted, selected[-1].session.session_id)
        return SessionListResult(tuple(item.summary for item in selected), next_cursor)

    async def rename(self, command: SessionRenameCommand) -> SessionRenameResult:
        request_hash = canonical_json_sha256(
            {
                "operation": "rename",
                "workspaceId": command.workspace_id,
                "sessionId": command.session_id,
                "title": command.title,
                "expectedRevision": command.expected_revision,
            }
        )
        receipt_id = _mutation_receipt_id(command.workspace_id, command.idempotency_key)
        stored: tuple[StoredEvent, ...] = ()
        result: SessionRenameResult
        try:
            async with self._unit_of_work.begin() as uow:
                existing = await uow.entities.get(SESSION_MUTATION_COLLECTION, receipt_id)
                if existing is not None:
                    return _decode_rename_receipt(existing, request_hash)
                projection = await _load_projection(uow)
                projected = _select_session(projection, command.workspace_id, command.session_id)
                _require_mutable(projected)
                await _require_no_operation(uow.entities, command.session_id)
                _require_revision(projected.session, command.expected_revision)
                now = self._clock.utcnow()
                updated = replace(
                    projected.session,
                    title=command.title,
                    updated_at=now,
                    revision=projected.session.revision + 1,
                )
                summary = _make_summary(
                    updated,
                    turn_count=len(projected.turns),
                    active_run_id=projected.summary.active_run_id,
                )
                result = SessionRenameResult(summary, updated.revision, True)
                lifecycle = replace(projected.lifecycle, event_sequence=projected.lifecycle.event_sequence + 1)
                event = _session_updated_event(
                    operation="rename",
                    request_hash=request_hash,
                    workspace_id=command.workspace_id,
                    session=summary,
                    changed_fields=("title", "updatedAt"),
                    state_revision=updated.revision,
                    occurred_at=now,
                )
                await uow.entities.put(
                    "sessions",
                    command.session_id,
                    updated,
                    expected_revision=projected.record.revision,
                )
                await uow.entities.put(
                    SESSION_LIFECYCLE_COLLECTION,
                    command.session_id,
                    _encode_lifecycle(lifecycle),
                    expected_revision=projected.lifecycle_record.revision,
                )
                stored = await uow.events.append(
                    command.session_id,
                    projected.lifecycle.event_sequence,
                    (event,),
                )
                await uow.entities.put(
                    SESSION_MUTATION_COLLECTION,
                    receipt_id,
                    _encode_rename_receipt(request_hash, result),
                    expected_revision=0,
                )
                await uow.commit()
        except BaseException as error:
            recovered = await self._recover_rename(receipt_id, request_hash)
            if recovered is None:
                raise
            if not isinstance(error, Exception):
                raise
            result, stored = recovered
        await self._publish(stored)
        return result

    async def soft_delete(self, command: SessionDeleteCommand) -> SessionDeleteResult:
        request_hash = canonical_json_sha256(
            {
                "operation": "delete",
                "workspaceId": command.workspace_id,
                "sessionId": command.session_id,
                "expectedRevision": command.expected_revision,
            }
        )
        receipt_id = _mutation_receipt_id(command.workspace_id, command.idempotency_key)
        replay = await self._read_delete_receipt(receipt_id, request_hash)
        if replay is not None:
            return replay
        gate_acquired = False
        completed = False
        release_gate_on_failure = True
        cancel_requested: tuple[str, ...] = ()
        try:
            gate_acquired = await self._acquire_delete_gate(command, request_hash, receipt_id)
            replay = await self._read_delete_receipt(receipt_id, request_hash)
            if replay is not None:
                completed = True
                return replay

            authority = await self._load_delete_authority(command)
            active_run_id = authority.summary.active_run_id
            if active_run_id is not None:
                run_state = next(
                    (state for state in await self._load_run_states((active_run_id,)) if state.run_id == active_run_id),
                    None,
                )
                if run_state is None:
                    raise SessionProjectionCorrupt("active root Run has no authoritative RunState")
                if _active_run_is_effectful(run_state):
                    raise SessionActiveEffectfulRun(
                        f"session {command.session_id!r} has active effectful Run {active_run_id!r}"
                    )
                active = await self._turn_manager.get(active_run_id)
                if active is None:
                    raise SessionActiveRunUnavailable(
                        f"active Run {active_run_id!r} is not owned by this Worker and cannot be safely cancelled"
                    )
                cancel_requested = (active_run_id,)
                await self._turn_manager.cancel(
                    active_run_id,
                    CancellationReason.now(CancellationCode.USER, "session soft-delete requested"),
                )
                await asyncio.gather(active.task, return_exceptions=True)
                await self._verify_run_durably_terminal(command.session_id, active_run_id)

            await self._cancel_pending_approvals(command.workspace_id, command.session_id)
            await self._approval_manager.revoke_session_grants(
                command.session_id,
                reason="session soft-deleted",
            )
            result, stored = await self._finalize_delete(
                command,
                request_hash,
                receipt_id,
                cancel_requested,
            )
            completed = True
            await self._publish(stored)
            return result
        except BaseException as error:
            if not isinstance(error, Exception):
                release_gate_on_failure = False
            recovered = await self._recover_delete(receipt_id, request_hash)
            if recovered is not None:
                completed = True
                result, stored = recovered
                if not isinstance(error, Exception):
                    raise
                await self._publish(stored)
                return result
            raise
        finally:
            if gate_acquired and not completed and release_gate_on_failure:
                await self._release_delete_gate(command.session_id, request_hash)

    async def delete(self, command: SessionDeleteCommand) -> SessionDeleteResult:
        """Alias whose semantics remain strictly soft-delete."""

        return await self.soft_delete(command)

    async def fork(self, command: SessionForkCommand) -> SessionForkResult:
        request_hash = canonical_json_sha256(
            {
                "operation": "fork",
                "workspaceId": command.workspace_id,
                "sourceSessionId": command.source_session_id,
                "forkTurnId": command.fork_turn_id,
                "forkRunId": command.fork_run_id,
                "title": command.title,
                "expectedRevision": command.expected_revision,
            }
        )
        receipt_id = _mutation_receipt_id(command.workspace_id, command.idempotency_key)
        stored: tuple[StoredEvent, ...] = ()
        replay = await self._read_fork_receipt(receipt_id, request_hash)
        if replay is not None:
            result, _validated_event = replay
            return replace(result, created=False)

        async with self._unit_of_work.begin() as uow:
            projection = await _load_projection(uow)
            source = _select_session(projection, command.workspace_id, command.source_session_id)
            run = await _validate_fork_boundary(uow, source, command)
        artifact_link_ids = await self._resolve_artifact_links(command, run.run_id)

        session_id = self._ids.new_id("ses")
        now = self._clock.utcnow()
        title = command.title or _default_fork_title(source.session.title)
        fork_reference = SessionForkReference(
            source_workspace_id=command.workspace_id,
            source_session_id=command.source_session_id,
            source_turn_id=command.fork_turn_id,
            source_run_id=run.run_id,
            source_event_sequence=run.event_sequence,
            artifact_link_ids=artifact_link_ids,
        )
        new_session = Session(
            session_id=session_id,
            workspace_id=command.workspace_id,
            profile_id=source.session.profile_id,
            title=title,
            status=SessionStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            revision=1,
            forked_from_session_id=command.source_session_id,
            forked_from_turn_id=command.fork_turn_id,
        )
        lifecycle = _Lifecycle(session_id, command.workspace_id, 1, None, None, fork_reference)
        result = SessionForkResult(
            session=_make_summary(new_session, turn_count=0, active_run_id=None),
            revision=1,
            source_session_id=command.source_session_id,
            fork_turn_id=command.fork_turn_id,
            fork_reference=fork_reference,
            created=True,
        )
        event = _session_updated_event(
            operation="fork",
            request_hash=request_hash,
            workspace_id=command.workspace_id,
            session=result.session,
            changed_fields=("created", "forkReference"),
            state_revision=1,
            occurred_at=now,
            fork_reference=fork_reference,
        )
        try:
            async with self._unit_of_work.begin() as uow:
                existing = await uow.entities.get(SESSION_MUTATION_COLLECTION, receipt_id)
                if existing is not None:
                    replayed, _validated_event = await self._replay_fork_in_uow(uow, existing, request_hash)
                    result = replace(replayed, created=False)
                    stored = ()
                else:
                    projection = await _load_projection(uow)
                    current = _select_session(projection, command.workspace_id, command.source_session_id)
                    current_run = await _validate_fork_boundary(uow, current, command)
                    if current_run.run_id != run.run_id or current_run.event_sequence != run.event_sequence:
                        raise SessionForkRejected("fork boundary changed while Artifact links were resolved")
                    await uow.entities.put("sessions", session_id, new_session, expected_revision=0)
                    await uow.entities.put(
                        SESSION_LIFECYCLE_COLLECTION,
                        session_id,
                        _encode_lifecycle(lifecycle),
                        expected_revision=0,
                    )
                    stored = await uow.events.append(session_id, 0, (event,))
                    await uow.entities.put(
                        SESSION_MUTATION_COLLECTION,
                        receipt_id,
                        _encode_fork_receipt(request_hash, result),
                        expected_revision=0,
                    )
                    await uow.commit()
        except BaseException as error:
            recovered = await self._read_fork_receipt(receipt_id, request_hash)
            if recovered is None:
                raise
            if not isinstance(error, Exception):
                raise
            result, stored = recovered
        await self._publish(stored)
        return result

    async def get_fork_reference(self, workspace_id: str, session_id: str) -> SessionForkReference | None:
        result = await self.get(SessionGetCommand(workspace_id, session_id, include_deleted=True))
        return result.fork_reference

    async def _decode_create_preparation_in_uow(
        self,
        uow: UnitOfWork,
        *,
        command: SessionCreateCommand,
        receipt_id: str,
        request_hash: str,
        value: object,
        fallback_connection_id: str,
    ) -> SessionCreatePreparation:
        decoded = _decode_create_operation(value, request_hash)
        if decoded.workspace_id != command.workspace_id:
            raise SessionIdempotencyConflict("create receipt belongs to a different workspace")
        if decoded.profile_id is not None and decoded.profile_id != command.profile_id:
            raise SessionIdempotencyConflict("create receipt belongs to a different profile")
        if decoded.title is not None and decoded.title != command.title:
            raise SessionIdempotencyConflict("create receipt title differs from the idempotent request")

        result: SessionCreateResult | None = None
        created_at = decoded.created_at
        if decoded.state is SessionCreateState.ACTIVE:
            result, _validated_event = await self._replay_active_create_in_uow(
                uow,
                session_id=decoded.session_id,
                request_hash=request_hash,
                workspace_id=command.workspace_id,
            )
            created_at = _parse_datetime(result.session.created_at, "create Session createdAt")
        if created_at is None:
            raise SessionProjectionCorrupt("create operation is missing its creation timestamp")
        return SessionCreatePreparation(
            command=command,
            receipt_id=receipt_id,
            request_hash=request_hash,
            session_id=decoded.session_id,
            connection_id=decoded.connection_id or fallback_connection_id,
            created_at=created_at,
            state=decoded.state,
            result=result,
            hook_decision=decoded.hook_decision,
            hook_reason_code=decoded.hook_reason_code,
        )

    async def _replay_active_create_in_uow(
        self,
        uow: UnitOfWork,
        *,
        session_id: str,
        request_hash: str,
        workspace_id: str,
    ) -> tuple[SessionCreateResult, tuple[StoredEvent, ...]]:
        projection = await _load_projection(uow)
        projected = _select_session(projection, workspace_id, session_id)
        events = await uow.events.read(session_id)
        event = _find_mutation_event(events, "create", request_hash)
        _validate_event_summary(event, session_id=session_id, workspace_id=workspace_id, revision=1)
        if projected.session.created_at != event.occurred_at:
            raise SessionProjectionCorrupt("create event timestamp differs from Session creation")
        return SessionCreateResult(projected.summary, projected.session.revision, False), (event,)

    async def _recover_create_preparation(
        self,
        command: SessionCreateCommand,
        receipt_id: str,
        request_hash: str,
        fallback_connection_id: str,
    ) -> SessionCreatePreparation | None:
        async with self._unit_of_work.begin() as uow:
            existing = await uow.entities.get(SESSION_CREATE_IDEMPOTENCY_COLLECTION, receipt_id)
            if existing is None:
                return None
            return await self._decode_create_preparation_in_uow(
                uow,
                command=command,
                receipt_id=receipt_id,
                request_hash=request_hash,
                value=existing,
                fallback_connection_id=fallback_connection_id,
            )

    async def _recover_rename(
        self,
        receipt_id: str,
        request_hash: str,
    ) -> tuple[SessionRenameResult, tuple[StoredEvent, ...]] | None:
        async with self._unit_of_work.begin() as uow:
            value = await uow.entities.get(SESSION_MUTATION_COLLECTION, receipt_id)
            if value is None:
                return None
            result = _decode_rename_receipt(value, request_hash)
            events = await uow.events.read(result.session.session_id)
            event = _find_mutation_event(events, "rename", request_hash)
            return result, (event,)

    async def _read_delete_receipt(
        self,
        receipt_id: str,
        request_hash: str,
    ) -> SessionDeleteResult | None:
        async with self._unit_of_work.begin() as uow:
            value = await uow.entities.get(SESSION_MUTATION_COLLECTION, receipt_id)
        return None if value is None else _decode_delete_receipt(value, request_hash)

    async def _recover_delete(
        self,
        receipt_id: str,
        request_hash: str,
    ) -> tuple[SessionDeleteResult, tuple[StoredEvent, ...]] | None:
        async with self._unit_of_work.begin() as uow:
            value = await uow.entities.get(SESSION_MUTATION_COLLECTION, receipt_id)
            if value is None:
                return None
            result = _decode_delete_receipt(value, request_hash)
            events = await uow.events.read(result.session_id)
            event = _find_mutation_event(events, "delete", request_hash)
            return result, (event,)

    async def _read_fork_receipt(
        self,
        receipt_id: str,
        request_hash: str,
    ) -> tuple[SessionForkResult, tuple[StoredEvent, ...]] | None:
        async with self._unit_of_work.begin() as uow:
            value = await uow.entities.get(SESSION_MUTATION_COLLECTION, receipt_id)
            if value is None:
                return None
            return await self._replay_fork_in_uow(uow, value, request_hash)

    async def _replay_fork_in_uow(
        self,
        uow: UnitOfWork,
        value: object,
        request_hash: str,
    ) -> tuple[SessionForkResult, tuple[StoredEvent, ...]]:
        result = _decode_fork_receipt(value, request_hash)
        projection = await _load_projection(uow)
        projected = _select_session(projection, result.session.workspace_id, result.session.session_id)
        if projected.lifecycle.fork_reference != result.fork_reference:
            raise SessionProjectionCorrupt("fork receipt and immutable lifecycle reference differ")
        if projected.session.forked_from_session_id != result.source_session_id:
            raise SessionProjectionCorrupt("fork receipt and destination Session source differ")
        events = await uow.events.read(result.session.session_id)
        event = _find_mutation_event(events, "fork", request_hash)
        _validate_event_summary(
            event,
            session_id=result.session.session_id,
            workspace_id=result.session.workspace_id,
            revision=1,
            expected_summary=result.session,
            expected_fork_reference=result.fork_reference,
        )
        return result, (event,)

    async def _acquire_delete_gate(
        self,
        command: SessionDeleteCommand,
        request_hash: str,
        receipt_id: str,
    ) -> bool:
        gate = {
            "schemaVersion": 1,
            "operation": "delete",
            "workspaceId": command.workspace_id,
            "sessionId": command.session_id,
            "requestHash": request_hash,
            "idempotencyKey": command.idempotency_key,
            "expectedRevision": command.expected_revision,
            "acquiredAt": _rfc3339(self._clock.utcnow()),
        }
        try:
            async with self._unit_of_work.begin() as uow:
                receipt = await uow.entities.get(SESSION_MUTATION_COLLECTION, receipt_id)
                if receipt is not None:
                    _decode_delete_receipt(receipt, request_hash)
                    return False
                projection = await _load_projection(uow)
                projected = _select_session(projection, command.workspace_id, command.session_id)
                _require_mutable(projected)
                _require_revision(projected.session, command.expected_revision)
                existing = await uow.entities.get(SESSION_OPERATION_COLLECTION, command.session_id)
                if existing is not None:
                    _require_same_delete_gate(existing, request_hash, command)
                    return True
                await uow.entities.put(
                    SESSION_OPERATION_COLLECTION,
                    command.session_id,
                    gate,
                    expected_revision=0,
                )
                await uow.commit()
        except BaseException as error:
            async with self._unit_of_work.begin() as uow:
                existing = await uow.entities.get(SESSION_OPERATION_COLLECTION, command.session_id)
            if existing is None:
                raise
            _require_same_delete_gate(existing, request_hash, command)
            if not isinstance(error, Exception):
                raise
        return True

    async def _release_delete_gate(self, session_id: str, request_hash: str) -> None:
        try:
            async with self._unit_of_work.begin() as uow:
                record = await _find_entity_record(uow.entities, SESSION_OPERATION_COLLECTION, session_id)
                if record is None:
                    return
                value = _strict_mapping(record.value, _DELETE_GATE_FIELDS, "session delete gate")
                if value["requestHash"] != request_hash:
                    return
                await uow.entities.delete(
                    SESSION_OPERATION_COLLECTION,
                    session_id,
                    expected_revision=record.revision,
                )
                await uow.commit()
        except Exception:
            # A retained gate fails closed and is recoverable by the same
            # idempotency key.  Cleanup failure must never widen start access.
            return

    async def _load_delete_authority(self, command: SessionDeleteCommand) -> _ProjectedSession:
        async with self._unit_of_work.begin() as uow:
            projection = await _load_projection(uow)
            projected = _select_session(projection, command.workspace_id, command.session_id)
            _require_mutable(projected)
            _require_revision(projected.session, command.expected_revision)
            gate = await uow.entities.get(SESSION_OPERATION_COLLECTION, command.session_id)
            _require_same_delete_gate(
                gate,
                canonical_json_sha256(
                    {
                        "operation": "delete",
                        "workspaceId": command.workspace_id,
                        "sessionId": command.session_id,
                        "expectedRevision": command.expected_revision,
                    }
                ),
                command,
            )
            return projected

    async def _load_run_states(self, run_ids: Sequence[str]) -> tuple[RunState, ...]:
        states: list[RunState] = []
        async with self._unit_of_work.begin() as uow:
            for run_id in run_ids:
                value = await uow.entities.get("run_states", run_id)
                if not isinstance(value, RunState) or value.run_id != run_id:
                    raise SessionProjectionCorrupt(f"RunState {run_id!r} is missing or corrupt")
                states.append(value)
        return tuple(states)

    async def _verify_run_durably_terminal(self, session_id: str, run_id: str) -> None:
        async with self._unit_of_work.begin() as uow:
            lease = await uow.entities.get("active_root_runs", session_id)
            run = await uow.entities.get("runs", run_id)
            state = await uow.entities.get("run_states", run_id)
            terminal = await uow.events.terminal_event(run_id)
            latest = await uow.events.latest_sequence(run_id)
        if lease is not None:
            raise SessionActiveRunUnavailable("cancelled Run still owns the durable Session lease")
        if not isinstance(run, Run) or not run.status.is_terminal:
            raise SessionActiveRunUnavailable("cancelled Run did not reach a durable terminal Run status")
        if not isinstance(state, RunState) or not state.phase.terminal:
            raise SessionActiveRunUnavailable("cancelled Run did not reach a durable terminal RunState")
        if terminal is None or terminal.sequence != run.event_sequence or latest != run.event_sequence:
            raise SessionActiveRunUnavailable("cancelled Run terminal event/cursor is not durable")

    async def _cancel_pending_approvals(self, workspace_id: str, session_id: str) -> None:
        async with self._unit_of_work.begin() as uow:
            records = await _scan_entities(uow.entities, "approvals")
            pending_ids: list[str] = []
            for record in records:
                approval = _checked_approval(record)
                binding = approval.request.binding
                if (
                    approval.state is ApprovalState.PENDING
                    and binding.workspace_id == workspace_id
                    and binding.session_id == session_id
                ):
                    pending_ids.append(approval.request.approval_id)
        for approval_id in pending_ids:
            try:
                await self._approval_manager.cancel(approval_id, "session soft-deleted")
            except ApprovalConflict:
                pending = await self._approval_manager.pending(approval_id)
                if pending is not None:
                    raise

    async def _finalize_delete(
        self,
        command: SessionDeleteCommand,
        request_hash: str,
        receipt_id: str,
        cancel_requested: tuple[str, ...],
    ) -> tuple[SessionDeleteResult, tuple[StoredEvent, ...]]:
        async with self._unit_of_work.begin() as uow:
            existing_receipt = await uow.entities.get(SESSION_MUTATION_COLLECTION, receipt_id)
            if existing_receipt is not None:
                result = _decode_delete_receipt(existing_receipt, request_hash)
                events = await uow.events.read(command.session_id)
                _find_mutation_event(events, "delete", request_hash)
                return result, ()
            projection = await _load_projection(uow)
            projected = _select_session(projection, command.workspace_id, command.session_id)
            _require_mutable(projected)
            _require_revision(projected.session, command.expected_revision)
            gate_record = await _find_entity_record(
                uow.entities,
                SESSION_OPERATION_COLLECTION,
                command.session_id,
            )
            if gate_record is None:
                raise SessionOperationConflict("session delete gate disappeared before final commit")
            _require_same_delete_gate(gate_record.value, request_hash, command)
            if projected.summary.active_run_id is not None:
                raise SessionActiveRunUnavailable("session acquired an active Run during deletion")
            approvals = await _scan_entities(uow.entities, "approvals")
            for record in approvals:
                approval = _checked_approval(record)
                binding = approval.request.binding
                if (
                    approval.state is ApprovalState.PENDING
                    and binding.workspace_id == command.workspace_id
                    and binding.session_id == command.session_id
                ):
                    raise SessionOperationConflict("a pending approval remains at delete commit")

            now = self._clock.utcnow()
            purge_after = now + self._retention_period
            updated = replace(
                projected.session,
                status=SessionStatus.DELETED,
                updated_at=now,
                revision=projected.session.revision + 1,
            )
            summary = _make_summary(updated, turn_count=len(projected.turns), active_run_id=None)
            lifecycle = replace(
                projected.lifecycle,
                event_sequence=projected.lifecycle.event_sequence + 1,
                deleted_at=now,
                purge_after=purge_after,
            )
            result = SessionDeleteResult(
                session_id=command.session_id,
                workspace_id=command.workspace_id,
                revision=updated.revision,
                deleted=True,
                deleted_at=now,
                purge_after=purge_after,
                active_runs_cancel_requested=cancel_requested,
            )
            event = _session_updated_event(
                operation="delete",
                request_hash=request_hash,
                workspace_id=command.workspace_id,
                session=summary,
                changed_fields=("deleted", "updatedAt"),
                state_revision=updated.revision,
                occurred_at=now,
            )
            await uow.entities.put(
                "sessions",
                command.session_id,
                updated,
                expected_revision=projected.record.revision,
            )
            await uow.entities.put(
                SESSION_LIFECYCLE_COLLECTION,
                command.session_id,
                _encode_lifecycle(lifecycle),
                expected_revision=projected.lifecycle_record.revision,
            )
            stored = await uow.events.append(
                command.session_id,
                projected.lifecycle.event_sequence,
                (event,),
            )
            await uow.entities.put(
                SESSION_MUTATION_COLLECTION,
                receipt_id,
                _encode_delete_receipt(request_hash, result),
                expected_revision=0,
            )
            await uow.entities.delete(
                SESSION_OPERATION_COLLECTION,
                command.session_id,
                expected_revision=gate_record.revision,
            )
            await uow.commit()
            return result, stored

    async def _resolve_artifact_links(self, command: SessionForkCommand, run_id: str) -> tuple[str, ...]:
        if self._artifact_link_resolver is None:
            return ()
        raw = await self._artifact_link_resolver(
            command.workspace_id,
            command.source_session_id,
            command.fork_turn_id,
            run_id,
        )
        if isinstance(raw, (str, bytes)):
            raise SessionForkRejected("Artifact link resolver must return a sequence of IDs")
        resolved = tuple(sorted(set(raw)))
        if len(resolved) != len(raw):
            raise SessionForkRejected("Artifact link resolver returned duplicate IDs")
        try:
            SessionForkReference(
                command.workspace_id,
                command.source_session_id,
                command.fork_turn_id,
                run_id,
                1,
                resolved,
            )
        except (TypeError, ValueError) as error:
            raise SessionForkRejected("Artifact link resolver returned invalid IDs") from error
        return resolved

    async def _publish(self, events: Sequence[StoredEvent]) -> None:
        if not events:
            return
        try:
            await self._event_sink.publish(events)
        except Exception as error:
            self.diagnostics.delivery_failures.append(
                DeliveryFailure(
                    event_ids=tuple(event.event_id for event in events),
                    error_type=type(error).__name__,
                    message=str(error),
                )
            )


async def _load_projection(uow: UnitOfWork) -> _Projection:
    session_records = await _scan_entities(uow.entities, "sessions")
    lifecycle_records = await _scan_entities(uow.entities, SESSION_LIFECYCLE_COLLECTION)
    turn_records = await _scan_entities(uow.entities, "turns")
    run_records = await _scan_entities(uow.entities, "runs")
    lease_records = await _scan_entities(uow.entities, "active_root_runs")

    sessions: dict[str, tuple[EntityRecord, Session]] = {}
    for record in session_records:
        session = record.value
        if not isinstance(session, Session) or session.session_id != record.entity_id:
            raise SessionProjectionCorrupt("Session entity key/value is corrupt")
        try:
            _SESSION_ID.validate_python(session.session_id, strict=True)
            _WORKSPACE_ID.validate_python(session.workspace_id, strict=True)
            _PROFILE_ID.validate_python(session.profile_id, strict=True)
            _validate_title(session.title)
        except (TypeError, ValueError) as error:
            raise SessionProjectionCorrupt("Session identity/title no longer passes the strict schema") from error
        if record.revision != session.revision or session.revision < 1:
            raise SessionProjectionCorrupt("Session entity/domain revisions differ")
        sessions[session.session_id] = (record, session)

    lifecycles: dict[str, tuple[EntityRecord, _Lifecycle]] = {}
    for record in lifecycle_records:
        lifecycle = _decode_lifecycle(record.value)
        if lifecycle.session_id != record.entity_id or lifecycle.session_id in lifecycles:
            raise SessionProjectionCorrupt("Session lifecycle key is corrupt or duplicated")
        lifecycles[lifecycle.session_id] = (record, lifecycle)
    if set(lifecycles) != set(sessions):
        raise SessionProjectionCorrupt("Session and lifecycle entity sets differ")

    turns_by_session: dict[str, list[Turn]] = {session_id: [] for session_id in sessions}
    ordinals: dict[str, set[int]] = {session_id: set() for session_id in sessions}
    turns: dict[str, Turn] = {}
    for record in turn_records:
        turn = record.value
        if not isinstance(turn, Turn) or turn.turn_id != record.entity_id or record.revision != turn.revision:
            raise SessionProjectionCorrupt("Turn entity key/value/revision is corrupt")
        if turn.session_id not in sessions:
            raise SessionProjectionCorrupt("Turn references a missing Session")
        if turn.turn_id in turns or turn.ordinal in ordinals[turn.session_id]:
            raise SessionProjectionCorrupt("Turn identity or Session ordinal is duplicated")
        if turn.updated_at < turn.created_at:
            raise SessionProjectionCorrupt("Turn updated_at precedes created_at")
        turns[turn.turn_id] = turn
        ordinals[turn.session_id].add(turn.ordinal)
        turns_by_session[turn.session_id].append(turn)

    runs_by_session: dict[str, list[Run]] = {session_id: [] for session_id in sessions}
    runs: dict[str, Run] = {}
    nonterminal_roots: dict[str, Run] = {}
    for record in run_records:
        run = record.value
        if not isinstance(run, Run) or run.run_id != record.entity_id:
            raise SessionProjectionCorrupt("Run entity key/value is corrupt")
        session_pair = sessions.get(run.session_id)
        turn = turns.get(run.turn_id)
        if session_pair is None or turn is None or turn.session_id != run.session_id:
            raise SessionProjectionCorrupt("Run references a missing or foreign Session/Turn")
        if session_pair[1].workspace_id != run.workspace_id:
            raise SessionProjectionCorrupt("Run and Session workspace identities differ")
        if run.run_id in runs:
            raise SessionProjectionCorrupt("Run identity is duplicated")
        latest = await uow.events.latest_sequence(run.run_id)
        terminal = await uow.events.terminal_event(run.run_id)
        if latest != run.event_sequence:
            raise SessionProjectionCorrupt("Run event cursor differs from Event Store")
        if run.status.is_terminal != (terminal is not None):
            raise SessionProjectionCorrupt("Run status and terminal Event Store fact differ")
        if terminal is not None:
            if terminal.sequence != run.event_sequence:
                raise SessionProjectionCorrupt("Run terminal event is not at the authoritative cursor")
            parsed = parse_persisted_domain_event(terminal.payload)
            if parsed.run_id != run.run_id or parsed.session_id != run.session_id or parsed.turn_id != run.turn_id:
                raise SessionProjectionCorrupt("Run terminal event lineage is corrupt")
        elif run.kind is RunKind.ROOT:
            if run.session_id in nonterminal_roots:
                raise SessionProjectionCorrupt("Session has multiple nonterminal root Runs")
            nonterminal_roots[run.session_id] = run
        runs[run.run_id] = run
        runs_by_session[run.session_id].append(run)

    active_by_session: dict[str, str] = {}
    for record in lease_records:
        lease = _strict_mapping(record.value, _LEASE_FIELDS, "active root Run lease")
        if record.entity_id != lease["sessionId"]:
            raise SessionProjectionCorrupt("active root Run lease key/session mismatch")
        if not all(isinstance(lease[key], str) for key in ("workspaceId", "sessionId", "runId", "acquiredAt")):
            raise SessionProjectionCorrupt("active root Run lease fields are corrupt")
        if lease["schemaVersion"] != 1:
            raise SessionProjectionCorrupt("active root Run lease schema is unsupported")
        session_pair = sessions.get(str(lease["sessionId"]))
        run = runs.get(str(lease["runId"]))
        if session_pair is None or run is None:
            raise SessionProjectionCorrupt("active root Run lease references missing authority")
        if (
            run.kind is not RunKind.ROOT
            or run.status.is_terminal
            or run.session_id != lease["sessionId"]
            or run.workspace_id != lease["workspaceId"]
            or session_pair[1].workspace_id != lease["workspaceId"]
        ):
            raise SessionProjectionCorrupt("active root Run lease binding is corrupt")
        _parse_datetime(lease["acquiredAt"], "active lease acquiredAt")
        active_by_session[run.session_id] = run.run_id
    if set(nonterminal_roots) != set(active_by_session):
        raise SessionProjectionCorrupt("nonterminal root Run and active Session lease sets differ")
    if any(nonterminal_roots[key].run_id != value for key, value in active_by_session.items()):
        raise SessionProjectionCorrupt("active Session lease owns the wrong root Run")

    projected: dict[str, _ProjectedSession] = {}
    for session_id, (record, session) in sessions.items():
        lifecycle_record, lifecycle = lifecycles[session_id]
        if lifecycle.workspace_id != session.workspace_id:
            raise SessionProjectionCorrupt("Session lifecycle workspace differs from Session")
        session_event_sequence = await uow.events.latest_sequence(session_id)
        if lifecycle.event_sequence != session_event_sequence:
            raise SessionProjectionCorrupt("Session lifecycle event cursor differs from Event Store")
        await _validate_session_event_stream(uow, session, lifecycle)
        _validate_lifecycle_against_session(lifecycle, session)
        session_turns = tuple(sorted(turns_by_session[session_id], key=lambda item: (item.ordinal, item.turn_id)))
        session_runs = tuple(sorted(runs_by_session[session_id], key=lambda item: item.run_id))
        projected[session_id] = _ProjectedSession(
            record,
            lifecycle_record,
            session,
            lifecycle,
            _make_summary(
                session,
                turn_count=len(session_turns),
                active_run_id=active_by_session.get(session_id),
            ),
            session_turns,
            session_runs,
        )
    return _Projection(projected)


async def _validate_session_event_stream(uow: UnitOfWork, session: Session, lifecycle: _Lifecycle) -> None:
    events = await uow.events.read(session.session_id)
    if not events or len(events) != lifecycle.event_sequence:
        raise SessionProjectionCorrupt("Session lifecycle lacks its complete Event Store fact stream")
    previous_revision = 0
    for expected_sequence, event in enumerate(events, start=1):
        if event.sequence != expected_sequence or event.event_type != EventType.SESSION_UPDATED.value or event.terminal:
            raise SessionProjectionCorrupt("Session event stream sequence/type/terminal flag is corrupt")
        parsed = parse_persisted_domain_event(event.payload)
        if (
            parsed.type is not EventType.SESSION_UPDATED
            or parsed.workspace_id != session.workspace_id
            or parsed.session_id != session.session_id
            or parsed.turn_id is not None
            or parsed.run_id is not None
            or parsed.root_run_id is not None
            or parsed.parent_run_id is not None
            or not isinstance(parsed.payload, SessionUpdatedPayload)
        ):
            raise SessionProjectionCorrupt("Session event authority/lineage is corrupt")
        if not previous_revision < parsed.state_revision <= session.revision:
            raise SessionProjectionCorrupt("Session event state revisions are not monotonic")
        previous_revision = parsed.state_revision
        summary = parsed.payload.session
        if summary.session_id != session.session_id or summary.workspace_id != session.workspace_id:
            raise SessionProjectionCorrupt("Session event payload identity is corrupt")
        if _parse_datetime(summary.updated_at, "Session event updatedAt") != event.occurred_at:
            raise SessionProjectionCorrupt("Session event row/payload timestamps differ")
        if (expected_sequence == 1) != ("created" in parsed.payload.changed_fields):
            raise SessionProjectionCorrupt("Session created fact must appear exactly once at sequence one")

    first_payload = parse_persisted_domain_event(events[0].payload).payload
    assert isinstance(first_payload, SessionUpdatedPayload)
    first_reference = first_payload.fork_reference
    if lifecycle.fork_reference is None:
        if first_reference is not None:
            raise SessionProjectionCorrupt("ordinary Session create event carries a fork reference")
    elif (
        first_reference is None
        or first_reference.to_wire() != _encode_fork_reference(lifecycle.fork_reference)
        or "forkReference" not in first_payload.changed_fields
    ):
        raise SessionProjectionCorrupt("fork Session event/lifecycle references differ")
    last_payload = parse_persisted_domain_event(events[-1].payload).payload
    assert isinstance(last_payload, SessionUpdatedPayload)
    if session.status is SessionStatus.DELETED:
        if not last_payload.session.deleted or "deleted" not in last_payload.changed_fields:
            raise SessionProjectionCorrupt("deleted Session lacks its final deleted event fact")
    elif last_payload.session.deleted:
        raise SessionProjectionCorrupt("active/archived Session event claims it is deleted")


async def _validate_fork_boundary(uow: UnitOfWork, source: _ProjectedSession, command: SessionForkCommand) -> Run:
    _require_mutable(source)
    _require_revision(source.session, command.expected_revision)
    await _require_no_operation(uow.entities, source.session.session_id)
    if source.summary.active_run_id is not None:
        raise SessionForkRejected("cannot fork a Session while it has an active root Run")
    turn = next((item for item in source.turns if item.turn_id == command.fork_turn_id), None)
    if turn is None:
        raise SessionForkRejected("fork Turn does not belong to the source Session")
    if turn.status not in {
        TurnStatus.COMPLETED,
        TurnStatus.CANCELLED,
        TurnStatus.FAILED,
        TurnStatus.INTERRUPTED,
    }:
        raise SessionForkRejected("fork boundary must be an explicitly terminal Turn")
    root_runs = tuple(item for item in source.runs if item.turn_id == turn.turn_id and item.kind is RunKind.ROOT)
    if command.fork_run_id is not None:
        candidates = tuple(item for item in root_runs if item.run_id == command.fork_run_id)
    else:
        candidates = root_runs
    if len(candidates) != 1:
        raise SessionForkRejected("fork boundary Run is missing or ambiguous; provide fork_run_id")
    run = candidates[0]
    if not run.status.is_terminal or run.event_sequence < 1:
        raise SessionForkRejected("fork boundary Run is not terminal")
    terminal = await uow.events.terminal_event(run.run_id)
    latest = await uow.events.latest_sequence(run.run_id)
    if terminal is None or terminal.sequence != run.event_sequence or latest != run.event_sequence:
        raise SessionForkRejected("fork boundary terminal Event Store cursor is missing or corrupt")
    parsed = parse_persisted_domain_event(terminal.payload)
    if parsed.session_id != source.session.session_id or parsed.turn_id != turn.turn_id or parsed.run_id != run.run_id:
        raise SessionForkRejected("fork boundary terminal event lineage is corrupt")
    return run


def _select_session(projection: _Projection, workspace_id: str, session_id: str) -> _ProjectedSession:
    projected = projection.sessions.get(session_id)
    if projected is None or projected.session.workspace_id != workspace_id:
        raise SessionNotFound(f"session {session_id!r} does not exist in workspace {workspace_id!r}")
    return projected


def _require_mutable(projected: _ProjectedSession) -> None:
    if projected.session.status is SessionStatus.DELETED:
        raise SessionDeleted(f"session {projected.session.session_id!r} is deleted")


def _require_revision(session: Session, expected_revision: int) -> None:
    if session.revision != expected_revision:
        raise SessionRevisionConflict(
            f"session {session.session_id!r} expected revision {expected_revision}, actual {session.revision}"
        )


async def _require_no_operation(entities: EntityStore, session_id: str) -> None:
    if await entities.get(SESSION_OPERATION_COLLECTION, session_id) is not None:
        raise SessionOperationConflict(f"session {session_id!r} has a lifecycle operation in progress")


def _active_run_is_effectful(state: RunState) -> bool:
    # Pre-dispatch work (planning, policy checks, or an approval wait) is safe
    # to cancel: no executor owns an unconfirmed side effect yet.  Once the
    # Run enters an execution/recording/persistence window, interruption is
    # fail-closed because ToolCall metadata alone cannot prove an executor is
    # read-only or idempotent.
    if state.phase in {
        RunPhase.EXECUTING_TOOLS,
        RunPhase.RECORDING_RESULTS,
        RunPhase.PERSISTING,
    }:
        return True
    if state.pending.client_invocation_ids:
        return True
    return False


def _make_summary(session: Session, *, turn_count: int, active_run_id: str | None) -> SessionSummary:
    return validate_wire(
        SessionSummary,
        {
            "sessionId": session.session_id,
            "workspaceId": session.workspace_id,
            "title": session.title,
            "createdAt": _rfc3339(session.created_at),
            "updatedAt": _rfc3339(session.updated_at),
            "activeRunId": active_run_id,
            "turnCount": turn_count,
            "deleted": session.status is SessionStatus.DELETED,
        },
    )


def _session_updated_event(
    *,
    operation: str,
    request_hash: str,
    workspace_id: str,
    session: SessionSummary,
    changed_fields: tuple[str, ...],
    state_revision: int,
    occurred_at: datetime,
    fork_reference: SessionForkReference | None = None,
) -> NewEvent:
    payload = validate_wire(
        SessionUpdatedPayload,
        {
            "session": session.to_wire(),
            "changedFields": list(changed_fields),
            "forkReference": (
                None
                if fork_reference is None
                else {
                    "sourceWorkspaceId": fork_reference.source_workspace_id,
                    "sourceSessionId": fork_reference.source_session_id,
                    "sourceTurnId": fork_reference.source_turn_id,
                    "sourceRunId": fork_reference.source_run_id,
                    "sourceEventSequence": fork_reference.source_event_sequence,
                    "artifactLinkIds": list(fork_reference.artifact_link_ids),
                }
            ),
        },
    )
    trace_id = _session_event_trace_id(session.session_id, operation, request_hash)
    record = make_domain_event_record(
        event_type=EventType.SESSION_UPDATED,
        payload=payload,
        trace_id=trace_id,
        workspace_id=workspace_id,
        session_id=session.session_id,
        turn_id=None,
        run_id=None,
        root_run_id=None,
        parent_run_id=None,
        state_revision=state_revision,
    )
    event_id = _session_event_id(session.session_id, operation, request_hash)
    return NewEvent(
        event_id=event_id,
        event_type=EventType.SESSION_UPDATED.value,
        payload=record.to_wire(),
        occurred_at=occurred_at,
        terminal=False,
        idempotency_key=f"session:{session.session_id}:{operation}:{request_hash}",
    )


async def _scan_entities(entities: EntityStore, collection: str) -> tuple[EntityRecord, ...]:
    records: list[EntityRecord] = []
    after_id: str | None = None
    seen: set[str] = set()
    while True:
        page = await entities.list(collection, after_id=after_id, limit=500)
        ids = tuple(record.entity_id for record in page)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise SessionProjectionCorrupt(f"{collection} pagination is not stable and unique")
        if after_id is not None and ids and ids[0] <= after_id:
            raise SessionProjectionCorrupt(f"{collection} pagination did not advance")
        if any(entity_id in seen for entity_id in ids):
            raise SessionProjectionCorrupt(f"{collection} pagination repeated an entity")
        records.extend(page)
        seen.update(ids)
        if len(records) > _MAX_SCAN_RECORDS:
            raise SessionProjectionCorrupt(f"{collection} exceeds the local projection safety limit")
        if len(page) < 500:
            return tuple(records)
        after_id = page[-1].entity_id


async def _find_entity_record(store: EntityStore, collection: str, entity_id: str) -> EntityRecord | None:
    after_id: str | None = None
    while True:
        page = await store.list(collection, after_id=after_id, limit=100)
        if not page:
            return None
        for record in page:
            if record.entity_id == entity_id:
                return record
            if record.entity_id > entity_id:
                return None
        after_id = page[-1].entity_id


def _checked_approval(record: EntityRecord) -> ApprovalRecord:
    approval = record.value
    if not isinstance(approval, ApprovalRecord):
        raise SessionProjectionCorrupt("approval relation is corrupt")
    if approval.request.approval_id != record.entity_id or approval.revision != record.revision:
        raise SessionProjectionCorrupt("approval key/domain/entity revisions differ")
    return approval


def _encode_lifecycle(value: _Lifecycle) -> dict[str, Any]:
    return {
        "schemaVersion": _LIFECYCLE_SCHEMA_VERSION,
        "sessionId": value.session_id,
        "workspaceId": value.workspace_id,
        "eventSequence": value.event_sequence,
        "deletedAt": None if value.deleted_at is None else _rfc3339(value.deleted_at),
        "purgeAfter": None if value.purge_after is None else _rfc3339(value.purge_after),
        "forkReference": None if value.fork_reference is None else _encode_fork_reference(value.fork_reference),
    }


def _decode_lifecycle(value: object) -> _Lifecycle:
    raw = _strict_mapping(value, _LIFECYCLE_FIELDS, "Session lifecycle")
    if raw["schemaVersion"] != _LIFECYCLE_SCHEMA_VERSION:
        raise SessionProjectionCorrupt("Session lifecycle schema is unsupported")
    session_id = _required_string(raw["sessionId"], "lifecycle sessionId")
    workspace_id = _required_string(raw["workspaceId"], "lifecycle workspaceId")
    event_sequence = raw["eventSequence"]
    if type(event_sequence) is not int or event_sequence < 0:
        raise SessionProjectionCorrupt("Session lifecycle eventSequence is invalid")
    deleted_at = _optional_datetime(raw["deletedAt"], "lifecycle deletedAt")
    purge_after = _optional_datetime(raw["purgeAfter"], "lifecycle purgeAfter")
    fork_raw = raw["forkReference"]
    fork_reference = None if fork_raw is None else _decode_fork_reference(fork_raw)
    return _Lifecycle(session_id, workspace_id, event_sequence, deleted_at, purge_after, fork_reference)


def _validate_lifecycle_against_session(lifecycle: _Lifecycle, session: Session) -> None:
    deleted = session.status is SessionStatus.DELETED
    if deleted != (lifecycle.deleted_at is not None and lifecycle.purge_after is not None):
        raise SessionProjectionCorrupt("Session status and retention metadata differ")
    if lifecycle.deleted_at is not None:
        assert lifecycle.purge_after is not None
        if lifecycle.deleted_at != session.updated_at or lifecycle.purge_after <= lifecycle.deleted_at:
            raise SessionProjectionCorrupt("Session retention timestamps are corrupt")
    reference = lifecycle.fork_reference
    fork_ids = (session.forked_from_session_id, session.forked_from_turn_id)
    if reference is None:
        if fork_ids != (None, None):
            raise SessionProjectionCorrupt("forked Session lacks its immutable fork reference")
    elif fork_ids != (reference.source_session_id, reference.source_turn_id):
        raise SessionProjectionCorrupt("Session fork fields differ from immutable fork reference")


def _encode_fork_reference(value: SessionForkReference) -> dict[str, Any]:
    return {
        "sourceWorkspaceId": value.source_workspace_id,
        "sourceSessionId": value.source_session_id,
        "sourceTurnId": value.source_turn_id,
        "sourceRunId": value.source_run_id,
        "sourceEventSequence": value.source_event_sequence,
        "artifactLinkIds": list(value.artifact_link_ids),
    }


def _decode_fork_reference(value: object) -> SessionForkReference:
    raw = _strict_mapping(value, _FORK_REFERENCE_FIELDS, "fork reference")
    links = raw["artifactLinkIds"]
    if not isinstance(links, list) or any(not isinstance(item, str) for item in links):
        raise SessionProjectionCorrupt("fork Artifact link IDs are corrupt")
    try:
        return SessionForkReference(
            _required_string(raw["sourceWorkspaceId"], "fork sourceWorkspaceId"),
            _required_string(raw["sourceSessionId"], "fork sourceSessionId"),
            _required_string(raw["sourceTurnId"], "fork sourceTurnId"),
            _required_string(raw["sourceRunId"], "fork sourceRunId"),
            _required_integer(raw["sourceEventSequence"], "fork sourceEventSequence"),
            tuple(links),
        )
    except (TypeError, ValueError) as error:
        raise SessionProjectionCorrupt("fork reference no longer passes the strict schema") from error


def _encode_create_operation(value: SessionCreatePreparation) -> dict[str, Any]:
    hook_outcome: dict[str, Any] | None = None
    if value.state is not SessionCreateState.PENDING:
        assert value.hook_decision is not None and value.hook_reason_code is not None
        hook_outcome = {
            "event": "SessionStart",
            "decision": value.hook_decision,
            "reasonCode": value.hook_reason_code,
        }
    return {
        "schemaVersion": 2,
        "requestHash": value.request_hash,
        "kind": "session",
        "state": value.state.value,
        "request": {
            "sessionId": value.session_id,
            "workspaceId": value.command.workspace_id,
            "profileId": value.command.profile_id,
            "title": value.command.title,
            "connectionId": value.connection_id,
            "createdAt": _rfc3339(value.created_at),
        },
        "hookOutcome": hook_outcome,
    }


def _decode_create_operation(value: object, request_hash: str) -> _DecodedCreateOperation:
    if not isinstance(value, Mapping):
        raise SessionProjectionCorrupt("create receipt must be a JSON object")
    schema_version = value.get("schemaVersion")
    if schema_version == 1:
        raw = _strict_mapping(value, {"schemaVersion", "requestHash", "kind", "receipt"}, "create receipt")
        if raw["kind"] != "session":
            raise SessionIdempotencyConflict("create receipt schema/kind is incompatible")
        if raw["requestHash"] != request_hash:
            raise SessionIdempotencyConflict("create idempotency key is bound to another request")
        receipt = _strict_mapping(raw["receipt"], {"sessionId", "workspaceId", "created"}, "create result")
        if type(receipt["created"]) is not bool:
            raise SessionProjectionCorrupt("create receipt created flag is corrupt")
        return _DecodedCreateOperation(
            state=SessionCreateState.ACTIVE,
            session_id=_required_string(receipt["sessionId"], "create sessionId"),
            workspace_id=_required_string(receipt["workspaceId"], "create workspaceId"),
            profile_id=None,
            title=None,
            connection_id=None,
            created_at=None,
            hook_decision="continue",
            hook_reason_code="legacy_create_receipt",
        )

    raw = _strict_mapping(
        value,
        {"schemaVersion", "requestHash", "kind", "state", "request", "hookOutcome"},
        "create operation",
    )
    if raw["schemaVersion"] != 2 or raw["kind"] != "session":
        raise SessionIdempotencyConflict("create receipt schema/kind is incompatible")
    if raw["requestHash"] != request_hash:
        raise SessionIdempotencyConflict("create idempotency key is bound to another request")
    try:
        state = SessionCreateState(raw["state"])
    except (TypeError, ValueError) as error:
        raise SessionProjectionCorrupt("create operation state is corrupt") from error
    request = _strict_mapping(
        raw["request"],
        {"sessionId", "workspaceId", "profileId", "title", "connectionId", "createdAt"},
        "create operation request",
    )
    session_id = _required_string(request["sessionId"], "create sessionId")
    workspace_id = _required_string(request["workspaceId"], "create workspaceId")
    profile_id = _required_string(request["profileId"], "create profileId")
    title = _required_string(request["title"], "create title")
    connection_id = _required_string(request["connectionId"], "create connectionId")
    try:
        _validate_connection_id(connection_id)
    except ValueError as error:
        raise SessionProjectionCorrupt("create connectionId is corrupt") from error
    created_at = _parse_datetime(request["createdAt"], "create createdAt")

    hook_decision: str | None = None
    hook_reason_code: str | None = None
    if state is SessionCreateState.PENDING:
        if raw["hookOutcome"] is not None:
            raise SessionProjectionCorrupt("pending create operation already has a Hook outcome")
    else:
        outcome = _strict_mapping(
            raw["hookOutcome"],
            {"event", "decision", "reasonCode"},
            "create Hook outcome",
        )
        if outcome["event"] != "SessionStart":
            raise SessionProjectionCorrupt("create Hook outcome event is corrupt")
        hook_decision = _required_string(outcome["decision"], "create Hook decision")
        hook_reason_code = _required_string(outcome["reasonCode"], "create Hook reasonCode")
        try:
            _validate_hook_reason_code(hook_reason_code)
        except ValueError as error:
            raise SessionProjectionCorrupt("create Hook reasonCode is corrupt") from error
        if state is SessionCreateState.ACTIVE and hook_decision != "continue":
            raise SessionProjectionCorrupt("active create operation lacks a continue Hook outcome")
        if state is SessionCreateState.ABORTED and hook_decision not in {"ask", "deny"}:
            raise SessionProjectionCorrupt("aborted create operation lacks an ask/deny Hook outcome")
    return _DecodedCreateOperation(
        state=state,
        session_id=session_id,
        workspace_id=workspace_id,
        profile_id=profile_id,
        title=title,
        connection_id=connection_id,
        created_at=created_at,
        hook_decision=hook_decision,
        hook_reason_code=hook_reason_code,
    )


def _require_same_create_preparation(
    expected: SessionCreatePreparation,
    actual: SessionCreatePreparation,
) -> None:
    if (
        expected.receipt_id != actual.receipt_id
        or expected.request_hash != actual.request_hash
        or expected.session_id != actual.session_id
        or expected.connection_id != actual.connection_id
        or expected.created_at != actual.created_at
        or expected.command != actual.command
    ):
        raise SessionIdempotencyConflict("Session create preparation identity changed before completion")


def _encode_rename_receipt(request_hash: str, result: SessionRenameResult) -> dict[str, Any]:
    return _mutation_receipt(
        "rename",
        request_hash,
        result.session.workspace_id,
        result.session.session_id,
        {
            "session": result.session.to_wire(),
            "revision": result.revision,
            "updated": result.updated,
        },
    )


def _decode_rename_receipt(value: object, request_hash: str) -> SessionRenameResult:
    raw = _decode_mutation_receipt(value, "rename", request_hash)
    result = _strict_mapping(raw["result"], {"session", "revision", "updated"}, "rename result")
    revision = _required_integer(result["revision"], "rename revision")
    if type(result["updated"]) is not bool:
        raise SessionProjectionCorrupt("rename updated flag is corrupt")
    summary = _decode_summary(result["session"])
    if summary.session_id != raw["sessionId"] or summary.workspace_id != raw["workspaceId"]:
        raise SessionProjectionCorrupt("rename receipt identity is corrupt")
    return SessionRenameResult(summary, revision, bool(result["updated"]))


def _encode_delete_receipt(request_hash: str, result: SessionDeleteResult) -> dict[str, Any]:
    return _mutation_receipt(
        "delete",
        request_hash,
        result.workspace_id,
        result.session_id,
        {
            "revision": result.revision,
            "deleted": result.deleted,
            "deletedAt": _rfc3339(result.deleted_at),
            "purgeAfter": _rfc3339(result.purge_after),
            "activeRunsCancelRequested": list(result.active_runs_cancel_requested),
        },
    )


def _decode_delete_receipt(value: object, request_hash: str) -> SessionDeleteResult:
    raw = _decode_mutation_receipt(value, "delete", request_hash)
    result = _strict_mapping(
        raw["result"],
        {"revision", "deleted", "deletedAt", "purgeAfter", "activeRunsCancelRequested"},
        "delete result",
    )
    active = result["activeRunsCancelRequested"]
    if not isinstance(active, list) or any(not isinstance(item, str) for item in active):
        raise SessionProjectionCorrupt("delete cancelled Run IDs are corrupt")
    if type(result["deleted"]) is not bool:
        raise SessionProjectionCorrupt("delete result flag is corrupt")
    return SessionDeleteResult(
        _required_string(raw["sessionId"], "delete sessionId"),
        _required_string(raw["workspaceId"], "delete workspaceId"),
        _required_integer(result["revision"], "delete revision"),
        bool(result["deleted"]),
        _parse_datetime(result["deletedAt"], "delete deletedAt"),
        _parse_datetime(result["purgeAfter"], "delete purgeAfter"),
        tuple(active),
    )


def _encode_fork_receipt(request_hash: str, result: SessionForkResult) -> dict[str, Any]:
    return _mutation_receipt(
        "fork",
        request_hash,
        result.session.workspace_id,
        result.session.session_id,
        {
            "session": result.session.to_wire(),
            "revision": result.revision,
            "sourceSessionId": result.source_session_id,
            "forkTurnId": result.fork_turn_id,
            "forkReference": _encode_fork_reference(result.fork_reference),
            "created": result.created,
        },
    )


def _decode_fork_receipt(value: object, request_hash: str) -> SessionForkResult:
    raw = _decode_mutation_receipt(value, "fork", request_hash)
    result = _strict_mapping(
        raw["result"],
        {"session", "revision", "sourceSessionId", "forkTurnId", "forkReference", "created"},
        "fork result",
    )
    if type(result["created"]) is not bool:
        raise SessionProjectionCorrupt("fork created flag is corrupt")
    summary = _decode_summary(result["session"])
    if summary.session_id != raw["sessionId"] or summary.workspace_id != raw["workspaceId"]:
        raise SessionProjectionCorrupt("fork receipt identity is corrupt")
    reference = _decode_fork_reference(result["forkReference"])
    return SessionForkResult(
        summary,
        _required_integer(result["revision"], "fork revision"),
        _required_string(result["sourceSessionId"], "fork sourceSessionId"),
        _required_string(result["forkTurnId"], "fork turnId"),
        reference,
        bool(result["created"]),
    )


def _mutation_receipt(
    operation: str,
    request_hash: str,
    workspace_id: str,
    session_id: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schemaVersion": _RECEIPT_SCHEMA_VERSION,
        "operation": operation,
        "requestHash": request_hash,
        "workspaceId": workspace_id,
        "sessionId": session_id,
        "result": dict(result),
    }


def _decode_mutation_receipt(value: object, operation: str, request_hash: str) -> Mapping[str, Any]:
    raw = _strict_mapping(value, _MUTATION_RECEIPT_FIELDS, f"{operation} receipt")
    if raw["schemaVersion"] != _RECEIPT_SCHEMA_VERSION or raw["operation"] != operation:
        raise SessionIdempotencyConflict("Session mutation idempotency key is bound to another operation")
    if raw["requestHash"] != request_hash:
        raise SessionIdempotencyConflict("Session mutation idempotency key is bound to another request")
    _required_string(raw["workspaceId"], "mutation workspaceId")
    _required_string(raw["sessionId"], "mutation sessionId")
    return raw


def _decode_summary(value: object) -> SessionSummary:
    try:
        return validate_wire(SessionSummary, thaw_json(value))
    except (TypeError, ValueError) as error:
        raise SessionProjectionCorrupt("stored SessionSummary is corrupt") from error


def _session_event_trace_id(session_id: str, operation: str, request_hash: str) -> str:
    seed = f"session:{session_id}:{operation}:{request_hash}"
    return f"trace_{hashlib.sha256(seed.encode()).hexdigest()}"


def _session_event_id(session_id: str, operation: str, request_hash: str) -> str:
    seed = f"session-event:{session_id}:{operation}:{request_hash}"
    return f"evt_{hashlib.sha256(seed.encode()).hexdigest()}"


def _legacy_session_event_id(operation: str, request_hash: str) -> str:
    seed = f"session-event:{operation}:{request_hash}"
    return f"evt_{hashlib.sha256(seed.encode()).hexdigest()}"


def _find_mutation_event(events: Sequence[StoredEvent], operation: str, request_hash: str) -> StoredEvent:
    # Releases before the Session identity was included in the audit-event seed
    # can still be replayed after an upgrade.  New events bind their globally
    # unique ID to the EventStore stream/Session so two equal-titled creates do
    # not collide, while the legacy ID remains read-only recovery input.
    legacy_id = _legacy_session_event_id(operation, request_hash)
    matching = tuple(
        event
        for event in events
        if event.event_id in {legacy_id, _session_event_id(event.stream_id, operation, request_hash)}
    )
    if len(matching) != 1:
        raise SessionProjectionCorrupt("mutation receipt lacks its unique audit event")
    event = matching[0]
    parsed = parse_persisted_domain_event(event.payload)
    if event.event_type != EventType.SESSION_UPDATED.value or parsed.type is not EventType.SESSION_UPDATED:
        raise SessionProjectionCorrupt("mutation audit event has the wrong type")
    return event


def _validate_event_summary(
    event: StoredEvent,
    *,
    session_id: str,
    workspace_id: str,
    revision: int,
    expected_summary: SessionSummary | None = None,
    expected_fork_reference: SessionForkReference | None = None,
) -> None:
    parsed = parse_persisted_domain_event(event.payload)
    if (
        parsed.session_id != session_id
        or parsed.workspace_id != workspace_id
        or parsed.state_revision != revision
        or not isinstance(parsed.payload, SessionUpdatedPayload)
    ):
        raise SessionProjectionCorrupt("Session audit event identity/revision is corrupt")
    summary = parsed.payload.session
    if summary.session_id != session_id or summary.workspace_id != workspace_id:
        raise SessionProjectionCorrupt("Session audit payload identity is corrupt")
    if expected_summary is not None and summary != expected_summary:
        raise SessionProjectionCorrupt("Session audit payload differs from its idempotency receipt")
    actual_reference = parsed.payload.fork_reference
    if expected_fork_reference is None:
        if actual_reference is not None:
            raise SessionProjectionCorrupt("non-fork Session audit unexpectedly carries a fork reference")
    elif actual_reference is None or actual_reference.to_wire() != _encode_fork_reference(expected_fork_reference):
        raise SessionProjectionCorrupt("fork audit event differs from its immutable lifecycle reference")


def _mutation_receipt_id(workspace_id: str, idempotency_key: str) -> str:
    return f"{workspace_id}:{idempotency_key}"


def _encode_cursor(workspace_id: str, include_deleted: bool, after_id: str) -> str:
    payload = json.dumps(
        {
            "schemaVersion": _PAGE_SCHEMA_VERSION,
            "workspaceId": workspace_id,
            "includeDeleted": include_deleted,
            "afterSessionId": after_id,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None, workspace_id: str, include_deleted: bool) -> str | None:
    if cursor is None:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")), parse_constant=_reject_json_constant)
        raw = _strict_mapping(
            value,
            {"schemaVersion", "workspaceId", "includeDeleted", "afterSessionId"},
            "Session page cursor",
        )
        if (
            raw["schemaVersion"] != _PAGE_SCHEMA_VERSION
            or raw["workspaceId"] != workspace_id
            or raw["includeDeleted"] is not include_deleted
        ):
            raise ValueError("cursor scope differs from the list command")
        after_id = _required_string(raw["afterSessionId"], "cursor afterSessionId")
        _SESSION_ID.validate_python(after_id, strict=True)
        return after_id
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Session page cursor is malformed or belongs to another query") from error


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _default_fork_title(source: str) -> str:
    suffix = " (fork)"
    return f"{source[: 512 - len(suffix)]}{suffix}"


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SessionProjectionCorrupt(f"{label} must be a non-empty string")
    return value


def _required_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise SessionProjectionCorrupt(f"{label} must be a positive integer")
    return value


def _strict_mapping(value: object, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SessionProjectionCorrupt(f"{label} must be a JSON object")
    if set(value) != fields:
        raise SessionProjectionCorrupt(f"{label} fields are incompatible")
    return value


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SessionProjectionCorrupt("Session timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise SessionProjectionCorrupt(f"{label} must be an RFC 3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SessionProjectionCorrupt(f"{label} is not a valid RFC 3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SessionProjectionCorrupt(f"{label} must include an offset")
    return parsed


def _optional_datetime(value: object, label: str) -> datetime | None:
    return None if value is None else _parse_datetime(value, label)


def _require_same_delete_gate(value: object, request_hash: str, command: SessionDeleteCommand) -> None:
    raw = _strict_mapping(value, _DELETE_GATE_FIELDS, "session delete gate")
    if (
        raw["schemaVersion"] != 1
        or raw["operation"] != "delete"
        or raw["workspaceId"] != command.workspace_id
        or raw["sessionId"] != command.session_id
        or raw["requestHash"] != request_hash
        or raw["idempotencyKey"] != command.idempotency_key
        or raw["expectedRevision"] != command.expected_revision
    ):
        raise SessionOperationConflict("Session is locked by another lifecycle operation")
    _parse_datetime(raw["acquiredAt"], "delete gate acquiredAt")


_LIFECYCLE_FIELDS = {
    "schemaVersion",
    "sessionId",
    "workspaceId",
    "eventSequence",
    "deletedAt",
    "purgeAfter",
    "forkReference",
}
_FORK_REFERENCE_FIELDS = {
    "sourceWorkspaceId",
    "sourceSessionId",
    "sourceTurnId",
    "sourceRunId",
    "sourceEventSequence",
    "artifactLinkIds",
}
_MUTATION_RECEIPT_FIELDS = {
    "schemaVersion",
    "operation",
    "requestHash",
    "workspaceId",
    "sessionId",
    "result",
}
_DELETE_GATE_FIELDS = {
    "schemaVersion",
    "operation",
    "workspaceId",
    "sessionId",
    "requestHash",
    "idempotencyKey",
    "expectedRevision",
    "acquiredAt",
}
_LEASE_FIELDS = {"schemaVersion", "workspaceId", "sessionId", "runId", "acquiredAt"}


__all__ = [
    "SESSION_LIFECYCLE_COLLECTION",
    "SESSION_MUTATION_COLLECTION",
    "SESSION_OPERATION_COLLECTION",
    "ArtifactLinkResolver",
    "SessionActiveEffectfulRun",
    "SessionActiveRunUnavailable",
    "SessionCreateCommand",
    "SessionCreatePreparation",
    "SessionCreateResult",
    "SessionCreateState",
    "SessionDeleteCommand",
    "SessionDeleteResult",
    "SessionDeleted",
    "SessionForkCommand",
    "SessionForkReference",
    "SessionForkRejected",
    "SessionForkResult",
    "SessionGetCommand",
    "SessionGetResult",
    "SessionIdempotencyConflict",
    "SessionLifecycleDiagnostics",
    "SessionLifecycleError",
    "SessionLifecycleService",
    "SessionListCommand",
    "SessionListResult",
    "SessionNotFound",
    "SessionOperationConflict",
    "SessionProjectionCorrupt",
    "SessionRenameCommand",
    "SessionRenameResult",
    "SessionRevisionConflict",
]
