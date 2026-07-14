"""Build, sign, assemble and audit one native Windows x64 or arm64 release.

This script intentionally has no unsigned production mode. Development builds
use the normal Python/Node commands; a distributable payload requires both an
external Ed25519 key and Authenticode signing identity.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from offeragent_harness._release_keys import PUBLIC_KEYS_BASE64URL
from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.runtime.production_process_catalog import (
    PROCESS_CATALOG_PATH,
    validate_process_catalog_payload,
)
from offeragent_harness.runtime.release_manifest import (
    BootstrapRecord,
    ProtocolCompatibility,
    RuntimeArchive,
    RuntimeFileRecord,
    RuntimePlatform,
    RuntimeReleaseManifest,
    canonical_manifest_bytes,
    encode_signature,
    native_windows_architecture,
    parse_manifest,
    runtime_content_digest,
    windows_pe_machine_for_architecture,
)
from offeragent_harness.runtime.release_privileges import (
    build_privilege_envelope_from_process_catalog,
    privilege_envelope_payload,
)
from offeragent_harness.runtime.windows_authenticode import WindowsAuthenticodeVerifier
from offeragent_harness.storage.migrations import LATEST_SCHEMA_VERSION

try:
    from scripts.frozen_payload_provenance import (
        OFFERAGENT_COMPONENT,
        PROVENANCE_PATH,
        SPDX_PATH,
        AuthenticodeTransform,
        Component,
        FrozenRuntimeEvidence,
        SourceClassifier,
        StaticPayloadSource,
        assert_payload_provenance,
        build_payload_provenance,
        build_spdx_document,
        canonical_json,
        capture_pyinstaller_target,
        merge_frozen_evidence,
        python_distribution_component,
    )
    from scripts.release_dependency_policy import assert_runtime_spdx_document, assert_sbom_packages_allowed
    from scripts.runtime_sbom import (
        runtime_closure_fingerprint,
        runtime_distribution_closure,
    )
except ModuleNotFoundError:  # Direct `python scripts/build_windows_release.py` execution.
    from frozen_payload_provenance import (  # type: ignore[import-not-found,no-redef]
        OFFERAGENT_COMPONENT,
        PROVENANCE_PATH,
        SPDX_PATH,
        AuthenticodeTransform,
        Component,
        FrozenRuntimeEvidence,
        SourceClassifier,
        StaticPayloadSource,
        assert_payload_provenance,
        build_payload_provenance,
        build_spdx_document,
        canonical_json,
        capture_pyinstaller_target,
        merge_frozen_evidence,
        python_distribution_component,
    )
    from release_dependency_policy import (  # type: ignore[import-not-found,no-redef]
        assert_runtime_spdx_document,
        assert_sbom_packages_allowed,
    )
    from runtime_sbom import (  # type: ignore[import-not-found,no-redef]
        runtime_closure_fingerprint,
        runtime_distribution_closure,
    )

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
PLUGIN = REPO / "src" / "interface" / "obsidian"
ENTRYPOINTS = ROOT / "scripts" / "entrypoints"
BUILTIN_SKILLS = ROOT / "packaging" / "runtime-skills"
PROCESS_CATALOG = ROOT / "packaging" / "process-catalog.v1.json"
FORBIDDEN_IMPORTS = ("django", "psycopg", "postgres", "khoj", "node_modules")
LOCAL_DEVELOPMENT_MARKER = b"OFFERAGENT_LOCAL_DEVELOPMENT_RUNTIME_V1"
LOCAL_DEVELOPMENT_ANCHOR_TOKEN = b"__OFFERAGENT_DEVELOPMENT_MANIFEST_SHA256__"
REQUIRED_EXES = (
    "offeragent-host.exe",
    "offeragent-process-host.exe",
    "offeragent-self-test.exe",
    "offeragent-worker.exe",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--architecture", choices=("x64", "arm64"), required=True)
    parser.add_argument("--plugin-minimum-version", required=True)
    parser.add_argument("--plugin-maximum-version", required=True)
    parser.add_argument("--build-commit", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--ed25519-private-key", type=Path, required=True)
    parser.add_argument("--sign-tool", type=Path, required=True)
    parser.add_argument("--certificate-sha1", required=True)
    parser.add_argument("--timestamp-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--iscc", type=Path, required=True)
    parser.add_argument("--ripgrep-executable", type=Path, required=True)
    args = parser.parse_args()
    require_release_host(args)
    with tempfile.TemporaryDirectory(prefix="offeragent-release-") as temporary:
        work = Path(temporary)
        runtime, frozen_evidence = build_runtime_onedir(work / "runtime", args)
        bootstrap = build_one_onedir(
            ENTRYPOINTS / "offeragent_bootstrap.py",
            "offeragent-bootstrap",
            work / "bootstrap",
        )
        static_sources = add_release_assets(
            runtime,
            architecture=args.architecture,
            ripgrep_executable=args.ripgrep_executable,
        )
        runtime_transforms = sign_executables(runtime, args)
        sign_executables(bootstrap, args)
        add_release_metadata(runtime, args, frozen_evidence, static_sources, runtime_transforms)
        output = args.output.resolve(strict=False) / f"windows-{args.architecture}"
        output.mkdir(parents=True, exist_ok=True)
        assemble_payload(runtime, bootstrap, output, args)
        build_plugin_package(output, args)
        audit_release(output, args)
        build_setup(output, args)
    return 0


def require_release_host(args: argparse.Namespace) -> None:
    try:
        native_architecture = native_windows_architecture()
    except RuntimeError as error:
        raise SystemExit(f"release build requires native Windows x64 or arm64: {error}") from error
    if native_architecture != args.architecture:
        raise SystemExit(
            f"release architecture {args.architecture} does not match native Windows {native_architecture}"
        )
    if not args.sign_tool.is_file() or args.sign_tool.suffix.casefold() != ".exe":
        raise SystemExit("SignTool.exe is required")
    if not args.ripgrep_executable.is_file() or args.ripgrep_executable.name.casefold() != "rg.exe":
        raise SystemExit("a concrete rg.exe build input is required")
    if not args.ed25519_private_key.is_file():
        raise SystemExit("external Ed25519 private key is required")
    if args.key_id not in PUBLIC_KEYS_BASE64URL:
        raise SystemExit("key-id is absent from generated release keyrings")
    if not __import__("importlib").util.find_spec("PyInstaller"):
        raise SystemExit("PyInstaller build dependency is unavailable")
    from offeragent_harness.protocol.messages import COMMAND_REGISTRY
    from offeragent_harness.runtime.application_domain_handlers import compose_domain_command_handlers

    if not callable(compose_domain_command_handlers) or len(COMMAND_REGISTRY) < 1:
        raise SystemExit("complete application command factory is unavailable")
    worker_composition = ROOT / "src" / "offeragent_harness" / "runtime" / "production_worker_composition.py"
    if not worker_composition.is_file():
        raise SystemExit("production Worker composition is incomplete; refusing a partial Runtime")
    module = importlib.import_module("offeragent_harness.runtime.production_worker_composition")
    if module.__dict__.get("PRODUCTION_WORKER_COMPOSITION_COMPLETE") is not True or not callable(
        module.__dict__.get("main")
    ):
        raise SystemExit("production Worker composition did not pass its explicit release gate")
    subprocess.run([sys.executable, "scripts/audit_repository_closure.py"], cwd=ROOT, check=True)
    subprocess.run([sys.executable, "scripts/check_forbidden_dependencies.py"], cwd=ROOT, check=True)
    subprocess.run([sys.executable, "scripts/check_architecture.py"], cwd=ROOT, check=True)
    subprocess.run([sys.executable, "scripts/build_web_assets.py", "check"], cwd=ROOT, check=True)


def build_runtime_onedir(
    destination: Path,
    args: argparse.Namespace,
) -> tuple[Path, FrozenRuntimeEvidence]:
    del args
    specifications = (
        (ENTRYPOINTS / "offeragent_host.py", "offeragent-host", destination / "host"),
        (ENTRYPOINTS / "offeragent_worker.py", "offeragent-worker", destination / "worker"),
        (ENTRYPOINTS / "offeragent_self_test.py", "offeragent-self-test", destination / "self-test"),
        (
            ENTRYPOINTS / "offeragent_process_host.py",
            "offeragent-process-host",
            destination / "process-host",
        ),
    )
    classifier = SourceClassifier(project_root=ROOT)
    roots: list[Path] = []
    target_evidence: list[FrozenRuntimeEvidence] = []
    for entrypoint, name, target_root in specifications:
        root = build_one_onedir(entrypoint, name, target_root)
        roots.append(root)
        target_evidence.append(
            capture_pyinstaller_target(
                name=name,
                root=root,
                work_root=target_root / "build" / name,
                classifier=classifier,
            )
        )
    merged = destination / "merged"
    merged.mkdir(parents=True)
    for root in roots:
        merge_identical_tree(root, merged)
    actual = {path.name for path in merged.glob("*.exe")}
    if not set(REQUIRED_EXES) <= actual:
        raise RuntimeError("PyInstaller output is missing Host/Worker/self-test")
    frozen_evidence = merge_frozen_evidence(merged_root=merged, targets=target_evidence)
    return merged, frozen_evidence


def build_one_onedir(
    entrypoint: Path,
    name: str,
    destination: Path,
    *,
    collect_submodules: tuple[str, ...] = (),
    hidden_imports: tuple[str, ...] = (),
    excluded_modules: tuple[str, ...] = (),
    add_data: tuple[tuple[Path, str], ...] = (),
) -> Path:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--noupx",
        "--console",
        "--name",
        name,
        "--distpath",
        str(destination / "dist"),
        "--workpath",
        str(destination / "build"),
        "--specpath",
        str(destination / "spec"),
    ]
    for package in collect_submodules:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", package) is None:
            raise ValueError("PyInstaller collect-submodules package is invalid")
        command.extend(("--collect-submodules", package))
    for module in hidden_imports:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module) is None:
            raise ValueError("PyInstaller hidden-import module is invalid")
        command.extend(("--hidden-import", module))
    for module in excluded_modules:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module) is None:
            raise ValueError("PyInstaller excluded module is invalid")
        command.extend(("--exclude-module", module))
    for source, target in add_data:
        if not source.exists() or re.fullmatch(r"[A-Za-z0-9_./-]+", target) is None or target.startswith("/"):
            raise ValueError("PyInstaller add-data mapping is invalid")
        command.extend(("--add-data", f"{source.resolve(strict=True)}{os.pathsep}{target}"))
    command.append(str(entrypoint))
    subprocess.run(command, cwd=ROOT, check=True, env=_pyinstaller_environment())
    root = destination / "dist" / name
    if not root.is_dir():
        raise RuntimeError(f"PyInstaller did not produce onedir for {name}")
    normalize_pyinstaller_base_library(root, destination / "build" / name)
    return root


def _pyinstaller_environment() -> dict[str, str]:
    """Remove ambient toolchains from DLL discovery during frozen builds."""

    blocked = {"PATH", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in blocked and not key.upper().lstrip("_").startswith("CONDA")
    }
    windows_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve(strict=True)
    trusted_path = (
        Path(sys.executable).resolve(strict=True).parent,
        Path(sys.base_prefix).resolve(strict=True),
        Path(sys.base_prefix).resolve(strict=True) / "DLLs",
        windows_root / "System32",
        windows_root / "System32" / "downlevel",
        windows_root,
    )
    actual = tuple(path for path in trusted_path if path.is_dir())
    if len(actual) < 4:
        raise RuntimeError("trusted PyInstaller PATH is incomplete")
    environment["PATH"] = os.pathsep.join(str(path) for path in actual)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    return environment


def normalize_pyinstaller_base_library(root: Path, work_root: Path) -> None:
    """Make PyInstaller's set-ordered stdlib ZIP byte-for-byte reproducible."""

    output_matches = [path for path in root.rglob("base_library.zip") if path.is_file() and not path.is_symlink()]
    source = work_root / "base_library.zip"
    if len(output_matches) != 1 or not source.is_file() or source.is_symlink():
        raise RuntimeError("PyInstaller base_library.zip source/output is missing or ambiguous")
    _normalize_zip(source)
    _normalize_zip(output_matches[0])
    if source.read_bytes() != output_matches[0].read_bytes():
        raise RuntimeError("normalized PyInstaller base_library.zip source/output differs")


