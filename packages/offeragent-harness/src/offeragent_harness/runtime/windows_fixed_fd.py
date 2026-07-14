"""Narrow Windows launcher for the packaged Host fd3/fd4 contracts.

``subprocess`` cannot describe arbitrary CRT descriptors on Windows.  Node's
``child_process.spawn`` does so by supplying the Microsoft CRT inheritance
block in ``STARTUPINFO.lpReserved2``.  The release self-test uses this module
to exercise exactly the same Host contract without adding a path-bearing argv
or a diagnostic transport to the Host.

Only explicitly listed anonymous-pipe handles are inherited.  A
``PROC_THREAD_ATTRIBUTE_HANDLE_LIST`` prevents unrelated inheritable handles
from crossing the process boundary.
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .windows_security import SecurityAttributes, current_user_security_attributes

_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_HANDLE_FLAG_INHERIT = 0x00000001
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_STILL_ACTIVE = 259
_ERROR_BROKEN_PIPE = 109
_FOPEN_PIPE = 0x01 | 0x08
_MAXIMUM_FIXED_FD = 16


class WindowsFixedFdError(OSError):
    pass


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


class _StartupInfoExW(ctypes.Structure):
    _fields_ = [("startup_info", _StartupInfoW), ("attribute_list", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("process", wintypes.HANDLE),
        ("thread", wintypes.HANDLE),
        ("process_id", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
    ]


class WindowsFixedFdProcess:
    """Owned child process plus bounded readers for its fixed output fds."""

    def __init__(
        self,
        *,
        kernel32: Any,
        process_handle: int,
        pid: int,
        output_handles: Mapping[int, int],
    ) -> None:
        self._kernel32 = kernel32
        self._process_handle = process_handle
        self._output_handles = dict(output_handles)
        self.pid = pid

    def read_output(self, descriptor: int, *, maximum_bytes: int, timeout_seconds: float) -> bytes:
        if maximum_bytes < 1 or timeout_seconds <= 0:
            raise ValueError("fixed fd output bounds must be positive")
        handle = self._output_handles.get(descriptor)
        if handle is None:
            raise WindowsFixedFdError("fixed output fd is unavailable")
        deadline = time.monotonic() + timeout_seconds
        payload = bytearray()
        try:
            while True:
                available = wintypes.DWORD()
                if not self._kernel32.PeekNamedPipe(
                    wintypes.HANDLE(handle),
                    None,
                    0,
                    None,
                    ctypes.byref(available),
                    None,
                ):
                    code = ctypes.get_last_error()
                    if code == _ERROR_BROKEN_PIPE:
                        break
                    raise _error_code("PeekNamedPipe", code)
                if available.value == 0:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("fixed fd output deadline expired")
                    time.sleep(0.01)
                    continue
                remaining = maximum_bytes + 1 - len(payload)
                if remaining <= 0:
                    raise WindowsFixedFdError("fixed fd output exceeded its limit")
                count = min(int(available.value), remaining, 64 * 1024)
                buffer = ctypes.create_string_buffer(count)
                read = wintypes.DWORD()
                if not self._kernel32.ReadFile(
                    wintypes.HANDLE(handle),
                    buffer,
                    count,
                    ctypes.byref(read),
                    None,
                ):
                    code = ctypes.get_last_error()
                    if code == _ERROR_BROKEN_PIPE:
                        break
                    raise _error_code("ReadFile(fixed fd)", code)
                payload.extend(buffer.raw[: read.value])
                if len(payload) > maximum_bytes:
                    raise WindowsFixedFdError("fixed fd output exceeded its limit")
            return bytes(payload)
        finally:
            self._close_output(descriptor)

    def poll(self) -> int | None:
        if self._process_handle <= 0:
            raise WindowsFixedFdError("fixed fd process handle is closed")
        code = wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(wintypes.HANDLE(self._process_handle), ctypes.byref(code)):
            raise _last_error("GetExitCodeProcess")
        return None if code.value == _STILL_ACTIVE else int(code.value)

    def wait(self, *, timeout_seconds: float) -> int:
        if timeout_seconds <= 0:
            raise ValueError("process wait timeout must be positive")
        if self._process_handle <= 0:
            raise WindowsFixedFdError("fixed fd process handle is closed")
        milliseconds = min(0xFFFFFFFE, max(1, int(timeout_seconds * 1000)))
        result = self._kernel32.WaitForSingleObject(wintypes.HANDLE(self._process_handle), milliseconds)
        if result == _WAIT_TIMEOUT:
            raise TimeoutError("fixed fd process exit deadline expired")
        if result != _WAIT_OBJECT_0:
            raise _last_error("WaitForSingleObject")
        code = self.poll()
        if code is None:
            raise WindowsFixedFdError("signalled process still reports active")
        return code

    def terminate(self, exit_code: int = 0xEF) -> None:
        if self._process_handle <= 0 or self.poll() is not None:
            return
        if not self._kernel32.TerminateProcess(wintypes.HANDLE(self._process_handle), exit_code):
            raise _last_error("TerminateProcess")

    def close(self) -> None:
        for descriptor in tuple(self._output_handles):
            self._close_output(descriptor)
        process_handle = self._process_handle
        self._process_handle = 0
        if process_handle > 0:
            self._kernel32.CloseHandle(wintypes.HANDLE(process_handle))

    def _close_output(self, descriptor: int) -> None:
        handle = self._output_handles.pop(descriptor, None)
        if handle is not None:
            self._kernel32.CloseHandle(wintypes.HANDLE(handle))


def launch_windows_fixed_fd_process(
    executable: Path,
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    child_inputs: Mapping[int, bytes],
    child_outputs: frozenset[int],
    cleanup_job_handle: int | None = None,
) -> WindowsFixedFdProcess:
    """Launch one image with only the requested Microsoft CRT fds inherited."""

    if os.name != "nt":
        raise WindowsFixedFdError("fixed fd launcher requires Windows")
    image = executable.resolve(strict=True)
    if not image.is_file() or image.suffix.casefold() != ".exe":
        raise WindowsFixedFdError("fixed fd image must be a local .exe")
    descriptors = {*child_inputs, *child_outputs}
    if (
        not descriptors
        or descriptors & {0, 1, 2}
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value > _MAXIMUM_FIXED_FD for value in descriptors
        )
        or set(child_inputs) & set(child_outputs)
    ):
        raise WindowsFixedFdError("fixed fd map is invalid")
    if any(not isinstance(payload, bytes) or len(payload) > 64 * 1024 for payload in child_inputs.values()):
        raise WindowsFixedFdError("fixed fd input is invalid")
    if cleanup_job_handle is not None and cleanup_job_handle <= 0:
        raise WindowsFixedFdError("fixed fd cleanup Job handle is invalid")
    normalized_environment = _validate_environment(environment)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_api(kernel32)
    child_handles: dict[int, int] = {}
    parent_inputs: dict[int, int] = {}
    parent_outputs: dict[int, int] = {}
    process_handle = 0
    thread_handle = 0
    attribute_list: ctypes.c_void_p | None = None
    try:
        with current_user_security_attributes() as source_attributes:
            attributes = SecurityAttributes(
                length=source_attributes.length,
                security_descriptor=source_attributes.security_descriptor,
                inherit_handle=True,
            )
            for descriptor in sorted(descriptors):
                read_handle, write_handle = _create_pipe(kernel32, attributes)
                if descriptor in child_inputs:
                    child_handles[descriptor] = read_handle
                    parent_inputs[descriptor] = write_handle
                    _clear_inheritance(kernel32, write_handle)
                else:
                    child_handles[descriptor] = write_handle
                    parent_outputs[descriptor] = read_handle
                    _clear_inheritance(kernel32, read_handle)

        startup = _StartupInfoExW()
        startup.startup_info.cb = ctypes.sizeof(_StartupInfoExW)
        crt_block = _crt_inheritance_block(child_handles)
        startup.startup_info.reserved2 = len(crt_block)
        startup.startup_info.reserved2_data = ctypes.cast(crt_block, ctypes.POINTER(ctypes.c_ubyte))
        attribute_buffer, attribute_list, handle_array, job_array = _process_attribute_list(
            kernel32,
            child_handles.values(),
            cleanup_job_handle=cleanup_job_handle,
        )
        startup.attribute_list = attribute_list
        process_info = _ProcessInformation()
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(image), *arguments]))
        environment_block = ctypes.create_unicode_buffer(_environment_block(normalized_environment))
        created = kernel32.CreateProcessW(
            str(image),
            command_line,
            None,
            None,
            True,
            _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT,
            environment_block,
            str(image.parent),
            ctypes.cast(ctypes.byref(startup), ctypes.POINTER(_StartupInfoW)),
            ctypes.byref(process_info),
        )
        del attribute_buffer, handle_array, job_array, crt_block
        if not created:
            raise _last_error("CreateProcessW(fixed fd)")
        process_handle = int(process_info.process)
        thread_handle = int(process_info.thread)
        if cleanup_job_handle is not None:
            in_job = wintypes.BOOL()
            if (
                not kernel32.IsProcessInJob(
                    wintypes.HANDLE(process_handle),
                    wintypes.HANDLE(cleanup_job_handle),
                    ctypes.byref(in_job),
                )
                or not in_job.value
            ):
                raise WindowsFixedFdError("fixed fd Host did not atomically join its cleanup Job")
        for handle in child_handles.values():
            kernel32.CloseHandle(wintypes.HANDLE(handle))
        child_handles.clear()
        for descriptor, payload in child_inputs.items():
            _write_all(kernel32, parent_inputs.pop(descriptor), payload)
        kernel32.CloseHandle(wintypes.HANDLE(thread_handle))
        thread_handle = 0
        result = WindowsFixedFdProcess(
            kernel32=kernel32,
            process_handle=process_handle,
            pid=int(process_info.process_id),
            output_handles=parent_outputs,
        )
        process_handle = 0
        parent_outputs = {}
        return result
    except BaseException:
        if process_handle > 0:
            kernel32.TerminateProcess(wintypes.HANDLE(process_handle), 0xEF)
        raise
    finally:
        if attribute_list is not None:
            kernel32.DeleteProcThreadAttributeList(attribute_list)
        for handle in (*child_handles.values(), *parent_inputs.values(), *parent_outputs.values()):
            kernel32.CloseHandle(wintypes.HANDLE(handle))
        if thread_handle > 0:
            kernel32.CloseHandle(wintypes.HANDLE(thread_handle))
        if process_handle > 0:
            kernel32.CloseHandle(wintypes.HANDLE(process_handle))


def _create_pipe(kernel32: Any, attributes: SecurityAttributes) -> tuple[int, int]:
    read_handle = wintypes.HANDLE()
    write_handle = wintypes.HANDLE()
    if not kernel32.CreatePipe(
        ctypes.byref(read_handle),
        ctypes.byref(write_handle),
        ctypes.byref(attributes),
        0,
    ):
        raise _last_error("CreatePipe(fixed fd)")
    if read_handle.value is None or write_handle.value is None:
        raise WindowsFixedFdError("CreatePipe returned an invalid handle")
    return int(read_handle.value), int(write_handle.value)


def _clear_inheritance(kernel32: Any, handle: int) -> None:
    if not kernel32.SetHandleInformation(wintypes.HANDLE(handle), _HANDLE_FLAG_INHERIT, 0):
        raise _last_error("SetHandleInformation(fixed fd)")


def _crt_inheritance_block(handles: Mapping[int, int]) -> ctypes.Array[ctypes.c_ubyte]:
    count = max(handles) + 1
    pointer_bytes = ctypes.sizeof(ctypes.c_void_p)
    invalid = (1 << (pointer_bytes * 8)) - 1
    flags = bytearray(count)
    values = [invalid] * count
    for descriptor, handle in handles.items():
        flags[descriptor] = _FOPEN_PIPE
        values[descriptor] = handle
    raw = (
        struct.pack("<i", count)
        + bytes(flags)
        + b"".join(int(value).to_bytes(pointer_bytes, "little", signed=False) for value in values)
    )
    if len(raw) > 0xFFFF:
        raise WindowsFixedFdError("CRT inheritance block is too large")
    return (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)


def _process_attribute_list(
    kernel32: Any,
    handles: Iterable[int],
    *,
    cleanup_job_handle: int | None,
) -> tuple[
    ctypes.Array[ctypes.c_char],
    ctypes.c_void_p,
    ctypes.Array[wintypes.HANDLE],
    ctypes.Array[wintypes.HANDLE] | None,
]:
    values = tuple(int(value) for value in handles)
    required = ctypes.c_size_t()
    attribute_count = 1 if cleanup_job_handle is None else 2
    kernel32.InitializeProcThreadAttributeList(None, attribute_count, 0, ctypes.byref(required))
    if required.value == 0:
        raise _last_error("InitializeProcThreadAttributeList(size)")
    buffer = ctypes.create_string_buffer(required.value)
    attribute_list = ctypes.cast(buffer, ctypes.c_void_p)
    if not kernel32.InitializeProcThreadAttributeList(attribute_list, attribute_count, 0, ctypes.byref(required)):
        raise _last_error("InitializeProcThreadAttributeList")
    array = (wintypes.HANDLE * len(values))(*(wintypes.HANDLE(value) for value in values))
    if not kernel32.UpdateProcThreadAttribute(
        attribute_list,
        0,
        _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
        ctypes.byref(array),
        ctypes.sizeof(array),
        None,
        None,
    ):
        error = _last_error("UpdateProcThreadAttribute(HANDLE_LIST)")
        kernel32.DeleteProcThreadAttributeList(attribute_list)
        raise error
    job_array: ctypes.Array[wintypes.HANDLE] | None = None
    if cleanup_job_handle is not None:
        job_array = (wintypes.HANDLE * 1)(wintypes.HANDLE(cleanup_job_handle))
        if not kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            _PROC_THREAD_ATTRIBUTE_JOB_LIST,
            ctypes.byref(job_array),
            ctypes.sizeof(job_array),
            None,
            None,
        ):
            error = _last_error("UpdateProcThreadAttribute(JOB_LIST)")
            kernel32.DeleteProcThreadAttributeList(attribute_list)
            raise error
    return buffer, attribute_list, array, job_array


def _write_all(kernel32: Any, handle: int, payload: bytes) -> None:
    try:
        offset = 0
        while offset < len(payload):
            chunk = payload[offset : offset + 64 * 1024]
            written = wintypes.DWORD()
            if not kernel32.WriteFile(
                wintypes.HANDLE(handle),
                chunk,
                len(chunk),
                ctypes.byref(written),
                None,
            ):
                raise _last_error("WriteFile(fixed fd)")
            if written.value < 1:
                raise WindowsFixedFdError("fixed fd input write made no progress")
            offset += written.value
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _validate_environment(environment: Mapping[str, str]) -> dict[str, str]:
    if not environment:
        raise WindowsFixedFdError("fixed fd environment is empty")
    folded: set[str] = set()
    result: dict[str, str] = {}
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "=" in key
            or "\x00" in key
            or "\x00" in value
            or key.casefold() in folded
        ):
            raise WindowsFixedFdError("fixed fd environment is invalid")
        folded.add(key.casefold())
        result[key] = value
    return result


def _environment_block(environment: Mapping[str, str]) -> str:
    return "\0".join(f"{key}={environment[key]}" for key in sorted(environment, key=str.casefold)) + "\0\0"


def _configure_api(kernel32: Any) -> None:
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.CreateProcessW.argtypes = [
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
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def _last_error(operation: str) -> WindowsFixedFdError:
    return _error_code(operation, ctypes.get_last_error())


def _error_code(operation: str, code: int) -> WindowsFixedFdError:
    return WindowsFixedFdError(code, f"{operation} failed: {ctypes.FormatError(code)}")


__all__ = [
    "WindowsFixedFdError",
    "WindowsFixedFdProcess",
    "launch_windows_fixed_fd_process",
]
