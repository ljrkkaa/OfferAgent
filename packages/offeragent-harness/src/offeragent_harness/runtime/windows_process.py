"""Win32 process and Job Object backends for the local Worker supervisor."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import re
import secrets
import subprocess
import time
import uuid
from collections.abc import Mapping
from ctypes import wintypes
from pathlib import Path
from typing import Any, Protocol

from .host_supervisor import (
    ManagedWorkerProcess,
    SupervisedWorkspaceIdentity,
    VerifiedWorkerExecutable,
    WorkerJob,
    WorkerLaunchRequest,
)
from .windows_security import current_user_security_attributes, protect_current_user_path

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


class SignedReleaseManifestTrust(Protocol):
    """Trust root supplied by the signed updater/release manifest verifier."""

    def authorizes(self, expected: VerifiedWorkerExecutable) -> bool: ...


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


class PinnedWorkerExecutableVerifier:
    """Pin a signed-manifest release and lock its signed executable for spawn."""

    def __init__(
        self,
        *,
        manifest_trust: SignedReleaseManifestTrust,
        authenticode: AuthenticodeVerifier,
    ) -> None:
        _require_windows()
        self._manifest_trust = manifest_trust
        self._authenticode = authenticode
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
        self._kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self._kernel32.ReadFile.restype = wintypes.BOOL
        self._kernel32.SetFilePointerEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_longlong),
            wintypes.DWORD,
        ]
        self._kernel32.SetFilePointerEx.restype = wintypes.BOOL
        self._kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def open_verified(self, expected: VerifiedWorkerExecutable) -> VerifiedExecutableLease:
        if not self._manifest_trust.authorizes(expected):
            raise ExecutableVerificationError("Worker release is not authorized by the signed manifest trust root")
        if not expected.executable.is_absolute() or not expected.version_directory.is_absolute():
            raise ExecutableVerificationError("Worker executable and version directory must be absolute")
        try:
            version_directory = expected.version_directory.resolve(strict=True)
            executable = expected.executable.resolve(strict=True)
        except OSError as error:
            raise ExecutableVerificationError("verified Worker release path is missing") from error
        if not version_directory.is_dir() or not executable.is_file():
            raise ExecutableVerificationError("Worker release paths have the wrong type")
        if executable.suffix.lower() != ".exe":
            raise ExecutableVerificationError("production Worker executable must be an .exe")
        try:
            executable.relative_to(version_directory)
        except ValueError as error:
            raise ExecutableVerificationError("Worker executable escapes its verified version directory") from error
        handle = self._kernel32.CreateFileW(
            str(executable),
            _GENERIC_READ,
            _FILE_SHARE_READ,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_SEQUENTIAL_SCAN,
            None,
        )
        raw_handle = int(handle) if handle else 0
        if raw_handle == 0 or raw_handle == _INVALID_HANDLE_VALUE:
            raise _last_error("CreateFileW(verified executable)")
        try:
            digest = _sha256_handle(self._kernel32, raw_handle)
            if f"sha256:{digest}" != expected.file_sha256:
                raise ExecutableVerificationError("Worker executable does not match the signed release manifest")
            if not self._authenticode.verify(executable):
                raise ExecutableVerificationError("Worker executable failed offline Authenticode verification")
            identity = _file_identity(self._kernel32, raw_handle)
            return VerifiedExecutableLease(
                kernel32=self._kernel32,
                path=executable,
                handle=raw_handle,
                file_identity=identity,
                content_sha256=digest,
            )
        except BaseException:
            self._kernel32.CloseHandle(wintypes.HANDLE(raw_handle))
            raise


class WindowsWorkerProcessBackend:
    """Create a hidden, suspended Worker with fixed argv and no handle inheritance."""

    def __init__(
        self,
        *,
        verifier: PinnedWorkerExecutableVerifier,
        self_test_nonce: str | None = None,
    ) -> None:
        _require_windows()
        self._verifier = verifier
        self._environment = (
            trusted_worker_environment()
            if self_test_nonce is None
            else _isolated_self_test_worker_environment(self_test_nonce)
        )
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()

    def spawn_suspended(self, request: WorkerLaunchRequest, *, job: WorkerJob) -> ManagedWorkerProcess:
        with self._verifier.open_verified(request.executable) as verified:
            executable = verified.path
            arguments = fixed_worker_argv(request, executable=executable)
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
            environment_block = ctypes.create_unicode_buffer(_environment_block(self._environment))
            attribute_buffer, attribute_list, job_handles = self._job_attribute_list(job.native_job_handle)
            startup = _StartupInfoExW()
            startup.startup_info.cb = ctypes.sizeof(_StartupInfoExW)
            startup.startup_info.flags = _STARTF_USESHOWWINDOW
            startup.startup_info.show_window = _SW_HIDE
            startup.attribute_list = attribute_list
            process_info = _ProcessInformation()
            flags = _CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT
            create_error = 0
            try:
                created = self._kernel32.CreateProcessW(
                    str(executable),
                    command_line,
                    None,
                    None,
                    False,
                    flags,
                    environment_block,
                    str(request.executable.version_directory.resolve(strict=True)),
                    ctypes.cast(ctypes.byref(startup), ctypes.POINTER(_StartupInfoW)),
                    ctypes.byref(process_info),
                )
                if not created:
                    create_error = ctypes.get_last_error()
            finally:
                # Keep both buffers alive through CreateProcessW, then destroy
                # the attribute list.  The Worker is already in the Job when
                # CreateProcessW returns, so Host death cannot orphan it.
                self._kernel32.DeleteProcThreadAttributeList(attribute_list)
                del attribute_buffer, job_handles
            if not created:
                raise _error_code("CreateProcessW", create_error)
            process_handle = int(process_info.process)
            thread_handle = int(process_info.thread)
            try:
                _clear_inherit_flag(self._kernel32, process_handle)
                _clear_inherit_flag(self._kernel32, thread_handle)
                image_path = _query_process_image_path(self._kernel32, process_handle)
                if os.path.normcase(str(image_path.resolve(strict=True))) != os.path.normcase(str(executable)):
                    raise ExecutableVerificationError("suspended process image path differs from the locked executable")
                # The locked path cannot be written or replaced.  Re-reading
                # its file ID after CreateProcess additionally catches an
                # unexpected filesystem redirect before releasing the lease.
                if not verified.identity_unchanged():
                    raise ExecutableVerificationError("Worker executable identity changed during suspended spawn")
                if not verified.content_unchanged():
                    raise ExecutableVerificationError("Worker executable content changed during suspended spawn")
                if not job.contains_process_handle(process_handle):
                    raise WindowsProcessError("CreateProcessW did not atomically join the requested Job Object")
                return WindowsManagedWorkerProcess(
                    kernel32=self._kernel32,
                    pid=int(process_info.process_id),
                    process_handle=process_handle,
                    thread_handle=thread_handle,
                )
            except BaseException:
                self._kernel32.TerminateProcess(wintypes.HANDLE(process_handle), 0xED)
                self._kernel32.CloseHandle(wintypes.HANDLE(thread_handle))
                self._kernel32.CloseHandle(wintypes.HANDLE(process_handle))
                raise

    def _job_attribute_list(
        self,
        job_handle: int,
    ) -> tuple[ctypes.Array[ctypes.c_char], ctypes.c_void_p, ctypes.Array[wintypes.HANDLE]]:
        if job_handle <= 0:
            raise WindowsProcessError("Job Object handle must be positive")
        required = ctypes.c_size_t()
        self._kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(required))
        if required.value == 0:
            raise _last_error("InitializeProcThreadAttributeList(size)")
        buffer = ctypes.create_string_buffer(required.value)
        attribute_list = ctypes.cast(buffer, ctypes.c_void_p)
        if not self._kernel32.InitializeProcThreadAttributeList(
            attribute_list,
            1,
            0,
            ctypes.byref(required),
        ):
            raise _last_error("InitializeProcThreadAttributeList")
        handles = (wintypes.HANDLE * 1)(wintypes.HANDLE(job_handle))
        if not self._kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            _PROC_THREAD_ATTRIBUTE_JOB_LIST,
            ctypes.byref(handles),
            ctypes.sizeof(handles),
            None,
            None,
        ):
            error = _last_error("UpdateProcThreadAttribute(PROC_THREAD_ATTRIBUTE_JOB_LIST)")
            self._kernel32.DeleteProcThreadAttributeList(attribute_list)
            raise error
        return buffer, attribute_list, handles

    def _configure_api(self) -> None:
        self._kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(_StartupInfoW),
            ctypes.POINTER(_ProcessInformation),
        ]
        self._kernel32.CreateProcessW.restype = wintypes.BOOL
        self._kernel32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        self._kernel32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        self._kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        self._kernel32.DeleteProcThreadAttributeList.restype = None
        self._kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
        self._kernel32.SetHandleInformation.restype = wintypes.BOOL
        self._kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateProcess.restype = wintypes.BOOL
        self._kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        self._kernel32.ResumeThread.restype = wintypes.DWORD
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self._kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL


class WindowsManagedWorkerProcess:
    def __init__(self, *, kernel32: Any, pid: int, process_handle: int, thread_handle: int) -> None:
        self._kernel32 = kernel32
        self._pid = pid
        self._process_handle = process_handle
        self._thread_handle = thread_handle
        self._closed = False

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def native_process_handle(self) -> int:
        if self._closed:
            raise WindowsProcessError("Worker process handle is closed")
        return self._process_handle

    def resume(self) -> None:
        if self._closed or self._thread_handle == 0:
            raise WindowsProcessError("Worker primary thread is unavailable")
        result = int(self._kernel32.ResumeThread(wintypes.HANDLE(self._thread_handle)))
        if result == 0xFFFFFFFF:
            raise _last_error("ResumeThread")
        thread_handle = self._thread_handle
        self._thread_handle = 0
        if not self._kernel32.CloseHandle(wintypes.HANDLE(thread_handle)):
            raise _last_error("CloseHandle(primary thread)")

    def poll(self) -> int | None:
        self._require_open()
        wait = int(self._kernel32.WaitForSingleObject(wintypes.HANDLE(self._process_handle), 0))
        if wait == _WAIT_TIMEOUT:
            return None
        if wait == _WAIT_OBJECT_0:
            return self._exit_code()
        if wait == _WAIT_FAILED:
            raise _last_error("WaitForSingleObject(process)")
        raise WindowsProcessError(f"unexpected process wait result 0x{wait:08x}")

    async def wait(self) -> int:
        self._require_open()
        return await asyncio.to_thread(self._wait_sync)

    def terminate(self, exit_code: int) -> None:
        self._require_open()
        if not 0 <= exit_code <= 0xFFFFFFFF:
            raise ValueError("exit code must fit an unsigned DWORD")
        if self.poll() is not None:
            return
        if not self._kernel32.TerminateProcess(wintypes.HANDLE(self._process_handle), exit_code):
            raise _last_error("TerminateProcess")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        thread_handle = self._thread_handle
        self._thread_handle = 0
        if thread_handle:
            self._kernel32.CloseHandle(wintypes.HANDLE(thread_handle))
        process_handle = self._process_handle
        self._process_handle = 0
        if process_handle and not self._kernel32.CloseHandle(wintypes.HANDLE(process_handle)):
            raise _last_error("CloseHandle(process)")

    def _wait_sync(self) -> int:
        wait = int(
            self._kernel32.WaitForSingleObject(
                wintypes.HANDLE(self._process_handle),
                _INFINITE,
            )
        )
        if wait != _WAIT_OBJECT_0:
            if wait == _WAIT_FAILED:
                raise _last_error("WaitForSingleObject(process)")
            raise WindowsProcessError(f"unexpected process wait result 0x{wait:08x}")
        return self._exit_code()

    def _exit_code(self) -> int:
        code = wintypes.DWORD(_STILL_ACTIVE)
        if not self._kernel32.GetExitCodeProcess(
            wintypes.HANDLE(self._process_handle),
            ctypes.byref(code),
        ):
            raise _last_error("GetExitCodeProcess")
        return int(code.value)

    def _require_open(self) -> None:
        if self._closed:
            raise WindowsProcessError("Worker process handle is closed")


class WindowsJobObjectBackend:
    def __init__(self) -> None:
        _require_windows()

    def create(self, workspace: SupervisedWorkspaceIdentity) -> WorkerJob:
        return WindowsWorkerJob(workspace)


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


def fixed_worker_argv(request: WorkerLaunchRequest, *, executable: Path | None = None) -> list[str]:
    """Return the only production argv; no caller-controlled arguments exist."""

    program = executable or request.executable.executable
    return [
        str(program),
        "--offeragent-runtime-mode",
        "worker",
        "--transport",
        "named-pipe",
        "--workspace-instance-id",
        request.workspace.workspace_instance_id,
        "--canonical-root-identity",
        request.workspace.canonical_root_identity,
        "--database-identity",
        request.workspace.database_identity,
        "--runtime-version",
        request.executable.runtime_version,
    ]


def minimal_worker_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Sanitize an explicitly supplied test environment.

    Production never calls this helper; :func:`trusted_worker_environment`
    obtains its values from Win32 Known Folders instead of the parent env.
    """

    selected: dict[str, str] = {}
    normalized = {key.casefold(): (key, value) for key, value in source.items()}
    for desired in _MINIMAL_ENVIRONMENT_KEYS:
        match = normalized.get(desired.casefold())
        if match is None:
            continue
        _, value = match
        if not value or "\x00" in value or "=" in desired:
            raise WindowsProcessError(f"invalid value for minimal environment key {desired}")
        selected[desired] = str(_canonical_directory(Path(value), label=desired))
    if "SystemRoot" not in selected and "WINDIR" not in selected:
        raise WindowsProcessError("SystemRoot or WINDIR is required for Worker creation")
    return selected


