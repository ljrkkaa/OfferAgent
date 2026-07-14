from __future__ import annotations

import hashlib
import stat
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from offeragent_harness.runtime.release_manifest import (
    BootstrapRecord,
    ProtocolCompatibility,
    ReleaseKeyring,
    ReleaseVerificationError,
    RuntimeArchive,
    RuntimeBundleVerifier,
    RuntimeFileRecord,
    RuntimePlatform,
    RuntimeReleaseManifest,
    SafeRuntimeZipExtractor,
    VerifiedRuntimeBundle,
    architecture_for_windows_pe_machine,
    canonical_manifest_bytes,
    encode_signature,
    runtime_content_digest,
)
from offeragent_harness.runtime.release_privileges import build_privilege_envelope_from_process_catalog

_PROCESS_CATALOG = (Path(__file__).resolve().parents[3] / "packaging" / "process-catalog.v1.json").read_bytes()


class _Authenticode:
    def __init__(self, *, trusted: bool = True) -> None:
        self.trusted = trusted

    def verify(self, executable: Path) -> bool:
        return self.trusted and executable.suffix.casefold() == ".exe"


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _pe(machine: int = 0x8664) -> bytes:
    header = bytearray(64)
    header[:2] = b"MZ"
    header[60:64] = (64).to_bytes(4, "little")
    return bytes(header) + b"PE\0\0" + machine.to_bytes(2, "little") + b"\0" * 18


def _create_bundle(
    root: Path,
    *,
    malicious_name: str | None = None,
    symlink_path: str | None = None,
    architecture: str = "x64",
    executable_machine: int | None = None,
    bootstrap_machine: int | None = None,
    bootstrap_dependency: bool = False,
) -> tuple[RuntimeBundleVerifier, Path]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    verifier = RuntimeBundleVerifier(
        keyring=ReleaseKeyring({"release-2026": public_key}),
        authenticode=_Authenticode(),
    )
    expected_machine = {"arm64": 0xAA64, "x64": 0x8664}[architecture]
    executable = _pe(expected_machine if executable_machine is None else executable_machine)
    files = {
        "LICENSES/AGPL-3.0.txt": b"license\n",
        "offeragent-host.exe": executable,
        "offeragent-process-host.exe": _pe(expected_machine),
        "offeragent-self-test.exe": _pe(expected_machine),
        "offeragent-worker.exe": _pe(expected_machine),
        "process-catalog.v1.json": _PROCESS_CATALOG,
        "provenance/slsa.json": b"{}\n",
        "sbom/runtime.spdx.json": b"{}\n",
        "web/index.html": b"<!doctype html>\n",
    }
    kinds = {
        "LICENSES/AGPL-3.0.txt": "license",
        "offeragent-host.exe": "executable",
        "offeragent-process-host.exe": "executable",
        "offeragent-self-test.exe": "executable",
        "offeragent-worker.exe": "executable",
        "process-catalog.v1.json": "asset",
        "provenance/slsa.json": "provenance",
        "sbom/runtime.spdx.json": "sbom",
        "web/index.html": "web",
    }
    records = tuple(
        RuntimeFileRecord(
            path=path,
            byte_length=len(payload),
            sha256=_sha256(payload),
            kind=kinds[path],
            authenticode=path.endswith(".exe"),
        )
        for path, payload in sorted(files.items())
    )
    bootstrap = _pe(expected_machine if bootstrap_machine is None else bootstrap_machine)
    dependency_payload = b"bootstrap-runtime-dependency"
    dependencies = (
        (
            RuntimeFileRecord(
                "bootstrap-runtime.dll",
                len(dependency_payload),
                _sha256(dependency_payload),
                "runtime",
            ),
        )
        if bootstrap_dependency
        else ()
    )
    manifest = RuntimeReleaseManifest(
        runtime_version="2.0.0",
        core_version="2.0.0",
        plugin_minimum_version="2.0.0",
        plugin_maximum_version="2.9.9",
        signing_key_id="release-2026",
        build_commit="a" * 40,
        created_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        platform=RuntimePlatform("windows", architecture, 19_045),
        protocol=ProtocolCompatibility("1.0", "1.2", "sha256:" + "b" * 64),
        state_schema_version=1,
        tool_abi_version="1.0",
        archive=RuntimeArchive("offeragent-runtime.zip", runtime_content_digest(records), 1024 * 1024),
        bootstrap=BootstrapRecord(
            "offeragent-bootstrap.exe",
            len(bootstrap),
            _sha256(bootstrap),
            True,
            dependencies,
        ),
        files=records,
        capabilities=("offline_install", "safe_update"),
        privilege_envelope=build_privilege_envelope_from_process_catalog(_PROCESS_CATALOG),
        schema_version=2,
    )
    manifest_bytes = canonical_manifest_bytes(manifest)
    signature = encode_signature(private_key.sign(manifest_bytes))
    root.mkdir()
    (root / "runtime-manifest.json").write_bytes(manifest_bytes)
    (root / "runtime-manifest.sig").write_bytes(signature)
    (root / "offeragent-bootstrap.exe").write_bytes(bootstrap)
    if bootstrap_dependency:
        (root / "bootstrap-runtime.dll").write_bytes(dependency_payload)
    with zipfile.ZipFile(root / "offeragent-runtime.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, payload in files.items():
            info = zipfile.ZipInfo(path)
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, payload)
        archive.writestr("runtime-manifest.json", manifest_bytes)
        archive.writestr("runtime-manifest.sig", signature)
        if malicious_name is not None:
            archive.writestr(malicious_name, b"attack")
        if symlink_path is not None:
            info = zipfile.ZipInfo(symlink_path)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"target")
    return verifier, root


