"""Handle-locked, reparse-safe deletion of an exact Windows directory tree."""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

_DELETE = 0x00010000
_FILE_LIST_DIRECTORY = 0x0001
_FILE_READ_ATTRIBUTES = 0x0080
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_DISPOSITION_INFO_CLASS = 4
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAXIMUM_NODES = 100_000
_MAXIMUM_DEPTH = 64


class WindowsSecureTreeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class WindowsFileIdentity:
    volume_id: str
    filesystem_id: str
    link_count: int
    attributes: int

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & _FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_reparse_point(self) -> bool:
        return bool(self.attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


class _FileTime(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", _FileTime),
        ("last_access_time", _FileTime),
        ("last_write_time", _FileTime),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


class _FileDispositionInformation(ctypes.Structure):
    _fields_ = [("delete_file", wintypes.BOOL)]


@dataclass(slots=True)
class _LockedNode:
    path: Path
    handle: int
    identity: WindowsFileIdentity
    children: list[_LockedNode] = field(default_factory=list)


class _WindowsFileApis:
    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsSecureTreeError("windows_required", "secure tree removal requires Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self.kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        self.kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self.kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self.kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def open_locked(self, path: Path) -> int:
        handle = self.kernel32.CreateFileW(
            _extended_local_path(path),
            _DELETE | _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
            _FILE_SHARE_READ,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        numeric = int(handle or 0)
        if not numeric or numeric == _INVALID_HANDLE_VALUE:
            raise _win32_error("plugin_handle_open_failed", "managed plugin entry could not be locked")
        return numeric

    def identity(self, handle: int) -> WindowsFileIdentity:
        information = _ByHandleFileInformation()
        if not self.kernel32.GetFileInformationByHandle(
            wintypes.HANDLE(handle),
            ctypes.byref(information),
        ):
            raise _win32_error("plugin_handle_identity_failed", "managed plugin identity is unavailable")
        file_index = (int(information.file_index_high) << 32) | int(information.file_index_low)
        return WindowsFileIdentity(
            volume_id=f"{int(information.volume_serial_number):x}",
            filesystem_id=f"{file_index:x}",
            link_count=int(information.number_of_links),
            attributes=int(information.file_attributes),
        )

    def final_path(self, handle: int) -> Path:
        buffer = ctypes.create_unicode_buffer(32_768)
        length = int(
            self.kernel32.GetFinalPathNameByHandleW(
                wintypes.HANDLE(handle),
                buffer,
                len(buffer),
                0,
            )
        )
        if length < 1 or length >= len(buffer):
            raise _win32_error("plugin_final_path_failed", "managed plugin final path is unavailable")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)

    def mark_delete(self, handle: int) -> None:
        disposition = _FileDispositionInformation(True)
        if not self.kernel32.SetFileInformationByHandle(
            wintypes.HANDLE(handle),
            _FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise _win32_error("plugin_handle_delete_failed", "managed plugin entry could not be deleted")

    def close(self, handle: int) -> None:
        if handle and not self.kernel32.CloseHandle(wintypes.HANDLE(handle)):
            raise _win32_error("plugin_handle_close_failed", "managed plugin handle could not be closed")


def identify_windows_file(path: Path) -> WindowsFileIdentity:
    """Read identity from an OPEN_REPARSE_POINT handle, never a followed path."""

    apis = _WindowsFileApis()
    handle = apis.open_locked(path)
    try:
        identity = apis.identity(handle)
        _validate_locked_path(apis, handle, path, path, identity, require_directory=True)
        return identity
    finally:
        apis.close(handle)


def remove_windows_tree(
    root: Path,
    *,
    expected_volume_id: str,
    expected_filesystem_id: str,
    race_barrier: Callable[[Path, tuple[str, ...]], None] | None = None,
) -> None:
    """Delete ``root`` while every discovered name is replacement-locked.

    Each object is opened with ``FILE_FLAG_OPEN_REPARSE_POINT`` and a sharing
    mode that refuses concurrent write/delete opens.  Its final handle path and
    file identity are checked before the handle is retained.  Deletion then
    uses that same DELETE handle, bottom-up; no child path is ever passed to a
    recursive path-based remover.
    """

    apis = _WindowsFileApis()
    handles: set[int] = set()
    root_key = _path_key(root)
    node_count = [0]
    try:
        root_node = _lock_node(
            apis,
            root,
            root,
            root_key,
            handles,
            node_count,
            depth=0,
            race_barrier=race_barrier,
        )
        if (
            root_node.identity.volume_id != expected_volume_id
            or root_node.identity.filesystem_id != expected_filesystem_id
        ):
            raise WindowsSecureTreeError(
                "plugin_directory_replaced",
                "installed plugin directory identity changed",
            )
        _delete_locked_node(apis, root_node, handles)
        try:
            root.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise WindowsSecureTreeError(
                "plugin_delete_ambiguous",
                "managed plugin removal result is ambiguous",
            ) from error
        raise WindowsSecureTreeError("plugin_delete_incomplete", "managed plugin directory still exists")
    finally:
        active_error = sys.exc_info()[0] is not None
        first_error: BaseException | None = None
        for handle in tuple(handles):
            try:
                apis.close(handle)
            except BaseException as error:  # pragma: no cover - catastrophic Win32 cleanup
                if first_error is None:
                    first_error = error
            handles.discard(handle)
        if first_error is not None and not active_error:
            raise first_error


def _lock_node(
    apis: _WindowsFileApis,
    path: Path,
    root: Path,
    root_key: str,
    handles: set[int],
    node_count: list[int],
    *,
    depth: int,
    race_barrier: Callable[[Path, tuple[str, ...]], None] | None,
) -> _LockedNode:
    if depth > _MAXIMUM_DEPTH or node_count[0] >= _MAXIMUM_NODES:
        raise WindowsSecureTreeError("plugin_tree_limit", "managed plugin tree exceeds safety limits")
    handle = apis.open_locked(path)
    handles.add(handle)
    node_count[0] += 1
    identity = apis.identity(handle)
    _validate_locked_path(
        apis,
        handle,
        path,
        root,
        identity,
        require_directory=True if depth == 0 else None,
    )
    final_key = _path_key(apis.final_path(handle))
    if final_key != root_key and not final_key.startswith(root_key + os.sep):
        raise WindowsSecureTreeError("plugin_tree_escape", "managed plugin entry escapes its root")
    node = _LockedNode(path=path, handle=handle, identity=identity)
    if not identity.is_directory:
        return node

    names = _directory_names(path)
    if race_barrier is not None:
        race_barrier(path, names)
    for name in names:
        if name in {"", ".", ".."} or "\\" in name or "/" in name or "\x00" in name:
            raise WindowsSecureTreeError("plugin_tree_name_invalid", "managed plugin entry name is invalid")
        node.children.append(
            _lock_node(
                apis,
                path / name,
                root,
                root_key,
                handles,
                node_count,
                depth=depth + 1,
                race_barrier=race_barrier,
            )
        )
    if _directory_names(path) != names:
        raise WindowsSecureTreeError("plugin_tree_changed", "managed plugin tree changed while locked")
    return node


def _validate_locked_path(
    apis: _WindowsFileApis,
    handle: int,
    opened_path: Path,
    root: Path,
    identity: WindowsFileIdentity,
    *,
    require_directory: bool | None,
) -> None:
    if identity.is_reparse_point:
        raise WindowsSecureTreeError("plugin_tree_reparse", "managed plugin tree contains a reparse point")
    if require_directory is not None and identity.is_directory is not require_directory:
        raise WindowsSecureTreeError("plugin_tree_type_changed", "managed plugin entry type changed")
    if not identity.is_directory and identity.link_count != 1:
        raise WindowsSecureTreeError("plugin_tree_hardlink", "managed plugin tree contains a hardlink")
    if _path_key(apis.final_path(handle)) != _path_key(opened_path):
        raise WindowsSecureTreeError("plugin_tree_path_changed", "managed plugin entry final path differs")
    try:
        observed = opened_path.lstat()
    except OSError as error:
        raise WindowsSecureTreeError("plugin_tree_path_changed", "managed plugin entry path changed") from error
    if (
        opened_path.is_symlink()
        or bool(getattr(observed, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)
        or (identity.is_directory and not stat.S_ISDIR(observed.st_mode))
        or (not identity.is_directory and not stat.S_ISREG(observed.st_mode))
        or (observed.st_ino and f"{observed.st_ino:x}" != identity.filesystem_id)
    ):
        raise WindowsSecureTreeError("plugin_tree_path_changed", "managed plugin entry path identity differs")
    root_key = _path_key(root)
    opened_key = _path_key(opened_path)
    if opened_key != root_key and not opened_key.startswith(root_key + os.sep):
        raise WindowsSecureTreeError("plugin_tree_escape", "managed plugin entry path escapes its root")


def _delete_locked_node(apis: _WindowsFileApis, node: _LockedNode, handles: set[int]) -> None:
    for child in node.children:
        _delete_locked_node(apis, child, handles)
    apis.mark_delete(node.handle)
    apis.close(node.handle)
    handles.discard(node.handle)
    node.handle = 0


def _directory_names(path: Path) -> tuple[str, ...]:
    try:
        with os.scandir(path) as entries:
            return tuple(sorted(entry.name for entry in entries))
    except OSError as error:
        raise WindowsSecureTreeError(
            "plugin_tree_enumeration_failed", "managed plugin tree cannot be enumerated"
        ) from error


def _extended_local_path(path: Path) -> str:
    value = os.path.abspath(str(path))
    if value.startswith(("\\\\", "//")):
        raise WindowsSecureTreeError("plugin_tree_remote", "managed plugin tree must be local")
    if value.startswith("\\\\?\\"):
        return value
    return "\\\\?\\" + value


def _path_key(path: Path) -> str:
    value = os.path.abspath(str(path))
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def _win32_error(code: str, message: str) -> WindowsSecureTreeError:
    winerror = ctypes.get_last_error()
    return WindowsSecureTreeError(code, f"{message} (Win32 {winerror})")


__all__ = [
    "WindowsFileIdentity",
    "WindowsSecureTreeError",
    "identify_windows_file",
    "remove_windows_tree",
]
