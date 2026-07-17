from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWork, SqliteUnitOfWorkFactory
from offeragent_harness.agent import RunBudget
from offeragent_harness.agent.loop import ToolExecution, ToolKernel
from offeragent_harness.agent.planner import Planner, PlanningAttempt, PlanningAttemptOutcome, PlanningStep
from offeragent_harness.agent.state import PendingWork, RunPhase, RunState
from offeragent_harness.config import HarnessConfig
from offeragent_harness.hooks import HookDecision, HookEvent
from offeragent_harness.models import ModelUsage
from offeragent_harness.permissions import (
    ApprovalBinding,
    ApprovalGrantState,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalScope,
    ApprovalState,
    RiskClass,
    approval_id_for,
)
from offeragent_harness.ports import (
    ApplicationCommandContext,
    CancellationToken,
    EntityStore,
    EventStore,
    InvocationJournal,
    NewEvent,
    ToolLifecycleObserver,
    UnitOfWork,
)
from offeragent_harness.protocol.events import (
    EventType,
    SessionUpdatedPayload,
    make_domain_event_record,
    parse_persisted_domain_event,
)
from offeragent_harness.protocol.messages import SessionCreateParams
from offeragent_harness.protocol.messages import SessionCreateResult as ProtocolSessionCreateResult
from offeragent_harness.runtime.application_dispatcher import ApplicationCommandHandler
from offeragent_harness.runtime.application_domain_handlers import DomainCommandIdentity, _session_handlers
from offeragent_harness.runtime.approval_manager import ApprovalManager, ApprovalRecord
from offeragent_harness.runtime.cancellation import CancellationCode, CancellationReason
from offeragent_harness.runtime.harness_service import (
    CreateSessionCommand,
    HarnessService,
    RunComponents,
    SessionRunConflict,
    StartTurnCommand,
)
from offeragent_harness.runtime.hook_lifecycle import LifecycleHookDenied
from offeragent_harness.runtime.session_service import (
    SESSION_CREATE_IDEMPOTENCY_COLLECTION,
    SESSION_OPERATION_COLLECTION,
    ArtifactLinkResolver,
    SessionActiveEffectfulRun,
    SessionCreateCommand,
    SessionCreateState,
    SessionDeleteCommand,
    SessionDeleted,
    SessionForkCommand,
    SessionForkRejected,
    SessionGetCommand,
    SessionLifecycleService,
    SessionListCommand,
    SessionNotFound,
    SessionOperationConflict,
    SessionProjectionCorrupt,
    SessionRenameCommand,
    SessionRevisionConflict,
)
from offeragent_harness.runtime.turn_manager import TurnManager
from offeragent_harness.sessions import (
    AgentLineage,
    Run,
    RunKind,
    RunStatus,
    Session,
    TerminationReason,
    Turn,
    TurnStatus,
)
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    ManualCancellationToken,
    ManualClock,
    RecordingEventSink,
)
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffect,
    SideEffectClass,
    SideEffectKind,
    SideEffectState,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
WORKSPACE_ID = "ws_session_lifecycle"
PROFILE_ID = "profile_session_lifecycle"


class _AckLossUnitOfWork:
    def __init__(self, inner: SqliteUnitOfWork, owner: _AckLossFactory) -> None:
        self._inner = inner
        self._owner = owner

    @property
    def entities(self) -> EntityStore:
        return self._inner.entities

    @property
    def events(self) -> EventStore:
        return self._inner.events

    @property
    def journal(self) -> InvocationJournal:
        return self._inner.journal

    async def __aenter__(self) -> _AckLossUnitOfWork:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self._inner.__aexit__(exc_type, exc, traceback)

    async def commit(self) -> None:
        await self._inner.commit()
        self._owner.commit_calls += 1
        if self._owner.fail_next_commit_ack or self._owner.commit_calls in self._owner.fail_commit_numbers:
            self._owner.fail_next_commit_ack = False
            raise ConnectionError("simulated SQLite commit ACK loss")

    async def rollback(self) -> None:
        await self._inner.rollback()


class _AckLossFactory:
    def __init__(self, inner: SqliteUnitOfWorkFactory) -> None:
        self.inner = inner
        self.fail_next_commit_ack = False
        self.fail_commit_numbers: set[int] = set()
        self.commit_calls = 0

    def begin(self) -> UnitOfWork:
        return _AckLossUnitOfWork(self.inner.begin(), self)


def _service(
    factory: SqliteUnitOfWorkFactory | _AckLossFactory,
    *,
    sink: RecordingEventSink | None = None,
    ids: DeterministicIdGenerator | None = None,
    manager: TurnManager | None = None,
    artifact_link_resolver: ArtifactLinkResolver | None = None,
) -> SessionLifecycleService:
    clock = ManualClock(NOW)
    turn_manager = manager or TurnManager()
    approvals = ApprovalManager(unit_of_work=factory, clock=clock)
    return SessionLifecycleService(
        unit_of_work=factory,
        event_sink=sink or RecordingEventSink(),
        clock=clock,
        ids=ids or DeterministicIdGenerator(),
        turn_manager=turn_manager,
        approval_manager=approvals,
        artifact_link_resolver=artifact_link_resolver,
    )


