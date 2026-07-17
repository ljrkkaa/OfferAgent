"""Win32 executable verification and Job Object primitives for local processes."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import re
import secrets
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any, Protocol

from .process_identity import (
    ManagedWorkerProcess,
    SupervisedWorkspaceIdentity,
)
from .windows_security import current_user_security_attributes

_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESHOWWINDOW = 0x00000001
_SW_HIDE = 0
_HANDLE_FLAG_INHERIT = 0x00000001
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF
_STILL_ACTIVE = 259
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
_FILE_BEGIN = 0
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_SELF_TEST_NONCE = re.compile(r"[0-9a-f]{16,64}")

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
_JOB_LIMIT_FLAGS = _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

_WTD_UI_NONE = 2
_WTD_REVOKE_NONE = 0
_WTD_CHOICE_FILE = 1
_WTD_STATEACTION_VERIFY = 1
_WTD_STATEACTION_CLOSE = 2
_WTD_CACHE_ONLY_URL_RETRIEVAL = 0x00001000
_WTD_SAFER_FLAG = 0x00000100

_MINIMAL_ENVIRONMENT_KEYS = (
    "APPDATA",
    "LOCALAPPDATA",
    "SystemRoot",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)


class WindowsProcessError(OSError):
    pass


class ExecutableVerificationError(WindowsProcessError):
    pass


class AuthenticodeVerifier(Protocol):
    def verify(self, executable: Path) -> bool: ...


class _StartupInfoW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR),
        ("title", wintypes.LPWSTR),
        ("x", wintypes.DWORD),
        ("y", wintypes.DWORD),
        ("x_size", wintypes.DWORD),
        ("y_size", wintypes.DWORD),
        ("x_count_chars", wintypes.DWORD),
        ("y_count_chars", wintypes.DWORD),
        ("fill_attribute", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("show_window", wintypes.WORD),
        ("reserved2", wintypes.WORD),
        ("reserved2_data", ctypes.POINTER(ctypes.c_ubyte)),
        ("stdin", wintypes.HANDLE),
        ("stdout", wintypes.HANDLE),
        ("stderr", wintypes.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("process", wintypes.HANDLE),
        ("thread", wintypes.HANDLE),
        ("process_id", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
    ]


class _StartupInfoExW(ctypes.Structure):
    _fields_ = [("startup_info", _StartupInfoW), ("attribute_list", ctypes.c_void_p)]


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    ]


class _WinTrustFileInfo(ctypes.Structure):
    _fields_ = [
        ("cb_struct", wintypes.DWORD),
        ("file_path", wintypes.LPCWSTR),
        ("file_handle", wintypes.HANDLE),
        ("known_subject", ctypes.POINTER(_Guid)),
    ]


class _WinTrustData(ctypes.Structure):
    _fields_ = [
        ("cb_struct", wintypes.DWORD),
        ("policy_callback_data", ctypes.c_void_p),
        ("sip_client_data", ctypes.c_void_p),
        ("ui_choice", wintypes.DWORD),
        ("revocation_checks", wintypes.DWORD),
        ("union_choice", wintypes.DWORD),
        ("file_info", ctypes.POINTER(_WinTrustFileInfo)),
        ("state_action", wintypes.DWORD),
        ("state_data", wintypes.HANDLE),
        ("url_reference", wintypes.LPWSTR),
        ("provider_flags", wintypes.DWORD),
        ("ui_context", wintypes.DWORD),
        ("signature_settings", ctypes.c_void_p),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _JobBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("total_user_time", ctypes.c_longlong),
        ("total_kernel_time", ctypes.c_longlong),
        ("this_period_total_user_time", ctypes.c_longlong),
        ("this_period_total_kernel_time", ctypes.c_longlong),
        ("total_page_fault_count", wintypes.DWORD),
        ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD),
        ("total_terminated_processes", wintypes.DWORD),
    ]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


_WINTRUST_ACTION_GENERIC_VERIFY_V2 = _Guid(
    0x00AAC56B,
    0xCD44,
    0x11D0,
    (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
)

_FOLDERID_PROFILE = "5e6c858f-0e22-4760-9afe-ea3317b67173"
_FOLDERID_LOCAL_APP_DATA = "f1b32785-6fba-4fcf-9d55-7b8e7f157091"
_FOLDERID_ROAMING_APP_DATA = "3eb685db-65f9-4cf6-a03a-e3ef65729f3d"


class WindowsAuthenticodeVerifier:
    """Offline Authenticode verification through WinVerifyTrust."""

    def __init__(self) -> None:
        _require_windows()
        self._wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
        self._wintrust.WinVerifyTrust.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(_Guid),
            ctypes.POINTER(_WinTrustData),
        ]
        self._wintrust.WinVerifyTrust.restype = ctypes.c_long

    def verify(self, executable: Path) -> bool:
        file_info = _WinTrustFileInfo(
            cb_struct=ctypes.sizeof(_WinTrustFileInfo),
            file_path=str(executable),
            file_handle=None,
            known_subject=None,
        )
        data = _WinTrustData(
            cb_struct=ctypes.sizeof(_WinTrustData),
            policy_callback_data=None,
            sip_client_data=None,
            ui_choice=_WTD_UI_NONE,
            revocation_checks=_WTD_REVOKE_NONE,
            union_choice=_WTD_CHOICE_FILE,
            file_info=ctypes.pointer(file_info),
            state_action=_WTD_STATEACTION_VERIFY,
            state_data=None,
            url_reference=None,
            provider_flags=_WTD_CACHE_ONLY_URL_RETRIEVAL | _WTD_SAFER_FLAG,
            ui_context=0,
            signature_settings=None,
        )
        status = int(
            self._wintrust.WinVerifyTrust(
                None,
                ctypes.byref(_WINTRUST_ACTION_GENERIC_VERIFY_V2),
                ctypes.byref(data),
            )
        )
        try:
            return status == 0
        finally:
            data.state_action = _WTD_STATEACTION_CLOSE
            self._wintrust.WinVerifyTrust(
                None,
                ctypes.byref(_WINTRUST_ACTION_GENERIC_VERIFY_V2),
                ctypes.byref(data),
            )


class VerifiedExecutableLease:
    """A read-only handle that denies write/delete sharing until spawn commits."""

    def __init__(
        self,
        *,
        kernel32: Any,
        path: Path,
        handle: int,
        file_identity: tuple[int, int],
        content_sha256: str,
    ) -> None:
        self._kernel32 = kernel32
        self.path = path
        self.handle = handle
        self.file_identity = file_identity
        self.content_sha256 = content_sha256
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle = self.handle
        self.handle = 0
        if handle and not self._kernel32.CloseHandle(wintypes.HANDLE(handle)):
            raise _last_error("CloseHandle(verified executable)")

    def identity_unchanged(self) -> bool:
        if self._closed:
            raise ExecutableVerificationError("verified executable lease is closed")
        return _file_identity(self._kernel32, self.handle) == self.file_identity

    def content_unchanged(self) -> bool:
        if self._closed:
            raise ExecutableVerificationError("verified executable lease is closed")
        return _sha256_handle(self._kernel32, self.handle) == self.content_sha256

    def __enter__(self) -> VerifiedExecutableLease:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class WindowsWorkerJob:
    """Current-SID Job with kill-on-close and no breakaway permission."""

    def __init__(self, workspace: SupervisedWorkspaceIdentity) -> None:
        _require_windows()
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()
        root_fragment = workspace.canonical_root_identity.removeprefix("sha256:")[:32]
        name = f"Local\\OfferAgent.Job.{root_fragment}.{secrets.token_hex(8)}"
        with current_user_security_attributes(allow_system=False) as attributes:
            handle = self._kernel32.CreateJobObjectW(ctypes.byref(attributes), name)
        if not handle:
            raise _last_error("CreateJobObjectW")
        self._handle = int(handle)
        self._closed = False
        information = _JobExtendedLimitInformation()
        information.basic_limit_information.limit_flags = _JOB_LIMIT_FLAGS
        if not self._kernel32.SetInformationJobObject(
            wintypes.HANDLE(self._handle),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = _last_error("SetInformationJobObject")
            self._kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = 0
            self._closed = True
            raise error

    @property
    def configured_limit_flags(self) -> int:
        return _JOB_LIMIT_FLAGS

    @property
    def native_job_handle(self) -> int:
        self._require_open()
        return self._handle

    def assign(self, process: ManagedWorkerProcess) -> None:
        self.assign_process_handle(process.native_process_handle)

    def assign_process_handle(self, process_handle: int) -> None:
        self._require_open()
        if process_handle <= 0:
            raise ValueError("process handle must be positive")
        if not self._kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self._handle),
            wintypes.HANDLE(process_handle),
        ):
            raise _last_error("AssignProcessToJobObject")

    def contains_process_handle(self, process_handle: int) -> bool:
        self._require_open()
        result = wintypes.BOOL()
        if not self._kernel32.IsProcessInJob(
            wintypes.HANDLE(process_handle),
            wintypes.HANDLE(self._handle),
            ctypes.byref(result),
        ):
            raise _last_error("IsProcessInJob")
        return bool(result.value)

    def terminate_tree(self, exit_code: int) -> None:
        self._require_open()
        if not 0 <= exit_code <= 0xFFFFFFFF:
            raise ValueError("exit code must fit an unsigned DWORD")
        if not self._kernel32.TerminateJobObject(wintypes.HANDLE(self._handle), exit_code):
            raise _last_error("TerminateJobObject")

    async def wait_empty(self, timeout_seconds: float) -> bool:
        self._require_open()
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        return await asyncio.to_thread(self._wait_empty_sync, timeout_seconds)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle = self._handle
        self._handle = 0
        if handle and not self._kernel32.CloseHandle(wintypes.HANDLE(handle)):
            raise _last_error("CloseHandle(Job)")

    def _wait_empty_sync(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while True:
            accounting = _JobBasicAccountingInformation()
            returned = wintypes.DWORD()
            if not self._kernel32.QueryInformationJobObject(
                wintypes.HANDLE(self._handle),
                _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(accounting),
                ctypes.sizeof(accounting),
                ctypes.byref(returned),
            ):
                raise _last_error("QueryInformationJobObject")
            if accounting.active_processes == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def _configure_api(self) -> None:
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        self._kernel32.IsProcessInJob.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def _require_open(self) -> None:
        if self._closed:
            raise WindowsProcessError("Worker Job handle is closed")


def current_user_profile_directory() -> Path:
    """Return the current Windows user's canonical Profile Known Folder."""

    _require_windows()
    return _known_folder(_FOLDERID_PROFILE)


