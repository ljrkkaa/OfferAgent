from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "web"


def test_web_assets_are_generated_and_schema_pinned() -> None:
    manifest = json.loads((ROOT / "schema" / "protocol-manifest.json").read_text(encoding="utf-8"))
    source = (WEB / "assets" / "app.js").read_text(encoding="utf-8")
    assert f'const SCHEMA_HASH = "{manifest["schemaHash"]}";' in source
    assert (WEB / "assets" / "app.css").read_bytes() == (WEB / "styles.css").read_bytes()


def test_web_diagnostics_use_sanitized_sqlite_identity() -> None:
    source = (WEB / "assets" / "app.js").read_text(encoding="utf-8")
    assert '["SQLite 身份", runtime.databaseIdentity]' in source
    assert "runtime.databasePath" not in source


def test_web_root_obeys_strict_csp_without_inline_code() -> None:
    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert re.search(r"<script\s+src=\"/assets/app\.js\"\s+defer></script>", index)
    assert '<link rel="stylesheet" href="/assets/app.css">' in index
    assert re.search(r"<script(?!\s+src=)", index) is None
    assert re.search(r"\sstyle=", index, flags=re.IGNORECASE) is None
    assert re.search(r"\son[a-z]+=", index, flags=re.IGNORECASE) is None


def test_web_client_has_no_remote_agent_or_model_execution_path() -> None:
    source = (WEB / "assets" / "app.js").read_text(encoding="utf-8")
    forbidden = (
        "api.openai.com",
        "khoj",
        "Authorization",
        "Bearer ",
        "model -> tool",
        "while (true)",
        "WebSocket(",
    )
    for marker in forbidden:
        assert marker not in source
    fetch_targets = re.findall(r"fetch\(([^,]+),", source)
    assert fetch_targets == ["path"]
    assert 'jsonFetch("/auth/exchange"' in source
    assert 'jsonFetch("/api/command"' in source


def test_web_client_uses_semantic_terminal_events_only() -> None:
    source = (WEB / "assets" / "app.js").read_text(encoding="utf-8")
    for event in ("turn.completed", "turn.cancelled", "turn.failed", "turn.interrupted"):
        assert event in source
    assert 'result.accepted ? "completed"' not in source
    assert 'phase === "terminal"' not in source


def test_web_source_is_valid_javascript() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is optional for the Python-only Runtime test environment")
    completed = subprocess.run(
        [node, "--check", str(WEB / "app.js")],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_web_javascript_command_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is optional for the Python-only Runtime test environment")
    completed = subprocess.run(
        [node, str(Path(__file__).with_name("local_web_contract.cjs"))],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "local web single Worker tool authority contract passed" in completed.stdout


def test_web_client_drives_conversation_controls_through_real_commands() -> None:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    for command in (
        "turn/retry",
        "turn/steer",
        "session/fork",
        "session/compact",
        "session/rename",
        "session/delete",
    ):
        assert f'command("{command}"' in source
    assert "sourceRunId: run.runId" in source
    assert "runConfig: null" in source
    assert 'mode: "steer"' in source
    assert "hardDelete: false" in source
    assert 'window.addEventListener("beforeunload"' in source
    before_unload = source.split('window.addEventListener("beforeunload"', maxsplit=1)[1]
    assert 'command("turn/cancel"' not in before_unload


def test_web_client_uses_runtime_model_catalog() -> None:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    for command in ("models/list", "models/health"):
        assert f'command("{command}"' in source
    assert "provider: model.provider" in source
    assert "model: model.model" in source
    assert "gpt-5.4" not in source
    assert "gpt-5.5" not in source


def test_web_client_reads_artifacts_in_bounded_text_only_pages() -> None:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'command("artifact/read"' in source
    assert "const ARTIFACT_PAGE_BYTES = 65_536" in source
    assert "const ARTIFACT_TOTAL_BYTES = 524_288" in source
    assert "maxBytes: ARTIFACT_PAGE_BYTES" in source
    assert "pre.textContent = view.text" in source
    for unsafe_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert unsafe_sink not in source


def test_web_client_renders_diff_session_approval_and_subagent_timeline_items() -> None:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    assert '["本会话允许", "allow_session", "session", ""]' in source
    assert 'renderArtifactLinks(approval.diffs, "查看 Diff 内容")' in source
    assert "usageText(run.usage, run.startedAt, run.completedAt)" in source
    assert "renderReferences(run.references)" in source
    assert "for (const item of run.timeline)" in source
    assert 'item.kind === "subagent"' in source
    assert "appendTimelineItem(run, {" in source


def test_web_citations_come_from_terminal_tool_facts_not_model_content() -> None:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    assert "const sourceRefs = Array.isArray(result.sourceRefs) ? result.sourceRefs : []" in source
    assert "run.references = mergeRefs(run.references, sourceRefs)" in source
    assistant_projection = source[
        source.index("function applyAssistantContent") : source.index("function applyPartialContent")
    ]
    content_projection = source[source.index("function contentProjection") : source.index("function mergeRefs")]
    assert "projection.references" not in assistant_projection
    assert "block.references" not in content_projection


def test_web_event_reducer_is_idempotent_and_fail_closed_on_gaps() -> None:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    assert "if (run.lastSequence >= event.sequence) return" in source
    assert "if (run.lastSequence + 1 !== event.sequence)" in source
    assert 'throw new Error("Session replay Run 游标回退")' in source
    assert 'throw new Error("事件 replay 分页没有取得进展")' in source
    assert 'throw new Error("Run lineage changed")' in source


def test_web_client_reads_real_extension_management_state_without_loopback_mutations() -> None:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    for command in ("skills/list", "skills/status", "shell/list", "hooks/list"):
        assert f'command("{command}"' in source
    for command in (
        "shell/install",
        "shell/confirm",
        "shell/set-enabled",
        "hooks/install",
        "hooks/confirm-layer",
        "hooks/confirm-workspace-command",
    ):
        assert f'command("{command}"' not in source
    assert "所有工具执行都由 Worker 的统一权限与审计链负责" in source
    assert "enabledSkills: [...state.selectedSkills].sort()" not in source
    assert "管理命令未暴露" not in source


def test_web_has_no_plugin_owned_or_headless_vault_execution_path() -> None:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    assert "vault/headless" not in source
    assert "headlessVaultWrite" not in source
    assert "clientTools" not in source
    assert "reverseRequests" not in source


def test_web_has_no_separate_memory_admin_protocol() -> None:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    for command in (
        "memory/settings",
        "memory/configure",
        "memory/list",
        "memory/get",
        "memory/review",
        "memory/edit",
        "memory/delete",
        "memory/export",
    ):
        assert f'command("{command}"' not in source
    assert "memory: true" not in source
    assert "正在读取固定 Vault Memory 文件" in source


def test_web_root_exposes_complete_local_controls() -> None:
    index = (WEB / "index.html").read_text(encoding="utf-8")
    for control_id in (
        "new-session",
        "rename-session",
        "compact-session",
        "delete-session",
        "model",
        "model-health",
        "steer",
        "cancel",
        "capabilities",
    ):
        assert f'id="{control_id}"' in index
    assert 'id="memory"' not in index
