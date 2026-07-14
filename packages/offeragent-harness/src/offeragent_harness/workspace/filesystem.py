"""Bounded, stable local Vault reads behind the workspace path boundary."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Protocol, TypeVar

from offeragent_harness.ports import CancellationToken, OperationCancelled, VaultEntry, VaultEntryKind, VaultRead
from offeragent_harness.ports.vault import VaultTransaction
from offeragent_harness.tools import ToolResult

from .path_policy import PathPolicyError, PathPolicyErrorCode, ResolvedWorkspacePath, WorkspacePathPolicy

T = TypeVar("T")
_READ_BLOCK_BYTES = 64 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class VaultFilesystemErrorCode(str, Enum):
    PATH_POLICY = "path_policy"
    HIDDEN = "hidden_path"
    UNSUPPORTED_TYPE = "unsupported_file_type"
    NOT_REGULAR = "not_regular_file"
    HARD_LINK = "hard_link_forbidden"
    TOO_LARGE = "file_too_large"
    LIST_LIMIT = "list_limit_exceeded"
    LIST_SCAN_LIMIT = "list_scan_limit_exceeded"
    CHANGED = "changed_during_read"
    IO = "filesystem_io"


class VaultFilesystemError(RuntimeError):
    def __init__(self, code: VaultFilesystemErrorCode, path: str, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}: {path!r}")
        self.code = code
        self.path = path
        self.detail = detail


@dataclass(frozen=True, slots=True)
class VaultReadPolicy:
    allowed_extensions: frozenset[str] | None
    max_file_bytes: int
    max_return_bytes: int
    max_list_entries: int
    max_list_scan_entries: int
    allowed_hidden_prefixes: tuple[str, ...] = ()
    allow_hardlinks: bool = False

    def __post_init__(self) -> None:
        normalized = (
            None
            if self.allowed_extensions is None
            else frozenset(_normalize_extension(item) for item in self.allowed_extensions)
        )
        if normalized is not None and not normalized:
            raise ValueError("Vault read policy extension allowlist cannot be empty")
        limits = (
            self.max_file_bytes,
            self.max_return_bytes,
            self.max_list_entries,
            self.max_list_scan_entries,
        )
        if any(value < 1 for value in limits):
            raise ValueError("Vault read limits must be positive")
        if self.max_return_bytes > self.max_file_bytes:
            raise ValueError("max_return_bytes cannot exceed max_file_bytes")
        if self.max_list_entries > self.max_list_scan_entries:
            raise ValueError("max_list_entries cannot exceed max_list_scan_entries")
        prefixes = tuple(_normalize_prefix(item) for item in self.allowed_hidden_prefixes)
        object.__setattr__(self, "allowed_extensions", normalized)
        object.__setattr__(self, "allowed_hidden_prefixes", prefixes)


class VaultTransactionExecutor(Protocol):
    async def execute(
        self,
        transaction: VaultTransaction,
        cancellation: CancellationToken,
    ) -> ToolResult: ...


class VaultFileSystem:
    """Instance-scoped Vault port; writes only enter via the transaction executor."""

    def __init__(
        self,
        *,
        workspace_id: str,
        paths: WorkspacePathPolicy,
        read_policy: VaultReadPolicy,
        transaction_executor: VaultTransactionExecutor,
        workspace_revision: Callable[[], int] | None = None,
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")
        self._workspace_id = workspace_id
        self._paths = paths
        self._root = paths.root().path
        root_info = os.stat(self._root, follow_symlinks=False)
        _ensure_not_reparse(root_info, "")
        if not stat.S_ISDIR(root_info.st_mode):
            raise ValueError("Vault root must remain a directory")
        self._root_identity = _file_identity(root_info)
        self._read_policy = read_policy
        self._workspace_revision = workspace_revision
        self._transaction_executor = transaction_executor

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    async def stat(self, relative_path: str, cancellation: CancellationToken) -> VaultEntry | None:
        cancellation.checkpoint()
        try:
            resolved = self._resolve(relative_path)
        except PathPolicyError as error:
            if error.code is PathPolicyErrorCode.NOT_FOUND:
                return None
            raise VaultFilesystemError(VaultFilesystemErrorCode.PATH_POLICY, relative_path, error.detail) from error
        if not resolved.exists:
            return None
        self._enforce_visibility(resolved.relative_path)
        try:
            entry = await _run_io(self._stat_sync, resolved, True, cancellation)
        except OSError as error:
            raise VaultFilesystemError(VaultFilesystemErrorCode.IO, relative_path, str(error)) from error
        cancellation.checkpoint()
        return entry

    async def read(self, relative_path: str, cancellation: CancellationToken) -> VaultRead:
        return await self.read_bounded(relative_path, self._read_policy.max_file_bytes, cancellation)

    async def read_bounded(
        self,
        relative_path: str,
        max_bytes: int,
        cancellation: CancellationToken,
    ) -> VaultRead:
        if max_bytes < 1:
            raise ValueError("bounded Vault read requires a positive max_bytes")
        cancellation.checkpoint()
        try:
            resolved = self._paths.resolve(relative_path, must_exist=True, expect_directory=False)
        except PathPolicyError as error:
            raise VaultFilesystemError(VaultFilesystemErrorCode.PATH_POLICY, relative_path, error.detail) from error
        self._enforce_visibility(resolved.relative_path)
        self._enforce_extension(resolved.path, resolved.relative_path)
        try:
            entry, content, truncated = await _run_io(
                self._read_sync,
                resolved,
                min(max_bytes, self._read_policy.max_file_bytes),
                cancellation,
            )
        except OperationCancelled:
            raise
        except VaultFilesystemError:
            raise
        except (OSError, UnicodeError) as error:
            raise VaultFilesystemError(VaultFilesystemErrorCode.IO, relative_path, str(error)) from error
        cancellation.checkpoint()
        return VaultRead(entry=entry, content=content, truncated=truncated)

    async def list(self, relative_path: str, cancellation: CancellationToken) -> tuple[VaultEntry, ...]:
        cancellation.checkpoint()
        try:
            resolved = self._paths.resolve(
                relative_path,
                must_exist=True,
                expect_directory=True,
                allow_root=relative_path == "",
            )
        except PathPolicyError as error:
            raise VaultFilesystemError(VaultFilesystemErrorCode.PATH_POLICY, relative_path, error.detail) from error
        if resolved.relative_path:
            self._enforce_visibility(resolved.relative_path)
        try:
            return await _run_io(self._list_sync, resolved, cancellation)
        except VaultFilesystemError:
            raise
        except OSError as error:
            raise VaultFilesystemError(VaultFilesystemErrorCode.IO, relative_path, str(error)) from error

    async def execute_transaction(
        self,
        transaction: VaultTransaction,
        cancellation: CancellationToken,
    ) -> ToolResult:
        if transaction.workspace_id != self._workspace_id:
            raise VaultFilesystemError(
                VaultFilesystemErrorCode.PATH_POLICY,
                transaction.workspace_id,
                "transaction belongs to a different workspace",
            )
        cancellation.checkpoint()
        result = await self._transaction_executor.execute(transaction, cancellation)
        cancellation.checkpoint()
        return result

    def _resolve(self, relative_path: str) -> ResolvedWorkspacePath:
        return self._paths.resolve(relative_path, allow_root=relative_path == "")

    def _enforce_visibility(self, relative_path: str) -> None:
        parts = relative_path.split("/")
        if not any(part.startswith(".") for part in parts):
            return
        if any(
            relative_path == prefix or relative_path.startswith(prefix + "/")
            for prefix in self._read_policy.allowed_hidden_prefixes
        ):
            return
        raise VaultFilesystemError(
            VaultFilesystemErrorCode.HIDDEN, relative_path, "hidden Vault path is not authorized"
        )

    def _enforce_extension(self, path: Path, relative_path: str) -> None:
        allowed = self._read_policy.allowed_extensions
        if allowed is not None and path.suffix.casefold() not in allowed:
            raise VaultFilesystemError(
                VaultFilesystemErrorCode.UNSUPPORTED_TYPE,
                relative_path,
                "file extension is not in the explicit read allowlist",
            )

    def _stat_sync(
        self,
        resolved: ResolvedWorkspacePath,
        include_hash: bool,
        cancellation: CancellationToken,
    ) -> VaultEntry:
        self._validate_root_identity()
        cancellation.checkpoint()
        path_info = _path_lstat(resolved)
        mode = path_info.st_mode
        if stat.S_ISDIR(mode):
            info = _stable_directory_stat(resolved, path_info)
            kind = VaultEntryKind.DIRECTORY
            digest = None
        elif stat.S_ISREG(mode):
            self._enforce_extension(resolved.path, resolved.relative_path)
            with self._open_verified_file(resolved, path_info) as stream:
                before = os.fstat(stream.fileno())
                self._enforce_file_info(before, resolved.relative_path)
                self._enforce_file_size(before, resolved.relative_path)
                if include_hash:
                    digest, _, _ = self._consume_file(
                        stream,
                        cancellation,
                        relative_path=resolved.relative_path,
                        capture_bytes=0,
                    )
                else:
                    digest = None
                info = os.fstat(stream.fileno())
                _require_unchanged(resolved, before, info, "file changed while it was being inspected")
                self._validate_open_path(resolved, info)
            kind = VaultEntryKind.FILE
        else:
            raise VaultFilesystemError(
                VaultFilesystemErrorCode.NOT_REGULAR,
                resolved.relative_path,
                "entry is not a regular file or directory",
            )
        cancellation.checkpoint()
        self._validate_root_identity()
        return VaultEntry(
            resource_id=f"vault:{self._workspace_id}:{resolved.relative_path}",
            relative_path=resolved.relative_path,
            kind=kind,
            size=info.st_size,
            modified_at=datetime.fromtimestamp(info.st_mtime, tz=timezone.utc),
            content_hash=digest,
            workspace_revision=self._revision(),
        )

    def _read_sync(
        self,
        resolved: ResolvedWorkspacePath,
        max_bytes: int,
        cancellation: CancellationToken,
    ) -> tuple[VaultEntry, bytes, bool]:
        self._validate_root_identity()
        path_info = _path_lstat(resolved)
        with self._open_verified_file(resolved, path_info) as stream:
            before = os.fstat(stream.fileno())
            self._enforce_file_info(before, resolved.relative_path)
            self._enforce_file_size(before, resolved.relative_path, max_bytes=max_bytes)
            digest, returned, truncated = self._consume_file(
                stream,
                cancellation,
                relative_path=resolved.relative_path,
                capture_bytes=min(self._read_policy.max_return_bytes, max_bytes),
                max_file_bytes=max_bytes,
            )
            after = os.fstat(stream.fileno())
            _require_unchanged(resolved, before, after, "file changed while it was being read")
            self._validate_open_path(resolved, after)
        self._validate_root_identity()
        entry = VaultEntry(
            resource_id=f"vault:{self._workspace_id}:{resolved.relative_path}",
            relative_path=resolved.relative_path,
            kind=VaultEntryKind.FILE,
            size=after.st_size,
            modified_at=datetime.fromtimestamp(after.st_mtime_ns / 1_000_000_000, tz=timezone.utc),
            content_hash=digest,
            workspace_revision=self._revision(),
        )
        return entry, returned, truncated

    def _list_sync(
        self,
        resolved: ResolvedWorkspacePath,
        cancellation: CancellationToken,
    ) -> tuple[VaultEntry, ...]:
        self._validate_root_identity()
        before = _path_lstat(resolved)
        if not stat.S_ISDIR(before.st_mode):
            raise VaultFilesystemError(
                VaultFilesystemErrorCode.CHANGED,
                resolved.relative_path,
                "listed path stopped being a directory",
            )
        _ensure_current_path_within_root(resolved)
        before_entries = self._directory_snapshot(resolved, cancellation)
        entries: list[VaultEntry] = []
        scanned = 0
        with os.scandir(resolved.path) as iterator:
            for directory_entry in iterator:
                cancellation.checkpoint()
                scanned += 1
                if scanned > self._read_policy.max_list_scan_entries:
                    raise VaultFilesystemError(
                        VaultFilesystemErrorCode.LIST_SCAN_LIMIT,
                        resolved.relative_path,
                        f"directory scan exceeds {self._read_policy.max_list_scan_entries} entries",
                    )
                child_relative = (
                    f"{resolved.relative_path}/{directory_entry.name}"
                    if resolved.relative_path
                    else directory_entry.name
                )
                try:
                    child = self._paths.resolve(child_relative, must_exist=True)
                    self._enforce_visibility(child.relative_path)
                    entry = self._stat_sync(child, False, cancellation)
                except (PathPolicyError, VaultFilesystemError):
                    # Unauthorized, reparse, hard-linked and unsupported entries
                    # do not become capabilities and are not leaked by listing.
                    continue
                entries.append(entry)
                if len(entries) > self._read_policy.max_list_entries:
                    raise VaultFilesystemError(
                        VaultFilesystemErrorCode.LIST_LIMIT,
                        resolved.relative_path,
                        f"authorized results exceed {self._read_policy.max_list_entries} entries",
                    )
        after = _path_lstat(resolved)
        _ensure_current_path_within_root(resolved)
        after_entries = self._directory_snapshot(resolved, cancellation)
        if (
            not stat.S_ISDIR(after.st_mode)
            or _version_token(before) != _version_token(after)
            or before_entries != after_entries
        ):
            raise VaultFilesystemError(
                VaultFilesystemErrorCode.CHANGED,
                resolved.relative_path,
                "directory changed while it was being listed",
            )
        self._validate_root_identity()
        return tuple(sorted(entries, key=lambda entry: entry.relative_path.casefold()))

    def _directory_snapshot(
        self,
        resolved: ResolvedWorkspacePath,
        cancellation: CancellationToken,
    ) -> tuple[tuple[str, tuple[int, int, int, int, int]], ...]:
        """Return a bounded, identity-bearing directory membership snapshot.

        Directory timestamps are advisory on supported filesystems.  Listing is
        therefore accepted only when independent before/after snapshots agree;
        this catches mutations that occur within a timestamp granularity window.
        """

        snapshot: list[tuple[str, tuple[int, int, int, int, int]]] = []
        scanned = 0
        try:
            with os.scandir(resolved.path) as iterator:
                for directory_entry in iterator:
                    cancellation.checkpoint()
                    scanned += 1
                    if scanned > self._read_policy.max_list_scan_entries:
                        raise VaultFilesystemError(
                            VaultFilesystemErrorCode.LIST_SCAN_LIMIT,
                            resolved.relative_path,
                            f"directory scan exceeds {self._read_policy.max_list_scan_entries} entries",
                        )
                    # ``DirEntry.stat`` may expose attributes cached when the
                    # iterator was opened. A fresh lstat is required for a
                    # snapshot that can be compared with a later scan.
                    snapshot.append(
                        (directory_entry.name, _version_token(os.lstat(resolved.path / directory_entry.name)))
                    )
        except VaultFilesystemError:
            raise
        except OSError as error:
            raise VaultFilesystemError(
                VaultFilesystemErrorCode.CHANGED,
                resolved.relative_path,
                "directory changed while its membership was being captured",
            ) from error
        return tuple(sorted(snapshot, key=lambda item: item[0].casefold()))

    def _open_verified_file(
        self,
        resolved: ResolvedWorkspacePath,
        expected_path_info: os.stat_result,
    ) -> BinaryIO:
        self._validate_root_identity()
        _ensure_not_reparse(expected_path_info, resolved.relative_path)
        _ensure_current_path_within_root(resolved)
        descriptor = _open_readonly_fd(resolved.path)
        stream: BinaryIO | None = None
        try:
            stream = os.fdopen(descriptor, "rb")
            opened = os.fstat(stream.fileno())
            self._enforce_file_info(opened, resolved.relative_path)
            if _file_identity(expected_path_info) != _file_identity(opened):
                raise VaultFilesystemError(
                    VaultFilesystemErrorCode.CHANGED,
                    resolved.relative_path,
                    "path identity changed before the file handle was opened",
                )
            self._validate_open_path(resolved, opened)
            return stream
        except BaseException:
            if stream is not None:
                stream.close()
            else:
                os.close(descriptor)
            raise

    def _validate_open_path(self, resolved: ResolvedWorkspacePath, opened: os.stat_result) -> None:
        current = _path_lstat(resolved)
        if _file_identity(current) != _file_identity(opened):
            raise VaultFilesystemError(
                VaultFilesystemErrorCode.CHANGED,
                resolved.relative_path,
                "open handle no longer identifies the authorized Vault path",
            )
        _ensure_current_path_within_root(resolved)
        self._validate_root_identity()

    def _validate_root_identity(self) -> None:
        try:
            info = os.stat(self._root, follow_symlinks=False)
        except OSError as error:
            raise VaultFilesystemError(
                VaultFilesystemErrorCode.CHANGED,
                "",
                "Vault root disappeared during an operation",
            ) from error
        _ensure_not_reparse(info, "")
        if not stat.S_ISDIR(info.st_mode) or _file_identity(info) != self._root_identity:
            raise VaultFilesystemError(
                VaultFilesystemErrorCode.CHANGED,
                "",
                "Vault root identity changed during an operation",
            )

    def _enforce_file_info(self, info: os.stat_result, relative_path: str) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise VaultFilesystemError(
                VaultFilesystemErrorCode.NOT_REGULAR,
                relative_path,
                "entry is not a regular file",
            )
        if info.st_nlink > 1 and not self._read_policy.allow_hardlinks:
            raise VaultFilesystemError(
                VaultFilesystemErrorCode.HARD_LINK,
                relative_path,
                "multiply-linked files are denied by the Vault read policy",
            )

    def _enforce_file_size(
        self,
        info: os.stat_result,
        relative_path: str,
        *,
        max_bytes: int | None = None,
    ) -> None:
        limit = (
            self._read_policy.max_file_bytes if max_bytes is None else min(max_bytes, self._read_policy.max_file_bytes)
        )
        if info.st_size > limit:
            raise VaultFilesystemError(
                VaultFilesystemErrorCode.TOO_LARGE,
                relative_path,
                f"file exceeds {limit} bytes",
            )

    def _consume_file(
        self,
        stream: BinaryIO,
        cancellation: CancellationToken,
        *,
        relative_path: str,
        capture_bytes: int,
        max_file_bytes: int | None = None,
    ) -> tuple[str, bytes, bool]:
        digest = hashlib.sha256()
        returned = bytearray()
        total = 0
        limit = (
            self._read_policy.max_file_bytes
            if max_file_bytes is None
            else min(max_file_bytes, self._read_policy.max_file_bytes)
        )
        strict_limit = limit + 1
        while total < strict_limit:
            cancellation.checkpoint()
            chunk = stream.read(min(_READ_BLOCK_BYTES, strict_limit - total))
            cancellation.checkpoint()
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise VaultFilesystemError(
                    VaultFilesystemErrorCode.TOO_LARGE,
                    relative_path,
                    f"file exceeds {limit} bytes while reading",
                )
            digest.update(chunk)
            remaining = capture_bytes - len(returned)
            if remaining > 0:
                returned.extend(chunk[:remaining])
        return f"sha256:{digest.hexdigest()}", bytes(returned), total > len(returned)

    def _revision(self) -> int:
        if self._workspace_revision is None:
            return 0
        revision = self._workspace_revision()
        if revision < 0:
            raise ValueError("workspace revision cannot be negative")
        return revision


def _normalize_extension(value: str) -> str:
    normalized = value.casefold()
    if not re_full_extension(normalized):
        raise ValueError(f"invalid file extension {value!r}")
    return normalized


def re_full_extension(value: str) -> bool:
    return len(value) >= 2 and value.startswith(".") and value[1:].isalnum()


def _normalize_prefix(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError(f"invalid hidden path prefix {value!r}")
    return normalized


def _path_lstat(resolved: ResolvedWorkspacePath) -> os.stat_result:
    try:
        info = os.stat(resolved.path, follow_symlinks=False)
    except FileNotFoundError as error:
        raise VaultFilesystemError(
            VaultFilesystemErrorCode.CHANGED,
            resolved.relative_path,
            "authorized path disappeared before it could be opened",
        ) from error
    _ensure_not_reparse(info, resolved.relative_path)
    return info


def _ensure_not_reparse(info: os.stat_result, relative_path: str) -> None:
    attributes = int(getattr(info, "st_file_attributes", 0))
    if stat.S_ISLNK(info.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise VaultFilesystemError(
            VaultFilesystemErrorCode.CHANGED,
            relative_path,
            "path became a reparse point after authorization",
        )


def _ensure_current_path_within_root(resolved: ResolvedWorkspacePath) -> None:
    try:
        current = resolved.path.resolve(strict=True)
        contained = os.path.commonpath(
            (os.path.normcase(resolved.root), os.path.normcase(current))
        ) == os.path.normcase(resolved.root)
    except (FileNotFoundError, OSError, ValueError) as error:
        raise VaultFilesystemError(
            VaultFilesystemErrorCode.CHANGED,
            resolved.relative_path,
            "authorized path could not be revalidated",
        ) from error
    if not contained:
        raise VaultFilesystemError(
            VaultFilesystemErrorCode.CHANGED,
            resolved.relative_path,
            "authorized path left the Vault before it could be opened",
        )


def _stable_directory_stat(
    resolved: ResolvedWorkspacePath,
    before: os.stat_result,
) -> os.stat_result:
    _ensure_current_path_within_root(resolved)
    after = _path_lstat(resolved)
    if not stat.S_ISDIR(after.st_mode) or _version_token(before) != _version_token(after):
        raise VaultFilesystemError(
            VaultFilesystemErrorCode.CHANGED,
            resolved.relative_path,
            "directory changed while it was being inspected",
        )
    return after


def _require_unchanged(
    resolved: ResolvedWorkspacePath,
    before: os.stat_result,
    after: os.stat_result,
    detail: str,
) -> None:
    if _version_token(before) != _version_token(after):
        raise VaultFilesystemError(VaultFilesystemErrorCode.CHANGED, resolved.relative_path, detail)


def _file_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _version_token(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_readonly_fd(path: Path) -> int:
    if os.name == "nt":
        import _winapi
        import msvcrt

        # FILE_SHARE_READ only. New write/delete/rename handles conflict while
        # this verified snapshot handle remains open.
        handle = _winapi.CreateFile(
            _windows_extended_path(path),
            0x80000000,
            0x00000001,
            0,
            3,
            0x00200000 | 0x08000000,
            0,
        )
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _windows_extended_path(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


async def _run_io(function: Callable[..., T], *args: object) -> T:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException:
            pass
        raise


__all__ = [
    "VaultFileSystem",
    "VaultFilesystemError",
    "VaultFilesystemErrorCode",
    "VaultReadPolicy",
    "VaultTransactionExecutor",
]
