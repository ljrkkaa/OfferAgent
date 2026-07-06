import json
from types import SimpleNamespace

import pytest

from khoj.database.models import ChatMessageModel
from khoj.processor.conversation import utils as convo_utils
from khoj.processor.conversation.notes_tool_loop import collect_notes_evidence_with_tools
from khoj.processor.conversation.utils import ResponseWithThought


def fake_model(*responses):
    calls = list(responses)

    async def send_message(**kwargs):
        if calls:
            return ResponseWithThought(text=calls.pop(0))
        return ResponseWithThought(text="done")

    return send_message


def test_chat_message_model_preserves_artifacts():
    message = ChatMessageModel(
        by="khoj",
        message="answer",
        artifacts=[{"id": "assistant:turn-1", "type": "assistant_response", "content": "answer"}],
    )

    assert message.model_dump()["artifacts"][0]["id"] == "assistant:turn-1"


@pytest.mark.asyncio
async def test_save_to_conversation_log_adds_assistant_artifact(monkeypatch):
    captured = {}

    async def fake_save_conversation(*args, **kwargs):
        captured["messages"] = args[1]
        return SimpleNamespace(id="conv-1", agent=None)

    monkeypatch.setattr(convo_utils.ConversationAdapters, "save_conversation", fake_save_conversation)

    await convo_utils.save_to_conversation_log(
        "根据 experiences/a.md 规划",
        "## 今日计划\n- [ ] 复习 RAG",
        user=SimpleNamespace(username="tester"),
        compiled_references=[
            {
                "query": "view_file:experiences/a.md",
                "file": "experiences/a.md",
                "uri": "local-kb://experiences/a.md#L1-L3",
                "compiled": "large source text should not be duplicated",
            }
        ],
        automation_id="skip-memory",
        tracer={"mid": "turn-1"},
    )

    khoj_message = captured["messages"][1]
    artifact = khoj_message.artifacts[0]
    assert artifact["id"] == "assistant:turn-1"
    assert artifact["content"] == "## 今日计划\n- [ ] 复习 RAG"
    assert artifact["source_refs"] == [
        {
            "query": "view_file:experiences/a.md",
            "file": "experiences/a.md",
            "uri": "local-kb://experiences/a.md#L1-L3",
        }
    ]


@pytest.mark.asyncio
async def test_notes_tool_loop_reads_llm_selected_file_lines(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("alpha\nRedis evidence\nomega\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    result = await collect_notes_evidence_with_tools(
        "Redis 怎么说？",
        [],
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps(
                [{"name": "view_file", "args": {"path": "notes.md", "start_line": 2, "end_line": 2}, "id": "1"}]
            ),
            "done",
        ),
    )

    assert result.references
    assert result.references[0]["uri"] == "local-kb://notes.md#L2-L2"
    assert "Redis evidence" in result.references[0]["compiled"]


@pytest.mark.asyncio
async def test_notes_tool_loop_appends_only_when_model_calls_write_tool(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("# 复盘\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")

    result = await collect_notes_evidence_with_tools(
        "写进 notes.md：HashMap 扩容要讲清楚。",
        [],
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {"path": "notes.md", "content": "HashMap 扩容要讲清楚。", "heading": "复盘"},
                            "id": "1",
                        }
                    ]
                }
            ),
            json.dumps({"grounded": True, "reason": "matches the previous assistant plan"}),
            "done",
        ),
    )

    assert "HashMap 扩容要讲清楚。" in (tmp_path / "notes.md").read_text(encoding="utf-8")
    assert result.references[-1]["query"] == "append_note"
    assert result.references[-1]["status"] == "written"


