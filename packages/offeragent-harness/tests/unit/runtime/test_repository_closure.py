from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from typing import cast

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "audit_repository_closure.py"
_NAMESPACE = run_path(str(_SCRIPT))
check = cast(Callable[[Path], list[str]], _NAMESPACE["check"])
FORBIDDEN_PATHS = cast(tuple[str, ...], _NAMESPACE["FORBIDDEN_PATHS"])
forbidden_distribution = cast(Callable[[str], bool], _NAMESPACE["forbidden_distribution"])
python_execution_problems = cast(Callable[[Path, Path], list[str]], _NAMESPACE["python_execution_problems"])
local_build_gate_problems = cast(Callable[[Path], list[str]], _NAMESPACE["_local_build_gate_problems"])


def test_repository_has_one_windows_local_production_closure() -> None:
    repository = Path(__file__).resolve().parents[5]

    assert check(repository) == []


def test_retired_release_sources_and_output_directories_cannot_return() -> None:
    expected = {
        "packages/offeragent-harness/release",
        "packages/offeragent-harness/release-out",
        "packages/offeragent-harness/src/offeragent_harness/runtime/development_self_test.py",
        "packages/offeragent-harness/src/offeragent_harness/runtime/lifecycle.py",
        "packages/offeragent-harness/src/offeragent_harness/runtime/production_process_catalog.py",
        "packages/offeragent-harness/src/offeragent_harness/runtime/windows_authenticode.py",
    }

    assert expected <= set(FORBIDDEN_PATHS)


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


def test_local_build_closure_rejects_removing_mandatory_calls(tmp_path: Path) -> None:
    source = (_SCRIPT.parent / "build_local_windows_plugin.py").read_text(encoding="utf-8")
    repository = tmp_path / "repository"
    build_script = repository / "packages" / "offeragent-harness" / "scripts" / "build_local_windows_plugin.py"
    build_script.parent.mkdir(parents=True)
    workflow = repository / ".github" / "workflows" / "windows-local-runtime.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("scripts/audit_repository_closure.py\n", encoding="utf-8")

    def call_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    class RemoveCall(ast.NodeTransformer):
        def __init__(self, caller: str, callee: str) -> None:
            self.caller = caller
            self.callee = callee
            self.active = False

        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
            previous = self.active
            self.active = node.name == self.caller
            updated = self.generic_visit(node)
            self.active = previous
            return updated

        def visit_Call(self, node: ast.Call) -> ast.AST:
            updated = self.generic_visit(node)
            if self.active and isinstance(updated, ast.Call) and call_name(updated.func) == self.callee:
                return ast.copy_location(ast.Constant(None), updated)
            return updated

    required_calls = (
        ("build_development_runtime", "_require_exact_root_executables"),
        ("build_development_runtime", "verify_project_source_snapshot"),
        ("main", "_require_embedded_schema_identity"),
        ("main", "require_source_tree_unchanged"),
        ("main", "source_tree_identity"),
    )
    for caller, callee in required_calls:
        tree = RemoveCall(caller, callee).visit(ast.parse(source))
        ast.fix_missing_locations(tree)
        build_script.write_text(ast.unparse(tree), encoding="utf-8")

        expected = f"local build {caller} must invoke {callee}"
        assert any(expected in problem for problem in local_build_gate_problems(repository))
