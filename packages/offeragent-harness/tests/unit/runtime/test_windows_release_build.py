from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace
from typing import cast

import pytest
from scripts.frozen_payload_provenance import (
    OFFERAGENT_COMPONENT,
    PROVENANCE_PATH,
    SPDX_PATH,
    AuthenticodeTransform,
    FrozenFile,
    FrozenRuntimeEvidence,
    FrozenTarget,
    StaticPayloadSource,
    build_payload_provenance,
    build_spdx_document,
    canonical_json,
    make_source_record,
)

from offeragent_harness.runtime.release_manifest import (
    BootstrapRecord,
    ProtocolCompatibility,
    RuntimeArchive,
    RuntimeFileRecord,
    RuntimePlatform,
    RuntimeReleaseManifest,
    canonical_manifest_bytes,
    runtime_content_digest,
)
from offeragent_harness.runtime.release_privileges import build_privilege_envelope_from_process_catalog

_BUILD_SCRIPT = run_path(str(Path(__file__).resolve().parents[3] / "scripts" / "build_windows_release.py"))
audit_runtime_archive = cast(Callable[[Path], None], _BUILD_SCRIPT["audit_runtime_archive"])
build_setup = cast(Callable[[Path, object], None], _BUILD_SCRIPT["build_setup"])
digest_file = cast(Callable[[Path], str], _BUILD_SCRIPT["digest_file"])
pyinstaller_environment = cast(Callable[[], dict[str, str]], _BUILD_SCRIPT["_pyinstaller_environment"])
records_for = cast(Callable[[Path], list[RuntimeFileRecord]], _BUILD_SCRIPT["records_for"])
normalize_pyinstaller_base_library = cast(
    Callable[[Path, Path], None],
    _BUILD_SCRIPT["normalize_pyinstaller_base_library"],
)
require_release_host = cast(Callable[[object], None], _BUILD_SCRIPT["require_release_host"])
sign_executables = cast(
    Callable[[Path, object], dict[str, AuthenticodeTransform]],
    _BUILD_SCRIPT["sign_executables"],
)
_CATALOG = (Path(__file__).resolve().parents[3] / "packaging" / "process-catalog.v1.json").read_bytes()
_INNO_SCRIPT = Path(__file__).resolve().parents[3] / "packaging" / "OfferAgent.iss"


def _payload_tree(
    root: Path,
    *,
    exact_skill_sbom: bool = True,
    forbidden_sbom: bool = False,
    malformed_spdx: bool = False,
    tampered_provenance_hash: bool = False,
    omit_authenticode_transforms: bool = False,
    process_catalog: bytes = _CATALOG,
) -> None:
    payloads = {
        "LICENSES/AGPL.txt": b"license\n",
        "offeragent-host.exe": b"host",
        "offeragent-process-host.exe": b"process-host",
        "offeragent-self-test.exe": b"self-test",
        "offeragent-worker.exe": b"worker",
        "process-catalog.v1.json": process_catalog,
        "skills/builtin/SKILL.md": b"signed builtin\n",
        "web/index.html": b"<!doctype html>\n",
    }
    for relative, payload in payloads.items():
        target = root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    evidence = FrozenRuntimeEvidence()
    evidence.register_component(OFFERAGENT_COMPONENT)
    toc_hash = "sha256:" + "1" * 64
    toc_records = tuple((kind, toc_hash) for kind in ("Analysis", "COLLECT", "EXE", "PKG", "PYZ"))
    target_executables = {
        "offeragent-host": "offeragent-host.exe",
        "offeragent-process-host": "offeragent-process-host.exe",
        "offeragent-self-test": "offeragent-self-test.exe",
        "offeragent-worker": "offeragent-worker.exe",
    }
    for name, executable in target_executables.items():
        source = make_source_record(
            OFFERAGENT_COMPONENT,
            f"project:scripts/entrypoints/{name}.py",
            "test-frozen-executable",
            root / executable,
        )
        evidence.register_source(source)
        evidence.files[executable] = FrozenFile(
            executable,
            OFFERAGENT_COMPONENT.spdx_id,
            (root / executable).stat().st_size,
            digest_file(root / executable),
            (source.identifier,),
            (name,),
        )
        evidence.targets[name] = FrozenTarget(name, executable, toc_records, (source.identifier,))
    transforms: dict[str, AuthenticodeTransform] = {}
    for executable in target_executables.values():
        path = root / executable
        pre_sign_sha256 = digest_file(path)
        path.write_bytes(path.read_bytes() + b"-authenticode-signed")
        transforms[executable] = AuthenticodeTransform(
            executable,
            pre_sign_sha256,
            digest_file(path),
            0x8664,
            True,
        )
    static_sources = {
        relative: StaticPayloadSource(
            OFFERAGENT_COMPONENT,
            path,
            f"project:test-fixture/{relative}",
        )
        for relative, path in {
            path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()
        }.items()
        if relative not in target_executables.values()
    }
    provenance = build_payload_provenance(
        runtime=root,
        evidence=evidence,
        static_sources=static_sources,
        architecture="x64",
        build_commit="a" * 40,
        runtime_version="2.0.0",
        source_date_epoch=int(datetime(2026, 7, 13, tzinfo=timezone.utc).timestamp()),
        runtime_dependency_closure_sha256="sha256:" + "2" * 64,
        uv_lock_sha256="sha256:" + "3" * 64,
        executable_transforms=transforms,
    )
    if omit_authenticode_transforms:
        provenance["transforms"] = []
    if tampered_provenance_hash:
        files = provenance["files"]
        assert isinstance(files, list)
        files[0]["sha256"] = "sha256:" + "0" * 64
    provenance_path = root / PROVENANCE_PATH
    provenance_path.parent.mkdir(parents=True)
    provenance_path.write_bytes(canonical_json(provenance))
    sbom = build_spdx_document(
        runtime=root,
        provenance=provenance,
        created_at="2026-07-13T00:00:00Z",
        document_namespace="https://spdx.offeragent.invalid/runtime/2.0.0/x64/test/closure",
    )
    if forbidden_sbom:
        packages = sbom["packages"]
        assert isinstance(packages, list)
        packages.append(
            {
                "SPDXID": "SPDXRef-Package-django",
                "name": "Django",
                "versionInfo": "1.0",
            }
        )
    if not exact_skill_sbom:
        files = sbom["files"]
        relationships = sbom["relationships"]
        assert isinstance(files, list)
        assert isinstance(relationships, list)
        skill_id = next(item["SPDXID"] for item in files if item["fileName"] == "./skills/builtin/SKILL.md")
        sbom["files"] = [item for item in files if item["SPDXID"] != skill_id]
        sbom["relationships"] = [item for item in relationships if item["relatedSpdxElement"] != skill_id]
    if malformed_spdx:
        sbom.pop("dataLicense")
    target = root / SPDX_PATH
    target.parent.mkdir()
    target.write_bytes(canonical_json(sbom))


