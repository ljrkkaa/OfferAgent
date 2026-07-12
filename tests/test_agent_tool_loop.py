import asyncio
import json

import pytest

from khoj.processor.conversation.agent_tool_loop import (
    AgentToolLoopResult,
    _build_planner_query,
    _partition_tool_calls,
    _send_planner_message,
    add_write_reference_context,
    build_agent_tool_registry,
    collect_agent_context_and_actions,
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
    assert result.artifacts == {}


@pytest.mark.asyncio
async def test_planner_uses_json_object_without_strict_tool_plan_schema():
    captured = []

    async def send_message(**kwargs):
        captured.append(kwargs)
        return ResponseWithThought(text='{"calls":[]}')

    response = await _send_planner_message(send_message, "plan", [])

    assert response.text == '{"calls":[]}'
    assert captured[0]["response_type"] == "json_object"
    assert "response_schema" not in captured[0]


def test_planner_query_preserves_skill_latest_results_and_completion_guard():
    transcript = [
        *[
            {
                "tool": "read_skill",
                "args": {"name": f"skill-{index}"},
                "result": f"skill-{index}-start{'x' * 8000}skill-{index}-end",
            }
            for index in range(3)
        ],
        *[{"tool": "view_file", "args": {"path": f"old-{index}.md"}, "result": "old" * 3000} for index in range(20)],
        {"tool": "view_file", "args": {"path": "latest.md"}, "result": "latest-evidence"},
        {
            "tool": "append_note",
            "args": {"path": "daily.md", "content": "candidate" * 3000},
            "result": {
                "status": "source_mismatch",
                "message": "verifier-specific-reason-remove-unsupported-hooks",
            },
        },
        {
            "tool": "system",
            "args": {},
            "result": "The user explicitly requested a persistent file change.",
        },
    ]

    query = _build_planner_query(
        "write the daily",
        [],
        {},
        transcript,
        runtime_facts={"persistent_write_required": True, "successful_write_result_present": False},
    )

    assert "skill-2-start" in query and "skill-2-end" in query
    assert "latest-evidence" in query
    assert "verifier-specific-reason-remove-unsupported-hooks" in query
    assert "explicitly requested a persistent file change" in query
    assert "client review UI is the confirmation" in query


def test_agent_tool_registry_exposes_web_kb_and_write_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    tools = build_agent_tool_registry(
        allow_local_kb=True, allow_openkb=True, allow_web=True, write_mode="client_actions"
    )

    assert {"web_search", "read_webpage", "view_file", "regex_search_files", "append_note", "propose_edit"} <= set(
        tools
    )
    assert tools["web_search"].handler is run_web_search_tool
    assert tools["append_note"].handler is run_notes_tool_call


def test_partition_tool_calls_groups_consecutive_safe_tools_only():
    registry = build_agent_tool_registry(
        allow_local_kb=True, allow_openkb=False, allow_web=True, write_mode="client_actions"
    )
    batches = _partition_tool_calls(
        [
            ToolCall(name="web_search", args={"query": "a"}, id="1"),
            ToolCall(name="read_webpage", args={"query": "b"}, id="2"),
            ToolCall(name="append_note", args={"path": "notes.md", "content": "x"}, id="3"),
            ToolCall(name="web_search", args={"query": "c"}, id="4"),
        ],
        registry,
    )

    assert [batch.is_concurrency_safe for batch in batches] == [True, False, True]
    assert [[call.id for call in batch.calls] for batch in batches] == [["1", "2"], ["3"], ["4"]]


@pytest.mark.asyncio
async def test_run_web_search_tool_records_online_results(monkeypatch):
    async def fake_search_online(**kwargs):
        yield {"Agent eval": {"organic": [{"title": "WebArena", "link": "https://example.com"}]}}

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.search_online", fake_search_online)

    result = AgentToolLoopResult()
    await run_web_search_tool(
        ToolCall(name="web_search", args={"query": "Agent eval"}, id="1"),
        result=result,
        user=object(),
        conversation_history=[],
    )

    assert "Agent eval" in result.online_results
    assert result.searched == ["web_search: Agent eval"]
    assert result.tool_transcript[-1]["tool"] == "web_search"


@pytest.mark.asyncio
async def test_run_web_search_tool_stores_large_result_as_artifact(monkeypatch):
    async def fake_search_online(**kwargs):
        yield {"Agent eval": {"organic": [{"title": "Large", "snippet": "x" * 9000}]}}

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.search_online", fake_search_online)

    result = AgentToolLoopResult()
    await run_web_search_tool(
        ToolCall(name="web_search", args={"query": "Agent eval"}, id="1"),
        result=result,
        user=object(),
        conversation_history=[],
    )

    transcript_result = result.tool_transcript[-1]["result"]
    assert transcript_result["artifact_id"] == "tool-result:1"
    assert transcript_result["truncated"] is True
    assert result.artifacts["tool-result:1"]["tool"] == "web_search"
    assert "x" * 9000 in result.artifacts["tool-result:1"]["content"]


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
    assert result.used_workspace_tools is True


def test_write_reference_becomes_program_context():
    result = AgentToolLoopResult()
    add_write_reference_context(
        result,
        {
            "action": "append_note",
            "status": "written",
            "file": "agent.md",
            "changed": True,
            "compiled": "Appended 5 lines",
        },
    )

    assert "append_note" in result.program_context[0]
    assert "written" in result.program_context[0]


@pytest.mark.asyncio
async def test_default_agent_loop_can_search_web_then_write(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
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
                                "write_intent": "summarize",
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

    async def fake_web(call, *, result, **kwargs):
        args = call.args
        result.online_results["agent evaluation"] = {"organic": [{"title": "source"}]}
        result.searched.append("web_search: agent evaluation")
        result.tool_transcript.append({"tool": "web_search", "args": args, "result": "Agent eval"})

    async def fake_notes_call(call, *, result, **kwargs):
        result.references.append(
            {
                "action": "append_note",
                "query": "append_note",
                "status": "action_prepared",
                "file": "agent.md",
                "changed": False,
            }
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
        write_mode="client_actions",
    )

    assert "agent evaluation" in result.online_results
    assert result.references[-1]["status"] == "action_prepared"
    assert "waiting for the client" in result.program_context[-1]


@pytest.mark.asyncio
async def test_required_write_cannot_stop_before_preparing_an_action(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    responses = iter(
        [
            json.dumps({"calls": []}),
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "daily/2026-07-10.md",
                                "content": "# Daily",
                                "source_refs": [{"type": "current_user_request"}],
                                "write_intent": "preserve",
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            json.dumps({"calls": []}),
        ]
    )
    prompts = []

    async def fake_send_message(**kwargs):
        prompts.append(kwargs["query"])
        return ResponseWithThought(text=next(responses))

    async def fake_notes_call(call, *, result, **kwargs):
        result.references.append(
            {
                "action": "append_note",
                "status": "action_prepared",
                "file": call.args["path"],
                "changed": False,
            }
        )
        result.tool_transcript.append({"tool": call.name, "args": call.args, "result": "prepared"})

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.run_notes_tool_call", fake_notes_call)

    result = await collect_agent_context_and_actions(
        "帮我制定并写入 2026-07-10 的学习计划",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=False,
        write_mode="client_actions",
        require_write_action=True,
    )

    assert len(prompts) == 3
    assert "explicitly requested a persistent file change" in prompts[1]
    assert result.references[-1]["status"] == "action_prepared"


@pytest.mark.asyncio
async def test_required_write_reports_truthful_failure_when_planner_retry_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    calls = 0

    async def failing_send_message(**kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("planner offline")

    result = await collect_agent_context_and_actions(
        "write daily",
        [],
        user=object(),
        agent=None,
        send_message=failing_send_message,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=False,
        write_mode="client_actions",
        require_write_action=True,
    )

    assert calls == 2
    assert any("Planner unavailable after retry" in error for error in result.errors)
    assert "no file change is pending or applied" in result.program_context[-1].lower()


@pytest.mark.asyncio
async def test_required_write_reserves_final_iterations_for_write_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    responses = iter(
        [
            json.dumps({"calls": [{"name": "view_file", "args": {"path": "source.md"}, "id": "1"}]}),
            json.dumps({"calls": [{"name": "view_file", "args": {"path": "extra.md"}, "id": "2"}]}),
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "daily/2026-07-10.md",
                                "content": "# Daily",
                                "source_refs": [{"type": "current_user_request"}],
                                "write_intent": "preserve",
                            },
                            "id": "3",
                        }
                    ]
                }
            ),
        ]
    )
    prompts = []

    async def fake_send_message(**kwargs):
        prompts.append(kwargs["query"])
        return ResponseWithThought(text=next(responses))

    async def fake_notes_call(call, *, result, **kwargs):
        if call.name == "append_note":
            result.references.append(
                {
                    "action": "append_note",
                    "status": "action_prepared",
                    "file": call.args["path"],
                    "changed": False,
                }
            )
        result.tool_transcript.append({"tool": call.name, "args": call.args, "result": "ok"})

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.run_notes_tool_call", fake_notes_call)

    result = await collect_agent_context_and_actions(
        "write daily",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=False,
        write_mode="client_actions",
        require_write_action=True,
        max_iterations=3,
    )

    assert '"write_completion_phase": true' in prompts[1]
    assert "only write tools are available" in prompts[1]
    assert any("not available: view_file" in error for error in result.errors)
    assert result.references[-1]["status"] == "action_prepared"


