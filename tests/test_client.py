# Standard Modules
import os
import re
import uuid
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from khoj.configure import UserAuthenticationBackend, configure_routes
from khoj.database.adapters import ConversationAdapters, EntryAdapters, FileObjectAdapters
from khoj.database.models import Agent, ChatMessageModel, Conversation, KhojApiUser, KhojUser, VaultActionBatch
from khoj.processor.content.markdown.markdown_to_entries import MarkdownToEntries
from khoj.processor.conversation.vault_actions import create_vault_action_batch
from khoj.search_type import text_search
from khoj.utils import constants, state
from tests.helpers import ChatModelFactory

BGE_TEST_MAX_DISTANCE = 0.36


# Test
# ----------------------------------------------------------------------------------------------------
def test_authentication_backend_seeds_explicit_bootstrap_api_key(monkeypatch):
    monkeypatch.setenv("KHOJ_API_KEY", "kk-old-secret")
    UserAuthenticationBackend()

    monkeypatch.setenv("KHOJ_API_KEY", "kk-bootstrap-secret")
    UserAuthenticationBackend()

    api_user = KhojApiUser.objects.get(token="kk-bootstrap-secret")
    assert api_user.user.username == "default"
    assert api_user.name == "Local bootstrap"
    assert list(KhojApiUser.objects.values_list("token", flat=True)) == ["kk-bootstrap-secret"]


def test_authentication_backend_removes_tokens_without_bootstrap_key(monkeypatch):
    monkeypatch.setenv("KHOJ_API_KEY", "kk-old-secret")
    UserAuthenticationBackend()

    monkeypatch.delenv("KHOJ_API_KEY")
    UserAuthenticationBackend()

    assert not KhojApiUser.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_search_requires_auth_when_anonymous_mode_is_disabled(client):
    # Arrange
    user_query = quote("How to call Khoj from Emacs?")

    # Act
    response = client.get(f"/api/search?q={user_query}")

    # Assert
    assert response.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_search_with_invalid_auth_key(client):
    # Arrange
    headers = {"Authorization": "Bearer invalid-token"}
    user_query = quote("How to call Khoj from Emacs?")

    # Act
    response = client.get(f"/api/search?q={user_query}", headers=headers)

    # Assert
    assert response.status_code == 403


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_search_with_invalid_content_type(client):
    # Arrange
    headers = {"Authorization": "Bearer kk-secret"}
    user_query = quote("How to call Khoj from Emacs?")

    # Act
    response = client.get(f"/api/search?q={user_query}&t=invalid_content_type", headers=headers)

    # Assert
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
def test_search_rejects_negative_limit(client):
    headers = {"Authorization": "Bearer kk-secret"}

    response = client.get("/api/search?q=random&n=-1&t=markdown", headers=headers)

    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("query", ["", "   "])
