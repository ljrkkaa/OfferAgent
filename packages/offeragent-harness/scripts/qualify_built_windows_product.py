"""Qualify only sealed Windows product outputs; never enter repository product source."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from offeragent_harness.qualification.windows_product_artifact import verify_paired_windows_artifacts
from offeragent_harness.qualification.windows_product_driver import QualificationDriverClient
from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config

_PRODUCTION_EXPORTS = [
    "StdioWorkerTransport",
    "VaultToolAdapter",
    "VaultChangeCoordinator",
    "FileVaultChangeJournal",
    "GitCheckpointStore",
    "ResearchBrowserAdapter",
]


class BuiltWindowsProductQualificationError(RuntimeError):
    pass


def smoke_built_windows_product(
    plugin_artifact: Path,
    qualification_artifact: Path,
    *,
    source_root_guard: Path,
    node_executable: Path,
    temporary_parent: Path | None = None,
) -> dict[str, Any]:
    """Start and stop the manifest-fixed Worker inside one identity-owned Vault."""

    paired = verify_paired_windows_artifacts(plugin_artifact, qualification_artifact)
    guard = _source_root_guard(source_root_guard)
    parent = Path(tempfile.gettempdir()) if temporary_parent is None else Path(temporary_parent)
    parent = parent.resolve(strict=True)
    ownership_token = secrets.token_hex(32)
    root = Path(tempfile.mkdtemp(prefix="offeragent-product-qualification-", dir=parent))
    marker = root / ".offeragent-qualification-owner.json"
    marker_payload = _canonical_json({"schemaVersion": 1, "token": ownership_token})
    marker.write_bytes(marker_payload)
    vault = root / "Vault"
    local_app_data = root / "LocalAppData"
    baseline = _offeragent_process_ids()
    result: dict[str, Any]
    removed = False
    try:
        vault.mkdir()
        local_app_data.mkdir()
        workspace_id = ensure_portable_workspace_config(vault).portable_workspace_id
        (vault / "agent.md").write_text("# OfferAgent built-product qualification\n", encoding="utf-8")
        runtime_root = paired.plugin_root / "runtime" / "windows-x64" / "local-development"
        runtime_manifest = _json_object(
            runtime_root / "development-runtime-manifest.json",
            "Runtime manifest",
        )
        protocol = runtime_manifest.get("protocol")
        if not isinstance(protocol, dict):
            raise BuiltWindowsProductQualificationError("Runtime manifest protocol is invalid")
        protocol_version = protocol.get("minimum")
        if protocol_version != protocol.get("maximum") or not isinstance(protocol_version, str):
            raise BuiltWindowsProductQualificationError("Runtime protocol range is not exact")
        schema_hash = protocol.get("schemaHash")
        if not isinstance(schema_hash, str):
            raise BuiltWindowsProductQualificationError("Runtime schema hash is invalid")
        payload = {
            "localAppData": str(local_app_data),
            "pluginVersion": paired.plugin_version,
            "protocolVersion": protocol_version,
            "runtimeVersion": paired.runtime_version,
            "schemaHash": schema_hash,
            "vaultRoot": str(vault),
            "workerExecutable": str(runtime_root / "offeragent-worker.exe"),
            "workspaceId": workspace_id,
        }
        with QualificationDriverClient(
            executable=node_executable,
            driver=paired.driver,
            working_directory=paired.qualification_root,
            source_root_guard=guard,
            environment_overrides=_offline_environment(),
        ) as driver:
            hello = driver.request("hello", {})
            if hello != {
                "driverProtocolVersion": 2,
                "reviewResolution": "explicit",
                "sourceFreeRuntime": True,
            }:
                raise BuiltWindowsProductQualificationError("qualification driver identity differs")
            started = driver.request("product/start", payload)
            identity = started.get("identity")
            worker_pid = identity.get("workerPid") if isinstance(identity, dict) else None
            transport = identity.get("transport") if isinstance(identity, dict) else None
            if (
                set(started) != {"identity", "reviewResolution", "sourceFreeRuntime"}
                or started.get("reviewResolution") != "explicit"
                or started.get("sourceFreeRuntime") is not True
                or not isinstance(worker_pid, int)
                or isinstance(worker_pid, bool)
                or worker_pid < 1
                or transport != "stdio"
            ):
                raise BuiltWindowsProductQualificationError("qualification driver offline start differs")
            observation = _offline_process_observation(worker_pid)
            allowed_loopback_sockets = observation["allowedLoopbackSockets"]
            allowed_system_descendants = observation["allowedSystemDescendants"]
            unexpected_sockets = observation["unexpectedSockets"]
            worker_descendants = observation["workerDescendants"]
            if unexpected_sockets:
                raise BuiltWindowsProductQualificationError(
                    f"offline Worker opened unexpected sockets: {unexpected_sockets}"
                )
            if worker_descendants:
                raise BuiltWindowsProductQualificationError(
                    f"offline Worker started unexpected descendants: {worker_descendants}"
                )
            stopped = driver.request("product/stop", {})
            if stopped != {"stopped": True, "workerPid": worker_pid}:
                raise BuiltWindowsProductQualificationError("qualification offline Worker did not stop exactly")
        result = {
            "driverProtocolVersion": 2,
            "sourceFreeRuntime": True,
            "transport": transport,
            "workerPid": worker_pid,
        }
        expected_keys = {"driverProtocolVersion", "sourceFreeRuntime", "transport", "workerPid"}
        if (
            set(result) != expected_keys
            or result.get("driverProtocolVersion") != 2
            or result.get("sourceFreeRuntime") is not True
            or result.get("transport") != "stdio"
            or not isinstance(result.get("workerPid"), int)
        ):
            raise BuiltWindowsProductQualificationError("qualification driver smoke contract differs")
    finally:
        _remove_owned_root(root, marker_payload)
        removed = not root.exists()
    final = _offeragent_process_ids()
    leaked = sorted(
        f"{image}:{process_id}"
        for image, process_ids in final.items()
        for process_id in process_ids - baseline.get(image, set())
    )
    if leaked:
        raise BuiltWindowsProductQualificationError(f"qualification leaked product processes: {leaked}")
    return {
        **result,
        "leakedProcesses": leaked,
        "runtimeManifestSha256": paired.runtime_manifest_sha256,
        "sourceCommit": paired.source_commit,
        "sourceTreeSha256": paired.source_tree_sha256,
        "temporaryRootRemoved": removed,
        "offlineStartup": {
            "networkBoundary": "closed-loopback-proxy-plus-active-socket-audit",
            "allowedLoopbackSockets": allowed_loopback_sockets,
            "allowedSystemDescendants": allowed_system_descendants,
            "unexpectedSockets": unexpected_sockets,
            "workerDescendants": worker_descendants,
            "systemPythonInvoked": False,
            "pipInvoked": False,
            "startupDownloadAttempted": False,
        },
    }


def probe_built_windows_product(
    plugin_artifact: Path,
    qualification_artifact: Path,
    *,
    source_root_guard: Path,
    node_executable: Path,
) -> dict[str, Any]:
    """Verify a pair, then execute only its precompiled plugin-adapter driver."""

    paired = verify_paired_windows_artifacts(plugin_artifact, qualification_artifact)
    guard = _source_root_guard(source_root_guard)
    completed = subprocess.run(
        [str(node_executable), str(paired.driver), "probe"],
        cwd=paired.qualification_root,
        env=_driver_environment(guard),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    report = _single_json_line(completed.stdout, "qualification driver probe")
    expected = {
        "driverProtocolVersion": 1,
        "productionExports": _PRODUCTION_EXPORTS,
        "sourceFreeRuntime": True,
    }
    if report != expected:
        raise BuiltWindowsProductQualificationError("qualification driver probe contract differs")
    return {
        **expected,
        "pluginRoot": str(paired.plugin_root),
        "qualificationRoot": str(paired.qualification_root),
        "runtimeManifestSha256": paired.runtime_manifest_sha256,
        "sourceCommit": paired.source_commit,
        "sourceTreeSha256": paired.source_tree_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="验证已密封的 OfferAgent Windows 产品构建")
    parser.add_argument("--plugin-artifact", type=Path, required=True)
    parser.add_argument("--qualification-artifact", type=Path, required=True)
    parser.add_argument("--source-root-guard", type=Path, required=True)
    parser.add_argument("--node-executable", type=Path)
    parser.add_argument("--probe-only", action="store_true")
    args = parser.parse_args()
    node = args.node_executable
    if node is None:
        discovered = shutil.which("node.exe") or shutil.which("node")
        if discovered is None:
            raise SystemExit("Node.js is unavailable for the qualification adapter")
        node = Path(discovered)
    operation = probe_built_windows_product if args.probe_only else smoke_built_windows_product
    report = operation(
        args.plugin_artifact,
        args.qualification_artifact,
        source_root_guard=args.source_root_guard,
        node_executable=node,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


def _source_root_guard(path: Path) -> Path:
    guard = Path(path)
    if not guard.is_absolute():
        raise BuiltWindowsProductQualificationError("source root guard must be absolute")
    try:
        guard = guard.resolve(strict=True)
    except OSError as error:
        raise BuiltWindowsProductQualificationError("source root guard is unavailable") from error
    if not guard.is_dir():
        raise BuiltWindowsProductQualificationError("source root guard is not a directory")
    return guard


def _driver_environment(source_root_guard: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["NODE_PATH"] = ""
    environment["OFFERAGENT_QUALIFICATION_FORBID_SOURCE_ROOT"] = str(source_root_guard)
    return environment


def _single_json_line(payload: str, label: str) -> dict[str, Any]:
    lines = payload.splitlines()
    if len(lines) != 1:
        raise BuiltWindowsProductQualificationError(f"{label} returned ambiguous output")
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise BuiltWindowsProductQualificationError(f"{label} returned malformed JSON") from error
    if not isinstance(value, dict):
        raise BuiltWindowsProductQualificationError(f"{label} result is not an object")
    return value


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BuiltWindowsProductQualificationError(f"{label} is unavailable or malformed") from error
    if not isinstance(value, dict):
        raise BuiltWindowsProductQualificationError(f"{label} is not an object")
    return value


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _remove_owned_root(root: Path, expected_marker: bytes) -> None:
    marker = root / ".offeragent-qualification-owner.json"
    try:
        info = marker.lstat()
        actual = marker.read_bytes()
    except OSError as error:
        raise BuiltWindowsProductQualificationError("qualification temp ownership marker is unavailable") from error
    if marker.is_symlink() or info.st_nlink != 1 or actual != expected_marker:
        raise BuiltWindowsProductQualificationError("qualification temp ownership identity differs")
    shutil.rmtree(root)


def _offline_environment() -> dict[str, str]:
    return {
        "ALL_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "UV_OFFLINE": "1",
    }


def _offline_process_observation(worker_pid: int) -> dict[str, list[str]]:
    if os.name != "nt" or worker_pid < 1:
        raise BuiltWindowsProductQualificationError("offline process observation requires Windows and a Worker PID")
    script = (
        "$ErrorActionPreference='Stop'; "
        "if($null -eq (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) -or "
        "$null -eq (Get-Command Get-NetUDPEndpoint -ErrorAction SilentlyContinue)){throw 'network audit unavailable'}; "
        f"$target=[int]{worker_pid}; "
        "$items=@(Get-CimInstance Win32_Process | ForEach-Object {"
        "[pscustomobject]@{name=[string]$_.Name;pid=[int]$_.ProcessId;parent=[int]$_.ParentProcessId}}); "
        "$owned=@($target); $desc=@(); "
        "do{$next=@($items | Where-Object {$owned -contains $_.parent -and $owned -notcontains $_.pid}); "
        "$desc+=@($next); $owned+=@($next | ForEach-Object {$_.pid})}while($next.Count -gt 0); "
        "$tcp=@(Get-NetTCPConnection -ErrorAction SilentlyContinue | "
        "Where-Object {$owned -contains $_.OwningProcess} | ForEach-Object {"
        "[pscustomobject]@{protocol='tcp';pid=[int]$_.OwningProcess;state=[string]$_.State;"
        "localAddress=[string]$_.LocalAddress;localPort=[int]$_.LocalPort;"
        "remoteAddress=[string]$_.RemoteAddress;remotePort=[int]$_.RemotePort}}); "
        "$udp=@(Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object {$owned -contains $_.OwningProcess} | "
        "ForEach-Object {[pscustomobject]@{protocol='udp';pid=[int]$_.OwningProcess;state='Bound';"
        "localAddress=[string]$_.LocalAddress;localPort=[int]$_.LocalPort;remoteAddress='';remotePort=0}}); "
        "[pscustomobject]@{activeSockets=@($tcp+$udp);"
        'workerDescendants=@($desc | ForEach-Object {"$($_.name):$($_.pid)"} | Sort-Object)} | '
        "ConvertTo-Json -Compress -Depth 4"
    )
    try:
        raw = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True,
            encoding="utf-8",
            timeout=30,
        ).strip()
        value = json.loads(raw)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise BuiltWindowsProductQualificationError("offline Worker process observation failed") from error
    if not isinstance(value, dict) or set(value) != {"activeSockets", "workerDescendants"}:
        raise BuiltWindowsProductQualificationError("offline Worker process observation shape differs")
    descendants = value["workerDescendants"]
    sockets = value["activeSockets"]
    if (
        not isinstance(descendants, list)
        or any(not isinstance(item, str) or not item for item in descendants)
        or not isinstance(sockets, list)
    ):
        raise BuiltWindowsProductQualificationError("offline Worker process observation entries differ")
    allowed, unexpected = _classify_offline_sockets(sockets)
    allowed_descendants = sorted(item for item in descendants if item.rsplit(":", 1)[0].casefold() == "conhost.exe")
    unexpected_descendants = sorted(set(descendants) - set(allowed_descendants))
    return {
        "allowedLoopbackSockets": allowed,
        "allowedSystemDescendants": allowed_descendants,
        "unexpectedSockets": unexpected,
        "workerDescendants": unexpected_descendants,
    }


def _classify_offline_sockets(values: Sequence[object]) -> tuple[list[str], list[str]]:
    fields = {
        "localAddress",
        "localPort",
        "pid",
        "protocol",
        "remoteAddress",
        "remotePort",
        "state",
    }
    records: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict) or set(value) != fields:
            raise BuiltWindowsProductQualificationError("offline Worker socket record differs")
        if (
            value["protocol"] not in {"tcp", "udp"}
            or not isinstance(value["pid"], int)
            or isinstance(value["pid"], bool)
            or not isinstance(value["state"], str)
            or not isinstance(value["localAddress"], str)
            or not isinstance(value["remoteAddress"], str)
            or not isinstance(value["localPort"], int)
            or not isinstance(value["remotePort"], int)
        ):
            raise BuiltWindowsProductQualificationError("offline Worker socket identity differs")
        records.append(value)
    loopback_ports = {
        port
        for record in records
        if record["protocol"] == "tcp"
        and record["state"] == "Established"
        and _loopback_address(record["localAddress"])
        and _loopback_address(record["remoteAddress"])
        for port in (record["localPort"], record["remotePort"])
    }
    allowed: list[str] = []
    unexpected: list[str] = []
    for record in records:
        identity = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        is_loopback_pair = (
            record["protocol"] == "tcp"
            and record["state"] == "Established"
            and _loopback_address(record["localAddress"])
            and _loopback_address(record["remoteAddress"])
        )
        is_bound_half = (
            record["protocol"] == "tcp"
            and record["state"] == "Bound"
            and _unspecified_address(record["localAddress"])
            and record["localPort"] in loopback_ports
        )
        (allowed if is_loopback_pair or is_bound_half else unexpected).append(identity)
    return sorted(allowed), sorted(unexpected)


def _loopback_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _unspecified_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_unspecified
    except ValueError:
        return False


def _offeragent_process_ids() -> dict[str, set[int]]:
    script = (
        "$items = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Name -in @('offeragent-worker.exe','offeragent-process-host.exe',"
        "'node.exe','conhost.exe') } | "
        "ForEach-Object { [pscustomobject]@{ name=$_.Name; pid=[int]$_.ProcessId } }); "
        "$items | ConvertTo-Json -Compress"
    )
    raw = subprocess.check_output(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        text=True,
        encoding="utf-8",
        timeout=30,
    ).strip()
    result: dict[str, set[int]] = {
        "conhost.exe": set(),
        "node.exe": set(),
        "offeragent-process-host.exe": set(),
        "offeragent-worker.exe": set(),
    }
    if not raw:
        return result
    value = json.loads(raw)
    records = value if isinstance(value, list) else [value]
    for record in records:
        if not isinstance(record, dict):
            continue
        name = record.get("name")
        process_id = record.get("pid")
        if name in result and isinstance(process_id, int):
            result[name].add(process_id)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