@pytest.mark.asyncio
async def test_notes_tool_loop_retries_source_bound_append_when_content_drifts(tmp_path, monkeypatch):
    daily = tmp_path / "daily" / "2026-07-01.md"
    daily.parent.mkdir()
    daily.write_text("# 2026-07-01 每日计划\n\n## 今日计划\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    chat_history = [
        SimpleNamespace(by="you", message="根据 experiences/面经-携程-AI应用开发-截图.md 给我规划下今日的学习日记"),
        SimpleNamespace(
            by="khoj",
            message=(
                "## 今日打卡\n"
                "- [ ] 携程 AI 应用开发面经\n\n"
                "## 今日计划\n"
                "- [ ] 复习携程 AI 应用开发面经\n"
                "- [ ] 梳理 RAG 项目回答\n"
                "- [ ] 准备 Agent 工程化追问\n"
            ),
        ),
    ]

    result = await collect_notes_evidence_with_tools(
        "把这个写进学习日记中",
        chat_history,
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "daily/2026-07-01.md",
                                "heading": "今日计划",
                                "content": "- 处理 raw/agent-e2e-2026-06-30.md 和 Windows 同步问题\n- 复习携程 AI 应用开发",
                                "source_refs": [{"type": "assistant_message", "turn": -1}],
                                "write_intent": "adapt_to_template",
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "daily/2026-07-01.md",
                                "heading": "今日计划",
                                "content": (
                                    "- [ ] 复习携程 AI 应用开发面经\n"
                                    "- [ ] 梳理 RAG 项目回答\n"
                                    "- [ ] 准备 Agent 工程化追问"
                                ),
                                "source_refs": [{"type": "assistant_message", "turn": -1}],
                                "write_intent": "adapt_to_template",
                            },
                            "id": "2",
                        }
                    ]
                }
            ),
            json.dumps({"grounded": True, "reason": "same plan"}),
            "done",
        ),
    )

    text = daily.read_text(encoding="utf-8")
    assert "raw/agent-e2e-2026-06-30.md" not in text
    assert "Windows 同步问题" not in text
    assert "携程 AI 应用开发面经" in text
    assert [ref["status"] for ref in result.references if ref["query"] == "append_note"] == ["source_mismatch", "written"]


@pytest.mark.asyncio
async def test_notes_tool_loop_uses_verifier_for_non_file_drift(tmp_path, monkeypatch):
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "2026-07-01.md").write_text("# 2026-07-01 每日计划\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    chat_history = [SimpleNamespace(by="khoj", message="- [ ] 复习携程 AI 应用开发面经\n- [ ] 梳理 RAG 项目回答")]

    result = await collect_notes_evidence_with_tools(
        "把这个写进学习日记中",
        chat_history,
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "daily/2026-07-01.md",
                                "content": "- [ ] 处理 Windows 同步问题\n- [ ] 复习携程 AI 应用开发面经",
                                "source_refs": [{"type": "assistant_message", "turn": -1}],
                                "write_intent": "adapt_to_template",
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            json.dumps({"grounded": False, "reason": "Windows sync is not in the source"}),
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "daily/2026-07-01.md",
                                "content": "- [ ] 复习携程 AI 应用开发面经\n- [ ] 梳理 RAG 项目回答",
                                "source_refs": [{"type": "assistant_message", "turn": -1}],
                                "write_intent": "adapt_to_template",
                            },
                            "id": "2",
                        }
                    ]
                }
            ),
            json.dumps({"grounded": True, "reason": "matches the source"}),
            "done",
        ),
    )

    text = (tmp_path / "daily" / "2026-07-01.md").read_text(encoding="utf-8")
    assert "Windows 同步问题" not in text
    assert "梳理 RAG 项目回答" in text
    assert [ref["status"] for ref in result.references if ref["query"] == "append_note"] == ["source_mismatch", "written"]


@pytest.mark.asyncio
async def test_notes_tool_loop_requires_source_refs_for_generated_append_content(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("# 计划\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    chat_history = [SimpleNamespace(by="khoj", message="- [ ] 复习携程 AI 应用开发面经")]

    result = await collect_notes_evidence_with_tools(
        "把这个写进 notes.md",
        chat_history,
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps(
                {"calls": [{"name": "append_note", "args": {"path": "notes.md", "content": "- [ ] 复习携程 AI 应用开发面经"}, "id": "1"}]}
            ),
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "notes.md",
                                "content": "- [ ] 复习携程 AI 应用开发面经",
                                "source_refs": [{"type": "assistant_message", "turn": -1}],
                                "write_intent": "preserve",
                            },
                            "id": "2",
                        }
                    ]
                }
            ),
            json.dumps({"grounded": True, "reason": "template adaptation from interview note"}),
            "done",
        ),
    )

    assert (tmp_path / "notes.md").read_text(encoding="utf-8").count("复习携程 AI 应用开发面经") == 1
    assert [ref["status"] for ref in result.references if ref["query"] == "append_note"] == ["source_refs_required", "written"]


