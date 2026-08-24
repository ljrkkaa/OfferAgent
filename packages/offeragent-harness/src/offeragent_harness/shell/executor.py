"""Tool Kernel adapter for trusted Shell command profiles."""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from jsonschema import Draft202012Validator

from offeragent_harness.models import thaw_json
from offeragent_harness.ports import (
    CancellationToken,
    Clock,
    ProcessArtifactBudget,
    ProcessOutputEncoding,
    ProcessOwnerKind,
    ProcessStdinMode,
    ProcessSupervisor,
    SupervisedProcessRequest,
)
from offeragent_harness.tools import (
    SideEffect,
    SideEffectKind,
    SideEffectState,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
)

from .profiles import ShellCommandProfile

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ShellToolExecutor:
    def __init__(
        self,
        profiles: Sequence[ShellCommandProfile],
        *,
        processes: ProcessSupervisor,
        clock: Clock,
        artifact_budget: ProcessArtifactBudget,
    ) -> None:
        by_key = {(profile.tool_name, profile.version): profile for profile in profiles}
        if len(by_key) != len(profiles):
            raise ValueError("Shell profile tool identities must be unique")
        self._profiles = by_key
        self._processes = processes
        self._clock = clock
        self._artifact_budget = artifact_budget
        self._definitions = tuple(
            sorted((profile.definition for profile in profiles), key=lambda item: (item.name, item.version))
        )

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        try:
            profile = self._profiles[(call.name, call.version)]
        except KeyError as error:
            raise ValueError("Shell tool call does not match an installed profile") from error
        arguments = thaw_json(call.arguments)
        errors = tuple(Draft202012Validator(profile.input_schema).iter_errors(arguments))
        if errors:
            raise ValueError("Shell tool arguments failed the captured profile schema")
        argv = _string_tuple(arguments["argv"])
        cwd = _object(arguments["cwd"])
        environment = _object(arguments["environment"])
        timeout_ms = arguments["timeoutMs"]
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            raise TypeError("Shell timeout must be an integer")
        requested_deadline = self._clock.utcnow() + timedelta(milliseconds=timeout_ms)
        deadline = min(requested_deadline, call.deadline) if call.deadline is not None else requested_deadline
        process_id = (
            "shell-"
            + hashlib.sha256(
                f"{call.workspace_id}\0{call.run_id}\0{call.tool_call_id}\0{call.args_hash}".encode()
            ).hexdigest()[:32]
        )
        result = await self._processes.execute(
            SupervisedProcessRequest(
                process_id=process_id,
                executable_id=profile.executable_id,
                arguments=argv,
                stdin=b"",
                environment=profile.environment,
                deadline=deadline,
                stdout_limit_bytes=profile.inline_output_limit_bytes,
                stderr_limit_bytes=profile.inline_output_limit_bytes,
                allow_network=profile.allow_network,
                owner_kind=ProcessOwnerKind.SHELL,
                owner_run_id=call.run_id,
                workspace_id=call.workspace_id,
                cwd_root_id=_string(cwd["rootId"]),
                cwd=_string(cwd["path"]),
                environment_profile_id=_string(environment["profileId"]),
                stdin_mode=ProcessStdinMode.CLOSED,
                artifact_limit_bytes=profile.artifact_output_limit_bytes,
                allow_artifact_spill=True,
                executable_profile_fingerprint=profile.executable_profile_fingerprint,
                artifact_budget=self._artifact_budget,
            ),
            cancellation,
        )
        artifact_ids = tuple(
            artifact_id
            for artifact_id in (result.stdout_artifact_id, result.stderr_artifact_id)
            if artifact_id is not None
        )
        data = {
            "exitCode": result.exit_code,
            "lifecycleState": result.lifecycle_state.value,
            "stdout": _render_output(result.stdout, result.stdout_encoding, result.stdout_artifact_id),
            "stderr": _render_output(result.stderr, result.stderr_encoding, result.stderr_artifact_id),
            "stdoutBytes": result.stdout_total_bytes,
            "stderrBytes": result.stderr_total_bytes,
            "outputTruncated": result.output_truncated,
        }
        effect = SideEffect(
            kind=SideEffectKind.PROCESS,
            state=(
                SideEffectState.COMMITTED
                if result.exit_code == 0 and not result.timed_out and not result.output_truncated
                else SideEffectState.PARTIAL
            ),
            resource_id=f"process:{process_id}",
            before_state=None,
            after_state={
                "exitCode": result.exit_code,
                "lifecycleState": result.lifecycle_state.value,
                "argsHash": call.args_hash,
            },
            metadata={
                "profileId": profile.profile_id,
                "cwdRootId": profile.cwd_root_id,
                "environmentProfileId": profile.environment_profile_id,
            },
        )
        if result.timed_out:
            return _failed(
                call,
                ToolResultStatus.TIMED_OUT,
                "shell_timeout",
                "受控命令已超时并终止完整进程树。",
                data,
                artifact_ids,
                effect,
                retryable=False,
            )
        if result.output_truncated:
            return _failed(
                call,
                ToolResultStatus.PARTIAL,
                "shell_output_limit_exceeded",
                "受控命令输出超过硬限制, 已终止完整进程树。",
                data,
                artifact_ids,
                effect,
                retryable=False,
            )
        if result.exit_code != 0:
            return _failed(
                call,
                ToolResultStatus.FAILED,
                "shell_nonzero_exit",
                f"受控命令退出码为 {result.exit_code}。",
                data,
                artifact_ids,
                effect,
                retryable=profile.retryable,
            )
        return ToolResult(
            tool_call_id=call.tool_call_id,
            status=ToolResultStatus.SUCCEEDED,
            data=data,
            user_visible_summary="受控命令已完成。",
            artifact_ids=artifact_ids,
            source_refs=(),
            side_effects=(effect,),
            retryable=False,
            before_state=None,
            after_state={"exitCode": 0, "argsHash": call.args_hash},
            error=None,
        )


def _failed(
    call: ToolCall,
    status: ToolResultStatus,
    code: str,
    message: str,
    data: Any,
    artifact_ids: tuple[str, ...],
    effect: SideEffect,
    *,
    retryable: bool,
) -> ToolResult:
    return ToolResult(
        tool_call_id=call.tool_call_id,
        status=status,
        data=data,
        user_visible_summary=message,
        artifact_ids=artifact_ids,
        source_refs=(),
        side_effects=(effect,),
        retryable=retryable,
        before_state=None,
        after_state={"exitCode": data["exitCode"], "argsHash": call.args_hash},
        error=ToolError(code=code, message=message, retryable=retryable, cancelled=False),
    )


def _render_output(content: bytes, encoding: ProcessOutputEncoding, artifact_id: str | None) -> dict[str, Any]:
    if encoding is ProcessOutputEncoding.BINARY:
        data: dict[str, Any] = {
            "encoding": "base64",
            "preview": base64.b64encode(content).decode("ascii"),
        }
    else:
        text = content.decode("utf-8", errors="strict")
        data = {"encoding": "utf-8", "preview": _sanitize_terminal_text(text)}
    if artifact_id is not None:
        data["artifactId"] = artifact_id
    return data


def _sanitize_terminal_text(value: str) -> str:
    return _CONTROL.sub("�", _ANSI_ESCAPE.sub("", value))


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("Shell structured capability must be an object")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("Shell capability value must be text")
    return value


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError("Shell argv must be a string array")
    return tuple(value)


__all__ = ["ShellToolExecutor"]
