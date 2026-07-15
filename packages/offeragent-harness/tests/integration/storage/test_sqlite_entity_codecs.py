from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteEntityStore, SqliteUnitOfWorkFactory
from offeragent_harness.agent import RunBudget
from offeragent_harness.agent.composer import CompositionEvent
from offeragent_harness.agent.loop import ToolExecution
from offeragent_harness.agent.planner import Planner, PlanningAttempt, PlanningAttemptOutcome, PlanningStep
from offeragent_harness.agent.state import (
    PendingWork,
    RunPhase,
    RunState,
    WriteObligation,
    WriteOutcome,
)
from offeragent_harness.foundation import canonical_json_sha256
from offeragent_harness.models import ModelUsage
from offeragent_harness.permissions import (
    ApprovalBinding,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalScope,
    ApprovalState,
    RiskClass,
)
from offeragent_harness.ports import CancellationToken, ToolLifecycleObserver
from offeragent_harness.runtime import TurnManager
from offeragent_harness.runtime.approval_manager import ApprovalRecord
from offeragent_harness.runtime.harness_service import (
    CreateSessionCommand,
    HarnessService,
    RunComponents,
    StartTurnCommand,
)
from offeragent_harness.sessions import (
    AgentLineage,
    Run,
    RunKind,
    RunStatus,
    Session,
    SessionStatus,
    TerminationReason,
    Turn,
    TurnStatus,
)
from offeragent_harness.storage import (
    EntityCodecError,
    EntityCodecTypeError,
    EntityCodecVersionError,
)
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualClock,
    RecordingEventSink,
)
from offeragent_harness.tools import (
    ResultSensitivity,
    SideEffect,
    SideEffectKind,
    SideEffectState,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)

NOW = datetime(2026, 7, 12, 8, 30, tzinfo=timezone.utc)


def _tool_result() -> ToolResult:
    return ToolResult(
        tool_call_id="call_1",
        status=ToolResultStatus.SUCCEEDED,
        data={"path": "notes/result.md", "count": 2},
        user_visible_summary="已写入",
        artifact_ids=("artifact_1",),
        source_refs=("vault:notes/source.md",),
        side_effects=(
            SideEffect(
                kind=SideEffectKind.FILE_WRITE,
                state=SideEffectState.COMMITTED,
                resource_id="vault:notes/result.md",
                before_state={"hash": "before"},
                after_state={"hash": "after"},
                metadata={"mode": "replace"},
            ),
        ),
        retryable=False,
        before_state={"hash": "before"},
        after_state={"hash": "after"},
        error=None,
        source_references=(
            {
                "type": "vault",
                "file": {"workspaceId": "workspace_1", "path": "notes/source.md"},
                "freshness": "unknown",
            },
        ),
    )


