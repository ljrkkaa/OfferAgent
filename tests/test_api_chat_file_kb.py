import json
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from khoj.processor.conversation.agent_tool_loop import AgentToolLoopResult
from khoj.processor.conversation.offeragent_intent_router import RouteDecision
from khoj.processor.conversation.utils import ResponseWithThought
from khoj.routers import api_chat
from khoj.routers.helpers import CommonQueryParamsClass
from khoj.utils.rawconfig import ChatRequestBody


class FakeConversation:
    def __init__(self):
        self.id = "conversation-id"
        self.agent = None
        self.messages = []
        self.file_filters = []

    async def asave(self):
        return None

    async def pop_message(self, interrupted=False):
        return None


async def noop_async(*args, **kwargs):
    return None


async def ok_response(*args, **kwargs):
    async def stream():
        yield ResponseWithThought(text="ok")

    return stream(), {}


async def run_chat(
    monkeypatch,
    q,
    *,
    agent_slug="default",
    client_app="web",
    client_capabilities=None,
    generate_response=ok_response,
    **patched,
):
    user = SimpleNamespace(id=1)
    user_scope = SimpleNamespace(object=user, client_app=client_app)
    agent = SimpleNamespace(slug=agent_slug)

    async def fake_conversation(*args, **kwargs):
        return FakeConversation()

    async def fake_default_agent():
        return agent

    async def fake_user_name(user):
        return "Test User"

    async def fake_memory_disabled(user):
        return False

    monkeypatch.setattr(api_chat.ConversationAdapters, "aget_conversation_by_user", fake_conversation)
    monkeypatch.setattr(api_chat.AgentAdapters, "aget_default_agent", fake_default_agent)
    monkeypatch.setattr(api_chat, "is_ready_to_chat", noop_async)
    monkeypatch.setattr(api_chat, "aget_user_name", fake_user_name)
    monkeypatch.setattr(api_chat.ConversationAdapters, "ais_memory_enabled", fake_memory_disabled)
    monkeypatch.setattr(api_chat.conversation_command_rate_limiter, "update_and_check_if_valid", noop_async)
    monkeypatch.setattr(api_chat, "agenerate_chat_response", generate_response)
    monkeypatch.setattr(api_chat, "save_to_conversation_log", noop_async)
    if "route_offeragent_intent" not in patched:
        async def fake_default_router(*args, **kwargs):
            return RouteDecision(route="default", intent="conversation", confidence=1.0)

        monkeypatch.setattr(api_chat, "route_offeragent_intent", fake_default_router)
    for name, value in patched.items():
        monkeypatch.setattr(api_chat, name, value)

    body = ChatRequestBody(q=q, stream=True, client_capabilities=client_capabilities)
    return [
        event
        async for event in api_chat.event_generator(
            body,
            user_scope,
            CommonQueryParamsClass(client=client_app),
            Headers({}),
            SimpleNamespace(),
        )
    ]


def events_of_type(events, event_type):
    return [
        json.loads(event) for event in events if event.startswith("{") and json.loads(event).get("type") == event_type
    ]


def fake_notes_model(*responses):
    calls = list(responses)

    async def send_message_to_model_wrapper(**kwargs):
        if calls:
            return ResponseWithThought(text=calls.pop(0))
        return ResponseWithThought(text="done")

    return send_message_to_model_wrapper


@pytest.mark.asyncio
async def test_search_indexed_notes_dedupes_files_and_prefers_raw_text(monkeypatch):
    user = SimpleNamespace(uuid="user-uuid")

    class Result:
        def __init__(self, file, entry):
            self.additional = {"file": file, "uri": f"file://{file}", "query": "daily"}
            self.corpus_id = file
            self.entry = entry
            self.score = 0.1

    async def fake_execute_search(*args, **kwargs):
        return [
            Result("daily/2026-07-06.md", "chunk one"),
            Result("daily/2026-07-06.md", "chunk two"),
            Result("daily/2026-07-05.md", "chunk three"),
        ]

    async def fake_file_objects(*args, **kwargs):
        return [SimpleNamespace(file_name="daily/2026-07-06.md", raw_text="full daily text")]

    monkeypatch.setattr(api_chat, "execute_search", fake_execute_search)
    monkeypatch.setattr(api_chat.FileObjectAdapters, "aget_file_objects_by_names", fake_file_objects)

    refs = await api_chat.search_indexed_notes(user, "daily", None, limit=2)

    assert [ref["file"] for ref in refs] == ["daily/2026-07-06.md", "daily/2026-07-05.md"]
    assert refs[0]["compiled"] == "# daily/2026-07-06.md\nfull daily text"
    assert refs[1]["compiled"] == "chunk three"


