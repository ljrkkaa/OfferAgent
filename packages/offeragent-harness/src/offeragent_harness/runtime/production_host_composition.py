"""Production-only composition root for the signed Windows Host executable."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from offeragent_harness.workspace.identity import WorkspaceRegistry

from .host_cli import (
    DpapiWorkspaceDiscoveryProvider,
    HostCliApplication,
    HostControlBroker,
    SupervisedHostAttachEngine,
)
from .host_supervisor import (
    HostSupervisor,
    LockFactory,
    RestartPolicy,
    SupervisedWorkspaceIdentity,
    VerifiedWorkerExecutable,
    WorkerSupervisor,
    WorkerSupervisorRegistry,
)
from .named_pipe import DiscoveryMaterialStore
from .process_lock import ProcessAlreadyRunning, ProcessLock, host_mutex_name
from .release_manifest import ReleaseKeyring, ReleaseVerificationError
from .release_trust import InstalledReleaseManifestTrust, load_embedded_release_keys
from .windows_authenticode import WindowsAuthenticodeVerifier
from .windows_named_pipe import DpapiCurrentUserProtector
from .windows_process import (
    PinnedWorkerExecutableVerifier,
    WindowsJobObjectBackend,
    WindowsWorkerProcessBackend,
    self_test_runtime_sandbox_root,
    trusted_worker_environment,
)
from .windows_security import protect_current_user_path
from .worker_control import WindowsWorkerControlClient


class SystemClock:
    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep_until(self, deadline: datetime) -> None:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")
        await asyncio.sleep(max(0.0, (deadline - self.utcnow()).total_seconds()))


def create_production_host_application(*, self_test_nonce: str | None = None) -> HostCliApplication:
    if os.name != "nt":
        raise OSError("OfferAgent Host production composition requires Windows")
    version_directory = Path(sys.executable).resolve(strict=True).parent
    trust = InstalledReleaseManifestTrust(
        version_directory,
        keyring=ReleaseKeyring(load_embedded_release_keys()),
    )
    authenticode = WindowsAuthenticodeVerifier()
    _verify_host_identity(version_directory, trust, authenticode)
    worker_executable = trust.worker_executable()
    pinned = PinnedWorkerExecutableVerifier(manifest_trust=trust, authenticode=authenticode)
    process_backend = WindowsWorkerProcessBackend(verifier=pinned, self_test_nonce=self_test_nonce)
    return create_windows_host_application(
        worker_executable=worker_executable,
        process_backend=process_backend,
        self_test_nonce=self_test_nonce,
    )


def create_windows_host_application(
    *,
    worker_executable: VerifiedWorkerExecutable,
    process_backend: WindowsWorkerProcessBackend,
    self_test_nonce: str | None = None,
    worker_restart_policy: RestartPolicy | None = None,
) -> HostCliApplication:
    """Compose the one Host implementation from an already verified Worker image.

    Production and the explicitly local-development entry point share this
    composition.  Trust establishment remains outside this seam, so the signed
    production entry point cannot silently select development trust.
    """

    if os.name != "nt":
        raise OSError("OfferAgent Host composition requires Windows")
    trusted_environment = trusted_worker_environment()
    if self_test_nonce is None:
        local_app_data = Path(trusted_environment["LOCALAPPDATA"])
    else:
        local_app_data = self_test_runtime_sandbox_root(self_test_nonce) / "LocalAppData"
    local_root = (local_app_data / "OfferAgent").resolve(strict=False)
    local_root.mkdir(parents=True, exist_ok=True)
    protect_current_user_path(local_root, directory=True)
    workspaces_root = local_root / "workspaces"
    workspaces_root.mkdir(exist_ok=True)
    protect_current_user_path(workspaces_root, directory=True)

    job_backend = WindowsJobObjectBackend()
    protector = DpapiCurrentUserProtector()
    clock = SystemClock()
    worker_control = WindowsWorkerControlClient(
        workspaces_root=workspaces_root,
        protector=protector,
        now=clock.utcnow,
    )

    def worker_factory(identity: SupervisedWorkspaceIdentity) -> WorkerSupervisor:
        return WorkerSupervisor(
            identity=identity,
            executable=worker_executable,
            process_backend=process_backend,
            job_backend=job_backend,
            readiness_probe=worker_control,
            shutdown_control=worker_control,
            clock=clock,
            restart_policy=worker_restart_policy,
        )

    lock_factory: LockFactory | None = None
    control_root = local_root / "host" / "control"
    if self_test_nonce is not None:
        if re.fullmatch(r"[0-9a-f]{16,64}", self_test_nonce) is None:
            raise ValueError("Host self-test nonce is invalid")

        def self_test_lock_factory(name: str) -> ProcessLock:
            return ProcessLock(f"{name}.SelfTest.{self_test_nonce}")

        lock_factory = self_test_lock_factory
        control_root = local_root / "host" / "self-test" / self_test_nonce / "control"
    supervisor = HostSupervisor(
        workers=WorkerSupervisorRegistry(worker_factory),
        clock=clock,
        lock_factory=lock_factory,
    )
    engine = SupervisedHostAttachEngine(
        supervisor=supervisor,
        registry=WorkspaceRegistry(local_root / "workspace-registry.json"),
        discovery=DpapiWorkspaceDiscoveryProvider(workspaces_root, protector=protector),
        now=clock.utcnow,
    )
    control_store = DiscoveryMaterialStore(control_root, protector=protector)
    control = HostControlBroker(engine=engine, material_store=control_store, now=clock.utcnow)
    return HostCliApplication(
        engine=engine,
        control=control,
        host_stopped=lambda: _host_is_stopped(self_test_nonce),
    )


def _host_is_stopped(self_test_nonce: str | None = None) -> bool:
    """Probe the current-SID owner mutex without starting a replacement Host."""

    name = host_mutex_name()
    if self_test_nonce is not None:
        if re.fullmatch(r"[0-9a-f]{16,64}", self_test_nonce) is None:
            raise ValueError("Host self-test nonce is invalid")
        name = f"{name}.SelfTest.{self_test_nonce}"
    lock = ProcessLock(name)
    try:
        lock.acquire(timeout_ms=0)
    except ProcessAlreadyRunning:
        return False
    else:
        lock.release()
        return True


def _verify_host_identity(
    version_directory: Path,
    trust: InstalledReleaseManifestTrust,
    authenticode: WindowsAuthenticodeVerifier,
) -> None:
    record = trust.manifest.by_path.get("offeragent-host.exe")
    executable = Path(sys.executable).resolve(strict=True)
    expected = (version_directory / "offeragent-host.exe").resolve(strict=True)
    if executable != expected or record is None or not record.authenticode:
        raise ReleaseVerificationError("host_identity_invalid", "signed Host identity is missing")
    digest = hashlib.sha256()
    with executable.open("rb", buffering=0) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if record.sha256 != f"sha256:{digest.hexdigest()}" or not authenticode.verify(executable):
        raise ReleaseVerificationError("host_identity_invalid", "signed Host identity verification failed")


__all__ = ["SystemClock", "create_production_host_application", "create_windows_host_application"]
