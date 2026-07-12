from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

import tomllib

FORBIDDEN_DISTRIBUTIONS = {
    "django",
    "khoj",
    "langchain",
    "langchain-community",
    "langchain-core",
    "psycopg",
    "psycopg2",
    "psycopg2-binary",
}
FORBIDDEN_IMPORT_ROOTS = {"django", "khoj", "langchain", "psycopg", "psycopg2"}


def distribution_name(requirement: str) -> str:
    head = requirement.split(";", 1)[0].strip()
    for token in ("[", "<", ">", "=", "!", "~", " "):
        head = head.split(token, 1)[0]
    return head.lower().replace("_", "-")


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def check(package_root: Path) -> list[str]:
    problems: list[str] = []
    config = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = config["project"].get("dependencies", [])
    for requirement in dependencies:
        name = distribution_name(requirement)
        if name in FORBIDDEN_DISTRIBUTIONS or name.startswith("langchain-"):
            problems.append(f"forbidden runtime dependency: {requirement}")

    source = package_root / "src" / "offeragent_harness"
    for path in sorted(source.rglob("*.py")):
        illegal = imported_roots(path) & FORBIDDEN_IMPORT_ROOTS
        if illegal:
            problems.append(f"{path.relative_to(package_root)} imports {sorted(illegal)}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    problems = check(args.package_root.resolve())
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("forbidden dependency check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
