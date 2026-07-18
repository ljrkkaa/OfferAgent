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


def _legacy_config(*, provider: str = "codex") -> dict[str, Any]:
    config = HarnessConfig().model_dump(mode="json")
    config["model"].update(
        {
            "provider": provider,
            "model": "gpt-catalog-candidate",
            "reasoning_effort": "high",
            "credential_handle": "secret:v1:" + "a" * 32,
            "organization_id": "org-sensitive-metadata",
        }
    )
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


def _write_v4(path: Path, config: dict[str, Any]) -> None:
    payload = {
        "version": 4,
        "scope": "workspace",
        "ownerId": OWNER,
        "revision": 8,
        "config": config,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_v5(path: Path, config: dict[str, Any]) -> None:
    payload = {
        "version": 5,
        "scope": "workspace",
        "ownerId": OWNER,
        "revision": 9,
        "config": config,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_v3_file_migrates_full_legacy_config_once_and_keeps_a_backup(tmp_path: Path) -> None:
    path = tmp_path / "workspace-config.json"
    _write_v3(path, _legacy_config())
    store = ConfigFileStore(path, scope=ConfigScope.WORKSPACE, owner_id=OWNER, now=lambda: NOW)

    migrated = store.load()

    assert not migrated.safe_mode
    assert migrated.migrated_from == 3
    assert migrated.backup_path is not None and migrated.backup_path.is_file()
    assert migrated.layer.revision == 7
    projected = migrated.layer.patch.payload()
    assert projected["model"] == {"reasoning_effort": "high", "proxy_url": None}
    assert projected["policy"] == HarnessConfig().model_dump(mode="json")["policy"]
    current = json.loads(path.read_text(encoding="utf-8"))
    assert current["version"] == 6
    assert "update" not in current["config"]
    assert "update_network_enabled" not in current["config"]["network"]

    repeated = store.load()

    assert not repeated.safe_mode
    assert repeated.migrated_from is None
    assert repeated.backup_path is None
    assert repeated.layer == migrated.layer
    assert len(tuple(tmp_path.glob("workspace-config.json.v3.*.bak"))) == 1


def test_v4_file_requires_fresh_reselection_even_for_explicit_codex_subscription(tmp_path: Path) -> None:
    path = tmp_path / "workspace-config.json"
    _write_v4(path, _legacy_config(provider="codex-subscription-experimental"))
    store = ConfigFileStore(path, scope=ConfigScope.WORKSPACE, owner_id=OWNER, now=lambda: NOW)

    loaded = store.load()

    assert not loaded.safe_mode
    assert loaded.migrated_from == 4
    assert loaded.layer.patch.payload()["model"] == {
        "reasoning_effort": "high",
        "proxy_url": None,
    }
    current = path.read_text(encoding="utf-8")
    assert '"version": 6' in current
    persisted_model = json.loads(current)["config"]["model"]
    for retired in (
        "provider",
        "wire_api",
        "credential_handle",
        "organization_id",
    ):
        assert retired not in persisted_model
    for sensitive in ("org-sensitive-metadata", "secret:v1:"):
        assert sensitive not in current


def test_v4_file_never_echoes_a_secret_shaped_legacy_model_candidate(tmp_path: Path) -> None:
    path = tmp_path / "workspace-config.json"
    config = _legacy_config(provider="codex-subscription-experimental")
    config["model"]["model"] = "token-must-not-survive"
    _write_v4(path, config)

    loaded = ConfigFileStore(path, scope=ConfigScope.WORKSPACE, owner_id=OWNER, now=lambda: NOW).load()

    assert "token-must-not-survive" not in repr(loaded) + path.read_text(encoding="utf-8")


@pytest.mark.parametrize("provider", ("codex", "deepseek", "openai", "local", None))
def test_v4_file_clears_model_candidate_from_every_other_or_missing_provider(
    tmp_path: Path,
    provider: str | None,
) -> None:
    path = tmp_path / "workspace-config.json"
    config = _legacy_config(provider=provider or "codex")
    if provider is None:
        config["model"].pop("provider")
    _write_v4(path, config)

    loaded = ConfigFileStore(path, scope=ConfigScope.WORKSPACE, owner_id=OWNER, now=lambda: NOW).load()

    assert not loaded.safe_mode
    assert "model" not in loaded.layer.patch.payload()["model"]


def test_v5_file_contracts_loopback_control_fields_with_value_free_report(tmp_path: Path) -> None:
    path = tmp_path / "workspace-config.json"
    config = HarnessConfig().model_dump(mode="json")
    config["ui"].update({"loopback_web_enabled": True, "persistent_web_lease": True})
    _write_v5(path, config)

    loaded = ConfigFileStore(path, scope=ConfigScope.WORKSPACE, owner_id=OWNER, now=lambda: NOW).load()

    assert loaded.migrated_from == 5
    assert loaded.retired_fields == ("ui.loopback_web_enabled", "ui.persistent_web_lease")
    assert loaded.retired_provider_ids == ()
    current = json.loads(path.read_text(encoding="utf-8"))
    assert current["version"] == 6
    assert set(current["config"]["ui"]) == {"locale", "show_diagnostics"}


def test_v6_file_rejects_retired_model_decision_fields(tmp_path: Path) -> None:
    path = tmp_path / "workspace-config.json"
    payload = {
        "version": 6,
        "scope": "workspace",
        "ownerId": OWNER,
        "revision": 1,
        "config": {"model": {"provider": "codex-subscription-experimental", "model": "gpt-candidate"}},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = ConfigFileStore(path, scope=ConfigScope.WORKSPACE, owner_id=OWNER, now=lambda: NOW).load()

    assert loaded.safe_mode
    assert loaded.layer.patch.payload() == {}
    assert loaded.backup_path is not None


@pytest.mark.parametrize(
    "corrupt",
    (
        lambda value: value.__setitem__("unknown", True),
        lambda value: value["update"].__setitem__("unknown", True),
        lambda value: value["network"].__setitem__("unknown", True),
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
