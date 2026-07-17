from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from offeragent_harness.workspace.identity import WorkspaceRegistry
from offeragent_harness.workspace.supervision import WorkspaceRegistrationBoundary


def test_registration_boundary_discards_vault_path_before_process_isolation(tmp_path: Path) -> None:
    vault = tmp_path / "真实 Vault"
    vault.mkdir()
    registry = WorkspaceRegistry(
        tmp_path / "state" / "registry.json",
        now=lambda: datetime(2026, 7, 13, tzinfo=timezone.utc),
        new_uuid=lambda: uuid.UUID("12345678-1234-4234-8234-123456789abc"),
    )
    boundary = WorkspaceRegistrationBoundary(registry)

    identity = boundary.register(vault, database_identity="sha256:" + "d" * 64)

    assert identity.workspace_instance_id == "wsi_12345678-1234-4234-8234-123456789abc"
    assert identity.canonical_root_identity.startswith("sha256:")
    assert str(vault.resolve()) not in repr(identity)
    assert registry.list()[0].root_identity.canonical_path == os.path.normcase(os.path.normpath(str(vault.resolve())))
