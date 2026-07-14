"""Win32 Named Pipe and DPAPI backend for the authenticated transport state machine."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TypeVar, cast

from .named_pipe import PipeByteStream

_PIPE_NAME = re.compile(r"^\\\\\.\\pipe\\OfferAgent\.[0-9a-f]{64}$")

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_OVERLAPPED = 0x40000000
_FILE_FLAG_WRITE_THROUGH = 0x80000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_SECURITY_SQOS_PRESENT = 0x00100000
_SECURITY_IDENTIFICATION = 0x00010000
_PIPE_ACCESS_DUPLEX = 0x00000003
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_PIPE_UNLIMITED_INSTANCES = 255
_ERROR_IO_PENDING = 997
_ERROR_PIPE_CONNECTED = 535
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_ERROR_PIPE_NOT_CONNECTED = 233
_ERROR_PIPE_BUSY = 231
_ERROR_OPERATION_ABORTED = 995
_ERROR_NOT_FOUND = 1168
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_FILE_NOT_FOUND = 2
_ERROR_SEM_TIMEOUT = 121
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF
_HANDLE_FLAG_INHERIT = 0x00000001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_SDDL_REVISION_1 = 1
_CRYPTPROTECT_UI_FORBIDDEN = 0x00000001
_MAX_WIN32_IO_CHUNK = 64 * 1024
_T = TypeVar("_T")


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


class _TOKEN_USER_VALUE(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class _Win32Api:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Named Pipes are only available on Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

        self.kernel32.CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
        ]
        self.kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        self.kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
        self.kernel32.ConnectNamedPipe.restype = wintypes.BOOL
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_OVERLAPPED),
        ]
        self.kernel32.ReadFile.restype = wintypes.BOOL
        self.kernel32.WriteFile.argtypes = self.kernel32.ReadFile.argtypes
        self.kernel32.WriteFile.restype = wintypes.BOOL
        self.kernel32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_OVERLAPPED),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        ]
        self.kernel32.GetOverlappedResult.restype = wintypes.BOOL
        self.kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
        self.kernel32.CancelIoEx.restype = wintypes.BOOL
        self.kernel32.CreateEventW.argtypes = [
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self.kernel32.CreateEventW.restype = wintypes.HANDLE
        self.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        self.kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
        self.kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
        self.kernel32.SetHandleInformation.restype = wintypes.BOOL
        self.kernel32.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.GetHandleInformation.restype = wintypes.BOOL
        self.kernel32.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
        self.kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
        self.kernel32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
        self.kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
        self.kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.GetCurrentProcess.argtypes = []
        self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel32.GetCurrentProcessId.argtypes = []
        self.kernel32.GetCurrentProcessId.restype = wintypes.DWORD
        self.kernel32.SetNamedPipeHandleState.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.SetNamedPipeHandleState.restype = wintypes.BOOL
        self.kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        self.kernel32.WaitNamedPipeW.restype = wintypes.BOOL
        self.kernel32.GetNamedPipeInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.GetNamedPipeInfo.restype = wintypes.BOOL
        self.kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.kernel32.FlushFileBuffers.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self.kernel32.LocalFree.restype = wintypes.HLOCAL

        self.advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi32.OpenProcessToken.restype = wintypes.BOOL
        self.advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetTokenInformation.restype = wintypes.BOOL
        self.advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
        self.advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.ULONG),
        ]
        self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self.advapi32.GetKernelObjectSecurity.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetKernelObjectSecurity.restype = wintypes.BOOL
        self.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(wintypes.ULONG),
        ]
        self.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL

        self.crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DATA_BLOB),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        self.crypt32.CryptProtectData.restype = wintypes.BOOL
        self.crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DATA_BLOB),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        self.crypt32.CryptUnprotectData.restype = wintypes.BOOL


_API: _Win32Api | None = None
_API_LOCK = threading.Lock()


def _api() -> _Win32Api:
    global _API
    with _API_LOCK:
        if _API is None:
            _API = _Win32Api()
        return _API


@dataclass(frozen=True, slots=True)
class Win32PeerIdentity:
    process_id: int
    session_id: int
    user_sid: str


@dataclass(frozen=True, slots=True)
class Win32PipeSecuritySnapshot:
    current_user_sid: str
    dacl_sddl: str
    allowed_sids: tuple[str, ...]
    peer: Win32PeerIdentity
    creation_pipe_mode: int
    queried_pipe_flags: int
    handle_inheritable: bool


class DpapiCurrentUserProtector:
    """DPAPI current-user protection; never uses machine scope or UI."""

    _ENTROPY = hashlib.sha256(b"OfferAgent.NamedPipe.Discovery.v1").digest()

    def protect(self, plaintext: bytes) -> bytes:
        return _crypt_data(plaintext, protect=True)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return _crypt_data(ciphertext, protect=False)


class Win32NamedPipeStream(PipeByteStream):
    def __init__(
        self,
        handle: int,
        *,
        server_end: bool,
        peer: Win32PeerIdentity,
        creation_pipe_mode: int,
    ) -> None:
        self._handle = handle
        self._server_end = server_end
        self._peer = peer
        self._creation_pipe_mode = creation_pipe_mode
        self._closed = False
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="offeragent-pipe-io")

    @property
    def peer(self) -> Win32PeerIdentity:
        return self._peer

    async def read(self, max_bytes: int) -> bytes:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        return await self._run(lambda: self._read_sync(max_bytes))

    async def write(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("Named Pipe writes require bytes")
        await self._run(lambda: self._write_sync(data))

    def cancel_pending_io(self) -> None:
        api = _api()
        with self._lock:
            if self._closed:
                return
            handle = self._handle
        if not api.kernel32.CancelIoEx(handle, None):
            error = ctypes.get_last_error()
            if error not in {_ERROR_NOT_FOUND, _ERROR_OPERATION_ABORTED}:
                return

    async def close(self) -> None:
        api = _api()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handle = self._handle
        api.kernel32.CancelIoEx(handle, None)
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
        if self._server_end:
            api.kernel32.DisconnectNamedPipe(handle)
        api.kernel32.CloseHandle(handle)

    def security_snapshot(self) -> Win32PipeSecuritySnapshot:
        api = _api()
        with self._lock:
            if self._closed:
                raise OSError("Named Pipe handle is closed")
            handle = self._handle
        sddl = _handle_dacl_sddl(api, handle)
        allowed = tuple(re.findall(r"\(A;[^)]*;;;([^)]+)\)", sddl))
        flags = wintypes.DWORD()
        if not api.kernel32.GetNamedPipeInfo(handle, ctypes.byref(flags), None, None, None):
            _raise_last_error("GetNamedPipeInfo failed")
        handle_flags = wintypes.DWORD()
        if not api.kernel32.GetHandleInformation(handle, ctypes.byref(handle_flags)):
            _raise_last_error("GetHandleInformation failed")
        return Win32PipeSecuritySnapshot(
            current_user_sid=current_user_sid(),
            dacl_sddl=sddl,
            allowed_sids=allowed,
            peer=self._peer,
            creation_pipe_mode=self._creation_pipe_mode,
            queried_pipe_flags=int(flags.value),
            handle_inheritable=bool(handle_flags.value & _HANDLE_FLAG_INHERIT),
        )

    async def _run(self, operation: Callable[[], _T]) -> _T:
        with self._lock:
            if self._closed:
                raise OSError("Named Pipe handle is closed")
        future = asyncio.get_running_loop().run_in_executor(self._executor, operation)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.cancel_pending_io()
            with suppress(Exception):
                await asyncio.shield(future)
            raise

    def _read_sync(self, max_bytes: int) -> bytes:
        buffer = ctypes.create_string_buffer(max_bytes)
        transferred = _overlapped_call(
            self._get_handle(),
            lambda api, handle, count, overlapped: api.kernel32.ReadFile(
                handle,
                buffer,
                max_bytes,
                count,
                overlapped,
            ),
            eof_is_empty=True,
        )
        return bytes(buffer.raw[:transferred])

    def _write_sync(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + _MAX_WIN32_IO_CHUNK]
            buffer = ctypes.create_string_buffer(chunk, len(chunk))

            def issue_write(
                api: _Win32Api,
                handle: int,
                count: Any,
                overlapped: Any,
                *,
                buffer: Any = buffer,
                length: int = len(chunk),
            ) -> Any:
                return api.kernel32.WriteFile(handle, buffer, length, count, overlapped)

            transferred = _overlapped_call(
                self._get_handle(),
                issue_write,
                eof_is_empty=False,
            )
            if transferred < 1:
                raise OSError("Named Pipe write made no progress")
            offset += transferred

    def _get_handle(self) -> int:
        with self._lock:
            if self._closed:
                raise OSError("Named Pipe handle is closed")
            return self._handle


class Win32NamedPipeListener:
    """Current-SID-only local Named Pipe listener with strict peer validation."""

    def __init__(self, pipe_name: str, *, buffer_bytes: int = 64 * 1024) -> None:
        if _PIPE_NAME.fullmatch(pipe_name) is None:
            raise ValueError("production pipe name must contain a 256-bit random suffix")
        if not 4096 <= buffer_bytes <= 16 * 1024 * 1024:
            raise ValueError("Named Pipe buffer_bytes is out of range")
        self.pipe_name = pipe_name
        self.buffer_bytes = buffer_bytes
        self.pipe_mode = _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS
        self._closed = False
        self._accept_handle: int | None = None
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="offeragent-pipe-accept")

    async def accept(self) -> Win32NamedPipeStream:
        with self._lock:
            if self._closed:
                raise OSError("Named Pipe listener is closed")
            if self._accept_handle is not None:
                raise RuntimeError("only one accept may be pending per listener")
            handle = self._create_instance()
            self._accept_handle = handle
        future = asyncio.get_running_loop().run_in_executor(
            self._executor,
            _connect_pipe_instance,
            handle,
        )
        try:
            await asyncio.shield(future)
            peer = _validated_peer_identity(handle, client=True)
            return Win32NamedPipeStream(
                handle,
                server_end=True,
                peer=peer,
                creation_pipe_mode=self.pipe_mode,
            )
        except asyncio.CancelledError:
            _cancel_and_close_handle(handle, disconnect=True)
            with suppress(Exception):
                await asyncio.shield(future)
            raise
        except BaseException:
            _cancel_and_close_handle(handle, disconnect=True)
            raise
        finally:
            with self._lock:
                if self._accept_handle == handle:
                    self._accept_handle = None

    async def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handle = self._accept_handle
            self._accept_handle = None
        if handle is not None:
            _cancel_and_close_handle(handle, disconnect=True)
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)

    def _create_instance(self) -> int:
        api = _api()
        descriptor, attributes = _current_user_security_attributes(api)
        try:
            handle = api.kernel32.CreateNamedPipeW(
                self.pipe_name,
                _PIPE_ACCESS_DUPLEX | _FILE_FLAG_OVERLAPPED,
                self.pipe_mode,
                _PIPE_UNLIMITED_INSTANCES,
                self.buffer_bytes,
                self.buffer_bytes,
                0,
                ctypes.byref(attributes),
            )
        finally:
            api.kernel32.LocalFree(descriptor)
        value = _handle_value(handle)
        _make_non_inheritable(api, value)
        return value


async def connect_windows_named_pipe(
    pipe_name: str,
    *,
    timeout_seconds: float = 10.0,
) -> Win32NamedPipeStream:
    if _PIPE_NAME.fullmatch(pipe_name) is None:
        raise ValueError("production pipe name must contain a 256-bit random suffix")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="offeragent-pipe-connect")
    future = asyncio.get_running_loop().run_in_executor(
        executor,
        _connect_client_handle,
        pipe_name,
        int(timeout_seconds * 1000),
    )
    try:
        handle = await asyncio.shield(future)
    except asyncio.CancelledError:

        def close_late_handle(completed: asyncio.Future[int]) -> None:
            try:
                late_handle = completed.result()
            except BaseException:
                return
            _cancel_and_close_handle(late_handle, disconnect=False)

        future.add_done_callback(close_late_handle)
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    except BaseException:
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True, cancel_futures=True)
    try:
        peer = _validated_peer_identity(handle, client=False)
        return Win32NamedPipeStream(
            handle,
            server_end=False,
            peer=peer,
            creation_pipe_mode=0,
        )
    except BaseException:
        _cancel_and_close_handle(handle, disconnect=False)
        raise


def current_user_sid() -> str:
    api = _api()
    token = wintypes.HANDLE()
    if not api.advapi32.OpenProcessToken(api.kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        _raise_last_error("OpenProcessToken(current process) failed")
    try:
        return _token_user_sid(api, _handle_value(token))
    finally:
        api.kernel32.CloseHandle(token)


def current_session_id() -> int:
    api = _api()
    session = wintypes.DWORD()
    if not api.kernel32.ProcessIdToSessionId(api.kernel32.GetCurrentProcessId(), ctypes.byref(session)):
        _raise_last_error("ProcessIdToSessionId(current process) failed")
    return int(session.value)


def write_current_user_only_file(path: Path, content: bytes) -> None:
    """Create a non-inheritable file whose protected DACL has one current SID ACE."""

    api = _api()
    descriptor, attributes = _current_user_security_attributes(api)
    handle: int | None = None
    try:
        raw = api.kernel32.CreateFileW(
            os.path.abspath(os.fspath(path)),
            _GENERIC_WRITE,
            0,
            ctypes.byref(attributes),
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_WRITE_THROUGH | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        handle = _handle_value(raw)
        _make_non_inheritable(api, handle)
        offset = 0
        while offset < len(content):
            chunk = content[offset : offset + _MAX_WIN32_IO_CHUNK]
            buffer = ctypes.create_string_buffer(chunk, len(chunk))
            written = wintypes.DWORD()
            if not api.kernel32.WriteFile(handle, buffer, len(chunk), ctypes.byref(written), None):
                _raise_last_error("WriteFile(discovery material) failed")
            if written.value < 1:
                raise OSError("discovery material write made no progress")
            offset += int(written.value)
        if not api.kernel32.FlushFileBuffers(handle):
            _raise_last_error("FlushFileBuffers(discovery material) failed")
    finally:
        api.kernel32.LocalFree(descriptor)
        if handle is not None:
            api.kernel32.CloseHandle(handle)


def _crypt_data(value: bytes, *, protect: bool) -> bytes:
    api = _api()
    input_buffer = ctypes.create_string_buffer(value, len(value))
    entropy_buffer = ctypes.create_string_buffer(DpapiCurrentUserProtector._ENTROPY)
    input_blob = _DATA_BLOB(
        len(value),
        ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    entropy_blob = _DATA_BLOB(
        len(DpapiCurrentUserProtector._ENTROPY),
        ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    output_blob = _DATA_BLOB()
    description = wintypes.LPWSTR()
    if protect:
        ok = api.crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "OfferAgent Named Pipe discovery",
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    else:
        ok = api.crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    try:
        if not ok:
            _raise_last_error("DPAPI current-user operation failed")
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            api.kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))
        if description:
            api.kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))


def _current_user_security_attributes(api: _Win32Api) -> tuple[wintypes.LPVOID, _SECURITY_ATTRIBUTES]:
    sid = current_user_sid()
    security_descriptor = wintypes.LPVOID()
    if not api.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"D:P(A;;GA;;;{sid})",
        _SDDL_REVISION_1,
        ctypes.byref(security_descriptor),
        None,
    ):
        _raise_last_error("current-user security descriptor creation failed")
    attributes = _SECURITY_ATTRIBUTES(
        nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
        lpSecurityDescriptor=security_descriptor,
        bInheritHandle=False,
    )
    return security_descriptor, attributes


def _token_user_sid(api: _Win32Api, token: int) -> str:
    required = wintypes.DWORD()
    api.advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
    if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or required.value < ctypes.sizeof(_TOKEN_USER_VALUE):
        _raise_last_error("GetTokenInformation(TokenUser) sizing failed")
    buffer = ctypes.create_string_buffer(required.value)
    if not api.advapi32.GetTokenInformation(
        token,
        _TOKEN_USER,
        buffer,
        required.value,
        ctypes.byref(required),
    ):
        _raise_last_error("GetTokenInformation(TokenUser) failed")
    token_user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER_VALUE)).contents
    sid_text = wintypes.LPWSTR()
    if not api.advapi32.ConvertSidToStringSidW(token_user.User.Sid, ctypes.byref(sid_text)):
        _raise_last_error("ConvertSidToStringSidW failed")
    try:
        return cast(str, sid_text.value)
    finally:
        api.kernel32.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))


def _process_user_sid(api: _Win32Api, process_id: int) -> str:
    process = api.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    process_value = _handle_value(process)
    token = wintypes.HANDLE()
    try:
        if not api.advapi32.OpenProcessToken(process_value, _TOKEN_QUERY, ctypes.byref(token)):
            _raise_last_error("OpenProcessToken(peer) failed")
        return _token_user_sid(api, _handle_value(token))
    finally:
        if token:
            api.kernel32.CloseHandle(token)
        api.kernel32.CloseHandle(process_value)


def _validated_peer_identity(handle: int, *, client: bool) -> Win32PeerIdentity:
    api = _api()
    process_id = wintypes.ULONG()
    getter = api.kernel32.GetNamedPipeClientProcessId if client else api.kernel32.GetNamedPipeServerProcessId
    if not getter(handle, ctypes.byref(process_id)) or process_id.value < 1:
        _raise_last_error("Named Pipe peer PID validation failed")
    session = wintypes.DWORD()
    if not api.kernel32.ProcessIdToSessionId(process_id.value, ctypes.byref(session)):
        _raise_last_error("Named Pipe peer session validation failed")
    sid = _process_user_sid(api, int(process_id.value))
    current_sid = current_user_sid()
    current_session = current_session_id()
    if int(session.value) != current_session or sid != current_sid:
        raise PermissionError("Named Pipe peer is not the current SID in the current Windows session")
    return Win32PeerIdentity(int(process_id.value), int(session.value), sid)


def _connect_pipe_instance(handle: int) -> None:
    api = _api()
    event = api.kernel32.CreateEventW(None, True, False, None)
    event_value = _handle_value(event)
    overlapped = _OVERLAPPED(hEvent=event_value)
    try:
        if api.kernel32.ConnectNamedPipe(handle, ctypes.byref(overlapped)):
            return
        error = ctypes.get_last_error()
        if error == _ERROR_PIPE_CONNECTED:
            return
        if error != _ERROR_IO_PENDING:
            raise _winerror("ConnectNamedPipe failed", error)
        if api.kernel32.WaitForSingleObject(event_value, _INFINITE) != _WAIT_OBJECT_0:
            _raise_last_error("ConnectNamedPipe wait failed")
        transferred = wintypes.DWORD()
        if not api.kernel32.GetOverlappedResult(
            handle,
            ctypes.byref(overlapped),
            ctypes.byref(transferred),
            False,
        ):
            raise _winerror("ConnectNamedPipe completion failed", ctypes.get_last_error())
    finally:
        api.kernel32.CloseHandle(event_value)


def _connect_client_handle(pipe_name: str, timeout_ms: int) -> int:
    api = _api()
    deadline = time.monotonic() + (timeout_ms / 1000)
    value: int | None = None
    while value is None:
        handle = api.kernel32.CreateFileW(
            pipe_name,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OVERLAPPED | _SECURITY_SQOS_PRESENT | _SECURITY_IDENTIFICATION,
            None,
        )
        candidate = getattr(handle, "value", handle)
        if candidate not in {None, 0, _INVALID_HANDLE_VALUE}:
            value = int(candidate)
            break
        error = ctypes.get_last_error()
        if error not in {_ERROR_PIPE_BUSY, _ERROR_FILE_NOT_FOUND}:
            raise _winerror("CreateFileW(Named Pipe) failed", error)
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if remaining_ms == 0:
            raise TimeoutError("Named Pipe server did not become available")
        if not api.kernel32.WaitNamedPipeW(pipe_name, min(remaining_ms, 100)):
            wait_error = ctypes.get_last_error()
            if wait_error not in {_ERROR_PIPE_BUSY, _ERROR_FILE_NOT_FOUND, _ERROR_SEM_TIMEOUT}:
                raise _winerror("WaitNamedPipeW failed", wait_error)
            time.sleep(0.01)
    try:
        _make_non_inheritable(api, value)
        byte_mode = wintypes.DWORD(_PIPE_READMODE_BYTE)
        if not api.kernel32.SetNamedPipeHandleState(value, ctypes.byref(byte_mode), None, None):
            _raise_last_error("SetNamedPipeHandleState(byte mode) failed")
        return value
    except BaseException:
        api.kernel32.CloseHandle(value)
        raise


def _overlapped_call(
    handle: int,
    issue: Callable[[_Win32Api, int, Any, Any], Any],
    *,
    eof_is_empty: bool,
) -> int:
    api = _api()
    event = api.kernel32.CreateEventW(None, True, False, None)
    event_value = _handle_value(event)
    overlapped = _OVERLAPPED(hEvent=event_value)
    transferred = wintypes.DWORD()
    try:
        if issue(api, handle, ctypes.byref(transferred), ctypes.byref(overlapped)):
            return int(transferred.value)
        error = ctypes.get_last_error()
        if eof_is_empty and error in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_PIPE_NOT_CONNECTED}:
            return 0
        if error != _ERROR_IO_PENDING:
            raise _winerror("Named Pipe I/O submission failed", error)
        if api.kernel32.WaitForSingleObject(event_value, _INFINITE) != _WAIT_OBJECT_0:
            _raise_last_error("Named Pipe overlapped wait failed")
        if not api.kernel32.GetOverlappedResult(
            handle,
            ctypes.byref(overlapped),
            ctypes.byref(transferred),
            False,
        ):
            error = ctypes.get_last_error()
            if eof_is_empty and error in {
                _ERROR_BROKEN_PIPE,
                _ERROR_NO_DATA,
                _ERROR_PIPE_NOT_CONNECTED,
                _ERROR_OPERATION_ABORTED,
            }:
                return 0
            raise _winerror("Named Pipe overlapped completion failed", error)
        return int(transferred.value)
    finally:
        api.kernel32.CloseHandle(event_value)


def _cancel_and_close_handle(handle: int, *, disconnect: bool) -> None:
    api = _api()
    api.kernel32.CancelIoEx(handle, None)
    if disconnect:
        api.kernel32.DisconnectNamedPipe(handle)
    api.kernel32.CloseHandle(handle)


def _make_non_inheritable(api: _Win32Api, handle: int) -> None:
    if not api.kernel32.SetHandleInformation(handle, _HANDLE_FLAG_INHERIT, 0):
        _raise_last_error("SetHandleInformation(non-inheritable) failed")


def _handle_dacl_sddl(api: _Win32Api, handle: int) -> str:
    required = wintypes.DWORD()
    api.advapi32.GetKernelObjectSecurity(handle, _DACL_SECURITY_INFORMATION, None, 0, ctypes.byref(required))
    if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or required.value < 1:
        _raise_last_error("GetKernelObjectSecurity sizing failed")
    descriptor = ctypes.create_string_buffer(required.value)
    if not api.advapi32.GetKernelObjectSecurity(
        handle,
        _DACL_SECURITY_INFORMATION,
        descriptor,
        required.value,
        ctypes.byref(required),
    ):
        _raise_last_error("GetKernelObjectSecurity failed")
    text = wintypes.LPWSTR()
    if not api.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        descriptor,
        _SDDL_REVISION_1,
        _DACL_SECURITY_INFORMATION,
        ctypes.byref(text),
        None,
    ):
        _raise_last_error("security descriptor SDDL conversion failed")
    try:
        return cast(str, text.value)
    finally:
        api.kernel32.LocalFree(ctypes.cast(text, wintypes.HLOCAL))


def _handle_value(handle: object) -> int:
    if isinstance(handle, int):
        value: int | None = handle
    else:
        value = cast(int | None, getattr(handle, "value", None))
    if value is None or value == 0 or value == _INVALID_HANDLE_VALUE:
        _raise_last_error("Win32 handle creation failed")
    return int(value)


def _winerror(message: str, code: int) -> OSError:
    return OSError(code, f"{message}: {ctypes.FormatError(code).strip()}")


def _raise_last_error(message: str) -> NoReturn:
    raise _winerror(message, ctypes.get_last_error())


__all__ = [
    "PIPE_REJECT_REMOTE_CLIENTS",
    "DpapiCurrentUserProtector",
    "Win32NamedPipeListener",
    "Win32NamedPipeStream",
    "Win32PeerIdentity",
    "Win32PipeSecuritySnapshot",
    "connect_windows_named_pipe",
    "current_session_id",
    "current_user_sid",
    "write_current_user_only_file",
]
