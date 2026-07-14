from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from offeragent_harness.ports import ApplicationCommandContext
from offeragent_harness.protocol.common import ApprovalDecision, ApprovalScope
from offeragent_harness.protocol.messages import (
    ApprovalResolveParams,
    HeadlessVaultWriteActivateParams,
    HeadlessVaultWriteRequestParams,
    HeadlessVaultWriteRevokeParams,
    HeadlessVaultWriteStatusResult,
)
from offeragent_harness.runtime.headless_vault_write import (
    HeadlessVaultWriteAuthority,
    HeadlessVaultWriteError,
)
from offeragent_harness.testing import InMemoryUnitOfWorkFactory, ManualCancellationToken, ManualClock
from offeragent_harness.tools import canonical_json_sha256

NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
ROOT = "sha256:" + "1" * 64
DATABASE = "sha256:" + "2" * 64


def required(value: str | None) -> str:
    assert value is not None
    return value


class Baseline:
    def __init__(self) -> None:
        self.root = ROOT
        self.database = DATABASE


def web(client_id: str = "web-headless-test") -> ApplicationCommandContext:
    return ApplicationCommandContext(transport="loopback-http", client_id=client_id, peer="127.0.0.1")


def request_params(request_id: str = "req_headless_1") -> HeadlessVaultWriteRequestParams:
    return HeadlessVaultWriteRequestParams(
        client_request_id=request_id,
        confirmation="obsidian_closed_disk_authoritative",
        ttl_seconds=300,
        expected_baseline_fingerprint=canonical_json_sha256({"rootIdentity": ROOT, "databaseIdentity": DATABASE}),
    )


def authority(
    *,
    store: InMemoryUnitOfWorkFactory | None = None,
    clock: ManualClock | None = None,
    baseline: Baseline | None = None,
    pipe_count: list[int] | None = None,
) -> tuple[HeadlessVaultWriteAuthority, InMemoryUnitOfWorkFactory, ManualClock, Baseline, list[int]]:
    store = store or InMemoryUnitOfWorkFactory()
    clock = clock or ManualClock(NOW)
    baseline = baseline or Baseline()
    pipe_count = pipe_count or [0]
    value = HeadlessVaultWriteAuthority(
        workspace_id="ws_headless",
        workspace_instance_id="wsi_headless",
        root_identity=ROOT,
        database_identity=DATABASE,
        unit_of_work=store,
        clock=clock,
        identity_probe=lambda: (baseline.root, baseline.database),
        pipe_connection_count=lambda: pipe_count[0],
    )
    return value, store, clock, baseline, pipe_count


async def approved_authority() -> tuple[
    HeadlessVaultWriteAuthority,
    InMemoryUnitOfWorkFactory,
    ManualClock,
    Baseline,
    list[int],
    HeadlessVaultWriteStatusResult,
]:
    value, store, clock, baseline, pipes = authority()
    await value.recover_after_restart()
    pending = await value.request(request_params(), web(), ManualCancellationToken())
    resolved = await value.try_resolve(
        ApprovalResolveParams(
            approval_id=required(pending.approval_id),
            decision=ApprovalDecision.ALLOW_ONCE,
            scope=ApprovalScope.ONCE,
            expected_args_hash=required(pending.args_hash),
        ),
        web(),
    )
    assert resolved is not None and resolved.operation_id == pending.operation_id and resolved.run_id is None
    return value, store, clock, baseline, pipes, pending


