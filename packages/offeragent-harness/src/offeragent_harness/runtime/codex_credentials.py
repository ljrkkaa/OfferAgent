"""Read-only broker for a local Codex ChatGPT subscription login.

The broker deliberately consumes only the current access token and account ID.
It never uses the shared refresh token and never writes Codex's ``auth.json``;
Codex remains the sole refresh owner.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from types import MappingProxyType
from typing import Any

from offeragent_harness.providers.openai_responses import ModelCredentialLease, ModelCredentialSourceError

_MAX_AUTH_BYTES = 64 * 1024
_MAX_TOKEN_BYTES = 32 * 1024
_MAX_ACCOUNT_BYTES = 512
_READ_ATTEMPTS = 3
_RETRY_SECONDS = 0.05
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("user", _SidAndAttributes)]


class CodexFileCredentialSource:
    """Lease the current Codex access token from a stable, local file snapshot."""

    def __init__(self, auth_path: Path | None = None, *, now: Callable[[], float] = time.time) -> None:
        self._path = auth_path or default_codex_auth_path()
        self._now = now
        _validate_local_path_shape(self._path)

    @contextmanager
    def lease(self) -> Iterator[ModelCredentialLease]:
        token_text, account_id = self._load_credentials_with_retry()
        token = bytearray(token_text.encode("utf-8"))
        token_text = ""
        credential_fingerprint = hashlib.sha256(token).hexdigest()
        account_fingerprint = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
        headers: Mapping[str, str] = MappingProxyType(
            {
                "ChatGPT-Account-ID": account_id,
                "originator": "codex_cli_rs",
                "User-Agent": "codex_cli_rs/0.0.0 (OfferAgent local subscription)",
            }
        )
        view = memoryview(token)
        try:
            yield ModelCredentialLease(
                material=view,
                headers=headers,
                credential_fingerprint=credential_fingerprint,
                account_fingerprint=account_fingerprint,
            )
        finally:
            view.release()
            for index in range(len(token)):
                token[index] = 0
            token.clear()

    def _load_credentials_with_retry(self) -> tuple[str, str]:
        last_error: BaseException | None = None
        for attempt in range(_READ_ATTEMPTS):
            try:
                raw = _read_stable_local_file(self._path)
                return _parse_auth(raw, now=self._now())
            except (OSError, TypeError, ValueError) as error:
                last_error = error
                if attempt + 1 < _READ_ATTEMPTS:
                    time.sleep(_RETRY_SECONDS)
        raise ModelCredentialSourceError("auth_required") from last_error

    def __repr__(self) -> str:
        return "CodexFileCredentialSource(<local Codex credential store>)"


def default_codex_auth_path() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        root = Path(configured)
    elif os.name == "nt":
        from .windows_process import current_user_profile_directory

        root = current_user_profile_directory() / ".codex"
    else:
        root = Path.home() / ".codex"
    path = root / "auth.json"
    _validate_local_path_shape(path)
    return path


def _read_stable_local_file(path: Path) -> bytearray:
    _validate_parent_chain(path.parent)
    before = path.lstat()
    _validate_auth_stat(path, before)
    if os.name == "nt" and _windows_owner_sid(path) != _current_windows_sid():
        raise ValueError("Codex credential owner differs from the current Windows user")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        _validate_auth_stat(path, opened)
        if not _same_object(before, opened):
            raise ValueError("Codex credential identity changed before it was opened")
        raw = bytearray()
        while len(raw) <= _MAX_AUTH_BYTES:
            chunk = os.read(descriptor, min(16_384, _MAX_AUTH_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after_read = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_AUTH_BYTES:
        _zero(raw)
        raise ValueError("Codex credential file exceeds its byte limit")
    if not raw:
        raise ValueError("Codex credential file is empty")
    after = path.lstat()
    _validate_auth_stat(path, after)
    if (
        not _same_snapshot(before, opened)
        or not _same_snapshot(opened, after_read)
        or not _same_snapshot(after_read, after)
    ):
        _zero(raw)
        raise ValueError("Codex credential file changed while it was read")
    if os.name == "nt" and _windows_owner_sid(path) != _current_windows_sid():
        _zero(raw)
        raise ValueError("Codex credential owner changed while it was read")
    final = path.lstat()
    if not _same_snapshot(after, final):
        _zero(raw)
        raise ValueError("Codex credential identity changed after owner validation")
    return raw


def _parse_auth(raw: bytearray, *, now: float) -> tuple[str, str]:
    try:
        decoded = raw.decode("utf-8", errors="strict")
        root = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise ValueError("Codex credential file is not strict JSON") from error
    finally:
        _zero(raw)
        raw.clear()
    if not isinstance(root, dict):
        raise ValueError("Codex credential root must be an object")
    mode = root.get("auth_mode")
    if not isinstance(mode, str) or mode.casefold().replace("_", "") not in {"chatgpt", "chatgptauthtokens"}:
        raise ValueError("Codex is not logged in with ChatGPT")
    tokens = root.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("Codex ChatGPT token bundle is missing")
    access_token = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not isinstance(access_token, str) or not isinstance(account_id, str):
        raise ValueError("Codex access token or account binding is missing")
    if (
        not access_token
        or len(access_token) > _MAX_TOKEN_BYTES
        or access_token.strip() != access_token
        or not access_token.isascii()
        or any(character in access_token for character in "\x00\r\n")
    ):
        raise ValueError("Codex access token has an unsafe shape")
    if (
        not account_id
        or len(account_id) > _MAX_ACCOUNT_BYTES
        or account_id.strip() != account_id
        or not account_id.isascii()
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in account_id)
    ):
        raise ValueError("Codex account binding has an unsafe shape")
    expires_at = _jwt_expiry(access_token)
    if expires_at <= now + 30:
        raise ValueError("Codex access token is expired")
    tokens.clear()
    root.clear()
    return access_token, account_id


def _jwt_expiry(token: str) -> float:
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        raise ValueError("Codex access token is not a JWT")
    payload_text = parts[1]
    padding = "=" * (-len(payload_text) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode((payload_text + padding).encode("ascii"))
        payload = json.loads(
            payload_bytes.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Codex access token payload is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("Codex access token payload must be an object")
    expiry = payload.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        raise ValueError("Codex access token expiry is missing")
    return float(expiry)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Codex credential JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _validate_local_path_shape(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("Codex credential path must be absolute")
    text = str(path)
    if text.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")):
        raise ValueError("Codex credential path must be a normal local path")
    if os.name == "nt":
        drive = path.drive
        remainder = text[len(drive) :]
        if not drive or ":" in remainder:
            raise ValueError("Codex credential path cannot use a device path or alternate data stream")


def _validate_parent_chain(parent: Path) -> None:
    current = parent
    while True:
        value = current.lstat()
        if current.is_symlink() or _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
            raise ValueError("Codex credential parent chain is not a regular local directory tree")
        if current.parent == current:
            break
        current = current.parent


def _validate_auth_stat(path: Path, value: os.stat_result) -> None:
    if path.is_symlink() or _is_reparse(value) or not stat.S_ISREG(value.st_mode):
        raise ValueError("Codex credential path is not a regular file")
    if value.st_nlink != 1:
        raise ValueError("Codex credential path must have exactly one hard link")
    if value.st_size < 1 or value.st_size > _MAX_AUTH_BYTES:
        raise ValueError("Codex credential file size is invalid")


def _is_reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _same_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return _same_object(first, second) and (
        first.st_nlink,
        first.st_size,
        first.st_mtime_ns,
        getattr(first, "st_file_attributes", 0),
    ) == (
        second.st_nlink,
        second.st_size,
        second.st_mtime_ns,
        getattr(second, "st_file_attributes", 0),
    )


def _windows_owner_sid(path: Path) -> str:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = int(
        advapi32.GetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
    )
    if status != 0:
        raise OSError(status, "Codex credential owner is unavailable")
    try:
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(owner, ctypes.byref(text)):
            raise OSError(ctypes.get_last_error(), "Codex credential owner SID is unavailable")
        try:
            sid = text.value
            if not sid:
                raise OSError("Codex credential owner SID is empty")
            return sid
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.LocalFree(descriptor)


def _current_windows_sid() -> str:
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
        raise OSError(ctypes.get_last_error(), "current Windows token is unavailable")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise OSError(ctypes.get_last_error(), "current Windows SID size is unavailable")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            ctypes.cast(buffer, ctypes.c_void_p),
            required,
            ctypes.byref(required),
        ):
            raise OSError(ctypes.get_last_error(), "current Windows SID is unavailable")
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(token_user.user.sid, ctypes.byref(text)):
            raise OSError(ctypes.get_last_error(), "current Windows SID text is unavailable")
        try:
            sid = text.value
            if not sid:
                raise OSError("current Windows SID text is empty")
            return sid
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def _zero(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


__all__ = [
    "CodexFileCredentialSource",
    "default_codex_auth_path",
]
