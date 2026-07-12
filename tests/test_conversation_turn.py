import asyncio
import logging
from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from khoj.database.adapters import ConversationAdapters
from khoj.database.models import ChatMessageModel, Conversation
from khoj.processor.conversation import conversation_turn
from khoj.processor.conversation.conversation_turn import ConversationTurn


def make_turn(**overrides):
    values = {
        "user": SimpleNamespace(username="tester"),
        "user_message": "根据 experiences/a.md 规划",
        "turn_id": "turn-1",
        "conversation_id": "conversation-1",
        "response": "## 今日计划\n- [ ] 复习 RAG",
    }
    values.update(overrides)
    return ConversationTurn(**values)


def test_chat_message_model_preserves_artifacts():
    message = ChatMessageModel(
        by="khoj",
        message="answer",
        artifacts=[{"id": "assistant:turn-1", "type": "assistant_response", "content": "answer"}],
    )

    assert message.model_dump()["artifacts"][0]["id"] == "assistant:turn-1"


@pytest.mark.asyncio
async def test_persist_conversation_turn_adds_assistant_artifact(monkeypatch):
    captured = {}

    async def fake_save_conversation(*args, **kwargs):
        captured["messages"] = args[1]
        return SimpleNamespace(id="conv-1", agent=None)

    monkeypatch.setattr(conversation_turn.ConversationAdapters, "save_conversation", fake_save_conversation)
    turn = make_turn(
        compiled_references=[
            {
                "query": "view_file:experiences/a.md",
                "file": "experiences/a.md",
                "uri": "local-kb://experiences/a.md#L1-L3",
                "compiled": "large source text should not be duplicated",
            }
        ]
    )

    await conversation_turn.persist_conversation_turn(turn, update_memory=False)

    artifact = captured["messages"][1].artifacts[0]
    assert artifact["id"] == "assistant:turn-1"
    assert artifact["content"] == "## 今日计划\n- [ ] 复习 RAG"
    assert artifact["source_refs"] == [
        {
            "query": "view_file:experiences/a.md",
            "file": "experiences/a.md",
            "uri": "local-kb://experiences/a.md#L1-L3",
        }
    ]