@pytest.mark.asyncio
async def test_registered_domain_entities_round_trip_exactly_after_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    lineage = AgentLineage.root("run_1")
    session = Session(
        session_id="session_1",
        workspace_id="workspace_1",
        profile_id="profile_1",
        title="面试准备",
        status=SessionStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        revision=2,
    )
    turn = Turn(
        turn_id="turn_1",
        session_id=session.session_id,
        ordinal=1,
        status=TurnStatus.COMPLETED,
        input_blocks=({"type": "text", "text": "你好"},),
        created_at=NOW,
        updated_at=NOW,
        revision=2,
    )
    run = Run(
        run_id=lineage.run_id,
        session_id=session.session_id,
        turn_id=turn.turn_id,
        workspace_id=session.workspace_id,
        lineage=lineage,
        kind=RunKind.ROOT,
        status=RunStatus.COMPLETED,
        attempt=1,
        event_sequence=9,
        config_snapshot={"model": "fake", "temperature": 0},
        created_at=NOW,
        updated_at=NOW,
        deadline_at=NOW + timedelta(minutes=1),
        termination_reason=TerminationReason.COMPLETED,
    )
    pending_arguments = {"path": "notes/pending.md"}
    pending_call = ToolCall(
        tool_call_id="call_2",
        run_id=run.run_id,
        workspace_id=session.workspace_id,
        name="workspace.read",
        version="1",
        arguments=pending_arguments,
        args_hash=canonical_json_sha256(pending_arguments),
        idempotency_key="idem_call_2",
        deadline=NOW + timedelta(minutes=1),
        lineage=lineage,
        definition_fingerprint="sha256:" + ("0" * 64),
        result_sensitivity=ResultSensitivity.WORKSPACE,
    )
    state = RunState(
        workspace_id=session.workspace_id,
        session_id=session.session_id,
        turn_id=turn.turn_id,
        run_id=run.run_id,
        lineage=lineage,
        phase=RunPhase.AWAITING_APPROVAL,
        revision=7,
        model_rounds=2,
        tool_calls=1,
        pending=PendingWork(
            tool_call_ids=frozenset({"call_2"}),
            tool_calls=(pending_call,),
            approval_ids=frozenset({"approval_1"}),
            child_run_ids=frozenset({"child_1"}),
        ),
        write_obligation=WriteObligation(
            required=True,
            reasons=("用户要求写入",),
            outcomes=(
                WriteOutcome(
                    "call_1",
                    ToolResultStatus.SUCCEEDED,
                    "已写入",
                ),
            ),
        ),
        tool_results=(_tool_result(),),
        assistant_text="处理中",
        tool_result_sensitivities={
            "call_2": ResultSensitivity.WORKSPACE,
            "call_1": ResultSensitivity.WORKSPACE,
        },
    )
    binding = ApprovalBinding(
        tool_name="vault.write",
        tool_version="1",
        definition_fingerprint="sha256:" + "0" * 64,
        args_hash="sha256:" + "a" * 64,
        workspace_id=session.workspace_id,
        session_id=session.session_id,
        principal_id="principal_1",
        root_run_id=run.run_id,
        run_id=run.run_id,
        agent_name="root",
        ancestor_run_ids=(),
        expected_state_hash="sha256:" + "b" * 64,
        expires_at=NOW + timedelta(minutes=5),
    )
    request = ApprovalRequest(
        approval_id="approval_1",
        tool_call_id="call_2",
        binding=binding,
        risk=RiskClass.WRITE,
        summary="写入 notes/result.md",
        diff_artifact_ids=("diff_1",),
    )
    pending_approval = ApprovalRecord(request, ApprovalState.PENDING, revision=1)
    resolution = ApprovalResolution(
        approval_id=request.approval_id,
        state=ApprovalState.APPROVED,
        scope=ApprovalScope.ONCE,
        resolved_at=NOW,
        resolver_id="user_1",
        include_descendants=False,
        reason="确认写入",
    )
    resolved_approval = ApprovalRecord(request, ApprovalState.APPROVED, revision=2, resolution=resolution)
    receipt = {
        "schemaVersion": 1,
        "requestHash": "sha256:request",
        "kind": "turn",
        "receipt": {"sessionId": session.session_id, "turnId": turn.turn_id, "runId": run.run_id},
    }

    factory = SqliteUnitOfWorkFactory(database_path)
    async with factory.begin() as uow:
        await uow.entities.put("sessions", session.session_id, session, expected_revision=0)
        await uow.entities.put("turns", turn.turn_id, turn, expected_revision=0)
        await uow.entities.put("runs", run.run_id, run, expected_revision=0)
        await uow.entities.put("run_states", run.run_id, state, expected_revision=0)
        await uow.entities.put("approvals", request.approval_id, pending_approval, expected_revision=0)
        await uow.entities.put("approvals", request.approval_id, resolved_approval, expected_revision=1)
        await uow.entities.put("turn_idempotency", "idem_1", receipt, expected_revision=0)
        await uow.commit()

    reopened = SqliteUnitOfWorkFactory(database_path)
    assert await reopened.get_entity("sessions", session.session_id) == session
    assert await reopened.get_entity("turns", turn.turn_id) == turn
    assert await reopened.get_entity("runs", run.run_id) == run
    assert await reopened.get_entity("run_states", run.run_id) == state
    assert await reopened.get_entity("approvals", request.approval_id) == resolved_approval
    assert await reopened.get_entity("turn_idempotency", "idem_1") == receipt

    # A durable entity must match the current schema exactly. Old state is
    # rejected rather than assigned an inferred result sensitivity.
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT value_json FROM entities WHERE collection = 'run_states' AND entity_id = ?",
            (run.run_id,),
        ).fetchone()
        assert row is not None
        envelope = json.loads(row[0])
        payload = envelope["payload"]
        payload.pop("toolResultSensitivities")
        payload["pending"]["toolCalls"][0].pop("resultSensitivity")
        connection.execute(
            "UPDATE entities SET value_json = ? WHERE collection = 'run_states' AND entity_id = ?",
            (json.dumps(envelope), run.run_id),
        )
    with pytest.raises(EntityCodecError, match="fields are incompatible"):
        await SqliteUnitOfWorkFactory(database_path).get_entity("run_states", run.run_id)


