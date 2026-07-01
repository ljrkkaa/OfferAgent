# Standard Modules
import os
import re
from urllib.parse import quote, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from khoj.configure import configure_routes
from khoj.database.adapters import EntryAdapters, FileObjectAdapters
from khoj.database.models import Agent, Conversation, KhojApiUser, KhojUser
from khoj.processor.content.org_mode.org_to_entries import OrgToEntries
from khoj.search_type import text_search
from khoj.utils import constants, state
from tests.helpers import ChatModelFactory

BGE_TEST_MAX_DISTANCE = 0.36


# Test
# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_search_with_no_auth_key(client):
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

    response = client.get("/api/search", params={"q": query, "t": "org"}, headers=headers)

    assert response.status_code == 200
    assert response.json() == []


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_search_with_valid_content_type(client):
    headers = {"Authorization": "Bearer kk-secret"}
    for content_type in ["all", "org", "markdown", "image", "pdf", "github", "notion", "plaintext", "image", "docx"]:
        # Act
        response = client.get(f"/api/search?q=random&t={content_type}", headers=headers)
        # Assert
        assert response.status_code == 200, f"Returned status: {response.status_code} for content type: {content_type}"


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_index_update_with_no_auth_key(client):
    # Arrange
    files = get_sample_files_data()

    # Act
    response = client.patch("/api/content", files=files)

    # Assert
    assert response.status_code == 403


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
def test_update_with_invalid_content_type(client):
    # Arrange
    headers = {"Authorization": "Bearer kk-secret"}

    # Act
    response = client.get("/api/update?t=invalid_content_type", headers=headers)

    # Assert
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
def test_index_update_with_invalid_content_type(client):
    headers = {"Authorization": "Bearer kk-secret"}
    files = get_sample_files_data()

    response = client.patch("/api/content?t=not-a-type", files=files, headers=headers)

    assert response.status_code == 422


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_regenerate_with_invalid_content_type(client):
    # Arrange
    headers = {"Authorization": "Bearer kk-secret"}

    # Act
    response = client.get("/api/update?force=true&t=invalid_content_type", headers=headers)

    # Assert
    assert response.status_code == 422


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_index_update_big_files(client):
    # Arrange
    state.billing_enabled = True
    files = get_big_size_sample_files_data()

    # Credential for the default_user, who is subscribed
    headers = {"Authorization": "Bearer kk-secret"}

    # Act
    response = client.patch("/api/content", files=files, headers=headers)

    # Assert
    assert response.status_code == 429


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_index_update_medium_file_unsubscribed(client, api_user4: KhojApiUser):
    # Arrange
    api_token = api_user4.token
    state.billing_enabled = True
    files = get_medium_size_sample_files_data()
    headers = {"Authorization": f"Bearer {api_token}"}

    # Act
    response = client.patch("/api/content", files=files, headers=headers)

    # Assert
    assert response.status_code == 429


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_index_update_normal_file_unsubscribed(client, api_user4: KhojApiUser):
    # Arrange
    api_token = api_user4.token
    state.billing_enabled = True
    files = get_sample_files_data()
    headers = {"Authorization": f"Bearer {api_token}"}

    # Act
    response = client.patch("/api/content", files=files, headers=headers)

    # Assert
    assert response.status_code == 200


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_index_update_big_files_no_billing(client):
    # Arrange
    state.billing_enabled = False
    files = get_big_size_sample_files_data()
    headers = {"Authorization": "Bearer kk-secret"}

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
    state.billing_enabled = True
    files = [("files", (f"path/to/filename{i}.org", f"Symphony No {i}", "text/org")) for i in range(1001)]

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
    for content_type in ["all", "org", "markdown", "image", "pdf", "notion"]:
        # Arrange
        files = get_sample_files_data()
        headers = {"Authorization": "Bearer kk-secret"}

        # Act
        response = client.patch(f"/api/content?t={content_type}", files=files, headers=headers)

        # Assert
        assert response.status_code == 200, f"Returned status: {response.status_code} for content type: {content_type}"


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_regenerate_with_github_fails_without_pat(client):
    # Act
    headers = {"Authorization": "Bearer kk-secret"}
    response = client.get("/api/update?force=true&t=github", headers=headers)

    # Arrange
    files = get_sample_files_data()

    # Act
    response = client.patch("/api/content?t=github", files=files, headers=headers)

    # Assert
    assert response.status_code == 200, f"Returned status: {response.status_code} for content type: github"


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_get_configured_types_via_api(client, sample_org_data, default_user3: KhojUser):
    # Act
    text_search.setup(OrgToEntries, sample_org_data, regenerate=False, user=default_user3)

    enabled_types = EntryAdapters.get_unique_file_types(user=default_user3).all().values_list("file_type", flat=True)

    # Assert
    assert list(enabled_types) == ["org"]


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_get_api_config_types(client, sample_org_data, default_user: KhojUser):
    # Arrange
    headers = {"Authorization": "Bearer kk-secret"}
    text_search.setup(OrgToEntries, sample_org_data, regenerate=False, user=default_user)

    # Act
    response = client.get("/api/content/types", headers=headers)

    # Assert
    assert response.status_code == 200
    assert set(response.json()) == {"all", "org", "plaintext"}


