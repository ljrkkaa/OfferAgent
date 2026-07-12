import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from khoj.processor.conversation import knowledge_workspace
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

    async def asave(self, **kwargs):
        return None

    async def arefresh_from_db(self, **kwargs):
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
    client_app="web",
    client_capabilities=None,
    generate_response=ok_response,
    **patched,
):
    user = SimpleNamespace(id=1)
    user_scope = SimpleNamespace(object=user, client_app=client_app)
    agent = SimpleNamespace(slug="khoj", name="OfferAgent", personality=None)

    async def fake_conversation(*args, **kwargs):
        return FakeConversation()

    async def fake_default_agent():
        return agent

    async def fake_user_name(user):
        return "Test User"

    async def fake_memory_disabled(user):
        return False

    async def fake_pop_message(*args, **kwargs):
        return None

    monkeypatch.setattr(api_chat.ConversationAdapters, "aget_conversation_by_user", fake_conversation)
    monkeypatch.setattr(api_chat.AgentAdapters, "aget_default_agent", fake_default_agent)
    monkeypatch.setattr(api_chat, "is_ready_to_chat", noop_async)
    monkeypatch.setattr(api_chat, "aget_user_name", fake_user_name)
    monkeypatch.setattr(api_chat.ConversationAdapters, "ais_memory_enabled", fake_memory_disabled)
    monkeypatch.setattr(api_chat.ConversationAdapters, "apop_message", fake_pop_message)
    monkeypatch.setattr(api_chat.conversation_command_rate_limiter, "update_and_check_if_valid", noop_async)
    monkeypatch.setattr(api_chat, "agenerate_chat_response", generate_response)
    monkeypatch.setattr(api_chat, "persist_conversation_turn", noop_async)
    if "route_offeragent_intent" not in patched:

        async def fake_default_router(*args, **kwargs):
            return RouteDecision(
                route="default",
                intent="conversation",
                requires_vault_write=False,
                needs_clarification=False,
                question="",
            )

        monkeypatch.setattr(api_chat, "route_offeragent_intent", fake_default_router)
    for name, value in patched.items():
        monkeypatch.setattr(api_chat, name, value)

    body = ChatRequestBody(q=q, stream=True, client_capabilities=client_capabilities)
    return [
        event
        async for event in api_chat.run_conversation_turn(
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


def test_vault_action_mode_separates_web_server_review_from_obsidian_client_actions(tmp_path, monkeypatch):
    capable = ChatRequestBody(q="write", client_capabilities={"vaultActions": True})
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")

    assert api_chat._vault_action_mode(capable, "web") == "server_review"
    assert api_chat._vault_action_mode(capable, "obsidian") == "client_actions"

    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "false")

    assert api_chat._vault_action_mode(capable, "web") == "disabled"
    assert api_chat._vault_action_mode(ChatRequestBody(q="write"), "obsidian") == "disabled"


def fake_notes_model(*responses):
    calls = list(responses)

    async def send_message_to_model_wrapper(**kwargs):
        response = calls.pop(0) if calls else "done"
        if response == "done":
            response = json.dumps({"calls": []})
        return ResponseWithThought(text=response)

    return send_message_to_model_wrapper


@pytest.mark.asyncio
async def test_search_indexed_evidence_dedupes_files_and_prefers_raw_text(monkeypatch):
    user = SimpleNamespace(uuid="user-uuid")

    class Result:
        def __init__(self, file, entry):
            self.additional = {"file": file, "uri": f"file://{file}", "query": "daily"}
            self.corpus_id = file
            self.entry = entry
            self.score = 0.1

    async def fake_query(*args, **kwargs):
        return [
            Result("daily/2026-07-06.md", "chunk one"),
            Result("daily/2026-07-06.md", "chunk two"),
            Result("daily/2026-07-05.md", "chunk three"),
        ]

    async def fake_file_objects(*args, **kwargs):
        return [SimpleNamespace(file_name="daily/2026-07-06.md", raw_text="full daily text")]

    monkeypatch.setattr(knowledge_workspace.text_search, "query", fake_query)
    monkeypatch.setattr(knowledge_workspace.text_search, "collate_results", lambda hits: iter(hits))
    monkeypatch.setattr(knowledge_workspace.FileObjectAdapters, "aget_file_objects_by_names", fake_file_objects)

    refs = await knowledge_workspace.search_indexed_evidence(user, "daily", None, limit=2)

    assert [ref["file"] for ref in refs] == ["daily/2026-07-06.md", "daily/2026-07-05.md"]
    assert refs[0]["compiled"] == "# daily/2026-07-06.md\nfull daily text"
    assert refs[1]["compiled"] == "chunk three"


@pytest.mark.asyncio
async def test_default_chat_uses_unified_agent_runtime(monkeypatch):
    captured = {}
    persisted = []

    async def fake_agent_runtime(*args, **kwargs):
        captured["runtime_kwargs"] = kwargs
        return AgentToolLoopResult(
            references=[{"query": "append_note", "status": "written", "file": "agent.md", "action": "append_note"}],
            inferred_queries=["agent evaluation"],
            online_results={"agent evaluation": {"organic": [{"title": "source"}]}},
            program_context=["Notes write tool result: written"],
            used_workspace_tools=True,
        )

    async def fake_persist(turn, *, update_memory=True):
        persisted.append((turn.used_workspace_tools, update_memory))

    async def fake_generate_response(
        q,
        chat_history,
        conversation,
        compiled_references,
        online_results,
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
        persist_conversation_turn=fake_persist,
    )

    assert captured["runtime_kwargs"]["allow_web"] is True
    assert captured["runtime_kwargs"]["write_mode"] == "disabled"
    assert captured["compiled_references"][-1]["query"] == "append_note"
    assert "agent evaluation" in captured["online_results"]
    assert "Notes write tool result" in "\n".join(captured["program_execution_context"])
    assert events_of_type(events, "references")[0]["data"]["inferredQueries"] == ["agent evaluation"]
    assert persisted == [(True, True)]


@pytest.mark.asyncio
async def test_clarification_response_uses_same_turn_persistence_boundary(monkeypatch):
    persisted = []

    async def fake_router(*args, **kwargs):
        return RouteDecision(
            route="default",
            intent="ambiguous_request",
            requires_vault_write=False,
            needs_clarification=True,
            question="你希望我读取资料，还是修改笔记？",
        )

    async def fake_persist(turn, *, update_memory):
        persisted.append((turn.response, update_memory))

    events = await run_chat(
        monkeypatch,
        "帮我处理一下",
        route_offeragent_intent=fake_router,
        persist_conversation_turn=fake_persist,
    )

    assert persisted == [("你希望我读取资料，还是修改笔记？", False)]
    assert events_of_type(events, "message")[0]["data"] == "你希望我读取资料，还是修改笔记？"


@pytest.mark.asyncio
async def test_message_delimiter_is_escaped_inside_stream_envelope(monkeypatch):
    delimiter = api_chat.ChatEvent.END_EVENT.value

    async def response_with_delimiter(*args, **kwargs):
        async def stream():
            yield ResponseWithThought(text=f"before{delimiter}after")

        return stream(), {}

    events = await run_chat(monkeypatch, "test delimiter", generate_response=response_with_delimiter)

    message_frames = events_of_type(events, "message")
    assert message_frames[0]["data"] == f"before{delimiter}after"
    assert all(delimiter not in event for event in events if event != delimiter)


@pytest.mark.asyncio
async def test_obsidian_router_notes_write_returns_vault_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    async def fake_router(*args, **kwargs):
        return RouteDecision(
            route="default",
            intent="create_daily_plan",
            requires_vault_write=True,
            needs_clarification=False,
            question="",
        )

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
                                "write_intent": "preserve",
                                "source_refs": [{"type": "current_user_request"}],
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            json.dumps({"grounded": True, "reason": "content comes from the current request"}),
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
            "mode": "create_only",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["/notes 写入 daily/test.md", "/default 写入 daily/test.md"])