def _assembled_payload(
    tmp_path: Path,
    *,
    exact_skill_sbom: bool = True,
    forbidden_sbom: bool = False,
    malformed_spdx: bool = False,
    tampered_provenance_hash: bool = False,
    omit_authenticode_transforms: bool = False,
    process_catalog: bytes = _CATALOG,
) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _payload_tree(
        runtime,
        exact_skill_sbom=exact_skill_sbom,
        forbidden_sbom=forbidden_sbom,
        malformed_spdx=malformed_spdx,
        tampered_provenance_hash=tampered_provenance_hash,
        omit_authenticode_transforms=omit_authenticode_transforms,
        process_catalog=process_catalog,
    )
    records = tuple(records_for(runtime))
    manifest = RuntimeReleaseManifest(
        "2.0.0",
        "2.0.0",
        "2.0.0",
        "2.9.9",
        "release-test",
        "a" * 40,
        datetime(2026, 7, 13, tzinfo=timezone.utc),
        RuntimePlatform("windows", "x64", 19_045),
        ProtocolCompatibility("1.0", "1.0", "sha256:" + "b" * 64),
        1,
        "1",
        RuntimeArchive("offeragent-runtime.zip", runtime_content_digest(records), 1024 * 1024),
        BootstrapRecord("offeragent-bootstrap.exe", 1, "sha256:" + "c" * 64, True),
        records,
        ("offline_install",),
        build_privilege_envelope_from_process_catalog(process_catalog),
        2,
    )
    manifest_bytes = canonical_manifest_bytes(manifest)
    signature = b"test-signature\n"
    (runtime / "runtime-manifest.json").write_bytes(manifest_bytes)
    (runtime / "runtime-manifest.sig").write_bytes(signature)
    platform_root = tmp_path / "payload"
    platform_root.mkdir()
    (platform_root / "runtime-manifest.json").write_bytes(manifest_bytes)
    (platform_root / "runtime-manifest.sig").write_bytes(signature)
    with zipfile.ZipFile(platform_root / "offeragent-runtime.zip", "w") as archive:
        for path in sorted(item for item in runtime.rglob("*") if item.is_file()):
            archive.write(path, path.relative_to(runtime).as_posix())
    return platform_root


