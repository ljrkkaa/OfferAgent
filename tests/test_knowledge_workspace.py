import json
from types import SimpleNamespace

import pytest

from khoj.processor.conversation.knowledge_workspace import (
    _tool_result_text,
    available_workspace_tools,
    execute_workspace_tool_calls,
    get_workspace_sources,
    search_workspace,
    workspace_planner_context,
)
from khoj.processor.conversation.utils import ResponseWithThought, ToolCall


def model_responses(*responses):
    calls = list(responses)

    async def send_message(**kwargs):
        if not calls:
            raise AssertionError("unexpected model call")
        return ResponseWithThought(text=calls.pop(0))

    return send_message


async def no_model_call(**kwargs):
    raise AssertionError("tool execution should not call the model")


def call(name, args, id="1"):
    return ToolCall(name=name, args=args, id=id)


def test_tool_result_compaction_preserves_head_and_tail_instructions():
    compacted = _tool_result_text(f"skill-head{'x' * 12000}skill-tail", limit=8000)

    assert len(compacted) <= 8000
    assert compacted.startswith("skill-head")
    assert compacted.endswith("skill-tail")
    assert "tool result truncated" in compacted


@pytest.mark.asyncio
async def test_execute_workspace_tools_reads_exact_file_lines(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("alpha\nRedis evidence\nomega\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    result = await execute_workspace_tool_calls(
        "Redis 怎么说？",
        [],
        object(),
        None,
        [call("view_file", {"path": "notes.md", "start_line": 2, "end_line": 2})],
        send_message=no_model_call,
    )

    assert result.references[0]["uri"] == "local-kb://notes.md#L2-L2"
    assert "Redis evidence" in result.references[0]["compiled"]


@pytest.mark.asyncio
async def test_execute_workspace_tools_prepares_grounded_user_content(tmp_path, monkeypatch):
    target = tmp_path / "notes.md"
    target.write_text("# 复盘\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    result = await execute_workspace_tool_calls(
        "写进 notes.md：HashMap 扩容要讲清楚。",
        [],
        object(),
        None,
        [
            call(
                "append_note",
                {
                    "path": "notes.md",
                    "content": "HashMap 扩容要讲清楚。",
                    "heading": "复盘",
                    "write_intent": "preserve",
                    "source_refs": [{"type": "current_user_request"}],
                },
            )
        ],
        send_message=model_responses(json.dumps({"grounded": True, "reason": "copied from request"})),
        write_mode="client_actions",
    )

    assert target.read_text(encoding="utf-8") == "# 复盘\n"
    assert result.references[-1]["status"] == "action_prepared"
    assert result.references[-1]["vault_action"]["content"] == "HashMap 扩容要讲清楚。"


@pytest.mark.asyncio
async def test_execute_workspace_tools_prepares_client_action_without_server_write(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    args = {
        "path": "daily/2026-07-09.md",
        "content": "# 2026-07-09 每日计划\n",
        "write_intent": "preserve",
        "source_refs": [{"type": "current_user_request"}],
    }

    result = await execute_workspace_tool_calls(
        "创建 daily/2026-07-09.md，内容是：# 2026-07-09 每日计划",
        [],
        object(),
        None,
        [call("append_note", args)],
        send_message=model_responses(json.dumps({"grounded": True, "reason": "copied from request"})),
        write_mode="client_actions",
    )

    assert not (tmp_path / "daily" / "2026-07-09.md").exists()
    assert result.references[-1]["vault_action"] == {
        "op": "create_file",
        "path": "daily/2026-07-09.md",
        "content": "# 2026-07-09 每日计划\n",
        "mode": "create_only",
    }


@pytest.mark.asyncio
async def test_execute_workspace_tools_rejects_drift_then_accepts_grounded_retry(tmp_path, monkeypatch):
    target = tmp_path / "daily.md"
    target.write_text("# Daily\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    history = [SimpleNamespace(by="khoj", message="- [ ] 复习携程面经\n- [ ] 梳理 RAG 项目回答")]
    shared = {
        "path": "daily.md",
        "source_refs": [{"type": "assistant_message", "turn": -1}],
        "write_intent": "adapt_to_template",
    }

    result = await execute_workspace_tool_calls(
        "把这个写进学习日记",
        history,
        object(),
        None,
        [
            call("append_note", {**shared, "content": "- [ ] 处理 Windows 同步问题"}, "1"),
            call("append_note", {**shared, "content": "- [ ] 复习携程面经\n- [ ] 梳理 RAG 项目回答"}, "2"),
        ],
        send_message=model_responses(
            json.dumps({"grounded": False, "reason": "unrelated topic"}),
            json.dumps({"grounded": True, "reason": "same plan"}),
        ),
        write_mode="client_actions",
    )

    text = target.read_text(encoding="utf-8")
    assert "Windows 同步问题" not in text
    assert "梳理 RAG 项目回答" not in text
    assert [ref["status"] for ref in result.references] == ["source_mismatch", "action_prepared"]


@pytest.mark.asyncio
async def test_execute_workspace_tools_uses_artifact_as_exact_source(tmp_path, monkeypatch):
    target = tmp_path / "daily.md"
    target.write_text("# Daily\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    history = [
        SimpleNamespace(
            by="khoj",
            message="plan",
            artifacts=[
                {
                    "id": "assistant:plan",
                    "type": "assistant_response",
                    "content": "- [ ] 复习携程面经",
                    "source_refs": [],
                }
            ],
        )
    ]

    result = await execute_workspace_tool_calls(
        "把刚才规划写进学习日记",
        history,
        object(),
        None,
        [
            call(
                "append_note",
                {
                    "path": "daily.md",
                    "artifact_id": "assistant:plan",
                    "write_intent": "preserve",
                    "source_refs": [{"type": "artifact", "id": "assistant:plan"}],
                },
            )
        ],
        send_message=no_model_call,
        write_mode="client_actions",
    )

    assert target.read_text(encoding="utf-8") == "# Daily\n"
    assert result.references[-1]["status"] == "action_prepared"
    assert result.references[-1]["vault_action"]["content"] == "- [ ] 复习携程面经"


@pytest.mark.asyncio
async def test_execute_workspace_tools_rejects_missing_artifact(tmp_path, monkeypatch):
    target = tmp_path / "daily.md"
    target.write_text("# Daily\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    result = await execute_workspace_tool_calls(
        "把 artifact 写入 daily.md",
        [],
        object(),
        None,
        [
            call(
                "append_note",
                {
                    "path": "daily.md",
                    "artifact_id": "assistant:missing",
                    "write_intent": "preserve",
                    "source_refs": [{"type": "artifact", "id": "assistant:missing"}],
                },
            )
        ],
        send_message=no_model_call,
        write_mode="client_actions",
    )

    assert target.read_text(encoding="utf-8") == "# Daily\n"
    assert result.references[-1]["status"] == "artifact_not_found"


@pytest.mark.asyncio
async def test_execute_workspace_tools_requires_read_before_edit(tmp_path, monkeypatch):
    target = tmp_path / "notes.md"
    target.write_text("Redis answer\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    blocked = await execute_workspace_tool_calls(
        "修改 notes.md",
        [],
        object(),
        None,
        [call("propose_edit", {"path": "notes.md", "find": "Redis answer", "replace": "Redis final"})],
        send_message=no_model_call,
        write_mode="client_actions",
    )
    allowed = await execute_workspace_tool_calls(
        "修改 notes.md",
        [],
        object(),
        None,
        [
            call("view_file", {"path": "notes.md"}, "1"),
            call("propose_edit", {"path": "notes.md", "find": "Redis answer", "replace": "Redis final"}, "2"),
        ],
        send_message=no_model_call,
        write_mode="client_actions",
    )

    assert blocked.references[-1]["status"] == "edit_source_required"
    assert allowed.references[-1]["status"] == "action_prepared"
    assert allowed.references[-1]["vault_action"]["op"] == "replace_text"
    assert target.read_text(encoding="utf-8") == "Redis answer\n"


@pytest.mark.asyncio
async def test_execute_workspace_tools_disables_writes_without_client_actions(tmp_path, monkeypatch):
    target = tmp_path / "notes.md"
    target.write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    result = await execute_workspace_tool_calls(
        "写进 notes.md",
        [],
        object(),
        None,
        [
            call(
                "append_note",
                {
                    "path": "notes.md",
                    "content": "new",
                    "write_intent": "preserve",
                    "source_refs": [{"type": "current_user_request"}],
                },
            )
        ],
        send_message=no_model_call,
    )

    assert target.read_text(encoding="utf-8") == "old\n"
    assert result.references == []
    assert result.errors == ["Notes tool is not available: append_note"]


@pytest.mark.asyncio
async def test_execute_workspace_tools_calls_openkb(monkeypatch):
    async def fake_wiki_search_documents(*args, **kwargs):
        return (
            [
                {
                    "query": "openkb:Redis",
                    "file": "wiki/index.md",
                    "uri": "openkb://local/wiki/index.md",
                    "compiled": "Redis",
                }
            ],
            ["openkb:Redis"],
            args[0],
        )

    monkeypatch.setattr(
        "khoj.processor.conversation.knowledge_workspace.wiki_search_documents", fake_wiki_search_documents
    )

    result = await execute_workspace_tool_calls(
        "Redis",
        [],
        object(),
        None,
        [call("wiki_search_documents", {"query": "Redis", "n": 1})],
        send_message=no_model_call,
        allow_local_kb=False,
        allow_openkb=True,
    )

    assert result.references[0]["uri"] == "openkb://local/wiki/index.md"


@pytest.mark.asyncio
async def test_execute_workspace_tools_rejects_unavailable_tool(tmp_path, monkeypatch):
    (tmp_path / "secret.md").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    result = await execute_workspace_tool_calls(
        "Redis",
        [],
        object(),
        None,
        [call("view_file", {"path": "secret.md"})],
        send_message=no_model_call,
        allow_local_kb=False,
        allow_openkb=True,
    )

    assert result.references == []
    assert result.errors == ["Notes tool is not available: view_file"]


def test_workspace_planner_context_loads_safe_profile_and_skill(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skills" / "note-taking" / "obsidian"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: obsidian\ndescription: Read and write Obsidian notes.\n---\nUse wikilinks.\n",
        encoding="utf-8",
    )
    (tmp_path / "agents.md").write_text("Daily notes live under daily/.", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    context = workspace_planner_context(allow_local_kb=True)
    names = {tool.name for tool in available_workspace_tools(allow_local_kb=True, allow_openkb=False)}

    assert "Daily notes live under daily/." in context
    assert "obsidian" in context
    assert "read_skill" in names


def test_workspace_planner_context_hides_local_data_when_disabled(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".codex" / "skills" / "private"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: private\ndescription: Private instructions.\n---\nsecret\n",
        encoding="utf-8",
    )
    (tmp_path / "agents.md").write_text("private vault profile", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    context = workspace_planner_context(allow_local_kb=False)
    names = {tool.name for tool in available_workspace_tools(allow_local_kb=False, allow_openkb=True)}

    assert "private vault profile" not in context
    assert "Private instructions" not in context
    assert "read_skill" not in names


@pytest.mark.asyncio
async def test_execute_workspace_tools_revalidates_skill_path(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".codex" / "skills" / "obsidian"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: obsidian\ndescription: Obsidian notes.\n---\nsafe instructions\n",
        encoding="utf-8",
    )
    outside = tmp_path.parent / "outside-skill.md"
    outside.write_text("outside secret", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    assert "read_skill" in {tool.name for tool in available_workspace_tools(allow_local_kb=True, allow_openkb=False)}

    skill_file.unlink()
    skill_file.symlink_to(outside)
    result = await execute_workspace_tool_calls(
        "读取技能",
        [],
        object(),
        None,
        [call("read_skill", {"name": "obsidian"})],
        send_message=no_model_call,
    )

    assert "outside secret" not in json.dumps(result.tool_transcript, ensure_ascii=False)
    assert result.errors == ["Notes tool is not available: read_skill"]


def test_workspace_planner_context_skips_profile_symlink_escape(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside-agents.md"
    outside.write_text("outside vault instructions", encoding="utf-8")
    (tmp_path / "agents.md").symlink_to(outside)
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    context = workspace_planner_context(allow_local_kb=True)

    assert "outside vault instructions" not in context


def test_workspace_sources_apply_engine_policy_once(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("local", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_KB_ENGINE", "file_first")

    file_first = get_workspace_sources()
    monkeypatch.setenv("KHOJ_KB_ENGINE", "openkb")
    openkb_only = get_workspace_sources()

    assert file_first.local_enabled is True
    assert file_first.openkb_enabled is False
    assert openkb_only.local_enabled is False


@pytest.mark.asyncio
async def test_workspace_search_uses_index_only_without_file_sources(monkeypatch):
    user = SimpleNamespace(uuid="user-1")
    hit = SimpleNamespace()

    async def fake_query(*args, **kwargs):
        return [hit]

    def fake_collate(hits):
        return iter(
            [
                SimpleNamespace(
                    entry="indexed evidence",
                    score=0.1,
                    corpus_id="entry-1",
                    additional={"file": "notes.md", "source": "indexed"},
                )
            ]
        )

    monkeypatch.delenv("KHOJ_LOCAL_KB_PATH", raising=False)
    monkeypatch.delenv("KHOJ_OBSIDIAN_VAULT_PATH", raising=False)
    monkeypatch.setenv("KHOJ_KB_ENGINE", "file_first")
    monkeypatch.setattr("khoj.processor.conversation.knowledge_workspace.text_search.query", fake_query)
    monkeypatch.setattr("khoj.processor.conversation.knowledge_workspace.text_search.collate_results", fake_collate)

    results = await search_workspace("Redis", user)

    assert results[0].entry == "indexed evidence"
    assert results[0].additional["source"] == "indexed"
