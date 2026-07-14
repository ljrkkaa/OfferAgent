"""Fail-closed audit for the Windows-local repository production closure.

Historical design documents may describe the retired server.  Executable
source, package metadata, locks and release assembly may not import, start or
ship it.  This check deliberately audits the repository, not only a built ZIP,
so a second Agent Loop cannot quietly return on the main branch.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

import tomllib  # type: ignore[import-not-found]

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]

FORBIDDEN_PATHS = (
    "src/khoj",
    "src/interface/web",
    "tests",
    "scripts",
    ".devcontainer",
    ".vscode",
    ".dockerignore",
    "Dockerfile",
    "prod.Dockerfile",
    "computer.Dockerfile",
    "gunicorn-config.py",
    "manifest.json",
    "versions.json",
    "pyproject.toml",
    "pytest.ini",
    "uv.lock",
    "src/interface/obsidian/src/api.ts",
    "src/interface/obsidian/src/chat_runtime.ts",
    "src/interface/obsidian/src/interact_with_files.ts",
    "src/interface/obsidian/src/pane_view.ts",
    "src/interface/obsidian/src/search_modal.ts",
    "src/interface/obsidian/src/settings.ts",
    "src/interface/obsidian/src/similar_view.ts",
    "src/interface/obsidian/tests/server_runtime.test.cjs",
    "src/interface/obsidian/tests/vault_actions.test.cjs",
)

REQUIRED_PATHS = (
    "packages/offeragent-harness/pyproject.toml",
    "packages/offeragent-harness/uv.lock",
    "packages/offeragent-harness/src/offeragent_harness/agent/loop.py",
    "packages/offeragent-harness/src/offeragent_harness/runtime/production_worker_composition.py",
    "packages/offeragent-harness/scripts/legacy_exporter/exporter.py",
    "packages/offeragent-harness/scripts/build_windows_release.py",
    "packages/offeragent-harness/web/index.html",
    "src/interface/obsidian/manifest.json",
    "src/interface/obsidian/package.json",
    "src/interface/obsidian/yarn.lock",
    "src/interface/obsidian/src/main.ts",
)

FORBIDDEN_IMPORT_ROOTS = {"django", "khoj", "langchain", "psycopg", "psycopg2"}
FORBIDDEN_DISTRIBUTIONS = {
    "django",
    "django-apscheduler",
    "django-unfold",
    "khoj",
    "langchain",
    "langchain-community",
    "langchain-core",
    "pgserver",
    "psycopg",
    "psycopg2",
    "psycopg2-binary",
}
LEGACY_EXECUTION_TOKENS = (
    "khoj.main",
    "src/khoj",
    "src\\khoj",
    "khoj/manage.py",
    "khoj\\manage.py",
    "api_chat",
    "agent_tool_loop",
    "gunicorn",
    "uvicorn",
    "django-admin",
    "postgres",
)
PROCESS_CALL_NAMES = {
    "call",
    "check_call",
    "check_output",
    "create_subprocess_exec",
    "create_subprocess_shell",
    "execv",
    "execve",
    "popen",
    "run",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "startfile",
    "system",
}
DYNAMIC_IMPORT_CALL_NAMES = {"__import__", "find_spec", "import_module", "run_module", "run_path"}
CANONICAL_LOOP = Path("packages/offeragent-harness/src/offeragent_harness/agent/loop.py")
PLUGIN_FORBIDDEN_TOKENS = (
    "OfferAgentServer",
    "khojUrl",
    "khojApiKey",
    "/api/chat",
    "updateContentIndex",
    "syncFolders",
    "connectedToBackend",
    "codex app-server",
)
AGENT_LOOP_SYMBOL = re.compile(r"(?i)(?:agent.*loop|loop.*agent|plannerloop)")


def normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def forbidden_distribution(value: str) -> bool:
    normalized = normalized_distribution_name(value)
    return normalized in FORBIDDEN_DISTRIBUTIONS or normalized.startswith("langchain-")


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _literal_strings(node: ast.AST) -> tuple[str, ...]:
    values: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            values.append(child.value)
    return tuple(values)


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def python_execution_problems(path: Path, relative: Path) -> list[str]:
    """Audit one executable Python file while allowing inert archival prose."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as error:
        return [f"{relative.as_posix()}: cannot parse executable Python: {error}"]
    problems: list[str] = []
    is_test = "tests" in relative.parts
    illegal = _import_roots(tree) & FORBIDDEN_IMPORT_ROOTS
    if illegal:
        problems.append(f"{relative.as_posix()}: imports retired runtime roots {sorted(illegal)}")

    for node in ast.walk(tree):
        if not is_test and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if AGENT_LOOP_SYMBOL.search(node.name) and relative != CANONICAL_LOOP:
                problems.append(f"{relative.as_posix()}:{node.lineno}: second Agent Loop-like symbol {node.name!r}")
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func).casefold()
        literals = " ".join(value.casefold().replace("\\", "/") for value in _literal_strings(node))
        if name in DYNAMIC_IMPORT_CALL_NAMES:
            dynamic_roots = {
                value.split(".", 1)[0].casefold()
                for value in _literal_strings(node)
                if value and "/" not in value and "\\" not in value
            }
            if dynamic_roots & FORBIDDEN_IMPORT_ROOTS:
                problems.append(f"{relative.as_posix()}:{node.lineno}: dynamically loads a retired runtime module")
        if name in PROCESS_CALL_NAMES and any(
            token.replace("\\", "/") in literals for token in LEGACY_EXECUTION_TOKENS
        ):
            problems.append(f"{relative.as_posix()}:{node.lineno}: executes an archived server/Agent command")
    return problems


