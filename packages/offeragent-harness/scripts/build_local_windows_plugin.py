"""Build the one hash-pinned Windows x64 plugin for personal use.

The resulting plugin bundle carries a mandatory development-only manifest and
is accepted only by the compile-time local plugin installer.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.runtime.development_runtime_manifest import (
    DEVELOPMENT_MANIFEST_NAME,
    DevelopmentBuildIdentity,
    DevelopmentRuntimeManifest,
    canonical_development_manifest_bytes,
    development_runtime_content_digest,
)
from offeragent_harness.runtime.local_process_catalog import (
    PROCESS_CATALOG_PATH,
    validate_process_catalog_payload,
)
from offeragent_harness.runtime.runtime_manifest import (
    ProtocolCompatibility,
    RuntimeFileRecord,
    native_windows_architecture,
)
from offeragent_harness.storage.migrations import LATEST_SCHEMA_VERSION

try:
    from scripts.frozen_payload import (
        FrozenRuntimeEvidence,
        SourceClassifier,
        capture_pyinstaller_target,
        merge_frozen_evidence,
        verify_project_source_snapshot,
    )
except ModuleNotFoundError:
    from frozen_payload import (  # type: ignore[import-not-found,no-redef]
        FrozenRuntimeEvidence,
        SourceClassifier,
        capture_pyinstaller_target,
        merge_frozen_evidence,
        verify_project_source_snapshot,
    )

try:
    from scripts.local_windows_runtime_build import (
        WINDOWS_X64_PE_MACHINE,
        add_local_assets,
        build_one_onedir,
        merge_identical_tree,
        pe_machine,
    )
except ModuleNotFoundError:
    from local_windows_runtime_build import (  # type: ignore[import-not-found,no-redef]
        WINDOWS_X64_PE_MACHINE,
        add_local_assets,
        build_one_onedir,
        merge_identical_tree,
        pe_machine,
    )

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
PLUGIN = REPO / "src" / "interface" / "obsidian"
DEVELOPMENT_ENTRYPOINTS = ROOT / "scripts" / "entrypoints" / "development"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
DEVELOPMENT_HIDDEN_IMPORTS = (
    "offeragent_harness.subagents.artifacts",
    "offeragent_harness.subagents.authority",
    "offeragent_harness.subagents.budget",
    "offeragent_harness.subagents.catalog",
    "offeragent_harness.subagents.context",
    "offeragent_harness.subagents.definitions",
    "offeragent_harness.subagents.lifecycle",
    "offeragent_harness.subagents.mailbox",
    "offeragent_harness.subagents.models",
    "offeragent_harness.subagents.policy",
    "offeragent_harness.subagents.recovery",
    "offeragent_harness.subagents.reducer",
    "offeragent_harness.subagents.scheduler",
    "offeragent_harness.subagents.service",
    "offeragent_harness.subagents.tools",
)
DOCUMENT_PARSER_HIDDEN_IMPORTS = (
    "PIL.Image",
    "onnxruntime",
    "pymupdf",
    "rapidocr",
)
DOCUMENT_PARSER_MODEL_FILES = (
    "PP-OCRv6_det_small.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "PP-OCRv6_rec_small.onnx",
)
DOCUMENT_PARSER_CUDA_DLLS = {
    "nvidia.cublas": ("cublas64_12.dll", "cublasLt64_12.dll"),
    "nvidia.cuda_runtime": ("cudart64_12.dll",),
    "nvidia.cudnn": (
        "cudnn64_9.dll",
        "cudnn_adv64_9.dll",
        "cudnn_cnn64_9.dll",
        "cudnn_engines_precompiled64_9.dll",
        "cudnn_engines_runtime_compiled64_9.dll",
        "cudnn_engines_tensor_ir64_9.dll",
        "cudnn_ext64_9.dll",
        "cudnn_graph64_9.dll",
        "cudnn_heuristic64_9.dll",
        "cudnn_ops64_9.dll",
    ),
    "nvidia.cufft": ("cufft64_11.dll",),
}
DEVELOPMENT_EXCLUDED_MODULES = (
    "_pytest",
    "django",
    "hypothesis",
    "khoj",
    "mypy",
    "numpy.testing",
    "offeragent_harness.cli",
    "offeragent_harness.migration",
    "offeragent_harness.testing",
    "psycopg",
    "pytest",
)
LOCAL_RUNTIME_EXES = (
    "offeragent-process-host.exe",
    "offeragent-worker.exe",
)
_FORBIDDEN_FROZEN_PROJECT_PREFIXES = (
    "project:src/offeragent_harness/testing/",
    "project:src/offeragent_harness/migration/",
    "project:src/offeragent_harness/cli.py",
)
_FORBIDDEN_FROZEN_DISTRIBUTIONS = (
    "python-distribution:django/",
    "python-distribution:hypothesis/",
    "python-distribution:mypy/",
    "python-distribution:psycopg/",
    "python-distribution:pytest/",
)


@dataclass(frozen=True)
class SourceTreeIdentity:
    source_tree_sha256: str
    schema_tree_sha256: str
    project_sources: tuple[tuple[str, str], ...]


def main() -> int:
    parser = argparse.ArgumentParser(description="构建个人本机开发版 OfferAgent 插件 (仅 Windows x64)")
    parser.add_argument("--output", type=Path, required=True, help="不存在的输出目录")
    parser.add_argument("--ripgrep-executable", type=Path, required=True, help="构建时显式提供的 rg.exe")
    parser.add_argument("--runtime-version", help="可选; 默认由源码指纹生成")
    args = parser.parse_args()
    require_local_build_host(args.ripgrep_executable)
    output = args.output.resolve(strict=False)
    if output.exists():
        raise SystemExit("output already exists; local build never overwrites an existing directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    run_static_gates()
    commit = _git_output("rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise SystemExit("Git HEAD is not a canonical commit identity")
    source_identity = source_tree_identity()
    source_digest = source_identity.source_tree_sha256
    runtime_version = args.runtime_version or f"0.1.0-local.{source_digest.removeprefix('sha256:')[:16]}"
    plugin_version = _plugin_version()
    with tempfile.TemporaryDirectory(prefix="offeragent-local-build-", dir=output.parent) as temporary:
        temporary_root = Path(temporary)
        runtime = build_development_runtime(
            temporary_root / "runtime-build",
            project_source_snapshot=dict(source_identity.project_sources),
        )
        _require_embedded_schema_identity(runtime, source_identity.schema_tree_sha256)
        add_local_assets(runtime, ripgrep_executable=args.ripgrep_executable)
        validate_process_catalog_payload((runtime / PROCESS_CATALOG_PATH).read_bytes())
        records = collect_runtime_records(runtime)
        manifest = DevelopmentRuntimeManifest(
            runtime_version=runtime_version,
            core_version=runtime_version,
            plugin_version=plugin_version,
            build=DevelopmentBuildIdentity(commit, source_digest),
            protocol=ProtocolCompatibility(PROTOCOL_VERSION, PROTOCOL_VERSION, schema_hash()),
            state_schema_version=LATEST_SCHEMA_VERSION,
            tool_abi_version="1",
            runtime_content_sha256=development_runtime_content_digest(records),
            files=records,
        )
        manifest_bytes = canonical_development_manifest_bytes(manifest)
        (runtime / DEVELOPMENT_MANIFEST_NAME).write_bytes(manifest_bytes)
        plugin_bundle = temporary_root / "offeragent-main.local-development.js"
        manifest_sha256 = _digest_bytes(manifest_bytes)
        subprocess.run(
            [
                _npm_command(),
                "run",
                "build:local",
                "--",
                f"--outfile={plugin_bundle}",
                f"--development-manifest-sha256={manifest_sha256}",
            ],
            cwd=PLUGIN,
            check=True,
        )
        bundle_bytes = plugin_bundle.read_bytes()
        if b"OFFERAGENT_LOCAL_DEVELOPMENT_RUNTIME_V1" not in bundle_bytes:
            raise RuntimeError("local plugin bundle lacks its compile-time development marker")
        if (
            manifest_sha256.encode("ascii") not in bundle_bytes
            or b"__OFFERAGENT_DEVELOPMENT_MANIFEST_SHA256__" in bundle_bytes
        ):
            raise RuntimeError("local plugin bundle lacks its compiled Runtime manifest anchor")
        staging = temporary_root / "offeragent-obsidian-plugin"
        staging.mkdir()
        plugin_sources = {
            "main.js": plugin_bundle,
            "manifest.json": PLUGIN / "manifest.json",
            "styles.css": PLUGIN / "styles.css",
        }
        for name, source in plugin_sources.items():
            if not source.is_file() or source.is_symlink():
                raise RuntimeError(f"local plugin build is missing {name}")
            shutil.copy2(source, staging / name)
        runtime_target = staging / "runtime" / "windows-x64" / "local-development"
        shutil.copytree(runtime, runtime_target)
        receipt = {
            "developmentOnly": True,
            "manifestSha256": manifest_sha256,
            "pluginVersion": plugin_version,
            "runtimeVersion": runtime_version,
            "schemaVersion": 1,
            "sourceTreeSha256": source_digest,
        }
        (staging / "local-development-build.json").write_bytes(_canonical_json(receipt) + b"\n")
        _require_embedded_schema_identity(runtime, source_identity.schema_tree_sha256)
        require_source_tree_unchanged(source_identity)
        os.replace(staging, output)
    print(output)
    return 0


def require_local_build_host(ripgrep_executable: Path) -> None:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("local development build requires CPython 3.12")
    try:
        architecture = native_windows_architecture()
    except RuntimeError as error:
        raise SystemExit(f"local development build requires native Windows x64: {error}") from error
    if architecture != "x64":
        raise SystemExit("local development build supports only native Windows x64")
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit("PyInstaller build dependency is unavailable")
    if shutil.which("npm.cmd") is None and shutil.which("npm") is None:
        raise SystemExit("npm is unavailable")
    if not ripgrep_executable.is_file() or ripgrep_executable.name.casefold() != "rg.exe":
        raise SystemExit("a concrete rg.exe build input is required")


def run_static_gates() -> None:
    for script in (
        "audit_repository_closure.py",
        "check_forbidden_dependencies.py",
        "check_architecture.py",
    ):
        subprocess.run([sys.executable, f"scripts/{script}"], cwd=ROOT, check=True)
    subprocess.run(
        [sys.executable, "-m", "offeragent_harness.protocol.schemas", "check"],
        cwd=ROOT,
        check=True,
    )


def build_development_runtime(
    destination: Path,
    *,
    project_source_snapshot: Mapping[str, str],
) -> Path:
    common_data = ((ROOT / "schema", "offeragent_harness/_schema"),)
    parser_data = _document_parser_add_data()
    parser_binaries = _document_parser_add_binaries()
    specifications = (
        (
            DEVELOPMENT_ENTRYPOINTS / "offeragent_worker.py",
            "offeragent-worker",
            destination / "worker",
            DEVELOPMENT_HIDDEN_IMPORTS,
            common_data,
            (),
        ),
        (
            DEVELOPMENT_ENTRYPOINTS / "offeragent_process_host.py",
            "offeragent-process-host",
            destination / "process-host",
            DEVELOPMENT_HIDDEN_IMPORTS + DOCUMENT_PARSER_HIDDEN_IMPORTS,
            common_data + parser_data,
            parser_binaries,
        ),
    )
    classifier = SourceClassifier(project_root=ROOT)
    roots: list[Path] = []
    target_evidence: list[FrozenRuntimeEvidence] = []
    for entrypoint, name, target, hidden_imports, add_data, add_binaries in specifications:
        root = build_one_onedir(
            entrypoint,
            name,
            target,
            hidden_imports=hidden_imports,
            excluded_modules=DEVELOPMENT_EXCLUDED_MODULES,
            add_data=add_data,
            add_binaries=add_binaries,
        )
        roots.append(root)
        target_evidence.append(
            capture_pyinstaller_target(
                name=name,
                root=root,
                work_root=target / "build" / name,
                classifier=classifier,
            )
        )
    merged = destination / "merged"
    merged.mkdir(parents=True)
    for root in roots:
        merge_identical_tree(root, merged)
    evidence = merge_frozen_evidence(merged_root=merged, targets=target_evidence)
    verify_project_source_snapshot(evidence, project_source_snapshot)
    audit_development_frozen_evidence(evidence)
    audit_development_pyinstaller_archives(merged)
    _require_document_parser_payload(merged)
    _require_exact_root_executables(merged)
    expected_machine = WINDOWS_X64_PE_MACHINE
    for executable in sorted(merged.glob("*.exe")):
        if pe_machine(executable) != expected_machine:
            raise RuntimeError(f"development executable is not native x64: {executable.name}")
    return merged


def _document_parser_add_data() -> tuple[tuple[Path, str], ...]:
    spec = importlib.util.find_spec("rapidocr")
    if spec is None or spec.origin is None:
        raise RuntimeError("the pinned RapidOCR build dependency is unavailable")
    try:
        package_root = Path(spec.origin).resolve(strict=True).parent
    except OSError as error:
        raise RuntimeError("the pinned RapidOCR package root is unavailable") from error
    relative_sources = (
        ("config.yaml", "rapidocr"),
        ("default_models.yaml", "rapidocr"),
        *((f"models/{name}", "rapidocr/models") for name in DOCUMENT_PARSER_MODEL_FILES),
    )
    result: list[tuple[Path, str]] = []
    for relative, target in relative_sources:
        source = package_root.joinpath(*relative.split("/"))
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(package_root)
        except (OSError, ValueError) as error:
            raise RuntimeError("a required RapidOCR parser asset is unavailable") from error
        if source.is_symlink() or not resolved.is_file():
            raise RuntimeError("a required RapidOCR parser asset is not a regular local file")
        result.append((resolved, target))
    return tuple(result)


def _document_parser_add_binaries() -> tuple[tuple[Path, str], ...]:
    result: list[tuple[Path, str]] = []
    for package, filenames in DOCUMENT_PARSER_CUDA_DLLS.items():
        spec = importlib.util.find_spec(package)
        locations = tuple(spec.submodule_search_locations or ()) if spec is not None else ()
        if len(locations) != 1:
            raise RuntimeError(f"the pinned CUDA binary package is unavailable: {package}")
        package_root = Path(locations[0]).resolve(strict=True)
        binary_root = (package_root / "bin").resolve(strict=True)
        binary_root.relative_to(package_root)
        target = f"{package.replace('.', '/')}/bin"
        for filename in filenames:
            source = (binary_root / filename).resolve(strict=True)
            try:
                source.relative_to(binary_root)
            except ValueError as error:
                raise RuntimeError("a required CUDA DLL escapes its package") from error
            if source.is_symlink() or not source.is_file():
                raise RuntimeError(f"a required CUDA DLL is unavailable: {filename}")
            result.append((source, target))
    return tuple(result)


def _require_document_parser_payload(runtime: Path) -> None:
    package_root = runtime / "_internal" / "rapidocr"
    required = {
        package_root / "config.yaml",
        package_root / "default_models.yaml",
        *(package_root / "models" / name for name in DOCUMENT_PARSER_MODEL_FILES),
    }
    if any(path.is_symlink() or not path.is_file() for path in required):
        raise RuntimeError("development Runtime lacks the explicit bundled document parser assets")
    models_root = package_root / "models"
    actual_models = set(models_root.rglob("*"))
    expected_models = {package_root / "models" / name for name in DOCUMENT_PARSER_MODEL_FILES}
    if actual_models != expected_models or any(path.is_symlink() or not path.is_file() for path in actual_models):
        raise RuntimeError("development Runtime document parser model set is not exact")
    internal = runtime / "_internal"
    for package, filenames in DOCUMENT_PARSER_CUDA_DLLS.items():
        binary_root = internal.joinpath(*package.split("."), "bin")
        expected = {binary_root / name for name in filenames}
        actual = set(binary_root.rglob("*")) if binary_root.is_dir() else set()
        if actual != expected or any(path.is_symlink() or not path.is_file() for path in actual):
            raise RuntimeError(f"development Runtime CUDA DLL set is not exact: {package}")


def _require_exact_root_executables(runtime: Path) -> None:
    expected = set(LOCAL_RUNTIME_EXES)
    actual = {path.name for path in runtime.glob("*.exe")}
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise RuntimeError(
            f"development Runtime root executable set differs: missing={missing}, unexpected={unexpected}"
        )


def audit_development_frozen_evidence(evidence: FrozenRuntimeEvidence) -> None:
    """Reject test fakes, migration/CLI entry points, and legacy server code."""

    forbidden = []
    for source in evidence.sources.values():
        locator = source.locator.replace("\\", "/")
        folded = locator.casefold()
        if any(folded.startswith(prefix.casefold()) for prefix in _FORBIDDEN_FROZEN_PROJECT_PREFIXES):
            forbidden.append(locator)
        if any(folded.startswith(prefix) for prefix in _FORBIDDEN_FROZEN_DISTRIBUTIONS):
            forbidden.append(locator)
        if (
            "/src/khoj/" in folded
            or folded.startswith("project:src/khoj/")
            or folded.startswith("project:tests/")
            or "/tests/" in folded
            or "/test_" in folded
            or "fake" in folded
        ):
            forbidden.append(locator)
    if forbidden:
        raise RuntimeError(f"development frozen payload contains forbidden sources: {sorted(set(forbidden))}")


def audit_development_pyinstaller_archives(runtime: Path) -> None:
    """Inspect the actual embedded PYZ names, not merely the source tree/TOCs."""

    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader  # type: ignore[import-untyped]

    forbidden_prefixes = (
        "_pytest",
        "django",
        "hypothesis",
        "khoj",
        "mypy",
        "offeragent_harness.cli",
        "offeragent_harness.migration",
        "offeragent_harness.testing",
        "psycopg",
        "pytest",
    )
    for executable_name in LOCAL_RUNTIME_EXES:
        executable = runtime / executable_name
        archive = CArchiveReader(str(executable))
        pyz_names = [name for name in archive.toc if name.casefold().endswith(".pyz")]
        if pyz_names != ["PYZ.pyz"]:
            raise RuntimeError(f"development executable has an unexpected embedded archive: {executable_name}")
        with tempfile.TemporaryDirectory(prefix="offeragent-pyz-audit-", dir=runtime.parent) as temporary:
            extracted = Path(temporary) / "PYZ.pyz"
            extracted.write_bytes(archive.extract("PYZ.pyz"))
            module_names = tuple(ZlibArchiveReader(str(extracted)).toc)
        forbidden = sorted(
            name
            for name in module_names
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden_prefixes)
            or name.startswith("tests.")
            or ".tests." in name
            or ".fake" in name.casefold()
        )
        if forbidden:
            raise RuntimeError(
                f"development executable archive contains forbidden modules ({executable_name}): {forbidden}"
            )
        required_hidden_imports = set(DEVELOPMENT_HIDDEN_IMPORTS)
        if executable_name == "offeragent-process-host.exe":
            required_hidden_imports.update(DOCUMENT_PARSER_HIDDEN_IMPORTS)
        missing = sorted(required_hidden_imports - set(module_names))
        if missing:
            raise RuntimeError(
                f"development executable archive lacks curated Runtime modules ({executable_name}): {missing}"
            )


def collect_runtime_records(runtime: Path) -> tuple[RuntimeFileRecord, ...]:
    records: list[RuntimeFileRecord] = []
    folded: set[str] = set()
    for path in sorted(runtime.rglob("*"), key=lambda item: item.relative_to(runtime).as_posix()):
        if path.is_dir():
            if path.is_symlink():
                raise RuntimeError("development Runtime contains a directory symlink")
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise RuntimeError("development Runtime contains a symlink, hard link, or special file")
        relative = path.relative_to(runtime).as_posix()
        if relative.casefold() in folded:
            raise RuntimeError("development Runtime paths collide on Windows")
        folded.add(relative.casefold())
        if relative.casefold().endswith(".exe"):
            kind = "executable"
        elif relative.startswith("skills/"):
            kind = "skill"
        elif relative.startswith("LICENSES/"):
            kind = "license"
        else:
            kind = "asset"
        records.append(
            RuntimeFileRecord(
                path=relative,
                byte_length=path.stat().st_size,
                sha256=_digest_file(path),
                kind=kind,
                authenticode=False,
            )
        )
    return tuple(records)


def source_tree_identity() -> SourceTreeIdentity:
    roots = (
        ROOT / "src" / "offeragent_harness",
        ROOT / "scripts" / "entrypoints" / "development",
        ROOT / "packaging",
        ROOT / "schema",
        PLUGIN / "src",
        PLUGIN / "scripts",
    )
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            raise RuntimeError(f"source identity root is missing: {root}")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise RuntimeError("source identity contains a symlink")
            if path.is_file() and (root == ROOT / "schema" or "__pycache__" not in path.parts):
                files.append(path)
    files.extend(
        (
            ROOT / "pyproject.toml",
            ROOT / "uv.lock",
            ROOT / "LICENSE",
            ROOT / "scripts" / "build_local_windows_plugin.py",
            ROOT / "scripts" / "frozen_payload.py",
            ROOT / "scripts" / "local_windows_runtime_build.py",
            ROOT / "scripts" / "runtime_sbom.py",
            PLUGIN / "package.json",
            PLUGIN / "yarn.lock",
            PLUGIN / "esbuild.config.mjs",
            PLUGIN / "manifest.json",
            PLUGIN / "styles.css",
        )
    )
    return _source_tree_identity_from_files(
        files,
        repository_root=REPO,
        schema_root=ROOT / "schema",
        project_root=ROOT,
    )


def source_tree_digest() -> str:
    return source_tree_identity().source_tree_sha256


def require_source_tree_unchanged(expected: SourceTreeIdentity) -> None:
    if source_tree_identity() != expected:
        raise RuntimeError("source tree changed during the local build; refusing a mixed-identity artifact")


def _source_tree_identity_from_files(
    files: Iterable[Path],
    *,
    repository_root: Path,
    schema_root: Path,
    project_root: Path,
) -> SourceTreeIdentity:
    entries: list[dict[str, object]] = []
    schema_entries: list[dict[str, object]] = []
    project_sources: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path in sorted(files):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("source identity contains a symlink or missing file")
        relative = path.relative_to(repository_root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        digest, size = _digest_file_and_size(path)
        entries.append({"path": relative, "sha256": digest, "size": size})
        try:
            schema_relative = path.relative_to(schema_root).as_posix()
        except ValueError:
            pass
        else:
            schema_entries.append({"path": schema_relative, "sha256": digest, "size": size})
        try:
            project_relative = path.relative_to(project_root).as_posix()
        except ValueError:
            pass
        else:
            project_sources.append((f"project:{project_relative}", digest))
    if not schema_entries:
        raise RuntimeError("source identity schema tree is empty")
    return SourceTreeIdentity(
        source_tree_sha256=_digest_bytes(_canonical_json({"files": entries})),
        schema_tree_sha256=_digest_bytes(_canonical_json({"files": schema_entries})),
        project_sources=tuple(project_sources),
    )


def _require_embedded_schema_identity(runtime: Path, expected: str) -> None:
    schema_root = runtime / "_internal" / "offeragent_harness" / "_schema"
    actual = _schema_tree_digest(schema_root)
    if actual != expected:
        raise RuntimeError("frozen Runtime schema differs from the source identity snapshot")


def _schema_tree_digest(schema_root: Path) -> str:
    if schema_root.is_symlink() or not schema_root.is_dir():
        raise RuntimeError("schema identity root is unsafe or missing")
    entries: list[dict[str, object]] = []
    for path in sorted(schema_root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("schema identity contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_nlink != 1:
            raise RuntimeError("schema identity contains a missing, hard-linked, or special file")
        digest, size = _digest_file_and_size(path)
        entries.append(
            {
                "path": path.relative_to(schema_root).as_posix(),
                "sha256": digest,
                "size": size,
            }
        )
    if not entries:
        raise RuntimeError("schema identity tree is empty")
    return _digest_bytes(_canonical_json({"files": entries}))


def _plugin_version() -> str:
    value = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))
    version = value.get("version") if isinstance(value, dict) else None
    if not isinstance(version, str) or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", version) is None:
        raise RuntimeError("plugin manifest version is invalid")
    return version


def _npm_command() -> str:
    result = shutil.which("npm.cmd") or shutil.which("npm")
    if result is None:
        raise RuntimeError("npm is unavailable")
    return result


def _git_output(*arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=REPO, text=True, encoding="utf-8").strip()


def _digest_file(path: Path) -> str:
    return _digest_file_and_size(path)[0]


def _digest_file_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _digest_bytes(payload: bytes) -> str:
    result = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if _SHA256.fullmatch(result) is None:
        raise AssertionError("SHA-256 encoding invariant failed")
    return result


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
