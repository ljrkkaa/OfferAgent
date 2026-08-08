from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from offeragent_harness.ports import ProcessStdinMode
from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.runtime.development_runtime_manifest import (
    DevelopmentBuildIdentity,
    DevelopmentRuntimeManifest,
    InstalledDevelopmentRuntimeTrust,
    canonical_development_manifest_bytes,
    development_runtime_content_digest,
)
from offeragent_harness.runtime.local_process_catalog import (
    PROCESS_CATALOG_PATH,
    LocalProcessCatalogError,
    load_local_process_catalog,
)
from offeragent_harness.runtime.runtime_manifest import ProtocolCompatibility, RuntimeFileRecord

ROOT = Path(__file__).resolve().parents[3]
CATALOG = (ROOT / "packaging" / "process-catalog.v1.json").read_bytes()


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _local_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    catalog_payload: bytes = CATALOG,
) -> tuple[Path, InstalledDevelopmentRuntimeTrust]:
    runtime = tmp_path / "runtime"
    payloads = {
        "offeragent-process-host.exe": b"process host\n",
        "offeragent-worker.exe": b"worker\n",
        "tools/rg.exe": b"ripgrep\n",
        PROCESS_CATALOG_PATH: catalog_payload,
        "skills/core/SKILL.md": b"# Core\n",
    }
    kinds = {
        "offeragent-process-host.exe": "executable",
        "offeragent-worker.exe": "executable",
        "tools/rg.exe": "executable",
        PROCESS_CATALOG_PATH: "asset",
        "skills/core/SKILL.md": "skill",
    }
    runtime.mkdir()
    for relative, payload in payloads.items():
        path = runtime.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    records = tuple(
        RuntimeFileRecord(relative, len(payload), _digest(payload), kinds[relative])
        for relative, payload in sorted(payloads.items())
    )
    manifest = DevelopmentRuntimeManifest(
        runtime_version="local-test",
        core_version="0.1.0",
        plugin_version="0.1.0",
        build=DevelopmentBuildIdentity("a" * 40, "sha256:" + "b" * 64),
        protocol=ProtocolCompatibility(PROTOCOL_VERSION, PROTOCOL_VERSION, schema_hash()),
        state_schema_version=1,
        tool_abi_version="1",
        runtime_content_sha256=development_runtime_content_digest(records),
        files=records,
    )
    (runtime / "development-runtime-manifest.json").write_bytes(canonical_development_manifest_bytes(manifest))
    monkeypatch.setattr(
        "offeragent_harness.runtime.development_runtime_manifest.native_windows_architecture",
        lambda: "x64",
    )
    return runtime, InstalledDevelopmentRuntimeTrust(runtime)


def test_local_catalog_runs_only_the_bundled_document_parser_as_the_current_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, trust = _local_runtime(tmp_path, monkeypatch)

    catalog = load_local_process_catalog(runtime, manifest_trust=trust)

    assert [item.executable_id for item in catalog.executable_profiles] == [
        "document-extract",
        "hook-continue",
        "shell-runtime-info",
    ]
    assert [item.profile_id for item in catalog.environment_profiles] == ["minimal"]
    assert [item.profile_id for item in catalog.shell_profiles] == ["runtime-info"]
    assert catalog.executable_profiles[0].allow_network is True
    assert all(not item.allow_network for item in catalog.executable_profiles[1:])
    assert all(not item.allow_network for item in catalog.shell_profiles)
    assert catalog.executable_profiles[0].allowed_stdin_modes == frozenset({ProcessStdinMode.FIXED_PAYLOAD})
    assert catalog.executable_profiles[0].fixed_arguments == ("document-extract",)
    assert catalog.executable_profiles[0].maximum_variable_arguments == 0
    assert catalog.executable_profiles[0].allowed_cwd_roots == frozenset({"process-scratch"})
    assert catalog.executable_profiles[0].appcontainer_filesystem == ()
    assert catalog.executable_profiles[1].allowed_stdin_modes == frozenset({ProcessStdinMode.FIXED_PAYLOAD})
    assert catalog.executable_profiles[2].allowed_stdin_modes == frozenset({ProcessStdinMode.CLOSED})
    assert catalog.shell_profiles[0].executable_profile_fingerprint == catalog.executable_profiles[2].fingerprint
    assert catalog.catalog_hash == _digest(CATALOG)


def test_catalog_asset_tamper_fails_against_pinned_size_and_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, trust = _local_runtime(tmp_path, monkeypatch)
    (runtime / PROCESS_CATALOG_PATH).write_bytes(CATALOG + b" ")

    with pytest.raises(LocalProcessCatalogError, match=r"manifest|identity|catalog"):
        load_local_process_catalog(runtime, manifest_trust=trust)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda payload: payload.replace(b'"schemaVersion":1', b'"schemaVersion":1,"schemaVersion":1', 1),
            "process_catalog_duplicate_key",
        ),
        (
            lambda payload: json.dumps(json.loads(payload), ensure_ascii=False, indent=2).encode() + b"\n",
            "process_catalog_noncanonical",
        ),
    ],
)
def test_catalog_rejects_noncanonical_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: object,
    code: str,
) -> None:
    assert callable(mutation)
    runtime, trust = _local_runtime(tmp_path, monkeypatch, catalog_payload=mutation(CATALOG))

    with pytest.raises(LocalProcessCatalogError) as captured:
        load_local_process_catalog(runtime, manifest_trust=trust)

    assert captured.value.code == code


@pytest.mark.parametrize(("section", "index"), [("executableProfiles", 1), ("shellProfiles", 0)])
def test_catalog_rejects_network_grants_outside_the_bundled_document_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    index: int,
) -> None:
    value = json.loads(CATALOG)
    value[section][index]["allowNetwork"] = True
    runtime, trust = _local_runtime(tmp_path, monkeypatch, catalog_payload=_canonical(value))

    with pytest.raises(LocalProcessCatalogError) as captured:
        load_local_process_catalog(runtime, manifest_trust=trust)

    assert captured.value.code == "process_catalog_network"


def test_current_user_document_profile_cannot_retain_appcontainer_grants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = json.loads(CATALOG)
    value["executableProfiles"][0]["appContainerFilesystem"] = [
        {"access": "read_write", "relativePath": "working", "rootId": "process-scratch"}
    ]
    runtime, trust = _local_runtime(tmp_path, monkeypatch, catalog_payload=_canonical(value))

    with pytest.raises(LocalProcessCatalogError) as captured:
        load_local_process_catalog(runtime, manifest_trust=trust)

    assert captured.value.code == "process_catalog_isolation"


def test_bundled_document_parser_cannot_be_changed_back_to_appcontainer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = json.loads(CATALOG)
    profile = value["executableProfiles"][0]
    profile["allowNetwork"] = False
    profile["appContainerFilesystem"] = [
        {"access": "read_write", "relativePath": "working", "rootId": "process-scratch"}
    ]
    runtime, trust = _local_runtime(tmp_path, monkeypatch, catalog_payload=_canonical(value))

    with pytest.raises(LocalProcessCatalogError) as captured:
        load_local_process_catalog(runtime, manifest_trust=trust)

    assert captured.value.code == "process_catalog_isolation"


def test_process_executable_hard_link_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, trust = _local_runtime(tmp_path, monkeypatch)
    try:
        os.link(runtime / "offeragent-process-host.exe", tmp_path / "second-process-host.exe")
    except OSError as error:
        pytest.skip(f"test filesystem cannot create a hard link: {error}")

    with pytest.raises(LocalProcessCatalogError):
        load_local_process_catalog(runtime, manifest_trust=trust)