@pytest.mark.asyncio
async def test_rejected_write_result_keeps_completion_phase_open_for_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    append_call = {
        "name": "append_note",
        "args": {
            "path": "daily/2026-07-10.md",
            "content": "# Daily",
            "source_refs": [{"type": "current_user_request"}],
            "write_intent": "preserve",
        },
    }
    responses = iter(
        [
            json.dumps({"calls": [{**append_call, "id": "1"}]}),
            json.dumps({"calls": [{**append_call, "id": "2"}]}),
            json.dumps({"calls": []}),
        ]
    )
    attempts = 0

    async def fake_send_message(**kwargs):
        return ResponseWithThought(text=next(responses))

    async def fake_notes_call(call, *, result, **kwargs):
        nonlocal attempts
        attempts += 1
        status = "source_mismatch" if attempts == 1 else "action_prepared"
        result.references.append(
            {
                "action": "append_note",
                "status": status,
                "file": call.args["path"],
                "changed": False,
            }
        )
        result.tool_transcript.append({"tool": call.name, "args": call.args, "result": {"status": status}})

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.run_notes_tool_call", fake_notes_call)

    result = await collect_agent_context_and_actions(
        "write daily",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=False,
        write_mode="client_actions",
        require_write_action=True,
        max_iterations=3,
    )

    assert attempts == 2
    assert [reference["status"] for reference in result.references] == ["source_mismatch", "action_prepared"]
    assert not any("exhausted its tool budget" in error for error in result.errors)