def _verify(verifier: RuntimeBundleVerifier, root: Path, *, architecture: str = "x64") -> VerifiedRuntimeBundle:
    return verifier.verify_bundle(
        root,
        expected_architecture=architecture,
        windows_build=22_631,
        plugin_version="2.0.0",
        protocol_version="1.1",
        schema_hash="sha256:" + "b" * 64,
    )


def test_signed_bundle_extracts_and_verifies_exact_tree(tmp_path: Path) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle")
    bundle = _verify(verifier, root)
    destination = tmp_path / "stage"

    SafeRuntimeZipExtractor().extract(bundle, destination)
    verifier.verify_installed_tree(destination, bundle)

    assert (destination / "offeragent-host.exe").read_bytes().startswith(b"MZ")


def test_installed_process_catalog_tamper_is_rejected_against_signed_envelope(tmp_path: Path) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle")
    bundle = _verify(verifier, root)
    destination = tmp_path / "stage"
    SafeRuntimeZipExtractor().extract(bundle, destination)
    (destination / "process-catalog.v1.json").write_bytes(b"[" + _PROCESS_CATALOG[1:])

    with pytest.raises(ReleaseVerificationError, match="hash"):
        verifier.verify_installed_tree(destination, bundle)


def test_current_manifest_pointer_signature_and_catalog_are_reverified(tmp_path: Path) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle")
    bundle = _verify(verifier, root)
    destination = tmp_path / "2.0.0"
    SafeRuntimeZipExtractor().extract(bundle, destination)
    manifest_hash = _sha256(bundle.manifest_bytes)

    current = verifier.verify_installed_manifest(
        destination,
        expected_manifest_hash=manifest_hash,
        expected_architecture="x64",
    )
    assert current.manifest.privilege_envelope == bundle.manifest.privilege_envelope

    with pytest.raises(ReleaseVerificationError, match="pointer"):
        verifier.verify_installed_manifest(
            destination,
            expected_manifest_hash="sha256:" + "0" * 64,
            expected_architecture="x64",
        )

    original_signature = (destination / "runtime-manifest.sig").read_bytes()
    signature = bytearray(original_signature)
    signature[0] = ord("A") if signature[0] != ord("A") else ord("B")
    (destination / "runtime-manifest.sig").write_bytes(signature)
    with pytest.raises(ReleaseVerificationError, match="signature"):
        verifier.verify_installed_manifest(
            destination,
            expected_manifest_hash=manifest_hash,
            expected_architecture="x64",
        )

    (destination / "runtime-manifest.sig").write_bytes(original_signature)
    catalog = destination / "process-catalog.v1.json"
    catalog.write_bytes(b"[" + catalog.read_bytes()[1:])
    with pytest.raises(ReleaseVerificationError, match="hash"):
        verifier.verify_installed_manifest(
            destination,
            expected_manifest_hash=manifest_hash,
            expected_architecture="x64",
        )


