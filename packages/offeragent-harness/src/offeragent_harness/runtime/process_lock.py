"""Current-user Host and per-workspace Worker single-instance locks."""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
from ctypes import wintypes
from enum import Enum
from typing import Protocol

from .windows_security import current_user_security_attributes, current_windows_identity

_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF


class ProcessLockError(RuntimeError):
    pass


class ProcessAlreadyRunning(ProcessLockError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"runtime owner already holds mutex {name}")


class MutexWaitResult(str, Enum):
    ACQUIRED = "acquired"
    ABANDONED = "abandoned"
    TIMEOUT = "timeout"


class MutexBackend(Protocol):
    def create(self, name: str) -> int: ...

    def wait(self, handle: int, timeout_ms: int) -> MutexWaitResult: ...

    def release(self, handle: int) -> None: ...

    def close(self, handle: int) -> None: ...


class WindowsMutexBackend:
    def __init__(self) -> None:
        if os.name != "nt":
            raise ProcessLockError("Windows named mutexes are only available on Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self._kernel32.ReleaseMutex.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def create(self, name: str) -> int:
        # Host/Worker ownership is deliberately narrower than a service-style
        # object: only the interactive Windows SID may open the mutex.
        with current_user_security_attributes(allow_system=False) as attributes:
            handle = self._kernel32.CreateMutexW(ctypes.byref(attributes), False, name)
        if not handle:
            raise _mutex_error("CreateMutexW")
        return int(handle)

    def wait(self, handle: int, timeout_ms: int) -> MutexWaitResult:
        result = int(self._kernel32.WaitForSingleObject(wintypes.HANDLE(handle), timeout_ms))
        if result == _WAIT_OBJECT_0:
            return MutexWaitResult.ACQUIRED
        if result == _WAIT_ABANDONED:
            return MutexWaitResult.ABANDONED
        if result == _WAIT_TIMEOUT:
            return MutexWaitResult.TIMEOUT
        if result == _WAIT_FAILED:
            raise _mutex_error("WaitForSingleObject")
        raise ProcessLockError(f"WaitForSingleObject returned unexpected status 0x{result:08x}")

    def release(self, handle: int) -> None:
        if not self._kernel32.ReleaseMutex(wintypes.HANDLE(handle)):
            raise _mutex_error("ReleaseMutex")

    def close(self, handle: int) -> None:
        if not self._kernel32.CloseHandle(wintypes.HANDLE(handle)):
            raise _mutex_error("CloseHandle")


class ProcessLock:
    """RAII wrapper around a non-inheritable, current-user named mutex."""

    def __init__(self, name: str, *, backend: MutexBackend | None = None) -> None:
        if not re.fullmatch(r"Local\\OfferAgent\.[A-Za-z0-9_.-]{1,180}", name):
            raise ValueError("invalid OfferAgent mutex name")
        self.name = name
        self._backend = backend or WindowsMutexBackend()
        self._handle: int | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self, *, timeout_ms: int = 0) -> MutexWaitResult:
        if timeout_ms < 0:
            raise ValueError("timeout_ms must be non-negative")
        if self._handle is not None:
            raise ProcessLockError("this ProcessLock instance is already acquired")
        handle = self._backend.create(self.name)
        try:
            result = self._backend.wait(handle, timeout_ms)
            if result is MutexWaitResult.TIMEOUT:
                raise ProcessAlreadyRunning(self.name)
            self._handle = handle
            return result
        except BaseException:
            self._backend.close(handle)
            raise

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            self._backend.release(handle)
        finally:
            self._handle = None
            self._backend.close(handle)

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def host_mutex_name(*, sid: str | None = None) -> str:
    return _mutex_name("Host", sid=sid)


def installer_mutex_name(*, sid: str | None = None) -> str:
    """Return the current-user mutex shared by plugin and Setup installs."""

    return _mutex_name("Installer", sid=sid)


def installation_ledger_mutex_name(*, sid: str | None = None) -> str:
    """Return the current-user mutex serializing installer ledger operations."""

    return _mutex_name("InstallationLedger", sid=sid)


def worker_mutex_name(workspace_identity_hash: str, *, sid: str | None = None) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", workspace_identity_hash):
        raise ValueError("workspace identity must be a canonical sha256 digest")
    workspace_fragment = workspace_identity_hash.removeprefix("sha256:")[:32]
    return _mutex_name(f"Worker.{workspace_fragment}", sid=sid)


def _mutex_name(role: str, *, sid: str | None) -> str:
    actual_sid = sid or current_windows_identity().sid
    if not re.fullmatch(r"S-\d-(?:\d+-)+\d+", actual_sid):
        raise ValueError("invalid Windows SID")
    sid_hash = hashlib.sha256(actual_sid.encode("ascii")).hexdigest()[:24]
    return f"Local\\OfferAgent.{role}.{sid_hash}"


def _mutex_error(operation: str) -> ProcessLockError:
    code = ctypes.get_last_error()
    return ProcessLockError(f"{operation} failed with Win32 error {code}: {ctypes.FormatError(code)}")


__all__ = [
    "MutexBackend",
    "MutexWaitResult",
    "ProcessAlreadyRunning",
    "ProcessLock",
    "ProcessLockError",
    "WindowsMutexBackend",
    "host_mutex_name",
    "installation_ledger_mutex_name",
    "installer_mutex_name",
    "worker_mutex_name",
]
