from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from offeragent_harness.workspace.portable_config import (
    PortableWorkspaceConfigError,
    ensure_portable_workspace_config,
    read_portable_workspace_config,
)
from offeragent_harness.workspace.runtime_identity import workspace_database_identity


def test_first_attach_atomically_creates_canonical_portable_workspace_id(tmp_path: Path) -> None:
    expected_uuid = uuid.UUID("018f0d5e-4b63-7d42-8e5a-010203040506")

    created = ensure_portable_workspace_config(tmp_path, new_uuid=lambda: expected_uuid)

    assert created.portable_workspace_id == f"ws_{expected_uuid}"
    assert read_portable_workspace_config(tmp_path) == created
    assert (tmp_path / ".offeragent" / "workspace.json").read_bytes() == (
        b'{"portableWorkspaceId":"ws_018f0d5e-4b63-7d42-8e5a-010203040506","schemaVersion":1}\n'
    )


def test_existing_identity_is_reused_and_not_replaced(tmp_path: Path) -> None:
    first = ensure_portable_workspace_config(tmp_path, new_uuid=lambda: uuid.UUID(int=1))
    second = ensure_portable_workspace_config(tmp_path, new_uuid=lambda: uuid.UUID(int=2))

    assert second == first


def test_noncanonical_or_unknown_workspace_fields_fail_closed(tmp_path: Path) -> None:
    directory = tmp_path / ".offeragent"
    directory.mkdir()
    target = directory / "workspace.json"
    workspace_id = "ws_018f0d5e-4b63-7d42-8e5a-010203040506"
    target.write_text(json.dumps({"schemaVersion": 1, "portableWorkspaceId": workspace_id}), encoding="utf-8")

    with pytest.raises(PortableWorkspaceConfigError, match="canonical"):
        read_portable_workspace_config(tmp_path)

    target.write_text(
        '{"portableWorkspaceId":"ws_018f0d5e-4b63-7d42-8e5a-010203040506","schemaVersion":1,"unknown":true}\n',
        encoding="utf-8",
    )
    with pytest.raises(PortableWorkspaceConfigError, match="fields"):
        read_portable_workspace_config(tmp_path)


def test_workspace_config_symlink_is_rejected(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink is unavailable")
    directory = tmp_path / ".offeragent"
    directory.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"portableWorkspaceId":"ws_valid","schemaVersion":1}\n', encoding="utf-8")
    try:
        (directory / "workspace.json").symlink_to(outside)
    except OSError:
        pytest.skip("test account cannot create symlinks")

    with pytest.raises(PortableWorkspaceConfigError, match="regular"):
        read_portable_workspace_config(tmp_path)


def test_database_identity_is_deterministic_and_rejects_non_instance_ids() -> None:
    workspace = "wsi_123e4567-e89b-42d3-a456-426614174000"
    assert workspace_database_identity(workspace).startswith("sha256:")
    assert workspace_database_identity(workspace) == workspace_database_identity(workspace)
    with pytest.raises(ValueError, match="instance"):
        workspace_database_identity("ws_client_supplied")
