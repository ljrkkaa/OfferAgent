"""Single supervised-process boundary shared by Hooks, Shell, and parsers.

The port intentionally describes argv, cwd, environment and stdin as separate
capabilities.  It never accepts a command line string: Windows quoting belongs
to the production ``CreateProcessW`` adapter and is not a shell parser.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from .cancellation import CancellationToken

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


class ProcessOwnerKind(str, Enum):
    HOOK = "hook"
    SHELL = "shell"
    PARSER = "parser"


class ProcessStdinMode(str, Enum):
    CLOSED = "closed"
    FIXED_PAYLOAD = "fixed_payload"
    DUPLEX = "duplex"


class ProcessLifecycleState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    TERMINATING = "terminating"
    EXITED = "exited"
    KILLED = "killed"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


class ProcessOutputEncoding(str, Enum):
    UTF8 = "utf-8"
    BINARY = "binary"


class ProcessArtifactReservation(Protocol):
    async def commit(self) -> None: ...

    async def release(self) -> None: ...


class ProcessArtifactBudget(Protocol):
    async def reserve_artifact_bytes(self, byte_length: int) -> ProcessArtifactReservation: ...


@dataclass(frozen=True, slots=True)
class SupervisedProcessRequest:
    process_id: str
    executable_id: str
    arguments: tuple[str, ...]
    stdin: bytes
    environment: Mapping[str, str]
    deadline: datetime
    stdout_limit_bytes: int
    stderr_limit_bytes: int
    allow_network: bool = False
    owner_kind: ProcessOwnerKind = ProcessOwnerKind.HOOK
    owner_run_id: str = "runtime"
    workspace_id: str = "runtime"
    cwd_root_id: str = "vault"
    cwd: str = ""
    environment_profile_id: str = "minimal"
    stdin_mode: ProcessStdinMode = ProcessStdinMode.FIXED_PAYLOAD
    artifact_limit_bytes: int = 16 * 1024 * 1024
    allow_artifact_spill: bool = False
    executable_profile_fingerprint: str | None = None
    artifact_budget: ProcessArtifactBudget | None = None

    def __post_init__(self) -> None:
        profile_identities = (self.process_id, self.executable_id, self.cwd_root_id, self.environment_profile_id)
        if any(not _IDENTIFIER.fullmatch(value) for value in profile_identities):
            raise ValueError("supervised process/profile identities must be canonical non-empty identifiers")
        if any(not value or "\x00" in value or len(value) > 1024 for value in (self.owner_run_id, self.workspace_id)):
            raise ValueError("supervised process owner identities must be bounded NUL-free strings")
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise ValueError("supervised process deadline must be timezone-aware")
        if self.stdout_limit_bytes < 1 or self.stderr_limit_bytes < 1 or self.artifact_limit_bytes < 1:
            raise ValueError("supervised process output limits must be positive")
        if self.artifact_limit_bytes < max(self.stdout_limit_bytes, self.stderr_limit_bytes):
            raise ValueError("artifact output limit must cover each inline output limit")
        if (
            self.executable_profile_fingerprint is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", self.executable_profile_fingerprint) is None
        ):
            raise ValueError("expected executable profile fingerprint must be canonical SHA-256")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in self.environment.items()):
            raise TypeError("supervised process environment must be text")
        if any("\x00" in value for value in (*self.arguments, *self.environment, *self.environment.values())):
            raise ValueError("argv and environment must be NUL-free")
        if self.stdin_mode is ProcessStdinMode.CLOSED and self.stdin:
            raise ValueError("closed stdin cannot carry a payload")
        object.__setattr__(self, "arguments", tuple(self.arguments))
        object.__setattr__(self, "stdin", bytes(self.stdin))
        object.__setattr__(self, "environment", dict(self.environment))


@dataclass(frozen=True, slots=True)
class SupervisedProcessResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_truncated: bool = False
    lifecycle_state: ProcessLifecycleState = ProcessLifecycleState.EXITED
    stdout_encoding: ProcessOutputEncoding = ProcessOutputEncoding.UTF8
    stderr_encoding: ProcessOutputEncoding = ProcessOutputEncoding.UTF8
    stdout_artifact_id: str | None = None
    stderr_artifact_id: str | None = None
    stdout_total_bytes: int | None = None
    stderr_total_bytes: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "stdout", bytes(self.stdout))
        object.__setattr__(self, "stderr", bytes(self.stderr))
        if self.stdout_total_bytes is None:
            object.__setattr__(self, "stdout_total_bytes", len(self.stdout))
        if self.stderr_total_bytes is None:
            object.__setattr__(self, "stderr_total_bytes", len(self.stderr))
        assert self.stdout_total_bytes is not None
        assert self.stderr_total_bytes is not None
        if self.stdout_total_bytes < len(self.stdout) or self.stderr_total_bytes < len(self.stderr):
            raise ValueError("process output totals cannot be smaller than inline output")
        if self.timed_out and self.lifecycle_state is not ProcessLifecycleState.TIMED_OUT:
            object.__setattr__(self, "lifecycle_state", ProcessLifecycleState.TIMED_OUT)


@runtime_checkable
class ProcessSupervisor(Protocol):
    """Execute a registered executable and own its full Windows process tree."""

    async def execute(
        self,
        request: SupervisedProcessRequest,
        cancellation: CancellationToken,
    ) -> SupervisedProcessResult: ...


__all__ = [
    "ProcessArtifactBudget",
    "ProcessArtifactReservation",
    "ProcessLifecycleState",
    "ProcessOutputEncoding",
    "ProcessOwnerKind",
    "ProcessStdinMode",
    "ProcessSupervisor",
    "SupervisedProcessRequest",
    "SupervisedProcessResult",
]
