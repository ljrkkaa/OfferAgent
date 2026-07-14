from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

from offeragent_harness.runtime.host_supervisor import SupervisedWorkspaceIdentity
from offeragent_harness.runtime.windows_process import WindowsWorkerJob

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")

_SYNCHRONIZE = 0x00100000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_CREATE_NO_WINDOW = 0x08000000


def workspace() -> SupervisedWorkspaceIdentity:
    return SupervisedWorkspaceIdentity(
        "wsi_12345678-1234-4234-8234-123456789abc",
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    )


async def wait_for_file(path: Path, *, timeout_seconds: float = 10) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not await asyncio.to_thread(_file_is_ready, path):
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"helper did not create {path.name}")
        await asyncio.sleep(0.01)


def _file_is_ready(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


async def wait_for_pid_exit(pid: int, *, timeout_seconds: float = 10) -> None:
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


def _open_job_assignment_handle(pid: int) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(
        _PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not handle:
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code))
    return int(handle)


def _close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(wintypes.HANDLE(handle)):
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code))


@pytest.mark.asyncio
async def test_closing_host_job_kills_child_and_grandchild_without_leaks(tmp_path: Path) -> None:
    helper = Path(__file__).parent / "helpers" / "job_tree_child.py"
    trigger = tmp_path / "trigger"
    child_pid_path = tmp_path / "child.pid"
    grandchild_pid_path = tmp_path / "grandchild.pid"
    process = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, str(helper), str(trigger), str(child_pid_path), str(grandchild_pid_path)],
        close_fds=True,
        creationflags=_CREATE_NO_WINDOW,
    )
    job = WindowsWorkerJob(workspace())
    grandchild_pid: int | None = None
    try:
        process_handle = _open_job_assignment_handle(process.pid)
        try:
            job.assign_process_handle(process_handle)
            assert job.contains_process_handle(process_handle)
        finally:
            _close_handle(process_handle)
        await asyncio.to_thread(trigger.write_text, "go", encoding="ascii")
        await wait_for_file(child_pid_path)
        await wait_for_file(grandchild_pid_path)
        child_pid = int(await asyncio.to_thread(child_pid_path.read_text, encoding="ascii"))
        grandchild_pid = int(await asyncio.to_thread(grandchild_pid_path.read_text, encoding="ascii"))
        assert _pid_is_alive(child_pid)
        assert _pid_is_alive(grandchild_pid)

        # Simulate abrupt Host loss: no graceful terminate call, just close the
        # last Job handle. KILL_ON_JOB_CLOSE must collect the full descendant tree.
        job.close()
        await wait_for_pid_exit(child_pid)
        await wait_for_pid_exit(grandchild_pid)
        await asyncio.to_thread(process.wait, 5)
        assert process.poll() is not None
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, 5)
        if grandchild_pid is not None and _pid_is_alive(grandchild_pid):
            _terminate_pid(grandchild_pid)


@pytest.mark.asyncio
async def test_atomic_job_list_prevents_orphan_if_host_dies_after_create_returns(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "atomic-child.pid"
    script = r"""
import hashlib
import os
import sys
from pathlib import Path
from offeragent_harness.runtime.host_supervisor import (
    SupervisedWorkspaceIdentity, VerifiedWorkerExecutable, WorkerLaunchRequest,
)
from offeragent_harness.runtime.windows_process import (
    PinnedWorkerExecutableVerifier, WindowsWorkerJob, WindowsWorkerProcessBackend,
)
class Trust:
    def authorizes(self, expected):
        return True
class Signature:
    def verify(self, executable):
        return True
image = Path(sys.executable).resolve()
digest = hashlib.sha256(image.read_bytes()).hexdigest()
release = VerifiedWorkerExecutable(image, image.parent, "atomic-test", "sha256:" + digest)
workspace = SupervisedWorkspaceIdentity(
    "wsi_12345678-1234-4234-8234-123456789abc",
    "sha256:" + "a" * 64,
    "sha256:" + "b" * 64,
)
verifier = PinnedWorkerExecutableVerifier(manifest_trust=Trust(), authenticode=Signature())
backend = WindowsWorkerProcessBackend(verifier=verifier)
job = WindowsWorkerJob(workspace)
child = backend.spawn_suspended(WorkerLaunchRequest(workspace, release), job=job)
Path(sys.argv[1]).write_text(str(child.pid), encoding="ascii")
os._exit(55)
"""
    host = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-c", script, str(child_pid_path)],
        cwd=Path(__file__).parents[3],
        close_fds=True,
        creationflags=_CREATE_NO_WINDOW,
    )
    await wait_for_file(child_pid_path)
    child_pid = int(await asyncio.to_thread(child_pid_path.read_text, encoding="ascii"))
    return_code = await asyncio.to_thread(host.wait, 15)

    assert return_code == 55
    await wait_for_pid_exit(child_pid)
    assert not _pid_is_alive(child_pid)