@pytest.mark.django_db(transaction=True)
def test_get_content_source_files_for_search_page(client, sample_org_data, default_user: KhojUser):
    headers = {"Authorization": "Bearer kk-secret"}
    text_search.setup(OrgToEntries, sample_org_data, regenerate=False, user=default_user)

    response = client.get("/api/content/computer", headers=headers)

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert any(file_name.endswith(".org") for file_name in response.json())


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
def test_create_chat_session_accepts_agent_slug_in_json_body(client, api_user: KhojApiUser):
    chat_model = ChatModelFactory()
    Agent.objects.update_or_create(
        name="Khoj",
        defaults={
            "slug": "khoj",
            "chat_model": chat_model,
            "privacy_level": Agent.PrivacyLevel.PUBLIC,
            "managed_by_admin": True,
        },
    )
    agent = Agent.objects.create(
        name="Obsidian Body Agent",
        slug="obsidian-body-agent",
        creator=api_user.user,
        chat_model=chat_model,
        privacy_level=Agent.PrivacyLevel.PRIVATE,
    )
    headers = {"Authorization": f"Bearer {api_user.token}"}

    response = client.post("/api/chat/sessions", headers=headers, json={"agent_slug": agent.slug})

    assert response.status_code == 200
    conversation = Conversation.objects.get(id=response.json()["conversation_id"])
    assert conversation.agent_id == agent.id

    response = client.post(f"/api/chat/sessions?agent_slug={agent.slug}", headers=headers)

    assert response.status_code == 200
    conversation = Conversation.objects.get(id=response.json()["conversation_id"])
    assert conversation.agent_id == agent.id

    response = client.post("/api/chat/sessions", headers=headers)

    assert response.status_code == 200
    conversation = Conversation.objects.get(id=response.json()["conversation_id"])
    assert conversation.agent.slug == "khoj"