@pytest.mark.asyncio
async def test_turn_persistence_is_exactly_once_when_completion_races_disconnect():
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def writer(turn, *, update_memory):
        calls.append((turn.response, update_memory))
        started.set()
        await release.wait()

    turn = make_turn(writer=writer)
    completed = asyncio.create_task(turn.persist())
    await started.wait()
    interrupted = asyncio.create_task(turn.persist(interrupted=True, update_memory=False))
    release.set()

    assert await completed is True
    assert await interrupted is False
    assert calls == [("## 今日计划\n- [ ] 复习 RAG", True)]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_independent_turns_append_to_same_conversation_without_lost_update(default_user):
    conversation = await Conversation.objects.acreate(user=default_user, conversation_log={"chat": []})
    first = make_turn(
        user=default_user,
        conversation_id=str(conversation.id),
        turn_id="turn-a",
        user_message="first question",
        response="first answer",
    )
    second = make_turn(
        user=default_user,
        conversation_id=str(conversation.id),
        turn_id="turn-b",
        user_message="second question",
        response="second answer",
    )

    await asyncio.gather(
        first.persist(update_memory=False),
        second.persist(update_memory=False),
    )

    await conversation.arefresh_from_db()
    turn_ids = [message.get("turnId") for message in conversation.conversation_log["chat"]]
    assert turn_ids.count("turn-a") == 2
    assert turn_ids.count("turn-b") == 2


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_duplicate_turn_id_is_idempotent_across_independent_turn_objects(default_user):
    conversation = await Conversation.objects.acreate(user=default_user, conversation_log={"chat": []})
    first = make_turn(user=default_user, conversation_id=str(conversation.id), turn_id="same-turn")
    second = make_turn(user=default_user, conversation_id=str(conversation.id), turn_id="same-turn")

    await asyncio.gather(
        first.persist(update_memory=False),
        second.persist(update_memory=False),
    )

    await conversation.arefresh_from_db()
    turn_ids = [message.get("turnId") for message in conversation.conversation_log["chat"]]
    assert turn_ids == ["same-turn", "same-turn"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_interrupted_turn_retry_completes_missing_assistant_message(default_user):
    conversation = await Conversation.objects.acreate(user=default_user, conversation_log={"chat": []})
    interrupted = make_turn(
        user=default_user,
        conversation_id=str(conversation.id),
        turn_id="resumed-turn",
        user_message="question",
        response="partial answer",
    )
    completed = make_turn(
        user=default_user,
        conversation_id=str(conversation.id),
        turn_id="resumed-turn",
        user_message="question",
        response="completed answer",
    )

    await interrupted.persist(interrupted=True, update_memory=False)
    await ConversationAdapters.apop_message(default_user, str(conversation.id), interrupted=True)
    await completed.persist(update_memory=False)

    await conversation.arefresh_from_db()
    messages = conversation.conversation_log["chat"]
    assert [(message["by"], message["message"]) for message in messages] == [
        ("you", "question"),
        ("khoj", "completed answer"),
    ]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_turn_append_and_message_delete_share_the_same_row_lock(default_user):
    conversation = await Conversation.objects.acreate(
        user=default_user,
        conversation_log={
            "chat": [
                {"by": "you", "message": "old question", "turnId": "old-turn"},
                {"by": "khoj", "message": "old answer", "turnId": "old-turn"},
            ]
        },
    )
    new_turn = make_turn(
        user=default_user,
        conversation_id=str(conversation.id),
        turn_id="new-turn",
        user_message="new question",
        response="new answer",
    )

    deleted, persisted = await asyncio.gather(
        sync_to_async(ConversationAdapters.delete_message_by_turn_id, thread_sensitive=False)(
            default_user,
            str(conversation.id),
            "old-turn",
        ),
        new_turn.persist(update_memory=False),
    )

    await conversation.arefresh_from_db()
    turn_ids = [message.get("turnId") for message in conversation.conversation_log["chat"]]
    assert deleted is True
    assert persisted is True
    assert turn_ids == ["new-turn", "new-turn"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_interrupted_message_pop_uses_atomic_adapter(default_user):
    conversation = await Conversation.objects.acreate(
        user=default_user,
        conversation_log={
            "chat": [
                {"by": "you", "message": "question", "turnId": "turn-1"},
                {"by": "khoj", "message": "", "turnId": "turn-1"},
            ]
        },
    )

    popped = await ConversationAdapters.apop_message(
        default_user,
        str(conversation.id),
        interrupted=True,
    )

    await conversation.arefresh_from_db()
    assert popped is not None and popped.turnId == "turn-1"
    assert [message["by"] for message in conversation.conversation_log["chat"]] == ["you"]


@pytest.mark.asyncio
async def test_interrupted_turn_saves_empty_marker_without_memory_update():
    captured = {}

    async def writer(turn, *, update_memory):
        captured["response"] = turn.response
        captured["update_memory"] = update_memory

    turn = make_turn(response="partial answer", writer=writer)

    assert await turn.persist(interrupted=True) is True
    assert captured == {"response": "", "update_memory": False}
    assert turn.response == "partial answer"


@pytest.mark.asyncio
async def test_persist_schedules_memory_without_waiting(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    captured = {}

    async def fake_save_conversation(*args, **kwargs):
        return SimpleNamespace(id="conv-1", agent="agent-1")

    async def fake_memory_update(turn, agent):
        captured["message"] = turn.user_message
        captured["agent"] = agent
        captured["used_workspace_tools"] = turn.used_workspace_tools
        started.set()
        await release.wait()

    monkeypatch.setattr(conversation_turn.ConversationAdapters, "save_conversation", fake_save_conversation)
    monkeypatch.setattr(conversation_turn, "_run_memory_update", fake_memory_update)
    turn = make_turn(user_message="以后回答简洁一点", response="好的", used_workspace_tools=True)

    await asyncio.wait_for(conversation_turn.persist_conversation_turn(turn), timeout=0.2)
    await asyncio.wait_for(started.wait(), timeout=0.2)

    assert captured == {
        "message": "以后回答简洁一点",
        "agent": "agent-1",
        "used_workspace_tools": True,
    }

    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_background_memory_update_failure_is_logged(monkeypatch, caplog):
    async def failing_memory_update(turn, agent):
        raise RuntimeError("memory boom")

    monkeypatch.setattr(conversation_turn, "_run_memory_update", failing_memory_update)

    with caplog.at_level(logging.ERROR, logger=conversation_turn.logger.name):
        task = conversation_turn.schedule_memory_update(make_turn())
        for _ in range(10):
            if task.done():
                break
            await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert task.done()
    assert "OfferAgent background memory update failed" in caplog.text
