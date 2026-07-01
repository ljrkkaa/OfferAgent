import json

import pytest

from khoj.processor.conversation.notes_tool_loop import collect_notes_evidence_with_tools
from khoj.processor.conversation.utils import ResponseWithThought


def fake_model(*responses):
    calls = list(responses)

    async def send_message(**kwargs):
        if calls:
            return ResponseWithThought(text=calls.pop(0))
        return ResponseWithThought(text="done")

    return send_message


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
        "写进 notes.md",
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
            "done",
        ),
    )

    assert "HashMap 扩容要讲清楚。" in (tmp_path / "notes.md").read_text(encoding="utf-8")
    assert result.references[-1]["query"] == "append_note"
    assert result.references[-1]["status"] == "written"


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
        "新建 raw/test.md",
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
