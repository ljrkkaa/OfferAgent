from __future__ import annotations

import sqlite3
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
    grant_from_resolution,
)

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_approval_grant_codec_survives_real_sqlite_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "grant.sqlite"
    binding = ApprovalBinding(
        tool_name="vault.transaction",
        tool_version="1",
        definition_fingerprint="sha256:" + "0" * 64,
        args_hash="sha256:" + "a" * 64,
        workspace_id="ws_1",
        session_id="session_1",
        principal_id="principal_1",
        root_run_id="run_1",
        run_id="run_1",
        agent_name="root",
        ancestor_run_ids=(),
        expected_state_hash="sha256:" + "b" * 64,
        expires_at=NOW + timedelta(minutes=5),
    )
    request = ApprovalRequest(
        approval_id="approval_1",
        tool_call_id="call_1",
        binding=binding,
        risk=RiskClass.WRITE,
        summary="write exact resource",
        diff_artifact_ids=("art_diff",),
    )
    resolution = ApprovalResolution(
        approval_id=request.approval_id,
        state=ApprovalState.APPROVED,
        scope=ApprovalScope.PERSISTENT,
        resolved_at=NOW,
        resolver_id="user_1",
        include_descendants=False,
    )
    grant = grant_from_resolution(request, resolution, expires_at=NOW + timedelta(days=30))

    factory = SqliteUnitOfWorkFactory(database_path)
    async with factory.begin() as uow:
        await uow.entities.put("approval_grants", grant.grant_id, grant, expected_revision=0)
        await uow.commit()

    reopened = SqliteUnitOfWorkFactory(database_path)
    assert await reopened.get_entity("approval_grants", grant.grant_id) == grant
    with sqlite3.connect(database_path) as connection:
        payload = connection.execute(
            "SELECT value_json FROM entities WHERE collection = ? AND entity_id = ?",
            ("approval_grants", grant.grant_id),
        ).fetchone()[0]
    assert '"codec":"offeragent.approval_grant"' in payload
    assert '"schemaVersion":2' in payload
