from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from offeragent_harness.permissions import (
    ApprovalBinding,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalScope,
    ApprovalState,
    RiskClass,
)
from offeragent_harness.runtime.approval_manager import (
    ApprovalConflict,
    ApprovalManager,
    ApprovalRecord,
)
from offeragent_harness.testing import InMemoryUnitOfWorkFactory, ManualCancellationToken, ManualClock


def request(now: datetime) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id="approval-1",
        tool_call_id="call-1",
        binding=ApprovalBinding(
            tool_name="vault.transaction",
            tool_version="1",
            definition_fingerprint="sha256:" + "0" * 64,
            args_hash="sha256:" + "a" * 64,
            workspace_id="ws",
            session_id="session",
            principal_id="principal",
            root_run_id="run",
            run_id="run",
            agent_name="root",
            ancestor_run_ids=(),
            expected_state_hash="sha256:" + "b" * 64,
            expires_at=now + timedelta(minutes=5),
        ),
        risk=RiskClass.WRITE,
        summary="write note",
        diff_artifact_ids=("artifact-1",),
    )


def resolution(now: datetime) -> ApprovalResolution:
    return ApprovalResolution(
        approval_id="approval-1",
        state=ApprovalState.APPROVED,
        scope=ApprovalScope.ONCE,
        resolved_at=now,
        resolver_id="user",
        include_descendants=False,
    )


@pytest.mark.asyncio
async def test_pending_approval_survives_manager_restart_and_resolves_once() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = ManualClock(now)
    uow = InMemoryUnitOfWorkFactory()
    first = ApprovalManager(unit_of_work=uow, clock=clock)
    approval = request(now)
    waiting = asyncio.create_task(first.request(approval, ManualCancellationToken()))
    await asyncio.sleep(0)
    assert await first.pending(approval.approval_id) == approval

    restarted = ApprovalManager(unit_of_work=uow, clock=clock)
    decision = resolution(now)
    assert await restarted.resolve(decision) == decision
    # A restarted manager persisted the decision; the original waiter observes it
    # by resolving the same durable record idempotently.
    assert await first.resolve(decision) == decision
    assert (await waiting).resolution == decision

    with pytest.raises(ApprovalConflict):
        await restarted.resolve(
            ApprovalResolution(
                approval_id="approval-1",
                state=ApprovalState.DENIED,
                scope=ApprovalScope.ONCE,
                resolved_at=now,
                resolver_id="other",
                include_descendants=False,
            )
        )


@pytest.mark.asyncio
async def test_late_approval_fails_closed_as_expired() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = ManualClock(now)
    manager = ApprovalManager(unit_of_work=InMemoryUnitOfWorkFactory(), clock=clock)
    approval = request(now)
    waiting = asyncio.create_task(manager.request(approval, ManualCancellationToken()))
    await asyncio.sleep(0)
    clock.advance(timedelta(minutes=6))
    result = (await waiting).resolution
    assert result.state is ApprovalState.EXPIRED


@pytest.mark.asyncio
async def test_cancellation_is_a_durable_resolution() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    manager = ApprovalManager(unit_of_work=InMemoryUnitOfWorkFactory(), clock=ManualClock(now))
    token = ManualCancellationToken()
    waiting = asyncio.create_task(manager.request(request(now), token))
    await asyncio.sleep(0)
    token.cancel(message="run cancelled")
    result = (await waiting).resolution
    assert result.state is ApprovalState.CANCELLED
    assert await manager.pending("approval-1") is None


@pytest.mark.asyncio
async def test_resolution_between_durable_create_and_waiter_registration_is_not_lost() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = ManualClock(now)
    manager = ApprovalManager(unit_of_work=InMemoryUnitOfWorkFactory(), clock=clock)
    approval = request(now)
    pending_created = asyncio.Event()
    allow_request_to_continue = asyncio.Event()
    original = manager._ensure_pending

    async def paused_ensure(value: ApprovalRequest) -> ApprovalRecord:
        record = await original(value)
        pending_created.set()
        await allow_request_to_continue.wait()
        return record

    manager._ensure_pending = paused_ensure  # type: ignore[assignment]
    waiting = asyncio.create_task(manager.request(approval, ManualCancellationToken()))
    await pending_created.wait()
    expected = resolution(now)
    assert await manager.resolve(expected) == expected
    allow_request_to_continue.set()

    assert (await asyncio.wait_for(waiting, timeout=1)).resolution == expected


@pytest.mark.asyncio
async def test_client_timestamp_cannot_create_future_or_already_expired_grant() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    uow = InMemoryUnitOfWorkFactory()
    clock = ManualClock(now)
    manager = ApprovalManager(unit_of_work=uow, clock=clock)
    approval = request(now)
    await manager._ensure_pending(approval)

    with pytest.raises(ApprovalConflict, match="future"):
        await manager.resolve(
            ApprovalResolution(
                approval_id=approval.approval_id,
                state=ApprovalState.APPROVED,
                scope=ApprovalScope.RUN,
                resolved_at=now + timedelta(minutes=1),
                resolver_id="user",
                include_descendants=False,
            )
        )

    clock.advance(timedelta(minutes=5))
    expired = await manager.resolve(
        ApprovalResolution(
            approval_id=approval.approval_id,
            state=ApprovalState.APPROVED,
            scope=ApprovalScope.RUN,
            resolved_at=approval.binding.expires_at,
            resolver_id="user",
            include_descendants=False,
        )
    )
    assert expired.state is ApprovalState.EXPIRED
    assert await uow.list_entities("approval_grants") == ()
