from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.permissions import (
    ApprovalBinding,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalScope,
    ApprovalState,
    RiskClass,
    approval_id_for,
)
from offeragent_harness.runtime.approval_manager import (
    ApprovalConflict,
    ApprovalManager,
    ApprovalRecord,
)
from offeragent_harness.testing import ManualCancellationToken, ManualClock

NOW = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
DEFINITION_FINGERPRINT = "sha256:" + "1" * 64


def binding(*, expires_at: datetime | None = None) -> ApprovalBinding:
    return ApprovalBinding(
        tool_name="vault.transaction",
        tool_version="1",
        definition_fingerprint=DEFINITION_FINGERPRINT,
        args_hash="sha256:" + "a" * 64,
        workspace_id="ws_1",
        session_id="ses_1",
        principal_id="principal_1",
        root_run_id="run_1",
        run_id="run_1",
        agent_name="root",
        ancestor_run_ids=(),
        expected_state_hash="sha256:" + "b" * 64,
        expires_at=expires_at or NOW + timedelta(minutes=5),
    )


def request(*, approval_id: str | None = None, value: ApprovalBinding | None = None) -> ApprovalRequest:
    actual_binding = value or binding()
    return ApprovalRequest(
        approval_id=approval_id or approval_id_for("call_1", actual_binding),
        tool_call_id="call_1",
        binding=actual_binding,
        risk=RiskClass.WRITE,
        summary="write exact note",
        diff_artifact_ids=("art_diff",),
    )


def drift_binding(original: ApprovalBinding, field: str, value: str) -> ApprovalBinding:
    if field == "workspace_id":
        return replace(original, workspace_id=value)
    if field == "session_id":
        return replace(original, session_id=value)
    if field == "principal_id":
        return replace(original, principal_id=value)
    if field == "definition_fingerprint":
        return replace(original, definition_fingerprint=value)
    if field == "args_hash":
        return replace(original, args_hash=value)
    if field == "expected_state_hash":
        return replace(original, expected_state_hash=value)
    if field == "agent_name":
        return replace(original, agent_name=value)
    raise AssertionError(f"unsupported drift field: {field}")


class RecordingObserver:
    def __init__(self) -> None:
        self.required_requests: list[ApprovalRequest] = []
        self.resolutions: list[ApprovalResolution] = []
        self.required_event = asyncio.Event()

    async def required(self, approval: ApprovalRequest) -> None:
        self.required_requests.append(approval)
        self.required_event.set()

    async def resolved(self, approval: ApprovalRequest, resolution: ApprovalResolution) -> None:
        del approval
        self.resolutions.append(resolution)


async def _wait_until_pending(manager: ApprovalManager, approval_id: str) -> None:
    for _ in range(100):
        if await manager.pending(approval_id) is not None:
            return
        await asyncio.sleep(0)
    raise AssertionError("approval did not become durable")


def test_canonical_approval_id_is_stable_across_ttl_refresh_but_changes_on_binding_drift() -> None:
    original = binding()
    assert approval_id_for("call_1", original) == approval_id_for(
        "call_1",
        replace(original, expires_at=NOW + timedelta(hours=1)),
    )
    assert approval_id_for("call_1", original) != approval_id_for(
        "call_1",
        replace(original, args_hash="sha256:" + "c" * 64),
    )


@pytest.mark.asyncio
async def test_sqlite_restart_reconnects_and_duplicate_request_reuses_original_id_and_expiry(tmp_path: Path) -> None:
    database_path = tmp_path / "approval-reconnect.sqlite"
    clock = ManualClock(NOW)
    factory = SqliteUnitOfWorkFactory(database_path)
    original_request = request()
    first = ApprovalManager(unit_of_work=factory, clock=clock)
    first_waiter = asyncio.create_task(first.request(original_request, ManualCancellationToken()))
    await _wait_until_pending(first, original_request.approval_id)
    first_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_waiter

    restarted = ApprovalManager(unit_of_work=SqliteUnitOfWorkFactory(database_path), clock=clock)
    assert await restarted.list_pending(
        workspace_id="ws_1",
        session_id="ses_1",
        principal_id="principal_1",
    ) == (original_request,)
    assert (
        await restarted.list_pending(
            workspace_id="ws_1",
            session_id="ses_1",
            principal_id="other_principal",
        )
        == ()
    )

    retry_binding = replace(original_request.binding, expires_at=NOW + timedelta(minutes=30))
    retry_request = request(approval_id="apr_" + "f" * 64, value=retry_binding)
    observer = RecordingObserver()
    retry_waiter = asyncio.create_task(restarted.request(retry_request, ManualCancellationToken(), observer))
    await asyncio.wait_for(observer.required_event.wait(), timeout=2)
    assert observer.required_requests == [original_request]
    assert observer.required_requests[0].binding.expires_at == NOW + timedelta(minutes=5)

    decision = ApprovalResolution(
        approval_id=original_request.approval_id,
        state=ApprovalState.APPROVED,
        scope=ApprovalScope.ONCE,
        resolved_at=NOW,
        resolver_id="principal_1",
        include_descendants=False,
    )
    assert await restarted.resolve(decision) == decision
    retry_receipt = await retry_waiter
    assert retry_receipt.request == original_request
    assert retry_receipt.resolution == decision
    records = await SqliteUnitOfWorkFactory(database_path).list_entities("approvals")
    assert [(row.entity_id, row.revision) for row in records] == [(original_request.approval_id, 2)]
    assert (
        await restarted.list_pending(
            workspace_id="ws_1",
            session_id="ses_1",
            principal_id="principal_1",
        )
        == ()
    )


