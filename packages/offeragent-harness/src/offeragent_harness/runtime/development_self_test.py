"""Pre-start self-test for the hash-pinned personal development Runtime."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from .development_runtime_manifest import InstalledDevelopmentRuntimeTrust
from .self_test import (
    RuntimeSelfTestError,
    SelfTestResult,
    _create_private_self_test_root,
    _diagnostic_code,
    _probe_host_worker_images,
    _probe_job_tree,
    _probe_loopback,
    _probe_named_pipe,
    _probe_packaged_host_worker_attach,
    _probe_sqlite,
    _probe_vault_read_only,
    _remove_private_self_test_root,
)


async def run_development_self_test(runtime_root: Path) -> SelfTestResult:
    checks: dict[str, bool] = {}
    diagnostic: str | None = None
    temporary_root: Path | None = None
    try:
        trust = InstalledDevelopmentRuntimeTrust(runtime_root)
        checks["development_manifest_exact_tree"] = True
        _probe_host_worker_images(trust.version_directory)
        checks["host_worker_hash_pinned_images"] = True
        await asyncio.to_thread(_probe_development_process_host, trust)
        checks["process_host_hash_pinned_runtime_info"] = True
        nonce = f"{time.time_ns():032x}"[-32:]
        temporary_root = await asyncio.to_thread(_create_private_self_test_root, nonce)
        try:
            await _probe_development_attach(trust, temporary_root, nonce)
        except RuntimeSelfTestError:
            # Windows can reject exactly the first complete Host/Worker attach
            # after an unsigned onedir tree is moved to its final plugin path,
            # while leaving a clean Job/process state and accepting the next
            # identical attach.  Retry once only after strict root cleanup and
            # with a fresh nonce.  A persistent fault still fails closed.
            await asyncio.to_thread(_remove_private_self_test_root, temporary_root)
            temporary_root = None
            nonce = f"{time.time_ns():032x}"[-32:]
            temporary_root = await asyncio.to_thread(_create_private_self_test_root, nonce)
            await _probe_development_attach(trust, temporary_root, nonce)
        checks["host_worker_fd3_fd4_attach"] = True
        checks["host_worker_job_cleanup"] = True
        _probe_sqlite(temporary_root / "self-test-state.sqlite")
        checks["sqlite_wal_integrity"] = True
        await _probe_vault_read_only(temporary_root / "read-only-vault")
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
                diagnostic = "self_test_cleanup_failed"
    return SelfTestResult(checks, diagnostic)


async def _probe_development_attach(
    trust: InstalledDevelopmentRuntimeTrust,
    temporary_root: Path,
    nonce: str,
) -> None:
    await _probe_packaged_host_worker_attach(
        trust.version_directory,
        temporary_root,
        nonce,
        plugin_version=trust.manifest.plugin_version,
        # A newly copied unsigned onedir tree can spend well over 40s in
        # first-path Windows scanning and DLL/PYZ cold loads.  The signed
        # release probe keeps its stricter default; the personal local build
        # gets one bounded cold-start window without skipping checks.
        # Keep an outer margin beyond the development Host's 120-second
        # Worker startup policy so discovery can publish and clean up without
        # racing the caller's watchdog.
        discovery_timeout_seconds=180.0,
    )


def _probe_development_process_host(trust: InstalledDevelopmentRuntimeTrust) -> None:
    completed = subprocess.run(
        [str(trust.version_directory / "offeragent-process-host.exe"), "shell-runtime-info"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        value = json.loads(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeSelfTestError("development Process Host probe was malformed") from error
    manifest = trust.manifest
    expected = {
        "buildCommit": manifest.build_commit,
        "coreVersion": manifest.core_version,
        "protocolMaximum": manifest.protocol.maximum,
        "protocolMinimum": manifest.protocol.minimum,
        "runtimeVersion": manifest.runtime_version,
        "toolAbiVersion": manifest.tool_abi_version,
    }
    canonical = (json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if completed.returncode != 0 or value != expected or completed.stdout != canonical or len(completed.stderr) > 4096:
        raise RuntimeSelfTestError("development Process Host identity probe failed")


def main(arguments: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if args == ["job-child"]:
        time.sleep(30)
        return 0
    if args != ["run", "--canonical-json"]:
        return 2
    result = asyncio.run(run_development_self_test(Path(sys.executable).resolve(strict=True).parent))
    sys.stdout.buffer.write(result.canonical_bytes())
    sys.stdout.buffer.flush()
    return 0 if result.healthy else 1


__all__ = ["main", "run_development_self_test"]