def _sha256_handle(kernel32: Any, handle: int) -> str:
    digest = hashlib.sha256()
    position = ctypes.c_longlong()
    if not kernel32.SetFilePointerEx(
        wintypes.HANDLE(handle),
        0,
        ctypes.byref(position),
        _FILE_BEGIN,
    ):
        raise _last_error("SetFilePointerEx(verified executable)")
    buffer = ctypes.create_string_buffer(1024 * 1024)
    while True:
        read = wintypes.DWORD()
        if not kernel32.ReadFile(
            wintypes.HANDLE(handle),
            buffer,
            len(buffer),
            ctypes.byref(read),
            None,
        ):
            raise _last_error("ReadFile(verified executable)")
        if read.value == 0:
            break
        digest.update(buffer.raw[: read.value])
    return digest.hexdigest()


def _file_identity(kernel32: Any, handle: int) -> tuple[int, int]:
    information = _ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(
        wintypes.HANDLE(handle),
        ctypes.byref(information),
    ):
        raise _last_error("GetFileInformationByHandle")
    index = (int(information.file_index_high) << 32) | int(information.file_index_low)
    return int(information.volume_serial_number), index


def _query_process_image_path(kernel32: Any, process_handle: int) -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    length = wintypes.DWORD(len(buffer))
    if not kernel32.QueryFullProcessImageNameW(
        wintypes.HANDLE(process_handle),
        0,
        buffer,
        ctypes.byref(length),
    ):
        raise _last_error("QueryFullProcessImageNameW")
    if length.value == 0:
        raise ExecutableVerificationError("suspended process reported an empty image path")
    return Path(buffer.value)


