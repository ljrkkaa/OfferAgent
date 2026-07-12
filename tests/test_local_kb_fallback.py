import pytest

from khoj.processor.conversation import knowledge_workspace


async def collect(async_iterable):
    return [item async for item in async_iterable]


@pytest.fixture
def local_kb(tmp_path, monkeypatch):
    (tmp_path / "local.md").write_text("local only", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.delenv("KHOJ_OBSIDIAN_VAULT_PATH", raising=False)
    return tmp_path


@pytest.mark.asyncio
async def test_local_kb_miss_does_not_fall_back_to_db_by_default(local_kb, monkeypatch):
    async def fail_db(*args, **kwargs):
        raise AssertionError("DB fallback should be disabled")

    monkeypatch.setattr(knowledge_workspace.FileObjectAdapters, "aget_file_objects_by_name", fail_db)
    monkeypatch.setattr(knowledge_workspace.FileObjectAdapters, "aget_file_objects_by_regex", fail_db)
    monkeypatch.setattr(knowledge_workspace.FileObjectAdapters, "aget_all_file_objects", fail_db)

    viewed = await collect(knowledge_workspace.view_workspace_file("db.md"))
    grepped = await collect(knowledge_workspace.grep_workspace_files("needle"))
    listed = await collect(knowledge_workspace.list_workspace_files(pattern="*.txt"))

    assert "not found in local knowledge base" in viewed[0][0]["compiled"]
    assert grepped[0]["compiled"] == "No matches found."
    assert listed[0]["compiled"] == "No files found."
