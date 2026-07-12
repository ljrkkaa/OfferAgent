"""
Tests for memory enable/disable settings and file-backed memory management.

These tests verify:
1. The behavior of ConversationAdapters.is_memory_enabled() for different combinations of:
   - ServerChatSettings.memory_mode (DISABLED, ENABLED_DEFAULT_OFF, ENABLED_DEFAULT_ON)
   - UserConversationConfig.enable_memory (True, False, or not set)

2. /api/memories file-backed list/update/delete behavior.
"""

from unittest.mock import MagicMock

import pytest

from khoj.database.adapters import ConversationAdapters
from khoj.database.models import ServerChatSettings, UserConversationConfig
from khoj.processor.conversation.offeragent_memory import create_memory, get_memory_by_id
from khoj.routers.helpers import get_user_config
from tests.helpers import (
    ServerChatSettingsFactory,
    UserFactory,
)


# ----------------------------------------------------------------------------------------------------
# Test is_memory_enabled with no server config (default behavior)
# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_memory_enabled_no_server_config_no_user_config():
    """When no server config and no user config exists, memory should be enabled (default on)."""
    user = UserFactory()

    result = ConversationAdapters.is_memory_enabled(user)

    assert result is True


@pytest.mark.django_db
def test_memory_enabled_no_server_config_user_enabled():
    """When no server config but user has explicitly enabled memory."""
    user = UserFactory()
    UserConversationConfig.objects.create(user=user, enable_memory=True)

    result = ConversationAdapters.is_memory_enabled(user)

    assert result is True


@pytest.mark.django_db
def test_memory_enabled_no_server_config_user_disabled():
    """When no server config but user has explicitly disabled memory."""
    user = UserFactory()
    UserConversationConfig.objects.create(user=user, enable_memory=False)

    result = ConversationAdapters.is_memory_enabled(user)

    assert result is False


# ----------------------------------------------------------------------------------------------------
# Test is_memory_enabled with server mode DISABLED
# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_memory_disabled_server_disabled_no_user_config():
    """When server disables memory, it should override everything - no user config."""
    user = UserFactory()
    ServerChatSettingsFactory(memory_mode=ServerChatSettings.MemoryMode.DISABLED)

    result = ConversationAdapters.is_memory_enabled(user)

    assert result is False


@pytest.mark.django_db
def test_memory_disabled_server_disabled_user_enabled():
    """When server disables memory, it should override user preference (enabled)."""
    user = UserFactory()
    ServerChatSettingsFactory(memory_mode=ServerChatSettings.MemoryMode.DISABLED)
    UserConversationConfig.objects.create(user=user, enable_memory=True)

    result = ConversationAdapters.is_memory_enabled(user)

    assert result is False


@pytest.mark.django_db
def test_memory_disabled_server_disabled_user_disabled():
    """When server disables memory, user disabled too - should be disabled."""
    user = UserFactory()
    ServerChatSettingsFactory(memory_mode=ServerChatSettings.MemoryMode.DISABLED)
    UserConversationConfig.objects.create(user=user, enable_memory=False)

    result = ConversationAdapters.is_memory_enabled(user)

    assert result is False


# ----------------------------------------------------------------------------------------------------
# Test is_memory_enabled with server mode ENABLED_DEFAULT_OFF
# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_memory_enabled_default_off_no_user_config():
    """When server is enabled_default_off and no user config, memory should be off."""
    user = UserFactory()
    ServerChatSettingsFactory(memory_mode=ServerChatSettings.MemoryMode.ENABLED_DEFAULT_OFF)

    result = ConversationAdapters.is_memory_enabled(user)

    assert result is False


@pytest.mark.django_db
def test_memory_enabled_default_off_user_enabled():
    """When server is enabled_default_off and user opts in, memory should be on."""
    user = UserFactory()
    ServerChatSettingsFactory(memory_mode=ServerChatSettings.MemoryMode.ENABLED_DEFAULT_OFF)
    UserConversationConfig.objects.create(user=user, enable_memory=True)

    result = ConversationAdapters.is_memory_enabled(user)

    assert result is True


@pytest.mark.django_db
def test_memory_enabled_default_off_user_disabled():
    """When server is enabled_default_off and user explicitly disabled, memory should be off."""
    user = UserFactory()
    ServerChatSettingsFactory(memory_mode=ServerChatSettings.MemoryMode.ENABLED_DEFAULT_OFF)
    UserConversationConfig.objects.create(user=user, enable_memory=False)

    result = ConversationAdapters.is_memory_enabled(user)

    assert result is False


