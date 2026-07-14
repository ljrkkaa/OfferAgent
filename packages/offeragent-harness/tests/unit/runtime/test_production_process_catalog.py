from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from offeragent_harness.ports import ProcessStdinMode
from offeragent_harness.runtime.production_process_catalog import (
    PROCESS_CATALOG_PATH,
    ProductionProcessCatalogError,
    load_production_process_catalog,
)
from offeragent_harness.runtime.release_manifest import (
    BootstrapRecord,
    ProtocolCompatibility,
    ReleaseKeyring,
    RuntimeArchive,
    RuntimeFileRecord,
    RuntimePlatform,
    RuntimeReleaseManifest,
    canonical_manifest_bytes,
    encode_signature,
    runtime_content_digest,
)
from offeragent_harness.runtime.release_trust import InstalledReleaseManifestTrust

NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[3]
CATALOG = (ROOT / "packaging" / "process-catalog.v1.json").read_bytes()


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _signed_runtime(
    tmp_path: Path,
    *,
    catalog_payload: bytes = CATALOG,
    process_host_payload: bytes = b"signed process host\n",
) -> tuple[Path, InstalledReleaseManifestTrust]:
    version = "2.0.0"
    runtime = tmp_path / "runtime" / version
    runtime.mkdir(parents=True)
    payloads = {
        "LICENSES/AGPL.txt": b"license\n",
        "offeragent-host.exe": b"host\n",
        "offeragent-process-host.exe": process_host_payload,
        "offeragent-self-test.exe": b"self-test\n",
        "offeragent-worker.exe": b"worker\n",
        PROCESS_CATALOG_PATH: catalog_payload,
        "provenance/slsa.json": b"{}\n",
        "sbom/runtime.spdx.json": b"{}\n",
        "web/index.html": b"<!doctype html>\n",
    }
    kinds = {
        "LICENSES/AGPL.txt": "license",
        "offeragent-host.exe": "executable",
        "offeragent-process-host.exe": "executable",
        "offeragent-self-test.exe": "executable",
        "offeragent-worker.exe": "executable",
        PROCESS_CATALOG_PATH: "asset",
        "provenance/slsa.json": "provenance",
        "sbom/runtime.spdx.json": "sbom",
        "web/index.html": "web",
    }
    for relative, payload in payloads.items():
        path = runtime.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    records = tuple(
        RuntimeFileRecord(
            relative,
            len(payload),
            _digest(payload),
            kinds[relative],
            relative.casefold().endswith(".exe"),
        )
        for relative, payload in sorted(payloads.items())
    )
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    manifest = RuntimeReleaseManifest(
        version,
        version,
        "2.0.0",
        "2.9.9",
        "release-test",
        "a" * 40,
        NOW,
        RuntimePlatform("windows", "x64", 19_045),
        ProtocolCompatibility("1.0", "1.0", "sha256:" + "b" * 64),
        1,
        "1",
        RuntimeArchive("offeragent-runtime.zip", runtime_content_digest(records), 1024 * 1024),
        BootstrapRecord("offeragent-bootstrap.exe", 1, _digest(b"b"), True),
        records,
        ("offline_install", "process_catalog_v1"),
    )
    manifest_bytes = canonical_manifest_bytes(manifest)
    (runtime / "runtime-manifest.json").write_bytes(manifest_bytes)
    (runtime / "runtime-manifest.sig").write_bytes(encode_signature(private_key.sign(manifest_bytes)))
    (runtime.parent / "current.json").write_bytes(
        _canonical({"currentVersion": version, "manifestHash": _digest(manifest_bytes)})
    )
    trust = InstalledReleaseManifestTrust(runtime, keyring=ReleaseKeyring({"release-test": public_key}))
    return runtime, trust


def test_signed_catalog_builds_two_zero_network_profiles_and_bound_shell(tmp_path: Path) -> None:
    runtime, trust = _signed_runtime(tmp_path)

    catalog = load_production_process_catalog(runtime, manifest_trust=trust)

    assert [item.executable_id for item in catalog.executable_profiles] == [
        "hook-continue",
        "shell-runtime-info",
    ]
    assert [item.profile_id for item in catalog.environment_profiles] == ["minimal"]
    assert [item.profile_id for item in catalog.signed_shell_profiles] == ["runtime-info"]
    assert all(not item.allow_network for item in catalog.executable_profiles)
    assert all(not item.allow_network for item in catalog.signed_shell_profiles)
    assert catalog.executable_profiles[0].allowed_stdin_modes == frozenset({ProcessStdinMode.FIXED_PAYLOAD})
    assert catalog.executable_profiles[1].allowed_stdin_modes == frozenset({ProcessStdinMode.CLOSED})
    assert all(
        item.appcontainer_filesystem[0].root_id == "process-scratch"
        and item.appcontainer_filesystem[0].relative_path == "working"
        for item in catalog.executable_profiles
    )
    shell = catalog.signed_shell_profiles[0]
    executable = catalog.executable_profiles[1]
    manifest_trust = catalog.manifest_trust
    assert manifest_trust is not None
    assert shell.executable_profile_fingerprint == executable.fingerprint
    assert manifest_trust.authorizes(executable)
    assert catalog.catalog_hash == _digest(CATALOG)


