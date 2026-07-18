from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from scripts import install_local_windows_plugin as installer
from scripts.install_local_windows_plugin import install_local_plugin

from offeragent_harness import migration
from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.runtime.development_runtime_manifest import (
    DEVELOPMENT_MANIFEST_NAME,
    DevelopmentBuildIdentity,
    DevelopmentRuntimeManifest,
    canonical_development_manifest_bytes,
    development_runtime_content_digest,
)
from offeragent_harness.runtime.runtime_manifest import ProtocolCompatibility, RuntimeFileRecord


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _artifact(root: Path) -> Path:
    root.mkdir()
    runtime = root / "runtime" / "windows-x64" / "local-development"
    payloads = {
        "offeragent-process-host.exe": b"process",
        "offeragent-worker.exe": b"worker",
        "process-catalog.v1.json": b"{}\n",
        "skills/local/SKILL.md": b"# Local\n",
        "tools/rg.exe": b"ripgrep",
        "web/index.html": b"<!doctype html>\n",
    }
    records: list[RuntimeFileRecord] = []
    for relative, payload in sorted(payloads.items()):
        target = runtime.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        records.append(
            RuntimeFileRecord(
                relative,
                len(payload),
                _digest(payload),
                "executable" if relative.endswith(".exe") else ("skill" if relative.startswith("skills/") else "asset"),
                False,
            )
        )
    source_hash = _digest(b"source")
    files = tuple(records)
    manifest = DevelopmentRuntimeManifest(
        runtime_version="0.1.0-local.0123456789abcdef",
        core_version="0.1.0-local.0123456789abcdef",
        plugin_version="2.0.0-beta.28",
        build=DevelopmentBuildIdentity("a" * 40, source_hash),
        protocol=ProtocolCompatibility(PROTOCOL_VERSION, PROTOCOL_VERSION, schema_hash()),
        state_schema_version=1,
        tool_abi_version="1",
        runtime_content_sha256=development_runtime_content_digest(files),
        files=files,
    )
    manifest_bytes = canonical_development_manifest_bytes(manifest)
    (runtime / DEVELOPMENT_MANIFEST_NAME).write_bytes(manifest_bytes)
    manifest_hash = _digest(manifest_bytes)
    (root / "main.js").write_text(
        f"// OFFERAGENT_LOCAL_DEVELOPMENT_RUNTIME_V1 {manifest_hash}\n",
        encoding="utf-8",
    )
    (root / "styles.css").write_text("/* local */\n", encoding="utf-8")
    templates = root / "migration" / "target-vault"
    (templates / "obsidian-cli").mkdir(parents=True)
    (templates / "agent.md").write_text("# Agent\n", encoding="utf-8")
    (templates / "obsidian-cli" / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "offeragent",
                "isDesktopOnly": True,
                "name": "OfferAgent",
                "version": manifest.plugin_version,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    receipt = {
        "developmentOnly": True,
        "manifestSha256": manifest_hash,
        "pluginVersion": manifest.plugin_version,
        "runtimeVersion": manifest.runtime_version,
        "schemaVersion": 1,
        "sourceTreeSha256": source_hash,
        "targetVaultTemplateSha256": installer._target_vault_template_digest(templates),
    }
    (root / "local-development-build.json").write_bytes(_canonical(receipt))
    return root


def test_installer_atomically_preserves_opaque_data_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    artifact = _artifact(tmp_path / "artifact")
    vault = tmp_path / "Vault"
    target = vault / ".obsidian" / "plugins" / "offeragent-obsidian-plugin"
    target.mkdir(parents=True)
    (target / "main.js").write_text("old", encoding="utf-8")
    secret = b"opaque-settings-never-decoded"
    (target / "data.json").write_bytes(secret)

    installed = install_local_plugin(artifact, vault)

    assert installed == target
    assert (target / "data.json").read_bytes() == secret
    assert "OFFERAGENT_LOCAL_DEVELOPMENT_RUNTIME_V1" in (target / "main.js").read_text(encoding="utf-8")
    assert not tuple(target.parent.glob(".offeragent-obsidian-plugin.backup-*"))


def test_installer_preserves_legacy_vault_change_journal_for_first_start_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    artifact = _artifact(tmp_path / "artifact")
    vault = tmp_path / "Vault"
    target = vault / ".obsidian" / "plugins" / "offeragent-obsidian-plugin"
    legacy_journal = target / "vault-change-journal"
    legacy_journal.mkdir(parents=True)
    (target / "main.js").write_text("old", encoding="utf-8")
    record = b'{"version":1,"batchId":"batch_pending","state":"applying"}\n'
    (legacy_journal / "batch_pending.json").write_bytes(record)

    installed = install_local_plugin(artifact, vault)

    assert installed == target
    assert (target / "vault-change-journal" / "batch_pending.json").read_bytes() == record
    assert not tuple(target.parent.glob(".offeragent-obsidian-plugin.backup-*"))


def test_installer_rolls_back_old_plugin_and_data_on_post_activation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    artifact = _artifact(tmp_path / "artifact")
    vault = tmp_path / "Vault"
    target = vault / ".obsidian" / "plugins" / "offeragent-obsidian-plugin"
    target.mkdir(parents=True)
    (target / "main.js").write_text("old", encoding="utf-8")
    secret = b"opaque-settings"
    (target / "data.json").write_bytes(secret)

    from scripts import install_local_windows_plugin as module

    original = module._verify_artifact
    calls = 0

    def fail_after_activation(root: Path, *, allow_data_json: bool = False) -> None:
        nonlocal calls
        calls += 1
        original(root, allow_data_json=allow_data_json)
        if calls == 3:
            raise RuntimeError("injected post-activation failure")

    monkeypatch.setattr(module, "_verify_artifact", fail_after_activation)
    with pytest.raises(RuntimeError, match="injected"):
        install_local_plugin(artifact, vault)

    assert (target / "main.js").read_text(encoding="utf-8") == "old"
    assert (target / "data.json").read_bytes() == secret


def test_legacy_migration_failure_is_inside_plugin_activation_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    artifact = _artifact(tmp_path / "artifact")
    vault = tmp_path / "Vault"
    target = vault / ".obsidian" / "plugins" / "offeragent-obsidian-plugin"
    target.mkdir(parents=True)
    (target / "main.js").write_text("old", encoding="utf-8")
    (target / "data.json").write_bytes(b"opaque-old-settings")

    from scripts import install_local_windows_plugin as module

    def fail_migration(installed: Path, root: Path) -> None:
        assert installed == target
        assert root == vault.resolve()
        assert "OFFERAGENT_LOCAL_DEVELOPMENT_RUNTIME_V1" in (installed / "main.js").read_text(encoding="utf-8")
        raise RuntimeError("injected legacy migration failure")

    monkeypatch.setattr(module, "_migrate_known_legacy_install", fail_migration)
    with pytest.raises(RuntimeError, match="legacy migration"):
        install_local_plugin(artifact, vault)

    assert (target / "main.js").read_text(encoding="utf-8") == "old"
    assert (target / "data.json").read_bytes() == b"opaque-old-settings"


def test_standard_legacy_install_is_discovered_as_one_explicit_migration_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = tmp_path / "Local"
    source_state = local / "OfferAgent" / "state.db"
    source_state.parent.mkdir(parents=True)
    source_state.write_bytes(b"legacy-snapshot")
    vault = tmp_path / "Vault"
    legacy_data = vault / ".obsidian" / "plugins" / "offeragent" / "data.json"
    legacy_data.parent.mkdir(parents=True)
    legacy_data.write_text('{"vaultPermissionMode":"read_only"}', encoding="utf-8")
    target = vault / ".obsidian" / "plugins" / "offeragent-obsidian-plugin"
    (target / "migration" / "target-vault").mkdir(parents=True)
    captured: list[migration.LegacyMigrationRequest] = []
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(migration, "migrate_legacy_obsidian", lambda request: captured.append(request))

    installer._migrate_known_legacy_install(target, vault)

    assert len(captured) == 1
    request = captured[0]
    assert request.source_state == source_state
    assert request.source_plugin_data == legacy_data
    assert request.target_plugin_data == target / "data.json"
    assert request.target_templates == target / "migration" / "target-vault"
    assert request.workspace_id.startswith("ws_")
    assert request.target_state_directory.parent == local / "OfferAgent" / "workspaces"
    assert request.source_attachments.parent == local / "OfferAgent" / "attachments"


def test_installer_rejects_data_json_inside_build_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    artifact = _artifact(tmp_path / "artifact")
    (artifact / "data.json").write_bytes(b"must-not-enter-build")
    vault = tmp_path / "Vault"
    vault.mkdir()

    with pytest.raises(RuntimeError, match=r"must not contain data\.json"):
        install_local_plugin(artifact, vault)


def test_installer_rejects_target_vault_template_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    artifact = _artifact(tmp_path / "artifact")
    (artifact / "migration" / "target-vault" / "agent.md").write_text("# Replaced after build\n", encoding="utf-8")
    vault = tmp_path / "Vault"
    vault.mkdir()

    with pytest.raises(RuntimeError, match="templates differ"):
        install_local_plugin(artifact, vault)


@pytest.mark.parametrize(
    "unexpected_relative",
    (
        "runtime/windows-arm64/local-development/retired.exe",
        "runtime/windows-x64/setup/retired.exe",
        "runtime/retired-runtime/retired.exe",
    ),
)
def test_installer_rejects_every_runtime_sibling_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unexpected_relative: str,
) -> None:
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    artifact = _artifact(tmp_path / "artifact")
    unexpected = artifact.joinpath(*unexpected_relative.split("/"))
    unexpected.parent.mkdir(parents=True)
    unexpected.write_bytes(b"retired")
    vault = tmp_path / "Vault"
    vault.mkdir()

    with pytest.raises(RuntimeError, match="Runtime layout is not exact"):
        install_local_plugin(artifact, vault)


