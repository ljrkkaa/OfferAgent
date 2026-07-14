from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from offeragent_harness.workspace import (
    PathPolicyError,
    PathPolicyErrorCode,
    WorkspacePathPolicy,
    WorkspaceRoot,
)


def test_resolves_normalized_path_under_vault(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    target = notes / "Offer.md"
    target.write_text("hello", encoding="utf-8")
    policy = WorkspacePathPolicy(tmp_path)

    resolved = policy.resolve("notes\\Offer.md", must_exist=True, expect_directory=False)

    assert resolved.path == target.resolve()
    assert resolved.relative_path == "notes/Offer.md"
    assert resolved.exists is True


@pytest.mark.parametrize(
    ("candidate", "code"),
    [
        ("../secret.txt", PathPolicyErrorCode.ESCAPE),
        ("notes/../../secret.txt", PathPolicyErrorCode.ESCAPE),
        (r"C:\Windows\win.ini", PathPolicyErrorCode.ABSOLUTE),
        (r"C:relative.txt", PathPolicyErrorCode.ABSOLUTE),
        (r"\\server\share\file", PathPolicyErrorCode.UNC),
        (r"\\?\C:\Windows\win.ini", PathPolicyErrorCode.DEVICE),
        (r"\\.\PhysicalDrive0", PathPolicyErrorCode.DEVICE),
        ("note.md:secret", PathPolicyErrorCode.ADS),
        ("CON", PathPolicyErrorCode.RESERVED_NAME),
        ("CONIN$", PathPolicyErrorCode.RESERVED_NAME),
        ("conout$.txt", PathPolicyErrorCode.RESERVED_NAME),
        ("folder/ConIn$.md", PathPolicyErrorCode.RESERVED_NAME),
        ("aux.txt", PathPolicyErrorCode.RESERVED_NAME),
        ("folder/NUL.md", PathPolicyErrorCode.RESERVED_NAME),
        ("note.md.", PathPolicyErrorCode.TRAILING_DOT_OR_SPACE),
        ("note.md ", PathPolicyErrorCode.TRAILING_DOT_OR_SPACE),
        ("notes//file.md", PathPolicyErrorCode.INVALID),
        ("notes/./file.md", PathPolicyErrorCode.INVALID),
        ("notes/<bad>.md", PathPolicyErrorCode.INVALID),
    ],
)
def test_rejects_ambiguous_and_escaping_windows_paths(
    tmp_path: Path, candidate: str, code: PathPolicyErrorCode
) -> None:
    policy = WorkspacePathPolicy(tmp_path)

    with pytest.raises(PathPolicyError) as captured:
        policy.resolve(candidate)

    assert captured.value.code is code


def test_additional_root_requires_explicit_capability_and_honors_write_flag(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    attachment = tmp_path / "attachment"
    vault.mkdir()
    attachment.mkdir()
    (attachment / "input.pdf").write_bytes(b"pdf")
    policy = WorkspacePathPolicy(
        vault,
        additional_roots=(WorkspaceRoot("attachments", attachment, readable=True, writable=False),),
    )

    resolved = policy.resolve("input.pdf", root_id="attachments", must_exist=True)
    assert resolved.root_id == "attachments"
    with pytest.raises(PathPolicyError) as captured:
        policy.resolve("output.txt", root_id="attachments", for_write=True)
    assert captured.value.code is PathPolicyErrorCode.UNKNOWN_ROOT
    with pytest.raises(PathPolicyError):
        policy.resolve("../attachment/input.pdf")


def test_missing_target_checks_existing_reparse_ancestor(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    link = vault / "linked"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            pytest.skip(f"creating a test symlink is not permitted: {error}")
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            pytest.fail(f"could not create Windows junction: {completed.stderr or completed.stdout}")
    policy = WorkspacePathPolicy(vault)

    with pytest.raises(PathPolicyError) as captured:
        policy.resolve("linked/new.md", for_write=True)

    assert captured.value.code is PathPolicyErrorCode.REPARSE_POINT


def test_root_resolution_is_explicit(tmp_path: Path) -> None:
    policy = WorkspacePathPolicy(tmp_path)
    with pytest.raises(PathPolicyError):
        policy.resolve("")
    assert policy.resolve("", allow_root=True).path == tmp_path.resolve()
