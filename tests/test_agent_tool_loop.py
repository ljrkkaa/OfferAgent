import json

import pytest

from khoj.processor.conversation.agent_tool_loop import (
    AgentToolLoopResult,
    add_write_reference_context,
    build_agent_tool_registry,
    collect_agent_context_and_actions,
    parse_agent_tool_calls,
    run_notes_tool_call,
    run_web_search_tool,
)
from khoj.processor.conversation.utils import ResponseWithThought, ToolCall


def test_agent_tool_loop_result_defaults_are_empty():
    result = AgentToolLoopResult()

    assert result.references == []
    assert result.inferred_queries == []
    assert result.online_results == {}
    assert result.program_context == []
    assert result.searched == []
    assert result.errors == []
    assert result.tool_transcript == []


def test_parse_agent_tool_calls_accepts_json_object():
    calls = parse_agent_tool_calls(
        '{"calls":[{"name":"web_search","args":{"query":"Agent evaluation benchmarks"},"id":"1"}]}'
    )

    assert calls[0].name == "web_search"
    assert calls[0].args == {"query": "Agent evaluation benchmarks"}
    assert calls[0].id == "1"


def test_parse_agent_tool_calls_accepts_json_string_arguments():
    calls = parse_agent_tool_calls(
        '{"calls":[{"name":"append_note","arguments":"{\\"path\\":\\"agent.md\\",\\"content\\":\\"hi\\"}"}]}'
    )

    assert calls[0].name == "append_note"
    assert calls[0].args == {"path": "agent.md", "content": "hi"}


def test_parse_agent_tool_calls_rejects_plain_text():
    assert parse_agent_tool_calls("I can answer directly.") == []


def test_agent_tool_registry_exposes_web_kb_and_write_tools():
    tools = build_agent_tool_registry(allow_local_kb=True, allow_openkb=True, allow_web=True)

    assert {"web_search", "read_webpage", "view_file", "regex_search_files", "append_note", "propose_edit"} <= set(
        tools
    )


@pytest.mark.asyncio
async def test_run_web_search_tool_records_online_results(monkeypatch):
    async def fake_search_online(**kwargs):
        yield {"Agent eval": {"organic": [{"title": "WebArena", "link": "https://example.com"}]}}

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.search_online", fake_search_online)

    result = AgentToolLoopResult()
    await run_web_search_tool({"query": "Agent eval"}, result=result, user=object(), conversation_history=[])

    assert "Agent eval" in result.online_results
    assert result.searched == ["web_search: Agent eval"]
    assert result.tool_transcript[-1]["tool"] == "web_search"


@pytest.mark.asyncio
async def test_run_notes_tool_call_delegates_to_notes_runtime(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("alpha\nAgent evidence\nomega\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    async def fake_send_message(**kwargs):
        return ResponseWithThought(text=json.dumps({"grounded": True}))

    result = AgentToolLoopResult()
    await run_notes_tool_call(
        ToolCall(name="view_file", args={"path": "notes.md"}, id="1"),
        result=result,
        query="read notes",
        chat_history=[],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=True,
        allow_openkb=False,
    )

    assert result.references[0]["uri"] == "local-kb://notes.md#L1-L3"
    assert result.tool_transcript[-1]["tool"] == "view_file"


def test_write_reference_becomes_program_context():
    result = AgentToolLoopResult()
    add_write_reference_context(
        result,
        {"action": "append_note", "status": "written", "file": "agent.md", "changed": True, "compiled": "Appended 5 lines"},
    )

    assert "append_note" in result.program_context[0]
    assert "written" in result.program_context[0]


@pytest.mark.asyncio
async def test_default_agent_loop_can_search_web_then_write(monkeypatch):
    responses = iter(
        [
            json.dumps({"calls": [{"name": "web_search", "args": {"query": "agent evaluation"}, "id": "1"}]}),
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "agent.md",
                                "content": "Agent eval",
                                "source_refs": [{"type": "tool_result", "tool": "web_search"}],
                            },
                            "id": "2",
                        }
                    ]
                }
            ),
            json.dumps({"calls": []}),
        ]
    )

    async def fake_send_message(**kwargs):
        return ResponseWithThought(text=next(responses))

    async def fake_web(args, *, result, **kwargs):
        result.online_results["agent evaluation"] = {"organic": [{"title": "source"}]}
        result.searched.append("web_search: agent evaluation")
        result.tool_transcript.append({"tool": "web_search", "args": args, "result": "Agent eval"})

    async def fake_notes_call(call, *, result, **kwargs):
        result.references.append(
            {"action": "append_note", "query": "append_note", "status": "written", "file": "agent.md", "changed": True}
        )
        result.tool_transcript.append({"tool": call.name, "args": call.args, "result": "written"})

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.run_web_search_tool", fake_web)
    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.run_notes_tool_call", fake_notes_call)

    result = await collect_agent_context_and_actions(
        "根据网络资料补充 agent 评估八股并写入项目",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=True,
    )

    assert "agent evaluation" in result.online_results
    assert result.references[-1]["status"] == "written"
    assert "written" in result.program_context[-1]


@pytest.mark.asyncio
async def test_default_agent_loop_writes_content_grounded_in_web_tool_result(tmp_path, monkeypatch):
    target = tmp_path / "agent-eval.md"
    target.write_text("# Agent Eval\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")

    async def fake_search_online(**kwargs):
        yield {
            "agent evaluation": {
                "organic": [
                    {
                        "title": "WebArena",
                        "link": "https://example.com/webarena",
                        "snippet": "WebArena evaluates autonomous agents on realistic web tasks.",
                    }
                ]
            }
        }

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.search_online", fake_search_online)

    responses = iter(
        [
            json.dumps({"calls": [{"name": "web_search", "args": {"query": "agent evaluation"}, "id": "1"}]}),
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "agent-eval.md",
                                "content": "- WebArena：评估 autonomous agents 在真实网页任务上的表现。",
                                "source_refs": [{"type": "tool_result", "tool": "web_search"}],
                                "write_intent": "summarize",
                            },
                            "id": "2",
                        }
                    ]
                }
            ),
            json.dumps({"grounded": True, "reason": "grounded in web search result"}),
            json.dumps({"calls": []}),
        ]
    )

    async def fake_send_message(**kwargs):
        return ResponseWithThought(text=next(responses))

    result = await collect_agent_context_and_actions(
        "根据网络资料补充 agent 评估八股并写入项目",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=True,
    )

    text = target.read_text(encoding="utf-8")
    assert "WebArena" in text
    assert result.references[-1]["status"] == "written"
    assert "Do not say writing is unavailable" in result.program_context[-1]
