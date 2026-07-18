from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from scripts.qualify_live_built_windows_product import (
    LiveBuiltProductQualificationError,
    _canonical_json,
    _remove_owned_root,
)


def test_owned_root_cleanup_clears_read_only_git_objects(tmp_path: Path) -> None:
    root = tmp_path / "qualification"
    git_object = root / "Vault" / ".git" / "objects" / "aa" / "object"
    git_object.parent.mkdir(parents=True)
    git_object.write_bytes(b"sealed git object")
    os.chmod(git_object, stat.S_IREAD)
    marker_payload = _canonical_json({"schemaVersion": 1, "token": "a" * 64})
    (root / ".offeragent-qualification-owner.json").write_bytes(marker_payload)

    _remove_owned_root(root, marker_payload)

    assert not root.exists()


def test_owned_root_cleanup_refuses_a_changed_marker(tmp_path: Path) -> None:
    root = tmp_path / "qualification"
    root.mkdir()
    expected = _canonical_json({"schemaVersion": 1, "token": "a" * 64})
    (root / ".offeragent-qualification-owner.json").write_bytes(
        _canonical_json({"schemaVersion": 1, "token": "b" * 64})
    )

    with pytest.raises(LiveBuiltProductQualificationError, match="ownership identity differs"):
        _remove_owned_root(root, expected)
