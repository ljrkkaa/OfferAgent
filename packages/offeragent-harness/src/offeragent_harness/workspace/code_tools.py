"""Claude Code-style local workspace tools with explicit process boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from offeragent_harness.models import thaw_json
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import CancellationToken, OperationCancelled, VaultEntry, VaultEntryKind, VaultRead
from offeragent_harness.tools import (
    MAX_TOOL_RESULT_SOURCE_REFERENCES,
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffect,
    SideEffectClass,
    SideEffectKind,
    SideEffectState,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
    vault_source_reference,
)
from offeragent_harness.workspace.filesystem import VaultFilesystemError

CODE_TOOL_VERSION = "1"
CODE_TOOL_OUTPUT_LIMIT_BYTES = 256 * 1024


class CodeToolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CodeToolLimits:
    max_file_bytes: int = 16 * 1024 * 1024
    max_glob_results: int = MAX_TOOL_RESULT_SOURCE_REFERENCES
    max_glob_scan_files: int = 100_000
    max_glob_depth: int = 64
    max_grep_results: int = MAX_TOOL_RESULT_SOURCE_REFERENCES
    max_line_chars: int = 16_384
    max_result_bytes: int = 240 * 1024
    grep_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (
            self.max_file_bytes < 1
            or self.max_glob_results < 1
            or self.max_glob_scan_files < self.max_glob_results
            or self.max_glob_depth < 1
            or self.max_grep_results < 1
            or self.max_line_chars < 1
            or self.max_result_bytes < 1
            or self.grep_timeout_seconds <= 0
        ):
            raise ValueError("Code tool limits must be positive")


@runtime_checkable
class CodeWorkspaceSource(Protocol):
    @property
    def workspace_id(self) -> str: ...

    async def read_bounded(
        self,
        relative_path: str,
        max_bytes: int,
        cancellation: CancellationToken,
    ) -> VaultRead: ...

    async def list(self, relative_path: str, cancellation: CancellationToken) -> tuple[VaultEntry, ...]: ...


def _schema(properties: Mapping[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_PATH = {"type": "string", "minLength": 1, "maxLength": 1024}
_OPTIONAL_PATH = {"type": "string", "maxLength": 1024}
_HASH = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
_SOURCE_REF = {"type": "string", "minLength": 1, "maxLength": 2200}
_LINE = {
    "type": "object",
    "properties": {
        "number": {"type": "integer", "minimum": 1},
        "text": {"type": "string", "maxLength": 16_384},
        "truncated": {"type": "boolean"},
    },
    "required": ["number", "text", "truncated"],
    "additionalProperties": False,
}


_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="glob",
        version=CODE_TOOL_VERSION,
        description=(
            "Find workspace files with an explicit POSIX glob pattern. "
            "To search the Vault root, omit the optional path argument; '.' and '..' are not valid workspace paths."
        ),
        input_schema=_schema(
            {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 1024},
                "path": _OPTIONAL_PATH,
                "maxResults": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_RESULT_SOURCE_REFERENCES},
            },
            ["pattern"],
        ),
        output_schema=_schema(
            {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 1024},
                "path": _OPTIONAL_PATH,
                "files": {"type": "array", "items": _PATH, "maxItems": MAX_TOOL_RESULT_SOURCE_REFERENCES},
                "truncated": {"type": "boolean"},
            },
            ["pattern", "path", "files", "truncated"],
        ),
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.READ,
        side_effect_class=SideEffectClass.READ,
        required_capabilities=frozenset({"workspace.read"}),
        concurrency_safe=True,
        idempotent=True,
        retryable=True,
        timeout_ms=20_000,
        output_limit_bytes=CODE_TOOL_OUTPUT_LIMIT_BYTES,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    ),
    ToolDefinition(
        name="grep",
        version=CODE_TOOL_VERSION,
        description=(
            "Search workspace text with ripgrep regular-expression semantics. "
            "To search the Vault root, omit the optional path argument; '.' and '..' are not valid workspace paths."
        ),
        input_schema=_schema(
            {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 4096},
                "path": _OPTIONAL_PATH,
                "glob": {"type": "string", "minLength": 1, "maxLength": 1024},
                "caseInsensitive": {"type": "boolean"},
                "maxResults": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_RESULT_SOURCE_REFERENCES},
            },
            ["pattern"],
        ),
        output_schema=_schema(
            {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 4096},
                "matches": {
                    "type": "array",
                    "maxItems": MAX_TOOL_RESULT_SOURCE_REFERENCES,
                    "items": _schema(
                        {
                            "path": _PATH,
                            "line": {"type": "integer", "minimum": 1},
                            "column": {"type": "integer", "minimum": 1},
                            "text": {"type": "string", "maxLength": 16_384},
                            "truncated": {"type": "boolean"},
                            "sourceRef": _SOURCE_REF,
                        },
                        ["path", "line", "column", "text", "truncated", "sourceRef"],
                    ),
                },
                "truncated": {"type": "boolean"},
            },
            ["pattern", "matches", "truncated"],
        ),
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.READ,
        side_effect_class=SideEffectClass.READ,
        required_capabilities=frozenset({"workspace.read"}),
        concurrency_safe=True,
        idempotent=True,
        retryable=True,
        timeout_ms=35_000,
        output_limit_bytes=CODE_TOOL_OUTPUT_LIMIT_BYTES,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    ),
    ToolDefinition(
        name="read",
        version=CODE_TOOL_VERSION,
        description="Read an exact bounded UTF-8 line range from one workspace file.",
        input_schema=_schema(
            {
                "path": _PATH,
                "startLine": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
                "endLine": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
                "maxLines": {"type": "integer", "minimum": 1, "maximum": 2_000},
            },
            ["path"],
        ),
        output_schema=_schema(
            {
                "path": _PATH,
                "startLine": {"type": "integer", "minimum": 0},
                "endLine": {"type": "integer", "minimum": 0},
                "totalLines": {"type": "integer", "minimum": 0},
                "contentHash": _HASH,
                "lines": {"type": "array", "items": _LINE, "maxItems": 2_000},
                "truncated": {"type": "boolean"},
                "sourceRef": _SOURCE_REF,
            },
            ["path", "startLine", "endLine", "totalLines", "contentHash", "lines", "truncated", "sourceRef"],
        ),
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.READ,
        side_effect_class=SideEffectClass.READ,
        required_capabilities=frozenset({"workspace.read"}),
        concurrency_safe=True,
        idempotent=True,
        retryable=True,
        timeout_ms=15_000,
        output_limit_bytes=CODE_TOOL_OUTPUT_LIMIT_BYTES,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    ),
)


ToolHandler = Callable[[Mapping[str, Any], CancellationToken], Awaitable[tuple[dict[str, Any], tuple[str, ...], str]]]


@dataclass(frozen=True, slots=True)
class _Operation:
    definition: ToolDefinition
    handler: ToolHandler


class CodeToolExecutor:
    """Run immutable read-only workspace operations without a shell intermediary."""

    def __init__(
        self,
        *,
        workspace_id: str,
        source: CodeWorkspaceSource,
        workspace_root: Path,
        ripgrep_path: Path,
        limits: CodeToolLimits | None = None,
    ) -> None:
        if not workspace_id or source.workspace_id != workspace_id:
            raise ValueError("Code tool workspace identity is invalid")
        root = workspace_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Code tool root must be a directory")
        resolved_executable = ripgrep_path.resolve(strict=True)
        if not resolved_executable.is_file() or resolved_executable.name.casefold() != "rg.exe":
            raise ValueError("Code tool ripgrep executable is invalid")
        definitions = code_tool_definitions()
        handlers: tuple[ToolHandler, ...] = (self._glob, self._grep, self._read)
        self._workspace_id = workspace_id
        self._source = source
        self._root = root
        self._ripgrep = resolved_executable
        self._limits = limits or CodeToolLimits()
        self._operations = MappingProxyType(
            {
                (definition.name, definition.version): _Operation(definition, handler)
                for definition, handler in zip(definitions, handlers, strict=True)
            }
        )

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(item.definition for item in self._operations.values())

    async def execute(self, tool_call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        if tool_call.workspace_id != self._workspace_id:
            return self._failure(tool_call, "workspace_mismatch", "Workspace tool belongs to another workspace")
        operation = self._operations.get((tool_call.name, tool_call.version))
        if operation is None or operation.definition.fingerprint != tool_call.definition_fingerprint:
            return self._failure(tool_call, "workspace_tool_unavailable", "Workspace tool is not registered")
        try:
            data, source_refs, summary = await operation.handler(thaw_json(tool_call.arguments), cancellation)
        except OperationCancelled:
            raise
        except VaultFilesystemError as error:
            return self._failure(tool_call, f"workspace_{error.code.value}", str(error))
        except CodeToolError as error:
            return self._failure(tool_call, error.code, str(error))
        # A search result is provenance, not an independent state change.  One
        # auditable observation records the file-access operation while the
        # result keeps its bounded per-file source references.
        effects = (
            SideEffect(
                SideEffectKind.READ,
                SideEffectState.OBSERVED,
                f"workspace:{self._workspace_id}",
                None,
                None,
                {
                    "toolCallId": tool_call.tool_call_id,
                    "toolName": tool_call.name,
                    "argsHash": tool_call.args_hash,
                    "sourceCount": len(source_refs),
                },
            ),
        )
        return ToolResult(
            tool_call_id=tool_call.tool_call_id,
            status=ToolResultStatus.SUCCEEDED,
            data=data,
            user_visible_summary=summary,
            artifact_ids=(),
            source_refs=source_refs,
            side_effects=effects,
            retryable=False,
            before_state=None,
            after_state=None,
            error=None,
            source_references=tuple(
                vault_source_reference(workspace_id=self._workspace_id, path=_path_from_ref(item))
                for item in source_refs
            ),
        )

    async def _glob(
        self,
        arguments: Mapping[str, Any],
        cancellation: CancellationToken,
    ) -> tuple[dict[str, Any], tuple[str, ...], str]:
        pattern = _glob_regex(_required_string(arguments, "pattern"))
        base = _optional_relative_path(arguments.get("path", ""))
        limit = _bounded_integer(
            arguments.get("maxResults", self._limits.max_glob_results),
            1,
            self._limits.max_glob_results,
        )
        files, scan_truncated = await _collect_files(
            self._source,
            base,
            max_depth=self._limits.max_glob_depth,
            max_files=self._limits.max_glob_scan_files,
            cancellation=cancellation,
        )

        def relative_to_base(item: VaultEntry) -> str:
            return item.relative_path.removeprefix(base + "/") if base else item.relative_path

        all_matches = tuple(
            item.relative_path for item in files if pattern.fullmatch(relative_to_base(item)) is not None
        )
        matched = all_matches[:limit]
        result_truncated = scan_truncated or len(all_matches) > limit
        source_refs = tuple(_source_ref(path) for path in matched)
        return (
            {
                "pattern": _required_string(arguments, "pattern"),
                "path": base,
                "files": list(matched),
                "truncated": result_truncated,
            },
            source_refs,
            f"Found {len(matched)} workspace file(s)",
        )

    async def _grep(
        self,
        arguments: Mapping[str, Any],
        cancellation: CancellationToken,
    ) -> tuple[dict[str, Any], tuple[str, ...], str]:
        pattern = _required_string(arguments, "pattern")
        base = _optional_relative_path(arguments.get("path", ""))
        target = _workspace_directory(self._root, base)
        limit = _bounded_integer(
            arguments.get("maxResults", self._limits.max_grep_results),
            1,
            self._limits.max_grep_results,
        )
        command = [
            str(self._ripgrep),
            "--json",
            "--no-config",
            "--line-number",
            "--column",
            "--color=never",
            "--max-filesize",
            str(self._limits.max_file_bytes),
            "--case-sensitive" if arguments.get("caseInsensitive") is not True else "--ignore-case",
        ]
        glob = arguments.get("glob")
        if glob is not None:
            if not isinstance(glob, str) or not glob or len(glob) > 1024:
                raise CodeToolError("grep_glob_invalid", "grep glob must be a non-empty bounded string")
            _glob_regex(glob)
            command.extend(("--glob", glob))
        command.extend(("--", pattern, str(target)))
        stdout, stderr, returncode, output_truncated = await _run_process(
            command,
            cancellation,
            self._limits.grep_timeout_seconds,
            self._limits.max_result_bytes,
        )
        if returncode not in {0, 1}:
            detail = _bounded_text(stderr.decode("utf-8", errors="replace"), 2_048)[0]
            raise CodeToolError("grep_failed", detail or f"ripgrep exited with status {returncode}")
        matches: list[dict[str, Any]] = []
        truncated = output_truncated
        raw_events = stdout.splitlines()
        for index, raw in enumerate(raw_events):
            cancellation.checkpoint()
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as error:
                if output_truncated and index == len(raw_events) - 1:
                    break
                raise CodeToolError("grep_protocol_invalid", "ripgrep emitted invalid JSON") from error
            if event.get("type") != "match":
                continue
            payload = event.get("data")
            if not isinstance(payload, Mapping):
                raise CodeToolError("grep_protocol_invalid", "ripgrep match event was invalid")
            relative_path = _ripgrep_relative_path(self._root, payload)
            line = _positive_integer(payload.get("line_number"), "ripgrep line number")
            submatches = payload.get("submatches")
            if not isinstance(submatches, list) or not submatches or not isinstance(submatches[0], Mapping):
                raise CodeToolError("grep_protocol_invalid", "ripgrep match column was missing")
            column = _nonnegative_integer(submatches[0].get("start"), "ripgrep match column") + 1
            text_value = _ripgrep_text(payload.get("lines"), "ripgrep match text")
            text, text_truncated = _bounded_text(text_value.rstrip("\r\n"), self._limits.max_line_chars)
            if len(matches) >= limit:
                truncated = True
                continue
            matches.append(
                {
                    "path": relative_path,
                    "line": line,
                    "column": column,
                    "text": text,
                    "truncated": text_truncated,
                    "sourceRef": _source_ref(relative_path, line),
                }
            )
        source_refs = tuple(item["sourceRef"] for item in matches)
        return (
            {"pattern": pattern, "matches": matches, "truncated": truncated},
            source_refs,
            f"ripgrep found {len(matches)} match(es)",
        )

    async def _read(
        self,
        arguments: Mapping[str, Any],
        cancellation: CancellationToken,
    ) -> tuple[dict[str, Any], tuple[str, ...], str]:
        path = _required_relative_path(arguments, "path")
        read = await self._source.read_bounded(path, self._limits.max_file_bytes, cancellation)
        if read.truncated:
            raise CodeToolError("read_too_large", "workspace file exceeds the read limit")
        try:
            text = read.content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CodeToolError("read_not_utf8", "workspace Read accepts UTF-8 text files only") from error
        start = _bounded_integer(arguments.get("startLine", 1), 1, 10_000_000)
        end_argument = arguments.get("endLine")
        max_lines = _bounded_integer(arguments.get("maxLines", 2_000), 1, 2_000)
        if end_argument is None:
            end = start + max_lines - 1
        else:
            end = _bounded_integer(end_argument, start, 10_000_000)
            if end - start + 1 > max_lines:
                end = start + max_lines - 1
        lines = text.splitlines()
        selected = []
        for number in range(start, min(end, len(lines)) + 1):
            value, truncated = _bounded_text(lines[number - 1], self._limits.max_line_chars)
            selected.append({"number": number, "text": value, "truncated": truncated})
        source_ref = (
            _source_ref(read.entry.relative_path, start, max(start, min(end, len(lines))))
            if selected
            else _source_ref(read.entry.relative_path)
        )
        content_hash = read.entry.content_hash
        if content_hash is None:
            content_hash = "sha256:" + hashlib.sha256(read.content).hexdigest()
        return (
            {
                "path": read.entry.relative_path,
                "startLine": start if selected else 0,
                "endLine": selected[-1]["number"] if selected else 0,
                "totalLines": len(lines),
                "contentHash": content_hash,
                "lines": selected,
                "truncated": end < len(lines),
                "sourceRef": source_ref,
            },
            (source_ref,),
            f"Read {len(selected)} line(s) from {read.entry.relative_path}",
        )

    @staticmethod
    def _failure(tool_call: ToolCall, code: str, message: str) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call.tool_call_id,
            status=ToolResultStatus.FAILED,
            data=None,
            user_visible_summary=message,
            artifact_ids=(),
            source_refs=(),
            side_effects=(),
            retryable=False,
            before_state=None,
            after_state=None,
            error=ToolError(code=code, message=message, retryable=False, cancelled=False, details={}),
            source_references=(),
        )


def code_tool_definitions() -> tuple[ToolDefinition, ...]:
    return _DEFINITIONS


async def _collect_files(
    source: CodeWorkspaceSource,
    base: str,
    *,
    max_depth: int,
    max_files: int,
    cancellation: CancellationToken,
) -> tuple[tuple[VaultEntry, ...], bool]:
    pending: list[tuple[str, int]] = [(base, 0)]
    files: list[VaultEntry] = []
    while pending:
        path, depth = pending.pop()
        if depth > max_depth:
            raise CodeToolError("glob_depth_exceeded", "glob path depth exceeds the configured limit")
        entries = await source.list(path, cancellation)
        for entry in reversed(entries):
            cancellation.checkpoint()
            if entry.kind is VaultEntryKind.DIRECTORY:
                pending.append((entry.relative_path, depth + 1))
            elif entry.kind is VaultEntryKind.FILE:
                files.append(entry)
                if len(files) >= max_files:
                    files.sort(key=lambda item: item.relative_path.casefold())
                    return tuple(files), True
    files.sort(key=lambda item: item.relative_path.casefold())
    return tuple(files), False


async def _run_process(
    command: Sequence[str],
    cancellation: CancellationToken,
    timeout_seconds: float,
    output_limit_bytes: int,
) -> tuple[bytes, bytes, int, bool]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    stdout_task = asyncio.create_task(_drain_process_stream(process.stdout, output_limit_bytes))
    stderr_task = asyncio.create_task(_drain_process_stream(process.stderr, output_limit_bytes))
    wait_task = asyncio.create_task(process.wait())
    cancelled = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            {wait_task, cancelled}, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED
        )
        if wait_task in done:
            stdout, stdout_truncated = await stdout_task
            stderr, stderr_truncated = await stderr_task
            return stdout, stderr, process.returncode or 0, stdout_truncated or stderr_truncated
        if cancelled in done:
            cancellation.checkpoint()
        raise CodeToolError("grep_timed_out", "ripgrep exceeded the configured execution timeout")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        for task in (stdout_task, stderr_task, wait_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, wait_task, return_exceptions=True)
        if not cancelled.done():
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)


async def _drain_process_stream(
    stream: asyncio.StreamReader | None,
    limit: int,
) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    captured = bytearray()
    truncated = False
    while chunk := await stream.read(65_536):
        remaining = limit - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    return bytes(captured), truncated


def _glob_regex(pattern: str) -> re.Pattern[str]:
    if not pattern or len(pattern) > 1024 or "\\" in pattern or "\x00" in pattern:
        raise CodeToolError("glob_pattern_invalid", "glob pattern must be a bounded POSIX path pattern")
    if any(segment in {".", ".."} for segment in pattern.split("/")):
        raise CodeToolError("glob_pattern_invalid", "glob pattern must not contain relative traversal segments")
    pieces: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    pieces.append("(?:.*/)?")
                    index += 1
                else:
                    pieces.append(".*")
                continue
            pieces.append("[^/]*")
        elif character == "?":
            pieces.append("[^/]")
        elif character == "[":
            close = pattern.find("]", index + 1)
            if close < 0:
                raise CodeToolError("glob_pattern_invalid", "glob character class is not closed")
            content = pattern[index + 1 : close]
            if not content or "/" in content:
                raise CodeToolError("glob_pattern_invalid", "glob character class is invalid")
            if content[0] == "!":
                content = "^" + content[1:]
            pieces.append("[" + content + "]")
            index = close
        else:
            pieces.append(re.escape(character))
        index += 1
    pieces.append("$")
    try:
        return re.compile("".join(pieces))
    except re.error as error:
        raise CodeToolError("glob_pattern_invalid", "glob pattern is invalid") from error


def _required_string(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise CodeToolError("tool_arguments_invalid", f"{key} must be a non-empty string")
    return value


def _required_relative_path(arguments: Mapping[str, Any], key: str) -> str:
    return _optional_relative_path(_required_string(arguments, key))


def _optional_relative_path(value: object) -> str:
    if not isinstance(value, str) or len(value) > 1024 or "\\" in value or "\x00" in value:
        raise CodeToolError("workspace_path_invalid", "workspace path must be a bounded POSIX relative path")
    if not value:
        return ""
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CodeToolError("workspace_path_invalid", "workspace path must not escape its root")
    return path.as_posix()


def _workspace_directory(root: Path, relative_path: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative_path).parts) if relative_path else root
    try:
        resolved = candidate.resolve(strict=True)
        if os.path.commonpath((str(root), str(resolved))) != str(root) or not resolved.is_dir():
            raise CodeToolError("workspace_path_invalid", "grep path must be a workspace directory")
    except OSError as error:
        raise CodeToolError("workspace_path_invalid", "grep path could not be resolved") from error
    return resolved


def _ripgrep_relative_path(root: Path, payload: Mapping[str, Any]) -> str:
    path_value = payload.get("path")
    if not isinstance(path_value, Mapping):
        raise CodeToolError("grep_protocol_invalid", "ripgrep match path was missing")
    raw = _ripgrep_text(path_value, "ripgrep match path")
    try:
        resolved = Path(raw).resolve(strict=True)
        relative = resolved.relative_to(root).as_posix()
    except (OSError, ValueError) as error:
        raise CodeToolError("grep_path_invalid", "ripgrep returned a path outside the workspace") from error
    return _optional_relative_path(relative)


def _ripgrep_text(value: object, label: str) -> str:
    text = value.get("text") if isinstance(value, Mapping) else None
    if not isinstance(text, str):
        raise CodeToolError("grep_protocol_invalid", f"{label} was missing")
    return text


def _positive_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CodeToolError("grep_protocol_invalid", f"{label} was invalid")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CodeToolError("grep_protocol_invalid", f"{label} was invalid")
    return value


def _bounded_integer(value: object, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > maximum:
        raise CodeToolError("tool_arguments_invalid", f"integer must be in {minimum}..{maximum}")
    return value


def _bounded_text(value: str, maximum: int) -> tuple[str, bool]:
    return (value, False) if len(value) <= maximum else (value[:maximum], True)


def _source_ref(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    if start_line is None:
        return f"workspace:{path}"
    return f"workspace:{path}#L{start_line}-L{end_line or start_line}"


def _path_from_ref(value: str) -> str:
    if not value.startswith("workspace:"):
        raise CodeToolError("source_reference_invalid", "workspace source reference is invalid")
    return value.removeprefix("workspace:").split("#", 1)[0]


__all__ = [
    "CODE_TOOL_OUTPUT_LIMIT_BYTES",
    "CODE_TOOL_VERSION",
    "CodeToolError",
    "CodeToolExecutor",
    "CodeToolLimits",
    "code_tool_definitions",
]