@pytest.mark.asyncio
async def test_notes_tool_loop_appends_artifact_content_without_recopied_chat(tmp_path, monkeypatch):
    (tmp_path / "daily").mkdir()
    target = tmp_path / "daily" / "2026-07-01.md"
    target.write_text("# 2026-07-01\n\n## 今日计划\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    chat_history = [
        SimpleNamespace(
            by="khoj",
            message="上一轮展示文本",
            artifacts=[
                {
                    "id": "assistant:plan",
                    "type": "assistant_response",
                    "content": "- [ ] 复习携程 AI 应用开发面经\n- [ ] 梳理 RAG 项目回答",
                    "source_refs": [{"file": "experiences/面经-携程-AI应用开发-截图.md"}],
                }
            ],
        ),
        SimpleNamespace(by="you", message="raw/agent-e2e-2026-06-30.md 在哪"),
        SimpleNamespace(
            by="khoj",
            message="Windows 同步问题说明",
            artifacts=[
                {
                    "id": "assistant:sync",
                    "type": "assistant_response",
                    "content": "- [ ] 处理 Windows 同步问题",
                    "source_refs": [],
                }
            ],
        ),
    ]

    result = await collect_notes_evidence_with_tools(
        "把刚才规划写进学习日记",
        chat_history,
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "daily/2026-07-01.md",
                                "heading": "今日计划",
                                "artifact_id": "assistant:plan",
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            "done",
        ),
    )

    text = target.read_text(encoding="utf-8")
    assert "携程 AI 应用开发面经" in text
    assert "Windows 同步问题" not in text
    assert "raw/agent-e2e-2026-06-30.md" not in text
    assert [ref["status"] for ref in result.references if ref["query"] == "append_note"] == ["written"]


@pytest.mark.asyncio
async def test_notes_tool_loop_verifies_content_adapted_from_artifact(tmp_path, monkeypatch):
    (tmp_path / "daily.md").write_text("# Daily\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    chat_history = [
        SimpleNamespace(
            by="khoj",
            message="plan",
            artifacts=[
                {
                    "id": "assistant:plan",
                    "type": "assistant_response",
                    "content": "- [ ] 复习携程 AI 应用开发面经\n- [ ] 梳理 RAG 项目回答",
                    "source_refs": [],
                }
            ],
        )
    ]

    result = await collect_notes_evidence_with_tools(
        "把这个整理成日记格式写入 daily.md",
        chat_history,
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "daily.md",
                                "artifact_id": "assistant:plan",
                                "content": "## 今日计划\n- [ ] 复习携程 AI 应用开发面经\n- [ ] 梳理 RAG 项目回答",
                                "source_refs": [{"type": "artifact", "id": "assistant:plan"}],
                                "write_intent": "adapt_to_template",
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            json.dumps({"grounded": True, "reason": "adapted from artifact"}),
            "done",
        ),
    )

    assert "## 今日计划" in (tmp_path / "daily.md").read_text(encoding="utf-8")
    assert [ref["status"] for ref in result.references if ref["query"] == "append_note"] == ["written"]


@pytest.mark.asyncio
async def test_notes_tool_loop_requires_read_before_propose_edit(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Redis answer\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    result = await collect_notes_evidence_with_tools(
        "把 notes.md 里的 Redis answer 改成 Redis final answer",
        [],
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "propose_edit",
                            "args": {"path": "notes.md", "find": "Redis answer", "replace": "Redis final answer"},
                            "id": "1",
                        }
                    ]
                }
            ),
            "done",
        ),
    )

    assert [ref["status"] for ref in result.references if ref["query"] == "propose_edit"] == [
        "edit_source_required"
    ]
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "Redis answer\n"


