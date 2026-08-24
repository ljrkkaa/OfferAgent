"""Production Win32 backend for :mod:`runtime.process_supervisor`.

Every child is created suspended and atomically associated with a current-SID,
kill-on-close Job Object.  Only the three declared standard handles are
inherited via ``PROC_THREAD_ATTRIBUTE_HANDLE_LIST``; argv is encoded with the
documented Windows quoting algorithm and is never passed to a shell.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import threading
from collections.abc import Mapping
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .process_identity import SupervisedWorkspaceIdentity, WorkerJob
from .process_supervisor import (
    ExecutableTrust,
    ManagedSupervisedProcess,
    ProcessExecutableProfile,
    ResolvedProcessFilesystemGrant,
)
from .windows_appcontainer import (
    AppContainerAclLease,
    WindowsAppContainerAclManager,
    WindowsAppContainerProfile,
)
from .windows_process import (
    AuthenticodeVerifier,
    ExecutableVerificationError,
    VerifiedExecutableLease,
    WindowsProcessError,
    WindowsWorkerJob,
    _file_identity,
    _ProcessInformation,
    _query_process_image_path,
    _sha256_handle,
    _StartupInfoExW,
    _StartupInfoW,
)
from .windows_security import current_user_security_attributes

_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESHOWWINDOW = 0x00000001
_STARTF_USESTDHANDLES = 0x00000100
_SW_HIDE = 0
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_HANDLE_FLAG_INHERIT = 0x00000001
_WAIT_OBJECT_0 = 0
_WAIT_FAILED = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF
_STILL_ACTIVE = 259
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_BROKEN_PIPE = 109
_ERROR_INSUFFICIENT_BUFFER = 122
_MAX_ENVIRONMENT_CODE_UNITS = 32_767
_TOKEN_QUERY = 0x0008
_TOKEN_IS_APP_CONTAINER = 29
_TOKEN_CAPABILITIES = 30
_TOKEN_APP_CONTAINER_SID = 31


class _SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("app_container_sid", ctypes.c_void_p),
        ("capabilities", ctypes.c_void_p),
        ("capability_count", wintypes.DWORD),
        ("reserved", wintypes.DWORD),
    ]


class _TokenAppContainerInformation(ctypes.Structure):
    _fields_ = [("token_app_container", ctypes.c_void_p)]


class _ZeroizingEnvironmentBlock:
    """Mutable UTF-16 environment block whose whole storage is wiped on exit.

    Secret values are decoded directly from the resolver-owned mutable byte
    view into numeric UTF-16 code units.  They never become Python ``str`` or
    ``bytes`` objects in the Worker.
    """

    def __init__(
        self,
        environment: Mapping[str, str],
    ) -> None:
        self._buffer = (ctypes.c_uint16 * _MAX_ENVIRONMENT_CODE_UNITS)()
        self._position = 0
        self._closed = False
        try:
            plain = {name.upper(): name for name in environment}
            if len(plain) != len(environment):
                raise WindowsProcessError("process environment contains case-insensitive duplicates")
            for folded in sorted(plain, key=str.casefold):
                name = plain[folded]
                self._append_text(name)
                self._append_unit(ord("="))
                self._append_text(environment[name])
                self._append_unit(0)
            # Entries are NUL-terminated and the block ends with one additional
            # NUL.  The zero-initialized following cell also covers an empty map.
            self._append_unit(0)
        except BaseException:
            self.close()
            raise

    @property
    def pointer(self) -> ctypes.c_void_p:
        if self._closed:
            raise WindowsProcessError("process environment block was already zeroized")
        return ctypes.c_void_p(ctypes.addressof(self._buffer))

    @property
    def byte_length(self) -> int:
        return ctypes.sizeof(self._buffer)

    def close(self) -> None:
        if self._closed:
            return
        ctypes.memset(ctypes.addressof(self._buffer), 0, ctypes.sizeof(self._buffer))
        self._position = 0
        self._closed = True

    def __enter__(self) -> _ZeroizingEnvironmentBlock:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _append_text(self, value: str) -> None:
        if "\x00" in value:
            raise WindowsProcessError("process environment contains NUL")
        for character in value:
            self._append_code_point(ord(character))

    def _append_utf8_secret(self, value: memoryview) -> None:
        data = value.cast("B")
        try:
            index = 0
            while index < len(data):
                first = data[index]
                if first == 0:
                    raise WindowsProcessError("secret process environment contains NUL")
                if first <= 0x7F:
                    code_point = first
                    width = 1
                elif 0xC2 <= first <= 0xDF:
                    code_point, width = self._decode_utf8(data, index, 2, first & 0x1F)
                elif 0xE0 <= first <= 0xEF:
                    code_point, width = self._decode_utf8(data, index, 3, first & 0x0F)
                    if code_point < 0x800 or 0xD800 <= code_point <= 0xDFFF:
                        raise WindowsProcessError("secret process environment is not canonical UTF-8")
                elif 0xF0 <= first <= 0xF4:
                    code_point, width = self._decode_utf8(data, index, 4, first & 0x07)
                    if code_point < 0x10000 or code_point > 0x10FFFF:
                        raise WindowsProcessError("secret process environment is not canonical UTF-8")
                else:
                    raise WindowsProcessError("secret process environment is not valid UTF-8")
                self._append_code_point(code_point)
                index += width
        finally:
            data.release()

    @staticmethod
    def _decode_utf8(
        data: memoryview,
        index: int,
        width: int,
        prefix: int,
    ) -> tuple[int, int]:
        if index + width > len(data):
            raise WindowsProcessError("secret process environment is truncated UTF-8")
        code_point = prefix
        for offset in range(1, width):
            current = data[index + offset]
            if current & 0xC0 != 0x80:
                raise WindowsProcessError("secret process environment is not valid UTF-8")
            code_point = (code_point << 6) | (current & 0x3F)
        if width == 2 and code_point < 0x80:
            raise WindowsProcessError("secret process environment is not canonical UTF-8")
        return code_point, width

    def _append_code_point(self, code_point: int) -> None:
        if code_point <= 0xFFFF:
            self._append_unit(code_point)
            return
        adjusted = code_point - 0x10000
        self._append_unit(0xD800 + (adjusted >> 10))
        self._append_unit(0xDC00 + (adjusted & 0x3FF))

    def _append_unit(self, value: int) -> None:
        # Keep one zero-initialized code unit available after the explicit
        # terminator so even the empty block is double-NUL terminated.
        if self._position >= _MAX_ENVIRONMENT_CODE_UNITS - 1:
            raise WindowsProcessError("process environment exceeds the Windows Unicode block limit")
        self._buffer[self._position] = value
        self._position += 1


class PinnedProcessExecutableVerifier:
    """Lock and re-verify a configured executable through suspended spawn."""

    def __init__(
        self,
        *,
        authenticode: AuthenticodeVerifier | None = None,
    ) -> None:
        _require_windows()
        self._authenticode = authenticode
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()

    def open_verified(self, profile: ProcessExecutableProfile) -> VerifiedExecutableLease:
        try:
            root = profile.fixed_root.resolve(strict=True)
            executable = profile.executable.resolve(strict=True)
            executable.relative_to(root)
        except (OSError, ValueError) as error:
            raise ExecutableVerificationError("process executable escaped its fixed installation root") from error
        if executable.suffix.casefold() != ".exe" or not executable.is_file():
            raise ExecutableVerificationError("local process profiles require an existing .exe")
        if profile.trust is ExecutableTrust.OS_AUTHENTICODE:
            if self._authenticode is None or not self._authenticode.verify(executable):
                raise ExecutableVerificationError("process executable failed offline Authenticode verification")

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
        if raw_handle in {0, _INVALID_HANDLE_VALUE}:
            raise _last_error("CreateFileW(process executable)")
        try:
            digest = _sha256_handle(self._kernel32, raw_handle)
            if profile.file_sha256 is not None and profile.file_sha256 != f"sha256:{digest}":
                raise ExecutableVerificationError("process executable differs from its pinned profile")
            identity = _file_identity(self._kernel32, raw_handle)
            if identity[1] != profile.captured_file_index:
                raise ExecutableVerificationError("process executable file identity differs from its captured profile")
            if f"sha256:{digest}" != profile.captured_content_sha256:
                raise ExecutableVerificationError("process executable content differs from its captured profile")
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

    def _configure_api(self) -> None:
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
        self._kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self._kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL


class WindowsSupervisedProcessBackend:
    """Direct ``CreateProcessW`` backend with atomic nested Job assignment."""

    def __init__(
        self,
        *,
        workspace: SupervisedWorkspaceIdentity,
        verifier: PinnedProcessExecutableVerifier,
        sandbox_state_directory: Path,
        parent_job: WorkerJob | None = None,
    ) -> None:
        _require_windows()
        self._workspace = workspace
        self._verifier = verifier
        self._parent_job = parent_job
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._appcontainer = WindowsAppContainerProfile(workspace)
        self._appcontainer_acls = WindowsAppContainerAclManager(
            self._appcontainer,
            sandbox_state_directory,
        )
        self._configure_api()

    async def spawn(
        self,
        profile: ProcessExecutableProfile,
        *,
        arguments: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        stdin: bytes,
        interactive_stdin: bool,
        allow_network: bool,
        filesystem_grants: tuple[ResolvedProcessFilesystemGrant, ...],
    ) -> ManagedSupervisedProcess:
        managed = await asyncio.to_thread(
            self._spawn_sync,
            profile,
            arguments,
            cwd,
            environment,
            stdin,
            interactive_stdin,
            allow_network,
            filesystem_grants,
        )
        managed.start_stdin_writer(close_after_payload=not interactive_stdin)
        return managed

    async def shutdown(self) -> None:
        await asyncio.to_thread(self._shutdown_sync)

    def _spawn_sync(
        self,
        profile: ProcessExecutableProfile,
        arguments: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        stdin: bytes,
        interactive_stdin: bool,
        allow_network: bool,
        filesystem_grants: tuple[ResolvedProcessFilesystemGrant, ...],
    ) -> WindowsManagedSupervisedProcess:
        invocation_job = WindowsWorkerJob(self._workspace)
        acl_lease: AppContainerAclLease | None = None
        appcontainer_sid: int | None = None
        child_stdin = parent_stdin = child_stdout = parent_stdout = child_stderr = parent_stderr = 0
        process_handle = thread_handle = 0
        created = False
        try:
            if not allow_network:
                if not filesystem_grants:
                    raise WindowsProcessError("network-denied process has no AppContainer filesystem grants")
                acl_lease = self._appcontainer_acls.acquire(filesystem_grants)
                appcontainer_sid = self._appcontainer.sid
            process_environment = (
                self._appcontainer_environment(environment)
                if appcontainer_sid is not None
                else self._current_user_environment(environment, temporary_directory=cwd)
            )
            child_stdout, parent_stdout = self._output_pipe()
            child_stderr, parent_stderr = self._output_pipe()
            if stdin or interactive_stdin:
                child_stdin, parent_stdin = self._input_pipe()
            else:
                child_stdin = self._open_null_stdin()
            with self._verifier.open_verified(profile) as verified:
                command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(verified.path), *arguments]))
                job_handles = [invocation_job.native_job_handle]
                if self._parent_job is not None:
                    job_handles.insert(0, self._parent_job.native_job_handle)
                (
                    attribute_buffer,
                    attribute_list,
                    job_array,
                    inherited_array,
                    security_capabilities,
                ) = self._attribute_list(
                    tuple(job_handles),
                    (child_stdin, child_stdout, child_stderr),
                    appcontainer_sid=appcontainer_sid,
                )
                startup = _StartupInfoExW()
                startup.startup_info.cb = ctypes.sizeof(_StartupInfoExW)
                startup.startup_info.flags = _STARTF_USESHOWWINDOW | _STARTF_USESTDHANDLES
                startup.startup_info.show_window = _SW_HIDE
                startup.startup_info.stdin = wintypes.HANDLE(child_stdin)
                startup.startup_info.stdout = wintypes.HANDLE(child_stdout)
                startup.startup_info.stderr = wintypes.HANDLE(child_stderr)
                startup.attribute_list = attribute_list
                process_info = _ProcessInformation()
                flags = (
                    _CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT
                )
                try:
                    with (
                        current_user_security_attributes(allow_system=False) as process_security,
                        current_user_security_attributes(allow_system=False) as thread_security,
                        _ZeroizingEnvironmentBlock(process_environment) as environment_block,
                    ):
                        created = bool(
                            self._kernel32.CreateProcessW(
                                str(verified.path),
                                command_line,
                                ctypes.byref(process_security),
                                ctypes.byref(thread_security),
                                True,
                                flags,
                                environment_block.pointer,
                                str(cwd),
                                ctypes.cast(ctypes.byref(startup), ctypes.POINTER(_StartupInfoW)),
                                ctypes.byref(process_info),
                            )
                        )
                    if not created:
                        raise _last_error("CreateProcessW(supervised process)")
                finally:
                    self._kernel32.DeleteProcThreadAttributeList(attribute_list)
                    del attribute_buffer, job_array, inherited_array, security_capabilities
                process_handle = int(process_info.process)
                thread_handle = int(process_info.thread)
                self._set_inherit(process_handle, False)
                self._set_inherit(thread_handle, False)
                self._verify_appcontainer_token(process_handle, expected_sid=appcontainer_sid)
                image = _query_process_image_path(self._kernel32, process_handle).resolve(strict=True)
                if os.path.normcase(str(image)) != os.path.normcase(str(verified.path)):
                    raise ExecutableVerificationError("created image differs from locked executable")
                if not verified.identity_unchanged() or not verified.content_unchanged():
                    raise ExecutableVerificationError("process executable changed during suspended spawn")
                if not invocation_job.contains_process_handle(process_handle):
                    raise WindowsProcessError("process did not atomically join its invocation Job")
                if self._parent_job is not None and not self._parent_job.contains_process_handle(process_handle):
                    raise WindowsProcessError("process did not atomically join the Worker Job")
                resumed = int(self._kernel32.ResumeThread(wintypes.HANDLE(thread_handle)))
                if resumed == 0xFFFFFFFF:
                    raise _last_error("ResumeThread(supervised process)")
            self._close_handle(thread_handle)
            thread_handle = 0
            self._close_handle(child_stdin)
            child_stdin = 0
            self._close_handle(child_stdout)
            child_stdout = 0
            self._close_handle(child_stderr)
            child_stderr = 0
            managed = WindowsManagedSupervisedProcess(
                kernel32=self._kernel32,
                pid=int(process_info.process_id),
                process_handle=process_handle,
                job=invocation_job,
                stdout_handle=parent_stdout,
                stderr_handle=parent_stderr,
                stdin_handle=parent_stdin,
                stdin_payload=stdin,
                acl_lease=acl_lease,
            )
            acl_lease = None
            process_handle = parent_stdout = parent_stderr = parent_stdin = 0
            return managed
        except BaseException:
            if process_handle:
                self._kernel32.TerminateProcess(wintypes.HANDLE(process_handle), 0xEE)
            invocation_job.close()
            if acl_lease is not None:
                acl_lease.close()
            raise
        finally:
            for handle in (
                thread_handle,
                process_handle,
                child_stdin,
                parent_stdin,
                child_stdout,
                parent_stdout,
                child_stderr,
                parent_stderr,
            ):
                self._close_handle(handle)

    def _output_pipe(self) -> tuple[int, int]:
        read_handle = wintypes.HANDLE()
        write_handle = wintypes.HANDLE()
        with current_user_security_attributes(allow_system=False) as attributes:
            attributes.inherit_handle = True
            if not self._kernel32.CreatePipe(
                ctypes.byref(read_handle),
                ctypes.byref(write_handle),
                ctypes.byref(attributes),
                0,
            ):
                raise _last_error("CreatePipe(output)")
        parent = int(read_handle.value or 0)
        child = int(write_handle.value or 0)
        try:
            self._set_inherit(parent, False)
            return child, parent
        except BaseException:
            self._close_handle(parent)
            self._close_handle(child)
            raise

    def _appcontainer_environment(self, environment: Mapping[str, str]) -> Mapping[str, str]:
        redirected = {key: value for key, value in environment.items()}

        def replace(name: str, value: str) -> None:
            for existing in tuple(redirected):
                if existing.casefold() == name.casefold():
                    redirected.pop(existing)
            redirected[name] = value

        local = self._appcontainer.local_app_data
        replace("LOCALAPPDATA", str(local))
        replace("TEMP", str(local / "Temp"))
        replace("TMP", str(local / "Temp"))
        windows = ctypes.create_unicode_buffer(32_768)
        length = int(self._kernel32.GetWindowsDirectoryW(windows, len(windows)))
        if length == 0 or length >= len(windows):
            raise _last_error("GetWindowsDirectoryW(AppContainer environment)")
        replace("SystemRoot", windows.value)
        replace("WINDIR", windows.value)
        return redirected

    def _current_user_environment(
        self,
        environment: Mapping[str, str],
        *,
        temporary_directory: Path,
    ) -> Mapping[str, str]:
        """Add the Windows baseline required by frozen current-user helpers."""

        baseline = {key: value for key, value in environment.items()}

        def replace(name: str, value: str) -> None:
            for existing in tuple(baseline):
                if existing.casefold() == name.casefold():
                    baseline.pop(existing)
            baseline[name] = value

        windows = ctypes.create_unicode_buffer(32_768)
        windows_length = int(self._kernel32.GetWindowsDirectoryW(windows, len(windows)))
        if windows_length == 0 or windows_length >= len(windows):
            raise _last_error("GetWindowsDirectoryW(current-user environment)")
        replace("SystemRoot", windows.value)
        replace("WINDIR", windows.value)
        replace("TEMP", str(temporary_directory))
        replace("TMP", str(temporary_directory))
        return baseline

    def _input_pipe(self) -> tuple[int, int]:
        read_handle = wintypes.HANDLE()
        write_handle = wintypes.HANDLE()
        with current_user_security_attributes(allow_system=False) as attributes:
            attributes.inherit_handle = True
            if not self._kernel32.CreatePipe(
                ctypes.byref(read_handle),
                ctypes.byref(write_handle),
                ctypes.byref(attributes),
                0,
            ):
                raise _last_error("CreatePipe(input)")
        child = int(read_handle.value or 0)
        parent = int(write_handle.value or 0)
        try:
            self._set_inherit(parent, False)
            return child, parent
        except BaseException:
            self._close_handle(parent)
            self._close_handle(child)
            raise

    def _open_null_stdin(self) -> int:
        handle = self._kernel32.CreateFileW(
            "NUL",
            _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        raw_handle = int(handle) if handle else 0
        if raw_handle in {0, _INVALID_HANDLE_VALUE}:
            raise _last_error("CreateFileW(NUL)")
        self._set_inherit(raw_handle, True)
        return raw_handle

    def _attribute_list(
        self,
        jobs: tuple[int, ...],
        inherited: tuple[int, ...],
        *,
        appcontainer_sid: int | None,
    ) -> tuple[Any, ...]:
        attribute_count = 3 if appcontainer_sid is not None else 2
        required = ctypes.c_size_t()
        self._kernel32.InitializeProcThreadAttributeList(None, attribute_count, 0, ctypes.byref(required))
        if required.value == 0:
            raise _last_error("InitializeProcThreadAttributeList(size)")
        buffer = ctypes.create_string_buffer(required.value)
        attribute_list = ctypes.cast(buffer, ctypes.c_void_p)
        if not self._kernel32.InitializeProcThreadAttributeList(
            attribute_list,
            attribute_count,
            0,
            ctypes.byref(required),
        ):
            raise _last_error("InitializeProcThreadAttributeList")
        job_array = (wintypes.HANDLE * len(jobs))(*(wintypes.HANDLE(item) for item in jobs))
        inherited_array = (wintypes.HANDLE * len(inherited))(*(wintypes.HANDLE(item) for item in inherited))
        security_capabilities = (
            _SecurityCapabilities(ctypes.c_void_p(appcontainer_sid), None, 0, 0)
            if appcontainer_sid is not None
            else None
        )
        try:
            if not self._kernel32.UpdateProcThreadAttribute(
                attribute_list,
                0,
                _PROC_THREAD_ATTRIBUTE_JOB_LIST,
                ctypes.byref(job_array),
                ctypes.sizeof(job_array),
                None,
                None,
            ):
                raise _last_error("UpdateProcThreadAttribute(JOB_LIST)")
            if not self._kernel32.UpdateProcThreadAttribute(
                attribute_list,
                0,
                _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.byref(inherited_array),
                ctypes.sizeof(inherited_array),
                None,
                None,
            ):
                raise _last_error("UpdateProcThreadAttribute(HANDLE_LIST)")
            if security_capabilities is not None and not self._kernel32.UpdateProcThreadAttribute(
                attribute_list,
                0,
                _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                ctypes.byref(security_capabilities),
                ctypes.sizeof(security_capabilities),
                None,
                None,
            ):
                raise _last_error("UpdateProcThreadAttribute(SECURITY_CAPABILITIES)")
            return buffer, attribute_list, job_array, inherited_array, security_capabilities
        except BaseException:
            self._kernel32.DeleteProcThreadAttributeList(attribute_list)
            raise

    def _verify_appcontainer_token(self, process_handle: int, *, expected_sid: int | None) -> None:
        token = wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(
            wintypes.HANDLE(process_handle),
            _TOKEN_QUERY,
            ctypes.byref(token),
        ):
            raise _last_error("OpenProcessToken(supervised process)")
        try:
            is_appcontainer = wintypes.DWORD()
            returned = wintypes.DWORD()
            if not self._advapi32.GetTokenInformation(
                token,
                _TOKEN_IS_APP_CONTAINER,
                ctypes.byref(is_appcontainer),
                ctypes.sizeof(is_appcontainer),
                ctypes.byref(returned),
            ):
                raise _last_error("GetTokenInformation(TokenIsAppContainer)")
            expected = expected_sid is not None
            if bool(is_appcontainer.value) is not expected:
                raise WindowsProcessError("created process AppContainer state differs from its network policy")
            if not expected:
                return
            required = wintypes.DWORD()
            self._advapi32.GetTokenInformation(
                token,
                _TOKEN_APP_CONTAINER_SID,
                None,
                0,
                ctypes.byref(returned),
            )
            error = ctypes.get_last_error()
            required.value = returned.value
            if required.value == 0 or error not in {0, _ERROR_INSUFFICIENT_BUFFER}:
                raise _error_code("GetTokenInformation(TokenAppContainerSid size)", error)
            sid_buffer = ctypes.create_string_buffer(required.value)
            if not self._advapi32.GetTokenInformation(
                token,
                _TOKEN_APP_CONTAINER_SID,
                sid_buffer,
                required,
                ctypes.byref(returned),
            ):
                raise _last_error("GetTokenInformation(TokenAppContainerSid)")
            information = ctypes.cast(
                sid_buffer,
                ctypes.POINTER(_TokenAppContainerInformation),
            ).contents
            assert expected_sid is not None
            if not information.token_app_container or not self._advapi32.EqualSid(
                information.token_app_container,
                ctypes.c_void_p(expected_sid),
            ):
                raise WindowsProcessError("created process uses an unexpected AppContainer Package SID")
            required = wintypes.DWORD()
            self._advapi32.GetTokenInformation(
                token,
                _TOKEN_CAPABILITIES,
                None,
                0,
                ctypes.byref(required),
            )
            error = ctypes.get_last_error()
            if required.value == 0 or error not in {0, _ERROR_INSUFFICIENT_BUFFER}:
                raise _error_code("GetTokenInformation(TokenCapabilities size)", error)
            buffer = ctypes.create_string_buffer(required.value)
            if not self._advapi32.GetTokenInformation(
                token,
                _TOKEN_CAPABILITIES,
                buffer,
                required,
                ctypes.byref(required),
            ):
                raise _last_error("GetTokenInformation(TokenCapabilities)")
            capability_count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
            if capability_count != 0:
                raise WindowsProcessError("network-denied AppContainer unexpectedly has capability SIDs")
        finally:
            self._kernel32.CloseHandle(token)

    def _shutdown_sync(self) -> None:
        self._appcontainer_acls.shutdown()
        self._appcontainer.close()

    def _set_inherit(self, handle: int, inherit: bool) -> None:
        flags = _HANDLE_FLAG_INHERIT if inherit else 0
        if not self._kernel32.SetHandleInformation(
            wintypes.HANDLE(handle),
            _HANDLE_FLAG_INHERIT,
            flags,
        ):
            raise _last_error("SetHandleInformation")

    def _close_handle(self, handle: int) -> None:
        if handle:
            self._kernel32.CloseHandle(wintypes.HANDLE(handle))

    def _configure_api(self) -> None:
        self._kernel32.CreatePipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.CreatePipe.restype = wintypes.BOOL
        self._kernel32.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        self._kernel32.GetWindowsDirectoryW.restype = wintypes.UINT
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
        self._kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        self._kernel32.ResumeThread.restype = wintypes.DWORD
        self._kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateProcess.restype = wintypes.BOOL
        self._kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self._kernel32.ReadFile.restype = wintypes.BOOL
        self._kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self._kernel32.WriteFile.restype = wintypes.BOOL
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self._kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self._advapi32.OpenProcessToken.restype = wintypes.BOOL
        self._advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._advapi32.GetTokenInformation.restype = wintypes.BOOL
        self._advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._advapi32.EqualSid.restype = wintypes.BOOL


class WindowsManagedSupervisedProcess:
    def __init__(
        self,
        *,
        kernel32: Any,
        pid: int,
        process_handle: int,
        job: WindowsWorkerJob,
        stdout_handle: int,
        stderr_handle: int,
        stdin_handle: int,
        stdin_payload: bytes,
        acl_lease: AppContainerAclLease | None,
    ) -> None:
        self._kernel32 = kernel32
        self._pid = pid
        self._process_handle = process_handle
        self._job = job
        self._stdout_handle = stdout_handle
        self._stderr_handle = stderr_handle
        self._stdin_handle = stdin_handle
        self._stdin_payload = bytes(stdin_payload)
        self._acl_lease = acl_lease
        self._stdin_task: asyncio.Task[None] | None = None
        self._stdin_write_lock = threading.Lock()
        self._async_stdin_lock: asyncio.Lock | None = None
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def native_job_handle(self) -> int:
        """Diagnostic handle used by Windows security contract tests."""

        return self._job.native_job_handle

    @property
    def native_process_handle(self) -> int:
        """Current-SID process handle for Windows security contract tests."""

        return self._process_handle

    @property
    def native_stdout_handle(self) -> int:
        """Non-inheritable parent pipe handle for security contract tests."""

        return self._stdout_handle

    @property
    def native_stderr_handle(self) -> int:
        """Non-inheritable parent pipe handle for security contract tests."""

        return self._stderr_handle

    def start_stdin_writer(self, *, close_after_payload: bool) -> None:
        self._async_stdin_lock = asyncio.Lock()
        if not close_after_payload or not self._stdin_handle or self._stdin_task is not None:
            return
        self._stdin_task = asyncio.create_task(asyncio.to_thread(self._write_stdin_sync))

    async def write_stdin(self, payload: bytes) -> None:
        if self._async_stdin_lock is None:
            raise WindowsProcessError("supervised process stdin writer was not initialized")
        async with self._async_stdin_lock:
            if not self._stdin_handle:
                raise WindowsProcessError("supervised process stdin is closed")
            await asyncio.to_thread(self._write_payload_sync, bytes(payload), False)

    async def read_stdout(self, maximum_bytes: int) -> bytes:
        return await asyncio.to_thread(self._read_sync, self._stdout_handle, maximum_bytes)

    async def read_stderr(self, maximum_bytes: int) -> bytes:
        return await asyncio.to_thread(self._read_sync, self._stderr_handle, maximum_bytes)

    async def wait(self) -> int:
        code = await asyncio.to_thread(self._wait_process_sync)
        await self._job.wait_empty(365 * 24 * 60 * 60)
        return code

    async def terminate_tree(self, *, grace_seconds: float) -> None:
        if grace_seconds > 0 and await self._job.wait_empty(grace_seconds):
            return
        self._job.terminate_tree(0xEF)
        await self._job.wait_empty(max(grace_seconds, 0.1))

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._stdin_task is not None:
                await asyncio.gather(self._stdin_task, return_exceptions=True)
            self._job.close()
            for attribute in ("_stdin_handle", "_stdout_handle", "_stderr_handle", "_process_handle"):
                handle = int(getattr(self, attribute))
                setattr(self, attribute, 0)
                if handle:
                    self._kernel32.CloseHandle(wintypes.HANDLE(handle))
            lease, self._acl_lease = self._acl_lease, None
            if lease is not None:
                await asyncio.to_thread(lease.close)

    def _read_sync(self, handle: int, maximum_bytes: int) -> bytes:
        if maximum_bytes < 1:
            raise ValueError("pipe read size must be positive")
        buffer = ctypes.create_string_buffer(maximum_bytes)
        read = wintypes.DWORD()
        if not self._kernel32.ReadFile(
            wintypes.HANDLE(handle),
            buffer,
            maximum_bytes,
            ctypes.byref(read),
            None,
        ):
            error = ctypes.get_last_error()
            if error == _ERROR_BROKEN_PIPE:
                return b""
            raise _error_code("ReadFile(process pipe)", error)
        return bytes(buffer.raw[: read.value])

    def _write_stdin_sync(self) -> None:
        self._write_payload_sync(self._stdin_payload, True)

    def _write_payload_sync(self, payload: bytes, close_after: bool) -> None:
        handle = self._stdin_handle
        try:
            with self._stdin_write_lock:
                offset = 0
                while offset < len(payload):
                    chunk = payload[offset : offset + 64 * 1024]
                    written = wintypes.DWORD()
                    buffer = ctypes.create_string_buffer(chunk)
                    if not self._kernel32.WriteFile(
                        wintypes.HANDLE(handle),
                        buffer,
                        len(chunk),
                        ctypes.byref(written),
                        None,
                    ):
                        error = ctypes.get_last_error()
                        if error == _ERROR_BROKEN_PIPE:
                            return
                        raise _error_code("WriteFile(process stdin)", error)
                    offset += int(written.value)
        finally:
            if close_after and handle:
                self._kernel32.CloseHandle(wintypes.HANDLE(handle))
                self._stdin_handle = 0

    def _wait_process_sync(self) -> int:
        wait = int(self._kernel32.WaitForSingleObject(wintypes.HANDLE(self._process_handle), _INFINITE))
        if wait != _WAIT_OBJECT_0:
            if wait == _WAIT_FAILED:
                raise _last_error("WaitForSingleObject(supervised process)")
            raise WindowsProcessError(f"unexpected process wait result 0x{wait:08x}")
        code = wintypes.DWORD(_STILL_ACTIVE)
        if not self._kernel32.GetExitCodeProcess(wintypes.HANDLE(self._process_handle), ctypes.byref(code)):
            raise _last_error("GetExitCodeProcess(supervised process)")
        return int(code.value)


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsProcessError("Windows process supervision is only available on Windows")


def _last_error(operation: str) -> WindowsProcessError:
    return _error_code(operation, ctypes.get_last_error())


def _error_code(operation: str, code: int) -> WindowsProcessError:
    return WindowsProcessError(code, f"{operation} failed: {ctypes.FormatError(code)}")


__all__ = [
    "PinnedProcessExecutableVerifier",
    "WindowsManagedSupervisedProcess",
    "WindowsSupervisedProcessBackend",
]
