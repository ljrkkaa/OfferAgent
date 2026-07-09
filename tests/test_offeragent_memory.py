import pytest

from khoj.processor.conversation.offeragent_memory import (
    MemorySelection,
    MemoryWriteDecision,
    apply_memory_write_decision,
    build_memory_selection_prompt,
    build_memory_write_prompt,
    create_memory,
    list_memories,
    select_memories_from_decision,
)


def test_reference_memory_type_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    with pytest.raises(ValueError):
        create_memory("daily files live in a folder", "reference")


def test_greeting_does_not_select_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    memory = create_memory("我偏好简洁回答", "feedback")

    prompt = build_memory_selection_prompt("你好", [memory])

    assert "casual small talk" in prompt
    assert select_memories_from_decision(MemorySelection(selected_memories=[]), [memory]) == []


def test_explicit_preference_creates_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    memory = apply_memory_write_decision(
        MemoryWriteDecision(
            action="create",
            memory_type="feedback",
            raw="我偏好简洁回答",
            description="偏好简洁回答",
        ),
        source_turn_id="turn-1",
    )

    assert memory is not None
    assert memory.memory_type == "feedback"
    assert "偏好简洁回答" in memory.raw


def test_daily_memory_correction_creates_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    memory = apply_memory_write_decision(
        MemoryWriteDecision(
            action="create",
            memory_type="feedback",
            raw="不要把 daily 总结写进记忆",
            description="不要保存 daily 总结",
        )
    )

    assert memory is not None
    assert memory.memory_type == "feedback"
    assert "daily" in memory.raw


def test_daily_analysis_with_notes_does_not_create_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    prompt = build_memory_write_prompt(
        "评价近五天 daily 学习情况",
        [],
        current_date="2026-07-07",
        used_notes_tool_loop=True,
    )
    memory = apply_memory_write_decision(MemoryWriteDecision(action="none"))

    assert "local_notes_or_tools_used: True" in prompt
    assert "daily summary" in prompt
    assert memory is None
    assert list_memories() == []


def test_ignore_memory_query_does_not_select_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    memory = create_memory("我偏好 Redis 面试题", "user")

    prompt = build_memory_selection_prompt("忽略记忆，Redis 怎么复习？", [memory])

    assert "not use memory" in prompt
    assert select_memories_from_decision(MemorySelection(selected_memories=[]), [memory]) == []
