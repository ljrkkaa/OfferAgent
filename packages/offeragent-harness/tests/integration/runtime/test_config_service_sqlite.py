from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.config import ConfigPatch, ConfigScope
from offeragent_harness.runtime.config_service import ConfigRevisionConflict, ConfigService, ConfigUpdateCommand
from offeragent_harness.testing import DeterministicIdGenerator, ManualClock, RecordingEventSink

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def service(database: Path) -> ConfigService:
    return ConfigService(
        unit_of_work=SqliteUnitOfWorkFactory(database, busy_timeout_ms=2_000),
        event_sink=RecordingEventSink(),
        clock=ManualClock(NOW),
        ids=DeterministicIdGenerator(),
    )


@pytest.mark.asyncio
async def test_config_survives_sqlite_reopen_and_keeps_vaults_isolated(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    first = service(database)
    workspace_a = "wsi_12345678-1234-4234-8234-123456789abc"
    workspace_b = "wsi_22345678-1234-4234-8234-123456789abc"
    for owner, model in ((workspace_a, "model-a"), (workspace_b, "model-b")):
        await first.update(
            ConfigUpdateCommand(
                ConfigScope.WORKSPACE,
                owner,
                0,
                f"idem-{model}",
                "user",
                ConfigPatch.model_validate({"model": {"model": model}}),
            )
        )

    reopened = service(database)
    snapshot_a = await reopened.snapshot(
        managed_owner_id="machine",
        profile_id="profile",
        workspace_id=workspace_a,
    )
    snapshot_b = await reopened.snapshot(
        managed_owner_id="machine",
        profile_id="profile",
        workspace_id=workspace_b,
    )
    assert snapshot_a.config.model.model == "model-a"
    assert snapshot_b.config.model.model == "model-b"
    assert snapshot_a.fingerprint != snapshot_b.fingerprint


@pytest.mark.asyncio
async def test_two_sqlite_writers_cannot_both_win_same_expected_revision(tmp_path: Path) -> None:
    database = tmp_path / "race.sqlite"
    first = service(database)
    second = service(database)
    owner = "wsi_12345678-1234-4234-8234-123456789abc"

    def command(name: str) -> ConfigUpdateCommand:
        return ConfigUpdateCommand(
            ConfigScope.WORKSPACE,
            owner,
            0,
            f"idem-{name}",
            "user",
            ConfigPatch.model_validate({"model": {"model": name}}),
        )

    results = await asyncio.gather(
        first.update(command("first")),
        second.update(command("second")),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    assert sum(isinstance(item, ConfigRevisionConflict) for item in results) == 1