@pytest.mark.asyncio
async def test_entity_codecs_fail_closed_for_wrong_types_and_unknown_versions(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    store = SqliteEntityStore(database_path)
    with pytest.raises(EntityCodecTypeError, match="requires Run"):
        await store.put("runs", "run_1", {"status": "completed"}, expected_revision=0)
    with pytest.raises(EntityCodecTypeError, match="plain JSON only"):
        await store.put("plugin_state", "bad", object(), expected_revision=0)

    session = Session(
        session_id="session_1",
        workspace_id="workspace_1",
        profile_id="profile_1",
        title="Session",
        status=SessionStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        revision=1,
    )
    await store.put("sessions", session.session_id, session, expected_revision=0)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT value_json FROM entities WHERE collection = 'sessions' AND entity_id = ?",
            (session.session_id,),
        ).fetchone()
        assert row is not None
        envelope = json.loads(row[0])
        envelope["schemaVersion"] = 99
        connection.execute(
            "UPDATE entities SET value_json = ? WHERE collection = 'sessions' AND entity_id = ?",
            (json.dumps(envelope), session.session_id),
        )

    with pytest.raises(EntityCodecVersionError, match="expected codec"):
        await SqliteEntityStore(database_path).get("sessions", session.session_id)

    await store.put("plugin_state", "valid", {"enabled": True}, expected_revision=0)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE entities SET value_json = ? WHERE collection = 'plugin_state' AND entity_id = 'valid'",
            ('{"codec":"json","schemaVersion":1,"payload":{},"unexpected":true}',),
        )
    with pytest.raises(EntityCodecError, match="fields are incompatible"):
        await store.get("plugin_state", "valid")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "memory"])
async def test_entity_listing_has_stable_bounded_id_pagination(tmp_path: Path, backend: str) -> None:
    factory: Any
    if backend == "sqlite":
        factory = SqliteUnitOfWorkFactory(tmp_path / "state.sqlite")
    else:
        factory = InMemoryUnitOfWorkFactory()

    async with factory.begin() as uow:
        for entity_id in ("delta", "alpha", "charlie", "bravo"):
            await uow.entities.put("recovery_records", entity_id, {"id": entity_id}, expected_revision=0)
        await uow.entities.put("recovery_records", "charlie", {"id": "charlie", "v": 2}, expected_revision=1)
        await uow.commit()

    first_page = await factory.list_entities("recovery_records", limit=2)
    second_page = await factory.list_entities("recovery_records", first_page[-1].entity_id, 2)
    assert [item.entity_id for item in first_page] == ["alpha", "bravo"]
    assert [item.entity_id for item in second_page] == ["charlie", "delta"]
    assert second_page[0].revision == 2
    assert second_page[0].value == {"id": "charlie", "v": 2}
    with pytest.raises(ValueError, match="between 1 and 1000"):
        await factory.list_entities("recovery_records", limit=0)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        await factory.list_entities("recovery_records", limit=1_001)


class _StopPlanner:
    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        cancellation.checkpoint()
        return PlanningStep(
            (),
            False,
            "done",
            attempts=(
                PlanningAttempt(
                    request_id=f"test-storage-{state.model_rounds + 1}",
                    repair_index=0,
                    outcome=PlanningAttemptOutcome.SUCCEEDED,
                    usage=ModelUsage(0, 0, 0, 0),
                ),
            ),
        )