def trusted_worker_environment() -> dict[str, str]:
    """Build the production environment exclusively from trusted Win32 APIs."""

    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel32.GetWindowsDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = int(kernel32.GetWindowsDirectoryW(buffer, len(buffer)))
    if length == 0 or length >= len(buffer):
        raise _last_error("GetWindowsDirectoryW")
    windows = _canonical_directory(Path(buffer.value), label="Windows directory")
    profile = _known_folder(_FOLDERID_PROFILE)
    local = _known_folder(_FOLDERID_LOCAL_APP_DATA)
    roaming = _known_folder(_FOLDERID_ROAMING_APP_DATA)
    preferred_temp = local / "Temp"
    temp = preferred_temp if preferred_temp.is_dir() else local
    return {
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(local),
        "SystemRoot": str(windows),
        "TEMP": str(temp),
        "TMP": str(temp),
        "USERPROFILE": str(profile),
        "WINDIR": str(windows),
    }


def self_test_runtime_sandbox_root(nonce: str) -> Path:
    """Return the non-production state root reserved for one release self-test.

    The root is derived from the current user's Known Folder rather than the
    process environment.  A caller therefore cannot redirect the diagnostic
    Host onto a production or arbitrary registry by changing ``LOCALAPPDATA``.
    """

    if _SELF_TEST_NONCE.fullmatch(nonce) is None:
        raise WindowsProcessError("Runtime self-test nonce is invalid")
    local = Path(trusted_worker_environment()["LOCALAPPDATA"])
    return local / "OfferAgent" / "self-test" / nonce