@pytest.mark.asyncio
async def test_default_chat_uses_unified_agent_runtime(monkeypatch):
    captured = {}

    async def fake_agent_runtime(*args, **kwargs):
        captured["runtime_kwargs"] = kwargs
        return AgentToolLoopResult(
            references=[{"query": "append_note", "status": "written", "file": "agent.md", "action": "append_note"}],
            inferred_queries=["agent evaluation"],
            online_results={"agent evaluation": {"organic": [{"title": "source"}]}},
            program_context=["Notes write tool result: written"],
        )

    async def fake_generate_response(
        q,
        chat_history,
        conversation,
        compiled_references,
        online_results,
        code_results,
        research_results,
        user,
        location,
        user_name,
        uploaded_images,
        attached_file_context,
        relevant_memories,
        program_execution_context,
        *args,
        **kwargs,
    ):
        captured["compiled_references"] = compiled_references
        captured["online_results"] = online_results
        captured["program_execution_context"] = program_execution_context

        async def stream():
            yield ResponseWithThought(text="ok")

        return stream(), {}

    events = await run_chat(
        monkeypatch,
        "根据网络资料补充 agent 评估八股并补充到项目里",
        collect_agent_context_and_actions=fake_agent_runtime,
        is_web_search_enabled=lambda: True,
        generate_response=fake_generate_response,
    )

    assert captured["runtime_kwargs"]["allow_web"] is True
    assert captured["compiled_references"][-1]["query"] == "append_note"
    assert "agent evaluation" in captured["online_results"]
    assert "Notes write tool result" in "\n".join(captured["program_execution_context"])
    assert events_of_type(events, "references")[0]["data"]["inferredQueries"] == ["agent evaluation"]


@pytest.mark.asyncio
async def test_obsidian_router_notes_write_returns_vault_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")

    async def fake_router(*args, **kwargs):
        return RouteDecision(route="notes", intent="create_daily_plan", confidence=0.95)

    events = await run_chat(
        monkeypatch,
        "帮我创建 daily/2026-07-09.md，内容是：# 2026-07-09 每日计划",
        client_app="obsidian",
        client_capabilities={"vaultActions": True},
        route_offeragent_intent=fake_router,
        send_message_to_model_wrapper=fake_notes_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "daily/2026-07-09.md",
                                "content": "# 2026-07-09 每日计划\n",
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            "done",
        ),
    )

    assert not (tmp_path / "daily" / "2026-07-09.md").exists()
    vault_actions = events_of_type(events, "vault_actions")
    assert vault_actions[0]["data"]["actions"] == [
        {
            "op": "create_file",
            "path": "daily/2026-07-09.md",
            "content": "# 2026-07-09 每日计划\n",
            "heading": None,
            "mode": "create_only",
        }
    ]


@pytest.mark.asyncio
async def test_default_chat_can_search_web_then_write_to_local_kb(tmp_path, monkeypatch):
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
    captured = {}

    async def fake_generate_response(
        q,
        chat_history,
        conversation,
        compiled_references,
        online_results,
        code_results,
        research_results,
        user,
        location,
        user_name,
        uploaded_images,
        attached_file_context,
        relevant_memories,
        program_execution_context,
        *args,
        **kwargs,
    ):
        captured["compiled_references"] = compiled_references
        captured["online_results"] = online_results
        captured["program_execution_context"] = program_execution_context

        async def stream():
            yield ResponseWithThought(text="ok")

        return stream(), {}

    await run_chat(
        monkeypatch,
        "根据网络资料补充 agent 评估八股并补充到项目里",
        is_web_search_enabled=lambda: True,
        send_message_to_model_wrapper=fake_notes_model(
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
            json.dumps({"grounded": True, "reason": "grounded in web result"}),
            json.dumps({"calls": []}),
        ),
        generate_response=fake_generate_response,
    )

    assert "WebArena" in target.read_text(encoding="utf-8")
    assert captured["compiled_references"][-1]["status"] == "written"
    assert "agent evaluation" in captured["online_results"]
    assert "Do not say writing is unavailable" in "\n".join(captured["program_execution_context"])