@pytest.mark.asyncio
async def test_two_sqlite_decision_makers_compete_with_cas_and_replay_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "approval-race.sqlite"
    clock = ManualClock(NOW)
    factory = SqliteUnitOfWorkFactory(database_path)
    approval = request()
    creator = ApprovalManager(unit_of_work=factory, clock=clock)
    await creator._ensure_pending(approval)

    first = ApprovalManager(unit_of_work=SqliteUnitOfWorkFactory(database_path), clock=clock)
    second = ApprovalManager(unit_of_work=SqliteUnitOfWorkFactory(database_path), clock=clock)
    reached = 0
    both_read = asyncio.Event()

    def pause_after_read(manager: ApprovalManager):  # type: ignore[no-untyped-def]
        original = manager._get_record

        async def paused(approval_id: str) -> ApprovalRecord | None:
            nonlocal reached
            record = await original(approval_id)
            reached += 1
            if reached == 2:
                both_read.set()
            await both_read.wait()
            return record

        manager._get_record = paused  # type: ignore[method-assign]

    pause_after_read(first)
    pause_after_read(second)
    allow = ApprovalResolution(
        approval.approval_id,
        ApprovalState.APPROVED,
        ApprovalScope.ONCE,
        NOW,
        "principal_a",
        False,
    )
    deny = ApprovalResolution(
        approval.approval_id,
        ApprovalState.DENIED,
        ApprovalScope.ONCE,
        NOW,
        "principal_b",
        False,
    )
    outcomes = await asyncio.gather(first.resolve(allow), second.resolve(deny), return_exceptions=True)
    winners = [outcome for outcome in outcomes if isinstance(outcome, ApprovalResolution)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, ApprovalConflict)]
    assert len(winners) == len(losers) == 1
    stored = await SqliteUnitOfWorkFactory(database_path).get_entity("approvals", approval.approval_id)
    assert isinstance(stored, ApprovalRecord)
    assert stored.resolution == winners[0]
    assert stored.revision == 2

    # Same decision intent is idempotent even when a transport retry receives a
    # fresh server timestamp; the durable first decision remains authoritative.
    clock.advance(timedelta(seconds=1))
    replay = replace(winners[0], resolved_at=clock.utcnow())
    replay_manager = ApprovalManager(unit_of_work=SqliteUnitOfWorkFactory(database_path), clock=clock)
    assert await replay_manager.resolve(replay) == winners[0]