def _python_files(repository_root: Path) -> Iterable[tuple[Path, Path]]:
    roots = (
        repository_root / "packages" / "offeragent-harness" / "src",
        repository_root / "packages" / "offeragent-harness" / "scripts",
        repository_root / "packages" / "offeragent-harness" / "tests",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if any(part in {".venv", "__pycache__", "build", "dist"} for part in path.parts):
                continue
            yield path, path.relative_to(repository_root)


def _metadata_problems(repository_root: Path) -> list[str]:
    problems: list[str] = []
    package_root = repository_root / "packages" / "offeragent-harness"
    config_path = package_root / "pyproject.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        return [f"cannot parse Harness package metadata: {error}"]
    project = config.get("project", {})
    if project.get("name") != "offeragent-harness":
        problems.append("Harness distribution name must remain offeragent-harness")
    requirements: list[str] = list(project.get("dependencies", []))
    for values in project.get("optional-dependencies", {}).values():
        requirements.extend(values)
    for requirement in requirements:
        name = re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0]
        if forbidden_distribution(name):
            problems.append(f"package metadata contains retired runtime dependency: {requirement}")
    for name, target in project.get("scripts", {}).items():
        rendered = f"{name}={target}".casefold()
        if not isinstance(target, str) or not target.startswith("offeragent_harness."):
            problems.append(f"console entry point is outside the local Harness: {name}={target}")
        if any(token in rendered for token in ("khoj", "django", "api_chat", "agent_tool_loop")):
            problems.append(f"console entry point names a retired runtime: {name}={target}")

    lock_path = package_root / "uv.lock"
    try:
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        problems.append(f"cannot parse Harness uv.lock: {error}")
    else:
        names = {
            normalized_distribution_name(item["name"])
            for item in lock.get("package", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        illegal = sorted(name for name in names if forbidden_distribution(name))
        if illegal:
            problems.append(f"Harness uv.lock contains retired runtime distributions: {illegal}")
        if "offeragent-harness" not in names:
            problems.append("Harness uv.lock does not contain the OfferAgent distribution")

    plugin_root = repository_root / "src" / "interface" / "obsidian"
    try:
        plugin_package = json.loads((plugin_root / "package.json").read_text(encoding="utf-8"))
        manifest = json.loads((plugin_root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        problems.append(f"cannot parse Obsidian package metadata: {error}")
    else:
        if plugin_package.get("name") != "offeragent-obsidian":
            problems.append("Obsidian distribution name must remain offeragent-obsidian")
        if plugin_package.get("packageManager") != "yarn@1.22.22":
            problems.append("Obsidian dependency lock must be bound to Yarn 1.22.22")
        if manifest.get("isDesktopOnly") is not True:
            problems.append("Obsidian manifest must be desktop-only")
        serialized = json.dumps(plugin_package, ensure_ascii=False, sort_keys=True)
        if any(token in serialized for token in PLUGIN_FORBIDDEN_TOKENS):
            problems.append("Obsidian package metadata contains a retired HTTP/sync surface")
        try:
            plugin_lock = (plugin_root / "yarn.lock").read_text(encoding="utf-8").casefold()
        except (OSError, UnicodeError) as error:
            problems.append(f"cannot read Obsidian yarn.lock: {error}")
        else:
            illegal_lock_tokens = [token for token in ("django", "khoj", "psycopg", "postgres") if token in plugin_lock]
            if illegal_lock_tokens:
                problems.append(f"Obsidian yarn.lock contains retired runtime tokens: {illegal_lock_tokens}")
    return problems


def _single_loop_problems(repository_root: Path) -> list[str]:
    source = repository_root / "packages" / "offeragent-harness" / "src" / "offeragent_harness"
    entries: list[str] = []
    for path in sorted(source.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                name = _call_name(decorator.func) if isinstance(decorator, ast.Call) else _call_name(decorator)
                if name == "agent_loop_entrypoint":
                    entries.append(f"{path.relative_to(repository_root).as_posix()}:{node.lineno}:{node.name}")
    expected_prefix = f"{CANONICAL_LOOP.as_posix()}:"
    if len(entries) != 1 or not entries[0].startswith(expected_prefix):
        return [f"repository must contain one canonical Agent Loop entry point, found {entries}"]
    return []


def _ui_boundary_problems(repository_root: Path) -> list[str]:
    problems: list[str] = []
    roots = (
        repository_root / "src" / "interface" / "obsidian" / "src",
        repository_root / "packages" / "offeragent-harness" / "web",
    )
    for root in roots:
        for path in sorted(item for item in root.rglob("*") if item.suffix in {".ts", ".js", ".html"}):
            try:
                value = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                problems.append(f"cannot read UI source {path.relative_to(repository_root)}: {error}")
                continue
            found = [token for token in PLUGIN_FORBIDDEN_TOKENS if token in value]
            if found:
                problems.append(f"{path.relative_to(repository_root).as_posix()}: retired HTTP/sync tokens {found}")
            if re.search(r"(?i)class\s+(?:AgentLoop|PlannerLoop)|while\s*\([^)]*model[^)]*\)", value):
                problems.append(f"{path.relative_to(repository_root).as_posix()}: UI owns an Agent/Planner loop")
    return problems


def _sbom_gate_problems(repository_root: Path) -> list[str]:
    scripts_root = repository_root / "packages" / "offeragent-harness" / "scripts"
    build_script = scripts_root / "build_windows_release.py"
    build_tree = ast.parse(build_script.read_text(encoding="utf-8"), filename=str(build_script))

    def calls_by_function(tree: ast.Module) -> dict[str, set[str]]:
        callers: dict[str, set[str]] = {}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            callers[node.name] = {_call_name(child.func) for child in ast.walk(node) if isinstance(child, ast.Call)}
        return callers

    callers = calls_by_function(build_tree)
    problems: list[str] = []
    required = {
        "add_release_metadata": {
            "assert_sbom_packages_allowed",
            "build_payload_provenance",
            "build_spdx_document",
        },
        "audit_runtime_archive": {
            "assert_payload_provenance",
            "assert_runtime_spdx_document",
            "assert_sbom_packages_allowed",
        },
    }
    problems.extend(
        f"release {caller} must invoke {callee} for SBOM dependency closure"
        for caller, callees in required.items()
        for callee in sorted(callees)
        if callee not in callers.get(caller, set())
    )
    release_gate = next(
        (
            node
            for node in build_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "require_release_host"
        ),
        None,
    )
    release_literals = set(_literal_strings(release_gate)) if release_gate is not None else set()
    if "scripts/audit_repository_closure.py" not in release_literals:
        problems.append("signed release gate does not run the repository closure audit")

    audit_script = scripts_root / "audit_windows_release.py"
    audit_tree = ast.parse(audit_script.read_text(encoding="utf-8"), filename=str(audit_script))
    independent_calls = calls_by_function(audit_tree).get("main", set())
    for callee in ("assert_payload_provenance", "assert_runtime_spdx_document", "assert_sbom_packages_allowed"):
        if callee not in independent_calls:
            problems.append(f"independent Windows release audit does not invoke {callee}")

    workflow = repository_root / ".github" / "workflows" / "windows-local-runtime.yml"
    try:
        workflow_text = workflow.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        problems.append(f"cannot read Windows-local CI workflow: {error}")
    else:
        if "scripts/audit_repository_closure.py" not in workflow_text:
            problems.append("Windows-local CI does not run the repository closure audit")
    return problems


def check(repository_root: Path) -> list[str]:
    repository_root = repository_root.resolve(strict=True)
    problems: list[str] = []
    for relative in FORBIDDEN_PATHS:
        if repository_root.joinpath(*relative.split("/")).exists():
            problems.append(f"retired executable/runtime path still exists: {relative}")
    for relative in REQUIRED_PATHS:
        if not repository_root.joinpath(*relative.split("/")).is_file():
            problems.append(f"required Windows-local source is missing: {relative}")
    for path, relative_path in _python_files(repository_root):
        problems.extend(python_execution_problems(path, relative_path))
    problems.extend(_metadata_problems(repository_root))
    problems.extend(_single_loop_problems(repository_root))
    problems.extend(_ui_boundary_problems(repository_root))
    problems.extend(_sbom_gate_problems(repository_root))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit the repository's single Windows-local production closure")
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    problems = check(args.repository_root)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("repository production closure audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
