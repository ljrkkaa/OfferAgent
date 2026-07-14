from __future__ import annotations

import os
from pathlib import Path

import pytest

from offeragent_harness.runtime.production_worker_composition import (
    WorkerCommandLine,
    _resolve_worker_bootstrap,
)
from offeragent_harness.workspace.identity import WorkspaceRegistry
from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config
from offeragent_harness.workspace.runtime_identity import workspace_database_identity

pytestmark = pytest.mark.skipif(os.name != "nt", reason="production Worker bootstrap requires Windows")


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
    state_parent = local_app_data / "OfferAgent" / "workspaces"
    assert not state_parent.exists()

    bootstrap = _resolve_worker_bootstrap(
        WorkerCommandLine(
            workspace_instance_id=record.workspace_instance_id,
            canonical_root_identity=record.root_identity.identity_hash,
            database_identity=workspace_database_identity(record.workspace_instance_id),
            runtime_version="1.0.0",
        )
    )

    assert bootstrap.canonical_root == vault.resolve(strict=True)
    assert bootstrap.state_directory == state_parent / record.workspace_instance_id
    assert bootstrap.state_directory.is_dir()
