"""Entrypoint composition for an explicitly local, hash-pinned x64 Runtime.

The module supplies trust adapters only.  Host supervision, Worker composition,
Agent Core, transports, tools and storage remain the production implementations.
Formal release entry points never import or select this module.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .development_runtime_manifest import (
    DevelopmentManifestHashVerifier,
    DevelopmentRuntimeError,
    InstalledDevelopmentRuntimeTrust,
)
from .host_cli import HostCliApplication, parse_self_test_attach_arguments, parse_self_test_stop_arguments
from .host_cli import main as host_cli_main
from .host_supervisor import RestartPolicy
from .local_process_host import VerifiedRuntimeInfo
from .local_process_host import main as process_host_main
from .production_host_composition import create_windows_host_application
from .production_worker_composition import _run_worker, parse_worker_arguments
from .windows_process import (
    PinnedWorkerExecutableVerifier,
    WindowsWorkerProcessBackend,
)


def load_development_trust() -> InstalledDevelopmentRuntimeTrust:
    return InstalledDevelopmentRuntimeTrust(Path(sys.executable).resolve(strict=True).parent)


def create_development_host_application(*, self_test_nonce: str | None = None) -> HostCliApplication:
    """Build the shared Host only after exact local manifest verification."""

    trust = load_development_trust()
    executable = Path(sys.executable).resolve(strict=True)
    if executable.name.casefold() != "offeragent-host.exe" or not trust.verify_file(executable):
        raise DevelopmentRuntimeError(
            "development_host_identity_invalid",
            "development Host differs from its pinned manifest identity",
        )
    pinned = PinnedWorkerExecutableVerifier(
        manifest_trust=trust,
        authenticode=DevelopmentManifestHashVerifier(trust),
    )
    backend = WindowsWorkerProcessBackend(verifier=pinned, self_test_nonce=self_test_nonce)
    return create_windows_host_application(
        worker_executable=trust.worker_executable(),
        process_backend=backend,
        self_test_nonce=self_test_nonce,
        # The personal frozen Runtime performs recovery before it publishes
        # discovery. Windows startup on a real personal Vault can legitimately
        # exceed the signed channel's strict 30-second default; this override
        # is reachable only from the development composition root.
        worker_restart_policy=RestartPolicy(startup_timeout_seconds=120.0),
    )


def host_main(arguments: Sequence[str] | None = None) -> int:
    actual = list(sys.argv[1:] if arguments is None else arguments)
    if actual == ["image-probe", "--canonical-json"]:
        return _image_probe("host")
    if actual == ["control-probe", "--canonical-json"]:

        async def probe() -> None:
            application = create_development_host_application(self_test_nonce=f"{os.getpid():016x}")
            await application.self_test_control_start_stop()

        try:
            asyncio.run(probe())
        except BaseException:
            return 2
        sys.stdout.buffer.write(
            (
                json.dumps(
                    {"pid": os.getpid(), "role": "host", "status": "control-ready"},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        sys.stdout.buffer.flush()
        return 0
    nonce: str | None = None
    try:
        if actual[:1] == ["self-test-attach"]:
            nonce, _, _ = parse_self_test_attach_arguments(actual)
        elif actual[:1] == ["self-test-stop-all"]:
            nonce, _ = parse_self_test_stop_arguments(actual)
        application = create_development_host_application(self_test_nonce=nonce)
        return host_cli_main(actual, application=application)
    except BaseException:
        try:
            os.write(2, b"offeragent-host: local development startup failed\n")
        except OSError:
            pass
        return 2


def worker_main(arguments: Sequence[str] | None = None) -> int:
    actual = list(sys.argv[1:] if arguments is None else arguments)
    if actual == ["image-probe", "--canonical-json"]:
        return _image_probe("worker")
    try:
        command = parse_worker_arguments(actual)
        trust = load_development_trust()
        executable = Path(sys.executable).resolve(strict=True)
        if executable.name.casefold() != "offeragent-worker.exe" or not trust.verify_file(executable):
            raise DevelopmentRuntimeError(
                "development_worker_identity_invalid",
                "development Worker differs from its pinned manifest identity",
            )
        asyncio.run(_run_worker(command, development_trust=trust))
    except BaseException as error:
        try:
            code = getattr(error, "code", type(error).__name__)
            os.write(2, f"offeragent-worker: local development startup failed ({code})\n".encode())
        except OSError:
            pass
        return 2
    return 0


def process_host_main_entry(arguments: list[str] | None = None) -> int:
    return process_host_main(arguments, info_loader=_load_development_runtime_info)


def _load_development_runtime_info(runtime_root: Path, executable: Path) -> VerifiedRuntimeInfo:
    trust = InstalledDevelopmentRuntimeTrust(runtime_root)
    image = executable.resolve(strict=True)
    if image.name.casefold() != "offeragent-process-host.exe" or not trust.verify_file(image):
        raise DevelopmentRuntimeError(
            "development_process_host_identity_invalid",
            "development process host differs from its pinned manifest identity",
        )
    manifest = trust.manifest
    return VerifiedRuntimeInfo(
        runtime_version=manifest.runtime_version,
        core_version=manifest.core_version,
        tool_abi_version=manifest.tool_abi_version,
        protocol_minimum=manifest.protocol.minimum,
        protocol_maximum=manifest.protocol.maximum,
        build_commit=manifest.build_commit,
    )


def _image_probe(role: str) -> int:
    if role not in {"host", "worker"}:
        return 2
    try:
        trust = load_development_trust()
        executable = Path(sys.executable).resolve(strict=True)
        if executable.name.casefold() != f"offeragent-{role}.exe" or not trust.verify_file(executable):
            return 2
    except BaseException:
        return 2
    sys.stdout.buffer.write(
        (
            json.dumps(
                {"pid": os.getpid(), "role": role},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )
    sys.stdout.buffer.flush()
    return 0


__all__ = [
    "create_development_host_application",
    "host_main",
    "load_development_trust",
    "process_host_main_entry",
    "worker_main",
]