def test_signature_tampering_is_rejected_before_archive_use(tmp_path: Path) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle")
    signature_path = root / "runtime-manifest.sig"
    signature = bytearray(signature_path.read_bytes())
    signature[0] = ord("A") if signature[0] != ord("A") else ord("B")
    signature_path.write_bytes(signature)

    with pytest.raises(ReleaseVerificationError, match="signature"):
        _verify(verifier, root)


@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "/absolute.txt",
        "C:/drive.txt",
        "//server/share.txt",
        "\\\\?\\C:\\device.txt",
        "web/stream.txt:secret",
        "web/CON.txt",
        "web/trailing. ",
    ],
)
def test_unsafe_zip_paths_are_rejected(tmp_path: Path, name: str) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle", malicious_name=name)
    bundle = _verify(verifier, root)

    with pytest.raises(ReleaseVerificationError, match="archive"):
        SafeRuntimeZipExtractor().extract(bundle, tmp_path / "stage")

    assert not (tmp_path / "escape.txt").exists()


def test_zip_symlink_is_rejected(tmp_path: Path) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle", symlink_path="web/link")
    bundle = _verify(verifier, root)

    with pytest.raises(ReleaseVerificationError, match=r"symlink|special"):
        SafeRuntimeZipExtractor().extract(bundle, tmp_path / "stage")


def test_inner_runtime_hash_tamper_is_rejected(tmp_path: Path) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle")
    bundle = _verify(verifier, root)
    destination = tmp_path / "stage"
    SafeRuntimeZipExtractor().extract(bundle, destination)
    (destination / "web" / "index.html").write_bytes(b"tampered")

    with pytest.raises(ReleaseVerificationError, match=r"length|hash"):
        verifier.verify_installed_tree(destination, bundle)


def test_bootstrap_dependency_closure_is_signed_and_exact(tmp_path: Path) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle", bootstrap_dependency=True)
    (root / "bootstrap-runtime.dll").write_bytes(b"tampered")

    with pytest.raises(ReleaseVerificationError, match=r"length|hash"):
        _verify(verifier, root)


def test_unsigned_extra_outer_dll_is_rejected_to_prevent_bootstrap_side_loading(tmp_path: Path) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle")
    (root / "version.dll").write_bytes(b"unsigned side-load")

    with pytest.raises(ReleaseVerificationError, match="file set"):
        _verify(verifier, root)


def test_x86_executable_is_rejected_even_when_hash_and_signature_match(tmp_path: Path) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle", executable_machine=0x014C)
    bundle = _verify(verifier, root)
    destination = tmp_path / "stage"
    SafeRuntimeZipExtractor().extract(bundle, destination)

    with pytest.raises(ReleaseVerificationError, match="architecture"):
        verifier.verify_installed_tree(destination, bundle)


def test_native_arm64_bundle_and_installed_tree_are_accepted(tmp_path: Path) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle", architecture="arm64")
    bundle = _verify(verifier, root, architecture="arm64")
    destination = tmp_path / "stage"

    SafeRuntimeZipExtractor().extract(bundle, destination)
    verifier.verify_installed_tree(destination, bundle)

    assert bundle.manifest.platform.architecture == "arm64"


