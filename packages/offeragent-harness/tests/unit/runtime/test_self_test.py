from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

import pytest

from offeragent_harness.runtime import development_self_test, self_test
from offeragent_harness.runtime.host_supervisor import SupervisedWorkspaceIdentity
from offeragent_harness.runtime.runtime_installer import RuntimeInstallError, SubprocessRuntimeSelfTestRunner
from offeragent_harness.runtime.self_test import RuntimeSelfTestError, SelfTestResult
from offeragent_harness.runtime.windows_fixed_fd import launch_windows_fixed_fd_process
from offeragent_harness.runtime.windows_process import WindowsWorkerJob
from offeragent_harness.workspace.identity import WorkspaceRegistry
from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config


def test_self_test_result_is_canonical_and_healthy_only_when_every_check_passes() -> None:
    result = SelfTestResult({"web": True, "job": True}, None)
    assert result.healthy is True
    assert result.canonical_bytes() == (b'{"checks":{"job":true,"web":true},"diagnosticCode":null,"healthy":true}\n')

    failed = SelfTestResult({"web": True, "job": False}, "self_test_job")
    assert failed.healthy is False


class _FixedStopProcess:
    def __init__(self, payload: bytes, *, exit_code: int = 0) -> None:
        self.payload = payload
        self.exit_code = exit_code
        self.closed = False

    def read_output(self, fd: int, *, maximum_bytes: int, timeout_seconds: float) -> bytes:
        assert fd == 3
        assert maximum_bytes == 4 * 1024
        assert timeout_seconds == 65
        return self.payload

    def wait(self, *, timeout_seconds: float) -> int:
        assert timeout_seconds == 10
        return self.exit_code

    def poll(self) -> int:
        return self.exit_code

    def terminate(self) -> None:
        raise AssertionError("completed stop process must not be terminated")

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["stopped", "already_stopped"])
async def test_self_test_stop_accepts_both_exact_success_postconditions(
    status: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = (
        json.dumps(
            {"schemaVersion": 1, "status": status, "type": "result"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    process = _FixedStopProcess(payload)

    def launch(*args: object, **kwargs: object) -> _FixedStopProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(self_test, "launch_windows_fixed_fd_process", launch)
    await self_test._request_self_test_stop(tmp_path / "host.exe", ["stop"], {})
    assert process.closed is True


@pytest.mark.asyncio
async def test_self_test_stop_rejects_noncanonical_or_failed_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def launcher_for(candidate: _FixedStopProcess) -> Callable[..., _FixedStopProcess]:
        def launch(*args: object, **kwargs: object) -> _FixedStopProcess:
            del args, kwargs
            return candidate

        return launch

    for process in (
        _FixedStopProcess(b'{"schemaVersion":1,"status":"invalid","type":"result"}\n'),
        _FixedStopProcess(b'{"schemaVersion":1,"status":"stopped","type":"result"}\n', exit_code=2),
    ):
        monkeypatch.setattr(self_test, "launch_windows_fixed_fd_process", launcher_for(process))
        with pytest.raises(RuntimeSelfTestError, match="stop-all receipt was invalid"):
            await self_test._request_self_test_stop(tmp_path / "host.exe", ["stop"], {})
        assert process.closed is True


@pytest.mark.skipif(os.name != "nt", reason="requires Windows CRT descriptor inheritance")
def test_native_launcher_exercises_exact_fd3_fd4_contract() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"LOCALAPPDATA", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
    }
    process = launch_windows_fixed_fd_process(
        Path(sys.executable),
        ["-c", "import os; os.write(3, b'discovery:' + os.read(4, 32))"],
        environment=environment,
        child_inputs={4: b"vault-root"},
        child_outputs=frozenset({3}),
    )
    try:
        assert process.read_output(3, maximum_bytes=128, timeout_seconds=5) == b"discovery:vault-root"
        assert process.wait(timeout_seconds=5) == 0
    finally:
        if process.poll() is None:
            process.terminate()
        process.close()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows HANDLE_LIST")
def test_native_launcher_does_not_inherit_an_unlisted_handle() -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    unrelated = kernel32.CreateEventW(None, False, False, None)
    assert unrelated
    assert kernel32.SetHandleInformation(unrelated, 1, 1)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"LOCALAPPDATA", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
    }
    script = (
        "import ctypes,os,sys;from ctypes import wintypes;"
        "k=ctypes.WinDLL('kernel32',use_last_error=True);"
        "k.SetEvent.argtypes=[wintypes.HANDLE];k.SetEvent.restype=wintypes.BOOL;"
        "ok=k.SetEvent(wintypes.HANDLE(int(sys.argv[1])));"
        "os.write(3,b'inherited' if ok else b'closed')"
    )
    process = launch_windows_fixed_fd_process(
        Path(sys.executable),
        ["-c", script, str(int(unrelated))],
        environment=environment,
        child_inputs={},
        child_outputs=frozenset({3}),
    )
    try:
        assert process.read_output(3, maximum_bytes=32, timeout_seconds=5) == b"closed"
        assert process.wait(timeout_seconds=5) == 0
        assert kernel32.WaitForSingleObject(unrelated, 0) == 0x00000102
    finally:
        if process.poll() is None:
            process.terminate()
        process.close()
        kernel32.CloseHandle(unrelated)


@pytest.mark.skipif(os.name != "nt", reason="requires nested Windows Jobs")
def test_self_test_outer_job_kills_host_descendants_on_timeout() -> None:
    identity = SupervisedWorkspaceIdentity(
        f"wsi_{uuid.uuid4()}",
        "sha256:" + hashlib.sha256(b"self-test-host").hexdigest(),
        "sha256:" + hashlib.sha256(b"self-test-cleanup-job").hexdigest(),
    )
    job = WindowsWorkerJob(identity)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"LOCALAPPDATA", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
    }
    script = (
        "import os,subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],close_fds=True);"
        "os.write(3,str(p.pid).encode('ascii'));os.close(3);time.sleep(30)"
    )
    process = launch_windows_fixed_fd_process(
        Path(sys.executable),
        ["-c", script],
        environment=environment,
        child_inputs={},
        child_outputs=frozenset({3}),
        cleanup_job_handle=job.native_job_handle,
    )
    job_closed = False
    try:
        grandchild_pid = int(process.read_output(3, maximum_bytes=32, timeout_seconds=5).decode("ascii"))
        assert self_test._windows_process_is_running(grandchild_pid)
        job.close()
        job_closed = True
        process.wait(timeout_seconds=5)
        self_test._wait_windows_process_gone(grandchild_pid, 5)
    finally:
        if not job_closed:
            job.close()
        if process.poll() is None:
            process.terminate()
        process.close()


def test_missing_workspace_registry_record_fails_the_attach_gate(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    vault.mkdir()
    portable = ensure_portable_workspace_config(vault)
    registry = WorkspaceRegistry(tmp_path / "state" / "workspace-registry.json")

    with pytest.raises(RuntimeSelfTestError, match="Workspace registry"):
        self_test._require_self_test_workspace_record(registry, vault, portable.portable_workspace_id)


def test_live_worker_after_stop_fails_the_job_cleanup_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(self_test, "_windows_process_is_running", lambda pid: True)

    with pytest.raises(RuntimeSelfTestError, match="Job tree"):
        self_test._wait_windows_process_gone(42, 0.001)


@pytest.mark.skipif(os.name != "nt", reason="Windows denies deletion while SQLite HANDLE is open")
def test_sqlite_probe_closes_database_before_returning(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"

    self_test._probe_sqlite(database)

    database.unlink()
    assert not database.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["attach", "fd3_fd4", "registry", "job"])
async def test_any_host_worker_attach_fault_blocks_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    sandbox = tmp_path / "sandbox"

    def create_sandbox(nonce: str) -> Path:
        del nonce
        sandbox.mkdir()
        return sandbox

    async def broken_probe(runtime_root: Path, root: Path, nonce: str) -> None:
        del runtime_root, root, nonce
        raise RuntimeSelfTestError(f"{fault} fault")

    monkeypatch.setattr(self_test, "_verify_release_tree", lambda root: None)
    monkeypatch.setattr(self_test, "_probe_host_worker_images", lambda root: None)
    monkeypatch.setattr(self_test, "_probe_signed_process_host", lambda root: None)
    monkeypatch.setattr(self_test, "_create_private_self_test_root", create_sandbox)
    monkeypatch.setattr(self_test, "_probe_packaged_host_worker_attach", broken_probe)

    result = await self_test.run_self_test(tmp_path)

    assert result.healthy is False
    assert result.diagnostic_code == "self_test_runtime_self_test"
    assert "host_worker_signed_attach" not in result.checks


@pytest.mark.asyncio
async def test_successful_self_test_emits_every_installer_required_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = tmp_path / "sandbox"

    def create_sandbox(nonce: str) -> Path:
        del nonce
        sandbox.mkdir()
        return sandbox

    async def noop_async(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(self_test, "_verify_release_tree", lambda root: None)
    monkeypatch.setattr(self_test, "_probe_host_worker_images", lambda root: None)
    monkeypatch.setattr(self_test, "_probe_signed_process_host", lambda root: None)
    monkeypatch.setattr(self_test, "_create_private_self_test_root", create_sandbox)
    monkeypatch.setattr(self_test, "_probe_packaged_host_worker_attach", noop_async)
    monkeypatch.setattr(self_test, "_probe_sqlite", lambda path: None)
    monkeypatch.setattr(self_test, "_probe_vault_read_only", noop_async)
    monkeypatch.setattr(self_test, "_probe_loopback", lambda: None)
    monkeypatch.setattr(self_test, "_probe_named_pipe", noop_async)
    monkeypatch.setattr(self_test, "_probe_job_tree", lambda: None)

    result = await self_test.run_self_test(tmp_path)

    assert result.healthy is True
    assert SubprocessRuntimeSelfTestRunner._REQUIRED_CHECKS <= set(result.checks)


@pytest.mark.asyncio
async def test_self_test_cleanup_failure_is_not_hidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = tmp_path / "sandbox"

    def create_sandbox(nonce: str) -> Path:
        del nonce
        sandbox.mkdir()
        return sandbox

    async def noop_async(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def fail_cleanup(root: Path) -> None:
        del root
        raise PermissionError("synthetic open handle")

    monkeypatch.setattr(self_test, "_verify_release_tree", lambda root: None)
    monkeypatch.setattr(self_test, "_probe_host_worker_images", lambda root: None)
    monkeypatch.setattr(self_test, "_probe_signed_process_host", lambda root: None)
    monkeypatch.setattr(self_test, "_create_private_self_test_root", create_sandbox)
    monkeypatch.setattr(self_test, "_probe_packaged_host_worker_attach", noop_async)
    monkeypatch.setattr(self_test, "_probe_sqlite", lambda path: None)
    monkeypatch.setattr(self_test, "_probe_vault_read_only", noop_async)
    monkeypatch.setattr(self_test, "_probe_loopback", lambda: None)
    monkeypatch.setattr(self_test, "_probe_named_pipe", noop_async)
    monkeypatch.setattr(self_test, "_probe_job_tree", lambda: None)
    monkeypatch.setattr(self_test, "_remove_private_self_test_root", fail_cleanup)

    result = await self_test.run_self_test(tmp_path)

    assert result.healthy is False
    assert result.diagnostic_code == "self_test_cleanup_failed"


@pytest.mark.asyncio
async def test_development_self_test_allows_bounded_cold_path_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = tmp_path / "sandbox"
    observed_timeout: float | None = None

    class Manifest:
        plugin_version = "2.0.0-beta.28"

    class Trust:
        version_directory = tmp_path
        manifest = Manifest()

    async def capture_attach(
        runtime_root: Path,
        root: Path,
        nonce: str,
        *,
        plugin_version: str | None = None,
        discovery_timeout_seconds: float = 0,
    ) -> None:
        del runtime_root, root, nonce, plugin_version
        nonlocal observed_timeout
        observed_timeout = discovery_timeout_seconds

    async def noop_async(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def create_sandbox(nonce: str) -> Path:
        del nonce
        sandbox.mkdir()
        return sandbox

    monkeypatch.setattr(development_self_test, "InstalledDevelopmentRuntimeTrust", lambda root: Trust())
    monkeypatch.setattr(development_self_test, "_probe_host_worker_images", lambda root: None)
    monkeypatch.setattr(development_self_test, "_probe_development_process_host", lambda trust: None)
    monkeypatch.setattr(development_self_test, "_create_private_self_test_root", create_sandbox)
    monkeypatch.setattr(development_self_test, "_probe_packaged_host_worker_attach", capture_attach)
    monkeypatch.setattr(development_self_test, "_probe_sqlite", lambda path: None)
    monkeypatch.setattr(development_self_test, "_probe_vault_read_only", noop_async)
    monkeypatch.setattr(development_self_test, "_probe_loopback", lambda: None)
    monkeypatch.setattr(development_self_test, "_probe_named_pipe", noop_async)
    monkeypatch.setattr(development_self_test, "_probe_job_tree", lambda: None)

    result = await development_self_test.run_development_self_test(tmp_path)

    assert result.healthy is True
    assert observed_timeout == 180.0
    assert not sandbox.exists()


@pytest.mark.asyncio
async def test_development_self_test_retries_one_clean_cold_attach_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = tmp_path / "sandbox"
    attempts = 0
    removed_roots = 0

    class Manifest:
        plugin_version = "2.0.0-beta.28"

    class Trust:
        version_directory = tmp_path
        manifest = Manifest()

    async def cold_once(
        runtime_root: Path,
        root: Path,
        nonce: str,
        *,
        plugin_version: str | None = None,
        discovery_timeout_seconds: float = 0,
    ) -> None:
        del runtime_root, root, nonce, plugin_version, discovery_timeout_seconds
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeSelfTestError("synthetic first-path attach rejection")

    async def noop_async(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def create_sandbox(nonce: str) -> Path:
        del nonce
        sandbox.mkdir()
        return sandbox

    original_remove = self_test._remove_private_self_test_root

    def observe_remove(root: Path) -> None:
        nonlocal removed_roots
        original_remove(root)
        removed_roots += 1

    monkeypatch.setattr(development_self_test, "InstalledDevelopmentRuntimeTrust", lambda root: Trust())
    monkeypatch.setattr(development_self_test, "_probe_host_worker_images", lambda root: None)
    monkeypatch.setattr(development_self_test, "_probe_development_process_host", lambda trust: None)
    monkeypatch.setattr(development_self_test, "_create_private_self_test_root", create_sandbox)
    monkeypatch.setattr(development_self_test, "_probe_packaged_host_worker_attach", cold_once)
    monkeypatch.setattr(development_self_test, "_probe_sqlite", lambda path: None)
    monkeypatch.setattr(development_self_test, "_probe_vault_read_only", noop_async)
    monkeypatch.setattr(development_self_test, "_probe_loopback", lambda: None)
    monkeypatch.setattr(development_self_test, "_probe_named_pipe", noop_async)
    monkeypatch.setattr(development_self_test, "_probe_job_tree", lambda: None)
    monkeypatch.setattr(development_self_test, "_remove_private_self_test_root", observe_remove)

    result = await development_self_test.run_development_self_test(tmp_path)

    assert result.healthy is True
    assert attempts == 2
    assert removed_roots == 2
    assert not sandbox.exists()


@pytest.mark.parametrize(
    "missing",
    ["fd3_fd4_attach_contract", "host_worker_signed_attach", "host_worker_job_cleanup"],
)
def test_installer_rejects_self_test_that_omits_real_attach_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    executable = tmp_path / "offeragent-self-test.exe"
    executable.write_bytes(b"MZ")
    runner = SubprocessRuntimeSelfTestRunner()
    checks = {name: True for name in runner._REQUIRED_CHECKS if name != missing}
    payload = (
        json.dumps(
            {"checks": checks, "diagnosticCode": None, "healthy": True},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr=b""),
    )

    with pytest.raises(RuntimeInstallError, match="malformed"):
        runner.run(tmp_path, timeout_seconds=5)