@pytest.mark.asyncio
async def test_two_workers_concurrently_create_only_one_pending_approval(tmp_path: Path) -> None:
    database_path = tmp_path / "approval-create-race.sqlite"
    first = ApprovalManager(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        clock=ManualClock(NOW),
    )
    second = ApprovalManager(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        clock=ManualClock(NOW),
    )
    left = request(approval_id="apr_" + "a" * 64)
    right = request(approval_id="apr_" + "b" * 64)
    left_record, right_record = await asyncio.gather(
        first._ensure_pending(left),
        second._ensure_pending(right),
    )
    assert left_record.request.approval_id == right_record.request.approval_id
    records = await SqliteUnitOfWorkFactory(database_path).list_entities("approvals")
    assert len(records) == 1
    assert records[0].entity_id == left_record.request.approval_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workspace_id", "ws_other"),
        ("session_id", "ses_other"),
        ("principal_id", "principal_other"),
        ("definition_fingerprint", "sha256:" + "2" * 64),
        ("args_hash", "sha256:" + "c" * 64),
        ("expected_state_hash", "sha256:" + "d" * 64),
        ("agent_name", "other_agent"),
    ],
)
async def test_binding_drift_cancels_original_and_never_creates_a_second_record(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    database_path = tmp_path / f"drift-{field}.sqlite"
    manager = ApprovalManager(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        clock=ManualClock(NOW),
    )
    original = request()
    await manager._ensure_pending(original)
    drifted_binding = drift_binding(original.binding, field, value)
    drifted = request(approval_id="apr_" + "e" * 64, value=drifted_binding)

    with pytest.raises(ApprovalConflict, match="binding drifted"):
        await manager.request(drifted, ManualCancellationToken())
    records = await SqliteUnitOfWorkFactory(database_path).list_entities("approvals")
    assert len(records) == 1
    record = records[0].value
    assert isinstance(record, ApprovalRecord)
    assert record.request == original
    assert record.state is ApprovalState.CANCELLED
    assert record.resolution is not None
    assert record.resolution.resolver_id == "runtime:binding-drift"
    assert (
        await manager.list_pending(
            workspace_id="ws_1",
            session_id="ses_1",
            principal_id="principal_1",
        )
        == ()
    )


@pytest.mark.asyncio
async def test_expired_or_revoked_request_is_returned_fail_closed_without_duplicate(tmp_path: Path) -> None:
    database_path = tmp_path / "expired.sqlite"
    clock = ManualClock(NOW)
    manager = ApprovalManager(unit_of_work=SqliteUnitOfWorkFactory(database_path), clock=clock)
    original = request()
    await manager._ensure_pending(original)
    clock.advance(timedelta(minutes=6))
    restarted = ApprovalManager(unit_of_work=SqliteUnitOfWorkFactory(database_path), clock=clock)
    assert (
        await restarted.list_pending(
            workspace_id="ws_1",
            session_id="ses_1",
            principal_id="principal_1",
        )
        == ()
    )
    retry = request(
        approval_id="apr_" + "d" * 64,
        value=replace(binding(), expires_at=clock.utcnow() + timedelta(minutes=5)),
    )
    expired = (await restarted.request(retry, ManualCancellationToken())).resolution
    assert expired.state is ApprovalState.EXPIRED
    assert expired.approval_id == original.approval_id
    assert len(await SqliteUnitOfWorkFactory(database_path).list_entities("approvals")) == 1

    revoked_path = tmp_path / "revoked.sqlite"
    revoked_manager = ApprovalManager(
        unit_of_work=SqliteUnitOfWorkFactory(revoked_path),
        clock=ManualClock(NOW),
    )
    revoked_request = request()
    await revoked_manager._ensure_pending(revoked_request)
    await revoked_manager.cancel(revoked_request.approval_id, "run revoked")
    result = (
        await revoked_manager.request(
            request(approval_id="apr_" + "c" * 64),
            ManualCancellationToken(),
        )
    ).resolution
    assert result.state is ApprovalState.CANCELLED
    assert len(await SqliteUnitOfWorkFactory(revoked_path).list_entities("approvals")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "invalid", "message"),
    [
        ("argsHash", "sha256:not-canonical", "args_hash must be a canonical"),
        ("expectedStateHash", "unknown-sentinel", "expected_state_hash must be a canonical"),
    ],
)
async def test_corrupt_persisted_binding_digest_fails_closed_on_reconnect(
    tmp_path: Path,
    field: str,
    invalid: str,
    message: str,
) -> None:
    database_path = tmp_path / f"corrupt-{field}.sqlite"
    manager = ApprovalManager(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        clock=ManualClock(NOW),
    )
    approval = request()
    await manager._ensure_pending(approval)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT value_json FROM entities WHERE collection = 'approvals' AND entity_id = ?",
            (approval.approval_id,),
        ).fetchone()
        assert row is not None
        envelope = json.loads(row[0])
        envelope["payload"]["request"]["binding"][field] = invalid
        connection.execute(
            "UPDATE entities SET value_json = ? WHERE collection = 'approvals' AND entity_id = ?",
            (json.dumps(envelope), approval.approval_id),
        )

    restarted = ApprovalManager(
        unit_of_work=SqliteUnitOfWorkFactory(database_path),
        clock=ManualClock(NOW),
    )
    with pytest.raises(ValueError, match=message):
        await restarted.list_pending(
            workspace_id="ws_1",
            session_id="ses_1",
            principal_id="principal_1",
        )


def test_expected_state_absence_is_explicit_and_arbitrary_sentinels_are_rejected() -> None:
    assert replace(binding(), expected_state_hash="absent").expected_state_hash == "absent"
    with pytest.raises(ValueError, match="expected_state_hash"):
        replace(binding(), expected_state_hash="missing")