def test_manifest_architecture_must_match_detected_windows_architecture(tmp_path: Path) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle", architecture="arm64")

    with pytest.raises(ReleaseVerificationError, match="architecture"):
        _verify(verifier, root, architecture="x64")


def test_current_signed_runtime_architecture_is_reverified_before_update_comparison(tmp_path: Path) -> None:
    verifier, root = _create_bundle(tmp_path / "bundle", architecture="arm64")
    bundle = _verify(verifier, root, architecture="arm64")
    destination = tmp_path / "2.0.0"
    SafeRuntimeZipExtractor().extract(bundle, destination)

    with pytest.raises(ReleaseVerificationError, match="architecture"):
        verifier.verify_installed_manifest(
            destination,
            expected_manifest_hash=_sha256(bundle.manifest_bytes),
            expected_architecture="x64",
        )


def test_bootstrap_pe_must_match_signed_manifest_architecture(tmp_path: Path) -> None:
    verifier, root = _create_bundle(
        tmp_path / "bundle",
        architecture="arm64",
        bootstrap_machine=0x8664,
    )

    with pytest.raises(ReleaseVerificationError, match="architecture"):
        _verify(verifier, root, architecture="arm64")


@pytest.mark.parametrize("architecture", ["x86", "aarch64", "unknown", ""])
def test_runtime_platform_rejects_x86_aliases_and_unknown_architectures(architecture: str) -> None:
    with pytest.raises(ReleaseVerificationError, match="x64 and arm64"):
        RuntimePlatform("windows", architecture, 19_045)


@pytest.mark.parametrize("machine", [0x014C, 0, 0x0200])
def test_native_machine_mapping_rejects_x86_and_unknown(machine: int) -> None:
    with pytest.raises(ReleaseVerificationError, match="x64 or arm64"):
        architecture_for_windows_pe_machine(machine)


def test_native_machine_mapping_accepts_only_exact_x64_and_arm64_coff_values() -> None:
    assert architecture_for_windows_pe_machine(0x8664) == "x64"
    assert architecture_for_windows_pe_machine(0xAA64) == "arm64"


def test_manifest_requires_canonical_sorted_capabilities() -> None:
    records = (
        RuntimeFileRecord("offeragent-host.exe", 1, "sha256:" + "0" * 64, "executable", True),
        RuntimeFileRecord("offeragent-process-host.exe", 1, "sha256:" + "1" * 64, "executable", True),
        RuntimeFileRecord("offeragent-self-test.exe", 1, "sha256:" + "2" * 64, "executable", True),
        RuntimeFileRecord("offeragent-worker.exe", 1, "sha256:" + "3" * 64, "executable", True),
        RuntimeFileRecord("provenance/a", 1, "sha256:" + "4" * 64, "provenance"),
        RuntimeFileRecord("sbom/a", 1, "sha256:" + "5" * 64, "sbom"),
        RuntimeFileRecord("web/index.html", 1, "sha256:" + "6" * 64, "web"),
        RuntimeFileRecord("z-license", 1, "sha256:" + "6" * 64, "license"),
    )
    archive = RuntimeArchive("runtime.zip", runtime_content_digest(records), 1024)
    base = RuntimeReleaseManifest(
        "1",
        "1",
        "1",
        "1",
        "key",
        "a" * 40,
        datetime.now(timezone.utc),
        RuntimePlatform("windows", "x64", 19_045),
        ProtocolCompatibility("1.0", "1.0", "sha256:" + "0" * 64),
        1,
        "1",
        archive,
        BootstrapRecord("bootstrap.exe", 1, "sha256:" + "0" * 64, True),
        records,
        ("a", "b"),
    )

    with pytest.raises(ReleaseVerificationError, match="sorted"):
        replace(base, capabilities=("b", "a"))