def test_release_records_and_exact_audit_cover_builtin_skills(tmp_path: Path) -> None:
    platform_root = _assembled_payload(tmp_path)

    audit_runtime_archive(platform_root)

    with zipfile.ZipFile(platform_root / "offeragent-runtime.zip", "a") as archive:
        archive.writestr("unsigned-extra.txt", b"extra")
    with pytest.raises(RuntimeError, match="file set"):
        audit_runtime_archive(platform_root)


def test_pyinstaller_base_library_normalization_is_order_independent(tmp_path: Path) -> None:
    work = tmp_path / "build"
    root = tmp_path / "dist"
    (root / "_internal").mkdir(parents=True)
    work.mkdir()
    source = work / "base_library.zip"
    output = root / "_internal" / "base_library.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("b.pyc", b"b")
        archive.writestr("a.pyc", b"a")
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("a.pyc", b"a")
        archive.writestr("b.pyc", b"b")

    normalize_pyinstaller_base_library(root, work)

    assert source.read_bytes() == output.read_bytes()


def test_release_audit_rejects_sbom_that_omits_signed_skill(tmp_path: Path) -> None:
    platform_root = _assembled_payload(tmp_path, exact_skill_sbom=False)

    with pytest.raises(RuntimeError, match=r"SPDX|SBOM"):
        audit_runtime_archive(platform_root)


def test_release_audit_rejects_retired_server_dependency_in_sbom(tmp_path: Path) -> None:
    platform_root = _assembled_payload(tmp_path, forbidden_sbom=True)

    with pytest.raises(RuntimeError, match="retired server dependencies"):
        audit_runtime_archive(platform_root)


def test_release_audit_rejects_non_spdx_dependency_document(tmp_path: Path) -> None:
    platform_root = _assembled_payload(tmp_path, malformed_spdx=True)

    with pytest.raises(RuntimeError, match="SBOM document shape"):
        audit_runtime_archive(platform_root)


def test_release_audit_rejects_signed_but_false_payload_hash_provenance(tmp_path: Path) -> None:
    platform_root = _assembled_payload(tmp_path, tampered_provenance_hash=True)

    with pytest.raises(RuntimeError, match=r"hashes differ|post-capture mutation"):
        audit_runtime_archive(platform_root)


def test_release_audit_rejects_signed_executables_without_declared_transform(tmp_path: Path) -> None:
    platform_root = _assembled_payload(tmp_path, omit_authenticode_transforms=True)

    with pytest.raises(RuntimeError, match="transform"):
        audit_runtime_archive(platform_root)


def test_release_audit_rejects_network_enabled_process_catalog(tmp_path: Path) -> None:
    document = json.loads(_CATALOG)
    document["executableProfiles"][0]["allowNetwork"] = True
    payload = (json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    with pytest.raises(RuntimeError, match="network"):
        _assembled_payload(tmp_path, process_catalog=payload)


def test_inno_uninstall_is_bootstrap_gated_preserve_first_and_never_deletes_vault_notes() -> None:
    source = _INNO_SCRIPT.read_text(encoding="utf-8")

    assert "function InitializeUninstall(): Boolean;" in source
    assert "offeragent-bootstrap.exe" in source
    assert "inno-ledger uninstall --operation-id" in source
    assert "--scope selected --installation-id" in source
    assert "--scope all --mode" in source
    assert "--mode preserve-data" in source
    assert "--mode purge-data" in source
    assert "PurgeConfirmation = 'DELETE OFFERAGENT LOCAL DATA'" in source
    assert "ewWaitUntilTerminated" in source
    assert "ResultCode = 0" in source
    assert "UninstallSilent" in source
    uninstall_delete = source.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]
    assert "vault-installations.json" in uninstall_delete
    assert "Type: dirifempty" in uninstall_delete
    assert "filesandordirs" not in uninstall_delete.casefold()
    assert "*" not in uninstall_delete
    assert "DelTree(" not in source
    assert "notes\\" not in source.casefold()


def test_setup_ledger_records_the_packaged_plugin_version_not_runtime_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "release"
    plugin = output / "setup-payload" / "offeragent-obsidian-plugin"
    plugin.mkdir(parents=True)
    (plugin / "manifest.json").write_text('{"version":"2.0.0-beta.28"}\n', encoding="utf-8")
    iscc = tmp_path / "ISCC.exe"
    sign_tool = tmp_path / "signtool.exe"
    iscc.write_bytes(b"iscc")
    sign_tool.write_bytes(b"sign")
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool, **kwargs: object) -> None:
        del kwargs
        assert check
        commands.append(command)
        if command[0] == str(iscc):
            (output / "OfferAgent-for-Obsidian-Setup-x64.exe").write_bytes(b"signed")

    class _Verifier:
        def verify(self, path: Path) -> bool:
            return path.read_bytes() == b"signed"

    monkeypatch.setattr(build_setup.__globals__["subprocess"], "run", fake_run)
    monkeypatch.setitem(build_setup.__globals__, "WindowsAuthenticodeVerifier", _Verifier)

    build_setup(
        output,
        SimpleNamespace(
            architecture="x64",
            certificate_sha1="a" * 40,
            iscc=iscc,
            runtime_version="2.0.0",
            sign_tool=sign_tool,
            timestamp_url="https://timestamp.invalid",
        ),
    )

    assert "/DPluginVersion=2.0.0-beta.28" in commands[0]
    assert "/DRuntimeVersion=2.0.0" in commands[0]


