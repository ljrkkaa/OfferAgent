import pytest

from khoj.integrations.qqbot.adapter import handle_message, is_allowed, normalize_inbound, split_response


def test_normalize_official_style_payload():
    message = normalize_inbound(
        {
            "d": {
                "content": " Redis 怎么回答 ",
                "author": {"id": "user-1"},
                "channel_id": "channel-1",
                "guild_id": "guild-1",
            }
        }
    )

    assert message.text == "Redis 怎么回答"
    assert message.user_id == "user-1"
    assert message.chat_id == "channel-1"
    assert message.group_id == "guild-1"
    assert message.client == "qqbot"


def test_allowlist_blocks_by_default_and_allows_known_ids():
    message = normalize_inbound({"content": "hello", "user_id": "u1", "chat_id": "c1", "group_id": "g1"})

    assert not is_allowed(message, [])
    assert is_allowed(message, ["u1"])
    assert is_allowed(message, ["c1"])
    assert is_allowed(message, ["g1"])


def test_split_response_keeps_chunks_under_limit():
    chunks = split_response("第一段\n\n第二段很长很长", limit=6)

    assert chunks[0] == "第一段"
    assert "".join(chunks[1:]) == "第二段很长很长"
    assert all(len(chunk) <= 6 for chunk in chunks)


@pytest.mark.asyncio
async def test_handle_message_calls_chat_client_with_qqbot_context():
    captured = {}

    async def chat_client(**kwargs):
        captured.update(kwargs)
        return {"response": "ok"}

    responses = await handle_message(
        {"content": "HashMap 怎么说", "user_id": "u1", "chat_id": "c1"},
        chat_client,
        allowlist=["u1"],
    )

    assert captured == {
        "text": "HashMap 怎么说",
        "client": "qqbot",
        "user_id": "u1",
        "chat_id": "c1",
        "group_id": None,
    }
    assert [response.text for response in responses] == ["ok"]


@pytest.mark.asyncio
async def test_handle_message_returns_no_response_when_blocked():
    called = False

    def chat_client(**kwargs):
        nonlocal called
        called = True
        return "ok"

    responses = await handle_message(
        {"content": "写进 notes.md", "user_id": "u1", "chat_id": "c1"},
        chat_client,
        allowlist=[],
    )

    assert responses == []
    assert called is False
