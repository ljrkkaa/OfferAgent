from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import qualify_built_windows_product

from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.qualification.windows_product_artifact import (
    WindowsProductArtifactError,
    verify_paired_windows_artifacts,
)
from offeragent_harness.runtime.development_runtime_manifest import (
    DevelopmentBuildIdentity,
    DevelopmentRuntimeManifest,
    canonical_development_manifest_bytes,
    development_runtime_content_digest,
)
from offeragent_harness.runtime.runtime_manifest import ProtocolCompatibility, RuntimeFileRecord


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _paired_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    plugin = tmp_path / "plugin"
    qualification = tmp_path / "qualification"
    runtime = plugin / "runtime" / "windows-x64" / "local-development"
    runtime.mkdir(parents=True)
    qualification.mkdir()
    runtime_payloads = {
        "offeragent-process-host.exe": b"MZ process",
        "offeragent-worker.exe": b"MZ worker",
        "process-catalog.v1.json": b"{}\n",
        "skills/core/SKILL.md": b"# Core\n",
        "tools/rg.exe": b"MZ ripgrep",
    }
    records = []
    for relative, payload in sorted(runtime_payloads.items()):
        target = runtime.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        records.append(
            RuntimeFileRecord(
                path=relative,
                byte_length=len(payload),
                sha256=_sha256(payload),
                kind=(
                    "executable"
                    if relative.endswith(".exe")
                    else ("skill" if relative.startswith("skills/") else "asset")
                ),
                authenticode=False,
            )
        )
    runtime_files = tuple(records)
    runtime_manifest = canonical_development_manifest_bytes(
        DevelopmentRuntimeManifest(
            runtime_version="0.1.0-test",
            core_version="0.1.0-test",
            plugin_version="2.0.0-test",
            build=DevelopmentBuildIdentity("c" * 40, "sha256:" + "a" * 64),
            protocol=ProtocolCompatibility(PROTOCOL_VERSION, PROTOCOL_VERSION, schema_hash()),
            state_schema_version=1,
            tool_abi_version="1",
            runtime_content_sha256=development_runtime_content_digest(runtime_files),
            files=runtime_files,
        )
    )
    (runtime / "development-runtime-manifest.json").write_bytes(runtime_manifest)
    for name, payload in {
        "main.js": b"OFFERAGENT_LOCAL_DEVELOPMENT_RUNTIME_V1\n" + _sha256(runtime_manifest).encode(),
        "manifest.json": b'{"isDesktopOnly":true,"version":"2.0.0-test"}\n',
        "styles.css": b"/* local */\n",
    }.items():
        (plugin / name).write_bytes(payload)
    migration = plugin / "migration" / "target-vault"
    (migration / "obsidian-cli").mkdir(parents=True)
    (migration / "agent.md").write_text("# Agent\n", encoding="utf-8")
    (migration / "obsidian-cli" / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    receipt = _canonical(
        {
            "developmentOnly": True,
            "manifestSha256": _sha256(runtime_manifest),
            "pluginVersion": "2.0.0-test",
            "runtimeVersion": "0.1.0-test",
            "schemaVersion": 1,
            "sourceTreeSha256": "sha256:" + "a" * 64,
            "targetVaultTemplateSha256": "sha256:5d30618e7c290489839b3ae6b1c050c1197e5574c36f1d2f5e619ca4255a4843",
        }
    )
    (plugin / "local-development-build.json").write_bytes(receipt)
    driver = b'"use strict";\nprocess.stdout.write("ready\\n");\n'
    (qualification / "offeragent-qualification-driver.cjs").write_bytes(driver)
    manifest = {
        "driver": {
            "path": "offeragent-qualification-driver.cjs",
            "sha256": _sha256(driver),
            "size": len(driver),
        },
        "pluginBuildReceiptSha256": _sha256(receipt),
        "pluginVersion": "2.0.0-test",
        "runtimeManifestSha256": _sha256(runtime_manifest),
        "runtimeVersion": "0.1.0-test",
        "schemaVersion": 1,
        "sourceCommit": "c" * 40,
        "sourceTreeSha256": "sha256:" + "a" * 64,
    }
    (qualification / "qualification-manifest.json").write_bytes(_canonical(manifest))
    return plugin, qualification


def test_verifier_accepts_only_a_sealed_driver_bound_to_the_plugin_receipt(tmp_path: Path) -> None:
    plugin, qualification = _paired_artifacts(tmp_path)

    verified = verify_paired_windows_artifacts(plugin, qualification)

    assert verified.source_commit == "c" * 40
    assert verified.source_tree_sha256 == "sha256:" + "a" * 64
    assert verified.driver == qualification / "offeragent-qualification-driver.cjs"
    assert verified.plugin_root == plugin

    verified.driver.write_bytes(b"tampered\n")
    with pytest.raises(WindowsProductArtifactError, match="driver differs"):
        verify_paired_windows_artifacts(plugin, qualification)


def test_verifier_rejects_plugin_layout_and_bundle_anchor_tampering(tmp_path: Path) -> None:
    plugin, qualification = _paired_artifacts(tmp_path)
    (plugin / "unexpected.txt").write_text("not installed\n", encoding="utf-8")

    with pytest.raises(WindowsProductArtifactError, match="plugin artifact file set is not exact"):
        verify_paired_windows_artifacts(plugin, qualification)

    (plugin / "unexpected.txt").unlink()
    (plugin / "main.js").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(WindowsProductArtifactError, match="bundle anchor"):
        verify_paired_windows_artifacts(plugin, qualification)


def test_verifier_rejects_tampered_migration_template_content(tmp_path: Path) -> None:
    plugin, qualification = _paired_artifacts(tmp_path)
    (plugin / "migration" / "target-vault" / "agent.md").write_text("# Tampered\n", encoding="utf-8")

    with pytest.raises(WindowsProductArtifactError, match="migration template digest"):
        verify_paired_windows_artifacts(plugin, qualification)


def test_probe_runs_only_the_sealed_driver_with_the_repository_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin, qualification = _paired_artifacts(tmp_path)
    source = tmp_path / "repository"
    source.mkdir()
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **options: object) -> object:
        calls.append((command, options))
        return type(
            "Completed",
            (),
            {
                "stdout": json.dumps(
                    {
                        "driverProtocolVersion": 1,
                        "productionExports": [
                            "StdioWorkerTransport",
                            "VaultToolAdapter",
                            "VaultChangeCoordinator",
                            "FileVaultChangeJournal",
                            "GitCheckpointStore",
                            "ResearchBrowserAdapter",
                        ],
                        "sourceFreeRuntime": True,
                    }
                )
                + "\n",
            },
        )()

    monkeypatch.setattr("scripts.qualify_built_windows_product.subprocess.run", run)

    report = qualify_built_windows_product.probe_built_windows_product(
        plugin,
        qualification,
        source_root_guard=source,
        node_executable=Path("C:/node/node.exe"),
    )

    assert report["sourceFreeRuntime"] is True
    command, options = calls[0]
    assert command == [
        "C:\\node\\node.exe",
        str(qualification / "offeragent-qualification-driver.cjs"),
        "probe",
    ]
    assert options["cwd"] == qualification
    environment = options["env"]
    assert isinstance(environment, dict)
    assert environment["OFFERAGENT_QUALIFICATION_FORBID_SOURCE_ROOT"] == str(source)
    assert environment["NODE_PATH"] == ""