def test_search_empty_query_returns_empty(client, query):
    headers = {"Authorization": "Bearer kk-secret"}

    response = client.get("/api/search", params={"q": query, "t": "markdown"}, headers=headers)

    assert response.status_code == 200
    assert response.json() == []


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_search_with_valid_content_type(client):
    headers = {"Authorization": "Bearer kk-secret"}
    for content_type in ["all", "markdown", "pdf", "plaintext"]:
        # Act
        response = client.get(f"/api/search?q=random&t={content_type}", headers=headers)
        # Assert
        assert response.status_code == 200, f"Returned status: {response.status_code} for content type: {content_type}"


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_index_update_requires_auth_when_anonymous_mode_is_disabled(client):
    # Arrange
    files = get_sample_files_data()

    # Act
    response = client.patch("/api/content", files=files)

    # Assert
    assert response.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_anonymous_mode_explicitly_uses_default_user(client):
    state.anonymous_mode = True

    response = client.get("/api/search?q=hello")

    assert response.status_code == 200


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_index_update_with_invalid_auth_key(client):
    # Arrange
    files = get_sample_files_data()
    headers = {"Authorization": "Bearer kk-invalid-token"}

    # Act
    response = client.patch("/api/content", files=files, headers=headers)

    # Assert
    assert response.status_code == 403


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_index_update_big_files(client):
    # Arrange
    files = get_big_size_sample_files_data()

    headers = {"Authorization": "Bearer kk-secret"}

    # Act
    response = client.patch("/api/content", files=files, headers=headers)

    # Assert
    assert response.status_code == 429


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_index_update_medium_file(client, api_user4: KhojApiUser):
    # Arrange
    api_token = api_user4.token
    files = get_medium_size_sample_files_data()
    headers = {"Authorization": f"Bearer {api_token}"}

    # Act
    response = client.patch("/api/content", files=files, headers=headers)

    # Assert
    assert response.status_code == 429


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_index_update_normal_file(client, api_user4: KhojApiUser):
    # Arrange
    api_token = api_user4.token
    files = get_sample_files_data()
    headers = {"Authorization": f"Bearer {api_token}"}

    # Act
    response = client.patch("/api/content", files=files, headers=headers)

    # Assert
    assert response.status_code == 200


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_index_update(client):
    # Arrange
    files = get_sample_files_data()
    headers = {"Authorization": "Bearer kk-secret"}

    # Act
    response = client.patch("/api/content", files=files, headers=headers)

    # Assert
    assert response.status_code == 200


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_index_update_fails_if_more_than_1000_files(client, api_user4: KhojApiUser):
    # Arrange
    api_token = api_user4.token
    files = [("files", (f"path/to/filename{i}.markdown", f"Symphony No {i}", "text/markdown")) for i in range(1001)]

    headers = {"Authorization": f"Bearer {api_token}"}

    # Act
    ok_response = client.patch("/api/content", files=files[:1000], headers=headers)
    bad_response = client.patch("/api/content", files=files, headers=headers)

    # Assert
    assert ok_response.status_code == 200
    assert bad_response.status_code == 400
    assert bad_response.content.decode("utf-8") == '{"detail":"Too many files. Maximum number of files is 1000."}'


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_regenerate_with_valid_content_type(client):
    for content_type in ["all", "markdown", "pdf", "plaintext"]:
        # Arrange
        files = get_sample_files_data()
        headers = {"Authorization": "Bearer kk-secret"}

        # Act
        response = client.patch(f"/api/content?t={content_type}", files=files, headers=headers)

        # Assert
        assert response.status_code == 200, f"Returned status: {response.status_code} for content type: {content_type}"


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_get_configured_types_via_api(client, sample_markdown_data, default_user3: KhojUser):
    # Act
    text_search.setup(MarkdownToEntries, sample_markdown_data, regenerate=False, user=default_user3)

    enabled_types = EntryAdapters.get_unique_file_types(user=default_user3).all().values_list("file_type", flat=True)

    # Assert
    assert list(enabled_types) == ["markdown"]


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_get_api_config_types(client, sample_markdown_data, api_user: KhojApiUser):
    # Arrange
    headers = {"Authorization": "Bearer kk-secret"}
    text_search.setup(MarkdownToEntries, sample_markdown_data, regenerate=False, user=api_user.user)

    # Act
    response = client.get("/api/content/types", headers=headers)

    # Assert
    assert response.status_code == 200
    assert set(response.json()) == {"all", "markdown", "plaintext"}


@pytest.mark.django_db(transaction=True)
def test_get_content_source_files_for_search_page(client, sample_markdown_data, api_user: KhojApiUser):
    headers = {"Authorization": "Bearer kk-secret"}
    text_search.setup(MarkdownToEntries, sample_markdown_data, regenerate=False, user=api_user.user)

    response = client.get("/api/content/computer", headers=headers)

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert any(file_name.endswith(".markdown") for file_name in response.json())


@pytest.mark.django_db(transaction=True)
def test_get_missing_content_file_returns_not_found(client):
    headers = {"Authorization": "Bearer kk-secret"}

    response = client.get("/api/content/file?file_name=missing.md", headers=headers)

    assert response.status_code == 404
    assert response.json() == {"error": "File not found"}


@pytest.mark.django_db(transaction=True)
def test_content_file_routes_accept_encoded_special_file_names(client, api_user: KhojApiUser):
    headers = {"Authorization": f"Bearer {api_user.token}"}
    file_name = "notes/R&D #1?.md"
    FileObjectAdapters.create_file_object(api_user.user, file_name, "special file content")

    response = client.get(f"/api/content/file?file_name={quote(file_name, safe='')}", headers=headers)

    assert response.status_code == 200
    assert response.json()["file_name"] == file_name
    assert response.json()["raw_text"] == "special file content"

    response = client.delete(f"/api/content/file?filename={quote(file_name, safe='')}", headers=headers)

    assert response.status_code == 201
    assert FileObjectAdapters.get_file_object_by_name(api_user.user, file_name) is None


@pytest.mark.django_db(transaction=True)
def test_content_files_rejects_negative_page(client, api_user: KhojApiUser):
    response = client.get("/api/content/files?page=-1", headers={"Authorization": f"Bearer {api_user.token}"})

    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
def test_delete_all_content_removes_file_objects(client, api_user: KhojApiUser):
    headers = {"Authorization": f"Bearer {api_user.token}"}
    FileObjectAdapters.create_file_object(api_user.user, "notes/stale.md", "stale content")

    response = client.get("/api/content/files", headers=headers)
    assert response.status_code == 200
    assert "notes/stale.md" in [file["file_name"] for file in response.json()["files"]]

    response = client.delete("/api/content/type/all", headers=headers)
    assert response.status_code == 200

    response = client.get("/api/content/files", headers=headers)
    assert response.status_code == 200
    assert response.json()["files"] == []


@pytest.mark.django_db(transaction=True)
def test_convert_text_file_replaces_invalid_utf8(client, api_user: KhojApiUser):
    headers = {"Authorization": f"Bearer {api_user.token}"}

    response = client.post(
        "/api/content/convert",
        headers=headers,
        files={"files": ("latin1.txt", b"caf\xe9", "text/plain")},
    )

    assert response.status_code == 200
    assert response.json()[0]["name"] == "latin1.txt"
    assert response.json()[0]["content"] == "caf\ufffd"


