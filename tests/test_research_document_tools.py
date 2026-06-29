from types import SimpleNamespace

import pytest

from khoj.routers import helpers, research
from khoj.routers.research import _document_research_tools
from khoj.utils.helpers import ConversationCommand


def test_semantic_search_files_hidden_without_rag_fallback(monkeypatch):
    monkeypatch.delenv("KHOJ_ENABLE_RAG_FALLBACK", raising=False)

    tools = _document_research_tools()

    assert ConversationCommand.SemanticSearchFiles not in tools
    assert tools == [
        ConversationCommand.RegexSearchFiles,
        ConversationCommand.ViewFile,
        ConversationCommand.ListFiles,
    ]


def test_semantic_search_files_visible_with_rag_fallback(monkeypatch):
    monkeypatch.setenv("KHOJ_ENABLE_RAG_FALLBACK", "true")

    tools = _document_research_tools()

    assert tools[0] == ConversationCommand.SemanticSearchFiles


@pytest.mark.asyncio
async def test_local_kb_exposes_disk_document_tools_without_entries(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("local notes", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.delenv("KHOJ_ENABLE_RAG_FALLBACK", raising=False)

    async def no_entries(user):
        return False

    monkeypatch.setattr(research.EntryAdapters, "auser_has_entries", no_entries)
    monkeypatch.setattr(research.AgentAdapters, "get_agent_chat_model", lambda agent, user: None)
    captured = {}

    async def fake_send_message_to_model_wrapper(**kwargs):
        captured["tools"] = kwargs["tools"]
        return SimpleNamespace(text="", thought=None, raw_content=None)

    monkeypatch.setattr(research, "send_message_to_model_wrapper", fake_send_message_to_model_wrapper)

    [item async for item in research.apick_next_tool("read my notes", [], user=object(), user_name="test")]

    tool_names = {tool.name for tool in captured["tools"]}
    assert "list_files" in tool_names
    assert "view_file" in tool_names
    assert "regex_search_files" in tool_names
    assert "semantic_search_files" not in tool_names


@pytest.mark.asyncio
async def test_local_kb_makes_notes_source_available_without_entries(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("local notes", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    async def no_entries(user):
        return False

    monkeypatch.setattr(helpers.EntryAdapters, "auser_has_entries", no_entries)
    monkeypatch.setattr(helpers.AgentAdapters, "get_agent_chat_model", lambda agent, user: None)
    captured = {}

    async def fake_send_message_to_model_wrapper(query, **kwargs):
        captured["prompt"] = query
        return SimpleNamespace(text='{"source":["notes"],"output":"text"}')

    monkeypatch.setattr(helpers, "send_message_to_model_wrapper", fake_send_message_to_model_wrapper)

    selected = await helpers.aget_data_sources_and_output_format("read my notes", [], user=object())

    assert '- "notes":' in captured["prompt"]
    assert selected["sources"] == [ConversationCommand.Notes]


def test_local_kb_counts_as_user_document_source(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("local notes", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setattr(helpers.EntryAdapters, "user_has_entries", lambda user: False)

    assert helpers.has_user_document_source(object()) is True


def test_user_config_has_documents_with_local_kb(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("local notes", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setattr(helpers.EntryAdapters, "user_has_entries", lambda user: False)
    monkeypatch.setattr(helpers, "has_required_scope", lambda *_args, **_kwargs: False)
    request = SimpleNamespace(session={})
    user = SimpleNamespace(username="test")

    config = helpers.get_user_config(user, request)

    assert config["has_documents"] is True