class _Composer:
    def stream(
        self,
        state: RunState,
        *,
        partial: bool,
        cancellation: CancellationToken,
    ) -> AsyncIterator[CompositionEvent]:
        async def generate() -> AsyncIterator[CompositionEvent]:
            cancellation.checkpoint()
            yield CompositionEvent(text_delta="answer")

        return generate()


class _NoToolKernel:
    async def execute_batch(
        self,
        calls: Sequence[ToolCall],
        cancellation: CancellationToken,
        observer: ToolLifecycleObserver | None = None,
    ) -> tuple[ToolExecution, ...]:
        del observer
        raise AssertionError("no tool call expected")


class _Components:
    planner: Planner = _StopPlanner()

    def build(self, command: StartTurnCommand, state: RunState) -> RunComponents:
        return RunComponents(
            planner_factory=lambda budget: self.planner,
            tool_kernel_factory=lambda budget: _NoToolKernel(),
            composer=_Composer(),
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


def _harness(
    database_path: Path,
    *,
    manager: TurnManager,
    ids_start: int,
    sink: RecordingEventSink,
) -> tuple[HarnessService, SqliteUnitOfWorkFactory]:
    factory = SqliteUnitOfWorkFactory(database_path)
    return (
        HarnessService(
            unit_of_work=factory,
            event_sink=sink,
            clock=ManualClock(NOW),
            ids=DeterministicIdGenerator(start=ids_start),
            components=_Components(),
            turn_manager=manager,
        ),
        factory,
    )


@pytest.mark.asyncio
async def test_real_harness_turn_completes_and_recovers_exactly_after_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    first_manager = TurnManager()
    first_sink = RecordingEventSink()
    first, first_factory = _harness(
        database_path,
        manager=first_manager,
        ids_start=1,
        sink=first_sink,
    )
    session_command = CreateSessionCommand("ws_1", "profile_1", "Session", "session-idem")
    session_receipt = await first.create_session(session_command)
    turn_command = StartTurnCommand(
        workspace_id="ws_1",
        session_id=session_receipt.session_id,
        turn_id="turn_client_1",
        idempotency_key="turn-idem",
        input_blocks=({"type": "text", "text": "hello"},),
        run_config={"model": "fake"},
    )
    turn_receipt = await first.start_turn(turn_command)
    active = await first_manager.get(turn_receipt.run_id)
    assert active is not None
    completed_state = await active.task
    completed_turn = await first.get_turn(turn_command.turn_id)
    completed_run = await first.get_run(turn_receipt.run_id)
    events_before_reopen = await first.replay_events(turn_receipt.run_id)

    assert completed_state.phase is RunPhase.COMPLETED
    assert completed_state.assistant_text == "answer"
    assert completed_turn.status is TurnStatus.COMPLETED
    assert completed_run.status is RunStatus.COMPLETED
    assert completed_run.termination_reason is TerminationReason.COMPLETED
    assert len([event for event in events_before_reopen if event.terminal]) == 1
    assert events_before_reopen[-1].event_type == "turn.completed"

    second_sink = RecordingEventSink()
    reopened, reopened_factory = _harness(
        database_path,
        manager=TurnManager(),
        ids_start=500,
        sink=second_sink,
    )
    duplicate_session = await reopened.create_session(session_command)
    duplicate_turn = await reopened.start_turn(turn_command)

    assert duplicate_session.session_id == session_receipt.session_id
    assert not duplicate_session.created
    assert duplicate_turn == replace(turn_receipt, duplicate=True)
    assert await reopened.get_run_state(turn_receipt.run_id) == completed_state
    assert await reopened.get_turn(turn_command.turn_id) == completed_turn
    assert await reopened.get_run(turn_receipt.run_id) == completed_run
    assert await reopened.replay_events(turn_receipt.run_id) == events_before_reopen
    assert second_sink.events == []
    run_page = await reopened_factory.list_entities("runs")
    assert [(item.entity_id, item.value) for item in run_page] == [(turn_receipt.run_id, completed_run)]
    raw_receipt = await first_factory.get_entity(
        "turn_idempotency",
        f"{session_receipt.session_id}:{turn_command.idempotency_key}",
    )
    assert isinstance(raw_receipt, dict)
    assert raw_receipt["schemaVersion"] == 1