@pytest.mark.django_db(transaction=True)
def test_convert_text_file_without_content_type(client, api_user: KhojApiUser):
    headers = {"Authorization": f"Bearer {api_user.token}"}

    response = client.post(
        "/api/content/convert",
        headers=headers,
        files={"files": ("plain-no-type", b"hello from upload", None)},
    )

    assert response.status_code == 200
    assert response.json()[0]["name"] == "plain-no-type"
    assert response.json()[0]["content"] == "hello from upload"


@pytest.mark.django_db(transaction=True)
def test_sidebar_chat_session_endpoints_return_lists(client, api_user: KhojApiUser):
    chat_model = ChatModelFactory()
    Agent.objects.update_or_create(
        name="Khoj",
        defaults={
            "slug": "khoj",
            "chat_model": chat_model,
        },
    )
    headers = {"Authorization": f"Bearer {api_user.token}"}
    create_response = client.post("/api/chat/sessions", headers=headers)
    assert create_response.status_code == 200
    conversation_id = create_response.json()["conversation_id"]

    sessions_response = client.get("/api/chat/sessions", headers=headers)
    filters_response = client.get(
        f"/api/chat/conversation/file-filters/{conversation_id}",
        headers=headers,
    )

    assert sessions_response.status_code == 200
    assert isinstance(sessions_response.json(), list)
    assert any(session["conversation_id"] == conversation_id for session in sessions_response.json())
    assert filters_response.status_code == 200
    assert isinstance(filters_response.json(), list)