@pytest.mark.asyncio
async def test_request_is_durable_idempotent_and_requires_exact_reliable_baseline() -> None:
    value, _, _, baseline, _ = authority()
    await value.recover_after_restart()
    initial = await value.status(web(), ManualCancellationToken())
    assert initial.state == "read_only"
    assert initial.can_request is True

    pending = await value.request(request_params(), web(), ManualCancellationToken())
    replay = await value.request(request_params(), web(), ManualCancellationToken())
    assert replay == pending
    assert pending.state == "pending_approval"
    assert pending.approval_id and pending.operation_id and pending.args_hash

    changed = request_params().model_copy(update={"ttl_seconds": 600})
    with pytest.raises(HeadlessVaultWriteError, match="clientRequestId"):
        await value.request(changed, web(), ManualCancellationToken())

    baseline.root = "sha256:" + "3" * 64
    changed_identity = await value.status(web(), ManualCancellationToken())
    assert changed_identity.state == "baseline_unreliable"
    assert changed_identity.reason_code == "workspace_identity_changed"
    assert changed_identity.baseline_reliable is False
    assert changed_identity.can_request is False
    with pytest.raises(HeadlessVaultWriteError, match="identity"):
        await value.request(request_params("req_headless_3"), web(), ManualCancellationToken())


@pytest.mark.asyncio
async def test_status_never_offers_browser_activation_to_a_pipe_context() -> None:
    value, _, _, _, _ = authority()
    await value.recover_after_restart()
    status = await value.status(
        ApplicationCommandContext(
            transport="windows-named-pipe",
            client_id="pipe-headless-status",
            peer="current-windows-sid",
        ),
        ManualCancellationToken(),
    )
    assert status.state == "read_only"
    assert status.can_request is False
    assert status.can_activate is False


@pytest.mark.asyncio
async def test_administrative_approval_is_not_a_fake_run_and_reusable_scope_is_denied() -> None:
    value, _, _, _, _ = authority()
    await value.recover_after_restart()
    pending = await value.request(request_params(), web(), ManualCancellationToken())
    with pytest.raises(PermissionError, match="one-time"):
        await value.try_resolve(
            ApprovalResolveParams(
                approval_id=required(pending.approval_id),
                decision=ApprovalDecision.ALLOW_SESSION,
                scope=ApprovalScope.SESSION,
                expected_args_hash=required(pending.args_hash),
            ),
            web(),
        )
    denied = await value.try_resolve(
        ApprovalResolveParams(
            approval_id=required(pending.approval_id),
            decision=ApprovalDecision.DENY,
            scope=ApprovalScope.ONCE,
            expected_args_hash=required(pending.args_hash),
        ),
        web(),
    )
    assert denied is not None
    assert denied.status == "denied"
    assert denied.run_id is None
    assert denied.operation_id == pending.operation_id


@pytest.mark.asyncio
async def test_activate_claim_revoke_and_ack_replays_are_exactly_bound() -> None:
    value, _, _, _, _, pending = await approved_authority()
    approved = await value.status(web(), ManualCancellationToken())
    assert approved.state == "approved"
    activated = await value.activate(
        HeadlessVaultWriteActivateParams(
            client_request_id="req_activate_1",
            approval_id=required(approved.approval_id),
            expected_args_hash=required(approved.args_hash),
            expected_revision=approved.revision,
        ),
        web(),
        ManualCancellationToken(),
    )
    replay = await value.activate(
        HeadlessVaultWriteActivateParams(
            client_request_id="req_activate_1",
            approval_id=required(activated.approval_id),
            expected_args_hash=required(activated.args_hash),
            expected_revision=pending.revision,
        ),
        web(),
        ManualCancellationToken(),
    )
    assert replay == activated

    claim = canonical_json_sha256({"turn": "turn_exact", "idempotencyKey": "same"})
    grant_id = await value.claim_for_turn(web().client_id, claim)
    assert grant_id is not None
    assert await value.claim_for_turn(web().client_id, claim) == grant_id
    assert await value.claim_for_turn(web().client_id, canonical_json_sha256({"turn": "other"})) is None
    await value.validate_grant(grant_id)

    claimed = await value.status(web(), ManualCancellationToken())
    revoked = await value.revoke(
        HeadlessVaultWriteRevokeParams(
            client_request_id="req_revoke_1",
            approval_id=required(claimed.approval_id),
            expected_revision=claimed.revision,
            reason="用户撤销 headless 写入",
        ),
        web(),
        ManualCancellationToken(),
    )
    replay_revoke = await value.revoke(
        HeadlessVaultWriteRevokeParams(
            client_request_id="req_revoke_1",
            approval_id=required(revoked.approval_id),
            expected_revision=claimed.revision,
            reason="用户撤销 headless 写入",
        ),
        web(),
        ManualCancellationToken(),
    )
    assert replay_revoke == revoked
    with pytest.raises(HeadlessVaultWriteError):
        await value.validate_grant(grant_id)


