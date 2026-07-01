from types import SimpleNamespace

import pytest

from khoj.routers import helpers
from khoj.routers.helpers import ChatEvent


@pytest.mark.asyncio
async def test_summarize_uses_kb_read_not_fileobject_raw_text(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("alpha\nbeta\n", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    async def fail_db_lookup(*args, **kwargs):
        raise AssertionError("DB file lookup should not run for local KB file filters")

    captured = {}

    async def fake_summary(q, contextual_data, **kwargs):
        captured["contextual_data"] = contextual_data
        return "summary"

    async def status(message):
        yield f"{ChatEvent.STATUS.value}:{message}"

    monkeypatch.setattr(helpers.FileObjectAdapters, "aget_file_objects_by_names", fail_db_lookup)
    monkeypatch.setattr(helpers, "extract_relevant_summary", fake_summary)

    results = [
        item
        async for item in helpers.generate_summary_from_files(
            "summarize",
            user=SimpleNamespace(email="test@example.com"),
            file_filters=["notes.md"],
            send_status_func=status,
        )
    ]

    assert results[-1] == "summary"
    assert "File: notes.md" in captured["contextual_data"]
    assert "1: alpha" in captured["contextual_data"]
