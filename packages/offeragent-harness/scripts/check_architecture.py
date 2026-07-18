from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

CORE_FORBIDDEN_ROOTS = {"django", "khoj", "langchain", "psycopg", "psycopg2"}
MODEL_FORBIDDEN_ROOTS = {"os", "pathlib", "shutil", "subprocess"}
MODEL_BOUNDARY_DIRECTORIES = {"models", "providers"}
MODEL_CLIENT_ROOTS = {"httpx", "anthropic", "openai"}
REMOVED_AGENT_LOOP_MODULES = {"interactive.py"}
RETIRED_MODEL_ADAPTER_MODULES = {"deepseek_chat.py", "factory.py", "ollama.py"}
RETIRED_MODEL_IMPORTS = {"deepseek_chat", "factory", "ollama"}
RUNTIME_CONTROL_MODULES = {"loopback_gateway.py", "loopback_server.py"}
RUNTIME_CONTROL_IMPORTS = {
    "aiohttp",
    "fastapi",
    "http.server",
    "socketserver",
    "starlette",
    "uvicorn",
    "websockets",
}
_PROBE_NAME = re.compile(r"(?:probe.*(?:model|vision|capabilit)|(?:model|vision|capabilit).*probe)", re.IGNORECASE)
_OBSIDIAN_AGENT_LOOP = re.compile(
    r"\b(?:class|interface|type)\s+\w*(?:AgentLoop|Planner)\b|\b(?:run|start)\w*AgentLoop\b"
)
_OBSIDIAN_RUNTIME_CONTROL = re.compile(
    r"\bnew\s+WebSocket\s*\(|\bWebSocketServer\b|\bloopbackWeb\b|"
    r"[\"\'](?:web/launch|models/health|secrets/(?:put|delete))[\"\']",
    re.IGNORECASE,
)
_OBSIDIAN_MODEL_NETWORK = re.compile(
    r"chatgpt\.com/backend-api/codex|api\.openai\.com|\bnew\s+OpenAI\s*\(|"
    r"\bfrom\s+[\"\'](?:openai|@anthropic-ai/sdk)[\"\']"
)
_OBSIDIAN_DIRECT_HTTP = re.compile(
    r"(?<![.\w])(?:fetch|axios|request|requestUrl)\s*\(|\bXMLHttpRequest\b|"
    r"\b(?:createServer|Deno\.serve|Bun\.serve)\s*\("
)
_OBSIDIAN_MODEL_OWNER = re.compile(r"(?:model|provider|codex|openai|llm|inference)", re.IGNORECASE)
_OBSIDIAN_MODEL_DECLARATION = re.compile(
    r"\b(?:class|interface|type|function)\s+\w*(?:model|provider|codex|openai|llm|inference)\w*",
    re.IGNORECASE,
)
_OBSIDIAN_CONTROL_OWNER = re.compile(
    r"(?:control|gateway|transport|server|worker_client|harness_client|runtime_client)", re.IGNORECASE
)
_OBSIDIAN_CONTROL_DECLARATION = re.compile(
    r"\b(?:class|interface|type|function)\s+(?:\w+(?:Control|Gateway|Transport|Server)|Control|Gateway|Transport)\b"
)


