"""Fail-closed Windows path validation for Vault and additional root capabilities."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_RESERVED_BASENAMES = frozenset(
    {
        "CON",
        "CONIN$",
        "CONOUT$",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{suffix}" for suffix in "123456789¹²³"),
        *(f"LPT{suffix}" for suffix in "123456789¹²³"),
    }
)
_DEVICE_PREFIX = re.compile(r"^(?:[/\\]{2}[?.][/\\]|[/\\]\?\?[/\\]|GLOBALROOT(?:[/\\]|$))", re.IGNORECASE)
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class PathPolicyErrorCode(str, Enum):
    INVALID = "invalid_path"
    ABSOLUTE = "absolute_path"
    UNC = "unc_path"
    DEVICE = "device_path"
    ADS = "alternate_data_stream"
    RESERVED_NAME = "reserved_name"
    TRAILING_DOT_OR_SPACE = "trailing_dot_or_space"
    ESCAPE = "path_escape"
    REPARSE_POINT = "reparse_point"
    NOT_FOUND = "not_found"
    WRONG_KIND = "wrong_kind"
    UNKNOWN_ROOT = "unknown_root"


class PathPolicyError(ValueError):
    def __init__(self, code: PathPolicyErrorCode, path: str, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}: {path!r}")
        self.code = code
        self.path = path
        self.detail = detail


@dataclass(frozen=True)
class WorkspaceRoot:
    """An explicitly authorized filesystem root.

    Additional directories never become ambient authority: callers must name the
    corresponding ``root_id`` on every resolution.
    """

    root_id: str
    path: Path
    readable: bool = True
    writable: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.root_id):
            raise ValueError("root_id must be lowercase ASCII and at most 64 characters")
        canonical = self.path.expanduser().resolve(strict=True)
        if not canonical.is_dir():
            raise ValueError(f"workspace root is not a directory: {canonical}")
        object.__setattr__(self, "path", canonical)


@dataclass(frozen=True)
class ResolvedWorkspacePath:
    root_id: str
    root: Path
    path: Path
    relative_path: str
    exists: bool


class WorkspacePathPolicy:
    """Resolve untrusted relative paths beneath explicit Windows roots.

    This is the lexical and ancestry gate. Executors must still perform their
    final hash/existence checks immediately before a write to close TOCTOU races.
    """

    def __init__(
        self,
        vault_root: Path,
        *,
        additional_roots: tuple[WorkspaceRoot, ...] = (),
        reject_reparse_points: bool = True,
    ) -> None:
        roots = (WorkspaceRoot("vault", vault_root, readable=True, writable=True), *additional_roots)
        by_id = {item.root_id: item for item in roots}
        if len(by_id) != len(roots):
            raise ValueError("workspace root IDs must be unique")
        self._roots = by_id
        self._reject_reparse_points = reject_reparse_points

    @property
    def roots(self) -> tuple[WorkspaceRoot, ...]:
        return tuple(self._roots.values())

    def root(self, root_id: str = "vault") -> WorkspaceRoot:
        try:
            return self._roots[root_id]
        except KeyError as error:
            raise PathPolicyError(
                PathPolicyErrorCode.UNKNOWN_ROOT, root_id, "root capability is not authorized"
            ) from error

    def resolve(
        self,
        untrusted_path: str,
        *,
        root_id: str = "vault",
        must_exist: bool = False,
        expect_directory: bool | None = None,
        allow_root: bool = False,
        for_write: bool = False,
    ) -> ResolvedWorkspacePath:
        root_capability = self.root(root_id)
        if for_write and not root_capability.writable:
            raise PathPolicyError(PathPolicyErrorCode.UNKNOWN_ROOT, root_id, "root capability is read-only")
        if not for_write and not root_capability.readable:
            raise PathPolicyError(PathPolicyErrorCode.UNKNOWN_ROOT, root_id, "root capability is not readable")

        parts = _validate_relative_path(untrusted_path, allow_root=allow_root)
        lexical = root_capability.path.joinpath(*parts)
        if self._reject_reparse_points:
            _reject_existing_reparse_ancestors(root_capability.path, parts, untrusted_path)
        canonical = lexical.resolve(strict=False)
        if not _is_within(root_capability.path, canonical):
            raise PathPolicyError(PathPolicyErrorCode.ESCAPE, untrusted_path, "resolved path leaves authorized root")

        exists = canonical.exists()
        if must_exist and not exists:
            raise PathPolicyError(PathPolicyErrorCode.NOT_FOUND, untrusted_path, "path does not exist")
        if exists and expect_directory is not None and canonical.is_dir() is not expect_directory:
            expected = "directory" if expect_directory else "file"
            raise PathPolicyError(PathPolicyErrorCode.WRONG_KIND, untrusted_path, f"path is not a {expected}")
        relative_path = canonical.relative_to(root_capability.path).as_posix()
        if relative_path == ".":
            relative_path = ""
        return ResolvedWorkspacePath(
            root_id=root_id,
            root=root_capability.path,
            path=canonical,
            relative_path=relative_path,
            exists=exists,
        )


def _validate_relative_path(untrusted_path: str, *, allow_root: bool) -> tuple[str, ...]:
    if not isinstance(untrusted_path, str) or "\x00" in untrusted_path:
        raise PathPolicyError(PathPolicyErrorCode.INVALID, str(untrusted_path), "path must be a NUL-free string")
    if _DEVICE_PREFIX.match(untrusted_path):
        raise PathPolicyError(PathPolicyErrorCode.DEVICE, untrusted_path, "Windows device namespace is forbidden")
    if untrusted_path.startswith(("\\\\", "//")):
        raise PathPolicyError(PathPolicyErrorCode.UNC, untrusted_path, "UNC paths are forbidden")
    if untrusted_path.startswith(("\\", "/")) or _DRIVE_PREFIX.match(untrusted_path):
        raise PathPolicyError(PathPolicyErrorCode.ABSOLUTE, untrusted_path, "only root-relative paths are allowed")
    if untrusted_path == "":
        if allow_root:
            return ()
        raise PathPolicyError(PathPolicyErrorCode.INVALID, untrusted_path, "empty path is not allowed")

    raw_parts = re.split(r"[/\\]", untrusted_path)
    parts: list[str] = []
    for part in raw_parts:
        if part in {"", "."}:
            raise PathPolicyError(PathPolicyErrorCode.INVALID, untrusted_path, "empty and dot segments are forbidden")
        if part == "..":
            raise PathPolicyError(PathPolicyErrorCode.ESCAPE, untrusted_path, "parent traversal is forbidden")
        if part[-1] in {".", " "}:
            raise PathPolicyError(
                PathPolicyErrorCode.TRAILING_DOT_OR_SPACE,
                untrusted_path,
                "Windows ignores trailing dots and spaces",
            )
        if ":" in part:
            raise PathPolicyError(
                PathPolicyErrorCode.ADS, untrusted_path, "drive and alternate stream syntax is forbidden"
            )
        if any(character in _WINDOWS_FORBIDDEN or ord(character) < 32 for character in part):
            raise PathPolicyError(
                PathPolicyErrorCode.INVALID, untrusted_path, "path contains forbidden Windows characters"
            )
        basename = part.split(".", maxsplit=1)[0].upper()
        if basename in _RESERVED_BASENAMES:
            raise PathPolicyError(PathPolicyErrorCode.RESERVED_NAME, untrusted_path, "reserved DOS device name")
        parts.append(part)
    return tuple(parts)


def _reject_existing_reparse_ancestors(root: Path, parts: tuple[str, ...], original: str) -> None:
    current = root
    for part in parts:
        current /= part
        try:
            stat = current.lstat()
        except FileNotFoundError:
            return
        attributes = int(getattr(stat, "st_file_attributes", 0))
        if current.is_symlink() or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise PathPolicyError(
                PathPolicyErrorCode.REPARSE_POINT,
                original,
                f"reparse point is forbidden beneath workspace root ({current})",
            )


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(root), os.path.normcase(candidate))) == os.path.normcase(root)
    except ValueError:
        return False


__all__ = [
    "PathPolicyError",
    "PathPolicyErrorCode",
    "ResolvedWorkspacePath",
    "WorkspacePathPolicy",
    "WorkspaceRoot",
]
