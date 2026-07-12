from types import SimpleNamespace

import pytest

from khoj.processor.conversation import knowledge_workspace
from khoj.routers import helpers, research
from khoj.routers.research import _document_research_tools
from khoj.utils.helpers import ConversationCommand


def test_document_tools_are_file_first():
    tools = _document_research_tools()

    assert tools == [
        ConversationCommand.RegexSearchFiles,
        ConversationCommand.ViewFile,
        ConversationCommand.ListFiles,
        ConversationCommand.KbHeadings,
        ConversationCommand.KbResolveLink,
    ]


@pytest.mark.asyncio
async def test_local_kb_exposes_disk_document_tools_without_entries(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("local notes", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

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
    assert "kb_headings" in tool_names
    assert "kb_resolve_link" in tool_names


@pytest.mark.asyncio
async def test_local_kb_headings_and_resolve_link_helpers(tmp_path, monkeypatch):
    (tmp_path / "index.md").write_text("[Java](interview/java.md)", encoding="utf-8")
    interview = tmp_path / "interview"
    interview.mkdir()
    (interview / "java.md").write_text("# Java\nbody\n## HashMap\nnotes\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    headings = [item async for item in knowledge_workspace.view_workspace_headings("interview/java.md")]
    resolved = [
        item async for item in knowledge_workspace.resolve_workspace_link("index.md", "[Java](interview/java.md)")
    ]

    assert "# Java (L1-L4)" in headings[0]["compiled"]
    assert "## HashMap (L3-L4)" in headings[0]["compiled"]
    assert resolved[0]["compiled"] == "Resolved to interview/java.md"


def test_local_kb_counts_as_user_document_source(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("local notes", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setattr(helpers.EntryAdapters, "user_has_entries", lambda user: False)

    assert helpers.has_user_document_source(object()) is True


def test_user_config_has_documents_with_local_kb(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("local notes", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setattr(helpers.EntryAdapters, "user_has_entries", lambda user: False)
    request = SimpleNamespace(session={})
    user = SimpleNamespace(username="test")

    config = helpers.get_user_config(user, request)

    assert config["has_documents"] is True
