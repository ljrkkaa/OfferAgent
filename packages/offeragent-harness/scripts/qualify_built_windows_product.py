"""Qualify only sealed Windows product outputs; never enter repository product source."""

from __future__ import annotations

import argparse
import hashlib
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

from offeragent_harness.qualification.owned_temporary_root import (
    OwnedTemporaryRootError,
    remove_owned_temporary_root,
)
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
    trace_path = root / "offline-audit.jsonl"
    trace_token = secrets.token_hex(32)
    vault = root / "Vault"
    local_app_data = root / "LocalAppData"
    owned_processes: dict[int, str] = {}
    result: dict[str, Any] | None = None
    offline_audit: dict[str, Any] | None = None
    primary_error: BaseException | None = None
    primary_cause: BaseException | None = None
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
            environment_overrides=_offline_environment(trace_path, trace_token),
        ) as driver:
            if driver.process_id < 1:
                raise BuiltWindowsProductQualificationError("qualification driver process identity differs")
            owned_processes[driver.process_id] = "node.exe"
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
            owned_processes[worker_pid] = "offeragent-worker.exe"
            observation = _offline_process_observation(worker_pid)
            allowed_loopback_sockets = observation["allowedLoopbackSockets"]
            allowed_system_descendants = observation["allowedSystemDescendants"]
            unexpected_sockets = observation["unexpectedSockets"]
            worker_descendants = observation["workerDescendants"]
            for descendant in allowed_system_descendants:
                name, process_id = descendant.rsplit(":", 1)
                owned_processes[int(process_id)] = name
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
        offline_audit = _offline_audit_report(trace_path, trace_token)
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
    except BuiltWindowsProductQualificationError as error:
        primary_error = error
    except (OSError, subprocess.SubprocessError) as error:
        primary_error = BuiltWindowsProductQualificationError(str(error))
        primary_cause = error
    except BaseException as error:
        primary_error = error

    audit_errors: list[str] = []
    if offline_audit is None:
        try:
            offline_audit = _offline_audit_report(trace_path, trace_token)
        except BaseException as error:
            audit_errors.append(f"offline guard audit failed: {type(error).__name__}")
    try:
        _remove_owned_root(root, marker_payload)
        removed = not root.exists()
    except BaseException as error:
        audit_errors.append(f"qualification owned-root cleanup failed: {type(error).__name__}")
    leaked: list[str] = []
    try:
        leaked = _alive_owned_processes(owned_processes)
        if leaked:
            audit_errors.append(f"qualification leaked product processes: {leaked}")
    except BaseException as error:
        audit_errors.append(f"qualification process safety audit failed: {type(error).__name__}")
    if not removed:
        audit_errors.append("qualification temporary root was not removed")
    if audit_errors:
        audit_summary = "; ".join(audit_errors)
        if primary_error is not None:
            raise BuiltWindowsProductQualificationError(f"{primary_error}; safety audit: {audit_summary}") from (
                primary_cause or primary_error
            )
        raise BuiltWindowsProductQualificationError(f"qualification safety audit failed: {audit_summary}")
    if primary_error is not None:
        if primary_cause is not None:
            raise primary_error from primary_cause
        raise primary_error
    assert result is not None and offline_audit is not None
    return {
        **result,
        "leakedProcesses": leaked,
        "runtimeManifestSha256": paired.runtime_manifest_sha256,
        "sourceCommit": paired.source_commit,
        "sourceTreeSha256": paired.source_tree_sha256,
        "temporaryRootRemoved": removed,
        "offlineStartup": {
            **offline_audit,
            "networkBoundary": "pre-import-python-audit-deny-plus-active-socket-audit",
            "allowedLoopbackSockets": allowed_loopback_sockets,
            "allowedSystemDescendants": allowed_system_descendants,
            "unexpectedSockets": unexpected_sockets,
            "workerDescendants": worker_descendants,
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
    try:
        remove_owned_temporary_root(root, expected_marker)
    except OwnedTemporaryRootError as error:
        raise BuiltWindowsProductQualificationError(str(error)) from error


def _offline_environment(trace_path: Path, token: str) -> dict[str, str]:
    return {
        "ALL_PROXY": "",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "NO_PROXY": "",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "UV_OFFLINE": "1",
        "OFFERAGENT_OFFLINE_QUALIFICATION_TRACE": str(trace_path),
        "OFFERAGENT_OFFLINE_QUALIFICATION_TOKEN": token,
    }


def _offline_audit_report(trace_path: Path, token: str) -> dict[str, Any]:
    try:
        info = trace_path.lstat()
        payload = trace_path.read_bytes()
    except OSError as error:
        raise BuiltWindowsProductQualificationError("offline guard trace is unavailable") from error
    if trace_path.is_symlink() or info.st_nlink != 1 or not payload.endswith(b"\n"):
        raise BuiltWindowsProductQualificationError("offline guard trace identity differs")
    token_sha256 = f"sha256:{hashlib.sha256(token.encode()).hexdigest()}"
    records: list[dict[str, Any]] = []
    fields = {
        "decision",
        "event",
        "pipInvoked",
        "schemaVersion",
        "sequence",
        "startupDownloadAttempted",
        "systemPythonInvoked",
        "target",
        "tokenSha256",
    }
    for sequence, line in enumerate(payload.splitlines(keepends=True)):
        try:
            record = json.loads(line.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise BuiltWindowsProductQualificationError("offline guard trace is malformed") from error
        if (
            not isinstance(record, dict)
            or set(record) != fields
            or record.get("schemaVersion") != 1
            or record.get("sequence") != sequence
            or record.get("tokenSha256") != token_sha256
            or record.get("decision") not in {"allow", "deny"}
            or any(
                not isinstance(record.get(field), bool)
                for field in ("pipInvoked", "startupDownloadAttempted", "systemPythonInvoked")
            )
            or not isinstance(record.get("event"), str)
            or not isinstance(record.get("target"), str)
            or line != _canonical_json(record)
        ):
            raise BuiltWindowsProductQualificationError("offline guard trace record differs")
        records.append(record)
    if not records or records[0]["event"] != "guard.installed" or records[0]["decision"] != "allow":
        raise BuiltWindowsProductQualificationError("offline guard was not installed before Worker startup")
    denied = [record for record in records if record["decision"] == "deny"]
    if denied:
        raise BuiltWindowsProductQualificationError("offline guard captured a denied startup attempt")
    return {
        "auditEventCount": len(records),
        "auditTraceSha256": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "pipInvoked": any(record["pipInvoked"] for record in records),
        "startupDownloadAttempted": any(record["startupDownloadAttempted"] for record in records),
        "systemPythonInvoked": any(record["systemPythonInvoked"] for record in records),
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


def _alive_owned_processes(owned: dict[int, str]) -> list[str]:
    if not owned:
        return []
    script = (
        "$ErrorActionPreference='Stop'; "
        "$items = @(Get-CimInstance Win32_Process -ErrorAction Stop | "
        "Where-Object { [int]$_.ProcessId -gt 0 } | "
        "ForEach-Object { [pscustomobject]@{ name=[string]$_.Name; pid=[int]$_.ProcessId; "
        "parent=[int]$_.ParentProcessId } }); "
        "$items | ConvertTo-Json -Compress"
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
        raise BuiltWindowsProductQualificationError("qualification process leak audit failed") from error
    records = value if isinstance(value, list) else [value]
    return _owned_process_tree(records, owned)


def _owned_process_tree(records: Sequence[object], roots: dict[int, str]) -> list[str]:
    processes: dict[int, tuple[str, int]] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"name", "parent", "pid"}:
            raise BuiltWindowsProductQualificationError("qualification process leak audit shape differs")
        name = record["name"]
        process_id = record["pid"]
        parent_id = record["parent"]
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or process_id < 1
            or not isinstance(parent_id, int)
            or isinstance(parent_id, bool)
            or parent_id < 0
            or process_id in processes
        ):
            raise BuiltWindowsProductQualificationError("qualification process leak audit record differs")
        processes[process_id] = (name, parent_id)
    owned_ids = set(roots)
    while True:
        descendants = {
            process_id
            for process_id, (_name, parent_id) in processes.items()
            if parent_id in owned_ids and process_id not in owned_ids
        }
        if not descendants:
            break
        owned_ids.update(descendants)
    result: list[str] = []
    for process_id in sorted(owned_ids):
        process = processes.get(process_id)
        if process is None:
            continue
        name, _parent_id = process
        if process_id not in roots or name.casefold() == roots[process_id].casefold():
            result.append(f"{name}:{process_id}")
    return sorted(result)


if __name__ == "__main__":
    raise SystemExit(main())
