"""Canonical records shared by the hash-pinned local Runtime artifact."""

from __future__ import annotations

import ctypes
import os
import re
from dataclasses import dataclass

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROTOCOL_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_RESERVED_NAMES = frozenset(
    {
        "CON",
        "CONIN$",
        "CONOUT$",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_ALLOWED_KINDS = frozenset({"asset", "executable", "license", "skill", "web"})
_WINDOWS_X64_PE_MACHINE = 0x8664


class RuntimeManifestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProtocolCompatibility:
    minimum: str
    maximum: str
    schema_hash: str

    def __post_init__(self) -> None:
        minimum = _protocol_tuple(self.minimum)
        maximum = _protocol_tuple(self.maximum)
        if minimum > maximum:
            raise RuntimeManifestError("protocol_range_invalid", "protocol compatibility range is inverted")
        if not _SHA256.fullmatch(self.schema_hash):
            raise RuntimeManifestError("schema_hash_invalid", "Runtime schemaHash is invalid")


@dataclass(frozen=True, slots=True)
class RuntimeFileRecord:
    path: str
    byte_length: int
    sha256: str
    kind: str
    authenticode: bool = False

    def __post_init__(self) -> None:
        _validate_runtime_path(self.path)
        if self.path == "development-runtime-manifest.json":
            raise RuntimeManifestError("manifest_self_reference", "manifest cannot include itself")
        if self.byte_length < 0 or self.byte_length > 2 * 1024 * 1024 * 1024:
            raise RuntimeManifestError("file_size_invalid", "Runtime file size is outside limits")
        if not _SHA256.fullmatch(self.sha256):
            raise RuntimeManifestError("file_hash_invalid", "Runtime file SHA-256 is invalid")
        if self.kind not in _ALLOWED_KINDS:
            raise RuntimeManifestError("file_kind_invalid", "Runtime file kind is unsupported")
        if self.authenticode and not self.path.casefold().endswith(".exe"):
            raise RuntimeManifestError(
                "authenticode_target_invalid",
                "Authenticode verification is only valid for PE executables",
            )


def native_windows_architecture() -> str:
    """Return the native Windows machine architecture, rejecting emulation."""

    if os.name != "nt":
        raise RuntimeError("native Windows architecture is unavailable")
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        is_wow64_process2 = kernel32.IsWow64Process2
        get_current_process.argtypes = []
        get_current_process.restype = ctypes.c_void_p
        is_wow64_process2.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ushort),
            ctypes.POINTER(ctypes.c_ushort),
        ]
        is_wow64_process2.restype = ctypes.c_int
        process_machine = ctypes.c_ushort()
        native_machine = ctypes.c_ushort()
        if not is_wow64_process2(
            get_current_process(),
            ctypes.byref(process_machine),
            ctypes.byref(native_machine),
        ):
            raise OSError(ctypes.get_last_error(), "IsWow64Process2 failed")
    except (AttributeError, OSError) as error:
        raise RuntimeError("native Windows architecture could not be queried") from error
    if native_machine.value == _WINDOWS_X64_PE_MACHINE:
        return "x64"
    raise RuntimeError("native Windows architecture is unsupported")


def _protocol_tuple(value: str) -> tuple[int, int]:
    match = _PROTOCOL_VERSION.fullmatch(value)
    if match is None:
        raise RuntimeManifestError("protocol_version_invalid", "protocol version is invalid")
    return int(match.group(1)), int(match.group(2))


def _validate_runtime_path(value: str) -> None:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise RuntimeManifestError("runtime_path_invalid", "Runtime path must be canonical POSIX text")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise RuntimeManifestError("runtime_path_absolute", "absolute Runtime paths are forbidden")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RuntimeManifestError("runtime_path_traversal", "Runtime path traversal is forbidden")
    for part in parts:
        if part[-1] in {".", " "} or any(character in _WINDOWS_FORBIDDEN or ord(character) < 32 for character in part):
            raise RuntimeManifestError("runtime_path_windows", "Runtime path is ambiguous on Windows")
        if part.split(".", 1)[0].upper() in _RESERVED_NAMES:
            raise RuntimeManifestError("runtime_path_device", "Runtime path contains a reserved device name")


__all__ = [
    "ProtocolCompatibility",
    "RuntimeFileRecord",
    "RuntimeManifestError",
    "native_windows_architecture",
]
