"""Packaged Runtime self-test used before and after atomic activation."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from offeragent_harness.ports import ApplicationCommandContext, CancellationToken
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.capabilities import CapabilityName, CapabilitySet
from offeragent_harness.protocol.messages import InitializeResult, RuntimeStatusResult, ShutdownResult
from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.runtime.cancellation import CancellationScope
from offeragent_harness.workspace.filesystem import VaultFileSystem, VaultReadPolicy
from offeragent_harness.workspace.identity import WorkspaceInstanceRecord, WorkspaceRegistry
from offeragent_harness.workspace.path_policy import WorkspacePathPolicy
from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config
from offeragent_harness.workspace.runtime_identity import workspace_database_identity

from .host_cli import parse_self_test_attach_arguments, parse_self_test_stop_arguments
from .host_supervisor import SupervisedWorkspaceIdentity
from .named_pipe import (
    ConnectionRole,
    DiscoveryMaterialStore,
    DuplexJsonRpcConnection,
    HandshakeReplayGuard,
    authenticate_client_stream,
    authenticate_server_stream,
    parse_discovery_material,
)
from .release_manifest import ReleaseKeyring, RuntimeBundleVerifier, VerifiedRuntimeBundle, parse_manifest
from .release_trust import load_embedded_release_keys
from .windows_authenticode import WindowsAuthenticodeVerifier
from .windows_fixed_fd import WindowsFixedFdProcess, launch_windows_fixed_fd_process
from .windows_named_pipe import DpapiCurrentUserProtector, Win32NamedPipeListener, connect_windows_named_pipe
from .windows_process import WindowsWorkerJob, self_test_runtime_sandbox_root
from .windows_security import protect_current_user_path


class RuntimeSelfTestError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SelfTestResult:
    checks: dict[str, bool]
    diagnostic_code: str | None

    @property
    def healthy(self) -> bool:
        return bool(self.checks) and all(self.checks.values()) and self.diagnostic_code is None

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                {
                    "checks": dict(sorted(self.checks.items())),
                    "diagnosticCode": self.diagnostic_code,
                    "healthy": self.healthy,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")


class _NoWrites:
    async def execute(self, transaction: object, cancellation: object) -> object:
        del transaction, cancellation
        raise AssertionError("read-only self-test attempted a Vault transaction")


async def run_self_test(runtime_root: Path) -> SelfTestResult:
    checks: dict[str, bool] = {}
    diagnostic: str | None = None
    temporary_root: Path | None = None
    try:
        root = _resolved_directory(runtime_root)
        _verify_release_tree(root)
        checks["runtime_hashes_authenticode_web"] = True
        _probe_host_worker_images(root)
        checks["host_worker_start"] = True
        await asyncio.to_thread(_probe_signed_process_host, root)
        checks["process_host_signed_runtime_info"] = True
        nonce = secrets.token_hex(16)
        temporary_root = await asyncio.to_thread(_create_private_self_test_root, nonce)
        await _probe_packaged_host_worker_attach(root, temporary_root, nonce)
        checks["fd3_fd4_attach_contract"] = True
        checks["host_worker_signed_attach"] = True
        checks["host_worker_job_cleanup"] = True
        _probe_sqlite(temporary_root / "state.sqlite")
        checks["sqlite_wal_integrity"] = True
        await _probe_vault_read_only(temporary_root / "vault")
        checks["vault_read_only"] = True
        _probe_loopback()
        checks["loopback_random_bind"] = True
        await _probe_named_pipe(temporary_root / "pipe")
        checks["named_pipe_handshake"] = True
        _probe_job_tree()
        checks["job_object_exit"] = True
    except BaseException as error:
        diagnostic = _diagnostic_code(error)
    finally:
        if temporary_root is not None:
            try:
                await asyncio.to_thread(_remove_private_self_test_root, temporary_root)
            except BaseException:
                # A healthy self-test must not leave nonce-bound Runtime state
                # behind.  In particular, Windows refuses to remove an open
                # SQLite database; never hide that handle leak from activation.
                diagnostic = "self_test_cleanup_failed"
    return SelfTestResult(checks, diagnostic)


def _verify_release_tree(root: Path) -> None:
    manifest_bytes = (root / "runtime-manifest.json").read_bytes()
    signature = (root / "runtime-manifest.sig").read_bytes()
    manifest = parse_manifest(manifest_bytes)
    keyring = ReleaseKeyring(load_embedded_release_keys())
    keyring.verify(manifest.signing_key_id, manifest_bytes, signature)
    bundle = VerifiedRuntimeBundle(
        root=root,
        archive=root / manifest.archive.file_name,
        manifest_path=root / "runtime-manifest.json",
        signature_path=root / "runtime-manifest.sig",
        bootstrap_path=root / manifest.bootstrap.path,
        manifest_bytes=manifest_bytes,
        signature_bytes=signature,
        manifest=manifest,
    )
    RuntimeBundleVerifier(
        keyring=keyring,
        authenticode=WindowsAuthenticodeVerifier(),
        require_authenticode=True,
    ).verify_installed_tree(root, bundle)


def _probe_host_worker_images(root: Path) -> None:
    for role in ("host", "worker"):
        executable = root / f"offeragent-{role}.exe"
        completed = subprocess.run(
            [str(executable), "image-probe", "--canonical-json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            value = json.loads(completed.stdout.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeSelfTestError(f"{role} image probe was malformed") from error
        if (
            completed.returncode != 0
            or len(completed.stdout) > 4096
            or len(completed.stderr) > 4096
            or not isinstance(value, dict)
            or set(value) != {"pid", "role"}
            or value.get("role") != role
            or (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
            != completed.stdout
        ):
            raise RuntimeSelfTestError(f"{role} image probe failed")
        if not isinstance(value.get("pid"), int) or value["pid"] < 1:
            raise RuntimeSelfTestError(f"{role} image probe PID is invalid")


def _probe_packaged_host_control(runtime_root: Path, local_app_data: Path) -> None:
    local_app_data.mkdir()
    environment = dict(os.environ)
    environment["LOCALAPPDATA"] = str(local_app_data)
    completed = subprocess.run(
        [str(runtime_root / "offeragent-host.exe"), "control-probe", "--canonical-json"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
        shell=False,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        value = json.loads(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeSelfTestError("Host control probe was malformed") from error
    if (
        completed.returncode != 0
        or len(completed.stdout) > 4096
        or len(completed.stderr) > 4096
        or not isinstance(value, dict)
        or value.get("role") != "host"
        or value.get("status") != "control-ready"
        or set(value) != {"pid", "role", "status"}
        or not isinstance(value.get("pid"), int)
        or value["pid"] < 1
        or (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        != completed.stdout
    ):
        raise RuntimeSelfTestError("Host control probe failed")


def _probe_signed_process_host(runtime_root: Path) -> None:
    manifest = parse_manifest((runtime_root / "runtime-manifest.json").read_bytes())
    completed = subprocess.run(
        [str(runtime_root / "offeragent-process-host.exe"), "shell-runtime-info"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        value = json.loads(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeSelfTestError("signed Process Host probe was malformed") from error
    expected = {
        "buildCommit": manifest.build_commit,
        "coreVersion": manifest.core_version,
        "protocolMaximum": manifest.protocol.maximum,
        "protocolMinimum": manifest.protocol.minimum,
        "runtimeVersion": manifest.runtime_version,
        "toolAbiVersion": manifest.tool_abi_version,
    }
    if (
        completed.returncode != 0
        or len(completed.stdout) > 4096
        or len(completed.stderr) > 4096
        or value != expected
        or (json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        != completed.stdout
    ):
        raise RuntimeSelfTestError("signed Process Host probe failed")


class _RejectServerRequests:
    def require_ready(self) -> None:
        return None

    async def dispatch(
        self,
        method: str,
        params: Mapping[str, object] | WireModel,
        cancellation: CancellationToken,
        *,
        context: ApplicationCommandContext | None = None,
    ) -> object:
        del params, cancellation, context
        raise RuntimeSelfTestError(f"packaged Worker unexpectedly requested client method {method}")


async def _probe_packaged_host_worker_attach(
    runtime_root: Path,
    sandbox: Path,
    nonce: str,
    *,
    plugin_version: str | None = None,
    discovery_timeout_seconds: float = 40.0,
) -> None:
    """Exercise signed Host attach, supervised Worker, Pipe, stop and Job cleanup."""

    attach_arguments = [
        "self-test-attach",
        "--nonce",
        nonce,
        "--discovery-fd",
        "3",
        "--vault-root-fd",
        "4",
    ]
    stop_arguments = ["self-test-stop-all", "--nonce", nonce, "--result-fd", "3"]
    parse_self_test_attach_arguments(attach_arguments)
    parse_self_test_stop_arguments(stop_arguments)
    vault = sandbox / "Vault"
    vault.mkdir()
    protect_current_user_path(vault, directory=True)
    portable = ensure_portable_workspace_config(vault)
    local_app_data = sandbox / "LocalAppData"
    host = runtime_root / "offeragent-host.exe"
    environment = _self_test_host_environment(sandbox)
    cleanup_identity = SupervisedWorkspaceIdentity(
        workspace_instance_id=f"wsi_{uuid.uuid4()}",
        canonical_root_identity=f"sha256:{hashlib.sha256((nonce + ':host').encode()).hexdigest()}",
        database_identity=f"sha256:{hashlib.sha256((nonce + ':job').encode()).hexdigest()}",
    )
    cleanup_job = WindowsWorkerJob(cleanup_identity)
    cleanup_job_closed = False
    owner: WindowsFixedFdProcess | None = None
    connection: DuplexJsonRpcConnection | None = None
    material = None
    worker_pid: int | None = None
    stopped = False
    try:
        owner = await asyncio.to_thread(
            launch_windows_fixed_fd_process,
            host,
            attach_arguments,
            environment=environment,
            child_inputs={4: str(vault.resolve(strict=True)).encode("utf-8", errors="strict")},
            child_outputs=frozenset({3}),
            cleanup_job_handle=cleanup_job.native_job_handle,
        )
        discovery_payload = await asyncio.to_thread(
            owner.read_output,
            3,
            maximum_bytes=64 * 1024,
            timeout_seconds=discovery_timeout_seconds,
        )
        if owner.poll() is not None:
            raise RuntimeSelfTestError("signed Host exited instead of remaining the attach owner")
        try:
            material = parse_discovery_material(discovery_payload, now=datetime.now(timezone.utc))
        except (RuntimeError, ValueError) as error:
            raise RuntimeSelfTestError("signed Host fd3 discovery was invalid") from error

        registry = WorkspaceRegistry(local_app_data / "OfferAgent" / "workspace-registry.json")
        record = await asyncio.to_thread(
            _require_self_test_workspace_record,
            registry,
            vault,
            portable.portable_workspace_id,
        )
        database_identity = workspace_database_identity(record.workspace_instance_id)
        state_directory = local_app_data / "OfferAgent" / "workspaces" / record.workspace_instance_id
        store = DiscoveryMaterialStore(state_directory / "transport", protector=DpapiCurrentUserProtector())
        stored_material = await asyncio.to_thread(store.load, now=datetime.now(timezone.utc))
        if stored_material != material:
            raise RuntimeSelfTestError("Host discovery differs from the supervised Worker discovery store")

        stream = await connect_windows_named_pipe(material.pipe_name, timeout_seconds=5)
        await authenticate_client_stream(stream, material, now=lambda: datetime.now(timezone.utc))
        connection = DuplexJsonRpcConnection(
            stream,
            role=ConnectionRole.CLIENT,
            dispatcher=_RejectServerRequests(),
            command_transport="windows-named-pipe",
            command_peer="current-windows-sid",
            connection_id="runtime-self-test-host-attach",
        )
        await connection.start()
        if plugin_version is None:
            plugin_version = parse_manifest(
                (runtime_root / "runtime-manifest.json").read_bytes()
            ).plugin_minimum_version
        initialized = await connection.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientVersion": plugin_version,
                "workspaceId": portable.portable_workspace_id,
                "capabilities": CapabilitySet.from_enabled(set(CapabilityName)).to_wire(),
                "requiredCapabilities": ["eventReplay", "loopbackWeb"],
                "schemaHash": schema_hash(),
            },
            timeout_seconds=10,
        )
        if (
            not isinstance(initialized, InitializeResult)
            or initialized.host_pid != owner.pid
            or initialized.worker_pid == owner.pid
            or initialized.workspace_instance_id != record.workspace_instance_id
            or initialized.workspace_id != portable.portable_workspace_id
        ):
            raise RuntimeSelfTestError("Host-attached Worker initialize identity differs")
        worker_pid = initialized.worker_pid
        if not _windows_process_is_running(worker_pid):
            raise RuntimeSelfTestError("Host-attached Worker is not running after initialize")
        status = await connection.request("runtime/status", {}, timeout_seconds=10)
        if (
            not isinstance(status, RuntimeStatusResult)
            or status.host_pid != owner.pid
            or status.worker_pid != worker_pid
            or status.workspace_instance_id != record.workspace_instance_id
            or status.workspace_id != portable.portable_workspace_id
            or status.database_identity != database_identity
        ):
            raise RuntimeSelfTestError("Host-attached Worker status identity differs")
        shutdown = await connection.request(
            "shutdown",
            {"reason": "upgrade", "gracePeriodMs": 5_000},
            timeout_seconds=10,
        )
        if not isinstance(shutdown, ShutdownResult) or not shutdown.accepted:
            raise RuntimeSelfTestError("Host-attached Worker rejected shutdown")
        await connection.close()
        connection = None

        await _request_self_test_stop(host, stop_arguments, environment)
        stopped = True
        owner_exit = await asyncio.to_thread(owner.wait, timeout_seconds=20)
        if owner_exit != 0:
            raise RuntimeSelfTestError("signed Host did not exit cleanly after stop-all")
        await asyncio.to_thread(_wait_windows_process_gone, worker_pid, 15.0)
        await _assert_pipe_listener_removed(material.pipe_name)
        _assert_discovery_removed(store)
        control_store = DiscoveryMaterialStore(
            local_app_data / "OfferAgent" / "host" / "self-test" / nonce / "control",
            protector=DpapiCurrentUserProtector(),
        )
        _assert_discovery_removed(control_store)
        if not await cleanup_job.wait_empty(5):
            raise RuntimeSelfTestError("Host self-test cleanup Job did not become empty")
        cleanup_job.close()
        cleanup_job_closed = True
    finally:
        try:
            if connection is not None:
                await connection.close()
        finally:
            if owner is not None:
                try:
                    if owner.poll() is None and not stopped:
                        try:
                            await _request_self_test_stop(host, stop_arguments, environment)
                        except BaseException:
                            pass
                finally:
                    try:
                        if not cleanup_job_closed:
                            cleanup_job.close()
                            cleanup_job_closed = True
                    finally:
                        if owner.poll() is None:
                            try:
                                await asyncio.to_thread(owner.wait, timeout_seconds=10)
                            except TimeoutError:
                                owner.terminate()
                                await asyncio.to_thread(owner.wait, timeout_seconds=5)
                        owner.close()
            elif not cleanup_job_closed:
                cleanup_job.close()


async def _request_self_test_stop(
    host: Path,
    arguments: list[str],
    environment: Mapping[str, str],
) -> None:
    process = await asyncio.to_thread(
        launch_windows_fixed_fd_process,
        host,
        arguments,
        environment=environment,
        child_inputs={},
        child_outputs=frozenset({3}),
    )
    try:
        payload = await asyncio.to_thread(
            process.read_output,
            3,
            maximum_bytes=4 * 1024,
            timeout_seconds=65,
        )
        exit_code = await asyncio.to_thread(process.wait, timeout_seconds=10)
        expected = {
            b'{"schemaVersion":1,"status":"already_stopped","type":"result"}\n',
            b'{"schemaVersion":1,"status":"stopped","type":"result"}\n',
        }
        if exit_code != 0 or payload not in expected:
            raise RuntimeSelfTestError("signed Host stop-all receipt was invalid")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout_seconds=5)
            except TimeoutError:
                pass
        process.close()


def _self_test_host_environment(sandbox: Path) -> dict[str, str]:
    allowed = {"APPDATA", "LOCALAPPDATA", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "WINDIR"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    if not any(key.upper() in {"SYSTEMROOT", "WINDIR"} for key in environment):
        raise RuntimeSelfTestError("Windows directory is unavailable to the signed Host")
    # Do not replace USERPROFILE/APPDATA/LOCALAPPDATA with empty synthetic
    # directories.  The Host obtains those roots from Windows Known Folders,
    # and SHGetKnownFolderPath legitimately fails when the profile backing the
    # launched process does not exist.  Nonce-bound Host/Worker state is
    # already redirected by ``self_test_runtime_sandbox_root`` and
    # ``_isolated_self_test_worker_environment``.  Only scratch files need an
    # environment override here.
    isolated = {"TEMP": sandbox / "Temp", "TMP": sandbox / "Temp"}
    for root in sorted(set(isolated.values()), key=str):
        root.mkdir(parents=True, exist_ok=True)
        protect_current_user_path(root, directory=True)
    environment.update({key: str(value.resolve(strict=True)) for key, value in isolated.items()})
    return environment


def _windows_process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        code = ctypes.get_last_error()
        if code in {87, 1168}:
            return False
        raise RuntimeSelfTestError("Worker process identity could not be inspected")
    try:
        result = int(kernel32.WaitForSingleObject(handle, 0))
        if result == 0x00000102:
            return True
        if result == 0x00000000:
            return False
        raise RuntimeSelfTestError("Worker process wait state was invalid")
    finally:
        kernel32.CloseHandle(handle)


def _wait_windows_process_gone(pid: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while _windows_process_is_running(pid):
        if time.monotonic() >= deadline:
            raise RuntimeSelfTestError("Worker Job tree remained alive after Host stop-all")
        time.sleep(0.05)


async def _assert_pipe_listener_removed(pipe_name: str) -> None:
    try:
        stream = await connect_windows_named_pipe(pipe_name, timeout_seconds=0.25)
    except (OSError, TimeoutError):
        return
    await stream.close()
    raise RuntimeSelfTestError("Worker Named Pipe listener survived Host stop-all")


def _assert_discovery_removed(store: DiscoveryMaterialStore) -> None:
    try:
        store.load(now=datetime.now(timezone.utc))
    except (OSError, RuntimeError, ValueError):
        return
    raise RuntimeSelfTestError("Runtime discovery material survived Host stop-all")


def _require_self_test_workspace_record(
    registry: WorkspaceRegistry,
    vault: Path,
    portable_workspace_id: str,
) -> WorkspaceInstanceRecord:
    record = registry.lookup(vault)
    if record is None or record.portable_workspace_id != portable_workspace_id:
        raise RuntimeSelfTestError("signed Host attach did not commit the isolated Workspace registry")
    return record


@dataclass(frozen=True, slots=True)
class _PreparedWorkerProbe:
    arguments: tuple[str, ...]
    environment: dict[str, str]
    state_directory: Path
    workspace_id: str
    workspace_instance_id: str
    database_identity: str
    plugin_minimum_version: str


async def _probe_packaged_worker(runtime_root: Path, local_app_data: Path, vault: Path) -> None:
    """Direct Worker diagnostic; it is deliberately not an activation gate."""

    prepared = await asyncio.to_thread(_prepare_packaged_worker_probe, runtime_root, local_app_data, vault)
    child = await asyncio.create_subprocess_exec(
        *prepared.arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=prepared.environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    connection: DuplexJsonRpcConnection | None = None
    try:
        store = DiscoveryMaterialStore(prepared.state_directory / "transport", protector=DpapiCurrentUserProtector())
        deadline = time.monotonic() + 30.0
        material = None
        while material is None and time.monotonic() < deadline:
            if child.returncode is not None:
                raise RuntimeSelfTestError("packaged Worker exited before discovery")
            try:
                material = await asyncio.to_thread(store.load, now=datetime.now(timezone.utc))
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                await asyncio.sleep(0.05)
        if material is None:
            raise RuntimeSelfTestError("packaged Worker discovery timed out")
        stream = await connect_windows_named_pipe(material.pipe_name, timeout_seconds=5)
        await authenticate_client_stream(stream, material, now=lambda: datetime.now(timezone.utc))
        connection = DuplexJsonRpcConnection(
            stream,
            role=ConnectionRole.CLIENT,
            dispatcher=_RejectServerRequests(),
            command_transport="windows-named-pipe",
            command_peer="current-windows-sid",
            connection_id="runtime-self-test",
        )
        await connection.start()
        initialized = await connection.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientVersion": prepared.plugin_minimum_version,
                "workspaceId": prepared.workspace_id,
                "capabilities": CapabilitySet.from_enabled(set(CapabilityName)).to_wire(),
                "requiredCapabilities": ["eventReplay", "loopbackWeb"],
                "schemaHash": schema_hash(),
            },
            timeout_seconds=10,
        )
        if not isinstance(initialized, InitializeResult) or initialized.worker_pid != child.pid:
            raise RuntimeSelfTestError("packaged Worker initialize identity differs")
        status = await connection.request("runtime/status", {}, timeout_seconds=10)
        if (
            not isinstance(status, RuntimeStatusResult)
            or status.worker_pid != child.pid
            or status.workspace_instance_id != prepared.workspace_instance_id
            or status.database_identity != prepared.database_identity
        ):
            raise RuntimeSelfTestError("packaged Worker status identity differs")
        await connection.request(
            "shutdown",
            {"reason": "upgrade", "gracePeriodMs": 5_000},
            timeout_seconds=10,
        )
        await connection.close()
        connection = None
        await asyncio.wait_for(child.wait(), timeout=15)
        if child.returncode != 0:
            raise RuntimeSelfTestError("packaged Worker did not exit cleanly")
    finally:
        if connection is not None:
            await connection.close()
        if child.returncode is None:
            child.terminate()
            try:
                await asyncio.wait_for(child.wait(), timeout=5)
            except TimeoutError:
                child.kill()
                await asyncio.wait_for(child.wait(), timeout=5)


def _prepare_packaged_worker_probe(runtime_root: Path, local_app_data: Path, vault: Path) -> _PreparedWorkerProbe:
    local_app_data.mkdir()
    offeragent_root = local_app_data / "OfferAgent"
    workspaces_root = offeragent_root / "workspaces"
    workspaces_root.mkdir(parents=True)
    vault.mkdir()
    portable = ensure_portable_workspace_config(vault)
    record = WorkspaceRegistry(offeragent_root / "workspace-registry.json").register(
        vault,
        portable_workspace_id=portable.portable_workspace_id,
    )
    database_identity = workspace_database_identity(record.workspace_instance_id)
    manifest = parse_manifest((runtime_root / "runtime-manifest.json").read_bytes())
    worker = runtime_root / "offeragent-worker.exe"
    arguments = (
        str(worker),
        "--offeragent-runtime-mode",
        "worker",
        "--transport",
        "named-pipe",
        "--workspace-instance-id",
        record.workspace_instance_id,
        "--canonical-root-identity",
        record.root_identity.identity_hash,
        "--database-identity",
        database_identity,
        "--runtime-version",
        manifest.runtime_version,
    )
    environment = dict(os.environ)
    environment["LOCALAPPDATA"] = str(local_app_data)
    return _PreparedWorkerProbe(
        arguments=arguments,
        environment=environment,
        state_directory=workspaces_root / record.workspace_instance_id,
        workspace_id=portable.portable_workspace_id,
        workspace_instance_id=record.workspace_instance_id,
        database_identity=database_identity,
        plugin_minimum_version=manifest.plugin_minimum_version,
    )


def _probe_sqlite(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        with connection:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            connection.execute("CREATE TABLE self_test(value TEXT NOT NULL)")
            connection.execute("INSERT INTO self_test(value) VALUES ('ok')")
            result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        # sqlite3.Connection.__exit__ commits or rolls back; it does not close.
        # An explicit close is required before rmtree can remove the database on
        # Windows and keeps repeated plugin self-tests residue-free.
        connection.close()
    if mode is None or str(mode[0]).casefold() != "wal" or result != ("ok",):
        raise RuntimeSelfTestError("SQLite WAL/integrity probe failed")


def _remove_private_self_test_root(root: Path) -> None:
    shutil.rmtree(root)
    if root.exists():
        raise RuntimeSelfTestError("Runtime self-test private root cleanup was not confirmed")


async def _probe_vault_read_only(root: Path) -> None:
    _create_self_test_vault(root)
    filesystem = VaultFileSystem(
        workspace_id="ws_self_test",
        paths=WorkspacePathPolicy(root),
        read_policy=VaultReadPolicy(
            allowed_extensions=frozenset({".md"}),
            max_file_bytes=4096,
            max_return_bytes=4096,
            max_list_entries=10,
            max_list_scan_entries=10,
        ),
        workspace_revision=lambda: 0,
        transaction_executor=_NoWrites(),  # type: ignore[arg-type]
    )
    cancellation = CancellationScope(name="self-test-vault")
    value = await filesystem.read("note.md", cancellation)
    if value.content != b"self-test\n":
        raise RuntimeSelfTestError("Vault read-only probe returned wrong content")
    await cancellation.close()


def _probe_loopback() -> None:
    sockets: list[socket.socket] = []
    try:
        for family, address in ((socket.AF_INET, ("127.0.0.1", 0)), (socket.AF_INET6, ("::1", 0))):
            try:
                listener = socket.socket(family, socket.SOCK_STREAM)
                listener.bind(address)
                listener.listen(1)
                sockets.append(listener)
            except OSError:
                if family == socket.AF_INET:
                    raise
    finally:
        for listener in sockets:
            listener.close()


async def _probe_named_pipe(directory: Path) -> None:
    protector = DpapiCurrentUserProtector()
    store = DiscoveryMaterialStore(directory, protector=protector)
    material = store.issue(now=datetime.now(timezone.utc))
    listener = Win32NamedPipeListener(material.pipe_name)

    async def server() -> None:
        stream = await listener.accept()
        try:
            await authenticate_server_stream(
                stream,
                material,
                now=lambda: datetime.now(timezone.utc),
                replay_guard=HandshakeReplayGuard(),
            )
        finally:
            await stream.close()

    task = asyncio.create_task(server())
    client = await connect_windows_named_pipe(material.pipe_name, timeout_seconds=5)
    try:
        await authenticate_client_stream(client, material, now=lambda: datetime.now(timezone.utc))
        await task
    finally:
        await client.close()
        await listener.close()
        store.remove()


def _probe_job_tree() -> None:
    identity = SupervisedWorkspaceIdentity(
        workspace_instance_id=f"wsi_{uuid.uuid4()}",
        canonical_root_identity=f"sha256:{hashlib.sha256(b'self-test-root').hexdigest()}",
        database_identity=f"sha256:{hashlib.sha256(b'self-test-db').hexdigest()}",
    )
    job = WindowsWorkerJob(identity)
    child = subprocess.Popen(
        [sys.executable, "job-child"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        process_handle = int(child._handle)  # type: ignore[attr-defined]
        job.assign_process_handle(process_handle)
        job.close()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def _diagnostic_code(error: BaseException) -> str:
    name = type(error).__name__.replace("Error", "").replace("Exception", "")
    normalized = "".join(f"_{character.lower()}" if character.isupper() else character for character in name).strip("_")
    return f"self_test_{normalized or 'failed'}"[:128]


def _resolved_directory(path: Path) -> Path:
    value = path.resolve(strict=True)
    if not value.is_dir():
        raise RuntimeSelfTestError("Runtime root is not a directory")
    return value


def _create_private_self_test_root(nonce: str) -> Path:
    root = self_test_runtime_sandbox_root(nonce)
    parent = root.parent
    parent.mkdir(parents=True, exist_ok=True)
    protect_current_user_path(parent, directory=True)
    root.mkdir()
    protect_current_user_path(root, directory=True)
    return root


def _create_self_test_vault(root: Path) -> None:
    root.mkdir()
    # ``Path.write_text`` translates LF to CRLF on Windows.  The Vault probe
    # asserts exact bytes, so generate its canonical fixture as bytes.
    (root / "note.md").write_bytes(b"self-test\n")


def main(arguments: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if args == ["job-child"]:
        import time

        time.sleep(30)
        return 0
    if args != ["run", "--canonical-json"]:
        return 2
    result = asyncio.run(run_self_test(Path(sys.executable).resolve(strict=True).parent))
    sys.stdout.buffer.write(result.canonical_bytes())
    sys.stdout.buffer.flush()
    return 0 if result.healthy else 1


__all__ = ["RuntimeSelfTestError", "SelfTestResult", "main", "run_self_test"]
