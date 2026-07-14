from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from typing import cast

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "audit_repository_closure.py"
_NAMESPACE = run_path(str(_SCRIPT))
check = cast(Callable[[Path], list[str]], _NAMESPACE["check"])
forbidden_distribution = cast(Callable[[str], bool], _NAMESPACE["forbidden_distribution"])
python_execution_problems = cast(Callable[[Path, Path], list[str]], _NAMESPACE["python_execution_problems"])


def test_repository_has_one_windows_local_production_closure() -> None:
    repository = Path(__file__).resolve().parents[5]

    assert check(repository) == []


def test_archival_words_are_inert_but_cannot_be_executed(tmp_path: Path) -> None:
    inert = tmp_path / "inert.py"
    inert.write_text('"""Historical command: python -m khoj.main."""\n', encoding="utf-8")
    executable = tmp_path / "executable.py"
    executable.write_text(
        'import subprocess\nsubprocess.run(["python", "-m", "khoj.main"], check=True)\n',
        encoding="utf-8",
    )

    assert python_execution_problems(inert, Path("docs/inert.py")) == []
    assert any(
        "executes an archived server/Agent command" in problem
        for problem in python_execution_problems(executable, Path("scripts/executable.py"))
    )


def test_distribution_policy_normalizes_aliases_and_langchain_family() -> None:
    assert forbidden_distribution("psycopg2_binary") is True
    assert forbidden_distribution("Django") is True
    assert forbidden_distribution("langchain-experimental") is True
    assert forbidden_distribution("pydantic") is False
