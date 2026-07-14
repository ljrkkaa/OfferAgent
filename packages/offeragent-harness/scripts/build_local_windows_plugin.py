"""Build one unsigned, hash-pinned Windows x64 plugin for personal use.

This command is intentionally separate from ``build_windows_release.py``.  It
cannot emit a release ZIP, setup program, signature, keyring, or Authenticode
claim.  The resulting plugin bundle carries a mandatory development-only
manifest and is accepted only by the compile-time local plugin installer.
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
from pathlib import Path

from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.runtime.development_runtime_manifest import (
    DEVELOPMENT_MANIFEST_NAME,
    DevelopmentBuildIdentity,
    DevelopmentRuntimeManifest,
    canonical_development_manifest_bytes,
    development_runtime_content_digest,
)
from offeragent_harness.runtime.production_process_catalog import (
    PROCESS_CATALOG_PATH,
    validate_process_catalog_payload,
)
from offeragent_harness.runtime.release_manifest import (
    ProtocolCompatibility,
    RuntimeFileRecord,
    native_windows_architecture,
    windows_pe_machine_for_architecture,
)
from offeragent_harness.storage.migrations import LATEST_SCHEMA_VERSION

try:
    from scripts.frozen_payload_provenance import (
        FrozenRuntimeEvidence,
        SourceClassifier,
        capture_pyinstaller_target,
        merge_frozen_evidence,
    )
except ModuleNotFoundError:
    from frozen_payload_provenance import (  # type: ignore[import-not-found,no-redef]
        FrozenRuntimeEvidence,
        SourceClassifier,
        capture_pyinstaller_target,
        merge_frozen_evidence,
    )

try:
    from scripts.build_windows_release import (
        REQUIRED_EXES,
        add_release_assets,
        build_one_onedir,
        merge_identical_tree,
        pe_machine,
    )
except ModuleNotFoundError:
    from build_windows_release import (  # type: ignore[import-not-found,no-redef]
        REQUIRED_EXES,
        add_release_assets,
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
    "offeragent_harness.subagents.write_coordinator",
)
DEVELOPMENT_EXCLUDED_MODULES = (
    "_pytest",
    "django",
    "hypothesis",
    "khoj",
    "mypy",
    "offeragent_harness._release_keys",
    "offeragent_harness.cli",
    "offeragent_harness.migration",
    "offeragent_harness.runtime.bootstrap_cli",
    "offeragent_harness.testing",
    "psycopg",
    "pytest",
)
_FORBIDDEN_FROZEN_PROJECT_PREFIXES = (
    "project:src/offeragent_harness/testing/",
    "project:src/offeragent_harness/migration/",
    "project:src/offeragent_harness/cli.py",
    "project:src/offeragent_harness/runtime/bootstrap_cli.py",
    "project:src/offeragent_harness/_release_keys.py",
)
_FORBIDDEN_FROZEN_DISTRIBUTIONS = (
    "python-distribution:django/",
    "python-distribution:hypothesis/",
    "python-distribution:mypy/",
    "python-distribution:psycopg/",
    "python-distribution:pytest/",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建个人本机开发版 OfferAgent 插件 (仅 Windows x64)")
    parser.add_argument("--output", type=Path, required=True, help="不存在的输出目录")
    parser.add_argument("--ripgrep-executable", type=Path, required=True, help="构建时显式提供的 rg.exe")
    parser.add_argument("--runtime-version", help="可选; 默认由源码指纹生成")
    parser.add_argument("--skip-static-checks", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    require_local_build_host(args.ripgrep_executable)
    output = args.output.resolve(strict=False)
    if output.exists():
        raise SystemExit("output already exists; local build never overwrites an existing directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not args.skip_static_checks:
        run_static_gates()
    commit = _git_output("rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise SystemExit("Git HEAD is not a canonical commit identity")
    source_digest = source_tree_digest()
    runtime_version = args.runtime_version or f"0.1.0-local.{source_digest.removeprefix('sha256:')[:16]}"
    plugin_version = _plugin_version()
    with tempfile.TemporaryDirectory(prefix="offeragent-local-build-", dir=output.parent) as temporary:
        temporary_root = Path(temporary)
        runtime = build_development_runtime(temporary_root / "runtime-build")
        add_release_assets(
            runtime,
            architecture="x64",
            ripgrep_executable=args.ripgrep_executable,
        )
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
        if b"release keyring is empty" in bundle_bytes:
            raise RuntimeError("local plugin bundle unexpectedly contains the production installer")
        if source_tree_digest() != source_digest:
            raise RuntimeError("source tree changed during the local build; refusing a mixed-identity artifact")
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
        os.replace(staging, output)
    print(output)
    return 0


def require_local_build_host(ripgrep_executable: Path) -> None:
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
    subprocess.run([sys.executable, "scripts/build_web_assets.py", "check"], cwd=ROOT, check=True)


def build_development_runtime(destination: Path) -> Path:
    specifications = (
        (DEVELOPMENT_ENTRYPOINTS / "offeragent_host.py", "offeragent-host", destination / "host"),
        (DEVELOPMENT_ENTRYPOINTS / "offeragent_worker.py", "offeragent-worker", destination / "worker"),
        (DEVELOPMENT_ENTRYPOINTS / "offeragent_self_test.py", "offeragent-self-test", destination / "self-test"),
        (
            DEVELOPMENT_ENTRYPOINTS / "offeragent_process_host.py",
            "offeragent-process-host",
            destination / "process-host",
        ),
    )
    classifier = SourceClassifier(project_root=ROOT)
    roots: list[Path] = []
    target_evidence: list[FrozenRuntimeEvidence] = []
    for entrypoint, name, target in specifications:
        root = build_one_onedir(
            entrypoint,
            name,
            target,
            hidden_imports=DEVELOPMENT_HIDDEN_IMPORTS,
            excluded_modules=DEVELOPMENT_EXCLUDED_MODULES,
            add_data=(
                (ROOT / "web", "offeragent_harness/_web"),
                (ROOT / "schema", "offeragent_harness/_schema"),
            ),
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
    audit_development_frozen_evidence(evidence)
    audit_development_pyinstaller_archives(merged)
    actual = {path.name for path in merged.glob("*.exe")}
    if not set(REQUIRED_EXES) <= actual:
        raise RuntimeError("development PyInstaller output is missing a required executable")
    expected_machine = windows_pe_machine_for_architecture("x64")
    for executable in sorted(merged.glob("*.exe")):
        if pe_machine(executable) != expected_machine:
            raise RuntimeError(f"development executable is not native x64: {executable.name}")
    return merged


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
        "offeragent_harness._release_keys",
        "offeragent_harness.cli",
        "offeragent_harness.migration",
        "offeragent_harness.runtime.bootstrap_cli",
        "offeragent_harness.testing",
        "psycopg",
        "pytest",
    )
    for executable_name in REQUIRED_EXES:
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
        missing = sorted(set(DEVELOPMENT_HIDDEN_IMPORTS) - set(module_names))
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
        elif relative.startswith("web/"):
            kind = "web"
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


def source_tree_digest() -> str:
    roots = (
        ROOT / "src" / "offeragent_harness",
        ROOT / "scripts" / "entrypoints" / "development",
        ROOT / "packaging",
        ROOT / "web",
        PLUGIN / "src",
        PLUGIN / "scripts",
    )
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            raise RuntimeError(f"source identity root is missing: {root}")
        files.extend(path for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    files.extend(
        (
            ROOT / "pyproject.toml",
            ROOT / "uv.lock",
            ROOT / "LICENSE",
            ROOT / "scripts" / "build_local_windows_plugin.py",
            ROOT / "scripts" / "build_windows_release.py",
            PLUGIN / "package.json",
            PLUGIN / "esbuild.config.mjs",
            PLUGIN / "manifest.json",
            PLUGIN / "styles.css",
        )
    )
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for path in sorted(files):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("source identity contains a symlink or missing file")
        relative = path.relative_to(REPO).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        entries.append({"path": relative, "sha256": _digest_file(path), "size": path.stat().st_size})
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
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _digest_bytes(payload: bytes) -> str:
    result = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if _SHA256.fullmatch(result) is None:
        raise AssertionError("SHA-256 encoding invariant failed")
    return result


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