@pytest.mark.asyncio
async def test_notes_tool_loop_allows_propose_edit_after_view_file(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Redis answer\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    result = await collect_notes_evidence_with_tools(
        "把 notes.md 里的 Redis answer 改成 Redis final answer",
        [],
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps(
                {
                    "calls": [
                        {"name": "view_file", "args": {"path": "notes.md"}, "id": "1"},
                        {
                            "name": "propose_edit",
                            "args": {"path": "notes.md", "find": "Redis answer", "replace": "Redis final answer"},
                            "id": "2",
                        },
                    ]
                }
            ),
            "done",
        ),
    )

    statuses = [ref["status"] for ref in result.references if ref["query"] == "propose_edit"]
    assert statuses == ["proposed"]
    assert "-Redis answer" in result.references[-1]["compiled"]
    assert "+Redis final answer" in result.references[-1]["compiled"]
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "Redis answer\n"


@pytest.mark.asyncio
async def test_notes_tool_loop_allows_propose_edit_with_file_source_ref(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Redis answer\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    result = await collect_notes_evidence_with_tools(
        "把 notes.md 里的 Redis answer 改成 Redis final answer",
        [],
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "propose_edit",
                            "args": {
                                "path": "notes.md",
                                "find": "Redis answer",
                                "replace": "Redis final answer",
                                "source_refs": [{"type": "file", "path": "notes.md", "start_line": 1, "end_line": 1}],
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            "done",
        ),
    )

    assert [ref["status"] for ref in result.references if ref["query"] == "propose_edit"] == ["proposed"]


@pytest.mark.asyncio
async def test_notes_tool_loop_rejects_missing_artifact_id(tmp_path, monkeypatch):
    (tmp_path / "daily.md").write_text("# Daily\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")

    result = await collect_notes_evidence_with_tools(
        "把 artifact 写入 daily.md",
        [],
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {"path": "daily.md", "artifact_id": "assistant:missing"},
                            "id": "1",
                        }
                    ]
                }
            ),
            "done",
        ),
    )

    assert (tmp_path / "daily.md").read_text(encoding="utf-8") == "# Daily\n"
    assert [ref["status"] for ref in result.references if ref["query"] == "append_note"] == ["artifact_not_found"]


@pytest.mark.asyncio
async def test_notes_tool_loop_includes_recent_artifact_catalog(tmp_path, monkeypatch):
    (tmp_path / "daily.md").write_text("# Daily\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        return ResponseWithThought(text=json.dumps({"calls": []}))

    await collect_notes_evidence_with_tools(
        "把这个写入 daily.md",
        [
            SimpleNamespace(
                by="khoj",
                message="plan",
                artifacts=[
                    {
                        "id": "assistant:plan",
                        "type": "assistant_response",
                        "content": "- [ ] 复习携程 AI 应用开发面经",
                        "source_refs": [{"file": "experiences/面经.md"}],
                    }
                ],
            )
        ],
        user=object(),
        agent=None,
        send_message=send_message,
        max_iterations=1,
    )

    assert "Recent conversation artifacts" in calls[0]["query"]
    assert "assistant:plan" in calls[0]["query"]
    assert '"artifact_id"' in calls[0]["query"]


@pytest.mark.asyncio
async def test_notes_tool_loop_allows_template_adapted_file_append(tmp_path, monkeypatch):
    (tmp_path / "experiences").mkdir()
    (tmp_path / "daily").mkdir()
    (tmp_path / "experiences" / "面经-携程-AI应用开发-截图.md").write_text(
        "- RAG 重点追问 chunking、检索、embedding 选型、召回排序和评估。\n"
        "- lost in middle 可以从上下文排序、摘要压缩、rerank 和长上下文评估角度准备。\n"
        "- Skills 要讲清 description、触发边界、失败处理和可观测性。\n",
        encoding="utf-8",
    )
    (tmp_path / "daily" / "2026-07-01.md").write_text("# 2026-07-01 每日计划\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")

    content = (
        "## 今日学习计划：携程 AI 应用开发面经复习\n\n"
        "### 今日目标\n\n"
        "今天围绕携程 AI 应用开发面经复习，把高频问题整理成面试时可以自然表达的工程化回答。\n\n"
        "### 今日重点\n\n"
        "- [ ] 复习 RAG chunking、检索、embedding 选型、召回排序和评估。\n"
        "- [ ] 整理 lost in middle 的上下文排序、摘要压缩、rerank 和长上下文评估。\n"
        "- [ ] 准备 Skills description、触发边界、失败处理和可观测性。\n\n"
        "### 今日产出\n\n"
        "- [ ] 输出一版 1 分钟口述答案。"
    )

    result = await collect_notes_evidence_with_tools(
        "根据 experiences/面经-携程-AI应用开发-截图.md 写入学习日记",
        [],
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "daily/2026-07-01.md",
                                "content": content,
                                "source_refs": [{"type": "file", "path": "experiences/面经-携程-AI应用开发-截图.md"}],
                                "write_intent": "adapt_to_template",
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            json.dumps({"grounded": True, "reason": "template adaptation from interview note"}),
            "done",
        ),
    )

    assert "今日学习计划：携程 AI 应用开发面经复习" in (tmp_path / "daily" / "2026-07-01.md").read_text(
        encoding="utf-8"
    )
    assert [ref["status"] for ref in result.references if ref["query"] == "append_note"] == ["written"]