def _isolated_self_test_worker_environment(nonce: str) -> dict[str, str]:
    environment = trusted_worker_environment()
    sandbox = self_test_runtime_sandbox_root(nonce)
    roots = {
        "APPDATA": sandbox / "RoamingAppData",
        "LOCALAPPDATA": sandbox / "LocalAppData",
        "TEMP": sandbox / "Temp",
        "TMP": sandbox / "Temp",
        "USERPROFILE": sandbox / "Profile",
    }
    for root in sorted(set(roots.values()), key=str):
        root.mkdir(parents=True, exist_ok=True)
        protect_current_user_path(root, directory=True)
    environment.update({key: str(value.resolve(strict=True)) for key, value in roots.items()})
    return environment


def _environment_block(environment: Mapping[str, str]) -> str:
    entries = [f"{key}={environment[key]}" for key in sorted(environment, key=str.casefold)]
    return "\0".join(entries) + "\0\0"


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


def _clear_inherit_flag(kernel32: Any, handle: int) -> None:
    if not kernel32.SetHandleInformation(
        wintypes.HANDLE(handle),
        _HANDLE_FLAG_INHERIT,
        0,
    ):
        raise _last_error("SetHandleInformation")


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
    "PinnedWorkerExecutableVerifier",
    "SignedReleaseManifestTrust",
    "VerifiedExecutableLease",
    "WindowsAuthenticodeVerifier",
    "WindowsJobObjectBackend",
    "WindowsManagedWorkerProcess",
    "WindowsProcessError",
    "WindowsWorkerJob",
    "WindowsWorkerProcessBackend",
    "fixed_worker_argv",
    "minimal_worker_environment",
    "self_test_runtime_sandbox_root",
    "trusted_worker_environment",
]
