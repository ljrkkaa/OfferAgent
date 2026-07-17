from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import sys
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.config import HarnessConfig
from offeragent_harness.hooks import (
    HookCommandSpec,
    HookDefinition,
    HookEvent,
    HookImplementation,
    HookInvocation,
    HookLayer,
    HookScope,
)
from offeragent_harness.ports import ProcessStdinMode
from offeragent_harness.runtime.cancellation import (
    CancellationCode,
    CancellationReason,
    CancellationScope,
    RunCancelled,
)
from offeragent_harness.runtime.process_identity import SupervisedWorkspaceIdentity
from offeragent_harness.runtime.process_supervisor import (
    ExecutableTrust,
    ProcessEnvironmentProfile,
    ProcessExecutableProfile,
    ProcessFilesystemAccess,
    ProcessFilesystemCapability,
    ProcessSupervisorService,
)
from offeragent_harness.runtime.production_hooks import ProductionHookBundleFactory
from offeragent_harness.runtime.windows_process_supervisor import (
    PinnedProcessExecutableVerifier,
    WindowsSupervisedProcessBackend,
)
from offeragent_harness.testing import DeterministicIdGenerator, ManualCancellationToken, RecordingEventSink
from offeragent_harness.workspace import WorkspacePathPolicy

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects and CreateProcessW")

WORKSPACE_ID = "workspace-hook-windows-tree"
_SYNCHRONIZE = 0x00100000
_PROCESS_TERMINATE = 0x0001
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102


class WallClock:
    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep_until(self, deadline: datetime) -> None:
        await asyncio.sleep(max(0.0, (deadline - self.utcnow()).total_seconds()))


def _config() -> HarnessConfig:
    return HarnessConfig.model_validate(
        {
            "extensibility": {"hooks_enabled": True},
            "policy": {"workspace_trusted": True},
        }
    )


def _workspace() -> SupervisedWorkspaceIdentity:
    return SupervisedWorkspaceIdentity(
        "wsi_12345678-1234-4234-8234-123456789abc",
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    )


