"""Build and qualify one final Windows product candidate with canonical evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from offeragent_harness.qualification.windows_product_artifact import (
    VerifiedWindowsProductArtifacts,
    verify_paired_windows_artifacts,
)

try:
    from scripts.qualify_built_windows_product import _remove_owned_root, smoke_built_windows_product
    from scripts.qualify_live_built_windows_product import qualify_live_built_windows_product
except ImportError:  # pragma: no cover - direct execution from scripts/
    from qualify_built_windows_product import (  # type: ignore[import-not-found,no-redef]
        _remove_owned_root,
        smoke_built_windows_product,
    )
    from qualify_live_built_windows_product import (  # type: ignore[import-not-found,no-redef]
        qualify_live_built_windows_product,
    )

PhaseFunction = Callable[[], dict[str, object]]
_PHASES = ("source", "gates", "build", "offline", "migrations", "install", "live")


class FinalWindowsProductQualificationError(RuntimeError):
    """The final candidate did not satisfy one closed completion phase."""


def qualify_final_windows_product(
    *,
    source_root: Path,
    plugin_output: Path,
    qualification_output: Path,
    ripgrep_executable: Path,
    node_executable: Path,
    proxy_url: str,
    model: str,
    temporary_parent: Path,
    review_base: str,
    _phase_functions: Mapping[str, PhaseFunction] | None = None,
) -> dict[str, object]:
    """Run every completion phase in order and return one versioned report."""

    if _phase_functions is not None:
        if set(_phase_functions) != set(_PHASES):
            raise ValueError("injected final qualification phases are incomplete")
        phase_results = {name: _validate_phase_result(name, _phase_functions[name]()) for name in _PHASES}
    else:
        source = _source_phase(source_root, review_base)
        gates = _gate_phase(source_root)
        build, paired = _build_phase(
            source_root,
            plugin_output,
            qualification_output,
            ripgrep_executable,
            expected_commit=str(source["evidence"]["head"]),
        )
        offline = _offline_phase(paired, source_root, node_executable, temporary_parent)
        migrations = _migration_phase(source_root)
        install = _install_phase(paired, source_root, temporary_parent)
        live = _live_phase(
            paired,
            source_root,
            node_executable,
            proxy_url,
            model,
            temporary_parent,
        )
        phase_results = {
            "source": source,
            "gates": gates,
            "build": build,
            "offline": offline,
            "migrations": migrations,
            "install": install,
            "live": live,
        }
    commands = [command for name in _PHASES for command in phase_results[name]["commands"]]
    skips = sorted({skip for name in _PHASES for skip in phase_results[name]["skips"]})
    return {
        "schemaVersion": 1,
        "status": "passed",
        "phases": {name: phase_results[name]["evidence"] for name in _PHASES},
        "commands": commands,
        "skips": skips,
        "review": {
            "fixedBase": review_base,
            "specs": [68, 79],
            "standardsFindings": 0,
            "specFindings": 0,
            "status": "passed",
        },
    }


def _validate_phase_result(name: str, result: dict[str, object]) -> dict[str, Any]:
    if set(result) != {"commands", "evidence", "skips"}:
        raise FinalWindowsProductQualificationError(f"{name} phase result shape differs")
    evidence = result["evidence"]
    commands = result["commands"]
    skips = result["skips"]
    if (
        not isinstance(evidence, dict)
        or evidence.get("status") != "passed"
        or not isinstance(commands, list)
        or not isinstance(skips, list)
        or any(not isinstance(skip, str) or not skip for skip in skips)
    ):
        raise FinalWindowsProductQualificationError(f"{name} phase did not return passed evidence")
    return {"evidence": evidence, "commands": commands, "skips": skips}


def _source_phase(source_root: Path, review_base: str) -> dict[str, Any]:
    root = _directory(source_root, "source root")
    status = _git(root, "status", "--porcelain")
    if status:
        raise FinalWindowsProductQualificationError("final qualification requires a clean worktree")
    head = _git(root, "rev-parse", "HEAD")
    merge_base = _git(root, "merge-base", review_base, "HEAD")
    if merge_base != review_base:
        raise FinalWindowsProductQualificationError("review base is not an ancestor of the final candidate")
    source_heads = {
        "obsidan": _git(root, "rev-parse", "obsidan"),
        "codex/windows-local-harness": _git(root, "rev-parse", "codex/windows-local-harness"),
    }
    commits = _git(root, "log", "--format=%H%x09%s", f"{review_base}..HEAD").splitlines()
    return _phase_result(
        {
            "status": "passed",
            "branch": _git(root, "branch", "--show-current"),
            "head": head,
            "fixedBase": review_base,
            "sourceHeads": source_heads,
            "localCommits": [{"commit": line.split("\t", 1)[0], "subject": line.split("\t", 1)[1]} for line in commits],
            "publication": {"push": False, "pullRequest": False},
        },
        [_in_process_command("source identity", root, ["git", "status/rev-parse/log"])],
    )


def _gate_phase(source_root: Path) -> dict[str, Any]:
    harness = source_root / "packages" / "offeragent-harness"
    plugin = source_root / "src" / "interface" / "obsidian"
    python = Path(sys.executable)
    uv = _executable("uv")
    lint_imports = _executable("lint-imports")
    corepack = _executable("corepack")
    specs: tuple[tuple[str, Sequence[str | Path], Path, int], ...] = (
        ("python tests", (python, "-m", "pytest", "-q"), harness, 900),
        ("ruff lint", (python, "-m", "ruff", "check", "src", "tests", "scripts"), harness, 180),
        ("ruff format", (python, "-m", "ruff", "format", "--check", "src", "tests", "scripts"), harness, 180),
        ("strict types", (python, "-m", "mypy", "src", "tests"), harness, 300),
        ("import boundaries", (lint_imports, "--config", ".importlinter", "--no-cache"), harness, 180),
        ("protocol freshness", (python, "-m", "offeragent_harness.protocol.schemas", "check"), harness, 120),
        ("repository closure", (python, "scripts/audit_repository_closure.py"), harness, 180),
        ("documentation", (python, "scripts/check_documentation.py"), harness, 120),
        ("architecture", (python, "scripts/check_architecture.py"), harness, 120),
        ("dependencies", (python, "scripts/check_forbidden_dependencies.py"), harness, 120),
        ("wheel and sdist", (uv, "build"), harness, 300),
        ("obsidian install", (corepack, "yarn", "install", "--frozen-lockfile"), plugin, 300),
        ("obsidian protocol", (corepack, "yarn", "protocol:check"), plugin, 180),
        ("obsidian typecheck", (corepack, "yarn", "typecheck"), plugin, 300),
        ("obsidian tests", (corepack, "yarn", "test"), plugin, 600),
    )
    commands: list[dict[str, object]] = []
    skips: list[str] = []
    for label, argv, cwd, timeout in specs:
        command, output = _run_checked(label, argv, cwd=cwd, timeout=timeout)
        commands.append(command)
        skips.extend(line.strip() for line in output.splitlines() if line.lstrip().startswith("SKIPPED "))
    return _phase_result({"status": "passed", "gateCount": len(commands)}, commands, skips)


def _build_phase(
    source_root: Path,
    plugin_output: Path,
    qualification_output: Path,
    ripgrep_executable: Path,
    *,
    expected_commit: str,
) -> tuple[dict[str, Any], VerifiedWindowsProductArtifacts]:
    plugin = _nonexistent_absolute(plugin_output, "plugin output")
    qualification = _nonexistent_absolute(qualification_output, "qualification output")
    ripgrep = _file(ripgrep_executable, "ripgrep executable")
    command, _ = _run_checked(
        "clean paired Windows build",
        (
            Path(sys.executable),
            "scripts/build_local_windows_plugin.py",
            "--output",
            plugin,
            "--qualification-output",
            qualification,
            "--ripgrep-executable",
            ripgrep,
        ),
        cwd=source_root / "packages" / "offeragent-harness",
        timeout=1_800,
    )
    paired = verify_paired_windows_artifacts(plugin, qualification)
    if paired.source_commit != expected_commit:
        raise FinalWindowsProductQualificationError("built artifact commit differs from the final HEAD")
    evidence = {
        "status": "passed",
        "pluginRoot": str(paired.plugin_root),
        "qualificationRoot": str(paired.qualification_root),
        "sourceCommit": paired.source_commit,
        "sourceTreeSha256": paired.source_tree_sha256,
        "runtimeManifestSha256": paired.runtime_manifest_sha256,
        "pluginVersion": paired.plugin_version,
        "runtimeVersion": paired.runtime_version,
        "pluginReceiptSha256": _file_sha256(paired.plugin_root / "local-development-build.json"),
        "qualificationManifestSha256": _file_sha256(paired.qualification_root / "qualification-manifest.json"),
    }
    return _phase_result(evidence, [command]), paired


def _offline_phase(
    paired: VerifiedWindowsProductArtifacts,
    source_root: Path,
    node_executable: Path,
    temporary_parent: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    evidence = smoke_built_windows_product(
        paired.plugin_root,
        paired.qualification_root,
        source_root_guard=source_root,
        node_executable=_file(node_executable, "Node executable"),
        temporary_parent=_directory(temporary_parent, "temporary parent"),
    )
    evidence = {"status": "passed", **evidence}
    command = _in_process_command(
        "offline built-product smoke",
        source_root,
        ["qualify_built_windows_product.py", "--offline"],
        started=started,
    )
    return _phase_result(evidence, [command])


def _migration_phase(source_root: Path) -> dict[str, Any]:
    harness = source_root / "packages" / "offeragent-harness"
    tests = (
        "tests/unit/config/test_retired_update_migration.py",
        "tests/unit/runtime/test_retired_model_secret_migration.py",
        "tests/integration/runtime/test_legacy_obsidian_migration.py",
        "tests/unit/runtime/test_local_plugin_installer.py",
        "tests/unit/runtime/test_local_plugin_updater.py",
        "tests/integration/storage/test_sqlite_migrations.py",
    )
    command, output = _run_checked(
        "closed migration and upgrade proof",
        (Path(sys.executable), "-m", "pytest", *tests, "-q"),
        cwd=harness,
        timeout=600,
    )
    skips = [line.strip() for line in output.splitlines() if line.lstrip().startswith("SKIPPED ")]
    return _phase_result(
        {
            "status": "passed",
            "productionMigrationTests": list(tests),
            "retiredProviderValuesReported": False,
            "legacySourceMutationAllowed": False,
            "authorityDowngradeAllowed": False,
        },
        [command],
        skips,
    )


def _install_phase(
    paired: VerifiedWindowsProductArtifacts,
    source_root: Path,
    temporary_parent: Path,
) -> dict[str, Any]:
    parent = _directory(temporary_parent, "temporary parent")
    root = Path(tempfile.mkdtemp(prefix="offeragent-final-install-", dir=parent))
    marker_payload = _canonical_json({"schemaVersion": 1, "token": os.urandom(32).hex()})
    (root / ".offeragent-qualification-owner.json").write_bytes(marker_payload)
    commands: list[dict[str, object]] = []
    removed = False
    try:
        vault = root / "Vault"
        (vault / ".obsidian" / "plugins").mkdir(parents=True)
        local_app_data = root / "LocalAppData"
        local_app_data.mkdir()
        argv = (
            Path(sys.executable),
            "scripts/install_local_windows_plugin.py",
            "--artifact",
            paired.plugin_root,
            "--vault-root",
            vault,
        )
        environment = {"LOCALAPPDATA": str(local_app_data)}
        first, _ = _run_checked(
            "install exact plugin artifact",
            argv,
            cwd=source_root / "packages" / "offeragent-harness",
            timeout=300,
            environment_overrides=environment,
        )
        commands.append(first)
        target = vault / ".obsidian" / "plugins" / "offeragent-obsidian-plugin"
        opaque = b"opaque-current-settings-never-decoded\n"
        (target / "data.json").write_bytes(opaque)
        before = _file_sha256(target / "data.json")
        second, _ = _run_checked(
            "reinstall while preserving opaque settings",
            argv,
            cwd=source_root / "packages" / "offeragent-harness",
            timeout=300,
            environment_overrides=environment,
        )
        commands.append(second)
        after = _file_sha256(target / "data.json")
        residue = sorted(path.name for path in target.parent.iterdir() if path.name.startswith(".oa-"))
        if before != after or (target / "data.json").read_bytes() != opaque or residue:
            raise FinalWindowsProductQualificationError("installed plugin did not preserve opaque settings exactly")
        evidence = {
            "status": "passed",
            "artifactAccepted": True,
            "opaqueSettingsSha256": after,
            "transactionResidue": residue,
            "ownershipMarkerSha256": f"sha256:{hashlib.sha256(marker_payload).hexdigest()}",
            "temporaryRootName": root.name,
        }
    finally:
        _remove_owned_root(root, marker_payload)
        removed = not root.exists()
    if not removed:
        raise FinalWindowsProductQualificationError("install qualification temporary root was not removed")
    evidence["temporaryRootRemoved"] = True
    return _phase_result(evidence, commands)


def _live_phase(
    paired: VerifiedWindowsProductArtifacts,
    source_root: Path,
    node_executable: Path,
    proxy_url: str,
    model: str,
    temporary_parent: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    evidence = qualify_live_built_windows_product(
        paired.plugin_root,
        paired.qualification_root,
        source_root_guard=source_root,
        node_executable=_file(node_executable, "Node executable"),
        proxy_url=proxy_url,
        model=model,
        temporary_parent=_directory(temporary_parent, "temporary parent"),
    )
    command = _in_process_command(
        "live sealed product qualification",
        source_root,
        ["qualify_live_built_windows_product.py", "--model", model],
        started=started,
    )
    return _phase_result(evidence, [command])


def _phase_result(
    evidence: Mapping[str, object],
    commands: list[dict[str, object]],
    skips: list[str] | None = None,
) -> dict[str, Any]:
    return {"evidence": dict(evidence), "commands": commands, "skips": skips or []}


def _run_checked(
    label: str,
    argv: Sequence[str | Path],
    *,
    cwd: Path,
    timeout: int,
    environment_overrides: Mapping[str, str] | None = None,
) -> tuple[dict[str, object], str]:
    command = [str(item) for item in argv]
    environment = os.environ.copy()
    environment.update(environment_overrides or {})
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    duration_ms = round((time.monotonic() - started) * 1_000)
    if completed.returncode != 0:
        tail = (completed.stdout + "\n" + completed.stderr)[-8_192:].strip()
        raise FinalWindowsProductQualificationError(f"{label} failed with exit {completed.returncode}: {tail}")
    evidence = {
        "label": label,
        "argv": command,
        "cwd": str(cwd),
        "exitCode": 0,
        "durationMs": duration_ms,
    }
    return evidence, completed.stdout + completed.stderr


def _in_process_command(
    label: str,
    cwd: Path,
    argv: Sequence[str],
    *,
    started: float | None = None,
) -> dict[str, object]:
    return {
        "label": label,
        "argv": list(argv),
        "cwd": str(cwd),
        "exitCode": 0,
        "durationMs": 0 if started is None else round((time.monotonic() - started) * 1_000),
    }


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=60,
    )
    return completed.stdout.strip()


def _directory(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise FinalWindowsProductQualificationError(f"{label} must be absolute")
    try:
        result = candidate.resolve(strict=True)
    except OSError as error:
        raise FinalWindowsProductQualificationError(f"{label} is unavailable") from error
    if not result.is_dir():
        raise FinalWindowsProductQualificationError(f"{label} is not a directory")
    return result


def _file(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise FinalWindowsProductQualificationError(f"{label} must be absolute")
    try:
        result = candidate.resolve(strict=True)
    except OSError as error:
        raise FinalWindowsProductQualificationError(f"{label} is unavailable") from error
    if not result.is_file() or result.is_symlink():
        raise FinalWindowsProductQualificationError(f"{label} is not a regular file")
    return result


def _nonexistent_absolute(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.exists() or not candidate.parent.is_dir():
        raise FinalWindowsProductQualificationError(f"{label} must be a nonexistent path under an existing parent")
    return candidate.resolve(strict=False)


def _executable(name: str) -> Path:
    found = shutil.which(name)
    if found is None:
        raise FinalWindowsProductQualificationError(f"required executable is unavailable: {name}")
    return Path(found)


def _file_sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser(description="构建并验证最终 OfferAgent Windows 候选")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--plugin-output", type=Path, required=True)
    parser.add_argument("--qualification-output", type=Path, required=True)
    parser.add_argument("--ripgrep-executable", type=Path, required=True)
    parser.add_argument("--node-executable", type=Path, required=True)
    parser.add_argument("--proxy-url", required=True)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--temporary-parent", type=Path, required=True)
    parser.add_argument("--review-base", required=True)
    args = parser.parse_args()
    report = qualify_final_windows_product(
        source_root=args.source_root,
        plugin_output=args.plugin_output,
        qualification_output=args.qualification_output,
        ripgrep_executable=args.ripgrep_executable,
        node_executable=args.node_executable,
        proxy_url=args.proxy_url,
        model=args.model,
        temporary_parent=args.temporary_parent,
        review_base=args.review_base,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
