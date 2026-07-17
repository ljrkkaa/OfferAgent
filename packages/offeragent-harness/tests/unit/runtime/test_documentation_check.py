from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.check_documentation import check


def test_repository_documentation_references_only_tracked_local_inputs() -> None:
    repository = Path(__file__).resolve().parents[5]

    assert check(repository) == []


def test_documentation_check_rejects_missing_links_and_script_commands(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "[missing](docs/missing.md)\n[external authority](../task.md)\n\n"
        "```powershell\nuv run python scripts/missing.py\n```\n",
        encoding="utf-8",
    )

    problems = check(tmp_path, tracked_paths=frozenset())

    assert problems == [
        "README.md: local link target is missing: docs/missing.md",
        "README.md: local link escapes repository: ../task.md",
        "README.md: documented Python script is missing: scripts/missing.py",
    ]


def test_documentation_check_rejects_existing_untracked_inputs(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docs" / "tracked.md").write_text("tracked\n", encoding="utf-8")
    (tmp_path / "docs" / "untracked.md").write_text("untracked\n", encoding="utf-8")
    (tmp_path / "scripts" / "tracked.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "scripts" / "untracked.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[tracked file](docs/tracked.md)\n"
        "[tracked directory](docs)\n"
        "[untracked file](docs/untracked.md)\n\n"
        "```powershell\n"
        "uv run python scripts/tracked.py\n"
        "uv run python scripts/untracked.py\n"
        "```\n",
        encoding="utf-8",
    )

    problems = check(
        tmp_path,
        tracked_paths={"README.md", "docs/tracked.md", "scripts/tracked.py"},
    )

    assert problems == [
        "README.md: local link target is not tracked by Git: docs/untracked.md",
        "README.md: documented Python script is not tracked by Git: scripts/untracked.py",
    ]


def test_documentation_check_rejects_absolute_and_reference_style_local_links(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "untracked.md").write_text("untracked\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[drive](C:/outside.md)\n"
        "[root](/outside.md)\n"
        r"[share](\\server\share\outside.md)"
        "\n"
        "[reference][local]\n"
        "[local]: docs/untracked.md\n"
        "[external][authority]\n"
        "[authority]: https://example.com/reference\n",
        encoding="utf-8",
    )

    problems = check(tmp_path, tracked_paths={"README.md"})

    assert problems == [
        "README.md: absolute local link target is forbidden: C:/outside.md",
        "README.md: absolute local link target is forbidden: /outside.md",
        r"README.md: absolute local link target is forbidden: \\server\share\outside.md",
        "README.md: local link target is not tracked by Git: docs/untracked.md",
    ]


def test_documentation_check_reads_tracked_inputs_from_git_index(tmp_path: Path) -> None:
    (tmp_path / "tracked.md").write_text("tracked\n", encoding="utf-8")
    (tmp_path / "untracked.md").write_text("untracked\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[tracked](tracked.md)\n[untracked](untracked.md)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "README.md", "tracked.md"], cwd=tmp_path, check=True)

    problems = check(tmp_path)

    assert problems == ["README.md: local link target is not tracked by Git: untracked.md"]
