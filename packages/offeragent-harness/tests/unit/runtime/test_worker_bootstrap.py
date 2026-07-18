from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from offeragent_harness.runtime.production_worker_composition import (
    ProductionWorkerError,
    WorkerCommandLine,
    _resolve_worker_bootstrap,
    parse_worker_arguments,
)
from offeragent_harness.workspace.identity import WorkspaceRegistry
from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config
from offeragent_harness.workspace.runtime_identity import workspace_database_identity

pytestmark = pytest.mark.skipif(os.name != "nt", reason="production Worker bootstrap requires Windows")


def test_direct_stdio_arguments_preserve_the_plugin_journal_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_app_data = tmp_path / "LocalAppData"
    local_app_data.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    vault = tmp_path / "Vault"
    vault.mkdir()
    ensure_portable_workspace_config(vault)
    journal = vault / ".obsidian" / "offeragent" / "vault-change-journal"
    journal.mkdir(parents=True)

    command = parse_worker_arguments(
        [
            "stdio",
            "--vault-root",
            str(vault),
            "--runtime-version",
            "1.0.0",
            "--plugin-journal-directory",
            str(journal),
            "--plugin-recovery-token",
            "a" * 64,
        ]
    )

    assert command.plugin_journal_directory == journal
    assert command.plugin_recovery_token == "a" * 64


def test_worker_bootstrap_rejects_a_junction_in_original_plugin_journal_ancestry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_app_data = tmp_path / "LocalAppData"
    local_app_data.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    vault = tmp_path / "Vault"
    vault.mkdir()
    ensure_portable_workspace_config(vault)
    actual = vault / ".shadow" / "offeragent" / "vault-change-journal"
    actual.mkdir(parents=True)
    junction = vault / ".linked"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(vault / ".shadow")],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"directory junctions are unavailable: {created.stderr or created.stdout}")
    try:
        command = parse_worker_arguments(
            [
                "stdio",
                "--vault-root",
                str(vault),
                "--runtime-version",
                "1.0.0",
                "--plugin-journal-directory",
                str(junction / "offeragent" / "vault-change-journal"),
                "--plugin-recovery-token",
                "a" * 64,
            ]
        )

        with pytest.raises(ProductionWorkerError, match="plugin journal directory is unsafe"):
            _resolve_worker_bootstrap(command)
    finally:
        junction.rmdir()


def test_worker_bootstrap_creates_missing_state_parent_under_current_user_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_app_data = tmp_path / "LocalAppData"
    local_app_data.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    vault = tmp_path / "Vault"
    vault.mkdir()
    portable = ensure_portable_workspace_config(vault)
    registry = WorkspaceRegistry(local_app_data / "OfferAgent" / "workspace-registry.json")
    record = registry.register(vault, portable_workspace_id=portable.portable_workspace_id)
    (vault / ".obsidian" / "offeragent" / "vault-change-journal").mkdir(parents=True)
    state_parent = local_app_data / "OfferAgent" / "workspaces"
    assert not state_parent.exists()

    bootstrap = _resolve_worker_bootstrap(
        WorkerCommandLine(
            workspace_instance_id=record.workspace_instance_id,
            canonical_root_identity=record.root_identity.identity_hash,
            database_identity=workspace_database_identity(record.workspace_instance_id),
            runtime_version="1.0.0",
            plugin_journal_directory=vault / ".obsidian" / "offeragent" / "vault-change-journal",
            plugin_recovery_token="a" * 64,
        )
    )

    assert bootstrap.canonical_root == vault.resolve(strict=True)
    assert bootstrap.state_directory == state_parent / record.workspace_instance_id
    assert bootstrap.state_directory.is_dir()
    assert bootstrap.plugin_journal_directory == vault / ".obsidian" / "offeragent" / "vault-change-journal"
    assert bootstrap.plugin_recovery_token == "a" * 64


def test_worker_bootstrap_rejects_plugin_journal_outside_selected_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_app_data = tmp_path / "LocalAppData"
    local_app_data.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    vault = tmp_path / "Vault"
    vault.mkdir()
    portable = ensure_portable_workspace_config(vault)
    registry = WorkspaceRegistry(local_app_data / "OfferAgent" / "workspace-registry.json")
    record = registry.register(vault, portable_workspace_id=portable.portable_workspace_id)

    with pytest.raises(ProductionWorkerError, match="plugin journal"):
        _resolve_worker_bootstrap(
            WorkerCommandLine(
                workspace_instance_id=record.workspace_instance_id,
                canonical_root_identity=record.root_identity.identity_hash,
                database_identity=workspace_database_identity(record.workspace_instance_id),
                runtime_version="1.0.0",
                plugin_journal_directory=tmp_path / "other" / "vault-change-journal",
                plugin_recovery_token="a" * 64,
            )
        )
