from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.config import ConfigPatch, ConfigScope, HarnessConfig
from offeragent_harness.runtime.config_service import (
    ConfigCorrupt,
    ConfigRevisionConflict,
    ConfigService,
    ConfigUpdateCommand,
)
from offeragent_harness.testing import DeterministicIdGenerator, ManualClock, RecordingEventSink

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def _legacy_v2_layer(
    owner: str,
    *,
    provider: str | None = "codex-subscription-experimental",
) -> dict[str, Any]:
    config = HarnessConfig().model_dump(mode="json")
    config["model"]["model"] = "gpt-catalog-candidate"
    config["model"]["credential_handle"] = "secret:v1:" + "b" * 32
    if provider is None:
        config["model"].pop("provider", None)
    else:
        config["model"]["provider"] = provider
    config["network"]["update_network_enabled"] = False
    config["update"] = {
        "automatic_check": False,
        "automatic_install": False,
        "channel": "disabled",
    }
    return {
        "schemaVersion": 2,
        "scope": "workspace",
        "ownerId": owner,
        "revision": 1,
        "eventSequence": 1,
        "config": config,
        "updatedAt": NOW.isoformat(),
    }


async def _store_layer(database: Path, owner: str, layer: dict[str, Any]) -> SqliteUnitOfWorkFactory:
    factory = SqliteUnitOfWorkFactory(database, busy_timeout_ms=2_000)
    await factory.entity_store.put(
        "config_layers",
        f"workspace:{owner}",
        layer,
        expected_revision=0,
    )
    return factory


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
                ConfigPatch.model_validate({"model": {"model": model, "account_binding": "sha256:" + "a" * 64}}),
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
    stored_a = await SqliteUnitOfWorkFactory(database).get_entity("config_layers", f"workspace:{workspace_a}")
    assert snapshot_a.config.model.model == "model-a"
    assert snapshot_b.config.model.model == "model-b"
    assert snapshot_a.fingerprint != snapshot_b.fingerprint
    assert isinstance(stored_a, dict)
    assert stored_a["schemaVersion"] == 5


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
            ConfigPatch.model_validate({"model": {"model": name, "account_binding": "sha256:" + "a" * 64}}),
        )

    results = await asyncio.gather(
        first.update(command("first")),
        second.update(command("second")),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    assert sum(isinstance(item, ConfigRevisionConflict) for item in results) == 1


@pytest.mark.asyncio
async def test_legacy_v2_sqlite_layer_strips_full_retired_config_on_repeated_reads(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite"
    owner = "wsi_32345678-1234-4234-8234-123456789abc"
    legacy = _legacy_v2_layer(owner)
    factory = await _store_layer(database, owner, legacy)
    reopened = service(database)

    first = await reopened.layer(ConfigScope.WORKSPACE, owner)
    second = await reopened.layer(ConfigScope.WORKSPACE, owner)
    snapshot = await reopened.snapshot(
        managed_owner_id="machine",
        profile_id="profile",
        workspace_id=owner,
    )

    assert first == second
    assert first.revision == 1 and first.event_sequence == 1
    assert first.patch.payload()["model"] == {
        "reasoning_effort": "medium",
        "proxy_url": None,
    }
    assert snapshot.config.model.model == ""
    assert await factory.get_entity("config_layers", f"workspace:{owner}") == legacy


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("codex", "deepseek", "openai", "local", None))
async def test_legacy_sqlite_layer_clears_model_for_every_other_or_missing_provider(
    tmp_path: Path,
    provider: str | None,
) -> None:
    database = tmp_path / "legacy-provider.sqlite"
    owner = "wsi_52345678-1234-4234-8234-123456789abc"
    await _store_layer(database, owner, _legacy_v2_layer(owner, provider=provider))

    layer = await service(database).layer(ConfigScope.WORKSPACE, owner)

    assert "model" not in layer.patch.payload()["model"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value["config"].__setitem__("unknown", True),
        lambda value: value["config"]["update"].__setitem__("unknown", True),
        lambda value: value["config"]["network"].__setitem__("unknown", True),
        lambda value: value.__setitem__("unknown", True),
        lambda value: value.__setitem__("schemaVersion", 4),
    ),
)
async def test_sqlite_legacy_migration_rejects_unknown_or_current_schema_retired_fields(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    database = tmp_path / "corrupt.sqlite"
    owner = "wsi_42345678-1234-4234-8234-123456789abc"
    legacy = deepcopy(_legacy_v2_layer(owner))
    mutate(legacy)
    await _store_layer(database, owner, legacy)

    with pytest.raises(ConfigCorrupt):
        await service(database).layer(ConfigScope.WORKSPACE, owner)


@pytest.mark.asyncio
async def test_current_sqlite_layer_rejects_retired_model_decision_fields_without_echoing_values(
    tmp_path: Path,
) -> None:
    database = tmp_path / "current-corrupt.sqlite"
    owner = "wsi_62345678-1234-4234-8234-123456789abc"
    raw = _legacy_v2_layer(owner)
    raw["schemaVersion"] = 4
    raw["config"] = {
        "model": {
            "model": "gpt-candidate",
            "credential_handle": "secret:v1:" + "c" * 32,
        }
    }
    await _store_layer(database, owner, raw)

    with pytest.raises(ConfigCorrupt) as captured:
        await service(database).layer(ConfigScope.WORKSPACE, owner)

    assert "secret:v1:" not in str(captured.value)