class _StaticSessionConfig:
    async def snapshot(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(config=HarnessConfig())


class _SessionStartHarness:
    def __init__(self, sessions: SessionLifecycleService, decision: HookDecision) -> None:
        self.sessions = sessions
        self.decision = decision
        self.hook_calls = 0
        self.hook_connection_ids: list[str] = []

    async def session_started(self, **values: object) -> None:
        self.hook_calls += 1
        connection_id = values.get("connection_id")
        assert isinstance(connection_id, str)
        self.hook_connection_ids.append(connection_id)
        if self.decision is not HookDecision.CONTINUE:
            raise LifecycleHookDenied(HookEvent.SESSION_START, self.decision)


def _session_create_handler(harness: _SessionStartHarness) -> ApplicationCommandHandler:
    return _session_handlers(
        identity=DomainCommandIdentity(WORKSPACE_ID, PROFILE_ID, "managed_local", "actor_local"),
        harness=harness,  # type: ignore[arg-type]
        projections=object(),  # type: ignore[arg-type]
        config=_StaticSessionConfig(),  # type: ignore[arg-type]
    )["session/create"]


async def _create(service: SessionLifecycleService, suffix: str) -> str:
    result = await service.create(
        SessionCreateCommand(
            WORKSPACE_ID,
            PROFILE_ID,
            f"Session {suffix}",
            f"create-{suffix}",
        )
    )
    return result.session.session_id


async def _seed_terminal_boundary(
    factory: SqliteUnitOfWorkFactory,
    *,
    session_id: str,
    turn_id: str = "turn_fork_boundary",
    run_id: str = "run_fork_boundary",
) -> None:
    turn = Turn(
        turn_id=turn_id,
        session_id=session_id,
        ordinal=1,
        status=TurnStatus.CANCELLED,
        input_blocks=({"type": "text", "text": "fork here"},),
        created_at=NOW,
        updated_at=NOW,
    )
    run = Run(
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
        workspace_id=WORKSPACE_ID,
        lineage=AgentLineage.root(run_id),
        kind=RunKind.ROOT,
        status=RunStatus.CANCELLED,
        attempt=1,
        event_sequence=1,
        config_snapshot={"model": "fake"},
        created_at=NOW,
        updated_at=NOW,
        deadline_at=None,
        termination_reason=TerminationReason.CANCELLED_BY_USER,
    )
    record = make_domain_event_record(
        event_type=EventType.TURN_CANCELLED,
        payload={
            "reason": "fixture terminal boundary",
            "usage": {"inputTokens": 0, "outputTokens": 0},
            "partialContent": [],
            "code": "fixture",
        },
        trace_id="trace_fork_boundary",
        workspace_id=WORKSPACE_ID,
        session_id=session_id,
        turn_id=turn_id,
        run_id=run_id,
        root_run_id=run_id,
        parent_run_id=None,
        state_revision=1,
    )
    event = NewEvent(
        event_id="evt_fork_boundary",
        event_type=EventType.TURN_CANCELLED.value,
        payload=record.to_wire(),
        occurred_at=NOW,
        terminal=True,
        idempotency_key="fork-boundary-terminal",
    )
    async with factory.begin() as uow:
        await uow.entities.put("turns", turn_id, turn, expected_revision=0)
        await uow.entities.put("runs", run_id, run, expected_revision=0)
        await uow.events.append(run_id, 0, (event,))
        await uow.commit()


async def _seed_dangerous_active_run(
    factory: SqliteUnitOfWorkFactory,
    *,
    session_id: str,
    phase: RunPhase,
    turn_id: str = "turn_effectful",
    run_id: str = "run_effectful",
) -> None:
    async with factory.begin() as uow:
        current = await uow.entities.get("sessions", session_id)
        if not isinstance(current, Session):
            raise AssertionError("fixture Session is missing")
        session = replace(current, updated_at=NOW, revision=current.revision + 1)
        turn = Turn(
            turn_id=turn_id,
            session_id=session_id,
            ordinal=1,
            status=TurnStatus.RUNNING,
            input_blocks=({"type": "text", "text": "execute"},),
            created_at=NOW,
            updated_at=NOW,
        )
        lineage = AgentLineage.root(run_id)
        run = Run(
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
            workspace_id=WORKSPACE_ID,
            lineage=lineage,
            kind=RunKind.ROOT,
            status=RunStatus(phase.value),
            attempt=1,
            event_sequence=1,
            config_snapshot={"model": "fake"},
            created_at=NOW,
            updated_at=NOW,
            deadline_at=None,
        )
        state = RunState(
            workspace_id=WORKSPACE_ID,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            lineage=lineage,
            phase=phase,
            revision=5,
            pending=PendingWork(),
        )
        record = make_domain_event_record(
            event_type=EventType.TURN_STARTED,
            payload={
                "input": [{"type": "text", "text": "execute"}],
                "runConfig": {"model": "fake"},
                "attempt": 1,
            },
            trace_id="trace_effectful",
            workspace_id=WORKSPACE_ID,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            root_run_id=run_id,
            parent_run_id=None,
            state_revision=0,
        )
        event = NewEvent(
            event_id="evt_effectful_started",
            event_type=EventType.TURN_STARTED.value,
            payload=record.to_wire(),
            occurred_at=NOW,
            terminal=False,
            idempotency_key="effectful-started",
        )
        await uow.entities.put("sessions", session_id, session, expected_revision=current.revision)
        await uow.entities.put("turns", turn_id, turn, expected_revision=0)
        await uow.entities.put("runs", run_id, run, expected_revision=0)
        await uow.entities.put("run_states", run_id, state, expected_revision=0)
        await uow.entities.put(
            "active_root_runs",
            session_id,
            {
                "schemaVersion": 1,
                "workspaceId": WORKSPACE_ID,
                "sessionId": session_id,
                "runId": run_id,
                "acquiredAt": NOW.isoformat(),
            },
            expected_revision=0,
        )
        await uow.events.append(run_id, 0, (event,))
        await uow.commit()


@pytest.mark.asyncio
async def test_sqlite_list_get_use_stable_pagination_and_fail_closed_on_orphan_relation(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "session-pages.sqlite")
    service = _service(factory)
    created = [await _create(service, str(index)) for index in range(5)]

    first = await service.list(SessionListCommand(WORKSPACE_ID, limit=2))
    assert [item.session_id for item in first.sessions] == created[:2]
    assert first.next_cursor is not None
    second = await service.list(SessionListCommand(WORKSPACE_ID, cursor=first.next_cursor, limit=2))
    assert [item.session_id for item in second.sessions] == created[2:4]
    assert second.next_cursor is not None
    third = await service.list(SessionListCommand(WORKSPACE_ID, cursor=second.next_cursor, limit=2))
    assert [item.session_id for item in third.sessions] == created[4:]
    assert third.next_cursor is None
    assert len(set(created)) == 5
    assert (await service.get_fork_reference(WORKSPACE_ID, created[0])) is None

    with pytest.raises(SessionNotFound):
        await service.get(SessionGetCommand("ws_other", created[0]))
    with pytest.raises(ValueError, match="another query"):
        await service.list(SessionListCommand("ws_other", cursor=first.next_cursor, limit=2))
    with pytest.raises(ValueError, match="another query"):
        await service.list(SessionListCommand(WORKSPACE_ID, cursor=first.next_cursor, limit=2, include_deleted=True))

    with pytest.raises(ValueError, match="cursor"):
        await service.list(SessionListCommand(WORKSPACE_ID, cursor=first.next_cursor + "broken", limit=2))

    orphan = Turn(
        turn_id="turn_orphan",
        session_id="ses_missing",
        ordinal=1,
        status=TurnStatus.CANCELLED,
        input_blocks=({"type": "text", "text": "orphan"},),
        created_at=NOW,
        updated_at=NOW,
    )
    async with factory.begin() as uow:
        await uow.entities.put("turns", orphan.turn_id, orphan, expected_revision=0)
        await uow.commit()
    with pytest.raises(SessionProjectionCorrupt, match="missing Session"):
        await service.list(SessionListCommand(WORKSPACE_ID))


@pytest.mark.asyncio
async def test_sqlite_rename_cas_race_and_event_delivery_ack_loss_are_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "session-rename.sqlite"
    factory = SqliteUnitOfWorkFactory(database_path)
    sink = RecordingEventSink(acknowledgement_loss_calls=frozenset({2}))
    service = _service(factory, sink=sink)
    session_id = await _create(service, "rename")

    renamed = await service.rename(SessionRenameCommand(WORKSPACE_ID, session_id, "Renamed", 1, "rename-once"))
    assert renamed.revision == 2
    assert renamed.session.title == "Renamed"
    assert len(service.diagnostics.delivery_failures) == 1
    replay = await service.rename(SessionRenameCommand(WORKSPACE_ID, session_id, "Renamed", 1, "rename-once"))
    assert replay == renamed
    assert sink.publish_calls == 2
    events = await factory.event_store.read(session_id)
    assert [event.event_type for event in events] == ["session.updated", "session.updated"]
    payload = parse_persisted_domain_event(events[-1].payload).payload
    assert isinstance(payload, SessionUpdatedPayload)
    assert payload.changed_fields == ["title", "updatedAt"]

    race_session = await _create(service, "race")
    left = _service(SqliteUnitOfWorkFactory(database_path), ids=DeterministicIdGenerator(start=100))
    right = _service(SqliteUnitOfWorkFactory(database_path), ids=DeterministicIdGenerator(start=200))
    outcomes = await asyncio.gather(
        left.rename(SessionRenameCommand(WORKSPACE_ID, race_session, "Left", 1, "rename-left")),
        right.rename(SessionRenameCommand(WORKSPACE_ID, race_session, "Right", 1, "rename-right")),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    assert sum(isinstance(item, SessionRevisionConflict) for item in outcomes) == 1


@pytest.mark.asyncio
async def test_create_commit_ack_loss_reconciles_one_event_and_normal_retry_does_not_republish(tmp_path: Path) -> None:
    inner = SqliteUnitOfWorkFactory(tmp_path / "session-create-ack.sqlite")
    factory = _AckLossFactory(inner)
    sink = RecordingEventSink()
    service = _service(factory, sink=sink)
    factory.fail_next_commit_ack = True

    created = await service.create(SessionCreateCommand(WORKSPACE_ID, PROFILE_ID, "ACK-safe create", "create-ack"))
    assert created.created
    assert len(await inner.event_store.read(created.session.session_id)) == 1
    assert sink.publish_calls == 1

    restarted = _service(inner, sink=sink, ids=DeterministicIdGenerator(start=100))
    replay = await restarted.create(SessionCreateCommand(WORKSPACE_ID, PROFILE_ID, "ACK-safe create", "create-ack"))
    assert replay.session.session_id == created.session.session_id
    assert not replay.created
    assert sink.publish_calls == 1


@pytest.mark.asyncio
async def test_same_title_create_requests_have_session_bound_event_identities_and_replay(tmp_path: Path) -> None:
    database_path = tmp_path / "same-title-create.sqlite"
    factory = SqliteUnitOfWorkFactory(database_path)
    sink = RecordingEventSink()
    service = _service(factory, sink=sink)
    first_command = SessionCreateCommand(WORKSPACE_ID, PROFILE_ID, "新会话", "same-title-first")
    second_command = SessionCreateCommand(WORKSPACE_ID, PROFILE_ID, "新会话", "same-title-second")

    first = await service.create(first_command)
    second = await service.create(second_command)
    replay = await service.create(second_command)

    assert first.created
    assert second.created
    assert not replay.created
    assert replay.session.session_id == second.session.session_id
    assert first.session.session_id != second.session.session_id
    assert len((await service.list(SessionListCommand(WORKSPACE_ID))).sessions) == 2
    first_events = await factory.event_store.read(first.session.session_id)
    second_events = await factory.event_store.read(second.session.session_id)
    assert len(first_events) == len(second_events) == 1
    assert first_events[0].event_id != second_events[0].event_id
    assert first_events[0].idempotency_key != second_events[0].idempotency_key
    assert first.session.session_id in first_events[0].idempotency_key
    assert second.session.session_id in second_events[0].idempotency_key
    assert sink.publish_calls == 2
    for command in (first_command, second_command):
        receipt = await factory.get_entity(
            SESSION_CREATE_IDEMPOTENCY_COLLECTION,
            f"{WORKSPACE_ID}:{PROFILE_ID}:{command.idempotency_key}",
        )
        assert isinstance(receipt, dict)
        assert receipt["state"] == SessionCreateState.ACTIVE.value


@pytest.mark.asyncio
async def test_active_create_replay_accepts_legacy_session_event_identity(tmp_path: Path) -> None:
    database_path = tmp_path / "create-event.sqlite"
    factory = SqliteUnitOfWorkFactory(database_path)
    service = _service(factory)
    command = SessionCreateCommand(WORKSPACE_ID, PROFILE_ID, "Create event", "create-event")
    created = await service.create(command)
    receipt = await factory.get_entity(
        SESSION_CREATE_IDEMPOTENCY_COLLECTION,
        f"{WORKSPACE_ID}:{PROFILE_ID}:{command.idempotency_key}",
    )
    assert isinstance(receipt, dict)
    request_hash = receipt["requestHash"]
    assert isinstance(request_hash, str)
    legacy_event_id = f"evt_{hashlib.sha256(f'session-event:create:{request_hash}'.encode()).hexdigest()}"
    legacy_trace_id = f"trace_{hashlib.sha256(f'session:create:{request_hash}'.encode()).hexdigest()}"
    legacy_idempotency_key = f"session:create:{request_hash}"
    with sqlite3.connect(database_path) as connection:
        updated = connection.execute(
            """
            UPDATE events
            SET event_id = ?, idempotency_key = ?, payload_json = json_set(payload_json, '$.traceId', ?)
            WHERE stream_id = ? AND sequence = 1
            """,
            (legacy_event_id, legacy_idempotency_key, legacy_trace_id, created.session.session_id),
        )
        assert updated.rowcount == 1
        connection.commit()

    restarted_factory = SqliteUnitOfWorkFactory(database_path)
    restarted = _service(restarted_factory, ids=DeterministicIdGenerator(start=100))
    replay = await restarted.create(command)

    assert not replay.created
    assert replay.session.session_id == created.session.session_id
    events = await restarted_factory.event_store.read(created.session.session_id)
    assert len(events) == 1
    assert events[0].event_id == legacy_event_id
    assert events[0].idempotency_key == legacy_idempotency_key
    assert parse_persisted_domain_event(events[0].payload).trace_id == legacy_trace_id


@pytest.mark.asyncio
async def test_session_start_deny_soft_aborts_invisibly_and_replays_after_ack_loss_and_restart(
    tmp_path: Path,
) -> None:
    inner = SqliteUnitOfWorkFactory(tmp_path / "session-start-deny.sqlite")
    factory = _AckLossFactory(inner)
    factory.fail_commit_numbers = {2}
    service = _service(factory)
    harness = _SessionStartHarness(service, HookDecision.DENY)
    handler = _session_create_handler(harness)
    params = SessionCreateParams(title="Denied by Hook", client_request_id="req_session_start_deny")
    first_context = ApplicationCommandContext(
        transport="stdio",
        client_id="pipe-session-start-original",
        peer="current-windows-sid",
    )

    with pytest.raises(LifecycleHookDenied) as first:
        await handler(params, ManualCancellationToken(), first_context)
    assert first.value.event is HookEvent.SESSION_START
    assert first.value.decision is HookDecision.DENY
    assert harness.hook_calls == 1

    receipt_id = f"{WORKSPACE_ID}:{PROFILE_ID}:req_session_start_deny"
    receipt = await inner.get_entity(SESSION_CREATE_IDEMPOTENCY_COLLECTION, receipt_id)
    assert isinstance(receipt, dict)
    assert receipt["state"] == "aborted"
    assert receipt["hookOutcome"] == {
        "event": "SessionStart",
        "decision": "deny",
        "reasonCode": "session_start_hook_deny",
    }
    denied_session_id = receipt["request"]["sessionId"]
    assert await inner.event_store.read(denied_session_id) == ()
    assert (await service.list(SessionListCommand(WORKSPACE_ID))).sessions == ()
    with pytest.raises(SessionNotFound):
        await service.get(SessionGetCommand(WORKSPACE_ID, denied_session_id))

    restarted = _service(inner, ids=DeterministicIdGenerator(start=100))
    harness.sessions = restarted
    replay_handler = _session_create_handler(harness)
    replay_context = ApplicationCommandContext(
        transport="loopback-http",
        client_id="web-session-start-retry",
        peer="loopback",
    )
    with pytest.raises(LifecycleHookDenied) as replay:
        await replay_handler(params, ManualCancellationToken(), replay_context)
    assert replay.value.decision is HookDecision.DENY
    assert harness.hook_calls == 1
    assert (await restarted.list(SessionListCommand(WORKSPACE_ID))).sessions == ()
    replayed_receipt = await inner.get_entity(SESSION_CREATE_IDEMPOTENCY_COLLECTION, receipt_id)
    assert isinstance(replayed_receipt, dict)
    assert replayed_receipt["request"]["connectionId"] == "pipe-session-start-original"


@pytest.mark.asyncio
async def test_session_start_allow_activation_ack_loss_restarts_without_invoking_hook_twice(tmp_path: Path) -> None:
    inner = SqliteUnitOfWorkFactory(tmp_path / "session-start-allow.sqlite")
    factory = _AckLossFactory(inner)
    factory.fail_commit_numbers = {2}
    sink = RecordingEventSink()
    service = _service(factory, sink=sink)
    harness = _SessionStartHarness(service, HookDecision.CONTINUE)
    handler = _session_create_handler(harness)
    params = SessionCreateParams(title="Allowed by Hook", client_request_id="req_session_start_allow")
    context = ApplicationCommandContext(
        transport="stdio",
        client_id="pipe-session-start-allow",
        peer="current-windows-sid",
    )

    created = await handler(params, ManualCancellationToken(), context)
    assert isinstance(created, ProtocolSessionCreateResult)
    assert created.created
    assert harness.hook_calls == 1
    assert len(await inner.event_store.read(created.session.session_id)) == 1
    assert sink.publish_calls == 1

    restarted = _service(inner, sink=sink, ids=DeterministicIdGenerator(start=100))
    harness.sessions = restarted
    replayed = await _session_create_handler(harness)(
        params,
        ManualCancellationToken(),
        ApplicationCommandContext(
            transport="loopback-http",
            client_id="web-session-start-allow-retry",
            peer="loopback",
        ),
    )
    assert isinstance(replayed, ProtocolSessionCreateResult)
    assert replayed.session.session_id == created.session.session_id
    assert not replayed.created
    assert harness.hook_calls == 1
    assert sink.publish_calls == 1
    assert len((await restarted.list(SessionListCommand(WORKSPACE_ID))).sessions) == 1
    receipt_id = f"{WORKSPACE_ID}:{PROFILE_ID}:req_session_start_allow"
    receipt = await inner.get_entity(SESSION_CREATE_IDEMPOTENCY_COLLECTION, receipt_id)
    assert isinstance(receipt, dict)
    assert receipt["state"] == SessionCreateState.ACTIVE.value
    assert receipt["hookOutcome"]["decision"] == "continue"


@pytest.mark.asyncio
async def test_pending_create_ack_loss_restart_runs_hook_with_original_connection_before_activation(
    tmp_path: Path,
) -> None:
    inner = SqliteUnitOfWorkFactory(tmp_path / "session-start-pending-restart.sqlite")
    factory = _AckLossFactory(inner)
    factory.fail_commit_numbers = {1}
    command = SessionCreateCommand(
        WORKSPACE_ID,
        PROFILE_ID,
        "Pending before restart",
        "req_session_start_pending_restart",
    )
    first = _service(factory)
    pending = await first.begin_create(command, connection_id="pipe-session-start-before-restart")
    assert pending.state is SessionCreateState.PENDING
    assert (await first.list(SessionListCommand(WORKSPACE_ID))).sessions == ()
    assert await inner.event_store.read(pending.session_id) == ()

    restarted = _service(inner, ids=DeterministicIdGenerator(start=100))
    harness = _SessionStartHarness(restarted, HookDecision.CONTINUE)
    created = await _session_create_handler(harness)(
        SessionCreateParams(
            title=command.title,
            client_request_id=command.idempotency_key,
        ),
        ManualCancellationToken(),
        ApplicationCommandContext(
            transport="loopback-http",
            client_id="web-session-start-after-restart",
            peer="loopback",
        ),
    )
    assert isinstance(created, ProtocolSessionCreateResult)
    assert created.created
    assert created.session.session_id == pending.session_id
    assert harness.hook_calls == 1
    assert harness.hook_connection_ids == ["pipe-session-start-before-restart"]
    assert len(await inner.event_store.read(pending.session_id)) == 1


@pytest.mark.asyncio
async def test_delete_final_commit_ack_loss_reconciles_tombstone_event_and_gate(tmp_path: Path) -> None:
    inner = SqliteUnitOfWorkFactory(tmp_path / "session-delete-ack.sqlite")
    normal = _service(inner)
    session_id = await _create(normal, "delete-ack")
    factory = _AckLossFactory(inner)
    factory.fail_commit_numbers = {2}
    sink = RecordingEventSink()
    service = _service(factory, sink=sink, ids=DeterministicIdGenerator(start=100))

    command = SessionDeleteCommand(WORKSPACE_ID, session_id, 1, "delete-ack")
    deleted = await service.soft_delete(command)
    assert deleted.deleted
    assert await inner.get_entity(SESSION_OPERATION_COLLECTION, session_id) is None
    assert len(await inner.event_store.read(session_id)) == 2
    assert sink.publish_calls == 1

    replay = await service.soft_delete(command)
    assert replay == deleted
    assert sink.publish_calls == 1


class _PauseAfterDeleteGate(SessionLifecycleService):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.gate_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def _load_delete_authority(self, command: SessionDeleteCommand):  # type: ignore[no-untyped-def]
        self.gate_entered.set()
        await self.release.wait()
        return await super()._load_delete_authority(command)


class _StopPlanner:
    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        return PlanningStep(
            (),
            False,
            "done",
            attempts=(
                PlanningAttempt(
                    request_id=f"test-stop-{state.model_rounds + 1}",
                    repair_index=0,
                    outcome=PlanningAttemptOutcome.SUCCEEDED,
                    usage=ModelUsage(0, 0, 0, 0),
                ),
            ),
        )


class _WaitingPlanner:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        del state
        self.entered.set()
        await cancellation.wait()
        cancellation.checkpoint()
        raise AssertionError("cancellation checkpoint must raise")


def _historical_write_definition() -> ToolDefinition:
    return ToolDefinition(
        name="fixture.write",
        version="1",
        description="fixture committed write",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={},
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        required_capabilities=frozenset({"fixture.write"}),
        concurrency_safe=False,
        idempotent=True,
        retryable=False,
        timeout_ms=1_000,
        output_limit_bytes=1_024,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


class _HistoricalEffectPlanner:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self._calls = 0

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        self._calls += 1
        if self._calls == 1:
            arguments = {"path": "fixture.md"}
            definition = _historical_write_definition()
            return PlanningStep(
                (
                    ToolCall(
                        tool_call_id="call_historical_write",
                        run_id=state.run_id,
                        workspace_id=state.workspace_id,
                        name=definition.name,
                        version=definition.version,
                        arguments=arguments,
                        args_hash=canonical_json_sha256(arguments),
                        idempotency_key="historical-write",
                        deadline=None,
                        lineage=state.lineage,
                        definition_fingerprint=definition.fingerprint,
                        result_sensitivity=definition.result_sensitivity,
                    ),
                ),
                True,
                None,
                attempts=(
                    PlanningAttempt(
                        request_id=f"test-historical-{state.model_rounds + 1}",
                        repair_index=0,
                        outcome=PlanningAttemptOutcome.SUCCEEDED,
                        usage=ModelUsage(0, 0, 0, 0),
                    ),
                ),
            )
        self.entered.set()
        await cancellation.wait()
        cancellation.checkpoint()
        raise AssertionError("cancellation checkpoint must raise")


class _HistoricalEffectKernel:
    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        cancellation.checkpoint()
        assert observer is not None
        (call,) = calls
        result = ToolResult(
            tool_call_id=call.tool_call_id,
            status=ToolResultStatus.SUCCEEDED,
            data={"ok": True},
            user_visible_summary="fixture write committed",
            artifact_ids=(),
            source_refs=(),
            side_effects=(
                SideEffect(
                    SideEffectKind.FILE_WRITE,
                    SideEffectState.COMMITTED,
                    "fixture.md",
                    None,
                    {"hash": "after"},
                ),
            ),
            retryable=False,
            before_state=None,
            after_state={"hash": "after"},
            error=None,
        )
        execution = ToolExecution(call, _historical_write_definition(), result)
        await observer.execution_started(call, execution.definition)
        await observer.result_available(call, execution.definition, result)
        return (execution,)


class _NoToolKernel:
    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        del calls, cancellation, observer
        raise AssertionError("no ToolCall expected")


class _Components:
    def __init__(self, planner: Planner, kernel: ToolKernel | None = None) -> None:
        self._planner = planner
        self._kernel = kernel or _NoToolKernel()

    def build(self, command: StartTurnCommand, state: RunState) -> RunComponents:
        del command, state
        return RunComponents(
            planner_factory=lambda _budget: self._planner,
            tool_kernel_factory=lambda _budget: self._kernel,
            budget=RunBudget(
                max_model_rounds=4,
                max_tool_calls=4,
                max_parallel_reads=2,
                max_wall_seconds=60,
                max_input_tokens=100,
                max_output_tokens=100,
                max_cost=Decimal("1"),
                max_artifact_bytes=10_000,
                max_subagents=2,
            ),
        )


def _turn_command(session_id: str) -> StartTurnCommand:
    return StartTurnCommand(
        workspace_id=WORKSPACE_ID,
        session_id=session_id,
        turn_id="turn_delete_race",
        idempotency_key="turn-delete-race",
        input_blocks=({"type": "text", "text": "hello"},),
        run_config={"model": "fake"},
    )


@pytest.mark.asyncio
async def test_delete_gate_blocks_new_turn_and_same_idempotency_resumes_after_restart(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "session-delete-gate.sqlite")
    clock = ManualClock(NOW)
    manager = TurnManager()
    approvals = ApprovalManager(unit_of_work=factory, clock=clock)
    sink = RecordingEventSink()
    pausing = _PauseAfterDeleteGate(
        unit_of_work=factory,
        event_sink=sink,
        clock=clock,
        ids=DeterministicIdGenerator(),
        turn_manager=manager,
        approval_manager=approvals,
    )
    harness = HarnessService(
        unit_of_work=factory,
        event_sink=sink,
        clock=clock,
        ids=DeterministicIdGenerator(start=100),
        components=_Components(_StopPlanner()),
        turn_manager=manager,
        approval_manager=approvals,
        session_service=pausing,
    )
    created = await harness.create_session(
        CreateSessionCommand(WORKSPACE_ID, PROFILE_ID, "Delete race", "create-delete-race")
    )
    command = SessionDeleteCommand(WORKSPACE_ID, created.session_id, 1, "delete-resume")
    deleting = asyncio.create_task(harness.delete_session(command))
    await pausing.gate_entered.wait()

    with pytest.raises(SessionRunConflict, match="lifecycle operation"):
        await harness.start_turn(_turn_command(created.session_id))
    deleting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await deleting
    assert await factory.get_entity(SESSION_OPERATION_COLLECTION, created.session_id) is not None

    restarted = _service(factory, ids=DeterministicIdGenerator(start=200))
    with pytest.raises(SessionOperationConflict, match="another lifecycle operation"):
        await restarted.soft_delete(SessionDeleteCommand(WORKSPACE_ID, created.session_id, 1, "different-delete-key"))
    deleted = await restarted.soft_delete(command)
    assert deleted.deleted
    assert await factory.get_entity(SESSION_OPERATION_COLLECTION, created.session_id) is None


@pytest.mark.asyncio
async def test_soft_delete_cancels_planning_run_with_historical_committed_write(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "session-delete-active.sqlite")
    clock = ManualClock(NOW)
    manager = TurnManager()
    approvals = ApprovalManager(unit_of_work=factory, clock=clock)
    planner = _HistoricalEffectPlanner()
    sink = RecordingEventSink()
    harness = HarnessService(
        unit_of_work=factory,
        event_sink=sink,
        clock=clock,
        ids=DeterministicIdGenerator(),
        components=_Components(planner, _HistoricalEffectKernel()),
        turn_manager=manager,
        approval_manager=approvals,
    )
    created = await harness.create_session(
        CreateSessionCommand(WORKSPACE_ID, PROFILE_ID, "Active delete", "create-active-delete")
    )
    receipt = await harness.start_turn(_turn_command(created.session_id))
    await planner.entered.wait()
    current = await harness.sessions.get(SessionGetCommand(WORKSPACE_ID, created.session_id))
    deleted = await harness.delete_session(
        SessionDeleteCommand(WORKSPACE_ID, created.session_id, current.revision, "delete-active")
    )
    assert deleted.active_runs_cancel_requested == (receipt.run_id,)
    assert (await harness.get_run(receipt.run_id)).status is RunStatus.CANCELLED
    assert await factory.get_entity("active_root_runs", created.session_id) is None
    assert (await harness.sessions.get_fork_reference(WORKSPACE_ID, created.session_id)) is None


@pytest.mark.asyncio
async def test_soft_delete_refuses_unconfirmed_effect_window_without_cancelling_it(
    tmp_path: Path,
) -> None:
    phase = RunPhase.EXECUTING_TOOLS
    factory = SqliteUnitOfWorkFactory(tmp_path / f"session-delete-danger-{phase.value}.sqlite")
    manager = TurnManager()
    service = _service(factory, manager=manager)
    session_id = await _create(service, "effectful")
    await _seed_dangerous_active_run(
        factory,
        session_id=session_id,
        phase=phase,
    )

    async def wait_for_cleanup(cancellation):  # type: ignore[no-untyped-def]
        await cancellation.wait()

    active = await manager.start(
        session_id=session_id,
        run_id="run_effectful",
        factory=wait_for_cleanup,
    )
    with pytest.raises(SessionForkRejected, match="active root Run"):
        await service.fork(
            SessionForkCommand(
                WORKSPACE_ID,
                session_id,
                "turn_effectful",
                2,
                f"fork-danger-{phase.value}",
                fork_run_id="run_effectful",
            )
        )
    with pytest.raises(SessionActiveEffectfulRun, match="active effectful Run"):
        await service.soft_delete(SessionDeleteCommand(WORKSPACE_ID, session_id, 2, "delete-effectful"))
    assert not active.cancellation.cancelled
    assert await factory.get_entity(SESSION_OPERATION_COLLECTION, session_id) is None

    await manager.cancel(
        active.run_id,
        CancellationReason.now(CancellationCode.USER, "fixture cleanup"),
    )
    await active.task


@pytest.mark.asyncio
async def test_soft_delete_cancels_pending_approval_and_revokes_session_grant(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "session-delete-approvals.sqlite")
    clock = ManualClock(NOW)
    manager = TurnManager()
    approvals = ApprovalManager(unit_of_work=factory, clock=clock)
    service = SessionLifecycleService(
        unit_of_work=factory,
        event_sink=RecordingEventSink(),
        clock=clock,
        ids=DeterministicIdGenerator(),
        turn_manager=manager,
        approval_manager=approvals,
    )
    session_id = await _create(service, "approvals")

    def approval(tool_call_id: str, suffix: str) -> ApprovalRequest:
        binding = ApprovalBinding(
            tool_name="fixture.write",
            tool_version="1",
            definition_fingerprint="sha256:" + suffix * 64,
            args_hash="sha256:" + suffix * 64,
            workspace_id=WORKSPACE_ID,
            session_id=session_id,
            principal_id="principal_session_delete",
            root_run_id=f"run_{suffix}",
            run_id=f"run_{suffix}",
            agent_name="root",
            ancestor_run_ids=(),
            expected_state_hash="absent",
            expires_at=NOW.replace(hour=13),
        )
        return ApprovalRequest(
            approval_id=approval_id_for(tool_call_id, binding),
            tool_call_id=tool_call_id,
            binding=binding,
            risk=RiskClass.WRITE,
            summary=f"approval {suffix}",
            diff_artifact_ids=(f"art_{suffix}",),
        )

    reusable = approval("call_reusable", "a")
    pending = approval("call_pending", "b")
    await approvals._ensure_pending(reusable)
    await approvals.resolve(
        ApprovalResolution(
            approval_id=reusable.approval_id,
            state=ApprovalState.APPROVED,
            scope=ApprovalScope.SESSION,
            resolved_at=NOW,
            resolver_id="user",
            include_descendants=False,
        )
    )
    await approvals._ensure_pending(pending)

    deleted = await service.soft_delete(SessionDeleteCommand(WORKSPACE_ID, session_id, 1, "delete-approvals"))
    assert deleted.deleted
    stored_pending = await factory.get_entity("approvals", pending.approval_id)
    assert isinstance(stored_pending, ApprovalRecord)
    assert stored_pending.state is ApprovalState.CANCELLED
    grants = await factory.list_entities("approval_grants")
    assert len(grants) == 1
    assert grants[0].value.state is ApprovalGrantState.REVOKED


@pytest.mark.asyncio
async def test_fork_is_reference_only_restart_idempotent_and_survives_source_soft_delete(tmp_path: Path) -> None:
    database_path = tmp_path / "session-fork.sqlite"
    factory = SqliteUnitOfWorkFactory(database_path)
    sink = RecordingEventSink()

    async def artifact_links(
        workspace_id: str,
        source_session_id: str,
        source_turn_id: str,
        source_run_id: str,
    ) -> Sequence[str]:
        assert (workspace_id, source_session_id, source_turn_id, source_run_id) == (
            WORKSPACE_ID,
            source_id,
            "turn_fork_boundary",
            "run_fork_boundary",
        )
        return ("art_a", "art_b")

    service = _service(factory, sink=sink, artifact_link_resolver=artifact_links)
    source_id = await _create(service, "source")
    await _seed_terminal_boundary(factory, session_id=source_id)

    with pytest.raises(SessionNotFound):
        await service.fork(
            SessionForkCommand(
                "ws_other",
                source_id,
                "turn_fork_boundary",
                1,
                "fork-cross-workspace",
                fork_run_id="run_fork_boundary",
            )
        )
    with pytest.raises(SessionForkRejected, match="does not belong"):
        await service.fork(
            SessionForkCommand(
                WORKSPACE_ID,
                source_id,
                "turn_missing",
                1,
                "fork-missing-turn",
            )
        )

    command = SessionForkCommand(
        WORKSPACE_ID,
        source_id,
        "turn_fork_boundary",
        1,
        "fork-once",
        fork_run_id="run_fork_boundary",
        title="Reference-only fork",
    )
    forked = await service.fork(command)
    assert forked.created
    assert forked.fork_reference.source_event_sequence == 1
    assert forked.fork_reference.artifact_link_ids == ("art_a", "art_b")
    turns = await factory.list_entities("turns")
    runs = await factory.list_entities("runs")
    assert [record.entity_id for record in turns] == ["turn_fork_boundary"]
    assert [record.entity_id for record in runs] == ["run_fork_boundary"]
    fork_events = await factory.event_store.read(forked.session.session_id)
    assert len(fork_events) == 1
    fork_payload = parse_persisted_domain_event(fork_events[0].payload).payload
    assert isinstance(fork_payload, SessionUpdatedPayload)
    assert fork_payload.changed_fields == ["created", "forkReference"]
    assert fork_payload.fork_reference is not None
    assert fork_payload.fork_reference.source_session_id == source_id

    restarted = _service(SqliteUnitOfWorkFactory(database_path), sink=sink, ids=DeterministicIdGenerator(start=100))
    replay = await restarted.fork(command)
    assert replay.session.session_id == forked.session.session_id
    assert not replay.created
    publish_calls_before_delete = sink.publish_calls

    deleted = await restarted.soft_delete(SessionDeleteCommand(WORKSPACE_ID, source_id, 1, "delete-source"))
    assert deleted.deleted
    after_delete_replay = await restarted.fork(command)
    assert after_delete_replay.fork_reference == forked.fork_reference
    assert not after_delete_replay.created
    assert sink.publish_calls == publish_calls_before_delete + 1
    assert await restarted.get_fork_reference(WORKSPACE_ID, forked.session.session_id) == forked.fork_reference
    with pytest.raises(SessionDeleted):
        await restarted.fork(replace(command, idempotency_key="fork-after-delete"))


@pytest.mark.asyncio
async def test_fork_commit_ack_loss_reconciles_immutable_event_without_duplicate_publish(tmp_path: Path) -> None:
    inner = SqliteUnitOfWorkFactory(tmp_path / "session-fork-ack.sqlite")
    normal = _service(inner)
    source_id = await _create(normal, "fork-ack-source")
    await _seed_terminal_boundary(inner, session_id=source_id)
    factory = _AckLossFactory(inner)
    sink = RecordingEventSink()
    service = _service(factory, sink=sink, ids=DeterministicIdGenerator(start=100))
    command = SessionForkCommand(
        WORKSPACE_ID,
        source_id,
        "turn_fork_boundary",
        1,
        "fork-ack",
        fork_run_id="run_fork_boundary",
    )
    factory.fail_next_commit_ack = True
    forked = await service.fork(command)
    assert forked.created
    assert len(await inner.event_store.read(forked.session.session_id)) == 1
    assert sink.publish_calls == 1
    replay = await service.fork(command)
    assert not replay.created
    assert sink.publish_calls == 1
