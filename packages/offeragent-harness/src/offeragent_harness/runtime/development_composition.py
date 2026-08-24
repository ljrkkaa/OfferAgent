"""Entrypoint composition for the hash-pinned personal Windows x64 Runtime."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from .development_runtime_manifest import DevelopmentRuntimeError, InstalledDevelopmentRuntimeTrust
from .local_process_host import VerifiedRuntimeInfo
from .local_process_host import main as process_host_main
from .process_lock import ProcessLock, worker_mutex_name
from .production_worker_composition import _run_worker, parse_worker_arguments
from .startup import RuntimeStartupBlocked


def load_development_trust() -> InstalledDevelopmentRuntimeTrust:
    return InstalledDevelopmentRuntimeTrust(Path(sys.executable).resolve(strict=True).parent)


def worker_main(arguments: Sequence[str] | None = None) -> int:
    actual = list(sys.argv[1:] if arguments is None else arguments)
    try:
        command = parse_worker_arguments(actual)
        with ProcessLock(worker_mutex_name(command.canonical_root_identity)):
            trust = load_development_trust()
            executable = Path(sys.executable).resolve(strict=True)
            if executable.name.casefold() != "offeragent-worker.exe" or not trust.verify_file(executable):
                raise DevelopmentRuntimeError(
                    "development_worker_identity_invalid",
                    "local Worker differs from its pinned manifest identity",
                )
            asyncio.run(_run_worker(command, development_trust=trust))
    except BaseException as error:
        try:
            code = _worker_failure_code(error)
            os.write(2, f"offeragent-worker: local startup failed ({code})\n".encode())
        except OSError:
            pass
        return 2
    return 0


def _worker_failure_code(error: BaseException) -> str:
    if isinstance(error, RuntimeStartupBlocked):
        return f"startup_{error.phase.value}_failed"
    candidate = getattr(error, "code", None)
    if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", candidate):
        return candidate
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", name) else "worker_startup_failed"


def process_host_main_entry(arguments: list[str] | None = None) -> int:
    return process_host_main(arguments, info_loader=_load_development_runtime_info)


def _load_development_runtime_info(runtime_root: Path, executable: Path) -> VerifiedRuntimeInfo:
    trust = InstalledDevelopmentRuntimeTrust(runtime_root)
    image = executable.resolve(strict=True)
    if image.name.casefold() != "offeragent-process-host.exe" or not trust.verify_file(image):
        raise DevelopmentRuntimeError(
            "development_process_host_identity_invalid",
            "local process host differs from its pinned manifest identity",
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


__all__ = ["load_development_trust", "process_host_main_entry", "worker_main"]
