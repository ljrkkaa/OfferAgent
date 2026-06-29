from types import SimpleNamespace

import pytest

from khoj.routers import helpers


async def collect(async_iterable):
    return [item async for item in async_iterable]


@pytest.fixture
def local_kb(tmp_path, monkeypatch):
    (tmp_path / "local.md").write_text("local only", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.delenv("KHOJ_OBSIDIAN_VAULT_PATH", raising=False)
    monkeypatch.delenv("KHOJ_ENABLE_RAG_FALLBACK", raising=False)
    return tmp_path


@pytest.mark.asyncio
async def test_local_kb_miss_does_not_fall_back_to_db_by_default(local_kb, monkeypatch):
    async def fail_db(*args, **kwargs):
        raise AssertionError("DB fallback should be disabled")

    monkeypatch.setattr(helpers.FileObjectAdapters, "aget_file_objects_by_name", fail_db)
    monkeypatch.setattr(helpers.FileObjectAdapters, "aget_file_objects_by_regex", fail_db)
    monkeypatch.setattr(helpers.FileObjectAdapters, "aget_all_file_objects", fail_db)

    viewed = await collect(helpers.view_file_content("db.md"))
    grepped = await collect(helpers.grep_files("needle"))
    listed = await collect(helpers.list_files(pattern="*.txt"))

    assert "not found in local knowledge base" in viewed[0][0]["compiled"]
    assert grepped[0]["compiled"] == "No matches found."
    assert listed[0]["compiled"] == "No files found."


@pytest.mark.asyncio
async def test_local_kb_miss_can_fall_back_when_enabled(local_kb, monkeypatch):
    monkeypatch.setenv("KHOJ_ENABLE_RAG_FALLBACK", "true")

    async def fake_get_by_name(*args, **kwargs):
        return [SimpleNamespace(raw_text="db file")]

    async def fake_get_by_regex(*args, **kwargs):
        return [SimpleNamespace(file_name="db.txt", raw_text="needle")]

    async def fake_get_all(*args, **kwargs):
        return [SimpleNamespace(file_name="db.txt")]

    monkeypatch.setattr(helpers.FileObjectAdapters, "aget_file_objects_by_name", fake_get_by_name)
    monkeypatch.setattr(helpers.FileObjectAdapters, "aget_file_objects_by_regex", fake_get_by_regex)
    monkeypatch.setattr(helpers.FileObjectAdapters, "aget_all_file_objects", fake_get_all)

    viewed = await collect(helpers.view_file_content("db.md"))
    grepped = await collect(helpers.grep_files("needle"))
    listed = await collect(helpers.list_files(pattern="*.txt"))

    assert viewed[0][0]["compiled"] == "db file"
    assert "db.txt:1: needle" in grepped[0]["compiled"]
    assert listed[0]["compiled"] == "db.txt"