@pytest.mark.asyncio
async def test_disabled_write_mode_rejects_planner_write_call(tmp_path, monkeypatch):
    target = tmp_path / "notes.md"
    target.write_text("original\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    responses = iter(
        [
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "notes.md",
                                "content": "must not be written",
                                "source_refs": [{"type": "current_user_request"}],
                                "write_intent": "preserve",
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            json.dumps({"calls": []}),
        ]
    )

    async def fake_send_message(**kwargs):
        return ResponseWithThought(text=next(responses))

    result = await collect_agent_context_and_actions(
        "write this",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=False,
        write_mode="disabled",
    )

    assert target.read_text(encoding="utf-8") == "original\n"
    assert result.references == []
    assert result.errors == ["Agent runtime tool is not available: append_note"]


@pytest.mark.asyncio
async def test_default_agent_loop_injects_runtime_facts(monkeypatch):
    captured = {}

    async def fake_send_message(**kwargs):
        captured["query"] = kwargs["query"]
        return ResponseWithThought(text=json.dumps({"calls": []}))

    await collect_agent_context_and_actions(
        "hello",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        client_app="obsidian",
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=True,
        write_mode="client_actions",
    )

    assert '"client_app": "obsidian"' in captured["query"]
    assert '"local_kb_available": true' in captured["query"]
    assert '"openkb_available": false' in captured["query"]
    assert '"vault_actions_enabled": true' in captured["query"]
    assert "writes require an explicit client VaultAction review and apply" in captured["query"]


