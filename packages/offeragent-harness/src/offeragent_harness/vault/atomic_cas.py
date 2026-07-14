"""Race-safe compare-and-swap primitives for local Vault files.

The production runtime is Windows-only.  On Windows every directory and file
participating in a mutation is opened with ``FILE_FLAG_OPEN_REPARSE_POINT``.
Directory handles deny delete sharing and file handles deny both write and
delete sharing.  The same ``DELETE`` file handle is then renamed with
``FileRenameInfo`` and ``ReplaceIfExists=FALSE``.  Consequently neither the
claim, publish, nor rollback path can replace an entry selected by name after
it was verified.

The POSIX implementation exists for deterministic development tests.  It uses
the platform's native atomic no-replace rename primitive (``renameat2`` or
``renamex_np``); an unsupported platform fails closed instead of falling back
to a check-then-replace sequence.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from .schema import ABSENT_HASH

_READ_BLOCK_BYTES = 64 * 1024
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


class AtomicVaultCasError(RuntimeError):
    """A CAS operation failed without permission to overwrite anything."""


class AtomicVaultCasConflict(AtomicVaultCasError):
    """The selected filesystem state no longer matches the approved state."""


class AtomicVaultCasUncertain(AtomicVaultCasError):
    """The filesystem did not expose enough evidence to prove an outcome."""


VaultCasBarrier = Callable[[str, str], None]


@dataclass(frozen=True, slots=True)
class _NativeIdentity:
    volume: int
    file_id: int
    links: int
    attributes: int
    size: int

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & _FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_reparse_point(self) -> bool:
        return bool(self.attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


@dataclass(slots=True)
class _LockedEntry:
    path: Path
    handle: int
    identity: _NativeIdentity
    directory: bool
    closed: bool = False


class _FileTime(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
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


class _FileRenameInformation(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("replace_if_exists", ctypes.c_ubyte),
        ("root_directory", wintypes.HANDLE),
        ("file_name_length", wintypes.DWORD),
        ("file_name", wintypes.WCHAR * 1),
    ]


class _FileDispositionInformation(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [("delete_file", wintypes.BOOL)]


class _WindowsCasBackend:
    _DELETE = 0x00010000
    _GENERIC_READ = 0x80000000
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_READ_ATTRIBUTES = 0x0080
    _SYNCHRONIZE = 0x00100000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    _FILE_BEGIN = 0
    _FILE_RENAME_INFO_CLASS = 3
    _FILE_DISPOSITION_INFO_CLASS = 4
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self, root: Path, max_file_bytes: int) -> None:
        if os.name != "nt":
            raise AtomicVaultCasError("Windows CAS backend requires Windows")
        self._root = root
        self._root_key = _path_key(root)
        self._max_file_bytes = max_file_bytes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self._kernel32.SetFilePointerEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_longlong),
            wintypes.DWORD,
        ]
        self._kernel32.SetFilePointerEx.restype = wintypes.BOOL
        self._kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self._kernel32.ReadFile.restype = wintypes.BOOL
        self._kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def lock_directory(self, path: Path, expected: tuple[int, int]) -> _LockedEntry:
        handle = self._open(
            path,
            self._FILE_LIST_DIRECTORY | self._FILE_READ_ATTRIBUTES | self._SYNCHRONIZE,
            self._FILE_SHARE_READ | self._FILE_SHARE_WRITE,
            self._FILE_FLAG_BACKUP_SEMANTICS | self._FILE_FLAG_OPEN_REPARSE_POINT,
        )
        entry = _LockedEntry(path, handle, self._identity(handle), True)
        try:
            self._validate_entry(entry, path, expected=expected, require_directory=True, expected_hash=None)
            return entry
        except BaseException:
            self.close(entry)
            raise

    def lock_file(self, path: Path, expected_hash: str) -> _LockedEntry:
        handle = self._open(
            path,
            self._GENERIC_READ | self._DELETE | self._FILE_READ_ATTRIBUTES | self._SYNCHRONIZE,
            self._FILE_SHARE_READ,
            self._FILE_FLAG_OPEN_REPARSE_POINT | self._FILE_FLAG_SEQUENTIAL_SCAN,
        )
        entry = _LockedEntry(path, handle, self._identity(handle), False)
        try:
            self._validate_entry(entry, path, expected=None, require_directory=False, expected_hash=expected_hash)
            return entry
        except BaseException:
            self.close(entry)
            raise

    def validate_directory(self, entry: _LockedEntry, expected: tuple[int, int]) -> None:
        self._validate_entry(
            entry,
            entry.path,
            expected=expected,
            require_directory=True,
            expected_hash=None,
        )

    def validate_file(self, entry: _LockedEntry, expected_path: Path, expected_hash: str) -> None:
        self._validate_entry(
            entry,
            expected_path,
            expected=None,
            require_directory=False,
            expected_hash=expected_hash,
        )

    def rename_noreplace(self, entry: _LockedEntry, destination: Path) -> None:
        if entry.closed:
            raise AtomicVaultCasUncertain("CAS source handle is already closed")
        source = entry.path
        encoded = _extended_local_path(destination).encode("utf-16-le")
        offset = _FileRenameInformation.file_name.offset
        # Although FileNameLength excludes a terminator, Windows filesystems
        # are permitted to inspect a complete FILE_RENAME_INFO allocation.
        # Reserve and zero the trailing WCHAR so the variable-length member
        # can never consume adjacent bytes as part of the destination name.
        buffer = ctypes.create_string_buffer(offset + len(encoded) + ctypes.sizeof(wintypes.WCHAR))
        information = _FileRenameInformation.from_buffer(buffer)
        information.replace_if_exists = 0
        information.root_directory = None
        information.file_name_length = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
        renamed = bool(
            self._kernel32.SetFileInformationByHandle(
                wintypes.HANDLE(entry.handle),
                self._FILE_RENAME_INFO_CLASS,
                buffer,
                len(buffer),
            )
        )
        winerror = ctypes.get_last_error()
        observed = self._final_path(entry.handle)
        if _path_key(observed) == _path_key(destination):
            entry.path = destination
            return
        if _path_key(observed) == _path_key(source):
            if renamed:
                raise AtomicVaultCasUncertain("rename reported success but source handle did not move")
            raise AtomicVaultCasConflict(f"atomic no-replace rename was refused (Win32 {winerror})")
        raise AtomicVaultCasUncertain(f"atomic rename outcome escaped both source and destination (Win32 {winerror})")

    def delete_locked(self, entry: _LockedEntry) -> None:
        if entry.closed:
            raise AtomicVaultCasUncertain("CAS delete handle is already closed")
        disposition = _FileDispositionInformation(True)
        deleted = bool(
            self._kernel32.SetFileInformationByHandle(
                wintypes.HANDLE(entry.handle),
                self._FILE_DISPOSITION_INFO_CLASS,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            )
        )
        if not deleted:
            raise _windows_error("locked CAS file could not be marked for deletion")
        path = entry.path
        self.close(entry)
        try:
            path.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise AtomicVaultCasUncertain("locked CAS deletion outcome is unconfirmed") from error
        raise AtomicVaultCasUncertain("locked CAS file remains visible after deletion")

    def close(self, entry: _LockedEntry) -> None:
        if entry.closed:
            return
        entry.closed = True
        if not self._kernel32.CloseHandle(wintypes.HANDLE(entry.handle)):
            raise _windows_error("CAS handle close failed")

    def _open(self, path: Path, access: int, sharing: int, flags: int) -> int:
        handle = self._kernel32.CreateFileW(
            _extended_local_path(path),
            access,
            sharing,
            None,
            self._OPEN_EXISTING,
            flags,
            None,
        )
        numeric = int(handle or 0)
        if not numeric or numeric == self._INVALID_HANDLE_VALUE:
            raise _windows_error(f"CAS handle open failed for {path.name}", conflict=True)
        return numeric

    def _identity(self, handle: int) -> _NativeIdentity:
        information = _ByHandleFileInformation()
        if not self._kernel32.GetFileInformationByHandle(
            wintypes.HANDLE(handle),
            ctypes.byref(information),
        ):
            raise _windows_error("CAS handle identity is unavailable")
        file_id = (int(information.file_index_high) << 32) | int(information.file_index_low)
        size = (int(information.file_size_high) << 32) | int(information.file_size_low)
        return _NativeIdentity(
            volume=int(information.volume_serial_number),
            file_id=file_id,
            links=int(information.number_of_links),
            attributes=int(information.file_attributes),
            size=size,
        )

    def _final_path(self, handle: int) -> Path:
        capacity = 32_768
        buffer = ctypes.create_unicode_buffer(capacity)
        length = int(
            self._kernel32.GetFinalPathNameByHandleW(
                wintypes.HANDLE(handle),
                buffer,
                capacity,
                0,
            )
        )
        if length < 1 or length >= capacity:
            raise _windows_error("CAS handle final path is unavailable")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)

    def _read_hash(self, entry: _LockedEntry) -> str:
        if entry.identity.size > self._max_file_bytes:
            raise AtomicVaultCasConflict("CAS target exceeds the approved file-size limit")
        position = ctypes.c_longlong()
        if not self._kernel32.SetFilePointerEx(
            wintypes.HANDLE(entry.handle),
            0,
            ctypes.byref(position),
            self._FILE_BEGIN,
        ):
            raise _windows_error("CAS target seek failed")
        digest = hashlib.sha256()
        total = 0
        buffer = ctypes.create_string_buffer(_READ_BLOCK_BYTES)
        while True:
            read = wintypes.DWORD()
            if not self._kernel32.ReadFile(
                wintypes.HANDLE(entry.handle),
                buffer,
                len(buffer),
                ctypes.byref(read),
                None,
            ):
                raise _windows_error("CAS target read failed")
            count = int(read.value)
            if count == 0:
                break
            total += count
            if total > self._max_file_bytes:
                raise AtomicVaultCasConflict("CAS target exceeds the approved file-size limit")
            digest.update(buffer.raw[:count])
        return f"sha256:{digest.hexdigest()}"

    def _validate_entry(
        self,
        entry: _LockedEntry,
        expected_path: Path,
        *,
        expected: tuple[int, int] | None,
        require_directory: bool,
        expected_hash: str | None,
    ) -> None:
        if entry.closed:
            raise AtomicVaultCasUncertain("CAS validation handle is closed")
        identity = self._identity(entry.handle)
        if identity.is_reparse_point:
            raise AtomicVaultCasConflict(f"reparse point is forbidden: {expected_path.name}")
        if identity.is_directory is not require_directory:
            raise AtomicVaultCasConflict(f"CAS path type changed: {expected_path.name}")
        if not require_directory and identity.links != 1:
            raise AtomicVaultCasConflict(f"hard-linked Vault file is forbidden: {expected_path.name}")
        final_path = self._final_path(entry.handle)
        if _path_key(final_path) != _path_key(expected_path):
            raise AtomicVaultCasConflict(f"CAS path identity changed: {expected_path.name}")
        final_key = _path_key(final_path)
        if final_key != self._root_key and not final_key.startswith(self._root_key + os.sep):
            raise AtomicVaultCasConflict("CAS handle escaped the authorized Vault root")
        try:
            observed = expected_path.lstat()
        except OSError as error:
            raise AtomicVaultCasConflict(f"CAS path disappeared: {expected_path.name}") from error
        attributes = int(getattr(observed, "st_file_attributes", 0))
        if stat.S_ISLNK(observed.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise AtomicVaultCasConflict(f"reparse point is forbidden: {expected_path.name}")
        if require_directory and not stat.S_ISDIR(observed.st_mode):
            raise AtomicVaultCasConflict(f"CAS directory type changed: {expected_path.name}")
        if not require_directory and not stat.S_ISREG(observed.st_mode):
            raise AtomicVaultCasConflict(f"CAS file type changed: {expected_path.name}")
        observed_pair = (int(observed.st_dev), int(observed.st_ino))
        if expected is not None and observed_pair != expected:
            raise AtomicVaultCasConflict(f"CAS directory identity changed: {expected_path.name}")
        if int(observed.st_ino) > 0 and int(observed.st_ino) != identity.file_id:
            raise AtomicVaultCasConflict(f"CAS handle/name identity differs: {expected_path.name}")
        if not require_directory and int(observed.st_nlink) != 1:
            raise AtomicVaultCasConflict(f"hard-linked Vault file is forbidden: {expected_path.name}")
        if expected_hash is not None and self._read_hash(entry) != expected_hash:
            raise AtomicVaultCasConflict(f"CAS content hash changed: {expected_path.name}")
        refreshed = self._identity(entry.handle)
        if refreshed != identity:
            raise AtomicVaultCasConflict(f"CAS handle metadata changed: {expected_path.name}")
        entry.identity = refreshed


class _PosixCasBackend:
    def __init__(self, root: Path, max_file_bytes: int) -> None:
        self._root = root
        self._root_key = _path_key(root)
        self._max_file_bytes = max_file_bytes

    def lock_directory(self, path: Path, expected: tuple[int, int]) -> _LockedEntry:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise AtomicVaultCasConflict(f"CAS directory open failed: {path.name}") from error
        info = os.fstat(descriptor)
        entry = _LockedEntry(path, descriptor, _posix_identity(info), True)
        try:
            self.validate_directory(entry, expected)
            return entry
        except BaseException:
            self.close(entry)
            raise

    def lock_file(self, path: Path, expected_hash: str) -> _LockedEntry:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise AtomicVaultCasConflict(f"CAS file open failed: {path.name}") from error
        info = os.fstat(descriptor)
        entry = _LockedEntry(path, descriptor, _posix_identity(info), False)
        try:
            self.validate_file(entry, path, expected_hash)
            return entry
        except BaseException:
            self.close(entry)
            raise

    def validate_directory(self, entry: _LockedEntry, expected: tuple[int, int]) -> None:
        self._validate_entry(entry, entry.path, expected=expected, directory=True, expected_hash=None)

    def validate_file(self, entry: _LockedEntry, expected_path: Path, expected_hash: str) -> None:
        self._validate_entry(entry, expected_path, expected=None, directory=False, expected_hash=expected_hash)

    def rename_noreplace(self, entry: _LockedEntry, destination: Path) -> None:
        if entry.closed:
            raise AtomicVaultCasUncertain("CAS source descriptor is already closed")
        source = entry.path
        _native_rename_noreplace(source, destination)
        try:
            observed = destination.lstat()
        except OSError as error:
            if source.exists():
                raise AtomicVaultCasConflict("atomic no-replace rename was refused") from error
            raise AtomicVaultCasUncertain("atomic no-replace rename outcome is unconfirmed") from error
        if (int(observed.st_dev), int(observed.st_ino)) != (entry.identity.volume, entry.identity.file_id):
            raise AtomicVaultCasUncertain("atomic no-replace rename selected an unexpected object")
        entry.path = destination

    def delete_locked(self, entry: _LockedEntry) -> None:
        if entry.closed:
            raise AtomicVaultCasUncertain("CAS delete descriptor is already closed")
        self._validate_path_identity(entry, entry.path)
        path = entry.path
        try:
            path.unlink()
        except OSError as error:
            raise AtomicVaultCasUncertain("locked CAS deletion outcome is unconfirmed") from error
        self.close(entry)

    @staticmethod
    def close(entry: _LockedEntry) -> None:
        if entry.closed:
            return
        entry.closed = True
        os.close(entry.handle)

    def _validate_entry(
        self,
        entry: _LockedEntry,
        expected_path: Path,
        *,
        expected: tuple[int, int] | None,
        directory: bool,
        expected_hash: str | None,
    ) -> None:
        if entry.closed:
            raise AtomicVaultCasUncertain("CAS validation descriptor is closed")
        before = os.fstat(entry.handle)
        _validate_posix_stat(before, expected_path, directory=directory)
        if not directory and before.st_nlink != 1:
            raise AtomicVaultCasConflict(f"hard-linked Vault file is forbidden: {expected_path.name}")
        self._validate_path_identity(entry, expected_path)
        pair = (int(before.st_dev), int(before.st_ino))
        if expected is not None and pair != expected:
            raise AtomicVaultCasConflict(f"CAS directory identity changed: {expected_path.name}")
        if expected_hash is not None and self._read_hash(entry) != expected_hash:
            raise AtomicVaultCasConflict(f"CAS content hash changed: {expected_path.name}")
        after = os.fstat(entry.handle)
        if _posix_identity(after) != _posix_identity(before):
            raise AtomicVaultCasConflict(f"CAS handle metadata changed: {expected_path.name}")
        entry.identity = _posix_identity(after)

    def _validate_path_identity(self, entry: _LockedEntry, expected_path: Path) -> None:
        try:
            observed = expected_path.lstat()
        except OSError as error:
            raise AtomicVaultCasConflict(f"CAS path disappeared: {expected_path.name}") from error
        pair = (int(observed.st_dev), int(observed.st_ino))
        if pair != (entry.identity.volume, entry.identity.file_id):
            raise AtomicVaultCasConflict(f"CAS handle/name identity differs: {expected_path.name}")
        key = _path_key(expected_path)
        if key != self._root_key and not key.startswith(self._root_key + os.sep):
            raise AtomicVaultCasConflict("CAS descriptor escaped the authorized Vault root")

    def _read_hash(self, entry: _LockedEntry) -> str:
        os.lseek(entry.handle, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(entry.handle, _READ_BLOCK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > self._max_file_bytes:
                raise AtomicVaultCasConflict("CAS target exceeds the approved file-size limit")
            digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"


class AtomicVaultChange:
    """A handle-bound mutation retained until batch commit or rollback."""

    def __init__(
        self,
        *,
        backend: _WindowsCasBackend | _PosixCasBackend,
        relative_path: str,
        target: Path,
        ancestry: Sequence[tuple[_LockedEntry, tuple[int, int]]],
        original: _LockedEntry | None,
        before_hash: str,
        after_hash: str,
        barrier: VaultCasBarrier | None,
    ) -> None:
        self.relative_path = relative_path
        self.target = target
        self._backend = backend
        self._ancestry = list(ancestry)
        self._original = original
        self._final: _LockedEntry | None = None
        self._before_hash = before_hash
        self._after_hash = after_hash
        self._barrier = barrier
        self._backup: Path | None = None
        self._rollback_transient: Path | None = None
        self._claimed = False
        self._published = False
        self._rollback_claimed = False
        self._claim_uncertain = False
        self._publish_uncertain = False
        self._closed = False

    @property
    def mutated(self) -> bool:
        return (
            self._claimed
            or self._published
            or self._rollback_claimed
            or self._claim_uncertain
            or self._publish_uncertain
        )

    @property
    def backup_path(self) -> Path | None:
        return self._backup

    def apply(self, *, temporary: Path | None, backup: Path | None, rollback_transient: Path) -> None:
        if self._closed:
            raise AtomicVaultCasUncertain("CAS change was already closed")
        self._rollback_transient = rollback_transient
        if temporary is not None:
            self._final = self._backend.lock_file(temporary, self._after_hash)
        self._validate_ancestry()
        if self._original is not None:
            if backup is None:
                raise AtomicVaultCasError("existing CAS target requires a backup path")
            self._backend.validate_file(self._original, self.target, self._before_hash)
            self._backup = backup
            try:
                self._backend.rename_noreplace(self._original, backup)
            except AtomicVaultCasConflict:
                raise
            except BaseException:
                self._claim_uncertain = True
                raise
            self._claimed = True
            self._signal("original_claimed")
            self._backend.validate_file(self._original, backup, self._before_hash)
        if self._final is not None:
            assert temporary is not None
            self._validate_ancestry()
            self._backend.validate_file(self._final, temporary, self._after_hash)
            self._signal("before_publish")
            try:
                self._backend.rename_noreplace(self._final, self.target)
            except AtomicVaultCasConflict:
                raise
            except BaseException:
                self._publish_uncertain = True
                raise
            self._published = True
            self._signal("published")
            self._backend.validate_file(self._final, self.target, self._after_hash)

    def verify_committed(self) -> None:
        self._validate_ancestry()
        self._signal("before_finalize")
        if self._after_hash == ABSENT_HASH:
            _require_absent(self.target, "deleted Vault target reappeared before commit")
        else:
            if self._final is None or not self._published:
                raise AtomicVaultCasUncertain("published Vault handle is missing")
            self._backend.validate_file(self._final, self.target, self._after_hash)
        if self._claimed:
            if self._original is None or self._backup is None:
                raise AtomicVaultCasUncertain("claimed original Vault handle is missing")
            self._backend.validate_file(self._original, self._backup, self._before_hash)

    def rollback(self) -> None:
        if self._closed:
            raise AtomicVaultCasUncertain("CAS rollback lease is already closed")
        self._validate_ancestry()
        try:
            if self._published:
                if self._final is None or self._rollback_transient is None:
                    raise AtomicVaultCasUncertain("CAS rollback final handle is missing")
                self._backend.validate_file(self._final, self.target, self._after_hash)
                self._signal("before_rollback_claim")
                self._backend.rename_noreplace(self._final, self._rollback_transient)
                self._published = False
                self._rollback_claimed = True
                self._signal("rollback_final_claimed")
                self._backend.validate_file(self._final, self._rollback_transient, self._after_hash)
            else:
                _require_absent(self.target, "rollback target was occupied by an external file")

            if self._claimed:
                if self._original is None or self._backup is None:
                    raise AtomicVaultCasUncertain("CAS rollback original handle is missing")
                _require_absent(self.target, "rollback refused to replace an external target")
                self._signal("before_rollback_restore")
                self._backend.rename_noreplace(self._original, self.target)
                self._claimed = False
                self._backend.validate_file(self._original, self.target, self._before_hash)
            else:
                _require_absent(self.target, "rollback of a create left a target behind")

            if self._final is not None:
                self._backend.delete_locked(self._final)
                self._final = None
                self._rollback_claimed = False
        finally:
            if not self._claimed and not self._published:
                self.close_preserving()

    def discard_unmutated(self) -> None:
        if self.mutated:
            raise AtomicVaultCasUncertain("mutated CAS change cannot be discarded")
        try:
            if self._final is not None:
                self._backend.delete_locked(self._final)
                self._final = None
        finally:
            self.close_preserving()

    def cleanup_committed(self) -> None:
        if self._closed:
            raise AtomicVaultCasUncertain("CAS commit lease is already closed")
        try:
            if self._claimed:
                if self._original is None:
                    raise AtomicVaultCasUncertain("CAS commit backup handle is missing")
                self._backend.delete_locked(self._original)
                self._original = None
                self._claimed = False
        finally:
            self.close_preserving()

    def close_preserving(self) -> None:
        if self._closed:
            return
        self._closed = True
        first_error: BaseException | None = None
        entries: list[_LockedEntry] = []
        if self._final is not None:
            entries.append(self._final)
        if self._original is not None:
            entries.append(self._original)
        entries.extend(entry for entry, _expected in reversed(self._ancestry))
        for entry in entries:
            try:
                self._backend.close(entry)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def _validate_ancestry(self) -> None:
        for entry, expected in self._ancestry:
            self._backend.validate_directory(entry, expected)

    def _signal(self, stage: str) -> None:
        if self._barrier is not None:
            self._barrier(stage, self.relative_path)


class AtomicVaultCas:
    """Factory for per-path CAS leases bound to one immutable Vault root."""

    def __init__(
        self,
        *,
        root: Path,
        max_file_bytes: int,
        barrier: VaultCasBarrier | None = None,
    ) -> None:
        self._root = root
        self._barrier = barrier
        if os.name == "nt":
            self._backend: _WindowsCasBackend | _PosixCasBackend = _WindowsCasBackend(
                root,
                max_file_bytes,
            )
        else:
            self._backend = _PosixCasBackend(root, max_file_bytes)

    def begin(
        self,
        *,
        relative_path: str,
        target: Path,
        expected_root_identity: tuple[int, int],
        parent_paths: Sequence[tuple[Path, tuple[int, int]]],
        expected_exists: bool,
        before_hash: str,
        after_hash: str,
    ) -> AtomicVaultChange:
        ancestry: list[tuple[_LockedEntry, tuple[int, int]]] = []
        original: _LockedEntry | None = None
        completed = False
        try:
            root = self._backend.lock_directory(self._root, expected_root_identity)
            ancestry.append((root, expected_root_identity))
            for path, identity in parent_paths:
                entry = self._backend.lock_directory(path, identity)
                if entry.identity.volume != root.identity.volume:
                    raise AtomicVaultCasConflict("Vault parent crossed a filesystem volume")
                ancestry.append((entry, identity))
            self._signal("ancestry_locked", relative_path)
            if expected_exists:
                original = self._backend.lock_file(target, before_hash)
                if original.identity.volume != root.identity.volume:
                    raise AtomicVaultCasConflict("Vault target crossed a filesystem volume")
            else:
                _require_absent(target, "Vault target appeared before CAS")
            self._signal("target_locked", relative_path)
            change = AtomicVaultChange(
                backend=self._backend,
                relative_path=relative_path,
                target=target,
                ancestry=ancestry,
                original=original,
                before_hash=before_hash,
                after_hash=after_hash,
                barrier=self._barrier,
            )
            completed = True
            return change
        finally:
            if not completed:
                first_error: BaseException | None = None
                if original is not None:
                    try:
                        self._backend.close(original)
                    except BaseException as error:
                        first_error = error
                for entry, _expected in reversed(ancestry):
                    try:
                        self._backend.close(entry)
                    except BaseException as error:
                        if first_error is None:
                            first_error = error
                if first_error is not None and sys.exc_info()[0] is None:
                    raise first_error

    def discard_temporary(self, path: Path, expected_hash: str) -> None:
        """Delete an uncommitted temporary only through its verified handle."""

        try:
            entry = self._backend.lock_file(path, expected_hash)
        except AtomicVaultCasConflict:
            try:
                path.lstat()
            except FileNotFoundError:
                return
            raise
        self._backend.delete_locked(entry)

    def recover_rename_noreplace(
        self,
        *,
        source: Path,
        destination: Path,
        expected_root_identity: tuple[int, int],
        parent_paths: Sequence[tuple[Path, tuple[int, int]]],
        expected_hash: str,
        expected_identity: tuple[int, int],
    ) -> None:
        """Move one manifest-bound recovery file without replacing any name.

        Recovery uses the same handle validation and native no-replace rename
        primitive as the live CAS.  An identity/hash match by pathname alone is
        never sufficient authorization to move a file.
        """

        if _path_key(source.parent) != _path_key(destination.parent):
            raise AtomicVaultCasConflict("recovery rename must remain in one verified Vault directory")
        ancestry, entry = self._lock_recovery_file(
            source=source,
            expected_root_identity=expected_root_identity,
            parent_paths=parent_paths,
            expected_hash=expected_hash,
            expected_identity=expected_identity,
        )
        try:
            _require_absent(destination, "recovery refused to replace an existing Vault path")
            self._backend.rename_noreplace(entry, destination)
            self._backend.validate_file(entry, destination, expected_hash)
        finally:
            first_error: BaseException | None = None
            try:
                self._backend.close(entry)
            except BaseException as error:
                first_error = error
            for ancestor, _expected in reversed(ancestry):
                try:
                    self._backend.close(ancestor)
                except BaseException as error:
                    if first_error is None:
                        first_error = error
            if first_error is not None and sys.exc_info()[0] is None:
                raise first_error

    def recover_delete(
        self,
        *,
        path: Path,
        expected_root_identity: tuple[int, int],
        parent_paths: Sequence[tuple[Path, tuple[int, int]]],
        expected_hash: str,
        expected_identity: tuple[int, int],
    ) -> None:
        """Delete one manifest-bound recovery file through its verified handle."""

        ancestry, entry = self._lock_recovery_file(
            source=path,
            expected_root_identity=expected_root_identity,
            parent_paths=parent_paths,
            expected_hash=expected_hash,
            expected_identity=expected_identity,
        )
        try:
            self._backend.delete_locked(entry)
        finally:
            first_error: BaseException | None = None
            if not entry.closed:
                try:
                    self._backend.close(entry)
                except BaseException as error:
                    first_error = error
            for ancestor, _expected in reversed(ancestry):
                try:
                    self._backend.close(ancestor)
                except BaseException as error:
                    if first_error is None:
                        first_error = error
            if first_error is not None and sys.exc_info()[0] is None:
                raise first_error

    def _lock_recovery_file(
        self,
        *,
        source: Path,
        expected_root_identity: tuple[int, int],
        parent_paths: Sequence[tuple[Path, tuple[int, int]]],
        expected_hash: str,
        expected_identity: tuple[int, int],
    ) -> tuple[list[tuple[_LockedEntry, tuple[int, int]]], _LockedEntry]:
        source_key = _path_key(source)
        root_key = _path_key(self._root)
        if not source_key.startswith(root_key + os.sep):
            raise AtomicVaultCasConflict("recovery source escaped the authorized Vault root")
        ancestry: list[tuple[_LockedEntry, tuple[int, int]]] = []
        entry: _LockedEntry | None = None
        try:
            root = self._backend.lock_directory(self._root, expected_root_identity)
            ancestry.append((root, expected_root_identity))
            for path, identity in parent_paths:
                parent = self._backend.lock_directory(path, identity)
                if parent.identity.volume != root.identity.volume:
                    raise AtomicVaultCasConflict("recovery parent crossed a filesystem volume")
                ancestry.append((parent, identity))
            entry = self._backend.lock_file(source, expected_hash)
            # Manifest identities use Python's stable ``st_dev/st_ino`` pair,
            # matching live preflight/root/parent identities.  On Windows the
            # native volume serial returned by GetFileInformationByHandle is
            # not encoded like ``st_dev``; the backend already proves that the
            # opened handle's file ID equals this pathname's ``st_ino``.
            observed_info = source.lstat()
            observed = int(observed_info.st_dev), int(observed_info.st_ino)
            if observed != expected_identity:
                raise AtomicVaultCasConflict("recovery file identity differs from the durable manifest")
            if entry.identity.volume != root.identity.volume:
                raise AtomicVaultCasConflict("recovery file crossed a filesystem volume")
            return ancestry, entry
        except BaseException:
            if entry is not None:
                try:
                    self._backend.close(entry)
                except BaseException:
                    pass
            for ancestor, _expected in reversed(ancestry):
                try:
                    self._backend.close(ancestor)
                except BaseException:
                    pass
            raise

    def _signal(self, stage: str, relative_path: str) -> None:
        if self._barrier is not None:
            self._barrier(stage, relative_path)


def _require_absent(path: Path, message: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise AtomicVaultCasUncertain(f"{message}: existence is unconfirmed") from error
    raise AtomicVaultCasConflict(message)


def _validate_posix_stat(info: os.stat_result, path: Path, *, directory: bool) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise AtomicVaultCasConflict(f"reparse point is forbidden: {path.name}")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise AtomicVaultCasConflict(f"CAS directory type changed: {path.name}")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise AtomicVaultCasConflict(f"CAS file type changed: {path.name}")


def _posix_identity(info: os.stat_result) -> _NativeIdentity:
    return _NativeIdentity(
        volume=int(info.st_dev),
        file_id=int(info.st_ino),
        links=int(info.st_nlink),
        attributes=_FILE_ATTRIBUTE_DIRECTORY if stat.S_ISDIR(info.st_mode) else 0,
        size=int(info.st_size),
    )


def _native_rename_noreplace(source: Path, destination: Path) -> None:
    encoded_source = os.fsencode(source)
    encoded_destination = os.fsencode(destination)
    library = ctypes.CDLL(None, use_errno=True)
    result: int
    if sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        renameat2 = library.renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = int(renameat2(-100, encoded_source, -100, encoded_destination, 1))
    elif sys.platform == "darwin" and hasattr(library, "renamex_np"):
        renamex_np = library.renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = int(renamex_np(encoded_source, encoded_destination, 0x00000004))
    else:
        raise AtomicVaultCasError("platform has no audited atomic no-replace rename primitive")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY, errno.ENOENT, errno.EBUSY}:
        raise AtomicVaultCasConflict(f"atomic no-replace rename was refused: {os.strerror(error_number)}")
    raise AtomicVaultCasError(f"atomic no-replace rename failed: {os.strerror(error_number)}")


def _extended_local_path(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _path_key(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def _windows_error(message: str, *, conflict: bool = False) -> AtomicVaultCasError:
    winerror = ctypes.get_last_error()
    error: AtomicVaultCasError
    if conflict:
        error = AtomicVaultCasConflict(f"{message} (Win32 {winerror})")
    else:
        error = AtomicVaultCasError(f"{message} (Win32 {winerror})")
    return error


__all__ = [
    "AtomicVaultCas",
    "AtomicVaultCasConflict",
    "AtomicVaultCasError",
    "AtomicVaultCasUncertain",
    "AtomicVaultChange",
    "VaultCasBarrier",
]