def test_release_build_rejects_cross_architecture_labeling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(require_release_host.__globals__, "native_windows_architecture", lambda: "arm64")

    with pytest.raises(SystemExit, match="does not match native Windows arm64"):
        require_release_host(SimpleNamespace(architecture="x64"))


def test_release_build_refuses_to_sign_a_mislabeled_pe(tmp_path: Path) -> None:
    executable = tmp_path / "offeragent-host.exe"
    image = bytearray(70)
    image[:2] = b"MZ"
    image[60:64] = (64).to_bytes(4, "little")
    image[64:68] = b"PE\0\0"
    image[68:70] = (0x014C).to_bytes(2, "little")
    executable.write_bytes(image)

    with pytest.raises(RuntimeError, match="refusing to sign PE"):
        sign_executables(tmp_path, SimpleNamespace(architecture="arm64"))


def test_sign_executables_records_only_verified_authenticode_transform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "offeragent-host.exe"
    image = bytearray(70)
    image[:2] = b"MZ"
    image[60:64] = (64).to_bytes(4, "little")
    image[64:68] = b"PE\0\0"
    image[68:70] = (0x8664).to_bytes(2, "little")
    executable.write_bytes(image)
    pre_sign_sha256 = digest_file(executable)

    def fake_run(command: list[str], *, check: bool) -> None:
        assert check is True
        target = Path(command[-1])
        target.write_bytes(target.read_bytes() + b"verified-authenticode")

    class _Verifier:
        def verify(self, path: Path) -> bool:
            return path.read_bytes().endswith(b"verified-authenticode")

    monkeypatch.setattr(sign_executables.__globals__["subprocess"], "run", fake_run)
    monkeypatch.setitem(sign_executables.__globals__, "WindowsAuthenticodeVerifier", _Verifier)
    transforms = sign_executables(
        tmp_path,
        SimpleNamespace(
            architecture="x64",
            sign_tool=Path("SignTool.exe"),
            certificate_sha1="certificate",
            timestamp_url="https://timestamp.invalid",
        ),
    )

    transform = transforms["offeragent-host.exe"]
    assert transform.pre_sign_sha256 == pre_sign_sha256
    assert transform.post_sign_sha256 == digest_file(executable)
    assert transform.pre_sign_sha256 != transform.post_sign_sha256
    assert transform.pe_machine == 0x8664
    assert transform.authenticode_verified is True


def test_release_and_inno_outputs_are_parameterized_for_x64_and_arm64() -> None:
    build_source = (Path(__file__).resolve().parents[3] / "scripts" / "build_windows_release.py").read_text(
        encoding="utf-8"
    )
    inno_source = _INNO_SCRIPT.read_text(encoding="utf-8")

    assert 'choices=("x64", "arm64")' in build_source
    assert 'f"windows-{args.architecture}"' in build_source
    assert 'f"OfferAgent-for-Obsidian-Setup-{args.architecture}.exe"' in build_source
    assert "RuntimeArchitecture must be x64 or arm64" in inno_source
    assert "ArchitecturesAllowed={#InstallerArchitecturesAllowed}" in inno_source
    assert '#define InstallerArchitecturesAllowed "x64os"' in inno_source
    assert '#define InstallerArchitecturesAllowed "arm64"' in inno_source
    assert "OfferAgent-for-Obsidian-Setup-{#RuntimeArchitecture}" in inno_source


def test_pyinstaller_environment_rebuilds_path_from_the_active_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", r"E:\miniconda;E:\untrusted-tools")
    monkeypatch.setenv("CONDA_PREFIX", r"E:\miniconda")
    monkeypatch.setenv("_CONDA_EXE", r"E:\miniconda\Scripts\conda.exe")
    environment = pyinstaller_environment()

    assert "untrusted-tools" not in environment["PATH"].casefold()
    assert "CONDA_PREFIX" not in environment
    assert "_CONDA_EXE" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONSAFEPATH"] == "1"
