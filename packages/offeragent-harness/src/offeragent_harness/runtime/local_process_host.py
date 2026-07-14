"""Signed, zero-network helper for bundled ProcessSupervisor profiles.

The helper deliberately exposes only three fixed entry points.  It is never a
general command interpreter and it never opens a socket:

* ``shell-runtime-info`` emits bounded, signed release metadata;
* ``hook-continue`` validates one bounded Hook input and returns ``continue``.

The executable is independently Authenticode signed and its exact bytes are
also pinned by the detached Ed25519 Runtime manifest.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from .release_manifest import ReleaseKeyring, ReleaseVerificationError, parse_manifest
from .release_trust import load_embedded_release_keys

_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_SIGNATURE_BYTES = 1024
_MAX_HOOK_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 50_000
_PROCESS_HOST_NAME = "offeragent-process-host.exe"


class LocalProcessHostError(RuntimeError):
    """Sanitized process-host failure; input bodies are never included."""


class _DuplicateKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedRuntimeInfo:
    runtime_version: str
    core_version: str
    tool_abi_version: str
    protocol_minimum: str
    protocol_maximum: str
    build_commit: str

    @property
    def value(self) -> Mapping[str, str]:
        return {
            "buildCommit": self.build_commit,
            "coreVersion": self.core_version,
            "protocolMaximum": self.protocol_maximum,
            "protocolMinimum": self.protocol_minimum,
            "runtimeVersion": self.runtime_version,
            "toolAbiVersion": self.tool_abi_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.value) + b"\n"


def load_verified_runtime_info(runtime_root: Path, executable: Path) -> VerifiedRuntimeInfo:
    """Verify the signed manifest and the currently executing helper image."""

    try:
        root = runtime_root.resolve(strict=True)
        image = executable.resolve(strict=True)
        image.relative_to(root)
    except (OSError, ValueError) as error:
        raise LocalProcessHostError("runtime_identity_invalid") from error
    if image.name.casefold() != _PROCESS_HOST_NAME or not image.is_file():
        raise LocalProcessHostError("process_host_identity_invalid")

    manifest_bytes = _read_bounded(root / "runtime-manifest.json", _MAX_MANIFEST_BYTES)
    signature = _read_bounded(root / "runtime-manifest.sig", _MAX_SIGNATURE_BYTES)
    try:
        manifest = parse_manifest(manifest_bytes)
        ReleaseKeyring(load_embedded_release_keys()).verify(
            manifest.signing_key_id,
            manifest_bytes,
            signature,
        )
    except ReleaseVerificationError as error:
        raise LocalProcessHostError("runtime_manifest_untrusted") from error
    record = manifest.by_path.get(_PROCESS_HOST_NAME)
    if record is None or record.kind != "executable" or not record.authenticode:
        raise LocalProcessHostError("process_host_manifest_record_missing")
    digest, length = _digest_file(image)
    if record.sha256 != digest or record.byte_length != length:
        raise LocalProcessHostError("process_host_manifest_identity_mismatch")
    return VerifiedRuntimeInfo(
        runtime_version=manifest.runtime_version,
        core_version=manifest.core_version,
        tool_abi_version=manifest.tool_abi_version,
        protocol_minimum=manifest.protocol.minimum,
        protocol_maximum=manifest.protocol.maximum,
        build_commit=manifest.build_commit,
    )


def run_hook_continue(stdin: BinaryIO, stdout: BinaryIO) -> int:
    payload = _read_stream_bounded(stdin, _MAX_HOOK_BYTES)
    value = _parse_json_object(payload, maximum_bytes=_MAX_HOOK_BYTES)
    if not value:
        raise LocalProcessHostError("hook_input_empty")
    stdout.write(b'{"decision":"continue"}\n')
    stdout.flush()
    return 0


def _parse_json_object(payload: bytes, *, maximum_bytes: int) -> dict[str, Any]:
    if not payload or len(payload) > maximum_bytes:
        raise LocalProcessHostError("json_size_invalid")
    try:
        text = payload.decode("utf-8", errors="strict")
        if text.startswith("\ufeff"):
            raise ValueError("BOM")
        value = json.loads(
            text,
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, _DuplicateKey) as error:
        raise LocalProcessHostError("json_invalid") from error
    if not isinstance(value, dict):
        raise LocalProcessHostError("json_object_required")
    _validate_json_complexity(value)
    return value


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(value)


def _validate_json_complexity(value: object) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise LocalProcessHostError("json_complexity_exceeded")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _read_bounded(path: Path, maximum: int) -> bytes:
    try:
        with path.open("rb", buffering=0) as stream:
            payload = stream.read(maximum + 1)
    except OSError as error:
        raise LocalProcessHostError("release_file_unavailable") from error
    if not payload or len(payload) > maximum:
        raise LocalProcessHostError("release_file_size_invalid")
    return payload


def _read_stream_bounded(stream: BinaryIO, maximum: int) -> bytes:
    payload = stream.read(maximum + 1)
    if not payload or len(payload) > maximum:
        raise LocalProcessHostError("input_size_invalid")
    return payload


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    try:
        with path.open("rb", buffering=0) as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                length += len(chunk)
    except OSError as error:
        raise LocalProcessHostError("process_host_unreadable") from error
    return f"sha256:{digest.hexdigest()}", length


def main(
    arguments: list[str] | None = None,
    *,
    info_loader: Callable[[Path, Path], VerifiedRuntimeInfo] = load_verified_runtime_info,
) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    try:
        info = info_loader(
            Path(sys.executable).resolve(strict=True).parent,
            Path(sys.executable),
        )
        if args == ["shell-runtime-info"]:
            sys.stdout.buffer.write(info.canonical_bytes())
            sys.stdout.buffer.flush()
            return 0
        if args == ["hook-continue"]:
            return run_hook_continue(sys.stdin.buffer, sys.stdout.buffer)
    except BaseException:
        try:
            sys.stderr.buffer.write(b"offeragent-process-host: operation failed\n")
            sys.stderr.buffer.flush()
        except OSError:
            pass
    return 2


__all__ = [
    "LocalProcessHostError",
    "VerifiedRuntimeInfo",
    "load_verified_runtime_info",
    "main",
    "run_hook_continue",
]
