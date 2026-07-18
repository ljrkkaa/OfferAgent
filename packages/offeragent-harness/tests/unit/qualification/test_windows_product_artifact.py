from __future__ import annotations

import hashlib
import json
import re
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
            "targetVaultTemplateSha256": "sha256:" + "b" * 64,
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
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **options: object) -> object:
        calls.append((command, options))
        payload = json.loads(str(options["input"]))
        assert Path(payload["workerExecutable"]) == (
            plugin / "runtime" / "windows-x64" / "local-development" / "offeragent-worker.exe"
        )
        assert Path(payload["vaultRoot"]).parent.parent == temporary_parent
        assert Path(payload["localAppData"]).parent == Path(payload["vaultRoot"]).parent
        assert re.fullmatch(
            r"ws_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            payload["workspaceId"],
        )
        workspace_config = json.loads(
            (Path(payload["vaultRoot"]) / ".offeragent" / "workspace.json").read_text(encoding="utf-8")
        )
        assert workspace_config["portableWorkspaceId"] == payload["workspaceId"]
        return type(
            "Completed",
            (),
            {
                "stdout": json.dumps(
                    {
                        "driverProtocolVersion": 1,
                        "sourceFreeRuntime": True,
                        "transport": "stdio",
                        "workerPid": 4242,
                    }
                )
                + "\n",
            },
        )()

    monkeypatch.setattr("scripts.qualify_built_windows_product.subprocess.run", run)
    monkeypatch.setattr(
        "scripts.qualify_built_windows_product._offeragent_process_ids",
        lambda: {"offeragent-process-host.exe": set(), "offeragent-worker.exe": set()},
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
    assert list(temporary_parent.iterdir()) == []
    assert calls[0][0][-1] == "smoke"