def _normalize_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "r") as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or any(
            "\\" in name or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/"))
            for name in names
        ):
            raise RuntimeError("PyInstaller base_library.zip contains unsafe or duplicate entries")
        entries = [(info.filename, info.compress_type, archive.read(info)) for info in infos]
    temporary = path.with_name(f".{path.name}.normalize.tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name, compression, payload in sorted(entries):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = compression
                if compression == zipfile.ZIP_DEFLATED:
                    archive.writestr(info, payload, compress_type=compression, compresslevel=9)
                else:
                    archive.writestr(info, payload, compress_type=compression)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def merge_identical_tree(source: Path, destination: Path) -> None:
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if item.is_symlink() or not item.is_file():
            raise RuntimeError("PyInstaller output contains a symlink/special file")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != item.read_bytes():
                raise RuntimeError(f"onedir dependency collision differs: {relative.as_posix()}")
        else:
            shutil.copy2(item, target)


def add_release_assets(
    runtime: Path,
    *,
    architecture: str,
    ripgrep_executable: Path,
) -> dict[str, StaticPayloadSource]:
    static_sources: dict[str, StaticPayloadSource] = {}
    if not BUILTIN_SKILLS.is_dir() or not any(BUILTIN_SKILLS.rglob("SKILL.md")):
        raise RuntimeError("release source contains no Builtin Skills")
    _copy_static_tree(
        source=BUILTIN_SKILLS,
        destination=runtime / "skills",
        runtime=runtime,
        locator_prefix="project:packaging/runtime-skills",
        static_sources=static_sources,
    )
    _copy_static_tree(
        source=ROOT / "web",
        destination=runtime / "web",
        runtime=runtime,
        locator_prefix="project:web",
        static_sources=static_sources,
    )
    if not PROCESS_CATALOG.is_file():
        raise RuntimeError("release source contains no signed Process catalog")
    try:
        validate_process_catalog_payload(PROCESS_CATALOG.read_bytes())
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise RuntimeError("release source Process catalog is invalid") from error
    _copy_static_file(
        source=PROCESS_CATALOG,
        destination=runtime / PROCESS_CATALOG.name,
        runtime=runtime,
        locator="project:packaging/process-catalog.v1.json",
        static_sources=static_sources,
    )
    ripgrep, version = _validated_ripgrep_executable(ripgrep_executable, architecture=architecture)
    _copy_static_file(
        source=ripgrep,
        destination=runtime / "tools" / "rg.exe",
        runtime=runtime,
        locator="build-input:ripgrep/rg.exe",
        static_sources=static_sources,
        component=Component(
            "SPDXRef-Package-ripgrep",
            "third-party",
            "ripgrep",
            version,
            "MIT OR Unlicense",
        ),
        kind="third-party-executable",
    )
    licenses = runtime / "LICENSES"
    licenses.mkdir()
    _copy_static_file(
        source=ROOT / "LICENSE",
        destination=licenses / "AGPL-3.0-or-later.txt",
        runtime=runtime,
        locator="project:LICENSE",
        static_sources=static_sources,
    )
    closure = runtime_distribution_closure()
    for item in closure:
        distribution = item.distribution
        for candidate in distribution.files or ():
            basename = Path(str(candidate)).name.casefold()
            if not basename.startswith(("license", "copying", "notice")):
                continue
            located = Path(str(distribution.locate_file(candidate)))
            if not located.is_file():
                continue
            candidate_path = str(candidate).replace("\\", "/")
            if any(part in {"", ".", ".."} for part in candidate_path.split("/")):
                continue
            suffix = digest_file(located).removeprefix("sha256:")[:16]
            target = licenses / "python" / item.canonical_name / f"{suffix}-{Path(candidate_path).name}"
            _copy_static_file(
                source=located,
                destination=target,
                runtime=runtime,
                locator=f"python-distribution:{item.canonical_name}/{candidate_path}",
                static_sources=static_sources,
                component=python_distribution_component(item.name, item.version),
                kind="license-evidence",
            )
    return static_sources


def _validated_ripgrep_executable(executable: Path, *, architecture: str) -> tuple[Path, str]:
    """Accept one explicit, native ripgrep input for the sealed Runtime payload."""

    try:
        resolved = executable.resolve(strict=True)
    except OSError as error:
        raise RuntimeError("ripgrep build input is unavailable") from error
    if not resolved.is_file() or resolved.is_symlink() or resolved.name.casefold() != "rg.exe":
        raise RuntimeError("ripgrep build input must be a regular rg.exe file")
    if pe_machine(resolved) != windows_pe_machine_for_architecture(architecture):
        raise RuntimeError("ripgrep build input architecture differs from the Runtime architecture")
    try:
        completed = subprocess.run(
            [str(resolved), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
            shell=False,
        )
        first_line = completed.stdout.decode("utf-8", errors="strict").splitlines()[0]
    except (IndexError, OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("ripgrep build input failed its version handshake") from error
    match = re.fullmatch(r"ripgrep (\d+\.\d+\.\d+) \(rev [0-9a-f]+\)", first_line)
    if completed.returncode != 0 or match is None:
        raise RuntimeError("ripgrep build input failed its version handshake")
    return resolved, match.group(1)


def add_release_metadata(
    runtime: Path,
    args: argparse.Namespace,
    frozen_evidence: FrozenRuntimeEvidence,
    static_sources: dict[str, StaticPayloadSource],
    executable_transforms: dict[str, AuthenticodeTransform],
) -> None:
    closure = runtime_distribution_closure()
    closure_fingerprint = runtime_closure_fingerprint(closure)
    provenance = build_payload_provenance(
        runtime=runtime,
        evidence=frozen_evidence,
        static_sources=static_sources,
        architecture=args.architecture,
        build_commit=args.build_commit,
        runtime_version=args.runtime_version,
        source_date_epoch=args.source_date_epoch,
        runtime_dependency_closure_sha256=closure_fingerprint,
        uv_lock_sha256=digest_file(ROOT / "uv.lock"),
        executable_transforms=executable_transforms,
    )
    provenance_path = runtime / PROVENANCE_PATH
    provenance_path.parent.mkdir()
    provenance_path.write_bytes(canonical_json(provenance))

    created_at = datetime.fromtimestamp(args.source_date_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    spdx = build_spdx_document(
        runtime=runtime,
        provenance=provenance,
        created_at=created_at,
        document_namespace=(
            f"https://spdx.offeragent.invalid/runtime/{args.runtime_version}/{args.architecture}/"
            f"{args.build_commit}/{digest_file(provenance_path).removeprefix('sha256:')}"
        ),
    )
    packages = spdx.get("packages")
    assert_sbom_packages_allowed(packages)
    assert_runtime_spdx_document(spdx)
    spdx_path = runtime / SPDX_PATH
    spdx_path.parent.mkdir()
    spdx_path.write_bytes(canonical_json(spdx))


def _copy_static_tree(
    *,
    source: Path,
    destination: Path,
    runtime: Path,
    locator_prefix: str,
    static_sources: dict[str, StaticPayloadSource],
) -> None:
    for item in sorted(source.rglob("*")):
        if item.is_dir():
            continue
        if item.is_symlink() or not item.is_file():
            raise RuntimeError("release static asset is a symlink or special file")
        relative = item.relative_to(source)
        _copy_static_file(
            source=item,
            destination=destination / relative,
            runtime=runtime,
            locator=f"{locator_prefix}/{relative.as_posix()}",
            static_sources=static_sources,
        )


def _copy_static_file(
    *,
    source: Path,
    destination: Path,
    runtime: Path,
    locator: str,
    static_sources: dict[str, StaticPayloadSource],
    component: Component | None = None,
    kind: str = "project-file",
) -> None:
    owner = component or OFFERAGENT_COMPONENT
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("release static source is a symlink or special file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError(f"duplicate release static asset: {destination.name}")
    shutil.copy2(source, destination)
    relative = destination.relative_to(runtime).as_posix()
    if relative in static_sources:
        raise RuntimeError(f"duplicate release static provenance: {relative}")
    static_sources[relative] = StaticPayloadSource(owner, source, locator, kind)


def sign_executables(root: Path, args: argparse.Namespace) -> dict[str, AuthenticodeTransform]:
    executables = sorted(root.rglob("*.exe"))
    if not executables:
        raise RuntimeError("release tree contains no executables")
    expected_machine = windows_pe_machine_for_architecture(args.architecture)
    transforms: dict[str, AuthenticodeTransform] = {}
    verifier = WindowsAuthenticodeVerifier()
    for executable in executables:
        if pe_machine(executable) != expected_machine:
            raise RuntimeError(
                f"refusing to sign PE whose machine differs from windows-{args.architecture}: {executable.name}"
            )
        pre_sign_sha256 = digest_file(executable)
        subprocess.run(
            [
                str(args.sign_tool),
                "sign",
                "/sha1",
                args.certificate_sha1,
                "/fd",
                "SHA256",
                "/tr",
                args.timestamp_url,
                "/td",
                "SHA256",
                str(executable),
            ],
            check=True,
        )
        post_sign_sha256 = digest_file(executable)
        if pre_sign_sha256 == post_sign_sha256:
            raise RuntimeError(f"Authenticode signing did not transform executable bytes: {executable.name}")
        if pe_machine(executable) != expected_machine:
            raise RuntimeError(f"Authenticode signing changed PE machine: {executable.name}")
        if not verifier.verify(executable):
            raise RuntimeError(f"Authenticode verification failed: {executable.name}")
        relative = executable.relative_to(root).as_posix()
        transforms[relative] = AuthenticodeTransform(
            relative,
            pre_sign_sha256,
            post_sign_sha256,
            expected_machine,
            True,
        )
    return transforms


def assemble_payload(runtime: Path, bootstrap: Path, output: Path, args: argparse.Namespace) -> None:
    private = load_private_key(args.ed25519_private_key, args.key_id)
    runtime_records = tuple(records_for(runtime))
    privilege_envelope = build_privilege_envelope_from_process_catalog((runtime / PROCESS_CATALOG_PATH).read_bytes())
    bootstrap_records = tuple(records_for(bootstrap, exclude={"offeragent-bootstrap.exe"}))
    bootstrap_exe = bootstrap / "offeragent-bootstrap.exe"
    maximum = sum(record.byte_length for record in runtime_records) + 32 * 1024 * 1024
    manifest = RuntimeReleaseManifest(
        runtime_version=args.runtime_version,
        core_version=args.runtime_version,
        plugin_minimum_version=args.plugin_minimum_version,
        plugin_maximum_version=args.plugin_maximum_version,
        signing_key_id=args.key_id,
        build_commit=args.build_commit,
        created_at=datetime.fromtimestamp(args.source_date_epoch, tz=timezone.utc),
        platform=RuntimePlatform("windows", args.architecture, 19_045),
        protocol=ProtocolCompatibility(PROTOCOL_VERSION, PROTOCOL_VERSION, schema_hash()),
        state_schema_version=LATEST_SCHEMA_VERSION,
        tool_abi_version="1",
        archive=RuntimeArchive(
            f"offeragent-runtime-{args.runtime_version}-windows-{args.architecture}.zip",
            runtime_content_digest(runtime_records),
            maximum,
        ),
        bootstrap=BootstrapRecord(
            "offeragent-bootstrap.exe",
            bootstrap_exe.stat().st_size,
            digest_file(bootstrap_exe),
            True,
            bootstrap_records,
        ),
        files=runtime_records,
        capabilities=("offline_install", "safe_update", "windows_named_pipe"),
        privilege_envelope=privilege_envelope,
        schema_version=2,
    )
    manifest_bytes = canonical_manifest_bytes(manifest)
    signature = encode_signature(private.sign(manifest_bytes))
    (runtime / "runtime-manifest.json").write_bytes(manifest_bytes)
    (runtime / "runtime-manifest.sig").write_bytes(signature)
    archive = output / manifest.archive.file_name
    deterministic_zip(runtime, archive, args.source_date_epoch)
    payload = output / "plugin-runtime" / f"windows-{args.architecture}"
    payload.mkdir(parents=True)
    shutil.copy2(archive, payload / archive.name)
    shutil.copy2(bootstrap_exe, payload / bootstrap_exe.name)
    for record in bootstrap_records:
        target = payload / Path(record.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bootstrap / Path(record.path), target)
    (payload / "runtime-manifest.json").write_bytes(manifest_bytes)
    (payload / "runtime-manifest.sig").write_bytes(signature)
    shutil.copytree(payload, output / "setup-payload", dirs_exist_ok=True)


def build_plugin_package(output: Path, args: argparse.Namespace) -> None:
    plugin_work = output / ".plugin-build"
    plugin_work.mkdir()
    plugin_bundle = plugin_work / "main.production.js"
    subprocess.run(
        ["npm.cmd", "run", "build", "--", f"--outfile={plugin_bundle}"],
        cwd=PLUGIN,
        check=True,
    )
    plugin_bundle_bytes = plugin_bundle.read_bytes()
    if LOCAL_DEVELOPMENT_MARKER in plugin_bundle_bytes or LOCAL_DEVELOPMENT_ANCHOR_TOKEN in plugin_bundle_bytes:
        raise RuntimeError("production plugin bundle contains the local-development installer marker")
    package = output / f"OfferAgent-Obsidian-Windows-{args.architecture}"
    package.mkdir()
    plugin_sources = {
        "main.js": plugin_bundle,
        "manifest.json": PLUGIN / "manifest.json",
        "styles.css": PLUGIN / "styles.css",
    }
    for name, source in plugin_sources.items():
        if not source.is_file():
            raise RuntimeError(f"Obsidian build output is missing {name}")
        shutil.copy2(source, package / name)
    shutil.copytree(output / "plugin-runtime", package / "runtime")
    offline = output / f"OfferAgent-Obsidian-Windows-{args.architecture}-{args.runtime_version}.zip"
    deterministic_zip(package, offline, args.source_date_epoch)
    shutil.copytree(package, output / "setup-payload" / "offeragent-obsidian-plugin", dirs_exist_ok=True)
    shutil.rmtree(plugin_work)


def records_for(root: Path, *, exclude: set[str] | None = None) -> list[RuntimeFileRecord]:
    records: list[RuntimeFileRecord] = []
    excluded = exclude or set()
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        lowered = relative.casefold()
        kind = "runtime"
        if lowered == "process-catalog.v1.json":
            kind = "asset"
        elif lowered.startswith("skills/"):
            kind = "skill"
        elif lowered.endswith(".exe"):
            kind = "executable"
        elif lowered.startswith("web/"):
            kind = "web"
        elif lowered.startswith("licenses/"):
            kind = "license"
        elif lowered.startswith("sbom/"):
            kind = "sbom"
        elif lowered.startswith("provenance/"):
            kind = "provenance"
        records.append(
            RuntimeFileRecord(relative, path.stat().st_size, digest_file(path), kind, lowered.endswith(".exe"))
        )
    return records


def deterministic_zip(source: Path, destination: Path, epoch: int) -> None:
    timestamp = datetime.fromtimestamp(max(epoch, 315532800), tz=timezone.utc)
    zip_time = (timestamp.year, timestamp.month, timestamp.day, timestamp.hour, timestamp.minute, timestamp.second)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(
            (item for item in source.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(source).as_posix(),
        ):
            info = zipfile.ZipInfo(path.relative_to(source).as_posix(), date_time=zip_time)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())


def audit_release(output: Path, args: argparse.Namespace) -> None:
    platform_root = output / "plugin-runtime" / f"windows-{args.architecture}"
    audit_runtime_archive(platform_root)
    forbidden = [
        path for path in platform_root.rglob("*") if any(part.casefold() in FORBIDDEN_IMPORTS for part in path.parts)
    ]
    if forbidden:
        raise RuntimeError("forbidden server dependency entered release payload")
    for executable in platform_root.rglob("*.exe"):
        if not WindowsAuthenticodeVerifier().verify(executable):
            raise RuntimeError(f"release audit found unsigned executable: {executable.name}")
        if pe_machine(executable) != windows_pe_machine_for_architecture(args.architecture):
            raise RuntimeError(f"release audit found PE that is not native {args.architecture}: {executable.name}")
    if not (output / f"OfferAgent-Obsidian-Windows-{args.architecture}-{args.runtime_version}.zip").is_file():
        raise RuntimeError("offline release ZIP is missing")
    packaged_main = output / f"OfferAgent-Obsidian-Windows-{args.architecture}" / "main.js"
    packaged_main_bytes = packaged_main.read_bytes() if packaged_main.is_file() else b""
    if (
        not packaged_main.is_file()
        or LOCAL_DEVELOPMENT_MARKER in packaged_main_bytes
        or LOCAL_DEVELOPMENT_ANCHOR_TOKEN in packaged_main_bytes
    ):
        raise RuntimeError("formal release contains a local-development plugin bundle")


def audit_runtime_archive(platform_root: Path) -> None:
    """Prove the signed manifest, ZIP, provenance and file-level SPDX are exact peers."""

    manifest = parse_manifest((platform_root / "runtime-manifest.json").read_bytes())
    archive_path = platform_root / manifest.archive.file_name
    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = tuple(item for item in archive.infolist() if not item.is_dir())
        actual_names = tuple(sorted(item.filename for item in infos))
        expected_names = tuple(
            sorted((*[record.path for record in manifest.files], "runtime-manifest.json", "runtime-manifest.sig"))
        )
        if actual_names != expected_names:
            raise RuntimeError("release ZIP file set differs from signed manifest")
        by_name = {item.filename: item for item in infos}
        if archive.read("runtime-manifest.json") != (platform_root / "runtime-manifest.json").read_bytes():
            raise RuntimeError("release ZIP and outer signed manifest differ")
        if archive.read("runtime-manifest.sig") != (platform_root / "runtime-manifest.sig").read_bytes():
            raise RuntimeError("release ZIP and outer manifest signature differ")
        for record in manifest.files:
            info = by_name[record.path]
            if info.file_size != record.byte_length:
                raise RuntimeError(f"release ZIP length differs from signed manifest: {record.path}")
            with archive.open(info, "r") as stream:
                hasher = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    hasher.update(chunk)
                digest = f"sha256:{hasher.hexdigest()}"
            if digest != record.sha256:
                raise RuntimeError(f"release ZIP hash differs from signed manifest: {record.path}")
        catalog_record = next((record for record in manifest.files if record.path == PROCESS_CATALOG_PATH), None)
        if catalog_record is None or catalog_record.kind != "asset":
            raise RuntimeError("signed manifest has no canonical Process catalog asset")
        try:
            catalog_payload = archive.read(catalog_record.path)
            validate_process_catalog_payload(catalog_payload)
        except (RuntimeError, TypeError, ValueError) as error:
            raise RuntimeError("signed Process catalog asset is invalid") from error
        rebuilt_privileges = build_privilege_envelope_from_process_catalog(catalog_payload)
        if manifest.privilege_envelope is None or privilege_envelope_payload(
            rebuilt_privileges
        ) != privilege_envelope_payload(manifest.privilege_envelope):
            raise RuntimeError("signed privilege envelope differs from canonical Process catalog")
        skill_records = tuple(record for record in manifest.files if record.kind == "skill")
        if not skill_records or any(not record.path.casefold().startswith("skills/") for record in skill_records):
            raise RuntimeError("signed manifest does not contain a bounded Builtin Skill tree")
        if any(record.kind != "skill" for record in manifest.files if record.path.casefold().startswith("skills/")):
            raise RuntimeError("signed manifest misclassifies a Builtin Skill file")
        sbom_record = next((record for record in manifest.files if record.path == SPDX_PATH), None)
        provenance_record = next((record for record in manifest.files if record.path == PROVENANCE_PATH), None)
        if sbom_record is None or sbom_record.kind != "sbom":
            raise RuntimeError("signed manifest has no Runtime SBOM")
        if provenance_record is None or provenance_record.kind != "provenance":
            raise RuntimeError("signed manifest has no frozen payload provenance")
        try:
            sbom_payload = archive.read(sbom_record.path)
            provenance_payload = archive.read(provenance_record.path)
            sbom = json.loads(sbom_payload.decode("utf-8", errors="strict"))
            provenance = json.loads(provenance_payload.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Runtime SBOM or payload provenance is malformed") from error
        if canonical_json(sbom) != sbom_payload or canonical_json(provenance) != provenance_payload:
            raise RuntimeError("Runtime SBOM or payload provenance is not canonical JSON")
        provenance_expected = {
            record.path: (record.byte_length, record.sha256)
            for record in manifest.files
            if record.path not in {SPDX_PATH, PROVENANCE_PATH}
        }
        expected_executables = {record.path for record in manifest.files if record.authenticode}
        if not isinstance(provenance, dict) or (
            provenance.get("architecture") != manifest.platform.architecture
            or provenance.get("buildCommit") != manifest.build_commit
            or provenance.get("runtimeVersion") != manifest.runtime_version
            or provenance.get("sourceDateEpoch") != int(manifest.created_at.timestamp())
        ):
            raise RuntimeError("Runtime payload provenance identity differs from signed manifest")
        ownership = assert_payload_provenance(
            provenance,
            expected_files=provenance_expected,
            expected_executables=expected_executables,
        )
        spdx_expected = {
            record.path: (record.byte_length, record.sha256) for record in manifest.files if record.path != SPDX_PATH
        }
        spdx_ownership = dict(ownership)
        spdx_ownership[PROVENANCE_PATH] = OFFERAGENT_COMPONENT.spdx_id
        packages = sbom.get("packages") if isinstance(sbom, dict) else None
        assert_sbom_packages_allowed(packages)
        provenance_components = provenance.get("components") if isinstance(provenance, dict) else None
        if (
            not isinstance(packages, list)
            or not isinstance(provenance_components, list)
            or {item.get("SPDXID") for item in packages if isinstance(item, dict)}
            != {item.get("id") for item in provenance_components if isinstance(item, dict)}
        ):
            raise RuntimeError("Runtime SPDX packages differ from frozen payload provenance")
        assert_runtime_spdx_document(
            sbom,
            expected_files=spdx_expected,
            expected_ownership=spdx_ownership,
        )
        if any(ownership.get(record.path) != OFFERAGENT_COMPONENT.spdx_id for record in skill_records):
            raise RuntimeError("Runtime provenance does not assign Builtin Skills to OfferAgent")


def build_setup(output: Path, args: argparse.Namespace) -> None:
    if not args.iscc.is_file():
        raise RuntimeError("Inno Setup compiler is unavailable")
    plugin_manifest_path = output / "setup-payload" / "offeragent-obsidian-plugin" / "manifest.json"
    try:
        plugin_manifest = json.loads(plugin_manifest_path.read_text(encoding="utf-8", errors="strict"))
        plugin_version = plugin_manifest["version"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError("Setup plugin manifest has no valid version identity") from error
    if not isinstance(plugin_version, str) or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", plugin_version) is None:
        raise RuntimeError("Setup plugin manifest version is invalid")
    subprocess.run(
        [
            str(args.iscc),
            f"/DReleaseRoot={output}",
            f"/DRuntimeVersion={args.runtime_version}",
            f"/DPluginVersion={plugin_version}",
            f"/DRuntimeArchitecture={args.architecture}",
            str(ROOT / "packaging" / "OfferAgent.iss"),
        ],
        check=True,
    )
    installer = output / f"OfferAgent-for-Obsidian-Setup-{args.architecture}.exe"
    if not installer.is_file():
        raise RuntimeError("Inno Setup did not produce the expected installer")
    subprocess.run(
        [
            str(args.sign_tool),
            "sign",
            "/sha1",
            args.certificate_sha1,
            "/fd",
            "SHA256",
            "/tr",
            args.timestamp_url,
            "/td",
            "SHA256",
            str(installer),
        ],
        check=True,
    )
    if not WindowsAuthenticodeVerifier().verify(installer):
        raise RuntimeError("Setup Authenticode verification failed")


def load_private_key(path: Path, key_id: str) -> Ed25519PrivateKey:
    raw = path.read_bytes()
    key = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("release key is not Ed25519")
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    encoded = base64.urlsafe_b64encode(public).rstrip(b"=").decode("ascii")
    if encoded != PUBLIC_KEYS_BASE64URL[key_id]:
        raise RuntimeError("external private key does not match generated public keyring")
    return key


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def pe_machine(path: Path) -> int:
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            return 0
        stream.seek(int.from_bytes(header[60:64], "little"))
        coff = stream.read(6)
    return int.from_bytes(coff[4:6], "little") if coff[:4] == b"PE\0\0" else 0


if __name__ == "__main__":
    raise SystemExit(main())
