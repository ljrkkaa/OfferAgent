from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.config import ConfigScope, HarnessConfig
from offeragent_harness.config.files import ConfigFileStore

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
OWNER = "ws_legacy"


def _legacy_config() -> dict[str, Any]:
    config = HarnessConfig().model_dump(mode="json")
    config["network"]["update_network_enabled"] = False
    config["update"] = {
        "automatic_check": False,
        "automatic_install": False,
        "channel": "disabled",
    }
    return config


def _write_v3(path: Path, config: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "scope": "workspace",
                "ownerId": OWNER,
                "revision": 7,
                "config": config,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_v3_file_migrates_full_legacy_config_once_and_keeps_a_backup(tmp_path: Path) -> None:
    path = tmp_path / "workspace-config.json"
    _write_v3(path, _legacy_config())
    store = ConfigFileStore(path, scope=ConfigScope.WORKSPACE, owner_id=OWNER, now=lambda: NOW)

    migrated = store.load()

    assert not migrated.safe_mode
    assert migrated.migrated_from == 3
    assert migrated.backup_path is not None and migrated.backup_path.is_file()
    assert migrated.layer.revision == 7
    assert migrated.layer.patch.payload() == HarnessConfig().model_dump(mode="json")
    current = json.loads(path.read_text(encoding="utf-8"))
    assert current["version"] == 5
    assert "update" not in current["config"]
    assert "update_network_enabled" not in current["config"]["network"]

    repeated = store.load()

    assert not repeated.safe_mode
    assert repeated.migrated_from is None
    assert repeated.backup_path is None
    assert repeated.layer == migrated.layer
    assert len(tuple(tmp_path.glob("workspace-config.json.v3.*.bak"))) == 1


@pytest.mark.parametrize(
    "corrupt",
    (
        lambda value: value.__setitem__("unknown", True),
        lambda value: value["update"].__setitem__("unknown", True),
        lambda value: value["network"].__setitem__("unknown", True),
        lambda value: value["ui"].__setitem__("unknown", True),
    ),
)
def test_v3_file_migration_rejects_every_non_retired_unknown_field(
    tmp_path: Path,
    corrupt: Callable[[dict[str, Any]], None],
) -> None:
    path = tmp_path / "workspace-config.json"
    config = deepcopy(_legacy_config())
    corrupt(config)
    _write_v3(path, config)
    store = ConfigFileStore(path, scope=ConfigScope.WORKSPACE, owner_id=OWNER, now=lambda: NOW)

    loaded = store.load()

    assert loaded.safe_mode
    assert loaded.migrated_from is None
    assert loaded.layer.patch.payload() == {}
    assert loaded.backup_path is not None
    assert loaded.backup_path.name.startswith("workspace-config.json.corrupt.")
    assert not path.exists()


@pytest.mark.parametrize(
    ("loopback_enabled", "persistent_lease"),
    ((False, False), (True, True), (None, None)),
)
def test_v4_file_removes_only_valid_retired_web_settings(
    tmp_path: Path,
    loopback_enabled: bool | None,
    persistent_lease: bool | None,
) -> None:
    path = tmp_path / "workspace-config.json"
    config = HarnessConfig().model_dump(mode="json")
    config["ui"]["loopback_web_enabled"] = loopback_enabled
    config["ui"]["persistent_web_lease"] = persistent_lease
    path.write_text(
        json.dumps(
            {
                "version": 4,
                "scope": "workspace",
                "ownerId": OWNER,
                "revision": 8,
                "config": config,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    migrated = ConfigFileStore(path, scope=ConfigScope.WORKSPACE, owner_id=OWNER, now=lambda: NOW).load()

    assert not migrated.safe_mode
    assert migrated.migrated_from == 4
    assert migrated.backup_path is not None and migrated.backup_path.name.startswith("workspace-config.json.v4.")
    assert migrated.layer.revision == 8
    current = json.loads(path.read_text(encoding="utf-8"))
    assert current["version"] == 5
    assert current["config"]["ui"] == HarnessConfig().model_dump(mode="json")["ui"]


@pytest.mark.parametrize("invalid", (0, "false", [], {}))
def test_v4_file_rejects_invalid_retired_web_setting(tmp_path: Path, invalid: object) -> None:
    path = tmp_path / "workspace-config.json"
    config = HarnessConfig().model_dump(mode="json")
    config["ui"]["loopback_web_enabled"] = invalid
    path.write_text(
        json.dumps(
            {
                "version": 4,
                "scope": "workspace",
                "ownerId": OWNER,
                "revision": 8,
                "config": config,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    loaded = ConfigFileStore(path, scope=ConfigScope.WORKSPACE, owner_id=OWNER, now=lambda: NOW).load()

    assert loaded.safe_mode
    assert loaded.migrated_from is None
    assert loaded.layer.patch.payload() == {}
    assert loaded.backup_path is not None and loaded.backup_path.name.startswith("workspace-config.json.corrupt.")