def test_smoke_owns_the_temporary_vault_and_drives_frozen_worker_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin, qualification = _paired_artifacts(tmp_path)
    source = tmp_path / "repository"
    temporary_parent = tmp_path / "qualification-runs"
    source.mkdir()
    temporary_parent.mkdir()
    client_options: dict[str, object] = {}
    requests: list[tuple[str, dict[str, object]]] = []

    class Driver:
        process_id = 30392

        def __enter__(self) -> Driver:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def request(self, command: str, params: dict[str, object], **_options: object) -> dict[str, object]:
            requests.append((command, params))
            if command == "hello":
                return {"driverProtocolVersion": 2, "reviewResolution": "explicit", "sourceFreeRuntime": True}
            if command == "product/start":
                assert Path(str(params["workerExecutable"])) == (
                    plugin / "runtime" / "windows-x64" / "local-development" / "offeragent-worker.exe"
                )
                assert Path(str(params["vaultRoot"])).parent.parent == temporary_parent
                assert Path(str(params["localAppData"])).parent == Path(str(params["vaultRoot"])).parent
                assert re.fullmatch(
                    r"ws_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    str(params["workspaceId"]),
                )
                workspace_config = json.loads(
                    (Path(str(params["vaultRoot"])) / ".offeragent" / "workspace.json").read_text(encoding="utf-8")
                )
                assert workspace_config["portableWorkspaceId"] == params["workspaceId"]
                return {
                    "identity": {"workerPid": 4242, "transport": "stdio"},
                    "reviewResolution": "explicit",
                    "sourceFreeRuntime": True,
                }
            if command == "product/stop":
                return {"stopped": True, "workerPid": 4242}
            raise AssertionError(command)

    def client(**options: object) -> Driver:
        client_options.update(options)
        return Driver()

    monkeypatch.setattr("scripts.qualify_built_windows_product.QualificationDriverClient", client)
    monkeypatch.setattr(
        "scripts.qualify_built_windows_product._offline_process_observation",
        lambda _pid: {
            "allowedLoopbackSockets": [],
            "allowedSystemDescendants": [],
            "unexpectedSockets": [],
            "workerDescendants": [],
        },
    )
    monkeypatch.setattr("scripts.qualify_built_windows_product._alive_owned_processes", lambda _owned: [])
    monkeypatch.setattr(
        "scripts.qualify_built_windows_product._offline_audit_report",
        lambda _path, _token: {
            "auditEventCount": 1,
            "auditTraceSha256": "sha256:" + "d" * 64,
            "pipInvoked": False,
            "startupDownloadAttempted": False,
            "systemPythonInvoked": False,
        },
    )

    report = qualify_built_windows_product.smoke_built_windows_product(
        plugin,
        qualification,
        source_root_guard=source,
        node_executable=Path("C:/node/node.exe"),
        temporary_parent=temporary_parent,
    )

    assert report["workerPid"] == 4242
    assert report["temporaryRootRemoved"] is True
    assert report["leakedProcesses"] == []
    assert report["offlineStartup"] == {
        "auditEventCount": 1,
        "auditTraceSha256": "sha256:" + "d" * 64,
        "networkBoundary": "pre-import-python-audit-deny-plus-active-socket-audit",
        "allowedLoopbackSockets": [],
        "allowedSystemDescendants": [],
        "unexpectedSockets": [],
        "workerDescendants": [],
        "systemPythonInvoked": False,
        "pipInvoked": False,
        "startupDownloadAttempted": False,
    }
    assert list(temporary_parent.iterdir()) == []
    assert [command for command, _params in requests] == ["hello", "product/start", "product/stop"]
    environment = client_options["environment_overrides"]
    assert isinstance(environment, dict)
    assert environment["HTTP_PROXY"] == ""
    assert environment["HTTPS_PROXY"] == ""
    assert environment["ALL_PROXY"] == ""
    assert environment["NO_PROXY"] == ""
    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["UV_OFFLINE"] == "1"
    assert Path(environment["OFFERAGENT_OFFLINE_QUALIFICATION_TRACE"]).name == "offline-audit.jsonl"
    assert len(environment["OFFERAGENT_OFFLINE_QUALIFICATION_TOKEN"]) == 64