async def test_explicit_write_capable_commands_preserve_required_write_outcome(tmp_path, monkeypatch, query):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    captured = {}

    async def fake_router(*args, **kwargs):
        return RouteDecision(
            route="default",
            intent="write_note",
            requires_vault_write=True,
            needs_clarification=False,
            question="",
        )

    async def fake_collect(*args, **kwargs):
        captured["require_write_action"] = kwargs["require_write_action"]
        return AgentToolLoopResult(used_workspace_tools=True)

    await run_chat(
        monkeypatch,
        query,
        client_app="obsidian",
        client_capabilities={"vaultActions": True},
        route_offeragent_intent=fake_router,
        collect_agent_context_and_actions=fake_collect,
    )

    assert captured["require_write_action"] is True


@pytest.mark.asyncio
async def test_disabled_client_keeps_truthful_no_write_context(monkeypatch):
    captured = {}

    async def fake_router(*args, **kwargs):
        return RouteDecision(
            route="default",
            intent="write_note",
            requires_vault_write=True,
            needs_clarification=False,
            question="",
        )

    async def fake_collect(*args, **kwargs):
        captured["require_write_action"] = kwargs["require_write_action"]
        return AgentToolLoopResult(used_workspace_tools=True)

    async def capture_response(*args, **kwargs):
        captured["program_context"] = args[12]

        async def stream():
            yield ResponseWithThought(text="not written")

        return stream(), {}

    await run_chat(
        monkeypatch,
        "写入 daily/test.md",
        route_offeragent_intent=fake_router,
        collect_agent_context_and_actions=fake_collect,
        generate_response=capture_response,
    )

    assert captured["require_write_action"] is False
    assert any("cannot prepare reviewed VaultActions" in item for item in captured["program_context"])


