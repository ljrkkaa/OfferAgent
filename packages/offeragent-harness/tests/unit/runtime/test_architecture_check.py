from __future__ import annotations

from pathlib import Path

from scripts.check_architecture import check


def _package(tmp_path: Path, provider_source: str) -> Path:
    package = tmp_path / "src" / "offeragent_harness"
    (package / "agent").mkdir(parents=True)
    (package / "providers").mkdir()
    (package / "agent" / "loop.py").write_text(
        "@agent_loop_entrypoint\ndef run():\n    return None\n",
        encoding="utf-8",
    )
    (package / "providers" / "adapter.py").write_text(provider_source, encoding="utf-8")
    return tmp_path


def test_architecture_check_covers_the_real_model_provider_adapter_directory(tmp_path: Path) -> None:
    root = _package(tmp_path, "import subprocess\n")

    problems = check(root)

    assert len(problems) == 1
    assert problems[0].replace("\\", "/") == ("providers/adapter.py: model boundary imports ['subprocess']")


def test_architecture_check_accepts_an_inference_only_provider(tmp_path: Path) -> None:
    root = _package(tmp_path, "from collections.abc import AsyncIterator\n")

    assert check(root) == []


def test_architecture_check_rejects_retired_provider_and_probe_surfaces(tmp_path: Path) -> None:
    root = _package(tmp_path, "from .deepseek_chat import DeepSeekChatGateway\n")
    package = root / "src" / "offeragent_harness"
    (package / "providers" / "deepseek_chat.py").write_text("class DeepSeekChatGateway: ...\n", encoding="utf-8")
    (package / "runtime").mkdir()
    (package / "runtime" / "model_probe.py").write_text(
        'METHOD = "models/health"\ndef probe_vision_capability(): ...\n',
        encoding="utf-8",
    )

    problems = [problem.replace("\\", "/") for problem in check(root)]

    assert "providers/deepseek_chat.py: retired model adapter module is forbidden" in problems
    assert "providers/adapter.py: retired model adapter import offeragent_harness.providers.deepseek_chat" in problems
    assert "runtime/model_probe.py: runtime model/capability probes are forbidden" in problems


def test_architecture_check_rejects_worker_vault_implementation_and_control_transport(tmp_path: Path) -> None:
    root = _package(tmp_path, "from collections.abc import AsyncIterator\n")
    package = root / "src" / "offeragent_harness"
    (package / "runtime").mkdir()
    (package / "runtime" / "worker.py").write_text(
        "from offeragent_harness.vault import VaultTransactionCoordinator\n",
        encoding="utf-8",
    )
    (package / "runtime" / "relative_worker.py").write_text(
        "from ..vault import VaultTransactionCoordinator\n",
        encoding="utf-8",
    )
    (package / "runtime" / "loopback_server.py").write_text("class AsyncioLoopbackServer: ...\n", encoding="utf-8")
    (package / "runtime" / "http_control.py").write_text(
        "import asyncio\n\nasync def serve():\n    return await asyncio.start_server(lambda: None, '127.0.0.1', 0)\n",
        encoding="utf-8",
    )

    problems = [problem.replace("\\", "/") for problem in check(root)]

    assert "runtime/worker.py: Worker Runtime imports Vault implementation" in problems
    assert "runtime/relative_worker.py: Worker Runtime imports Vault implementation" in problems
    assert "runtime/loopback_server.py: HTTP/WebSocket Runtime control module is forbidden" in problems
    assert "runtime/http_control.py: HTTP/WebSocket Runtime control is forbidden" in problems


def test_architecture_check_rejects_obsidian_agent_loop_and_runtime_control(tmp_path: Path) -> None:
    root = _package(tmp_path, "from collections.abc import AsyncIterator\n")
    obsidian = root / "src" / "interface" / "obsidian" / "src"
    (obsidian / "runtime").mkdir(parents=True)
    (obsidian / "agent_loop.ts").write_text("export function runAgentLoop() {}\n", encoding="utf-8")
    (obsidian / "runtime" / "control.ts").write_text(
        'export const socket = new WebSocket("ws://127.0.0.1:8765");\n',
        encoding="utf-8",
    )
    (obsidian / "planner.ts").write_text("export class Planner {}\n", encoding="utf-8")
    (obsidian / "model_planner.ts").write_text("export class ModelPlanner {}\n", encoding="utf-8")
    (obsidian / "runtime" / "model_client.ts").write_text(
        'fetch("https://chatgpt.com/backend-api/codex/responses");\n',
        encoding="utf-8",
    )
    (obsidian / "runtime" / "injected_model_client.ts").write_text(
        "export class ModelClient {\n"
        "  constructor(private readonly endpoint: string) {}\n"
        "  send() { return fetch(this.endpoint); }\n"
        "}\n",
        encoding="utf-8",
    )
    (obsidian / "runtime" / "http_control.ts").write_text(
        "export function control(loopbackEndpoint: string) { return fetch(loopbackEndpoint); }\n",
        encoding="utf-8",
    )

    problems = [problem.replace("\\", "/") for problem in check(root)]

    assert "src/interface/obsidian/src/agent_loop.ts: Obsidian Agent loop module is forbidden" in problems
    assert (
        "src/interface/obsidian/src/runtime/control.ts: Obsidian HTTP/WebSocket Runtime control is forbidden"
        in problems
    )
    assert "src/interface/obsidian/src/planner.ts: Obsidian Agent loop module is forbidden" in problems
    assert "src/interface/obsidian/src/model_planner.ts: Obsidian Agent loop module is forbidden" in problems
    assert (
        "src/interface/obsidian/src/runtime/model_client.ts: Obsidian model-network ownership is forbidden" in problems
    )
    assert (
        "src/interface/obsidian/src/runtime/injected_model_client.ts: Obsidian model-network ownership is forbidden"
        in problems
    )
    assert (
        "src/interface/obsidian/src/runtime/http_control.ts: Obsidian HTTP/WebSocket Runtime control is forbidden"
        in problems
    )


def test_architecture_check_allows_research_proxy_and_harness_rpc_calls(tmp_path: Path) -> None:
    root = _package(tmp_path, "from collections.abc import AsyncIterator\n")
    obsidian = root / "src" / "interface" / "obsidian" / "src" / "runtime"
    obsidian.mkdir(parents=True)
    (obsidian / "research_browser.ts").write_text(
        'import { createServer } from "node:http";\n'
        "export const proxy = createServer((_request, _response) => undefined);\n",
        encoding="utf-8",
    )
    (obsidian / "chat_store.ts").write_text(
        "export function start(client: HarnessClient) {\n"
        '  return client.request("turn/start", { model: "catalog-id" });\n'
        "}\n",
        encoding="utf-8",
    )

    assert check(root) == []
