"""One explicit PowerShell tool, equivalent to Claude Code's shell boundary."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from offeragent_harness.models import thaw_json
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import CancellationToken, OperationCancelled
from offeragent_harness.tools import (
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
)

POWERSHELL_TOOL_VERSION = "1"
POWERSHELL_OUTPUT_LIMIT_BYTES = 256 * 1024


class PowerShellToolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


_DEFINITION = ToolDefinition(
    name="shell.powershell",
    version=POWERSHELL_TOOL_VERSION,
    description="Execute one PowerShell command from an explicit workspace working directory.",
    input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1, "maxLength": 32_768},
            "workingDirectory": {"type": "string", "maxLength": 1024},
            "timeoutMs": {"type": "integer", "minimum": 1_000, "maximum": 120_000},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    output_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "exitCode": {"type": "integer"},
            "stdout": {"type": "string", "maxLength": POWERSHELL_OUTPUT_LIMIT_BYTES},
            "stderr": {"type": "string", "maxLength": POWERSHELL_OUTPUT_LIMIT_BYTES},
            "truncated": {"type": "boolean"},
        },
        "required": ["exitCode", "stdout", "stderr", "truncated"],
        "additionalProperties": False,
    },
    executor_location=ExecutorLocation.LOCAL,
    risk=RiskClass.EXECUTE,
    side_effect_class=SideEffectClass.EXECUTE,
    required_capabilities=frozenset({"shell.execute"}),
    concurrency_safe=False,
    idempotent=False,
    retryable=False,
    timeout_ms=125_000,
    output_limit_bytes=POWERSHELL_OUTPUT_LIMIT_BYTES,
    preflight_mode=PreflightMode.NONE,
    preflight_provider=None,
    approval_evidence=ApprovalEvidence.NONE,
    result_sensitivity=ResultSensitivity.WORKSPACE,
)


class PowerShellToolExecutor:
    """Execute through a discovered PowerShell binary, never through ``shell=True``."""

    def __init__(self, *, workspace_id: str, workspace_root: Path, executable: str | None = None) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")
        root = workspace_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("PowerShell workspace root must be a directory")
        candidate = executable or shutil.which("powershell.exe")
        if candidate is None:
            raise ValueError("PowerShell executable is required")
        path = Path(candidate).resolve(strict=True)
        if not path.is_file():
            raise ValueError("PowerShell executable is not a file")
        self._workspace_id = workspace_id
        self._root = root
        self._executable = path

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (_DEFINITION,)

    async def execute(self, tool_call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        if tool_call.workspace_id != self._workspace_id:
            return self._failure(
                tool_call,
                "powershell_workspace_mismatch",
                "PowerShell tool belongs to another workspace",
            )
        if (tool_call.name, tool_call.version) != (
            _DEFINITION.name,
            _DEFINITION.version,
        ) or tool_call.definition_fingerprint != _DEFINITION.fingerprint:
            return self._failure(tool_call, "powershell_tool_unavailable", "PowerShell tool is not registered")
        arguments = thaw_json(tool_call.arguments)
        try:
            command = _command(arguments)
            cwd = _working_directory(self._root, arguments.get("workingDirectory", ""))
            timeout_ms = _timeout(arguments.get("timeoutMs", 120_000))
            stdout, stderr, exit_code, output_truncated = await self._execute(
                command,
                cwd,
                timeout_ms / 1_000,
                cancellation,
            )
        except OperationCancelled:
            raise
        except PowerShellToolError as error:
            return self._failure(tool_call, error.code, str(error))
        stdout_text, stdout_truncated = _truncate(stdout.decode("utf-8", errors="replace"))
        stderr_text, stderr_truncated = _truncate(stderr.decode("utf-8", errors="replace"))
        data: dict[str, Any] = {
            "exitCode": exit_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "truncated": output_truncated or stdout_truncated or stderr_truncated,
        }
        return ToolResult(
            tool_call_id=tool_call.tool_call_id,
            status=ToolResultStatus.SUCCEEDED,
            data=data,
            user_visible_summary=f"PowerShell exited with code {exit_code}",
            artifact_ids=(),
            source_refs=(),
            side_effects=(
                SideEffect(
                    SideEffectKind.PROCESS,
                    SideEffectState.ATTEMPTED,
                    f"powershell:{self._executable.name}",
                    None,
                    None,
                    {"workspaceId": self._workspace_id, "workingDirectory": str(cwd)},
                ),
            ),
            retryable=False,
            before_state=None,
            after_state=None,
            error=None,
            source_references=(),
        )

    async def _execute(
        self,
        command: str,
        cwd: Path,
        timeout_seconds: float,
        cancellation: CancellationToken,
    ) -> tuple[bytes, bytes, int, bool]:
        process = await asyncio.create_subprocess_exec(
            str(self._executable),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout_task = asyncio.create_task(_drain_stream(process.stdout, POWERSHELL_OUTPUT_LIMIT_BYTES))
        stderr_task = asyncio.create_task(_drain_stream(process.stderr, POWERSHELL_OUTPUT_LIMIT_BYTES))
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
            raise PowerShellToolError("powershell_timed_out", "PowerShell exceeded its configured timeout")
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


def powershell_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (_DEFINITION,)


def _command(arguments: Mapping[str, Any]) -> str:
    value = arguments.get("command")
    if not isinstance(value, str) or not value or len(value) > 32_768 or "\x00" in value:
        raise PowerShellToolError("powershell_command_invalid", "PowerShell command must be a bounded non-empty string")
    return value


def _working_directory(root: Path, value: object) -> Path:
    if not isinstance(value, str) or len(value) > 1024 or "\\" in value or "\x00" in value:
        raise PowerShellToolError(
            "powershell_directory_invalid",
            "workingDirectory must be a workspace-relative POSIX path",
        )
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {".", "..", ""} for part in relative.parts):
        raise PowerShellToolError("powershell_directory_invalid", "workingDirectory must not escape the workspace")
    candidate = root.joinpath(*relative.parts) if value else root
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise PowerShellToolError("powershell_directory_invalid", "workingDirectory could not be resolved") from error
    if os.path.commonpath((str(root), str(resolved))) != str(root) or not resolved.is_dir():
        raise PowerShellToolError("powershell_directory_invalid", "workingDirectory must be a workspace directory")
    return resolved


def _timeout(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1_000 or value > 120_000:
        raise PowerShellToolError("powershell_timeout_invalid", "timeoutMs must be in 1000..120000")
    return value


def _truncate(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= POWERSHELL_OUTPUT_LIMIT_BYTES:
        return value, False
    return encoded[:POWERSHELL_OUTPUT_LIMIT_BYTES].decode("utf-8", errors="ignore"), True


async def _drain_stream(
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


__all__ = [
    "POWERSHELL_OUTPUT_LIMIT_BYTES",
    "POWERSHELL_TOOL_VERSION",
    "PowerShellToolError",
    "PowerShellToolExecutor",
    "powershell_tool_definitions",
]
