"""Shared build primitives for the personal Windows x64 Runtime bundle.

This module can only assemble the hash-pinned local Runtime tree. It has no
signing, release archive, installer, update, or publication path.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from offeragent_harness.runtime.local_process_catalog import validate_process_catalog_payload

try:
    from scripts.runtime_sbom import runtime_distribution_closure
except ModuleNotFoundError:
    from runtime_sbom import runtime_distribution_closure  # type: ignore[import-not-found,no-redef]

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_SKILLS = ROOT / "packaging" / "runtime-skills"
PROCESS_CATALOG = ROOT / "packaging" / "process-catalog.v1.json"
WINDOWS_X64_PE_MACHINE = 0x8664


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


def add_local_assets(runtime: Path, *, ripgrep_executable: Path) -> None:
    """Add only assets consumed by the personal Worker and process host."""

    if not BUILTIN_SKILLS.is_dir() or not any(BUILTIN_SKILLS.rglob("SKILL.md")):
        raise RuntimeError("local Runtime source contains no built-in Skills")
    _copy_static_tree(BUILTIN_SKILLS, runtime / "skills")
    _copy_static_tree(ROOT / "web", runtime / "web")
    if not PROCESS_CATALOG.is_file():
        raise RuntimeError("local Runtime source contains no Process catalog")
    try:
        validate_process_catalog_payload(PROCESS_CATALOG.read_bytes())
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise RuntimeError("local Runtime Process catalog is invalid") from error
    _copy_static_file(PROCESS_CATALOG, runtime / PROCESS_CATALOG.name)
    ripgrep, _version = _validated_ripgrep_executable(ripgrep_executable)
    _copy_static_file(ripgrep, runtime / "tools" / "rg.exe")
    licenses = runtime / "LICENSES"
    licenses.mkdir()
    _copy_static_file(ROOT / "LICENSE", licenses / "AGPL-3.0-or-later.txt")
    for item in runtime_distribution_closure():
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
            _copy_static_file(located, target)


def _validated_ripgrep_executable(executable: Path) -> tuple[Path, str]:
    try:
        resolved = executable.resolve(strict=True)
    except OSError as error:
        raise RuntimeError("ripgrep build input is unavailable") from error
    if not resolved.is_file() or resolved.is_symlink() or resolved.name.casefold() != "rg.exe":
        raise RuntimeError("ripgrep build input must be a regular rg.exe file")
    if pe_machine(resolved) != WINDOWS_X64_PE_MACHINE:
        raise RuntimeError("ripgrep build input is not native Windows x64")
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


def _copy_static_tree(source: Path, destination: Path) -> None:
    for item in sorted(source.rglob("*")):
        if item.is_dir():
            continue
        if item.is_symlink() or not item.is_file():
            raise RuntimeError("local Runtime static asset is a symlink or special file")
        _copy_static_file(item, destination / item.relative_to(source))


def _copy_static_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("local Runtime static source is a symlink or special file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError(f"duplicate local Runtime static asset: {destination.name}")
    shutil.copy2(source, destination)


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


__all__ = [
    "WINDOWS_X64_PE_MACHINE",
    "add_local_assets",
    "build_one_onedir",
    "merge_identical_tree",
    "pe_machine",
]