def test_offline_guard_continuously_denies_external_sockets_and_child_processes(tmp_path: Path) -> None:
    trace = (tmp_path / "offline-audit.jsonl").resolve()
    environment = os.environ.copy()
    environment["OFFERAGENT_OFFLINE_QUALIFICATION_TRACE"] = str(trace)
    environment["OFFERAGENT_OFFLINE_QUALIFICATION_TOKEN"] = "a" * 64
    program = """
import sys
import socket
sys.path.insert(0, "scripts/entrypoints/development")
from offline_qualification_bootstrap import install_offline_qualification_guard
install_offline_qualification_guard()
left, right = socket.socketpair()
left.close()
right.close()
attempts = [
    ("socket.bind", (None, ("127.0.0.1", 0))),
    ("socket.connect", (None, ("127.0.0.1", 9))),
    ("socket.sendto", (None, ("127.0.0.1", 9))),
    ("socket.connect", (None, ("203.0.113.10", 443))),
    ("subprocess.Popen", ("python.exe", ["python.exe", "-m", "pip"], None, None)),
]
for event, arguments in attempts:
    try:
        sys.audit(event, *arguments)
    except PermissionError:
        continue
    raise AssertionError(f"{event} was not denied")
"""

    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).parents[3],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    allowed = [record for record in records if record["decision"] == "allow"]
    denied = [record for record in records if record["decision"] == "deny"]
    assert allowed[0]["event"] == "guard.installed"
    assert any(
        record["event"] == "socket.connect" and record["target"] == "event-loop-socketpair" for record in allowed
    )
    assert [(record["event"], record["target"]) for record in denied] == [
        ("socket.bind", "loopback"),
        ("socket.connect", "loopback"),
        ("socket.sendto", "loopback"),
        ("socket.connect", "external-or-name"),
        ("subprocess.Popen", "child-process"),
    ]
    assert denied[0]["startupDownloadAttempted"] is False
    assert all(record["startupDownloadAttempted"] is True for record in denied[1:4])


