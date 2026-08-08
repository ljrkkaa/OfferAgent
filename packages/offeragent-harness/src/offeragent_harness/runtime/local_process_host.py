"""Hash-pinned helper for bundled ProcessSupervisor profiles.

The helper deliberately exposes only fixed entry points. It is never a
general command interpreter and its own code never opens a socket:

* ``shell-runtime-info`` emits bounded local Runtime metadata;
* ``hook-continue`` validates one bounded Hook input and returns ``continue``.
* ``document-extract`` parses one staged PDF/image through bounded canonical
  JSON as the current Windows user inside the Run's scratch directory.

The executable and its complete Runtime tree are pinned by the local manifest.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from offeragent_harness.documents import (
    DocumentParseError,
    DocumentParser,
    DocumentParserConfig,
    decode_canonical_request,
    encode_canonical_request,
    execute_canonical_request,
)
from offeragent_harness.documents.adapters import build_bundled_parser

from .document_parser_profile import (
    BUNDLED_DOCUMENT_PARSER_CONFIG,
    DOCUMENT_PARSER_FIXED_ARGUMENTS,
    DOCUMENT_PARSER_MAX_REQUEST_BYTES,
    DOCUMENT_PARSER_MAX_RESPONSE_BYTES,
)

_MAX_HOOK_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 50_000
_PROCESS_HOST_NAME = "offeragent-process-host.exe"
_DOCUMENT_DIRECTORY = re.compile(r"^document-[0-9]{3}$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class LocalProcessHostError(RuntimeError):
    """Sanitized process-host failure; input bodies are never included."""


class _DuplicateKey(ValueError):
    pass


class _NeverCancelled:
    """The supervisor cancels this isolated operation by terminating it."""

    def checkpoint(self) -> None:
        return


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


def run_hook_continue(stdin: BinaryIO, stdout: BinaryIO) -> int:
    payload = _read_stream_bounded(stdin, _MAX_HOOK_BYTES)
    value = _parse_json_object(payload, maximum_bytes=_MAX_HOOK_BYTES)
    if not value:
        raise LocalProcessHostError("hook_input_empty")
    stdout.write(b'{"decision":"continue"}\n')
    stdout.flush()
    return 0


def run_document_extract(
    stdin: BinaryIO,
    stdout: BinaryIO,
    *,
    cwd: Path | None = None,
    parser_factory: Callable[[DocumentParserConfig], DocumentParser] | None = None,
) -> int:
    """Execute one bounded canonical parse request against one staged source."""

    payload = _read_stream_bounded(stdin, DOCUMENT_PARSER_MAX_REQUEST_BYTES)
    try:
        request = decode_canonical_request(payload)
    except DocumentParseError as error:
        raise LocalProcessHostError("document_request_invalid") from error
    # A JSON string such as ``scratch/.\\document-000`` must not become valid
    # merely because pathlib normalizes it while constructing DocumentSource.
    if encode_canonical_request(request) != payload:
        raise LocalProcessHostError("document_source_path_noncanonical")
    _validate_staged_document_path(
        request.source.absolute_path,
        cwd=Path.cwd() if cwd is None else Path(cwd),
    )
    factory = build_bundled_parser if parser_factory is None else parser_factory
    parser = factory(BUNDLED_DOCUMENT_PARSER_CONFIG)
    response = execute_canonical_request(
        payload,
        parser=parser,
        cancellation=_NeverCancelled(),
        maximum_response_bytes=DOCUMENT_PARSER_MAX_RESPONSE_BYTES,
    )
    _write_stream_complete(stdout, response)
    stdout.flush()
    return 0


def _validate_staged_document_path(source: Path, *, cwd: Path) -> None:
    if not cwd.is_absolute() or not source.is_absolute():
        raise LocalProcessHostError("document_source_path_invalid")
    document_directory = source.parent
    if (
        source.name != "source.bin"
        or _DOCUMENT_DIRECTORY.fullmatch(document_directory.name) is None
        or document_directory.parent != cwd
        or source != cwd / document_directory.name / "source.bin"
    ):
        raise LocalProcessHostError("document_source_path_invalid")

    try:
        cwd_info = cwd.lstat()
        directory_info = document_directory.lstat()
        source_info = source.lstat()
        resolved_cwd = cwd.resolve(strict=True)
        resolved_directory = document_directory.resolve(strict=True)
        resolved_source = source.resolve(strict=True)
    except OSError as error:
        raise LocalProcessHostError("document_source_path_unavailable") from error
    if (
        not stat.S_ISDIR(cwd_info.st_mode)
        or not stat.S_ISDIR(directory_info.st_mode)
        or not stat.S_ISREG(source_info.st_mode)
        or source_info.st_nlink != 1
        or _is_link_or_reparse(cwd, cwd_info)
        or _is_link_or_reparse(document_directory, directory_info)
        or _is_link_or_reparse(source, source_info)
        or resolved_directory.parent != resolved_cwd
        or resolved_source.parent != resolved_directory
    ):
        raise LocalProcessHostError("document_source_path_unsafe")
    try:
        resolved_source.relative_to(resolved_cwd)
    except ValueError as error:
        raise LocalProcessHostError("document_source_path_escape") from error


def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


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


def _read_stream_bounded(stream: BinaryIO, maximum: int) -> bytes:
    payload = stream.read(maximum + 1)
    if not payload or len(payload) > maximum:
        raise LocalProcessHostError("input_size_invalid")
    return payload


def _write_stream_complete(stream: BinaryIO, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = stream.write(payload[offset:])
        if written is None or written <= 0:
            raise LocalProcessHostError("output_write_failed")
        offset += written


def main(
    arguments: list[str] | None = None,
    *,
    info_loader: Callable[[Path, Path], VerifiedRuntimeInfo],
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
        if args == list(DOCUMENT_PARSER_FIXED_ARGUMENTS):
            return run_document_extract(sys.stdin.buffer, sys.stdout.buffer)
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
    "main",
    "run_document_extract",
    "run_hook_continue",
]
