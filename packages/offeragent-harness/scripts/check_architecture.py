from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

CORE_FORBIDDEN_ROOTS = {"django", "khoj", "langchain", "psycopg", "psycopg2"}
MODEL_FORBIDDEN_ROOTS = {"os", "pathlib", "shutil", "subprocess"}


def import_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return decorator_name(node.func)
    return ""


def check(package_root: Path) -> list[str]:
    source = package_root / "src" / "offeragent_harness"
    problems: list[str] = []
    loop_entries: list[str] = []
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(source)
        if "testing" in relative.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = import_names(tree)
        roots = {name.split(".", 1)[0] for name in imports}
        illegal = roots & CORE_FORBIDDEN_ROOTS
        if illegal:
            problems.append(f"{relative}: forbidden legacy imports {sorted(illegal)}")
        if any(
            name == "offeragent_harness.testing" or name.startswith("offeragent_harness.testing.") for name in imports
        ):
            problems.append(f"{relative}: production code imports test fakes")
        if relative.parts and relative.parts[0] == "models":
            model_illegal = roots & MODEL_FORBIDDEN_ROOTS
            if model_illegal:
                problems.append(f"{relative}: model boundary imports {sorted(model_illegal)}")
            if any(
                name == "offeragent_harness.tools" or name.startswith("offeragent_harness.tools.") for name in imports
            ):
                problems.append(f"{relative}: model boundary imports tool implementation")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                decorator_name(decorator) == "agent_loop_entrypoint" for decorator in node.decorator_list
            ):
                loop_entries.append(f"{relative}:{node.lineno}:{node.name}")
    if len(loop_entries) != 1:
        problems.append(f"expected exactly one @agent_loop_entrypoint, found {len(loop_entries)}: {loop_entries}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    problems = check(args.package_root.resolve())
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("architecture check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