@pytest.mark.asyncio
async def test_default_agent_loop_retries_planner_once(monkeypatch):
    calls = 0

    async def fake_send_message(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary planner failure")
        return ResponseWithThought(text=json.dumps({"calls": []}))

    result = await collect_agent_context_and_actions(
        "hello",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=True,
    )

    assert calls == 2
    assert result.errors == []


@pytest.mark.asyncio
async def test_explicit_notes_uses_same_planner_and_requires_a_tool(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Redis evidence", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    responses = iter(
        [
            json.dumps({"calls": []}),
            json.dumps({"calls": [{"name": "view_file", "args": {"path": "notes.md"}, "id": "1"}]}),
            json.dumps({"calls": []}),
        ]
    )
    prompts = []

    async def fake_send_message(**kwargs):
        prompts.append(kwargs["query"])
        return ResponseWithThought(text=next(responses))

    result = await collect_agent_context_and_actions(
        "Redis",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=False,
        require_notes_evidence=True,
    )

    assert len(prompts) == 3
    assert "explicit Notes request" in prompts[1]
    assert result.references[0]["uri"] == "local-kb://notes.md#L1-L1"


@pytest.mark.asyncio
async def test_explicit_notes_requires_exact_read_after_discovery(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("alpha\nRedis evidence\nomega\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    responses = iter(
        [
            json.dumps({"calls": [{"name": "regex_search_files", "args": {"regex_pattern": "Redis"}, "id": "1"}]}),
            json.dumps({"calls": []}),
            json.dumps({"calls": [{"name": "view_file", "args": {"path": "notes.md"}, "id": "2"}]}),
            json.dumps({"calls": []}),
        ]
    )
    prompts = []

    async def fake_send_message(**kwargs):
        prompts.append(kwargs["query"])
        return ResponseWithThought(text=next(responses))

    result = await collect_agent_context_and_actions(
        "Redis",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=False,
        require_notes_evidence=True,
    )

    assert "No exact Notes evidence" in prompts[2]
    assert result.references[0]["uri"] == "local-kb://notes.md#L1-L3"


@pytest.mark.asyncio
async def test_tool_arguments_are_validated_before_permission_and_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    responses = iter(
        [
            json.dumps({"calls": [{"name": "view_file", "args": {}, "id": "1"}]}),
            json.dumps({"calls": []}),
        ]
    )
    permission_checks = []

    async def fake_send_message(**kwargs):
        return ResponseWithThought(text=next(responses))

    async def before_tool_call(command):
        permission_checks.append(command)

    result = await collect_agent_context_and_actions(
        "read",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        before_tool_call=before_tool_call,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=False,
    )

    assert permission_checks == []
    assert result.errors == ["required argument missing for view_file: path"]


@pytest.mark.asyncio
async def test_default_agent_loop_runs_consecutive_read_tools_concurrently(monkeypatch):
    responses = iter(
        [
            json.dumps(
                {
                    "calls": [
                        {"name": "web_search", "args": {"query": "first"}, "id": "1"},
                        {"name": "web_search", "args": {"query": "second"}, "id": "2"},
                    ]
                }
            ),
            json.dumps({"calls": []}),
        ]
    )
    active = 0
    max_active = 0

    async def fake_send_message(**kwargs):
        return ResponseWithThought(text=next(responses))

    async def fake_web(call, *, result, **kwargs):
        args = call.args
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        result.searched.append(f"web_search: {args['query']}")
        result.tool_transcript.append({"tool": "web_search", "args": args, "result": args["query"]})

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.run_web_search_tool", fake_web)

    result = await collect_agent_context_and_actions(
        "search both",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=False,
        allow_openkb=False,
        allow_web=True,
    )

    assert max_active == 2
    assert [item["args"]["query"] for item in result.tool_transcript] == ["first", "second"]


@pytest.mark.asyncio
async def test_default_agent_loop_does_not_move_read_tool_before_write(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    responses = iter(
        [
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "notes.md",
                                "content": "x",
                                "source_refs": [{"type": "current_user_request"}],
                                "write_intent": "preserve",
                            },
                            "id": "1",
                        },
                        {"name": "web_search", "args": {"query": "after"}, "id": "2"},
                    ]
                }
            ),
            json.dumps({"calls": []}),
        ]
    )
    events = []

    async def fake_send_message(**kwargs):
        return ResponseWithThought(text=next(responses))

    async def fake_notes_call(call, *, result, **kwargs):
        events.append("append:start")
        await asyncio.sleep(0.02)
        events.append("append:end")
        result.references.append(
            {
                "action": "append_note",
                "query": "append_note",
                "status": "action_prepared",
                "file": "notes.md",
                "changed": False,
            }
        )
        result.tool_transcript.append({"tool": call.name, "args": call.args, "result": "written"})

    async def fake_web(call, *, result, **kwargs):
        args = call.args
        events.append("web")
        result.tool_transcript.append({"tool": "web_search", "args": args, "result": "after"})

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.run_notes_tool_call", fake_notes_call)
    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.run_web_search_tool", fake_web)

    await collect_agent_context_and_actions(
        "write then search",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=True,
        write_mode="client_actions",
    )

    assert events == ["append:start", "append:end", "web"]