def test_offline_smoke_failure_still_audits_process_leaks_and_removes_owned_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin, qualification = _paired_artifacts(tmp_path)
    source = tmp_path / "repository"
    runs = tmp_path / "qualification-runs"
    source.mkdir()
    runs.mkdir()

    class FailingDriver:
        process_id = 30392

        def __enter__(self) -> FailingDriver:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def request(self, _command: str, _params: dict[str, object], **_options: object) -> dict[str, object]:
            raise OSError("primary offline failure")

    monkeypatch.setattr(
        "scripts.qualify_built_windows_product.QualificationDriverClient", lambda **_kwargs: FailingDriver()
    )
    monkeypatch.setattr(
        "scripts.qualify_built_windows_product._alive_owned_processes",
        lambda _owned: ["offeragent-worker.exe:4242"],
    )

    with pytest.raises(qualify_built_windows_product.BuiltWindowsProductQualificationError) as captured:
        qualify_built_windows_product.smoke_built_windows_product(
            plugin,
            qualification,
            source_root_guard=source,
            node_executable=Path("C:/node/node.exe"),
            temporary_parent=runs,
        )

    assert "primary offline failure" in str(captured.value)
    assert "offeragent-worker.exe:4242" in str(captured.value)
    assert list(runs.iterdir()) == []


def test_offline_socket_audit_allows_only_the_worker_event_loop_pair() -> None:
    records = [
        {
            "protocol": "tcp",
            "pid": 4242,
            "state": "Bound",
            "localAddress": "0.0.0.0",
            "localPort": 50001,
            "remoteAddress": "0.0.0.0",
            "remotePort": 0,
        },
        {
            "protocol": "tcp",
            "pid": 4242,
            "state": "Established",
            "localAddress": "127.0.0.1",
            "localPort": 50000,
            "remoteAddress": "127.0.0.1",
            "remotePort": 50001,
        },
        {
            "protocol": "tcp",
            "pid": 4242,
            "state": "Established",
            "localAddress": "127.0.0.1",
            "localPort": 50001,
            "remoteAddress": "127.0.0.1",
            "remotePort": 50000,
        },
        {
            "protocol": "tcp",
            "pid": 4242,
            "state": "Established",
            "localAddress": "10.0.0.5",
            "localPort": 50002,
            "remoteAddress": "203.0.113.10",
            "remotePort": 443,
        },
    ]

    allowed, unexpected = qualify_built_windows_product._classify_offline_sockets(records)

    assert len(allowed) == 3
    assert len(unexpected) == 1
    assert "203.0.113.10" in unexpected[0]