@pytest.mark.django_db(transaction=True)
def test_sidebar_chat_session_endpoints_return_lists(client, api_user: KhojApiUser):
    chat_model = ChatModelFactory()
    Agent.objects.update_or_create(
        name="Khoj",
        defaults={
            "slug": "khoj",
            "chat_model": chat_model,
            "privacy_level": Agent.PrivacyLevel.PUBLIC,
            "managed_by_admin": True,
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
            "privacy_level": Agent.PrivacyLevel.PUBLIC,
            "managed_by_admin": True,
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
                "message": "/notes ask from vault",
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
    assert data["response"]["chat"][0]["message"] == "/notes ask from vault"


@pytest.mark.django_db(transaction=True)
def test_delete_self_removes_current_user_data_and_revokes_token(
    client,
    api_user3: KhojApiUser,
    api_user2: KhojApiUser,
):
    chat_model = ChatModelFactory()
    agent = Agent.objects.create(
        name="Delete Me Agent",
        creator=api_user3.user,
        chat_model=chat_model,
        privacy_level=Agent.PrivacyLevel.PRIVATE,
    )
    Conversation.objects.create(user=api_user3.user, title="delete me", agent=agent)

    response = client.delete("/api/self", headers={"Authorization": f"Bearer {api_user3.token}"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert not KhojUser.objects.filter(id=api_user3.user_id).exists()
    assert not KhojApiUser.objects.filter(token=api_user3.token).exists()
    assert not Conversation.objects.filter(user_id=api_user3.user_id).exists()
    assert not Agent.objects.filter(id=agent.id).exists()
    assert KhojUser.objects.filter(id=api_user2.user_id).exists()

    revoked_response = client.get("/api/search?q=hello", headers={"Authorization": f"Bearer {api_user3.token}"})
    assert revoked_response.status_code == 403


def test_chat_options_endpoint_returns_command_map(client):
    response = client.get("/api/chat/options")

    assert response.status_code == 200
    assert isinstance(response.json(), dict)


def test_oauth_metadata_returns_google_provider(client):
    response = client.get("/auth/oauth/metadata")

    assert response.status_code == 200
    assert set(response.json()["google"]) == {"client_id", "redirect_uri"}


@pytest.mark.django_db(transaction=True)
def test_magic_link_auth_flow_sends_code_and_redirects(client, monkeypatch):
    sent = {}

    async def fake_send_magic_link_email(email, unique_id, base_url):
        sent.update({"email": email, "unique_id": unique_id, "base_url": base_url})

    monkeypatch.setattr("khoj.routers.auth.state.billing_enabled", False)
    monkeypatch.setattr("khoj.routers.auth.send_magic_link_email", fake_send_magic_link_email)

    response = client.post("/auth/magic", json={"email": "login-flow@example.com"})

    assert response.status_code == 200
    user = KhojUser.objects.get(email="login-flow@example.com")
    assert sent["email"] == user.email
    assert sent["unique_id"] == user.email_verification_code

    response = client.get(
        f"/auth/magic?code={user.email_verification_code}&email={quote(user.email, safe='')}",
        follow_redirects=False,
    )

    assert response.status_code in {302, 307}
    assert response.headers["location"] == "/"


@pytest.mark.django_db(transaction=True)
def test_magic_link_auth_rejects_invalid_code(client, default_user: KhojUser):
    response = client.get(
        f"/auth/magic?code=000000&email={quote(default_user.email, safe='')}",
        follow_redirects=False,
    )

    assert response.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_api_token_generate_list_delete_flow(client, api_user: KhojApiUser):
    headers = {"Authorization": f"Bearer {api_user.token}"}

    create_response = client.post("/auth/token", headers=headers)

    assert create_response.status_code == 200
    created_token = create_response.json()
    assert isinstance(created_token["token"], str)
    assert isinstance(created_token["name"], str)

    list_response = client.get("/auth/token", headers=headers)
    assert list_response.status_code == 200
    assert any(token["token"] == created_token["token"] for token in list_response.json())

    delete_response = client.delete(
        f"/auth/token?token={quote(created_token['token'], safe='')}",
        headers=headers,
    )

    assert delete_response.status_code == 200
    list_response = client.get("/auth/token", headers=headers)
    assert created_token["token"] not in [token["token"] for token in list_response.json()]


@pytest.mark.django_db(transaction=True)
def test_agent_generated_slug_is_url_safe(client, api_user: KhojApiUser):
    chat_model = ChatModelFactory()
    agent = Agent.objects.create(
        name="R&D / 面试 #1?",
        creator=api_user.user,
        chat_model=chat_model,
        privacy_level=Agent.PrivacyLevel.PRIVATE,
    )
    headers = {"Authorization": f"Bearer {api_user.token}"}

    assert re.fullmatch(r"[a-z0-9-]+", agent.slug)
    response = client.get(f"/api/agents/{agent.slug}", headers=headers)

    assert response.status_code == 200
    assert response.json()["slug"] == agent.slug


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
def test_chat_feedback_sends_authenticated_user_feedback(client, api_user: KhojApiUser, monkeypatch):
    sent_feedback = {}

    async def fake_send_query_feedback(uquery, kquery, sentiment, user_email):
        sent_feedback.update(
            {
                "uquery": uquery,
                "kquery": kquery,
                "sentiment": sentiment,
                "user_email": user_email,
            }
        )

    monkeypatch.setattr("khoj.routers.api_chat.send_query_feedback", fake_send_query_feedback)

    response = client.post(
        "/api/chat/feedback",
        headers={"Authorization": f"Bearer {api_user.token}"},
        json={
            "uquery": "What is RAG?",
            "kquery": "RAG means retrieval augmented generation.",
            "sentiment": "positive",
        },
    )

    assert response.status_code == 200
    assert sent_feedback == {
        "uquery": "What is RAG?",
        "kquery": "RAG means retrieval augmented generation.",
        "sentiment": "positive",
        "user_email": api_user.user.email,
    }


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
def test_set_user_name_accepts_encoded_special_characters(client, api_user: KhojApiUser):
    name = "A&B"

    response = client.patch(
        f"/api/user/name?name={quote(name, safe='')}",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 200
    api_user.user.refresh_from_db()
    assert api_user.user.first_name == name


def test_next_export_text_files_are_served(client, tmp_path, monkeypatch):
    (tmp_path / "index.txt").write_text("root rsc", encoding="utf-8")
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    (settings_dir / "index.txt").write_text("settings rsc", encoding="utf-8")
    monkeypatch.setattr(constants, "next_js_directory", tmp_path)

    assert client.get("/index.txt").text == "root rsc"
    assert client.get("/settings.txt").text == "settings rsc"
    assert client.get("/../secret.txt").status_code == 404


def test_home_static_directory_returns_not_found(client, tmp_path, monkeypatch):
    (tmp_path / "logo.svg").write_text("<svg />", encoding="utf-8")
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    monkeypatch.setattr(constants, "home_directory", tmp_path)

    assert client.get("/home/logo.svg").text == "<svg />"
    response = client.get("/home/assets")

    assert response.status_code == 404
    assert client.get("/home/missing.css").status_code == 404


def test_ip_location_uses_forwarded_public_ip(client, monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return (
                b'{"city":"San Francisco","region":"California","country":"US",'
                b'"country_code":"US","timezone":"America/Los_Angeles"}'
            )

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("khoj.routers.api.urlopen", fake_urlopen)

    response = client.get("/api/ip", headers={"X-Forwarded-For": "8.8.8.8"})

    assert captured == {"url": "https://ipapi.co/8.8.8.8/json", "timeout": 3}
    assert response.json() == {
        "city": "San Francisco",
        "region": "California",
        "country": "US",
        "countryCode": "US",
        "timezone": "America/Los_Angeles",
    }


def test_ip_location_skips_private_ip(client, monkeypatch):
    def fail_urlopen(*args, **kwargs):
        raise AssertionError("private IP should not call ipapi")

    monkeypatch.setattr("khoj.routers.api.urlopen", fail_urlopen)

    response = client.get("/api/ip", headers={"X-Forwarded-For": "127.0.0.1"})

    assert response.status_code == 200
    assert response.json() == {}


def test_automations_page_requires_auth(client):
    state.anonymous_mode = False

    response = client.get("/automations", follow_redirects=False)

    assert response.status_code == 303
    assert urlparse(response.headers["location"]).path == "/login"


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
        f"/api/search?q={user_query}&n=1&t=org&r=true&max_distance={BGE_TEST_MAX_DISTANCE}", headers=headers
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
        f"/api/search?q={user_query}&n=1&t=org&r=true&max_distance={BGE_TEST_MAX_DISTANCE}", headers=headers
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
    response = client.get(f"/api/search?q={user_query}&n=1&t=org", headers=headers)

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
    response = client.get(f"/api/search?q={user_query}&n=1&t=org", headers=headers)

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
    response = client.get(f"/api/search?q={user_query}&n=1&t=org", headers=headers)

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
        f"/api/search?q={user_query}&n=1&t=org&r=true&max_distance={BGE_TEST_MAX_DISTANCE}", headers=headers
    )

    # Assert
    assert response.status_code == 200

    assert len(response.json()) == 1, "Expected only 1 result"
    search_result = response.json()[0]["entry"]
    assert "Emacs load path" in search_result, "Expected 'Emacs load path' in search result"


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_different_user_data_not_accessed(client, sample_org_data, default_user: KhojUser):
    # Arrange
    headers = {"Authorization": "Bearer kk-token"}  # Token for default_user2
    text_search.setup(OrgToEntries, sample_org_data, regenerate=False, user=default_user)
    user_query = quote("How to git install application?")

    # Act
    response = client.get(f"/api/search?q={user_query}&n=1&t=org", headers=headers)

    # Assert
    assert response.status_code == 403
    # assert actual response has no data as the default_user is different from the user making the query (anonymous)
    assert len(response.json()) == 1 and response.json()["detail"] == "Forbidden"


# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_user_no_data_returns_empty(client, sample_org_data, api_user3: KhojApiUser):
    # Arrange
    token = api_user3.token
    headers = {"Authorization": "Bearer " + token}
    user_query = quote("How to git install application?")

    # Act
    response = client.get(f"/api/search?q={user_query}&n=1&t=org", headers=headers)

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
def test_chat_export_pages_do_not_overlap(client, api_user: KhojApiUser):
    Conversation.objects.filter(user=api_user.user).delete()
    for index in range(12):
        Conversation.objects.create(user=api_user.user, title=f"export-{index:02d}")
    headers = {"Authorization": f"Bearer {api_user.token}"}

    stats = client.get("/api/chat/stats", headers=headers).json()
    first_page = client.get("/api/chat/export?page=0", headers=headers).json()
    second_page = client.get("/api/chat/export?page=1", headers=headers).json()

    first_titles = {conversation["title"] for conversation in first_page}
    second_titles = {conversation["title"] for conversation in second_page}
    assert stats == {"num_conversations": 12}
    assert len(first_page) == 10
    assert len(second_page) == 2
    assert not first_titles & second_titles
    assert first_titles | second_titles == {f"export-{index:02d}" for index in range(12)}


@pytest.mark.django_db(transaction=True)
def test_share_missing_conversation_returns_not_found(client, api_user: KhojApiUser):
    response = client.post(
        "/api/chat/share?conversation_id=00000000-0000-0000-0000-000000000000",
        headers={"Authorization": f"Bearer {api_user.token}", "host": "localhost"},
    )

    assert response.status_code == 404
    assert response.json() == {"status": "error", "message": "Conversation not found"}


@pytest.mark.django_db(transaction=True)
def test_fork_missing_public_conversation_returns_not_found(client, api_user: KhojApiUser):
    response = client.post(
        "/api/chat/share/fork?public_conversation_slug=missing",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"status": "error", "message": "Conversation not found"}


@pytest.mark.django_db(transaction=True)
def test_delete_missing_public_conversation_returns_not_found(client, api_user: KhojApiUser):
    response = client.delete(
        "/api/chat/share?public_conversation_slug=missing",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"status": "error", "message": "Conversation not found"}


@pytest.mark.asyncio
async def test_websocket_chat_flushes_debounced_thought(monkeypatch):
    import asyncio
    import json

    from khoj.routers import api_chat
    from khoj.utils.rawconfig import ChatRequestBody

    async def fake_event_generator(*args, **kwargs):
        yield json.dumps({"type": api_chat.ChatEvent.THOUGHT.value, "data": "planning"})

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

    monkeypatch.setattr(api_chat, "event_generator", fake_event_generator)
    websocket = FakeWebSocket()

    await api_chat.process_chat_request(
        websocket,
        ChatRequestBody(q="hello", stream=True),
        common=None,
        interrupt_queue=asyncio.Queue(),
    )
    await asyncio.sleep(0.2)

    assert json.dumps({"type": "thought", "data": "planning"}) in websocket.sent
    assert api_chat.ChatEvent.END_EVENT.value in websocket.sent


@pytest.mark.asyncio
async def test_websocket_chat_flushes_first_debounced_message(monkeypatch):
    import asyncio

    from khoj.routers import api_chat
    from khoj.utils.rawconfig import ChatRequestBody

    async def fake_event_generator(*args, **kwargs):
        yield "hello"

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

    monkeypatch.setattr(api_chat, "event_generator", fake_event_generator)
    websocket = FakeWebSocket()

    await api_chat.process_chat_request(
        websocket,
        ChatRequestBody(q="hello", stream=True),
        common=None,
        interrupt_queue=asyncio.Queue(),
    )
    await asyncio.sleep(0.2)

    assert "hello" in websocket.sent
    assert api_chat.ChatEvent.END_EVENT.value in websocket.sent


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
    assert no_auth_response.status_code == 403


def get_sample_files_data():
    return [
        ("files", ("path/to/filename.org", "* practicing piano", "text/org")),
        ("files", ("path/to/filename1.org", "** top 3 reasons why I moved to SF", "text/org")),
        ("files", ("path/to/filename2.org", "* how to build a search engine", "text/org")),
        ("files", ("path/to/filename.pdf", "Moore's law does not apply to consumer hardware", "application/pdf")),
        ("files", ("path/to/filename1.pdf", "The sun is a ball of helium", "application/pdf")),
        ("files", ("path/to/filename2.pdf", "Effect of sunshine on baseline human happiness", "application/pdf")),
        ("files", ("path/to/filename.txt", "data,column,value", "text/plain")),
        ("files", ("path/to/filename1.txt", "<html>my first web page</html>", "text/plain")),
        ("files", ("path/to/filename2.txt", "2021-02-02 Journal Entry", "text/plain")),
        ("files", ("path/to/filename.md", "# Notes from client call", "text/markdown")),
        (
            "files",
            (
                "path/to/filename.docx",
                "## Studying anthropological records from the Fatimid caliphate",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ),
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
            ("path/to/filename.org", big_text, "text/org"),
        )
    ]


def get_medium_size_sample_files_data():
    big_text = "a" * (50 * 1024 * 1024)  # a string of approximately 50 MB
    return [
        (
            "files",
            ("path/to/filename.org", big_text, "text/org"),
        )
    ]