class _JobTreeOnlyBackend(WindowsSupervisedProcessBackend):
    """Isolate Job cancellation; AppContainer has dedicated tests."""

    async def spawn(self, profile: ProcessExecutableProfile, **kwargs: object):  # type: ignore[no-untyped-def]
        kwargs["allow_network"] = True
        kwargs["filesystem_grants"] = ()
        return await super().spawn(profile, **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cancelled_command_hook_kills_real_child_and_grandchild_job_tree(tmp_path: Path) -> None:
    helper = Path(__file__).parent / "helpers" / "job_tree_child.py"
    work = tmp_path / "hook-work"
    work.mkdir()
    trigger = work / "trigger"
    child_pid_path = work / "child.pid"
    grandchild_pid_path = work / "grandchild.pid"
    arguments = (
        str(helper.resolve(strict=True)),
        str(trigger),
        str(child_pid_path),
        str(grandchild_pid_path),
    )
    executable = await asyncio.to_thread(Path(sys.executable).resolve, strict=True)
    executable_profile = ProcessExecutableProfile(
        executable_id="python-hook-helper",
        executable=executable,
        fixed_root=executable.parent,
        trust=ExecutableTrust.FIXED_HASH,
        file_sha256="sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest(),
        fixed_arguments=arguments,
        maximum_variable_arguments=0,
        environment_profiles=frozenset({"minimal"}),
        allowed_stdin_modes=frozenset({ProcessStdinMode.FIXED_PAYLOAD}),
        allowed_cwd_roots=frozenset({"vault"}),
        appcontainer_filesystem=(
            ProcessFilesystemCapability("vault", "hook-work", ProcessFilesystemAccess.READ_WRITE),
        ),
    )
    command = HookCommandSpec(
        executable_profile.executable_id,
        arguments,
        frozenset(),
        executable_profile.fingerprint,
        "vault",
        "hook-work",
        "minimal",
        1024 * 1024,
    )
    managed = HookLayer(
        HookScope.MANAGED,
        "system",
        1,
        (
            HookDefinition(
                "managed-command-tree",
                HookScope.MANAGED,
                "system",
                HookEvent.TURN_START,
                HookImplementation.COMMAND,
                timeout_ms=120_000,
                command=command,
            ),
        ),
    )
    clock = WallClock()
    supervisor = ProcessSupervisorService(
        workspace_paths=WorkspacePathPolicy(tmp_path),
        workspace_id=WORKSPACE_ID,
        executable_profiles=(executable_profile,),
        environment_profiles=(ProcessEnvironmentProfile("minimal", frozenset()),),
        backend=_JobTreeOnlyBackend(
            workspace=_workspace(),
            verifier=PinnedProcessExecutableVerifier(),
            sandbox_state_directory=tmp_path / "process-sandbox",
        ),
        artifacts=None,
        clock=clock,
        graceful_termination_seconds=0,
    )
    factory = ProductionHookBundleFactory(
        workspace_id=WORKSPACE_ID,
        managed_layer=managed,
        builtin_handlers={},
        unit_of_work=SqliteUnitOfWorkFactory(tmp_path / "state.sqlite"),
        event_sink=RecordingEventSink(),
        processes=supervisor,
        clock=clock,
        ids=DeterministicIdGenerator(),
        environment={},
    )
    prepared = await factory.prepare(
        run_id="run-hook-tree",
        principal_id="principal-local",
        session_id="session-hook-tree",
        effective_config=_config(),
        cancellation=ManualCancellationToken(),
    )
    bundle = factory.build_prepared(prepared)
    assert bundle.hooks is not None and bundle.context is not None
    cancellation = CancellationScope(name="windows-hook-tree")
    running = asyncio.create_task(
        bundle.hooks.invoke(
            HookInvocation(
                "turn-start:run-hook-tree",
                "agent:run-hook-tree",
                HookEvent.TURN_START,
                bundle.context,
                "run-hook-tree",
                {"purpose": "process-tree-cancellation"},
            ),
            cancellation,
        )
    )
    child_pid: int | None = None
    grandchild_pid: int | None = None
    try:
        await _wait_for_file(child_pid_path)
        child_pid = int(await asyncio.to_thread(child_pid_path.read_text, encoding="ascii"))
        await asyncio.to_thread(trigger.write_text, "go", encoding="ascii")
        await _wait_for_file(grandchild_pid_path)
        grandchild_pid = int(await asyncio.to_thread(grandchild_pid_path.read_text, encoding="ascii"))
        assert _pid_is_alive(child_pid) and _pid_is_alive(grandchild_pid)

        await cancellation.cancel(CancellationReason.now(CancellationCode.USER, "cancel command Hook tree"))
        with pytest.raises(RunCancelled):
            await running
        await _wait_for_pid_exit(child_pid)
        await _wait_for_pid_exit(grandchild_pid)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await supervisor.shutdown()
        await cancellation.close()
        for pid in (child_pid, grandchild_pid):
            if pid is not None and _pid_is_alive(pid):
                _terminate_pid(pid)


async def _wait_for_file(path: Path, timeout_seconds: float = 10) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not await asyncio.to_thread(_file_is_ready, path):
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"helper did not create {path.name}")
        await asyncio.sleep(0.01)


def _file_is_ready(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


async def _wait_for_pid_exit(pid: int, timeout_seconds: float = 10) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while _pid_is_alive(pid):
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"PID {pid} remained alive")
        await asyncio.sleep(0.02)


def _pid_is_alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        wait = int(kernel32.WaitForSingleObject(handle, 0))
        if wait == _WAIT_OBJECT_0:
            return False
        if wait == _WAIT_TIMEOUT:
            return True
        raise OSError(f"unexpected process wait status 0x{wait:08x}")
    finally:
        kernel32.CloseHandle(handle)


def _terminate_pid(pid: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if handle:
        try:
            kernel32.TerminateProcess(handle, 0xEF)
        finally:
            kernel32.CloseHandle(handle)