@pytest.mark.django_db(transaction=True)
def test_chat_history_returns_obsidian_session_shape(client, api_user: KhojApiUser):
    chat_model = ChatModelFactory()
    Agent.objects.update_or_create(
        name="Khoj",
        defaults={
            "slug": "khoj",
            "chat_model": chat_model,
        },
    )
    headers = {"Authorization": f"Bearer {api_user.token}"}
    create_response = client.post("/api/chat/sessions?client=obsidian", headers=headers)
    assert create_response.status_code == 200
    conversation_id = create_response.json()["conversation_id"]
    conversation = Conversation.objects.get(id=conversation_id)
    conversation.title = "Obsidian Resume"
    conversation.conversation_log = {
        "chat": [
            {
                "by": "you",
                "message": "ask from vault",
                "turnId": "turn-1",
                "created": "2026-07-01T00:00:00Z",
            }
        ]
    }
    conversation.save()

    response = client.get(
        f"/api/chat/history?client=obsidian&conversation_id={conversation_id}",
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["response"]["conversation_id"] == conversation_id
    assert data["response"]["slug"] == "Obsidian Resume"
    assert data["response"]["agent"]["slug"] == "khoj"
    assert data["response"]["chat"][0]["by"] == "you"
    assert data["response"]["chat"][0]["message"] == "ask from vault"


@pytest.mark.django_db(transaction=True)
def test_removed_chat_options_endpoint_is_not_registered(client):
    response = client.get("/api/chat/options")

    assert response.status_code == 404


def test_removed_product_routes_are_not_registered(fastapi_app):
    registered_paths = {route.path for route in fastapi_app.routes if hasattr(route, "path")}
    removed_paths = {
        "/login",
        "/auth/token",
        "/api/self",
        "/api/user/name",
        "/api/update",
        "/api/content/size",
        "/api/chat/starters",
        "/api/chat/stats",
        "/api/chat/export",
        "/api/agents",
    }

    assert removed_paths.isdisjoint(registered_paths)


@pytest.mark.django_db(transaction=True)
def test_set_conversation_title_accepts_encoded_special_title(client, api_user: KhojApiUser):
    conversation = Conversation.objects.create(user=api_user.user, title="old")
    headers = {"Authorization": f"Bearer {api_user.token}"}
    title = "A&B #1? ok"

    response = client.patch(
        f"/api/chat/title?conversation_id={conversation.id}&title={quote(title, safe='')}",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    conversation.refresh_from_db()
    assert conversation.title == title


@pytest.mark.django_db(transaction=True)
def test_generate_chat_title_missing_conversation_returns_not_found(client, api_user: KhojApiUser):
    response = client.post(
        "/api/chat/title?conversation_id=00000000-0000-0000-0000-000000000000",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Conversation not found"}


@pytest.mark.django_db(transaction=True)
def test_generate_chat_title_does_not_overwrite_messages_appended_while_waiting(client, api_user, monkeypatch):
    from khoj.routers import api_chat

    conversation = Conversation.objects.create(user=api_user.user, conversation_log={"chat": []})

    async def append_turn_before_returning_title(user, *, conversation):
        await ConversationAdapters.save_conversation(
            user,
            [
                ChatMessageModel(by="you", message="question", turnId="concurrent-turn"),
                ChatMessageModel(by="khoj", message="answer", turnId="concurrent-turn"),
            ],
            conversation_id=str(conversation.id),
        )
        return "Generated title"

    monkeypatch.setattr(api_chat, "acreate_title_from_history", append_turn_before_returning_title)

    response = client.post(
        f"/api/chat/title?conversation_id={conversation.id}",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 200
    conversation.refresh_from_db()
    assert conversation.slug == "Generated title"
    assert [message["message"] for message in conversation.conversation_log["chat"]] == ["question", "answer"]


@pytest.mark.django_db(transaction=True)
def test_set_conversation_title_rejects_invalid_conversation_id(client, api_user: KhojApiUser):
    response = client.patch(
        "/api/chat/title?conversation_id=not-a-uuid&title=hello",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "success": False}


@pytest.mark.django_db(transaction=True)
def test_delete_chat_history_rejects_invalid_conversation_id(client, api_user: KhojApiUser):
    response = client.delete(
        "/api/chat/history?conversation_id=not-a-uuid",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"status": "error", "message": "Conversation not found"}


@pytest.mark.django_db(transaction=True)
def test_delete_missing_chat_history_returns_not_found(client, api_user: KhojApiUser):
    response = client.delete(
        "/api/chat/history?conversation_id=00000000-0000-0000-0000-000000000000",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"status": "error", "message": "Conversation not found"}


@pytest.mark.django_db(transaction=True)
def test_delete_empty_chat_history_id_does_not_clear_all(client, api_user: KhojApiUser):
    conversation = Conversation.objects.create(user=api_user.user)

    response = client.delete(
        "/api/chat/history?conversation_id=",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"status": "error", "message": "Conversation not found"}
    assert Conversation.objects.filter(id=conversation.id).exists()


@pytest.mark.django_db(transaction=True)
def test_delete_missing_message_turn_returns_not_found(client, api_user: KhojApiUser):
    conversation = Conversation.objects.create(
        user=api_user.user,
        conversation_log={"chat": [{"by": "you", "message": "hello", "turnId": "existing-turn"}]},
    )

    response = client.request(
        "DELETE",
        "/api/chat/conversation/message",
        headers={"Authorization": f"Bearer {api_user.token}"},
        json={"conversation_id": str(conversation.id), "turn_id": "missing-turn"},
    )

    assert response.status_code == 404
    assert response.json() == {"status": "error", "message": "Message not found"}


@pytest.mark.django_db(transaction=True)
def test_delete_message_empty_conversation_id_does_not_delete_latest(client, api_user: KhojApiUser):
    conversation = Conversation.objects.create(
        user=api_user.user,
        conversation_log={"chat": [{"by": "you", "message": "keep me", "turnId": "turn-a"}]},
    )

    response = client.request(
        "DELETE",
        "/api/chat/conversation/message",
        headers={"Authorization": f"Bearer {api_user.token}"},
        json={"conversation_id": "", "turn_id": "turn-a"},
    )

    assert response.status_code == 404
    conversation.refresh_from_db()
    assert conversation.conversation_log["chat"] == [{"by": "you", "message": "keep me", "turnId": "turn-a"}]


@pytest.mark.django_db(transaction=True)
def test_delete_message_turn_removes_matching_messages(client, api_user: KhojApiUser):
    conversation = Conversation.objects.create(
        user=api_user.user,
        conversation_log={
            "chat": [
                {"by": "you", "message": "hello", "turnId": "turn-a"},
                {"by": "khoj", "message": "hi", "turnId": "turn-a"},
                {"by": "you", "message": "keep me", "turnId": "turn-b"},
            ]
        },
    )

    response = client.request(
        "DELETE",
        "/api/chat/conversation/message",
        headers={"Authorization": f"Bearer {api_user.token}"},
        json={"conversation_id": str(conversation.id), "turn_id": "turn-a"},
    )

    assert response.status_code == 200
    conversation.refresh_from_db()
    assert conversation.conversation_log["chat"] == [{"by": "you", "message": "keep me", "turnId": "turn-b"}]


@pytest.mark.django_db(transaction=True)
def test_delete_message_turn_cancels_pending_vault_batch(client, api_user: KhojApiUser, tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    turn_id = str(uuid.uuid4())
    conversation = Conversation.objects.create(
        user=api_user.user,
        conversation_log={"chat": [{"by": "you", "message": "write", "turnId": turn_id}]},
    )
    batch = create_vault_action_batch(
        user=api_user.user,
        conversation=conversation,
        turn_id=turn_id,
        actions=[{"op": "create_file", "path": "daily.md", "content": "plan", "mode": "create_only"}],
    )

    response = client.request(
        "DELETE",
        "/api/chat/conversation/message",
        headers={"Authorization": f"Bearer {api_user.token}"},
        json={"conversation_id": str(conversation.id), "turn_id": turn_id},
    )

    batch.refresh_from_db()
    assert response.status_code == 200
    assert batch.status == VaultActionBatch.Status.CANCELLED
    assert batch.result["reason"] == "conversation_turn_deleted"


@pytest.mark.django_db(transaction=True)
def test_delete_invalid_content_type_returns_bad_request(client):
    headers = {"Authorization": "Bearer kk-secret"}

    response = client.delete("/api/content/type/not-a-type", headers=headers)

    assert response.status_code == 400
    assert response.json() == {"detail": "Unsupported content type: not-a-type"}


@pytest.mark.django_db(transaction=True)
def test_delete_invalid_content_source_returns_bad_request(client):
    headers = {"Authorization": "Bearer kk-secret"}

    response = client.delete("/api/content/source/not-a-source", headers=headers)

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid content source: not-a-source"}


@pytest.mark.django_db(transaction=True)
def test_next_export_text_files_are_served(client, tmp_path, monkeypatch):
    (tmp_path / "index.txt").write_text("root rsc", encoding="utf-8")
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    (settings_dir / "index.txt").write_text("settings rsc", encoding="utf-8")
    monkeypatch.setattr(constants, "next_js_directory", tmp_path)

    assert client.get("/index.txt").text == "root rsc"
    assert client.get("/settings.txt").text == "settings rsc"
    assert client.get("/../secret.txt").status_code == 404


def test_automations_page_uses_default_user(client):
    state.anonymous_mode = False

    response = client.get("/automations", follow_redirects=False)

    assert response.status_code == 200


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_get_configured_types_with_no_content_config(fastapi_app: FastAPI):
    # Arrange
    state.anonymous_mode = True
    configure_routes(fastapi_app)
    client = TestClient(fastapi_app)

    # Act
    response = client.get("/api/content/types")

    # Assert
    assert response.status_code == 200
    assert response.json() == ["all"]


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_notes_search(client, tmp_path, monkeypatch):
    # Arrange
    headers = {"Authorization": "Bearer kk-secret"}
    (tmp_path / "install.md").write_text("git clone https://github.com/khoj-ai/khoj", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    user_query = quote("How to git install application?")

    # Act
    response = client.get(
        f"/api/search?q={user_query}&n=1&t=markdown&r=true&max_distance={BGE_TEST_MAX_DISTANCE}", headers=headers
    )

    # Assert
    assert response.status_code == 200

    assert len(response.json()) == 1, "Expected only 1 result"
    search_result = response.json()[0]["entry"]
    assert "git clone https://github.com/khoj-ai/khoj" in search_result, "Expected 'git clone' in search result"


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_notes_search_no_results(client, tmp_path, monkeypatch):
    # Arrange
    headers = {"Authorization": "Bearer kk-secret"}
    (tmp_path / "install.md").write_text("git clone https://github.com/khoj-ai/khoj", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    user_query = quote("How to find my goat?")

    # Act
    response = client.get(
        f"/api/search?q={user_query}&n=1&t=markdown&r=true&max_distance={BGE_TEST_MAX_DISTANCE}", headers=headers
    )

    # Assert
    assert response.status_code == 200
    assert response.json() == [], "Expected no results"


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_notes_search_with_only_filters(client, tmp_path, monkeypatch):
    # Arrange
    headers = {"Authorization": "Bearer kk-secret"}
    (tmp_path / "emacs.md").write_text("Emacs load path", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    user_query = quote("Emacs")

    # Act
    response = client.get(f"/api/search?q={user_query}&n=1&t=markdown", headers=headers)

    # Assert
    assert response.status_code == 200
    # assert actual_data contains word "Emacs"
    search_result = response.json()[0]["entry"]
    assert "Emacs" in search_result


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_notes_search_with_include_filter(client, tmp_path, monkeypatch):
    # Arrange
    headers = {"Authorization": "Bearer kk-secret"}
    (tmp_path / "emacs.md").write_text("emacs install notes", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    user_query = quote("emacs")

    # Act
    response = client.get(f"/api/search?q={user_query}&n=1&t=markdown", headers=headers)

    # Assert
    assert response.status_code == 200
    # assert actual_data contains word "Emacs"
    search_result = response.json()[0]["entry"]
    assert "emacs" in search_result


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_notes_search_with_exclude_filter(client, tmp_path, monkeypatch):
    # Arrange
    headers = {"Authorization": "Bearer kk-secret"}
    (tmp_path / "emacs.md").write_text("emacs install notes", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    user_query = quote("emacs")

    # Act
    response = client.get(f"/api/search?q={user_query}&n=1&t=markdown", headers=headers)

    # Assert
    assert response.status_code == 200
    # assert actual_data does not contains word "clone"
    search_result = response.json()[0]["entry"]
    assert "clone" not in search_result


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_notes_search_requires_parent_context(client, tmp_path, monkeypatch):
    # Arrange
    headers = {"Authorization": "Bearer kk-secret"}
    (tmp_path / "emacs.md").write_text("Emacs load path", encoding="utf-8")
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    user_query = quote("Install Khoj on Emacs")

    # Act
    response = client.get(
        f"/api/search?q={user_query}&n=1&t=markdown&r=true&max_distance={BGE_TEST_MAX_DISTANCE}", headers=headers
    )

    # Assert
    assert response.status_code == 200

    assert len(response.json()) == 1, "Expected only 1 result"
    search_result = response.json()[0]["entry"]
    assert "Emacs load path" in search_result, "Expected 'Emacs load path' in search result"


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_different_user_data_not_accessed(client, sample_markdown_data, default_user: KhojUser):
    # Arrange
    headers = {"Authorization": "Bearer kk-token"}  # Token for default_user2
    text_search.setup(MarkdownToEntries, sample_markdown_data, regenerate=False, user=default_user)
    user_query = quote("How to git install application?")

    # Act
    response = client.get(f"/api/search?q={user_query}&n=1&t=markdown", headers=headers)

    # Assert
    assert response.status_code == 403
    # assert actual response has no data as the default_user is different from the user making the query (anonymous)
    assert len(response.json()) == 1 and response.json()["detail"] == "Forbidden"


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_user_no_data_returns_empty(client, sample_markdown_data, api_user3: KhojApiUser):
    # Arrange
    token = api_user3.token
    headers = {"Authorization": "Bearer " + token}
    user_query = quote("How to git install application?")

    # Act
    response = client.get(f"/api/search?q={user_query}&n=1&t=markdown", headers=headers)

    # Assert
    assert response.status_code == 200
    # assert actual response has no data as the default_user3, though other users have data
    assert len(response.json()) == 0
    assert response.json() == []


@pytest.mark.django_db(transaction=True)
def test_chat_invalid_conversation_id_returns_not_found(chat_client_no_background):
    response = chat_client_no_background.post(
        "/api/chat",
        json={"q": "hello", "conversation_id": "not-a-uuid", "stream": False},
    )

    assert response.status_code == 404
    assert "Conversation not-a-uuid not found" in response.json()["response"]


@pytest.mark.django_db(transaction=True)
def test_chat_empty_conversation_id_returns_not_found(chat_client_no_background):
    response = chat_client_no_background.post(
        "/api/chat",
        json={"q": "hello", "conversation_id": "", "stream": False},
    )

    assert response.status_code == 404
    assert "Conversation  not found" in response.json()["response"]


@pytest.mark.django_db(transaction=True)
def test_streaming_chat_invalid_conversation_id_returns_not_found(chat_client_no_background):
    response = chat_client_no_background.post(
        "/api/chat",
        json={"q": "hello", "conversation_id": "not-a-uuid", "stream": True},
    )

    assert response.status_code == 404
    assert "Conversation not-a-uuid not found" in response.json()["response"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("method", "url", "payload"),
    [
        ("post", "/api/chat/conversation/file-filters", {"filename": "missing.md"}),
        ("delete", "/api/chat/conversation/file-filters", {"filename": "missing.md"}),
        ("post", "/api/chat/conversation/file-filters/bulk", {"filenames": ["missing.md"]}),
        ("delete", "/api/chat/conversation/file-filters/bulk", {"filenames": ["missing.md"]}),
    ],
)
def test_file_filter_updates_missing_conversation_return_not_found(client, api_user: KhojApiUser, method, url, payload):
    payload = {**payload, "conversation_id": "00000000-0000-0000-0000-000000000000"}
    response = client.request(method, url, json=payload, headers={"Authorization": f"Bearer {api_user.token}"})

    assert response.status_code == 404
    assert response.json() == {"status": "error", "message": "Conversation not found"}


def test_chat_event_structured_streaming_predicate():
    from khoj.routers import api_chat

    assert not api_chat._should_emit_structured_event(api_chat.ChatEvent.STATUS, stream=False)
    assert api_chat._should_emit_structured_event(api_chat.ChatEvent.USAGE, stream=False)
    assert api_chat._should_emit_structured_event(api_chat.ChatEvent.STATUS, stream=True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_websocket_chat_forwards_enveloped_events(monkeypatch):
    import asyncio
    import json

    from khoj.routers import api_chat
    from khoj.utils.rawconfig import ChatRequestBody

    async def fake_run_conversation_turn(*args, **kwargs):
        yield json.dumps({"type": api_chat.ChatEvent.THOUGHT.value, "data": "planning"})
        yield api_chat.ChatEvent.END_EVENT.value
        yield json.dumps({"type": api_chat.ChatEvent.MESSAGE.value, "data": '{"type":"invoice"}'})
        yield api_chat.ChatEvent.END_EVENT.value

    class FakeUser:
        id = 1

    class FakeScopeUser:
        object = FakeUser()

    class FakeWebSocket:
        scope = {"user": FakeScopeUser()}
        headers = {}

        def __init__(self):
            self.sent = []

        async def send_text(self, text):
            self.sent.append(text)

    monkeypatch.setattr(api_chat, "run_conversation_turn", fake_run_conversation_turn)
    websocket = FakeWebSocket()

    await api_chat.process_chat_request(
        websocket,
        ChatRequestBody(q="hello", stream=True),
        common=None,
        interrupt_queue=asyncio.Queue(),
    )

    assert websocket.sent == [
        json.dumps({"type": "thought", "data": "planning"}),
        api_chat.ChatEvent.END_EVENT.value,
        json.dumps({"type": "message", "data": '{"type":"invoice"}'}),
        api_chat.ChatEvent.END_EVENT.value,
    ]


@pytest.mark.asyncio
async def test_interrupt_chat_task_waits_for_graceful_persistence():
    import asyncio

    from khoj.routers import api_chat

    interrupt_queue = asyncio.Queue()
    persisted = asyncio.Event()

    async def active_turn():
        signal, acknowledged = await interrupt_queue.get()
        assert signal == api_chat.ChatEvent.INTERRUPT.value
        await asyncio.sleep(0)
        persisted.set()
        acknowledged.set()

    task = asyncio.create_task(active_turn())
    await api_chat._interrupt_chat_task(task, interrupt_queue)

    assert persisted.is_set()
    assert task.done()


@pytest.mark.asyncio
async def test_interrupt_chat_task_cancels_after_bounded_grace(monkeypatch):
    import asyncio

    from khoj.routers import api_chat

    interrupt_queue = asyncio.Queue()
    cleaned_up = asyncio.Event()

    async def stuck_turn():
        await interrupt_queue.get()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    monkeypatch.setattr(api_chat, "WEBSOCKET_INTERRUPT_GRACE_SECONDS", 0.01)
    task = asyncio.create_task(stuck_turn())

    await api_chat._interrupt_chat_task(task, interrupt_queue)

    assert task.cancelled()
    assert cleaned_up.is_set()


@pytest.mark.asyncio
async def test_interrupt_chat_task_replaces_a_full_queue_without_blocking(monkeypatch):
    import asyncio

    from khoj.routers import api_chat

    interrupt_queue = asyncio.Queue(maxsize=1)
    interrupt_queue.put_nowait("stale instruction")
    cleaned_up = asyncio.Event()

    async def stuck_turn():
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    monkeypatch.setattr(api_chat, "WEBSOCKET_INTERRUPT_GRACE_SECONDS", 0.01)
    task = asyncio.create_task(stuck_turn())

    await asyncio.wait_for(api_chat._interrupt_chat_task(task, interrupt_queue), timeout=0.1)

    assert task.cancelled()
    assert cleaned_up.is_set()


def test_continuation_interrupt_rejects_a_full_queue_without_waiting():
    import asyncio

    from khoj.routers import api_chat

    interrupt_queue = asyncio.Queue(maxsize=1)
    interrupt_queue.put_nowait("existing instruction")

    assert api_chat._enqueue_interrupt_signal(interrupt_queue, "new instruction") is False
    assert interrupt_queue.get_nowait() == "existing instruction"


@pytest.mark.asyncio
async def test_http_disconnect_waiter_reaps_children_when_cancelled():
    import asyncio

    from khoj.routers import api_chat

    receive_started = asyncio.Event()
    receive_cleaned = asyncio.Event()

    class WaitingRequest:
        async def receive(self):
            receive_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                receive_cleaned.set()

    waiter = asyncio.create_task(api_chat._wait_for_http_disconnect(WaitingRequest(), asyncio.Event()))
    await receive_started.wait()
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert receive_cleaned.is_set()


@pytest.mark.asyncio
async def test_monitor_shutdown_allows_finalizer_to_finish():
    import asyncio

    from khoj.routers import api_chat

    shutdown = asyncio.Event()
    finalized = asyncio.Event()

    async def monitor():
        try:
            await shutdown.wait()
        finally:
            await asyncio.sleep(0)
            finalized.set()

    monitor_task = asyncio.create_task(monitor())
    await api_chat._shutdown_monitor_task(monitor_task, shutdown)

    assert finalized.is_set()
    assert monitor_task.done() and not monitor_task.cancelled()


@pytest.mark.asyncio
async def test_conversation_turn_wrapper_cleans_monitor_after_failure(monkeypatch):
    import asyncio

    from khoj.routers import api_chat
    from khoj.utils.rawconfig import ChatRequestBody

    monitor_cleaned = asyncio.Event()

    async def failing_turn(*args, shutdown_event, monitor_tasks, **kwargs):
        async def monitor():
            await shutdown_event.wait()
            monitor_cleaned.set()

        monitor_tasks.append(asyncio.create_task(monitor()))
        yield "event"
        raise RuntimeError("turn failed")

    monkeypatch.setattr(api_chat, "_run_conversation_turn_impl", failing_turn)
    iterator = api_chat.run_conversation_turn(
        ChatRequestBody(q="hello", stream=True),
        object(),
        object(),
        object(),
        object(),
    )

    with pytest.raises(RuntimeError, match="turn failed"):
        _ = [event async for event in iterator]

    assert monitor_cleaned.is_set()


def test_message_processor_requires_envelopes_and_preserves_json_text():
    from khoj.routers.helpers import MessageProcessor

    processor = MessageProcessor()
    with pytest.raises(ValueError, match="stream event"):
        processor.convert_message_chunk_to_json("plain text")

    processor.process_message_chunk('{"type":"message","data":"{\\"type\\":\\"invoice\\"}"}')

    assert processor.raw_response == '{"type":"invoice"}'


@pytest.mark.django_db(transaction=True)
def test_update_chat_model_rejects_invalid_id(chat_client_with_auth, api_user2: KhojApiUser, monkeypatch):
    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "openai")

    response = chat_client_with_auth.post(
        "/api/model/chat?id=not-a-number",
        headers={"Authorization": f"Bearer {api_user2.token}"},
    )

    assert response.status_code == 400
    assert response.json() == {"status": "error", "message": "Invalid chat model id"}


@pytest.mark.django_db(transaction=True)
def test_get_chat_model_handles_missing_config(client, api_user: KhojApiUser, monkeypatch):
    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "openai")

    response = client.get("/api/model/chat", headers={"Authorization": f"Bearer {api_user.token}"})

    assert response.status_code == 404
    assert response.json() == {"status": "error", "message": "Chat model not found"}


@pytest.mark.django_db(transaction=True)
def test_update_chat_model_accepts_free_model(client, api_user: KhojApiUser, monkeypatch):
    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "openai")
    chat_model = ChatModelFactory(friendly_name="Settings Free Model")
    headers = {"Authorization": f"Bearer {api_user.token}"}

    response = client.post(f"/api/model/chat?id={chat_model.id}", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    response = client.get("/api/model/chat", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"id": chat_model.id, "chat_model": "Settings Free Model"}


@pytest.mark.django_db(transaction=True)
def test_update_chat_model_reports_adapter_save_failure(client, api_user: KhojApiUser, monkeypatch):
    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "openai")
    chat_model = ChatModelFactory(friendly_name="Unsaved Free Model")

    async def fail_to_save_user_model(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "khoj.routers.api_model.ConversationAdapters.aset_user_conversation_processor",
        fail_to_save_user_model,
    )

    response = client.post(
        f"/api/model/chat?id={chat_model.id}",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"status": "error", "message": "Model not found"}


@pytest.mark.django_db(transaction=True)
def test_update_chat_fast_mode_requires_codex_runtime(client, api_user: KhojApiUser, monkeypatch):
    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "openai")

    response = client.post(
        "/api/model/chat/fast?enabled=true",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 400
    assert response.json() == {"status": "error", "message": "Fast mode requires Codex"}


@pytest.mark.skipif(os.getenv("OPENAI_API_KEY") is None, reason="requires OPENAI_API_KEY")
@pytest.mark.django_db(transaction=True)
def test_chat_with_unauthenticated_user(chat_client_with_auth, api_user2: KhojApiUser):
    # Arrange
    query = "Hello!"
    headers = {"Authorization": f"Bearer {api_user2.token}"}

    # Act
    auth_response = chat_client_with_auth.post("/api/chat", json={"q": query}, headers=headers)
    no_auth_response = chat_client_with_auth.post("/api/chat", json={"q": query})

    # Assert
    assert auth_response.status_code == 200
    assert no_auth_response.status_code == 200


def get_sample_files_data():
    return [
        ("files", ("path/to/filename.markdown", "* practicing piano", "text/markdown")),
        ("files", ("path/to/filename1.markdown", "** top 3 reasons why I moved to SF", "text/markdown")),
        ("files", ("path/to/filename2.markdown", "* how to build a search engine", "text/markdown")),
        ("files", ("path/to/filename.pdf", "Moore's law does not apply to consumer hardware", "application/pdf")),
        ("files", ("path/to/filename1.pdf", "The sun is a ball of helium", "application/pdf")),
        ("files", ("path/to/filename2.pdf", "Effect of sunshine on baseline human happiness", "application/pdf")),
        ("files", ("path/to/filename.txt", "data,column,value", "text/plain")),
        ("files", ("path/to/filename1.txt", "<html>my first web page</html>", "text/plain")),
        ("files", ("path/to/filename2.txt", "2021-02-02 Journal Entry", "text/plain")),
        ("files", ("path/to/filename.md", "# Notes from client call", "text/markdown")),
        (
            "files",
            ("path/to/filename1.md", "## Studying anthropological records from the Fatimid caliphate", "text/markdown"),
        ),
        ("files", ("path/to/filename2.md", "**Understanding science through the lens of art**", "text/markdown")),
    ]


def get_big_size_sample_files_data():
    # a string of approximately 100 MB
    big_text = "a" * (100 * 1024 * 1024)
    return [
        (
            "files",
            ("path/to/filename.markdown", big_text, "text/markdown"),
        )
    ]


def get_medium_size_sample_files_data():
    big_text = "a" * (50 * 1024 * 1024)  # a string of approximately 50 MB
    return [
        (
            "files",
            ("path/to/filename.markdown", big_text, "text/markdown"),
        )
    ]