@pytest.mark.asyncio
async def test_chat_notes_local_kb_uses_main_tool_loop(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Redis evidence", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    events = await run_chat(
        monkeypatch,
        "/notes Redis",
        send_message_to_model_wrapper=fake_notes_model(
            json.dumps([{"name": "view_file", "args": {"path": "notes.md"}, "id": "1"}]),
            "done",
        ),
    )
    reference_events = events_of_type(events, "references")

    assert reference_events
    assert reference_events[0]["data"]["context"][0]["uri"] == "local-kb://notes.md#L1-L1"


@pytest.mark.asyncio
async def test_default_chat_uses_synced_index_when_no_local_kb(monkeypatch):
    monkeypatch.delenv("KHOJ_LOCAL_KB_PATH", raising=False)
    captured = {}

    async def fake_agent_runtime(*args, **kwargs):
        return AgentToolLoopResult()

    async def fake_indexed_notes(*args, **kwargs):
        return [
            {
                "query": "最近五天 daily",
                "file": "daily/2026-07-06.md",
                "uri": "daily/2026-07-06.md",
                "compiled": "2026-07-06 daily evidence",
                "source": "indexed",
            }
        ]

    async def fake_generate_response(
        q,
        chat_history,
        conversation,
        compiled_references,
        *args,
        **kwargs,
    ):
        captured["compiled_references"] = compiled_references

        async def stream():
            yield ResponseWithThought(text="ok")

        return stream(), {}

    events = await run_chat(
        monkeypatch,
        "帮我评价一下我最近五天的daily任务完成的如何",
        collect_agent_context_and_actions=fake_agent_runtime,
        search_indexed_notes=fake_indexed_notes,
        generate_response=fake_generate_response,
    )

    assert captured["compiled_references"][0]["file"] == "daily/2026-07-06.md"
    assert events_of_type(events, "references")[0]["data"]["context"][0]["source"] == "indexed"


@pytest.mark.asyncio
async def test_notes_chat_uses_synced_index_when_no_local_kb(monkeypatch):
    monkeypatch.delenv("KHOJ_LOCAL_KB_PATH", raising=False)
    captured = {}

    async def fake_indexed_notes(*args, **kwargs):
        return [
            {
                "query": "daily",
                "file": "daily/2026-07-06.md",
                "uri": "daily/2026-07-06.md",
                "compiled": "2026-07-06 daily evidence",
                "source": "indexed",
            }
        ]

    async def fake_generate_response(
        q,
        chat_history,
        conversation,
        compiled_references,
        *args,
        **kwargs,
    ):
        captured["compiled_references"] = compiled_references

        async def stream():
            yield ResponseWithThought(text="ok")

        return stream(), {}

    events = await run_chat(
        monkeypatch,
        "/notes daily",
        search_indexed_notes=fake_indexed_notes,
        generate_response=fake_generate_response,
    )

    assert captured["compiled_references"][0]["file"] == "daily/2026-07-06.md"
    assert events_of_type(events, "references")[0]["data"]["context"][0]["source"] == "indexed"


