"""Worker-owned, profile-gated process supervision.

This module owns orchestration and bounded output capture.  The Windows adapter
owns ``CreateProcessW`` and Job Object details; neither layer accepts a raw
command line or invokes a command interpreter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from offeragent_harness.ports import (
    ArtifactMetadata,
    ArtifactState,
    ArtifactStore,
    CancellationToken,
    Clock,
    ProcessLifecycleState,
    ProcessOutputEncoding,
    ProcessOwnerKind,
    ProcessStdinMode,
    Sensitivity,
    SupervisedProcessRequest,
    SupervisedProcessResult,
)
from offeragent_harness.workspace.path_policy import WorkspacePathPolicy

_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEFAULT_ARGUMENT = re.compile(r"^[^\x00-\x1f\x7f]{0,4096}$")
_SHELL_METACHARACTERS = frozenset("&|<>^;`\r\n")
_COMMAND_INTERPRETERS = frozenset(
    {
        "bash.exe",
        "cmd.exe",
        "command.com",
        "cscript.exe",
        "mshta.exe",
        "powershell.exe",
        "pwsh.exe",
        "sh.exe",
        "wscript.exe",
    }
)
_SENSITIVE_ENVIRONMENT_FRAGMENTS = (
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "KEY",
    "PASSWORD",
    "PROXY",
    "SECRET",
    "TOKEN",
)
_DANGEROUS_ENVIRONMENT_NAMES = frozenset(
    {
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "PROMPT",
        "PSMODULEPATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "NODE_OPTIONS",
    }
)


class ProcessProfileError(ValueError):
    pass


class ProcessExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _ProcessArtifactBudgetExhausted(RuntimeError):
    """Internal signal: output remains bounded and no unreserved bytes are written."""


class ExecutableTrust(str, Enum):
    FIXED_HASH = "fixed_hash"
    OS_AUTHENTICODE = "os_authenticode"


class ProcessFilesystemAccess(str, Enum):
    READ = "read"
    READ_WRITE = "read_write"
    READ_EXECUTE = "read_execute"


@dataclass(frozen=True, slots=True)
class ProcessFilesystemCapability:
    """One hash-pinned, root-relative AppContainer filesystem grant.

    Empty paths are intentionally forbidden: a no-network process may receive
    a narrow subtree, but it cannot silently turn the whole Vault into an
    AppContainer resource.
    """

    root_id: str
    relative_path: str
    access: ProcessFilesystemAccess

    def __post_init__(self) -> None:
        if not _PROFILE_ID.fullmatch(self.root_id):
            raise ProcessProfileError("invalid AppContainer filesystem root capability")
        if (
            not self.relative_path
            or len(self.relative_path) > 1024
            or "\\" in self.relative_path
            or self.relative_path.startswith("/")
            or "\x00" in self.relative_path
            or any(segment in {"", ".", ".."} for segment in self.relative_path.split("/"))
        ):
            raise ProcessProfileError("AppContainer filesystem capability requires a narrow relative path")
        if self.access is ProcessFilesystemAccess.READ_EXECUTE:
            raise ProcessProfileError("workspace AppContainer capabilities cannot grant execute access")


@dataclass(frozen=True, slots=True)
class ResolvedProcessFilesystemGrant:
    path: Path
    access: ProcessFilesystemAccess


@dataclass(frozen=True, slots=True)
class ProcessEnvironmentProfile:
    profile_id: str
    allowed_names: frozenset[str]
    allowed_secret_names: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not _PROFILE_ID.fullmatch(self.profile_id):
            raise ProcessProfileError("invalid environment profile ID")
        folded = frozenset(name.upper() for name in self.allowed_names)
        if any(not name or "=" in name or "\x00" in name for name in folded):
            raise ProcessProfileError("environment names must be canonical")
        if any(_environment_name_is_sensitive(name) for name in folded):
            raise ProcessProfileError("environment profile contains a secret or dangerous variable")
        secret_folded = frozenset(name.upper() for name in self.allowed_secret_names)
        if len(secret_folded) != len(self.allowed_secret_names) or any(
            _ENVIRONMENT_NAME.fullmatch(name) is None or name in _DANGEROUS_ENVIRONMENT_NAMES or "PROXY" in name
            for name in secret_folded
        ):
            raise ProcessProfileError("secret environment profile contains an invalid or dangerous variable")
        if folded.intersection(secret_folded):
            raise ProcessProfileError("plain and secret environment profile names must be disjoint")
        object.__setattr__(self, "allowed_names", folded)
        object.__setattr__(self, "allowed_secret_names", secret_folded)


@dataclass(frozen=True, slots=True)
class ProcessExecutableProfile:
    executable_id: str
    executable: Path
    fixed_root: Path
    trust: ExecutableTrust
    file_sha256: str | None
    fixed_arguments: tuple[str, ...] = ()
    minimum_variable_arguments: int = 0
    maximum_variable_arguments: int = 32
    variable_argument_pattern: str = _DEFAULT_ARGUMENT.pattern
    allow_shell_metacharacters: bool = False
    environment_profiles: frozenset[str] = frozenset({"minimal"})
    allowed_stdin_modes: frozenset[ProcessStdinMode] = frozenset({ProcessStdinMode.CLOSED})
    allowed_cwd_roots: frozenset[str] = frozenset({"vault"})
    allow_network: bool = False
    appcontainer_filesystem: tuple[ProcessFilesystemCapability, ...] = ()
    captured_file_device: int = field(init=False, repr=False)
    captured_file_index: int = field(init=False, repr=False)
    captured_file_size: int = field(init=False, repr=False)
    captured_content_sha256: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not _PROFILE_ID.fullmatch(self.executable_id):
            raise ProcessProfileError("invalid executable profile ID")
        executable = self.executable.expanduser()
        fixed_root = self.fixed_root.expanduser()
        if not executable.is_absolute() or not fixed_root.is_absolute():
            raise ProcessProfileError("executable profiles require absolute fixed paths")
        try:
            canonical_root = fixed_root.resolve(strict=True)
            canonical_executable = executable.resolve(strict=True)
            canonical_executable.relative_to(canonical_root)
        except (OSError, ValueError) as error:
            raise ProcessProfileError("executable must exist beneath its fixed installation root") from error
        if not canonical_executable.is_file():
            raise ProcessProfileError("executable profile path must be a file")
        try:
            with canonical_executable.open("rb") as executable_stream:
                before = os.fstat(executable_stream.fileno())
                digest = hashlib.sha256()
                while chunk := executable_stream.read(1024 * 1024):
                    digest.update(chunk)
                after = os.fstat(executable_stream.fileno())
        except OSError as error:
            raise ProcessProfileError("executable profile file identity cannot be captured") from error
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity != after_identity or before.st_ino <= 0:
            raise ProcessProfileError("executable changed while its file identity was captured")
        captured_content_sha256 = f"sha256:{digest.hexdigest()}"
        if canonical_executable.name.casefold() in _COMMAND_INTERPRETERS:
            raise ProcessProfileError("command interpreters require a separately implemented high-risk profile")
        if self.trust is ExecutableTrust.FIXED_HASH:
            if self.file_sha256 is None or not _SHA256.fullmatch(self.file_sha256):
                raise ProcessProfileError("fixed executables require a pinned SHA-256")
        elif self.file_sha256 is not None and not _SHA256.fullmatch(self.file_sha256):
            raise ProcessProfileError("invalid executable SHA-256")
        if not 0 <= self.minimum_variable_arguments <= self.maximum_variable_arguments <= 128:
            raise ProcessProfileError("invalid argument count limits")
        try:
            pattern = re.compile(self.variable_argument_pattern)
        except re.error as error:
            raise ProcessProfileError("invalid executable argument pattern") from error
        if pattern.fullmatch("") is None and self.minimum_variable_arguments == 0:
            # Zero arguments is still valid; this check only ensures callers do
            # not mistake a pattern for a whole-command expression.
            pass
        if not self.environment_profiles or any(not _PROFILE_ID.fullmatch(item) for item in self.environment_profiles):
            raise ProcessProfileError("executable requires valid environment profile IDs")
        if not self.allowed_stdin_modes or not self.allowed_cwd_roots:
            raise ProcessProfileError("executable profile capabilities cannot be empty")
        if any(not _PROFILE_ID.fullmatch(item) for item in self.allowed_cwd_roots):
            raise ProcessProfileError("invalid cwd root capability")
        if any(
            _DEFAULT_ARGUMENT.fullmatch(argument) is None
            or any(character in argument for character in _SHELL_METACHARACTERS)
            for argument in self.fixed_arguments
        ):
            raise ProcessProfileError("fixed executable argv contains parser metacharacters")
        object.__setattr__(self, "executable", canonical_executable)
        object.__setattr__(self, "fixed_root", canonical_root)
        object.__setattr__(self, "fixed_arguments", tuple(self.fixed_arguments))
        object.__setattr__(self, "environment_profiles", frozenset(self.environment_profiles))
        object.__setattr__(self, "allowed_stdin_modes", frozenset(self.allowed_stdin_modes))
        object.__setattr__(self, "allowed_cwd_roots", frozenset(self.allowed_cwd_roots))
        capabilities = tuple(self.appcontainer_filesystem)
        if len({(item.root_id, item.relative_path) for item in capabilities}) != len(capabilities):
            raise ProcessProfileError("AppContainer filesystem capabilities must be unique")
        object.__setattr__(self, "appcontainer_filesystem", capabilities)
        object.__setattr__(self, "captured_file_device", int(before.st_dev))
        object.__setattr__(self, "captured_file_index", int(before.st_ino))
        object.__setattr__(self, "captured_file_size", int(before.st_size))
        object.__setattr__(self, "captured_content_sha256", captured_content_sha256)

    def validate_request(self, request: SupervisedProcessRequest) -> None:
        if request.environment_profile_id not in self.environment_profiles:
            raise ProcessProfileError("environment profile is not authorized for executable")
        if request.stdin_mode not in self.allowed_stdin_modes:
            raise ProcessProfileError("stdin mode is not authorized for executable")
        if request.cwd_root_id not in self.allowed_cwd_roots:
            raise ProcessProfileError("cwd root is not authorized for executable")
        if request.allow_network and not self.allow_network:
            raise ProcessProfileError("network is not authorized for executable")
        prefix = self.fixed_arguments
        if request.arguments[: len(prefix)] != prefix:
            raise ProcessProfileError("argv does not match the executable profile prefix")
        variable = request.arguments[len(prefix) :]
        if not self.minimum_variable_arguments <= len(variable) <= self.maximum_variable_arguments:
            raise ProcessProfileError("argv count exceeds the executable profile")
        pattern = re.compile(self.variable_argument_pattern)
        for argument in variable:
            if pattern.fullmatch(argument) is None:
                raise ProcessProfileError("argv value does not match the executable profile")
            if not self.allow_shell_metacharacters and any(
                character in argument for character in _SHELL_METACHARACTERS
            ):
                raise ProcessProfileError("shell metacharacters are forbidden by the executable profile")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "executableId": self.executable_id,
                "executablePath": str(self.executable),
                "capturedFileDevice": self.captured_file_device,
                "capturedFileIndex": self.captured_file_index,
                "capturedFileSize": self.captured_file_size,
                "capturedContentSha256": self.captured_content_sha256,
                "fixedRoot": str(self.fixed_root),
                "trust": self.trust.value,
                "fileSha256": self.file_sha256,
                "fixedArguments": self.fixed_arguments,
                "minimumVariableArguments": self.minimum_variable_arguments,
                "maximumVariableArguments": self.maximum_variable_arguments,
                "variableArgumentPattern": self.variable_argument_pattern,
                "allowShellMetacharacters": self.allow_shell_metacharacters,
                "environmentProfiles": sorted(self.environment_profiles),
                "allowedStdinModes": sorted(item.value for item in self.allowed_stdin_modes),
                "allowedCwdRoots": sorted(self.allowed_cwd_roots),
                "allowNetwork": self.allow_network,
                "appContainerFilesystem": [
                    {
                        "rootId": item.root_id,
                        "relativePath": item.relative_path,
                        "access": item.access.value,
                    }
                    for item in self.appcontainer_filesystem
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class ManagedSupervisedProcess(Protocol):
    @property
    def pid(self) -> int: ...

    async def read_stdout(self, maximum_bytes: int) -> bytes: ...

    async def read_stderr(self, maximum_bytes: int) -> bytes: ...

    async def write_stdin(self, payload: bytes) -> None: ...

    async def wait(self) -> int: ...

    async def terminate_tree(self, *, grace_seconds: float) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ActiveProcessSnapshot:
    process_id: str
    owner_kind: ProcessOwnerKind
    pid: int | None
    state: ProcessLifecycleState


class SupervisedProcessBackend(Protocol):
    async def spawn(
        self,
        profile: ProcessExecutableProfile,
        *,
        arguments: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        stdin: bytes,
        interactive_stdin: bool,
        allow_network: bool,
        filesystem_grants: tuple[ResolvedProcessFilesystemGrant, ...],
    ) -> ManagedSupervisedProcess: ...

    async def shutdown(self) -> None: ...


class ProcessSupervisorService:
    """The one Worker-owned process registry and lifecycle owner."""

    def __init__(
        self,
        *,
        workspace_paths: WorkspacePathPolicy,
        executable_profiles: tuple[ProcessExecutableProfile, ...],
        environment_profiles: tuple[ProcessEnvironmentProfile, ...],
        backend: SupervisedProcessBackend,
        artifacts: ArtifactStore | None,
        clock: Clock,
        workspace_id: str,
        graceful_termination_seconds: float = 1.0,
        read_chunk_bytes: int = 64 * 1024,
    ) -> None:
        if graceful_termination_seconds < 0 or read_chunk_bytes < 1:
            raise ValueError("invalid process supervisor limits")
        if not workspace_id or "\x00" in workspace_id:
            raise ValueError("process supervisor requires a canonical Workspace identity")
        executables = {profile.executable_id: profile for profile in executable_profiles}
        environments = {profile.profile_id: profile for profile in environment_profiles}
        if len(executables) != len(executable_profiles) or len(environments) != len(environment_profiles):
            raise ValueError("process profile IDs must be unique")
        if any(
            environment_id not in environments
            for executable in executables.values()
            for environment_id in executable.environment_profiles
        ):
            raise ValueError("executable references an unknown environment profile")
        self._paths = workspace_paths
        self._workspace_id = workspace_id
        self._executables = executables
        self._environments = environments
        self._backend = backend
        self._artifacts = artifacts
        self._clock = clock
        self._graceful_termination_seconds = graceful_termination_seconds
        self._read_chunk_bytes = read_chunk_bytes
        self._active: dict[str, ManagedSupervisedProcess] = {}
        self._active_owners: dict[str, ProcessOwnerKind] = {}
        self._spawning: dict[str, asyncio.Event] = {}
        self._active_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._closing = False
        self._backend_stopped = False

    @property
    def active_process_count(self) -> int:
        """Diagnostic count; ownership and mutation remain inside the supervisor."""

        return len(self._active) + len(self._spawning)

    async def active_processes(self) -> tuple[ActiveProcessSnapshot, ...]:
        """Return a content-free point-in-time process ownership snapshot."""

        async with self._active_lock:
            values = tuple(self._active.items())
            owners = dict(self._active_owners)
        return tuple(
            ActiveProcessSnapshot(
                process_id=process_id,
                owner_kind=owners[process_id],
                pid=process.pid if process.pid > 0 else None,
                state=(
                    ProcessLifecycleState.STARTING
                    if isinstance(process, _SpawningProcess)
                    else ProcessLifecycleState.RUNNING
                ),
            )
            for process_id, process in sorted(values)
            if process_id in owners
        )

    async def execute(
        self,
        request: SupervisedProcessRequest,
        cancellation: CancellationToken,
    ) -> SupervisedProcessResult:
        cancellation.checkpoint()
        if request.workspace_id != self._workspace_id:
            raise ProcessExecutionError(
                "process_workspace_mismatch",
                "process request is not bound to this Workspace supervisor",
            )
        if self._closing:
            raise ProcessExecutionError("supervisor_stopping", "process supervisor is stopping")
        try:
            executable = self._executables[request.executable_id]
            environment_profile = self._environments[request.environment_profile_id]
        except KeyError as error:
            raise ProcessExecutionError(
                "process_profile_unavailable", "registered process profile is unavailable"
            ) from error
        if (
            request.executable_profile_fingerprint is not None
            and request.executable_profile_fingerprint != executable.fingerprint
        ):
            raise ProcessExecutionError(
                "executable_profile_drift",
                "captured executable profile no longer matches the Worker registry",
            )
        try:
            executable.validate_request(request)
            cwd = self._paths.resolve(
                request.cwd,
                root_id=request.cwd_root_id,
                must_exist=True,
                expect_directory=True,
                allow_root=request.cwd == "",
            ).path
            environment = _validated_environment(request.environment, environment_profile)
            filesystem_grants = self._filesystem_grants(executable, request.cwd_root_id, cwd, request.allow_network)
        except ValueError as error:
            raise ProcessExecutionError("process_capability_denied", str(error)) from error
        if request.allow_artifact_spill and (self._artifacts is None or request.artifact_budget is None):
            raise ProcessExecutionError(
                "artifact_store_unavailable",
                "process output spill requires an Artifact Store and active Run budget",
            )
        if self._clock.utcnow() >= request.deadline:
            return SupervisedProcessResult(
                -1,
                b"",
                b"",
                timed_out=True,
                lifecycle_state=ProcessLifecycleState.TIMED_OUT,
            )

        async with self._active_lock:
            if self._closing:
                raise ProcessExecutionError("supervisor_stopping", "process supervisor is stopping")
            if request.process_id in self._active:
                raise ProcessExecutionError("process_id_conflict", "process ID is already owned")
            # Reserve the ID across spawn without exposing a half-owned process.
            self._active[request.process_id] = _SpawningProcess()
            self._active_owners[request.process_id] = request.owner_kind
            spawn_completed = asyncio.Event()
            self._spawning[request.process_id] = spawn_completed
        process: ManagedSupervisedProcess | None = None
        try:
            process = await self._backend.spawn(
                executable,
                arguments=request.arguments,
                cwd=cwd,
                environment=environment,
                stdin=request.stdin,
                interactive_stdin=False,
                allow_network=request.allow_network,
                filesystem_grants=filesystem_grants,
            )
            async with self._active_lock:
                self._active[request.process_id] = process
                stopping = self._closing
                self._spawning.pop(request.process_id, None)
                spawn_completed.set()
            if stopping:
                await process.terminate_tree(grace_seconds=0.0)
                raise ProcessExecutionError("supervisor_stopping", "process supervisor stopped during spawn")
            return await self._run_owned(process, request, cancellation)
        finally:
            if process is not None:
                await process.close()
            async with self._active_lock:
                self._active.pop(request.process_id, None)
                self._active_owners.pop(request.process_id, None)
                pending_spawn = self._spawning.pop(request.process_id, None)
                if pending_spawn is not None:
                    pending_spawn.set()

    def _filesystem_grants(
        self,
        profile: ProcessExecutableProfile,
        cwd_root_id: str,
        cwd: Path,
        allow_network: bool,
    ) -> tuple[ResolvedProcessFilesystemGrant, ...]:
        if allow_network:
            return ()
        resolved: dict[Path, ProcessFilesystemAccess] = {
            profile.fixed_root: ProcessFilesystemAccess.READ_EXECUTE,
        }
        cwd_authorized = _is_within(profile.fixed_root, cwd)
        for capability in profile.appcontainer_filesystem:
            path = self._paths.resolve(
                capability.relative_path,
                root_id=capability.root_id,
                must_exist=True,
                expect_directory=True,
                allow_root=False,
                for_write=capability.access is ProcessFilesystemAccess.READ_WRITE,
            ).path
            existing = resolved.get(path)
            if existing is None or _filesystem_access_rank(capability.access) > _filesystem_access_rank(existing):
                resolved[path] = capability.access
            if capability.root_id == cwd_root_id and _is_within(path, cwd):
                cwd_authorized = True
        if not cwd_authorized:
            raise ProcessProfileError(
                "network-denied process cwd is outside its declared AppContainer filesystem capabilities"
            )
        return tuple(
            ResolvedProcessFilesystemGrant(path, access)
            for path, access in sorted(resolved.items(), key=lambda item: os.path.normcase(str(item[0])))
        )

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            async with self._active_lock:
                if self._backend_stopped:
                    return
                self._closing = True
                active = tuple(self._active.values())
                spawning = tuple(self._spawning.values())
            await asyncio.gather(
                *(process.terminate_tree(grace_seconds=0.0) for process in active),
                return_exceptions=True,
            )
            if spawning:
                await asyncio.gather(*(event.wait() for event in spawning))
                async with self._active_lock:
                    spawned_after_stop = tuple(self._active.values())
                await asyncio.gather(
                    *(process.terminate_tree(grace_seconds=0.0) for process in spawned_after_stop),
                    return_exceptions=True,
                )
            await self._backend.shutdown()
            async with self._active_lock:
                self._backend_stopped = True

    async def _run_owned(
        self,
        process: ManagedSupervisedProcess,
        request: SupervisedProcessRequest,
        cancellation: CancellationToken,
    ) -> SupervisedProcessResult:
        hard_stdout = request.artifact_limit_bytes if request.allow_artifact_spill else request.stdout_limit_bytes
        hard_stderr = request.artifact_limit_bytes if request.allow_artifact_spill else request.stderr_limit_bytes
        overflow = asyncio.Event()
        stdout = _BoundedCapture(request.stdout_limit_bytes, hard_stdout, overflow)
        stderr = _BoundedCapture(request.stderr_limit_bytes, hard_stderr, overflow)
        stdout_task = asyncio.create_task(self._drain(process.read_stdout, stdout))
        stderr_task = asyncio.create_task(self._drain(process.read_stderr, stderr))
        wait_task = asyncio.create_task(process.wait())
        cancel_task = asyncio.create_task(cancellation.wait())
        deadline_task = asyncio.create_task(self._clock.sleep_until(request.deadline))
        overflow_task = asyncio.create_task(overflow.wait())
        timed_out = False
        cancelled = False
        lifecycle = ProcessLifecycleState.RUNNING
        exit_code = -1
        try:
            done, _ = await asyncio.wait(
                (wait_task, cancel_task, deadline_task, overflow_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            cancelled = cancel_task in done
            timed_out = deadline_task in done
            if cancelled or timed_out or (overflow_task in done and wait_task not in done):
                lifecycle = ProcessLifecycleState.TERMINATING
                await process.terminate_tree(grace_seconds=self._graceful_termination_seconds)
                exit_code = await wait_task
                lifecycle = ProcessLifecycleState.TIMED_OUT if timed_out else ProcessLifecycleState.KILLED
            else:
                exit_code = await wait_task
                lifecycle = ProcessLifecycleState.EXITED
        except asyncio.CancelledError:
            # ToolScheduler task cancellation is an independent cancellation
            # path from the domain token.  It must still terminate the Job
            # before pipe drains/handle closure can complete.
            await asyncio.shield(process.terminate_tree(grace_seconds=0.0))
            raise
        finally:
            for task in (cancel_task, deadline_task, overflow_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(cancel_task, deadline_task, overflow_task, return_exceptions=True)
            await asyncio.gather(stdout_task, stderr_task)

        state = (
            ArtifactState.CANCELLED
            if cancelled
            else ArtifactState.PARTIAL
            if timed_out or stdout.truncated or stderr.truncated
            else ArtifactState.COMPLETE
        )
        artifact_budget_exhausted = False
        try:
            stdout_artifact = await self._store_output(request, "stdout", stdout, state)
            stderr_artifact = await self._store_output(request, "stderr", stderr, state)
        except _ProcessArtifactBudgetExhausted:
            # The process is already stopped (normally or through the hard
            # capture limit).  Fail closed with bounded previews and never
            # write bytes for which the active Run could not reserve budget.
            stdout_artifact = None
            stderr_artifact = None
            artifact_budget_exhausted = True
        result = SupervisedProcessResult(
            exit_code,
            stdout.preview,
            stderr.preview,
            timed_out=timed_out,
            output_truncated=stdout.truncated or stderr.truncated or artifact_budget_exhausted,
            lifecycle_state=lifecycle,
            stdout_encoding=stdout.encoding,
            stderr_encoding=stderr.encoding,
            stdout_artifact_id=stdout_artifact,
            stderr_artifact_id=stderr_artifact,
            stdout_total_bytes=stdout.total_bytes,
            stderr_total_bytes=stderr.total_bytes,
        )
        if cancelled:
            cancellation.checkpoint()
        return result

    async def _drain(
        self,
        read: Callable[[int], Awaitable[bytes]],
        capture: _BoundedCapture,
    ) -> None:
        while True:
            chunk = await read(self._read_chunk_bytes)
            if not chunk:
                return
            capture.feed(chunk)

    async def _store_output(
        self,
        request: SupervisedProcessRequest,
        stream_name: str,
        capture: _BoundedCapture,
        state: ArtifactState,
    ) -> str | None:
        if not request.allow_artifact_spill or len(capture.content) <= capture.inline_limit:
            return None
        assert self._artifacts is not None
        assert request.artifact_budget is not None
        content = capture.content
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        identity = hashlib.sha256(
            f"{request.workspace_id}\0{request.process_id}\0{stream_name}\0{digest}".encode()
        ).hexdigest()[:32]
        artifact_id = f"process-output-{identity}"
        metadata = ArtifactMetadata(
            artifact_id=artifact_id,
            workspace_id=request.workspace_id,
            owner_run_id=request.owner_run_id,
            mime_type="text/plain; charset=utf-8"
            if capture.encoding is ProcessOutputEncoding.UTF8
            else "application/octet-stream",
            byte_length=len(content),
            sha256=digest,
            sensitivity=Sensitivity.PRIVATE,
            state=state,
            created_at=self._clock.utcnow(),
            attributes={
                "processIdHash": f"sha256:{hashlib.sha256(request.process_id.encode()).hexdigest()}",
                "ownerKind": request.owner_kind.value,
                "stream": stream_name,
                "truncated": capture.truncated,
            },
        )
        try:
            reservation = await request.artifact_budget.reserve_artifact_bytes(len(content))
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            raise _ProcessArtifactBudgetExhausted from error
        try:
            stored = await self._artifacts.put(
                metadata,
                content,
                idempotency_key=f"process-output:{identity}:{digest}",
            )
            if (
                stored.workspace_id != request.workspace_id
                or stored.owner_run_id != request.owner_run_id
                or stored.byte_length != len(content)
                or stored.sha256 != digest
            ):
                raise ProcessExecutionError(
                    "artifact_store_mismatch",
                    "process output Artifact metadata differs from the reserved content",
                )
            await reservation.commit()
            return stored.artifact_id
        except BaseException:
            await reservation.release()
            raise


class _BoundedCapture:
    def __init__(self, inline_limit: int, hard_limit: int, overflow: asyncio.Event) -> None:
        self.inline_limit = inline_limit
        self._hard_limit = hard_limit
        self._overflow = overflow
        self._content = bytearray()
        self.total_bytes = 0
        self.truncated = False

    @property
    def content(self) -> bytes:
        return bytes(self._content)

    @property
    def preview(self) -> bytes:
        return bytes(self._content[: self.inline_limit])

    @property
    def encoding(self) -> ProcessOutputEncoding:
        try:
            self._content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return ProcessOutputEncoding.BINARY
        return ProcessOutputEncoding.UTF8

    def feed(self, chunk: bytes) -> None:
        payload = bytes(chunk)
        self.total_bytes += len(payload)
        remaining = self._hard_limit - len(self._content)
        if remaining > 0:
            self._content.extend(payload[:remaining])
        if len(payload) > remaining:
            self.truncated = True
            self._overflow.set()


class _SpawningProcess:
    @property
    def pid(self) -> int:
        return 0

    async def read_stdout(self, maximum_bytes: int) -> bytes:
        del maximum_bytes
        return b""

    async def read_stderr(self, maximum_bytes: int) -> bytes:
        del maximum_bytes
        return b""

    async def write_stdin(self, payload: bytes) -> None:
        del payload

    async def wait(self) -> int:
        return -1

    async def terminate_tree(self, *, grace_seconds: float) -> None:
        del grace_seconds

    async def close(self) -> None:
        return None


def _validated_environment(
    requested: Mapping[str, str],
    profile: ProcessEnvironmentProfile,
) -> Mapping[str, str]:
    result: dict[str, str] = {}
    for name, value in requested.items():
        folded = name.upper()
        if folded not in profile.allowed_names or _environment_name_is_sensitive(folded):
            raise ProcessProfileError("environment variable is not authorized")
        if not value or "\x00" in value or len(value) > 32 * 1024:
            raise ProcessProfileError("environment value is invalid")
        existing = next((key for key in result if key.upper() == folded), None)
        if existing is not None:
            raise ProcessProfileError("environment contains case-insensitive duplicates")
        result[name] = value
    return result


def _environment_name_is_sensitive(name: str) -> bool:
    folded = name.upper()
    return folded in _DANGEROUS_ENVIRONMENT_NAMES or any(
        fragment in folded for fragment in _SENSITIVE_ENVIRONMENT_FRAGMENTS
    )


def _filesystem_access_rank(access: ProcessFilesystemAccess) -> int:
    return {
        ProcessFilesystemAccess.READ: 1,
        ProcessFilesystemAccess.READ_EXECUTE: 2,
        ProcessFilesystemAccess.READ_WRITE: 3,
    }[access]


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(root), os.path.normcase(candidate))) == os.path.normcase(root)
    except ValueError:
        return False


__all__ = [
    "ActiveProcessSnapshot",
    "ExecutableTrust",
    "ManagedSupervisedProcess",
    "ProcessEnvironmentProfile",
    "ProcessExecutableProfile",
    "ProcessExecutionError",
    "ProcessFilesystemAccess",
    "ProcessFilesystemCapability",
    "ProcessProfileError",
    "ProcessSupervisorService",
    "ResolvedProcessFilesystemGrant",
    "SupervisedProcessBackend",
]