@pytest.mark.asyncio
async def test_capable_client_can_search_web_then_prepare_vault_action(tmp_path, monkeypatch):
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
    captured = {}

    async def fake_generate_response(
        q,
        chat_history,
        conversation,
        compiled_references,
        online_results,
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
        client_app="obsidian",
        client_capabilities={"vaultActions": True},
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

    assert target.read_text(encoding="utf-8") == "# Agent Eval\n"
    assert captured["compiled_references"][-1]["status"] == "action_prepared"
    assert "agent evaluation" in captured["online_results"]
    assert "waiting for the client" in "\n".join(captured["program_execution_context"])
    assert events_of_type(events, "vault_actions")[0]["data"]["actions"][0]["op"] == "append_file"


@pytest.mark.asyncio
async def test_chat_notes_local_kb_uses_main_tool_loop(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Redis evidence", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    events = await run_chat(
        monkeypatch,
        "/notes Redis",
        send_message_to_model_wrapper=fake_notes_model(
            json.dumps({"calls": [{"name": "view_file", "args": {"path": "notes.md"}, "id": "1"}]}),
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
        search_indexed_evidence=fake_indexed_notes,
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
        search_indexed_evidence=fake_indexed_notes,
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

    monkeypatch.setattr(
        "khoj.processor.conversation.knowledge_workspace.wiki_search_documents", fake_wiki_search_documents
    )
    events = await run_chat(
        monkeypatch,
        "/notes Redis",
        send_message_to_model_wrapper=fake_notes_model(
            json.dumps({"calls": [{"name": "wiki_search_documents", "args": {"query": "Redis", "n": 1}, "id": "1"}]}),
            "done",
        ),
    )
    reference_events = events_of_type(events, "references")

    assert reference_events[0]["data"]["context"][0]["uri"] == "openkb://local/wiki/concepts/redis.md"


@pytest.mark.asyncio
async def test_chat_notes_write_request_prepares_vault_action(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("# 复盘\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    captured = {}

    async def fake_generate_response(
        q,
        chat_history,
        conversation,
        compiled_references,
        online_results,
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
        client_app="obsidian",
        client_capabilities={"vaultActions": True},
        send_message_to_model_wrapper=fake_notes_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "notes.md",
                                "content": "HashMap 扩容要讲清楚",
                                "heading": "复盘",
                                "write_intent": "preserve",
                                "source_refs": [{"type": "current_user_request"}],
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            json.dumps({"grounded": True, "reason": "content comes from the current request"}),
            "done",
        ),
        generate_response=fake_generate_response,
    )
    reference_events = events_of_type(events, "references")

    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "# 复盘\n"
    assert captured["compiled_references"][-1]["query"] == "vault_action"
    context = "\n".join(captured["program_execution_context"])
    assert '"action": "append_note"' in context
    assert '"status": "action_prepared"' in context
    assert '"file": "notes.md"' in context
    assert reference_events[0]["data"]["context"][-1]["status"] == "action_prepared"
    assert events_of_type(events, "vault_actions")[0]["data"]["actions"][0]["mode"] == "append"


@pytest.mark.asyncio
async def test_web_notes_write_creates_persistent_review_batch(tmp_path, monkeypatch):
    target = tmp_path / "notes.md"
    target.write_text("# 复盘\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    captured = {}

    def fake_create_batch(*, user, conversation, turn_id, actions):
        captured["batch_args"] = {
            "user_id": user.id,
            "conversation_id": conversation.id,
            "turn_id": turn_id,
            "actions": actions,
        }
        return SimpleNamespace(
            id="batch-id",
            conversation_id=conversation.id,
            turn_id=turn_id,
            status="pending",
            actions=actions,
            previews=[{"path": "notes.md", "diff": "+HashMap 扩容要讲清楚", "truncated": False}],
            expires_at=datetime(2026, 7, 10, 12, 30, tzinfo=UTC),
            result={},
        )

    async def fake_generate_response(
        q,
        chat_history,
        conversation,
        compiled_references,
        online_results,
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
            yield ResponseWithThought(text="等待你确认后写入。")

        return stream(), {}

    events = await run_chat(
        monkeypatch,
        "/notes 写进 `notes.md`：HashMap 扩容要讲清楚",
        client_app="web",
        client_capabilities={"vaultActions": True},
        create_vault_action_batch=fake_create_batch,
        send_message_to_model_wrapper=fake_notes_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "notes.md",
                                "content": "HashMap 扩容要讲清楚",
                                "heading": "复盘",
                                "write_intent": "preserve",
                                "source_refs": [{"type": "current_user_request"}],
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            json.dumps({"grounded": True, "reason": "content comes from the current request"}),
            "done",
        ),
        generate_response=fake_generate_response,
    )

    payload = events_of_type(events, "vault_actions")[0]["data"]
    assert target.read_text(encoding="utf-8") == "# 复盘\n"
    assert payload["id"] == "batch-id"
    assert payload["status"] == "pending"
    assert payload["previews"][0]["path"] == "notes.md"
    assert captured["batch_args"]["actions"] == payload["actions"]
    assert captured["compiled_references"][-1]["query"] == "vault_action_batch"
    assert "pending review" in "\n".join(captured["program_execution_context"])


@pytest.mark.asyncio
async def test_daily_plan_request_reads_matching_skill_and_template_before_web_batch(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skills" / "daily-planner"
    template_dir = tmp_path / "templates"
    skill_dir.mkdir(parents=True)
    template_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: daily-planner\n"
        'description: "Use for 每日计划、学习计划和某天的 daily 写入。"\n'
        "---\n\n"
        "DAILY_SKILL_SENTINEL: read the template before preparing a reviewed write.\n",
        encoding="utf-8",
    )
    (template_dir / "daily-template.md").write_text(
        "# TEMPLATE_SENTINEL {{date}} 每日计划\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    planner_queries = []
    captured = {}
    planner_responses = iter(
        [
            {"calls": [{"name": "read_skill", "args": {"name": "daily-planner"}, "id": "1"}]},
            {"calls": [{"name": "view_file", "args": {"path": "templates/daily-template.md"}, "id": "2"}]},
            {
                "calls": [
                    {
                        "name": "append_note",
                        "args": {
                            "path": "daily/2026-07-10.md",
                            "content": "# 2026-07-10 每日计划\n\n- [ ] 复习 Agent 工具系统\n",
                            "write_intent": "summarize",
                            "source_refs": [{"type": "tool_result", "tool": "view_file"}],
                        },
                        "id": "3",
                    }
                ]
            },
            {"calls": []},
        ]
    )

    async def skill_aware_planner(**kwargs):
        if kwargs.get("system_message") == "You are a strict write-grounding verifier for a local notes agent.":
            return ResponseWithThought(text=json.dumps({"grounded": True, "reason": "uses the viewed template"}))
        planner_queries.append(kwargs["query"])
        return ResponseWithThought(text=json.dumps(next(planner_responses)))

    def fake_create_batch(*, user, conversation, turn_id, actions):
        captured["actions"] = actions
        return SimpleNamespace(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            turn_id=turn_id,
            status="pending",
            actions=actions,
            previews=[
                {
                    "op": "create_file",
                    "path": "daily/2026-07-10.md",
                    "diff": "+# 2026-07-10 每日计划",
                    "truncated": False,
                }
            ],
            expires_at=datetime(2026, 7, 10, 12, 30, tzinfo=UTC),
            result={},
        )

    events = await run_chat(
        monkeypatch,
        "帮我制定并写入 2026-07-10 的学习计划",
        client_app="web",
        client_capabilities={"vaultActions": True},
        create_vault_action_batch=fake_create_batch,
        send_message_to_model_wrapper=skill_aware_planner,
    )

    payload = events_of_type(events, "vault_actions")[0]["data"]
    assert "daily-planner" in planner_queries[0]
    assert "每日计划" in planner_queries[0]
    assert "DAILY_SKILL_SENTINEL" in planner_queries[1]
    assert "TEMPLATE_SENTINEL" in planner_queries[2]
    assert captured["actions"][0]["path"] == "daily/2026-07-10.md"
    assert payload["status"] == "pending"
    assert not (tmp_path / "daily" / "2026-07-10.md").exists()


@pytest.mark.asyncio
async def test_chat_notes_tool_failure_stops_before_final_response(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("# 复盘\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

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
async def test_chat_notes_without_vault_capability_cannot_write(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("# 复盘\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    events = await run_chat(
        monkeypatch,
        "/notes 写进 notes.md：HashMap 扩容要讲清楚",
        client_app="unsupported-client",
        send_message_to_model_wrapper=fake_notes_model(
            json.dumps(
                {
                    "calls": [
                        {
                            "name": "append_note",
                            "args": {
                                "path": "notes.md",
                                "content": "blocked",
                                "write_intent": "preserve",
                                "source_refs": [{"type": "current_user_request"}],
                            },
                            "id": "1",
                        }
                    ]
                }
            ),
            "done",
        ),
    )

    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "# 复盘\n"
    assert events_of_type(events, "vault_actions") == []
    assert "couldn't find enough local knowledge base evidence" in "".join(events)