# ----------------------------------------------------------------------------------------------------
# Test is_memory_enabled with server mode ENABLED_DEFAULT_ON
# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_memory_enabled_default_on_no_user_config():
    """When server is enabled_default_on and no user config, memory should be on."""
    user = UserFactory()
    ServerChatSettingsFactory(memory_mode=ServerChatSettings.MemoryMode.ENABLED_DEFAULT_ON)

    result = ConversationAdapters.is_memory_enabled(user)

    assert result is True


@pytest.mark.django_db
def test_memory_enabled_default_on_user_enabled():
    """When server is enabled_default_on and user enabled, memory should be on."""
    user = UserFactory()
    ServerChatSettingsFactory(memory_mode=ServerChatSettings.MemoryMode.ENABLED_DEFAULT_ON)
    UserConversationConfig.objects.create(user=user, enable_memory=True)

    result = ConversationAdapters.is_memory_enabled(user)

    assert result is True


@pytest.mark.django_db
def test_memory_enabled_default_on_user_disabled():
    """When server is enabled_default_on and user opts out, memory should be off."""
    user = UserFactory()
    ServerChatSettingsFactory(memory_mode=ServerChatSettings.MemoryMode.ENABLED_DEFAULT_ON)
    UserConversationConfig.objects.create(user=user, enable_memory=False)

    result = ConversationAdapters.is_memory_enabled(user)

    assert result is False


# ----------------------------------------------------------------------------------------------------
# Test get_user_config returns correct enable_memory and server_memory_mode
# ----------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_get_user_config_memory_no_server_config():
    """get_user_config should return default values when no server config."""
    user = UserFactory()
    request = MagicMock()
    request.url = MagicMock()
    request.url.path = "/api/config"
    request.session = {}

    config = get_user_config(user, request, is_detailed=True)

    assert config["enable_memory"] is True
    assert config["server_memory_mode"] == "enabled_default_on"


@pytest.mark.django_db
def test_get_user_config_memory_server_disabled():
    """get_user_config should reflect server disabled mode."""
    user = UserFactory()
    ServerChatSettingsFactory(memory_mode=ServerChatSettings.MemoryMode.DISABLED)
    request = MagicMock()
    request.url = MagicMock()
    request.url.path = "/api/config"
    request.session = {}

    config = get_user_config(user, request, is_detailed=True)

    assert config["enable_memory"] is False
    assert config["server_memory_mode"] == "disabled"


@pytest.mark.django_db
def test_get_user_config_memory_server_enabled_default_off_user_opted_in():
    """get_user_config should show user opted in when server is default off."""
    user = UserFactory()
    ServerChatSettingsFactory(memory_mode=ServerChatSettings.MemoryMode.ENABLED_DEFAULT_OFF)
    UserConversationConfig.objects.create(user=user, enable_memory=True)
    request = MagicMock()
    request.url = MagicMock()
    request.url.path = "/api/config"
    request.session = {}

    config = get_user_config(user, request, is_detailed=True)

    assert config["enable_memory"] is True
    assert config["server_memory_mode"] == "enabled_default_off"


@pytest.mark.django_db
def test_get_user_config_memory_server_enabled_default_on_user_opted_out():
    """get_user_config should show user opted out when server is default on."""
    user = UserFactory()
    ServerChatSettingsFactory(memory_mode=ServerChatSettings.MemoryMode.ENABLED_DEFAULT_ON)
    UserConversationConfig.objects.create(user=user, enable_memory=False)
    request = MagicMock()
    request.url = MagicMock()
    request.url.path = "/api/config"
    request.session = {}

    config = get_user_config(user, request, is_detailed=True)

    assert config["enable_memory"] is False
    assert config["server_memory_mode"] == "enabled_default_on"


@pytest.mark.django_db(transaction=True)
def test_memories_api_list_update_delete_preserves_memory_identity(
    client, api_user, default_user, tmp_path, monkeypatch
):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    memory = create_memory(
        api_user.user,
        "prefers Java interview drills",
        "feedback",
        description="prefers Java interview drills",
    )
    create_memory(default_user, "must not leak across users", "user")
    headers = {"Authorization": f"Bearer {api_user.token}"}

    response = client.get("/api/memories", headers=headers)
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": memory.id,
            "raw": "prefers Java interview drills",
            "type": "feedback",
            "description": "prefers Java interview drills",
            "created_at": memory.created_at.isoformat(),
            "updated_at": memory.updated_at.isoformat(),
        }
    ]

    response = client.put(
        f"/api/memories/{memory.id}",
        headers=headers,
        json={"raw": "prefers Redis interview drills"},
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["id"] == memory.id
    assert updated["raw"] == "prefers Redis interview drills"
    assert updated["type"] == "feedback"
    assert updated["description"] == "prefers Redis interview drills"

    response = client.delete(f"/api/memories/{memory.id}", headers=headers)
    assert response.status_code == 204
    assert get_memory_by_id(api_user.user, memory.id) is None