def test_catalog_asset_tamper_fails_against_signed_size_and_hash(tmp_path: Path) -> None:
    runtime, trust = _signed_runtime(tmp_path)
    (runtime / PROCESS_CATALOG_PATH).write_bytes(CATALOG + b" ")

    with pytest.raises(ProductionProcessCatalogError, match=r"manifest|identity|catalog"):
        load_production_process_catalog(runtime, manifest_trust=trust)


def test_catalog_rejects_duplicate_keys_even_when_manifest_signs_bytes(tmp_path: Path) -> None:
    duplicated = CATALOG.replace(b'"schemaVersion":1', b'"schemaVersion":1,"schemaVersion":1', 1)
    runtime, trust = _signed_runtime(tmp_path, catalog_payload=duplicated)

    with pytest.raises(ProductionProcessCatalogError) as captured:
        load_production_process_catalog(runtime, manifest_trust=trust)

    assert captured.value.code == "process_catalog_duplicate_key"


def test_catalog_rejects_noncanonical_json_even_when_manifest_signs_bytes(tmp_path: Path) -> None:
    noncanonical = json.dumps(json.loads(CATALOG), ensure_ascii=False, indent=2).encode() + b"\n"
    runtime, trust = _signed_runtime(tmp_path, catalog_payload=noncanonical)

    with pytest.raises(ProductionProcessCatalogError) as captured:
        load_production_process_catalog(runtime, manifest_trust=trust)

    assert captured.value.code == "process_catalog_noncanonical"


@pytest.mark.parametrize("section", ["executableProfiles", "signedShellProfiles"])
def test_catalog_rejects_every_local_network_grant(tmp_path: Path, section: str) -> None:
    value = json.loads(CATALOG)
    value[section][0]["allowNetwork"] = True
    runtime, trust = _signed_runtime(tmp_path, catalog_payload=_canonical(value))

    with pytest.raises(ProductionProcessCatalogError) as captured:
        load_production_process_catalog(runtime, manifest_trust=trust)

    assert captured.value.code == "process_catalog_network"


def test_catalog_rejects_executable_content_drift(tmp_path: Path) -> None:
    runtime, trust = _signed_runtime(tmp_path)
    process_host = runtime / "offeragent-process-host.exe"
    process_host.write_bytes(b"X" * process_host.stat().st_size)

    with pytest.raises(ProductionProcessCatalogError):
        load_production_process_catalog(runtime, manifest_trust=trust)


def test_process_manifest_adapter_rejects_profile_identity_hash_path_and_network_drift(tmp_path: Path) -> None:
    runtime, trust = _signed_runtime(tmp_path)
    catalog = load_production_process_catalog(runtime, manifest_trust=trust)
    profile = catalog.executable_profiles[1]
    manifest_trust = catalog.manifest_trust
    assert manifest_trust is not None

    assert not manifest_trust.authorizes(replace(profile, executable_id="other-runtime-info"))
    assert not manifest_trust.authorizes(replace(profile, file_sha256="sha256:" + "0" * 64))
    assert not manifest_trust.authorizes(replace(profile, allow_network=True))
    worker = runtime / "offeragent-worker.exe"
    assert not manifest_trust.authorizes(replace(profile, executable=worker, file_sha256=_digest(worker.read_bytes())))


def test_process_manifest_adapter_rejects_hard_linked_executable(tmp_path: Path) -> None:
    runtime, trust = _signed_runtime(tmp_path)
    process_host = runtime / "offeragent-process-host.exe"
    try:
        os.link(process_host, tmp_path / "second-process-host.exe")
    except OSError as error:
        pytest.skip(f"test filesystem cannot create a hard link: {error}")

    with pytest.raises(ProductionProcessCatalogError):
        load_production_process_catalog(runtime, manifest_trust=trust)


def test_catalog_asset_symlink_is_rejected_even_when_bytes_match(tmp_path: Path) -> None:
    runtime, trust = _signed_runtime(tmp_path)
    catalog_path = runtime / PROCESS_CATALOG_PATH
    outside = tmp_path / "outside-process-catalog.json"
    outside.write_bytes(catalog_path.read_bytes())
    catalog_path.unlink()
    try:
        os.symlink(outside, catalog_path)
    except OSError as error:
        pytest.skip(f"test account cannot create a file symlink: {error}")

    with pytest.raises(ProductionProcessCatalogError):
        load_production_process_catalog(runtime, manifest_trust=trust)
