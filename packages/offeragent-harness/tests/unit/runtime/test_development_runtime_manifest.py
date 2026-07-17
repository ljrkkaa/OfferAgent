from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.runtime.development_runtime_manifest import (
    DEVELOPMENT_MANIFEST_NAME,
    DevelopmentBuildIdentity,
    DevelopmentRuntimeError,
    DevelopmentRuntimeManifest,
    InstalledDevelopmentRuntimeTrust,
    canonical_development_manifest_bytes,
    development_runtime_content_digest,
    parse_development_manifest,
)
from offeragent_harness.runtime.runtime_manifest import ProtocolCompatibility, RuntimeFileRecord


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _tree(tmp_path: Path) -> tuple[Path, DevelopmentRuntimeManifest]:
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
        target = tmp_path.joinpath(*relative.split("/"))
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
    files = tuple(records)
    manifest = DevelopmentRuntimeManifest(
        runtime_version="0.1.0-local.0123456789abcdef",
        core_version="0.1.0-local.0123456789abcdef",
        plugin_version="2.0.0-beta.28",
        build=DevelopmentBuildIdentity("a" * 40, _digest(b"source")),
        protocol=ProtocolCompatibility(PROTOCOL_VERSION, PROTOCOL_VERSION, schema_hash()),
        state_schema_version=1,
        tool_abi_version="1",
        runtime_content_sha256=development_runtime_content_digest(files),
        files=files,
    )
    (tmp_path / DEVELOPMENT_MANIFEST_NAME).write_bytes(canonical_development_manifest_bytes(manifest))
    return tmp_path, manifest


def test_canonical_development_manifest_round_trip(tmp_path: Path) -> None:
    _, manifest = _tree(tmp_path)
    payload = canonical_development_manifest_bytes(manifest)
    parsed = parse_development_manifest(payload)

    assert parsed == manifest
    assert b'"developmentOnly":true' in payload
    assert b"signingKeyId" not in payload
    assert b"authenticode" not in payload


def test_installed_development_trust_requires_exact_hash_pinned_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = _tree(tmp_path)
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )

    trust = InstalledDevelopmentRuntimeTrust(root)
    assert trust.manifest == manifest
    assert trust.version_directory == root
    assert trust.verify_file(root / "offeragent-worker.exe")

    (root / "offeragent-worker.exe").write_bytes(b"tampered")
    assert not trust.verify_file(root / "offeragent-worker.exe")
    with pytest.raises(DevelopmentRuntimeError, match="pinned manifest identity"):
        InstalledDevelopmentRuntimeTrust(root)


def test_development_trust_rejects_extra_file_and_noncanonical_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = _tree(tmp_path)
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    (root / "extra.dll").write_bytes(b"extra")
    with pytest.raises(DevelopmentRuntimeError, match="file set"):
        InstalledDevelopmentRuntimeTrust(root)

    payload = canonical_development_manifest_bytes(manifest)
    with pytest.raises(DevelopmentRuntimeError, match="canonical"):
        parse_development_manifest(payload.replace(b'"coreVersion"', b' "coreVersion"', 1))


def test_development_manifest_cannot_drop_development_only_marker(tmp_path: Path) -> None:
    _, manifest = _tree(tmp_path)
    payload = canonical_development_manifest_bytes(manifest).replace(
        b'"developmentOnly":true',
        b'"developmentOnly":false',
    )
    with pytest.raises(DevelopmentRuntimeError, match="development-only"):
        parse_development_manifest(payload)


def test_development_manifest_requires_runtime_ripgrep(tmp_path: Path) -> None:
    _, manifest = _tree(tmp_path)
    files = tuple(record for record in manifest.files if record.path != "tools/rg.exe")

    with pytest.raises(DevelopmentRuntimeError, match="ripgrep executables are required"):
        DevelopmentRuntimeManifest(
            runtime_version=manifest.runtime_version,
            core_version=manifest.core_version,
            plugin_version=manifest.plugin_version,
            build=manifest.build,
            protocol=manifest.protocol,
            state_schema_version=manifest.state_schema_version,
            tool_abi_version=manifest.tool_abi_version,
            runtime_content_sha256=development_runtime_content_digest(files),
            files=files,
        )


def test_established_development_trust_rejects_manifest_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = _tree(tmp_path)
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    trust = InstalledDevelopmentRuntimeTrust(root)
    replacement = DevelopmentRuntimeManifest(
        runtime_version=manifest.runtime_version,
        core_version=manifest.core_version,
        plugin_version=manifest.plugin_version,
        build=DevelopmentBuildIdentity("b" * 40, manifest.build.source_tree_sha256),
        protocol=manifest.protocol,
        state_schema_version=manifest.state_schema_version,
        tool_abi_version=manifest.tool_abi_version,
        runtime_content_sha256=manifest.runtime_content_sha256,
        files=manifest.files,
    )
    (root / DEVELOPMENT_MANIFEST_NAME).write_bytes(canonical_development_manifest_bytes(replacement))

    assert not trust.verify_file(root / "offeragent-worker.exe")
