"""Strict portable workspace identity shared by Worker and Obsidian client."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_MAXIMUM_BYTES = 4096
_WORKSPACE_ID = re.compile(r"^ws_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class PortableWorkspaceConfigError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PortableWorkspaceConfig:
    portable_workspace_id: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or _WORKSPACE_ID.fullmatch(self.portable_workspace_id) is None:
            raise PortableWorkspaceConfigError("workspace_config_invalid", "portable workspace identity is invalid")


def ensure_portable_workspace_config(
    vault_root: Path,
    *,
    new_uuid: Callable[[], uuid.UUID] | None = None,
) -> PortableWorkspaceConfig:
    """Read or create only ``.offeragent/workspace.json`` under a local Vault."""

    root = vault_root.resolve(strict=True)
    _require_directory(root, "vault_root_reparse")
    offeragent = root / ".offeragent"
    try:
        offeragent.mkdir()
    except FileExistsError:
        pass
    _require_directory(offeragent, "workspace_directory_reparse")
    target = offeragent / "workspace.json"
    try:
        return read_portable_workspace_config(root)
    except FileNotFoundError:
        pass
    identifier = f"ws_{new_uuid() if new_uuid is not None else uuid.uuid4()}"
    config = PortableWorkspaceConfig(identifier)
    payload = _canonical_bytes(config)
    temporary = offeragent / f".workspace.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(descriptor, "wb", buffering=0, closefd=False) as stream:
                stream.write(payload)
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            # Windows rename is same-directory atomic and refuses an existing
            # destination, so concurrent first attaches cannot overwrite IDs.
            os.rename(temporary, target)
        except FileExistsError:
            return read_portable_workspace_config(root)
        return read_portable_workspace_config(root)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_portable_workspace_config(vault_root: Path) -> PortableWorkspaceConfig:
    root = vault_root.resolve(strict=True)
    _require_directory(root, "vault_root_reparse")
    offeragent = root / ".offeragent"
    _require_directory(offeragent, "workspace_directory_reparse")
    target = offeragent / "workspace.json"
    try:
        info = target.lstat()
    except FileNotFoundError:
        raise
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or target.is_symlink()
        or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise PortableWorkspaceConfigError("workspace_config_type", "workspace config is not a regular file")
    if not 1 <= info.st_size <= _MAXIMUM_BYTES:
        raise PortableWorkspaceConfigError("workspace_config_size", "workspace config exceeds its limit")
    try:
        with target.open("rb", buffering=0) as stream:
            payload = stream.read(_MAXIMUM_BYTES + 1)
        raw = json.loads(payload.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PortableWorkspaceConfigError("workspace_config_malformed", "workspace config is malformed") from error
    if not isinstance(raw, dict) or set(raw) != {"portableWorkspaceId", "schemaVersion"}:
        raise PortableWorkspaceConfigError("workspace_config_fields", "workspace config fields are invalid")
    workspace_id = raw["portableWorkspaceId"]
    schema_version = raw["schemaVersion"]
    if not isinstance(workspace_id, str) or not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise PortableWorkspaceConfigError("workspace_config_fields", "workspace config values are invalid")
    config = PortableWorkspaceConfig(workspace_id, schema_version)
    if _canonical_bytes(config) != payload:
        raise PortableWorkspaceConfigError("workspace_config_noncanonical", "workspace config is not canonical JSON")
    return config


def _canonical_bytes(config: PortableWorkspaceConfig) -> bytes:
    return (
        json.dumps(
            {"portableWorkspaceId": config.portable_workspace_id, "schemaVersion": config.schema_version},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _require_directory(path: Path, code: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            raise
        raise PortableWorkspaceConfigError(code, "workspace path cannot be inspected") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise PortableWorkspaceConfigError(code, "workspace path is not a safe directory")


__all__ = [
    "PortableWorkspaceConfig",
    "PortableWorkspaceConfigError",
    "ensure_portable_workspace_config",
    "read_portable_workspace_config",
]
