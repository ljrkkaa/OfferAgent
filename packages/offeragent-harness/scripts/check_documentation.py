"""Fail closed when repository documentation references missing local inputs.

The repository documentation is part of the product contract.  Relative
Markdown links and documented Python script commands must resolve inside the
checked-out repository instead of depending on an untracked parent workspace.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Set
from pathlib import Path
from urllib.parse import unquote

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]

_IGNORED_DIRECTORY_NAMES = frozenset({".git", ".mypy_cache", ".pytest_cache", ".venv", "build", "dist", "node_modules"})
_MARKDOWN_LINK = re.compile(r"\[[^\]\r\n]+\]\((?P<target><[^>\r\n]+>|[^)\s\r\n]+)")
_MARKDOWN_REFERENCE_TARGET = re.compile(
    r"(?m)^[ \t]{0,3}\[[^\]\r\n]+\]:[ \t]*(?P<target><[^>\r\n]+>|[^\s\r\n]+)",
)
_PYTHON_SCRIPT_COMMAND = re.compile(
    r"(?m)^\s*(?:uv\s+run\s+)?python(?:\.exe)?\s+(?P<target>[^\s`]+\.py)(?:\s|$)",
)
_URI_SCHEMES = ("http://", "https://", "mailto:", "obsidian:")


class GitTrackingError(RuntimeError):
    """Raised when the repository index cannot be inspected safely."""


def documentation_files(repository_root: Path) -> Iterable[Path]:
    for path in sorted(repository_root.rglob("*.md")):
        try:
            relative = path.relative_to(repository_root)
        except ValueError:
            continue
        if any(part in _IGNORED_DIRECTORY_NAMES for part in relative.parts):
            continue
        if path.is_file():
            yield path


def _local_link_target(raw_target: str) -> str | None:
    target = raw_target[1:-1] if raw_target.startswith("<") and raw_target.endswith(">") else raw_target
    target = unquote(target).split("#", 1)[0].split("?", 1)[0]
    if not target or target.startswith("#") or target.casefold().startswith(_URI_SCHEMES):
        return None
    return target.replace("\\", "/")


def _is_absolute_local_link(target: str) -> bool:
    return re.match(r"^[A-Za-z]:/", target) is not None or target.startswith("/")


def _resolve_documented_script(repository_root: Path, document: Path, raw_target: str) -> Path | None:
    target = Path(raw_target.replace("\\", "/"))
    if target.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", raw_target):
        return None
    candidates = (
        document.parent / target,
        repository_root / target,
        repository_root / "packages" / "offeragent-harness" / target,
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _tracking_key(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    return normalized.casefold() if os.name == "nt" else normalized


def _git_tracked_paths(repository_root: Path) -> frozenset[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "ls-files", "--cached", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise GitTrackingError(f"cannot run git ls-files: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise GitTrackingError(f"git ls-files exited with status {result.returncode}{suffix}")
    return frozenset(
        _tracking_key(entry.decode("utf-8", errors="surrogateescape")) for entry in result.stdout.split(b"\0") if entry
    )


def _is_git_tracked(
    repository_root: Path,
    candidate: Path,
    tracked_paths: Set[str],
    *,
    allow_directory: bool,
) -> bool:
    lexical_candidate = Path(os.path.abspath(candidate))
    try:
        relative = lexical_candidate.relative_to(repository_root).as_posix()
    except ValueError:
        return False
    key = _tracking_key(relative)
    if key in tracked_paths:
        return True
    if not allow_directory or not candidate.is_dir():
        return False
    prefix = f"{key}/" if key else ""
    return any(path.startswith(prefix) for path in tracked_paths)


def check(repository_root: Path, *, tracked_paths: Set[str] | None = None) -> list[str]:
    repository_root = repository_root.resolve(strict=True)
    problems: list[str] = []
    if tracked_paths is None:
        try:
            tracked_paths = _git_tracked_paths(repository_root)
        except GitTrackingError as error:
            return [f"repository: cannot enumerate Git-tracked paths: {error}"]
    else:
        tracked_paths = frozenset(_tracking_key(path) for path in tracked_paths)
    for document in documentation_files(repository_root):
        relative = document.relative_to(repository_root).as_posix()
        try:
            content = document.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            problems.append(f"{relative}: cannot read documentation: {error}")
            continue
        link_matches = [*_MARKDOWN_LINK.finditer(content), *_MARKDOWN_REFERENCE_TARGET.finditer(content)]
        for match in sorted(link_matches, key=lambda item: item.start()):
            target = _local_link_target(match.group("target"))
            if target is None:
                continue
            if _is_absolute_local_link(target):
                problems.append(f"{relative}: absolute local link target is forbidden: {match.group('target')}")
                continue
            candidate = (document.parent / Path(target)).resolve(strict=False)
            try:
                candidate.relative_to(repository_root)
            except ValueError:
                problems.append(f"{relative}: local link escapes repository: {match.group('target')}")
                continue
            if not candidate.exists():
                problems.append(f"{relative}: local link target is missing: {match.group('target')}")
            elif not _is_git_tracked(
                repository_root,
                document.parent / Path(target),
                tracked_paths,
                allow_directory=True,
            ):
                problems.append(f"{relative}: local link target is not tracked by Git: {match.group('target')}")
        for match in _PYTHON_SCRIPT_COMMAND.finditer(content):
            target = match.group("target")
            script = _resolve_documented_script(repository_root, document, target)
            if script is None:
                problems.append(f"{relative}: documented Python script is missing: {target}")
            elif not _is_git_tracked(
                repository_root,
                script,
                tracked_paths,
                allow_directory=False,
            ):
                problems.append(f"{relative}: documented Python script is not tracked by Git: {target}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate repository-local documentation references")
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    arguments = parser.parse_args(argv)
    problems = check(arguments.repository_root)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("documentation check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
