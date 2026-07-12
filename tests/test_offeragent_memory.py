import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from khoj.processor.conversation.offeragent_memory import (
    MemorySelection,
    MemoryWriteDecision,
    apply_memory_write_decision,
    build_memory_selection_prompt,
    build_memory_write_prompt,
    create_memory,
    get_memory_by_id,
    list_memories,
    select_memories_from_decision,
)


def _delayed_memory_update(root, user_uuid, memory_id, ready, release):
    os.environ["KHOJ_LOCAL_KB_PATH"] = root
    from khoj.processor.conversation import offeragent_memory

    original_write = offeragent_memory._write_memory

    def delayed_write(*args, **kwargs):
        ready.set()
        if not release.wait(timeout=5):
            raise RuntimeError("test release timed out")
        return original_write(*args, **kwargs)

    offeragent_memory._write_memory = delayed_write
    offeragent_memory.update_memory(SimpleNamespace(uuid=user_uuid), memory_id, "updated value")


def _delete_memory_in_process(root, user_uuid, memory_id, started):
    os.environ["KHOJ_LOCAL_KB_PATH"] = root
    from khoj.processor.conversation.offeragent_memory import delete_memory

    started.set()
    delete_memory(SimpleNamespace(uuid=user_uuid), memory_id)


def test_reference_memory_type_is_rejected(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    with pytest.raises(ValueError):
        create_memory(default_user, "daily files live in a folder", "reference")


def test_greeting_does_not_select_memory(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    memory = create_memory(default_user, "我偏好简洁回答", "feedback")

    prompt = build_memory_selection_prompt("你好", [memory])

    assert "casual small talk" in prompt
    assert select_memories_from_decision(MemorySelection(selected_memories=[]), [memory]) == []


def test_explicit_preference_creates_memory(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    memory = apply_memory_write_decision(
        default_user,
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


def test_daily_memory_correction_creates_feedback(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    memory = apply_memory_write_decision(
        default_user,
        MemoryWriteDecision(
            action="create",
            memory_type="feedback",
            raw="不要把 daily 总结写进记忆",
            description="不要保存 daily 总结",
        ),
    )

    assert memory is not None
    assert memory.memory_type == "feedback"
    assert "daily" in memory.raw


def test_daily_analysis_with_notes_does_not_create_memory(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    prompt = build_memory_write_prompt(
        "评价近五天 daily 学习情况",
        [],
        current_date="2026-07-07",
        used_workspace_tools=True,
    )
    memory = apply_memory_write_decision(default_user, MemoryWriteDecision(action="none"))

    assert "local_notes_or_tools_used: True" in prompt
    assert "daily summary" in prompt
    assert memory is None
    assert list_memories(default_user) == []


def test_ignore_memory_query_does_not_select_memory(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    memory = create_memory(default_user, "我偏好 Redis 面试题", "user")

    prompt = build_memory_selection_prompt("忽略记忆，Redis 怎么复习？", [memory])

    assert "not use memory" in prompt
    assert select_memories_from_decision(MemorySelection(selected_memories=[]), [memory]) == []


def test_memories_are_isolated_by_user(tmp_path, monkeypatch, default_user, default_user2):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    memory = create_memory(default_user, "只属于第一个用户", "user")

    assert [item.id for item in list_memories(default_user)] == [memory.id]
    assert list_memories(default_user2) == []


def test_concurrent_same_name_memories_are_unique_and_indexed(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))

    def create(index):
        return create_memory(
            default_user,
            f"memory value {index}",
            "project",
            name="same memory",
            description="same memory",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        created = list(executor.map(create, range(8)))

    memories = list_memories(default_user)
    assert len({memory.id for memory in created}) == 8
    assert {memory.raw for memory in memories} == {f"memory value {index}" for index in range(8)}
    index_text = (created[0].path.parent / "MEMORY.md").read_text(encoding="utf-8")
    assert all(f"({memory.id})" in index_text for memory in created)


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded, use of fork.*:DeprecationWarning")
def test_cross_process_update_cannot_recreate_a_concurrently_deleted_memory(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    memory = create_memory(default_user, "original value", "project")
    context = multiprocessing.get_context("fork")
    update_ready = context.Event()
    update_release = context.Event()
    delete_started = context.Event()
    updater = context.Process(
        target=_delayed_memory_update,
        args=(str(tmp_path), str(default_user.uuid), memory.id, update_ready, update_release),
    )
    deleter = context.Process(
        target=_delete_memory_in_process,
        args=(str(tmp_path), str(default_user.uuid), memory.id, delete_started),
    )

    updater.start()
    assert update_ready.wait(timeout=5)
    deleter.start()
    assert delete_started.wait(timeout=5)
    time.sleep(0.2)
    update_release.set()
    updater.join(timeout=5)
    deleter.join(timeout=5)

    assert updater.exitcode == 0
    assert deleter.exitcode == 0
    assert get_memory_by_id(default_user, memory.id) is None


@pytest.mark.parametrize(
    "payload",
    [
        '```json\n{"action":"none"}\n```',
        '{"action":"none","legacy":true}',
        "{'action':'none'}",
    ],
)
def test_memory_write_decision_rejects_non_protocol_json(payload):
    with pytest.raises(ValueError):
        MemoryWriteDecision.model_validate_json(payload)