@pytest.mark.asyncio
async def test_chat_notes_local_kb_miss_returns_insufficient_evidence(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Redis evidence", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    async def fail_generate_response(*args, **kwargs):
        raise AssertionError("final generator should not run without local evidence")

    events = await run_chat(
        monkeypatch,
        "/notes no such thing",
        send_message_to_model_wrapper=fake_notes_model("done"),
        generate_response=fail_generate_response,
    )

    assert "couldn't find enough local knowledge base evidence" in "".join(events)


@pytest.mark.asyncio
async def test_chat_notes_openkb_not_ready_with_local_root_does_not_fall_back_to_general(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Redis evidence", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_KB_ENGINE", "openkb")
    monkeypatch.setenv("KHOJ_ENABLE_OPENKB", "true")
    monkeypatch.setenv("KHOJ_OPENKB_ROOT", str(tmp_path / "missing-openkb"))

    async def fail_generate_response(*args, **kwargs):
        raise AssertionError("notes-only chat should not fall back to a general response without evidence")

    events = await run_chat(
        monkeypatch,
        "/notes Redis",
        generate_response=fail_generate_response,
    )

    assert "haven't synced any notes yet" in "".join(events)


@pytest.mark.asyncio
async def test_chat_notes_openkb_engine_uses_wiki_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_KB_ENGINE", "openkb")
    monkeypatch.setenv("KHOJ_ENABLE_OPENKB", "true")
    monkeypatch.setenv("KHOJ_OPENKB_ROOT", str(tmp_path))
    monkeypatch.delenv("KHOJ_LOCAL_KB_PATH", raising=False)
    monkeypatch.delenv("KHOJ_OBSIDIAN_VAULT_PATH", raising=False)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "index.md").write_text("# Index", encoding="utf-8")

    async def fake_wiki_search_documents(*args, **kwargs):
        return (
            [
                {
                    "query": "openkb:Redis",
                    "file": "wiki/concepts/redis.md",
                    "uri": "openkb://local/wiki/concepts/redis.md",
                    "compiled": "# wiki/concepts/redis.md\nRedis evidence",
                    "wiki_path": "concepts/redis.md",
                    "evidence_type": "concept",
                    "source_pages": None,
                }
            ],
            ["openkb:Redis"],
            args[0],
        )

    monkeypatch.setattr("khoj.processor.conversation.notes_tool_loop.wiki_search_documents", fake_wiki_search_documents)
    events = await run_chat(
        monkeypatch,
        "/notes Redis",
        send_message_to_model_wrapper=fake_notes_model(
            json.dumps([{"name": "wiki_search_documents", "args": {"query": "Redis", "n": 1}, "id": "1"}]),
            "done",
        ),
    )
    reference_events = events_of_type(events, "references")

    assert reference_events[0]["data"]["context"][0]["uri"] == "openkb://local/wiki/concepts/redis.md"