@pytest.mark.parametrize("failed_replace_call", range(1, 7))
def test_every_activation_and_rollback_move_failure_preserves_the_unique_opaque_data_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_replace_call: int,
) -> None:
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    artifact = _artifact(tmp_path / "artifact")
    vault = tmp_path / "Vault"
    target = vault / ".obsidian" / "plugins" / "offeragent-obsidian-plugin"
    target.mkdir(parents=True)
    (target / "main.js").write_text("old", encoding="utf-8")
    secret = b"opaque-settings-preserved-after-rollback-fault"
    original_settings = target / "data.json"
    original_settings.write_bytes(secret)
    original_identity = (original_settings.stat().st_dev, original_settings.stat().st_ino)

    from scripts import install_local_windows_plugin as module

    original_verify = module._verify_artifact
    verify_calls = 0

    def fail_after_activation(root: Path, *, allow_data_json: bool = False) -> None:
        nonlocal verify_calls
        verify_calls += 1
        original_verify(root, allow_data_json=allow_data_json)
        if verify_calls == 3:
            raise RuntimeError("injected post-activation failure")

    original_replace = module._replace
    replace_calls = 0

    def fail_selected_move(source: Path, destination: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == failed_replace_call:
            raise OSError("injected install/rollback move failure")
        original_replace(source, destination)

    monkeypatch.setattr(module, "_verify_artifact", fail_after_activation)
    monkeypatch.setattr(module, "_replace", fail_selected_move)
    with pytest.raises(Exception) as captured:
        install_local_plugin(artifact, vault)
    if failed_replace_call >= 4:
        assert "preserved data.json recovery location" in str(captured.value)

    candidates = tuple(target.parent.glob("*"))
    preserved = tuple(path / "data.json" for path in candidates if (path / "data.json").exists())
    assert len(preserved) == 1
    assert (preserved[0].stat().st_dev, preserved[0].stat().st_ino) == original_identity
    assert preserved[0].read_bytes() == secret


def test_installer_rejects_existing_hard_linked_data_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    artifact = _artifact(tmp_path / "artifact")
    vault = tmp_path / "Vault"
    target = vault / ".obsidian" / "plugins" / "offeragent-obsidian-plugin"
    target.mkdir(parents=True)
    settings = target / "data.json"
    secret = b"hard-linked-opaque-settings"
    settings.write_bytes(secret)
    alias = tmp_path / "settings-hardlink"
    alias.hardlink_to(settings)

    with pytest.raises(RuntimeError, match="non-regular file"):
        install_local_plugin(artifact, vault)

    assert settings.read_bytes() == secret
    assert alias.read_bytes() == secret
    assert settings.stat().st_nlink == 2


def test_installer_rejects_a_reparse_component_in_the_vault_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    artifact = _artifact(tmp_path / "artifact")
    real_vault = tmp_path / "RealVault"
    real_vault.mkdir()
    linked_vault = tmp_path / "LinkedVault"
    try:
        linked_vault.symlink_to(real_vault, target_is_directory=True)
    except OSError:
        pytest.skip("current Windows account cannot create a directory symlink")

    with pytest.raises(RuntimeError, match="path component"):
        install_local_plugin(artifact, linked_vault)

    assert not (real_vault / ".obsidian").exists()


def test_installer_rejects_a_bundle_without_the_receipted_manifest_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    artifact = _artifact(tmp_path / "artifact")
    bundle = artifact / "main.js"
    bundle.write_text(
        f"// OFFERAGENT_LOCAL_DEVELOPMENT_RUNTIME_V1 sha256:{'0' * 64}\n",
        encoding="utf-8",
    )
    vault = tmp_path / "Vault"
    vault.mkdir()

    with pytest.raises(RuntimeError, match="manifest anchor"):
        install_local_plugin(artifact, vault)