def _known_folder(identifier: str) -> Path:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_Guid),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    folder_id = _guid_from_uuid(uuid.UUID(identifier))
    value = wintypes.LPWSTR()
    status = int(shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(value)))
    if status != 0:
        raise WindowsProcessError(status, f"SHGetKnownFolderPath failed with HRESULT 0x{status & 0xFFFFFFFF:08x}")
    try:
        path = value.value
        if not path:
            raise WindowsProcessError("SHGetKnownFolderPath returned an empty path")
        return _canonical_directory(Path(path), label="Known Folder")
    finally:
        ole32.CoTaskMemFree(ctypes.cast(value, ctypes.c_void_p))


def _guid_from_uuid(value: uuid.UUID) -> _Guid:
    fields = value.fields
    data4 = value.bytes[8:]
    return _Guid(
        fields[0],
        fields[1],
        fields[2],
        (ctypes.c_ubyte * 8)(*data4),
    )


def _canonical_directory(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise WindowsProcessError(f"{label} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise WindowsProcessError(f"{label} does not exist") from error
    if not resolved.is_dir():
        raise WindowsProcessError(f"{label} is not a directory")
    return resolved


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsProcessError("Windows process supervision is only available on Windows")


def _last_error(operation: str) -> WindowsProcessError:
    code = ctypes.get_last_error()
    return _error_code(operation, code)


def _error_code(operation: str, code: int) -> WindowsProcessError:
    return WindowsProcessError(code, f"{operation} failed: {ctypes.FormatError(code)}")


__all__ = [
    "AuthenticodeVerifier",
    "ExecutableVerificationError",
    "VerifiedExecutableLease",
    "WindowsAuthenticodeVerifier",
    "WindowsProcessError",
    "WindowsWorkerJob",
    "current_user_profile_directory",
]