@pytest.mark.asyncio
async def test_default_agent_loop_keeps_successful_sibling_when_concurrent_tool_fails(monkeypatch):
    responses = iter(
        [
            json.dumps(
                {
                    "calls": [
                        {"name": "web_search", "args": {"query": "bad"}, "id": "1"},
                        {"name": "web_search", "args": {"query": "good"}, "id": "2"},
                    ]
                }
            ),
            json.dumps({"calls": []}),
        ]
    )

    async def fake_send_message(**kwargs):
        return ResponseWithThought(text=next(responses))

    async def fake_web(call, *, result, **kwargs):
        args = call.args
        if args["query"] == "bad":
            raise RuntimeError("boom")
        result.searched.append("web_search: good")
        result.tool_transcript.append({"tool": "web_search", "args": args, "result": "good"})

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.run_web_search_tool", fake_web)

    result = await collect_agent_context_and_actions(
        "search both",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=False,
        allow_openkb=False,
        allow_web=True,
    )

    assert result.errors == ["boom"]
    assert [item.get("error") or item.get("result") for item in result.tool_transcript] == ["boom", "good"]


@pytest.mark.asyncio
async def test_default_agent_loop_remaps_concurrent_artifact_ids(monkeypatch):
    responses = iter(
        [
            json.dumps(
                {
                    "calls": [
                        {"name": "web_search", "args": {"query": "first"}, "id": "1"},
                        {"name": "web_search", "args": {"query": "second"}, "id": "2"},
                    ]
                }
            ),
            json.dumps({"calls": []}),
        ]
    )

    async def fake_send_message(**kwargs):
        return ResponseWithThought(text=next(responses))

    async def fake_web(call, *, result, **kwargs):
        args = call.args
        result.artifacts["tool-result:1"] = {
            "id": "tool-result:1",
            "tool": "web_search",
            "content": args["query"] * 5000,
        }
        result.tool_transcript.append(
            {
                "tool": "web_search",
                "args": args,
                "result": {"artifact_id": "tool-result:1", "truncated": True},
            }
        )

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.run_web_search_tool", fake_web)

    result = await collect_agent_context_and_actions(
        "search both",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=False,
        allow_openkb=False,
        allow_web=True,
    )

    assert list(result.artifacts) == ["tool-result:1", "tool-result:2"]
    assert [item["result"]["artifact_id"] for item in result.tool_transcript] == ["tool-result:1", "tool-result:2"]
    assert result.artifacts["tool-result:1"]["content"].startswith("first")
    assert result.artifacts["tool-result:2"]["content"].startswith("second")


@pytest.mark.asyncio
async def test_default_agent_loop_writes_content_grounded_in_web_tool_result(tmp_path, monkeypatch):
    target = tmp_path / "agent-eval.md"
    target.write_text("# Agent Eval\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

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
        write_mode="client_actions",
    )

    text = target.read_text(encoding="utf-8")
    assert "WebArena" not in text
    assert result.references[-1]["status"] == "action_prepared"
    assert "waiting for the client" in result.program_context[-1]
