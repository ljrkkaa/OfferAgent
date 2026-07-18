"""Current-user DPAPI SecretStore with SID-only filesystem ACLs."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from offeragent_harness.error_codes import ResourceConflictCause, ResourceNotFoundCause
from offeragent_harness.ports.secrets import (
    SecretHandle,
    SecretInput,
    SecretKind,
    SecretMetadata,
)

from .process_lock import ProcessLock
from .windows_security import current_windows_identity

_CRYPTPROTECT_UI_FORBIDDEN = 0x00000001
_SDDL_REVISION_1 = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_FOLDERID_LOCAL_APP_DATA = uuid.UUID("f1b32785-6fba-4fcf-9d55-7b8e7f157091")
_PROVIDER_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_SCOPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}")
_FILE_VERSION = 1
T = TypeVar("T")


class SecretStoreError(RuntimeError):
    pass


class SecretNotFound(SecretStoreError, ResourceNotFoundCause):
    pass


class SecretVersionConflict(SecretStoreError, ResourceConflictCause):
    pass


class SecretCorrupt(SecretStoreError):
    pass


class SecretDecryptionError(SecretStoreError):
    pass


class SecretConsumerError(SecretStoreError):
    pass


class SecretBindingMismatch(SecretStoreError):
    """The opaque handle exists but is not authorized for this consumer."""


_SECRET_ENVELOPE_FIELDS = {
    "version",
    "handle",
    "scopeId",
    "kind",
    "providerId",
    "secretVersion",
    "createdAt",
    "rotatedAt",
    "ciphertext",
    "ciphertextSha256",
}


def parse_secret_envelope_metadata(raw: bytes, *, filename: str) -> SecretMetadata:
    """Validate one encrypted envelope without decrypting or exposing its value."""

    metadata, _ = _parse_secret_envelope(raw, filename=filename)
    return metadata


def _parse_secret_envelope(
    raw: bytes,
    *,
    filename: str,
    expected_handle: SecretHandle | None = None,
) -> tuple[SecretMetadata, bytes]:
    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate secret envelope field")
            value[key] = item
        return value

    decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=closed_object)
    if not isinstance(decoded, Mapping) or set(decoded) != _SECRET_ENVELOPE_FIELDS:
        raise ValueError("secret envelope fields are incompatible")
    string_fields = ("handle", "scopeId", "kind", "providerId", "createdAt", "rotatedAt", "ciphertext")
    if any(not isinstance(decoded[field], str) for field in string_fields):
        raise ValueError("secret envelope string field is invalid")
    if type(decoded["version"]) is not int or decoded["version"] != _FILE_VERSION:
        raise ValueError("secret envelope version is incompatible")
    if type(decoded["secretVersion"]) is not int:
        raise ValueError("secret envelope secret version is invalid")
    ciphertext_hash = decoded["ciphertextSha256"]
    if not isinstance(ciphertext_hash, str) or re.fullmatch(r"[0-9a-f]{64}", ciphertext_hash) is None:
        raise ValueError("secret ciphertext hash is invalid")
    handle = SecretHandle(decoded["handle"])
    if expected_handle is not None and handle != expected_handle:
        raise ValueError("secret envelope handle mismatch")
    if filename != f"{_handle_digest(handle)}.secret":
        raise ValueError("secret filename does not match handle")
    ciphertext = base64.b64decode(decoded["ciphertext"], validate=True)
    if hashlib.sha256(ciphertext).hexdigest() != ciphertext_hash:
        raise ValueError("secret ciphertext hash mismatch")
    metadata = SecretMetadata(
        handle,
        decoded["scopeId"],
        SecretKind(decoded["kind"]),
        decoded["providerId"],
        decoded["secretVersion"],
        _parse_time(decoded["createdAt"]),
        _parse_time(decoded["rotatedAt"]),
    )
    return metadata, ciphertext


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    ]


class DpapiCurrentUserProtector:
    def __init__(self) -> None:
        _require_windows()
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    def protect(self, plaintext: bytearray, *, entropy: bytes) -> bytes:
        if not plaintext or not entropy:
            raise ValueError("DPAPI plaintext and entropy cannot be empty")
        input_buffer, input_blob = _blob_from_mutable(plaintext)
        entropy_buffer, entropy_blob = _blob_from_bytes(entropy)
        output = _DataBlob()
        try:
            if not self._crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                "OfferAgent secret",
                ctypes.byref(entropy_blob),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            ):
                raise _last_error("CryptProtectData")
            return ctypes.string_at(output.data, output.size)
        finally:
            del input_buffer, entropy_buffer
            if output.data:
                self._kernel32.LocalFree(output.data)

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytearray:
        if not ciphertext or not entropy:
            raise ValueError("DPAPI ciphertext and entropy cannot be empty")
        input_buffer, input_blob = _blob_from_bytes(ciphertext)
        entropy_buffer, entropy_blob = _blob_from_bytes(entropy)
        output = _DataBlob()
        description = wintypes.LPWSTR()
        try:
            if not self._crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                ctypes.byref(description),
                ctypes.byref(entropy_blob),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            ):
                raise SecretDecryptionError("DPAPI rejected the current user or entropy binding")
            return bytearray(ctypes.string_at(output.data, output.size))
        finally:
            del input_buffer, entropy_buffer
            if output.data:
                ctypes.memset(output.data, 0, output.size)
                self._kernel32.LocalFree(output.data)
            if description:
                self._kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))


class WindowsDpapiSecretStore:
    def __init__(
        self,
        root: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        new_uuid: Callable[[], uuid.UUID] | None = None,
        protector: DpapiCurrentUserProtector | None = None,
    ) -> None:
        _require_windows()
        self.root = (root or (_known_local_app_data() / "OfferAgent" / "secrets")).resolve()
        if not self.root.is_absolute():
            raise ValueError("SecretStore root must be absolute")
        self.root.mkdir(parents=True, exist_ok=True)
        _set_sid_only_dacl(self.root, directory=True)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._new_uuid = new_uuid or uuid.uuid4
        self._protector = protector or DpapiCurrentUserProtector()
        self._thread_lock = threading.RLock()
        sid_hash = hashlib.sha256(current_windows_identity().sid.encode("ascii")).hexdigest()[:24]
        self._mutex_name = f"Local\\OfferAgent.SecretStore.{sid_hash}"

    def create(
        self,
        *,
        scope_id: str,
        kind: SecretKind,
        provider_id: str,
        secret: SecretInput,
    ) -> SecretMetadata:
        _provider(provider_id)
        _scope(scope_id)
        plaintext = secret.take()
        try:
            with self._locked():
                handle = SecretHandle(f"secret:v1:{self._new_uuid().hex}")
                path = self._path(handle)
                if path.exists():
                    raise SecretStoreError("generated SecretHandle already exists")
                now = _aware(self._now())
                metadata = SecretMetadata(handle, scope_id, kind, provider_id, 1, now, now)
                ciphertext = self._protector.protect(plaintext, entropy=_entropy(handle, scope_id))
                self._write(path, metadata, ciphertext, replace=False)
                return metadata
        finally:
            _zero(plaintext)

    def rotate(
        self,
        handle: SecretHandle,
        *,
        scope_id: str,
        expected_version: int,
        secret: SecretInput,
    ) -> SecretMetadata:
        if expected_version < 1:
            raise ValueError("expected_version must be positive")
        plaintext = secret.take()
        try:
            with self._locked():
                metadata, _ = self._read(handle, scope_id=scope_id)
                if metadata.version != expected_version:
                    raise SecretVersionConflict(
                        f"secret expected version {expected_version}, actual {metadata.version}"
                    )
                updated = SecretMetadata(
                    handle,
                    scope_id,
                    metadata.kind,
                    metadata.provider_id,
                    metadata.version + 1,
                    metadata.created_at,
                    _aware(self._now()),
                )
                ciphertext = self._protector.protect(plaintext, entropy=_entropy(handle, scope_id))
                self._write(self._path(handle), updated, ciphertext, replace=True)
                return updated
        finally:
            _zero(plaintext)

    def metadata(self, handle: SecretHandle, *, scope_id: str) -> SecretMetadata:
        with self._locked():
            metadata, _ = self._read(handle, scope_id=scope_id)
            return metadata

    def list_metadata(self, *, scope_id: str) -> tuple[SecretMetadata, ...]:
        _scope(scope_id)
        with self._locked():
            items = [self._read_path(path)[0] for path in sorted(self.root.glob("*.secret"))]
            return tuple(
                sorted(
                    (item for item in items if item.scope_id == scope_id),
                    key=lambda item: item.handle.opaque_id,
                )
            )

    def consume(
        self,
        handle: SecretHandle,
        *,
        scope_id: str,
        expected_kind: SecretKind,
        expected_provider_id: str,
        consumer: Callable[[memoryview], T],
    ) -> T:
        if not isinstance(expected_kind, SecretKind):
            raise ValueError("expected_kind must be a SecretKind")
        _provider(expected_provider_id)
        with self._locked():
            metadata, ciphertext = self._read(handle, scope_id=scope_id)
            if metadata.kind is not expected_kind or metadata.provider_id != expected_provider_id:
                raise SecretBindingMismatch("SecretHandle is not bound to the expected consumer")
            plaintext = self._protector.unprotect(ciphertext, entropy=_entropy(handle, scope_id))
        view = memoryview(plaintext)
        try:
            try:
                result = consumer(view)
            except Exception:
                raise SecretConsumerError("secret consumer failed") from None
            if isinstance(result, (str, bytes, bytearray, memoryview)):
                raise SecretConsumerError("secret consumer cannot return plaintext-capable values")
            return result
        finally:
            view.release()
            _zero(plaintext)

    def delete(self, handle: SecretHandle, *, scope_id: str, expected_version: int) -> None:
        if expected_version < 1:
            raise ValueError("expected_version must be positive")
        with self._locked():
            metadata, _ = self._read(handle, scope_id=scope_id)
            if metadata.version != expected_version:
                raise SecretVersionConflict(f"secret expected version {expected_version}, actual {metadata.version}")
            try:
                self._path(handle).unlink()
            except FileNotFoundError as error:
                raise SecretNotFound("SecretHandle does not exist") from error

    def _read(self, handle: SecretHandle, *, scope_id: str) -> tuple[SecretMetadata, bytes]:
        _scope(scope_id)
        metadata, ciphertext = self._read_path(self._path(handle), expected_handle=handle)
        if metadata.scope_id != scope_id:
            raise SecretNotFound("SecretHandle does not exist in this scope")
        return metadata, ciphertext

    def _read_path(
        self,
        path: Path,
        *,
        expected_handle: SecretHandle | None = None,
    ) -> tuple[SecretMetadata, bytes]:
        try:
            metadata, ciphertext = _parse_secret_envelope(
                path.read_bytes(),
                filename=path.name,
                expected_handle=expected_handle,
            )
            return metadata, ciphertext
        except FileNotFoundError as error:
            raise SecretNotFound("SecretHandle does not exist") from error
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise SecretCorrupt("encrypted secret envelope is corrupt") from error

    def _write(self, path: Path, metadata: SecretMetadata, ciphertext: bytes, *, replace: bool) -> None:
        payload = {
            "version": _FILE_VERSION,
            "handle": metadata.handle.opaque_id,
            "scopeId": metadata.scope_id,
            "kind": metadata.kind.value,
            "providerId": metadata.provider_id,
            "secretVersion": metadata.version,
            "createdAt": _time(metadata.created_at),
            "rotatedAt": _time(metadata.rotated_at),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "ciphertextSha256": hashlib.sha256(ciphertext).hexdigest(),
        }
        encoded = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        temporary = self.root / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            _set_sid_only_dacl(temporary, directory=False)
            if not replace and path.exists():
                raise SecretStoreError("SecretHandle already exists")
            os.replace(temporary, path)
            _set_sid_only_dacl(path, directory=False)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _path(self, handle: SecretHandle) -> Path:
        path = self.root / f"{_handle_digest(handle)}.secret"
        if path.parent != self.root:
            raise SecretStoreError("SecretHandle escaped store root")
        return path

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            lock = ProcessLock(self._mutex_name)
            lock.acquire(timeout_ms=30_000)
            try:
                yield
            finally:
                lock.release()


def _blob_from_mutable(value: bytearray) -> tuple[Any, _DataBlob]:
    buffer = (ctypes.c_ubyte * len(value)).from_buffer(value)
    return buffer, _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))


def _blob_from_bytes(value: bytes) -> tuple[Any, _DataBlob]:
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    return buffer, _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))


def _entropy(handle: SecretHandle, scope_id: str) -> bytes:
    return hashlib.sha256(f"OfferAgent.SecretStore.v1\0{scope_id}\0{handle.opaque_id}".encode("ascii")).digest()


def _handle_digest(handle: SecretHandle) -> str:
    return hashlib.sha256(handle.opaque_id.encode("ascii")).hexdigest()


def _provider(value: str) -> None:
    if _PROVIDER_RE.fullmatch(value) is None:
        raise ValueError("provider_id must be canonical lowercase ASCII")


def _scope(value: str) -> None:
    if _SCOPE_RE.fullmatch(value) is None:
        raise ValueError("scope_id is invalid")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("SecretStore clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _time(value: datetime) -> str:
    return _aware(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware(parsed)


def _zero(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def _known_local_app_data() -> Path:
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
    guid = _guid(_FOLDERID_LOCAL_APP_DATA)
    value = wintypes.LPWSTR()
    status = int(shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(value)))
    if status != 0:
        raise SecretStoreError(f"SHGetKnownFolderPath failed with HRESULT 0x{status & 0xFFFFFFFF:08x}")
    try:
        path = value.value
        if not path:
            raise SecretStoreError("Known Folder API returned an empty path")
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_dir():
            raise SecretStoreError("LocalAppData Known Folder is not a directory")
        return resolved
    finally:
        ole32.CoTaskMemFree(ctypes.cast(value, ctypes.c_void_p))


def _guid(value: uuid.UUID) -> _Guid:
    return _Guid(
        value.fields[0],
        value.fields[1],
        value.fields[2],
        (ctypes.c_ubyte * 8)(*value.bytes[8:]),
    )


def _set_sid_only_dacl(path: Path, *, directory: bool) -> None:
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
        if not advapi32.SetFileSecurityW(str(path), information, descriptor):
            raise _last_error("SetFileSecurityW")
    finally:
        kernel32.LocalFree(descriptor)


def _require_windows() -> None:
    if os.name != "nt":
        raise SecretStoreError("Windows DPAPI SecretStore is only available on Windows")


def _last_error(operation: str) -> SecretStoreError:
    code = ctypes.get_last_error()
    return SecretStoreError(f"{operation} failed with Win32 error {code}: {ctypes.FormatError(code)}")


__all__ = [
    "DpapiCurrentUserProtector",
    "SecretBindingMismatch",
    "SecretConsumerError",
    "SecretCorrupt",
    "SecretDecryptionError",
    "SecretNotFound",
    "SecretStoreError",
    "SecretVersionConflict",
    "WindowsDpapiSecretStore",
    "parse_secret_envelope_metadata",
]