@pytest.mark.asyncio
async def test_chat_notes_local_kb_write_request_appends_and_reports_tool_result(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("# 复盘\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")

    captured = {}

    async def fake_generate_response(
        q,
        chat_history,
        conversation,
        compiled_references,
        online_results,
        code_results,
        research_results,
        user,
        location,
        user_name,
        uploaded_images,
        attached_file_context,
        relevant_memories,
        program_execution_context,
        *args,
        **kwargs,
    ):
        captured["compiled_references"] = compiled_references
        captured["program_execution_context"] = program_execution_context

        async def stream():
            yield ResponseWithThought(text="ok")

        return stream(), {}

    events = await run_chat(
        monkeypatch,
        "/notes 写进 `notes.md`：HashMap 扩容要讲清楚",
        agent_slug="interview",
        send_message_to_model_wrapper=fake_notes_model(
            json.dumps(
                [
                    {
                        "name": "append_note",
                        "args": {"path": "notes.md", "content": "HashMap 扩容要讲清楚", "heading": "复盘"},
                        "id": "1",
                    }
                ]
            ),
            "done",
        ),
        generate_response=fake_generate_response,
    )
    reference_events = events_of_type(events, "references")

    assert "HashMap 扩容要讲清楚" in (tmp_path / "notes.md").read_text(encoding="utf-8")
    assert captured["compiled_references"][-1]["query"] == "append_note"
    context = "\n".join(captured["program_execution_context"])
    assert '"action": "append_note"' in context
    assert '"status": "written"' in context
    assert '"file": "notes.md"' in context
    assert reference_events[0]["data"]["context"][-1]["status"] == "written"


@pytest.mark.asyncio
async def test_chat_notes_tool_failure_stops_before_final_response(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("# 复盘\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")

    async def fail_notes_model(**kwargs):
        raise ValueError("Empty response returned by Codex backend")

    async def fail_generate_response(*args, **kwargs):
        raise AssertionError("final generator should not run when Notes tools fail")

    events = await run_chat(
        monkeypatch,
        "/notes 写进 `notes.md`：HashMap 扩容要讲清楚",
        send_message_to_model_wrapper=fail_notes_model,
        generate_response=fail_generate_response,
    )

    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "# 复盘\n"
    assert "did not read or modify the local knowledge base" in "".join(events)


@pytest.mark.asyncio
async def test_chat_notes_qqbot_write_reports_blocked_tool_result(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("# 复盘\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")

    captured = {}

    async def fake_generate_response(
        q,
        chat_history,
        conversation,
        compiled_references,
        online_results,
        code_results,
        research_results,
        user,
        location,
        user_name,
        uploaded_images,
        attached_file_context,
        relevant_memories,
        program_execution_context,
        *args,
        **kwargs,
    ):
        captured["compiled_references"] = compiled_references
        captured["program_execution_context"] = program_execution_context

        async def stream():
            yield ResponseWithThought(text="ok")

        return stream(), {}

    events = await run_chat(
        monkeypatch,
        "/notes 写进 notes.md：HashMap 扩容要讲清楚",
        client_app="qqbot",
        send_message_to_model_wrapper=fake_notes_model(
            json.dumps([{"name": "append_note", "args": {"path": "notes.md", "content": "blocked"}, "id": "1"}]),
            "done",
        ),
        generate_response=fake_generate_response,
    )

    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "# 复盘\n"
    assert captured["compiled_references"][-1]["status"] == "blocked"
    context = "\n".join(captured["program_execution_context"])
    assert '"status": "blocked"' in context
    assert "Do not say writing is unavailable" not in context
    assert events_of_type(events, "references")[0]["data"]["context"][-1]["status"] == "blocked"


@pytest.mark.asyncio
async def test_chat_explicit_save_writes_openkb_exploration(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (tmp_path / "manifest.json").write_text(json.dumps({"status": "ready"}), encoding="utf-8")
    (wiki / "index.md").write_text("# Index", encoding="utf-8")
    monkeypatch.setenv("KHOJ_KB_ENGINE", "openkb")
    monkeypatch.setenv("KHOJ_ENABLE_OPENKB", "true")
    monkeypatch.setenv("KHOJ_OPENKB_ROOT", str(tmp_path))

    async def fake_wiki_search_documents(*args, **kwargs):
        return (
            [
                {
                    "query": "openkb:Redis",
                    "file": "wiki/index.md",
                    "uri": "openkb://local/wiki/index.md",
                    "compiled": "# Index\nRedis evidence",
                }
            ],
            ["openkb:Redis"],
            args[0],
        )

    async def fake_generate_response(*args, **kwargs):
        async def stream():
            yield ResponseWithThought(text="Redis answer with [[ghost-link]]")

        return stream(), {}

    monkeypatch.setattr("khoj.processor.conversation.notes_tool_loop.wiki_search_documents", fake_wiki_search_documents)
    events = await run_chat(
        monkeypatch,
        "/notes 保存这次 Redis 回答",
        send_message_to_model_wrapper=fake_notes_model(
            json.dumps([{"name": "wiki_search_documents", "args": {"query": "Redis", "n": 1}, "id": "1"}]),
            "done",
        ),
        generate_response=fake_generate_response,
    )

    saved = list((wiki / "explorations").glob("*.md"))
    status_events = events_of_type(events, "status")

    assert len(saved) == 1
    assert "[[ghost-link]]" not in saved[0].read_text(encoding="utf-8")
    assert any("Saved exploration" in event["data"] for event in status_events)


@pytest.mark.asyncio
async def test_chat_explicit_save_reports_openkb_save_failure(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-explorations"
    outside_dir.mkdir()
    try:
        (wiki / "explorations").symlink_to(outside_dir)
    except OSError:
        pytest.skip("symlinks are unavailable")
    (tmp_path / "manifest.json").write_text(json.dumps({"status": "ready"}), encoding="utf-8")
    (wiki / "index.md").write_text("# Index", encoding="utf-8")
    monkeypatch.setenv("KHOJ_KB_ENGINE", "openkb")
    monkeypatch.setenv("KHOJ_ENABLE_OPENKB", "true")
    monkeypatch.setenv("KHOJ_OPENKB_ROOT", str(tmp_path))

    async def fake_wiki_search_documents(*args, **kwargs):
        return (
            [
                {
                    "query": "openkb:Redis",
                    "file": "wiki/index.md",
                    "uri": "openkb://local/wiki/index.md",
                    "compiled": "# Index\nRedis evidence",
                }
            ],
            ["openkb:Redis"],
            args[0],
        )

    monkeypatch.setattr("khoj.processor.conversation.notes_tool_loop.wiki_search_documents", fake_wiki_search_documents)
    events = await run_chat(
        monkeypatch,
        "/notes 保存这次 Redis 回答",
        send_message_to_model_wrapper=fake_notes_model(
            json.dumps([{"name": "wiki_search_documents", "args": {"query": "Redis", "n": 1}, "id": "1"}]),
            "done",
        ),
    )

    status_events = events_of_type(events, "status")
    assert any("Could not save exploration" in event["data"] for event in status_events)
    assert events_of_type(events, "end_response")
    assert not any(outside_dir.iterdir())