@pytest.mark.asyncio
async def test_notes_tool_loop_blocks_qqbot_write(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")

    result = await collect_notes_evidence_with_tools(
        "写进 notes.md",
        [],
        user=object(),
        agent=None,
        client_app="qqbot",
        send_message=fake_model(
            json.dumps([{"name": "append_note", "args": {"path": "notes.md", "content": "new"}, "id": "1"}]),
            "done",
        ),
    )

    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "old\n"
    assert result.references[-1]["status"] == "blocked"
    assert result.references[-1]["changed"] is False
    assert "append_note" in result.searched[0]


@pytest.mark.asyncio
async def test_notes_tool_loop_can_call_openkb_tool(monkeypatch):
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

    monkeypatch.setattr("khoj.processor.conversation.notes_tool_loop.wiki_search_documents", fake_wiki_search_documents)

    result = await collect_notes_evidence_with_tools(
        "Redis",
        [],
        user=object(),
        agent=None,
        allow_openkb=True,
        send_message=fake_model(
            json.dumps([{"name": "wiki_search_documents", "args": {"query": "Redis", "n": 1}, "id": "1"}]),
            "done",
        ),
    )

    assert result.references[0]["uri"] == "openkb://local/wiki/index.md"


@pytest.mark.asyncio
async def test_notes_tool_loop_blocks_unexposed_tool(tmp_path, monkeypatch):
    (tmp_path / "secret.md").write_text("do not expose this", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    result = await collect_notes_evidence_with_tools(
        "Redis",
        [],
        user=object(),
        agent=None,
        allow_local_kb=False,
        allow_openkb=True,
        send_message=fake_model(
            json.dumps([{"name": "view_file", "args": {"path": "secret.md"}, "id": "1"}]),
            "done",
        ),
    )

    assert result.references == []
    assert "Notes tool is not available: view_file" in result.errors


@pytest.mark.asyncio
async def test_notes_tool_loop_hides_local_profile_and_skills_when_local_kb_disabled(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".codex" / "skills" / "obsidian-markdown"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: obsidian-markdown\ndescription: Use for Obsidian notes.\n---\nprivate skill instructions\n",
        encoding="utf-8",
    )
    (tmp_path / "agents.md").write_text("private vault profile", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return ResponseWithThought(text=json.dumps({"name": "read_skill", "args": {"name": "obsidian-markdown"}}))
        return ResponseWithThought(text="done")

    result = await collect_notes_evidence_with_tools(
        "Redis",
        [],
        user=object(),
        agent=None,
        allow_local_kb=False,
        allow_openkb=True,
        send_message=send_message,
    )

    assert "private vault profile" not in calls[0]["system_message"]
    assert "obsidian-markdown" not in calls[0]["system_message"]
    assert result.references == []
    assert "Notes tool is not available: read_skill" in result.errors


@pytest.mark.asyncio
async def test_notes_tool_loop_uses_text_tool_protocol(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Redis evidence", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return ResponseWithThought(text=json.dumps({"tool": "view_file", "arguments": {"path": "notes.md"}}))
        return ResponseWithThought(text="done")

    result = await collect_notes_evidence_with_tools(
        "Redis",
        [],
        user=object(),
        agent=None,
        send_message=send_message,
    )

    assert calls[0]["tools"] == []
    assert result.references[0]["uri"] == "local-kb://notes.md#L1-L1"


@pytest.mark.asyncio
async def test_notes_tool_loop_accepts_json_string_arguments(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Redis evidence", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    result = await collect_notes_evidence_with_tools(
        "Redis",
        [],
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps({"name": "view_file", "arguments": json.dumps({"path": "notes.md"})}),
            "done",
        ),
    )

    assert result.references[0]["uri"] == "local-kb://notes.md#L1-L1"


@pytest.mark.asyncio
async def test_notes_tool_loop_can_create_note_with_append_tool(tmp_path, monkeypatch):
    (tmp_path / "raw").mkdir()
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")

    result = await collect_notes_evidence_with_tools(
        "新建 raw/test.md，内容是 # Test",
        [],
        user=object(),
        agent=None,
        send_message=fake_model(
            json.dumps([{"name": "append_note", "args": {"path": "raw/test.md", "content": "# Test"}, "id": "1"}]),
            "done",
        ),
    )

    assert (tmp_path / "raw" / "test.md").read_text(encoding="utf-8") == "# Test\n"
    assert result.references[-1]["status"] == "written"


@pytest.mark.asyncio
async def test_notes_tool_loop_retries_when_first_plan_uses_no_tools(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Redis evidence", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return ResponseWithThought(text="I need more context.")
        if len(calls) == 2:
            assert "No tool call was returned" in kwargs["query"]
            return ResponseWithThought(text=json.dumps({"name": "view_file", "args": {"path": "notes.md"}}))
        return ResponseWithThought(text="done")

    result = await collect_notes_evidence_with_tools(
        "Redis",
        [],
        user=object(),
        agent=None,
        send_message=send_message,
    )

    assert len(calls) == 3
    assert result.references[0]["uri"] == "local-kb://notes.md#L1-L1"


@pytest.mark.asyncio
async def test_notes_tool_loop_requires_exact_view_after_discovery_tool(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("alpha\nRedis evidence\nomega\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return ResponseWithThought(
                text=json.dumps({"name": "regex_search_files", "args": {"regex_pattern": "Redis"}})
            )
        if len(calls) == 2:
            return ResponseWithThought(text="I found it.")
        if len(calls) == 3:
            assert "No exact Notes evidence has been collected yet" in kwargs["query"]
            return ResponseWithThought(text=json.dumps({"name": "view_file", "args": {"path": "notes.md"}}))
        return ResponseWithThought(text="done")

    result = await collect_notes_evidence_with_tools(
        "Redis",
        [],
        user=object(),
        agent=None,
        send_message=send_message,
    )

    assert len(calls) == 4
    assert result.references[0]["uri"] == "local-kb://notes.md#L1-L3"
    assert result.references[0]["kb_root"] == str(tmp_path)


@pytest.mark.asyncio
async def test_notes_tool_loop_retries_transient_planner_failure(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Redis evidence", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    calls = []
    statuses = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("incomplete chunked read")
        if len(calls) == 2:
            return ResponseWithThought(text=json.dumps({"name": "view_file", "args": {"path": "notes.md"}}))
        return ResponseWithThought(text="done")

    result = await collect_notes_evidence_with_tools(
        "Redis",
        [],
        user=object(),
        agent=None,
        send_message=send_message,
        send_status=statuses.append,
    )

    assert "Notes planner failed once; retrying" in statuses
    assert result.references[0]["uri"] == "local-kb://notes.md#L1-L1"


@pytest.mark.asyncio
async def test_notes_tool_loop_loads_vault_skills_and_profile(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".codex" / "skills" / "obsidian-markdown"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: obsidian-markdown\n"
        "description: Use for Obsidian notes, wikilinks, frontmatter, and daily notes.\n"
        "---\n"
        "Use Obsidian frontmatter and wikilinks.\n",
        encoding="utf-8",
    )
    (tmp_path / "agents.md").write_text("Daily notes live under daily/YYYY-MM-DD.md.", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            assert "obsidian-markdown" in kwargs["system_message"]
            assert "Daily notes live under daily/YYYY-MM-DD.md." in kwargs["system_message"]
            return ResponseWithThought(text=json.dumps({"name": "read_skill", "args": {"name": "obsidian-markdown"}}))
        assert "Use Obsidian frontmatter and wikilinks." in kwargs["query"]
        return ResponseWithThought(text="done")

    result = await collect_notes_evidence_with_tools(
        "给今天写日记",
        [],
        user=object(),
        agent=None,
        send_message=send_message,
    )

    assert result.references == []
    assert result.searched[0] == 'read_skill {"name": "obsidian-markdown"}'


@pytest.mark.asyncio
async def test_notes_tool_loop_loads_nested_official_obsidian_skill(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skills" / "note-taking" / "obsidian"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: obsidian\n"
        "description: Read, search, create, and edit notes in the Obsidian vault.\n"
        "---\n"
        "Use Obsidian wikilinks for related notes.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        return ResponseWithThought(text="done")

    await collect_notes_evidence_with_tools(
        "给今天写日记",
        [],
        user=object(),
        agent=None,
        send_message=send_message,
        max_iterations=1,
    )

    assert "obsidian" in calls[0]["system_message"]
    assert "Read, search, create, and edit notes in the Obsidian vault." in calls[0]["system_message"]
    assert calls[0]["response_type"] == "json_object"
    assert calls[0]["tools"] == []
    assert '"append_note"' in calls[0]["query"]
    assert '"source_refs"' in calls[0]["query"]
    assert '"write_intent"' in calls[0]["query"]
    assert '"read_skill"' in calls[0]["query"]
    assert '"view_file"' in calls[0]["query"]


@pytest.mark.asyncio
async def test_notes_tool_loop_reports_unreadable_skill_without_crashing(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".codex" / "skills" / "obsidian-markdown"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: obsidian-markdown\ndescription: Use for Obsidian notes.\n---\nUse Obsidian frontmatter.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    async def send_message(**kwargs):
        skill_file.unlink(missing_ok=True)
        return ResponseWithThought(text=json.dumps({"name": "read_skill", "args": {"name": "obsidian-markdown"}}))

    result = await collect_notes_evidence_with_tools(
        "给今天写日记",
        [],
        user=object(),
        agent=None,
        send_message=send_message,
        max_iterations=1,
    )

    assert result.references == []
    assert "Local skill is not readable: obsidian-markdown" in result.errors


@pytest.mark.asyncio
async def test_notes_tool_loop_revalidates_skill_path_before_read(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".codex" / "skills" / "obsidian-markdown"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: obsidian-markdown\ndescription: Use for Obsidian notes.\n---\nsafe skill instructions\n",
        encoding="utf-8",
    )
    outside = tmp_path.parent / "outside-skill.md"
    outside.write_text("outside secret instructions", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            skill_file.unlink()
            skill_file.symlink_to(outside)
            return ResponseWithThought(text=json.dumps({"name": "read_skill", "args": {"name": "obsidian-markdown"}}))
        return ResponseWithThought(text="done")

    result = await collect_notes_evidence_with_tools(
        "给今天写日记",
        [],
        user=object(),
        agent=None,
        send_message=send_message,
        max_iterations=2,
    )

    assert "outside secret instructions" not in calls[1]["query"]
    assert result.references == []
    assert "Local skill is not readable: obsidian-markdown" in result.errors


@pytest.mark.asyncio
async def test_notes_tool_loop_profile_skips_symlink_escape(tmp_path, monkeypatch):
    secret = tmp_path.parent / "outside-agents.md"
    secret.write_text("outside vault instructions", encoding="utf-8")
    (tmp_path / "agents.md").symlink_to(secret)
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        return ResponseWithThought(text="done")

    await collect_notes_evidence_with_tools(
        "读取规则",
        [],
        user=object(),
        agent=None,
        send_message=send_message,
    )

    assert "outside vault instructions" not in calls[0]["system_message"]