def import_names(tree: ast.AST, relative: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    names.add(node.module)
                continue
            package = ("offeragent_harness", *relative.parent.parts)
            ascend = node.level - 1
            if ascend >= len(package):
                continue
            base = package[: len(package) - ascend]
            if node.module:
                names.add(".".join((*base, *node.module.split("."))))
            else:
                names.update(".".join((*base, alias.name)) for alias in node.names)
    return names


def decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return decorator_name(node.func)
    return ""


def _canonical_retired_import(name: str, relative: Path) -> str | None:
    leaf = name.rsplit(".", 1)[-1]
    if leaf not in RETIRED_MODEL_IMPORTS:
        return None
    if name == leaf and relative.parts and relative.parts[0] == "providers":
        return f"offeragent_harness.providers.{leaf}"
    return name


def _contains_runtime_probe(tree: ast.AST, relative: Path) -> bool:
    if "probe" in relative.stem.casefold() and any(
        token in relative.stem.casefold() for token in ("model", "vision", "capability")
    ):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == "models/health":
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and _PROBE_NAME.search(node.name):
            return True
    return False


def _contains_runtime_control(tree: ast.AST, imports: set[str]) -> bool:
    if any(
        name == forbidden or name.startswith(f"{forbidden}.")
        for name in imports
        for forbidden in RUNTIME_CONTROL_IMPORTS
    ):
        return True
    server_calls = {
        "create_server",
        "HTTPServer",
        "run_app",
        "start_server",
        "start_unix_server",
        "ThreadingHTTPServer",
        "WebSocketServer",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        else:
            name = ""
        if name in server_calls:
            return True
        if name in {"bind", "listen", "serve_forever"} and isinstance(node.func, ast.Attribute):
            receiver = ast.unparse(node.func.value).casefold()
            if any(token in receiver for token in ("server", "socket", "sock", "listener")):
                return True
    return False


def _contains_obsidian_model_network(relative: str, text: str) -> bool:
    if _OBSIDIAN_MODEL_NETWORK.search(text):
        return True
    owns_model_boundary = bool(_OBSIDIAN_MODEL_OWNER.search(relative) or _OBSIDIAN_MODEL_DECLARATION.search(text))
    return bool(_OBSIDIAN_DIRECT_HTTP.search(text) and owns_model_boundary)


def _contains_obsidian_runtime_control(relative: str, text: str) -> bool:
    if _OBSIDIAN_RUNTIME_CONTROL.search(text):
        return True
    owns_control_boundary = bool(
        _OBSIDIAN_CONTROL_OWNER.search(Path(relative).stem) or _OBSIDIAN_CONTROL_DECLARATION.search(text)
    )
    return bool(_OBSIDIAN_DIRECT_HTTP.search(text) and owns_control_boundary)


def _obsidian_source(package_root: Path) -> Path | None:
    direct = package_root / "src" / "interface" / "obsidian" / "src"
    if direct.is_dir():
        return direct
    if package_root.parent.name == "packages":
        repository_source = package_root.parents[1] / "src" / "interface" / "obsidian" / "src"
        if repository_source.is_dir():
            return repository_source
    return None


def _check_obsidian_architecture(package_root: Path) -> list[str]:
    source = _obsidian_source(package_root)
    if source is None:
        return []
    problems: list[str] = []
    for path in sorted(source.rglob("*.ts")):
        relative = path.relative_to(source).as_posix()
        text = path.read_text(encoding="utf-8")
        display = f"src/interface/obsidian/src/{relative}"
        if path.name.casefold() in {"agent_loop.ts", "planner.ts"} or _OBSIDIAN_AGENT_LOOP.search(text):
            problems.append(f"{display}: Obsidian Agent loop module is forbidden")
        if _contains_obsidian_runtime_control(relative, text):
            problems.append(f"{display}: Obsidian HTTP/WebSocket Runtime control is forbidden")
        if _contains_obsidian_model_network(relative, text):
            problems.append(f"{display}: Obsidian model-network ownership is forbidden")
    return problems


def check(package_root: Path) -> list[str]:
    source = package_root / "src" / "offeragent_harness"
    problems: list[str] = []
    loop_entries: list[str] = []
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(source)
        if "testing" in relative.parts:
            continue
        if relative.name in REMOVED_AGENT_LOOP_MODULES:
            problems.append(f"{relative}: standalone Agent loop modules are forbidden")
        if relative.parts and relative.parts[0] == "providers" and relative.name in RETIRED_MODEL_ADAPTER_MODULES:
            problems.append(f"{relative}: retired model adapter module is forbidden")
        if relative.parts and relative.parts[0] == "runtime" and relative.name in RUNTIME_CONTROL_MODULES:
            problems.append(f"{relative}: HTTP/WebSocket Runtime control module is forbidden")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = import_names(tree, relative)
        for name in sorted(imports):
            retired = _canonical_retired_import(name, relative)
            if retired is not None and relative.name not in RETIRED_MODEL_ADAPTER_MODULES:
                problems.append(f"{relative}: retired model adapter import {retired}")
        if relative.parts and relative.parts[0] == "runtime":
            if any(
                name == "offeragent_harness.vault" or name.startswith("offeragent_harness.vault.") for name in imports
            ):
                problems.append(f"{relative}: Worker Runtime imports Vault implementation")
            if _contains_runtime_probe(tree, relative):
                problems.append(f"{relative}: runtime model/capability probes are forbidden")
            if relative.name not in RUNTIME_CONTROL_MODULES and _contains_runtime_control(tree, imports):
                problems.append(f"{relative}: HTTP/WebSocket Runtime control is forbidden")
        roots = {name.split(".", 1)[0] for name in imports}
        illegal = roots & CORE_FORBIDDEN_ROOTS
        if illegal:
            problems.append(f"{relative}: forbidden legacy imports {sorted(illegal)}")
        if any(
            name == "offeragent_harness.testing" or name.startswith("offeragent_harness.testing.") for name in imports
        ):
            problems.append(f"{relative}: production code imports test fakes")
        if relative.parts and relative.parts[0] in MODEL_BOUNDARY_DIRECTORIES:
            model_illegal = roots & MODEL_FORBIDDEN_ROOTS
            if model_illegal:
                problems.append(f"{relative}: model boundary imports {sorted(model_illegal)}")
            if any(
                name == "offeragent_harness.tools" or name.startswith("offeragent_harness.tools.") for name in imports
            ):
                problems.append(f"{relative}: model boundary imports tool implementation")
        elif roots & MODEL_CLIENT_ROOTS:
            problems.append(
                f"{relative}: direct model client imports are only allowed inside provider adapters "
                f"({sorted(roots & MODEL_CLIENT_ROOTS)})"
            )
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                decorator_name(decorator) == "agent_loop_entrypoint" for decorator in node.decorator_list
            ):
                loop_entries.append(f"{relative}:{node.lineno}:{node.name}")
    if len(loop_entries) != 1:
        problems.append(f"expected exactly one @agent_loop_entrypoint, found {len(loop_entries)}: {loop_entries}")
    problems.extend(_check_obsidian_architecture(package_root))
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
