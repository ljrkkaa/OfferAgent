"""Small stdlib-only Windows identity and security-descriptor helpers."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from typing import Any

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1
_SE_KERNEL_OBJECT = 6
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_HANDLE_FLAG_INHERIT = 0x00000001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_INVALID_PARAMETER = 87
_ERROR_NOT_FOUND = 1168


class WindowsSecurityError(OSError):
    pass


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("user", _SidAndAttributes)]


class SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.DWORD),
        ("security_descriptor", ctypes.c_void_p),
        ("inherit_handle", wintypes.BOOL),
    ]


@dataclass(frozen=True)
class CurrentWindowsIdentity:
    sid: str


class WindowsProcessIdentityState(str, Enum):
    """Trustworthy relationship between a PID and the current Windows SID.

    ``UNKNOWN`` is intentionally distinct from ``NOT_FOUND`` so callers never
    turn an access-denied or transient inspection failure into proof that a
    process is dead.
    """

    CURRENT_USER = "current_user"
    OTHER_USER = "other_user"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


def current_windows_identity() -> CurrentWindowsIdentity:
    if os.name != "nt":
        raise WindowsSecurityError("Windows identity is only available on Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _last_error("OpenProcessToken")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise _last_error("GetTokenInformation(size)")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            ctypes.cast(buffer, ctypes.c_void_p),
            required,
            ctypes.byref(required),
        ):
            raise _last_error("GetTokenInformation")
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_string = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(token_user.user.sid, ctypes.byref(sid_string)):
            raise _last_error("ConvertSidToStringSidW")
        try:
            sid = sid_string.value
            if not sid:
                raise WindowsSecurityError("ConvertSidToStringSidW returned an empty SID")
            return CurrentWindowsIdentity(sid=sid)
        finally:
            kernel32.LocalFree(ctypes.cast(sid_string, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def windows_process_identity_state(process_id: int) -> WindowsProcessIdentityState:
    """Inspect a PID through an open process handle without trusting PID text.

    A live handle prevents PID reuse while its token is inspected.  Only an
    explicit missing-process result or a successfully inspected different SID
    is negative evidence; every other Win32 failure is fail-closed ``UNKNOWN``.
    """

    if os.name != "nt":
        raise WindowsSecurityError("Windows process identity is only available on Windows")
    if process_id < 1 or process_id > 0xFFFFFFFF:
        raise ValueError("process_id must be a positive Windows DWORD")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    ctypes.set_last_error(0)
    process = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not process:
        error = ctypes.get_last_error()
        if error in {_ERROR_INVALID_PARAMETER, _ERROR_NOT_FOUND}:
            return WindowsProcessIdentityState.NOT_FOUND
        return WindowsProcessIdentityState.UNKNOWN

    token = wintypes.HANDLE()
    try:
        ctypes.set_last_error(0)
        if not advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
            # Access denied is common for protected system processes and is not
            # evidence that a same-SID Obsidian process has exited.
            return WindowsProcessIdentityState.UNKNOWN
        try:
            sid = _token_user_sid(advapi32, kernel32, token)
        except WindowsSecurityError:
            return WindowsProcessIdentityState.UNKNOWN
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)

    try:
        current_sid = current_windows_identity().sid
    except WindowsSecurityError:
        return WindowsProcessIdentityState.UNKNOWN
    if sid == current_sid:
        return WindowsProcessIdentityState.CURRENT_USER
    return WindowsProcessIdentityState.OTHER_USER


def _token_user_sid(advapi32: Any, kernel32: Any, token: wintypes.HANDLE) -> str:
    required = wintypes.DWORD()
    ctypes.set_last_error(0)
    advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
    if required.value == 0:
        raise _last_error("GetTokenInformation(size)")
    buffer = ctypes.create_string_buffer(required.value)
    if not advapi32.GetTokenInformation(
        token,
        _TOKEN_USER,
        ctypes.cast(buffer, ctypes.c_void_p),
        required,
        ctypes.byref(required),
    ):
        raise _last_error("GetTokenInformation")
    token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
    sid_string = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(token_user.user.sid, ctypes.byref(sid_string)):
        raise _last_error("ConvertSidToStringSidW")
    try:
        sid = sid_string.value
        if not sid:
            raise WindowsSecurityError("ConvertSidToStringSidW returned an empty SID")
        return sid
    finally:
        kernel32.LocalFree(ctypes.cast(sid_string, ctypes.c_void_p))


def protect_current_user_path(path: os.PathLike[str] | str, *, directory: bool) -> None:
    """Replace a filesystem DACL with a protected current-user-only ACL."""

    if os.name != "nt":
        raise WindowsSecurityError("Windows filesystem ACLs are only available on Windows")
    resolved = os.path.abspath(os.fspath(path))
    if not os.path.exists(resolved):
        raise FileNotFoundError(resolved)
    sid = current_windows_identity().sid
    ace_flags = "OICI" if directory else ""
    sddl = f"D:P(A;{ace_flags};GA;;;{sid})"
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        None,
    ):
        raise _last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        information = _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
        if not advapi32.SetFileSecurityW(resolved, information, descriptor):
            raise _last_error("SetFileSecurityW")
    finally:
        kernel32.LocalFree(descriptor)


def kernel_object_security_sddl(handle: int) -> str:
    """Return the DACL SDDL for a diagnostic/test kernel object handle."""

    if os.name != "nt":
        raise WindowsSecurityError("Windows kernel object security is only available on Windows")
    if handle <= 0:
        raise ValueError("kernel object handle must be positive")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    status = int(
        advapi32.GetSecurityInfo(
            wintypes.HANDLE(handle),
            _SE_KERNEL_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
    )
    if status != 0:
        raise WindowsSecurityError(status, f"GetSecurityInfo failed: {ctypes.FormatError(status)}")
    try:
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            _SDDL_REVISION_1,
            _DACL_SECURITY_INFORMATION,
            ctypes.byref(text),
            None,
        ):
            raise _last_error("ConvertSecurityDescriptorToStringSecurityDescriptorW")
        try:
            value = text.value
            if not value:
                raise WindowsSecurityError("security descriptor converted to empty SDDL")
            return value
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.LocalFree(descriptor)


def kernel_handle_is_inheritable(handle: int) -> bool:
    """Inspect HANDLE_FLAG_INHERIT without mutating the kernel object."""

    if os.name != "nt":
        raise WindowsSecurityError("Windows handles are only available on Windows")
    if handle <= 0:
        raise ValueError("kernel object handle must be positive")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetHandleInformation.restype = wintypes.BOOL
    flags = wintypes.DWORD()
    if not kernel32.GetHandleInformation(wintypes.HANDLE(handle), ctypes.byref(flags)):
        raise _last_error("GetHandleInformation")
    return bool(flags.value & _HANDLE_FLAG_INHERIT)


@contextmanager
def current_user_security_attributes(*, allow_system: bool = True) -> Iterator[SecurityAttributes]:
    """Yield a non-inheritable DACL for the current SID and optional SYSTEM."""

    if os.name != "nt":
        raise WindowsSecurityError("Windows security descriptors are only available on Windows")
    sid = current_windows_identity().sid
    system_ace = "(A;;GA;;;SY)" if allow_system else ""
    sddl = f"D:P{system_ace}(A;;GA;;;{sid})"
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        None,
    ):
        raise _last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    attributes = SecurityAttributes(
        length=ctypes.sizeof(SecurityAttributes),
        security_descriptor=descriptor,
        inherit_handle=False,
    )
    try:
        yield attributes
    finally:
        kernel32.LocalFree(descriptor)


def _last_error(operation: str) -> WindowsSecurityError:
    code = ctypes.get_last_error()
    return WindowsSecurityError(code, f"{operation} failed: {ctypes.FormatError(code)}")


__all__ = [
    "CurrentWindowsIdentity",
    "SecurityAttributes",
    "WindowsProcessIdentityState",
    "WindowsSecurityError",
    "current_user_security_attributes",
    "current_windows_identity",
    "kernel_handle_is_inheritable",
    "kernel_object_security_sddl",
    "protect_current_user_path",
    "windows_process_identity_state",
]