@pytest.mark.asyncio
async def test_expiry_baseline_drift_and_other_browser_fail_closed() -> None:
    value, _, clock, baseline, _, _ = await approved_authority()
    approved = await value.status(web(), ManualCancellationToken())
    with pytest.raises(PermissionError, match="another authenticated browser"):
        await value.try_resolve(
            ApprovalResolveParams(
                approval_id=required(approved.approval_id),
                decision=ApprovalDecision.ALLOW_ONCE,
                scope=ApprovalScope.ONCE,
                expected_args_hash=required(approved.args_hash),
            ),
            web("web-other"),
        )
    baseline.database = "sha256:" + "4" * 64
    with pytest.raises(HeadlessVaultWriteError, match="identity changed"):
        await value.activate(
            HeadlessVaultWriteActivateParams(
                client_request_id="req_activate_drift",
                approval_id=required(approved.approval_id),
                expected_args_hash=required(approved.args_hash),
                expected_revision=approved.revision,
            ),
            web(),
            ManualCancellationToken(),
        )

    baseline.database = DATABASE
    clock.advance(timedelta(minutes=6))
    expired = await value.status(web(), ManualCancellationToken())
    assert expired.state == "expired"
    assert expired.can_request is True


@pytest.mark.asyncio
async def test_pipe_registration_waits_for_local_execution_then_revokes() -> None:
    value, _, _, _, pipes, _ = await approved_authority()
    approved = await value.status(web(), ManualCancellationToken())
    await value.activate(
        HeadlessVaultWriteActivateParams(
            client_request_id="req_activate_race",
            approval_id=required(approved.approval_id),
            expected_args_hash=required(approved.args_hash),
            expected_revision=approved.revision,
        ),
        web(),
        ManualCancellationToken(),
    )
    grant_id = await value.claim_for_turn(web().client_id, canonical_json_sha256({"turn": "race"}))
    assert grant_id is not None

    entered = asyncio.Event()
    release = asyncio.Event()

    async def execute() -> None:
        async with value.hold_execution(grant_id):
            entered.set()
            await release.wait()

    async def connect_pipe() -> None:
        async with value.pipe_registration():
            pipes[0] = 1

    execution = asyncio.create_task(execute())
    await entered.wait()
    registration = asyncio.create_task(connect_pipe())
    await asyncio.sleep(0)
    assert not registration.done()
    release.set()
    await execution
    await registration
    with pytest.raises(HeadlessVaultWriteError, match="Pipe"):
        await value.validate_grant(grant_id)


@pytest.mark.asyncio
async def test_restart_invalidates_process_local_browser_grant() -> None:
    first, store, clock, baseline, pipes, _ = await approved_authority()
    approved = await first.status(web(), ManualCancellationToken())
    await first.activate(
        HeadlessVaultWriteActivateParams(
            client_request_id="req_activate_restart",
            approval_id=required(approved.approval_id),
            expected_args_hash=required(approved.args_hash),
            expected_revision=approved.revision,
        ),
        web(),
        ManualCancellationToken(),
    )
    restarted, _, _, _, _ = authority(store=store, clock=clock, baseline=baseline, pipe_count=pipes)
    await restarted.recover_after_restart()
    status = await restarted.status(web("web-after-restart"), ManualCancellationToken())
    assert status.state == "read_only"
    assert status.can_request is True
