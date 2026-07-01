# tests/test_agents.py
import asyncio
import warnings
from collections import Counter
from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone as django_timezone

from khoj.database.adapters import AgentAdapters, ConversationAdapters
from khoj.database.models import Agent, ChatModel, Conversation, Entry, FileObject, KhojApiUser, KhojUser, PriceTier
from khoj.routers.api_agents import _recent_conversation_cutoff
from khoj.routers.helpers import execute_search
from khoj.utils import state
from khoj.utils.helpers import get_absolute_path
from tests.helpers import ChatModelFactory, ConversationFactory

AGENT_KB_ENGLISH_NAME_MAX_DISTANCE = 0.8


def test_create_default_agent(default_user: KhojUser):
    ChatModelFactory()

    agent = AgentAdapters.create_default_agent()
    assert agent is not None
    assert agent.input_tools == []
    assert agent.output_modes == []
    assert agent.privacy_level == Agent.PrivacyLevel.PUBLIC
    assert agent.managed_by_admin


@pytest.mark.django_db(transaction=True)
def test_sync_create_conversation_session_accepts_agent_slug(default_user: KhojUser):
    chat_model = ChatModelFactory()
    agent = Agent.objects.create(
        name="Sync Session Agent",
        slug="sync-session-agent",
        creator=default_user,
        chat_model=chat_model,
        privacy_level=Agent.PrivacyLevel.PRIVATE,
    )

    conversation = ConversationAdapters.create_conversation_session(default_user, agent_slug=agent.slug)

    assert conversation.agent_id == agent.id


def test_recent_conversation_cutoff_is_timezone_aware():
    assert django_timezone.is_aware(_recent_conversation_cutoff())


def test_agents_endpoint_uses_timezone_aware_recent_conversation_cutoff(
    chat_client_with_auth, default_user2, api_user2
):
    ChatModelFactory()
    agent = AgentAdapters.create_default_agent()
    ConversationFactory(user=default_user2, agent=agent)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        response = chat_client_with_auth.get("/api/agents", headers={"Authorization": f"Bearer {api_user2.token}"})

    assert response.status_code == 200
    assert not [
        warning
        for warning in caught
        if warning.category is RuntimeWarning and "received a naive datetime" in str(warning.message)
    ]


@pytest.mark.django_db(transaction=True)
def test_agents_endpoint_handles_missing_default_agent(chat_client_with_auth, api_user2: KhojApiUser):
    chat_model = ChatModelFactory(friendly_name="agent-list-model")
    Agent.objects.filter(slug=AgentAdapters.DEFAULT_AGENT_SLUG).delete()
    agent = Agent.objects.create(
        name="User Agent Without Default",
        slug="user-agent-without-default",
        creator=api_user2.user,
        chat_model=chat_model,
        personality="Answer directly.",
        privacy_level=Agent.PrivacyLevel.PRIVATE,
    )

    response = chat_client_with_auth.get("/api/agents", headers={"Authorization": f"Bearer {api_user2.token}"})

    assert response.status_code == 200
    agents = response.json()
    assert any(packet["slug"] == agent.slug and packet["chat_model"] == "agent-list-model" for packet in agents)


@pytest.mark.django_db(transaction=True)
def test_agent_conversation_returns_virtual_default_agent_for_codex_runtime(
    chat_client_with_auth, api_user2: KhojApiUser, monkeypatch
):
    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "codex")
    monkeypatch.setenv("KHOJ_CODEX_MODEL", "gpt-test-codex")
    Agent.objects.all().delete()
    ChatModel.objects.all().delete()
    conversation = Conversation.objects.create(user=api_user2.user)

    response = chat_client_with_auth.get(
        f"/api/agents/conversation?conversation_id={conversation.id}",
        headers={"Authorization": f"Bearer {api_user2.token}"},
    )

    assert response.status_code == 200
    assert response.json()["slug"] == "khoj"
    assert response.json()["chat_model"] == "gpt-test-codex"


@pytest.mark.django_db(transaction=True)
def test_hidden_agent_create_requires_database_chat_model_in_codex_runtime(
    chat_client_with_auth, api_user2: KhojApiUser, monkeypatch
):
    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "codex")
    Agent.objects.all().delete()
    ChatModel.objects.all().delete()
    conversation = Conversation.objects.create(user=api_user2.user)

    response = chat_client_with_auth.post(
        f"/api/agents/hidden?conversation_id={conversation.id}",
        headers={"Authorization": f"Bearer {api_user2.token}"},
        json={"persona": "Use short answers.", "input_tools": [], "output_modes": []},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Agent editing requires a configured database chat model."


@pytest.mark.django_db(transaction=True)
def test_hidden_agent_create_without_chat_model_uses_default_model(
    chat_client_with_auth, api_user2: KhojApiUser, monkeypatch
):
    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "openai")
    chat_model = ChatModelFactory(name="default-model-id", friendly_name="Default Model")
    ConversationAdapters.set_default_chat_model(chat_model)
    conversation = Conversation.objects.create(user=api_user2.user)

    response = chat_client_with_auth.post(
        f"/api/agents/hidden?conversation_id={conversation.id}",
        headers={"Authorization": f"Bearer {api_user2.token}"},
        json={"persona": "Use short answers.", "input_tools": [], "output_modes": []},
    )

    assert response.status_code == 200
    assert response.json()["chat_model"] == "Default Model"
    conversation.refresh_from_db()
    assert conversation.agent.chat_model_id == chat_model.id


@pytest.mark.django_db(transaction=True)
def test_non_creator_cannot_update_public_agent(
    chat_client_with_auth, default_user: KhojUser, api_user2: KhojApiUser
):
    chat_model = ChatModelFactory(friendly_name="agent-permission-model")
    agent = Agent.objects.create(
        name="Shared Agent",
        slug="shared-agent",
        creator=default_user,
        chat_model=chat_model,
        personality="Stay helpful.",
        privacy_level=Agent.PrivacyLevel.PUBLIC,
    )
    headers = {"Authorization": f"Bearer {api_user2.token}"}

    response = chat_client_with_auth.patch(
        "/api/agents",
        headers=headers,
        json={
            "name": "Hijacked Agent",
            "persona": agent.personality,
            "privacy_level": agent.privacy_level,
            "icon": agent.style_icon,
            "color": agent.style_color,
            "chat_model": chat_model.friendly_name,
            "files": [],
            "input_tools": [],
            "output_modes": [],
            "slug": agent.slug,
        },
    )

    assert response.status_code == 404
    agent.refresh_from_db()
    assert agent.name == "Shared Agent"


@pytest.mark.django_db(transaction=True)
def test_non_creator_can_read_public_agent(chat_client_with_auth, default_user: KhojUser, api_user2: KhojApiUser):
    chat_model = ChatModelFactory(friendly_name="agent-read-permission-model")
    agent = Agent.objects.create(
        name="Readable Shared Agent",
        slug="readable-shared-agent",
        creator=default_user,
        chat_model=chat_model,
        personality="Stay helpful.",
        privacy_level=Agent.PrivacyLevel.PUBLIC,
    )
    headers = {"Authorization": f"Bearer {api_user2.token}"}

    response = chat_client_with_auth.get(f"/api/agents/{agent.slug}", headers=headers)

    assert response.status_code == 200
    assert response.json()["slug"] == agent.slug


@pytest.mark.django_db(transaction=True)
def test_unauthenticated_user_cannot_read_admin_private_agent(client):
    chat_model = ChatModelFactory(friendly_name="admin-private-agent-model")
    agent = Agent.objects.create(
        name="Admin Private Agent",
        slug="admin-private-agent",
        chat_model=chat_model,
        privacy_level=Agent.PrivacyLevel.PRIVATE,
    )

    response = client.get(f"/api/agents/{agent.slug}")

    assert response.status_code == 404


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_anonymous_async_agent_lookup_ignores_admin_private_agent(default_openai_chat_model_option: ChatModel):
    agent = await sync_to_async(Agent.objects.create)(
        name="Async Admin Private Agent",
        slug="async-admin-private-agent",
        chat_model=default_openai_chat_model_option,
        privacy_level=Agent.PrivacyLevel.PRIVATE,
    )

    assert await AgentAdapters.aget_agent_by_slug(agent.slug, None) is None
    assert await AgentAdapters.aget_agent_by_name(agent.name, None) is None


@pytest.mark.django_db(transaction=True)
def test_non_creator_cannot_delete_public_agent(
    chat_client_with_auth, default_user: KhojUser, api_user2: KhojApiUser
):
    chat_model = ChatModelFactory(friendly_name="agent-delete-permission-model")
    agent = Agent.objects.create(
        name="Shared Agent",
        slug="shared-agent-delete",
        creator=default_user,
        chat_model=chat_model,
        privacy_level=Agent.PrivacyLevel.PUBLIC,
    )
    headers = {"Authorization": f"Bearer {api_user2.token}"}

    response = chat_client_with_auth.delete(f"/api/agents/{agent.slug}", headers=headers)

    assert response.status_code == 404
    assert Agent.objects.filter(id=agent.id).exists()


@pytest.mark.django_db(transaction=True)
def test_create_agent_with_unknown_chat_model_returns_bad_request(chat_client_with_auth, api_user2: KhojApiUser):
    headers = {"Authorization": f"Bearer {api_user2.token}"}

    response = chat_client_with_auth.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "Broken Model Agent",
            "persona": "Stay helpful.",
            "privacy_level": Agent.PrivacyLevel.PRIVATE,
            "icon": Agent.StyleIconTypes.LIGHTBULB,
            "color": Agent.StyleColorTypes.BLUE,
            "chat_model": "missing-model",
            "files": [],
            "input_tools": [],
            "output_modes": [],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Unknown chat model: missing-model"


@pytest.mark.django_db(transaction=True)
def test_create_agent_with_unavailable_paid_chat_model_returns_forbidden(
    chat_client_with_auth, api_user4: KhojApiUser, monkeypatch
):
    monkeypatch.setattr(state, "billing_enabled", True)
    subscription = api_user4.user.subscription
    subscription.is_recurring = False
    subscription.renewal_date = django_timezone.now() - timedelta(days=1)
    subscription.save()
    paid_model = ChatModelFactory(friendly_name="paid-agent-model", price_tier=PriceTier.STANDARD)
    headers = {"Authorization": f"Bearer {api_user4.token}"}

    response = chat_client_with_auth.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "Paid Model Agent",
            "persona": "Stay helpful.",
            "privacy_level": Agent.PrivacyLevel.PRIVATE,
            "icon": Agent.StyleIconTypes.LIGHTBULB,
            "color": Agent.StyleColorTypes.BLUE,
            "chat_model": paid_model.friendly_name,
            "files": [],
            "input_tools": [],
            "output_modes": [],
        },
    )

    assert response.status_code == 403
    assert response.json()["error"] == "Chat model paid-agent-model is not available for this account."
    assert not Agent.objects.filter(name="Paid Model Agent", creator=api_user4.user).exists()


@pytest.mark.django_db(transaction=True)
def test_create_agent_rejects_invalid_privacy_level(chat_client_with_auth, api_user2: KhojApiUser):
    chat_model = ChatModelFactory(friendly_name="agent-invalid-privacy-model")
    headers = {"Authorization": f"Bearer {api_user2.token}"}

    response = chat_client_with_auth.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "Invalid Privacy Agent",
            "persona": "Stay helpful.",
            "privacy_level": "public-ish",
            "icon": Agent.StyleIconTypes.LIGHTBULB,
            "color": Agent.StyleColorTypes.BLUE,
            "chat_model": chat_model.friendly_name,
            "files": [],
            "input_tools": [],
            "output_modes": [],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Invalid privacy_level: public-ish"
    assert not Agent.objects.filter(name="Invalid Privacy Agent", creator=api_user2.user).exists()


@pytest.mark.django_db(transaction=True)
def test_hidden_agent_update_rejects_regular_agent(
    chat_client_with_auth, default_user2: KhojUser, api_user2: KhojApiUser
):
    chat_model = ChatModelFactory(friendly_name="hidden-agent-regular-model")
    agent = Agent.objects.create(
        name="Regular Agent",
        slug="regular-agent",
        creator=default_user2,
        chat_model=chat_model,
        personality="Keep me regular.",
        privacy_level=Agent.PrivacyLevel.PRIVATE,
        is_hidden=False,
    )
    headers = {"Authorization": f"Bearer {api_user2.token}"}

    response = chat_client_with_auth.patch(
        "/api/agents/hidden",
        headers=headers,
        json={
            "slug": agent.slug,
            "persona": "Overwrite me.",
            "chat_model": chat_model.friendly_name,
            "input_tools": [],
            "output_modes": [],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Agent with name regular-agent is not hidden."
    agent.refresh_from_db()
    assert agent.is_hidden is False
    assert agent.personality == "Keep me regular."


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_create_or_update_agent(default_user: KhojUser, default_openai_chat_model_option: ChatModel):
    new_agent = await AgentAdapters.aupdate_agent(
        default_user,
        "Test Agent",
        "Test Personality",
        Agent.PrivacyLevel.PRIVATE,
        "icon",
        "color",
        default_openai_chat_model_option.name,
        [],
        [],
        [],
    )
    assert new_agent is not None
    assert new_agent.name == "Test Agent"
    assert new_agent.privacy_level == Agent.PrivacyLevel.PRIVATE
    assert new_agent.creator == default_user


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_delete_missing_agent_by_slug_returns_false(default_user: KhojUser):
    deleted = await AgentAdapters.adelete_agent_by_slug("missing-agent", default_user)

    assert deleted is False


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_create_or_update_agent_with_knowledge_base(
    default_user2: KhojUser, default_openai_chat_model_option: ChatModel, chat_client
):
    full_filename = get_absolute_path("tests/data/markdown/having_kids.markdown")
    new_agent = await AgentAdapters.aupdate_agent(
        default_user2,
        "Test Agent",
        "Test Personality",
        Agent.PrivacyLevel.PRIVATE,
        "icon",
        "color",
        default_openai_chat_model_option.name,
        [full_filename],
        [],
        [],
    )
    entries = await sync_to_async(list)(Entry.objects.filter(agent=new_agent))
    file_names = set()
    for entry in entries:
        file_names.add(entry.file_path)

    assert new_agent is not None
    assert new_agent.name == "Test Agent"
    assert new_agent.privacy_level == Agent.PrivacyLevel.PRIVATE
    assert new_agent.creator == default_user2
    assert len(entries) > 0
    assert full_filename in file_names
    assert len(file_names) == 1


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_create_or_update_agent_with_knowledge_base_and_search(
    default_user2: KhojUser, default_openai_chat_model_option: ChatModel, chat_client
):
    full_filename = get_absolute_path("tests/data/markdown/having_kids.markdown")
    new_agent = await AgentAdapters.aupdate_agent(
        default_user2,
        "Test Agent",
        "Test Personality",
        Agent.PrivacyLevel.PRIVATE,
        "icon",
        "color",
        default_openai_chat_model_option.name,
        [full_filename],
        [],
        [],
    )

    search_result = await execute_search(user=default_user2, q="having kids", agent=new_agent)

    assert len(search_result) > 0
    assert any("Having Kids" in result.entry for result in search_result)


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_agent_with_knowledge_base_and_search_not_creator(
    default_user2: KhojUser, default_openai_chat_model_option: ChatModel, chat_client, default_user3: KhojUser
):
    full_filename = get_absolute_path("tests/data/markdown/having_kids.markdown")
    new_agent = await AgentAdapters.aupdate_agent(
        default_user2,
        "Test Agent",
        "Test Personality",
        Agent.PrivacyLevel.PUBLIC,
        "icon",
        "color",
        default_openai_chat_model_option.name,
        [full_filename],
        [],
        [],
    )

    search_result = await execute_search(user=default_user3, q="having kids", agent=new_agent)

    assert len(search_result) > 0
    assert any("Having Kids" in result.entry for result in search_result)


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_agent_with_knowledge_base_and_search_not_creator_and_private(
    default_user2: KhojUser, default_openai_chat_model_option: ChatModel, chat_client, default_user3: KhojUser
):
    full_filename = get_absolute_path("tests/data/markdown/having_kids.markdown")
    new_agent = await AgentAdapters.aupdate_agent(
        default_user2,
        "Test Agent",
        "Test Personality",
        Agent.PrivacyLevel.PRIVATE,
        "icon",
        "color",
        default_openai_chat_model_option.name,
        [full_filename],
        [],
        [],
    )

    search_result = await execute_search(user=default_user3, q="having kids", agent=new_agent)

    assert len(search_result) == 0


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_agent_with_knowledge_base_and_search_not_creator_and_private_accessible_to_none(
    default_user2: KhojUser, default_openai_chat_model_option: ChatModel, chat_client
):
    full_filename = get_absolute_path("tests/data/markdown/having_kids.markdown")
    new_agent = await AgentAdapters.aupdate_agent(
        default_user2,
        "Test Agent",
        "Test Personality",
        Agent.PrivacyLevel.PRIVATE,
        "icon",
        "color",
        default_openai_chat_model_option.name,
        [full_filename],
        [],
        [],
    )

    search_result = await execute_search(user=None, q="having kids", agent=new_agent)

    assert len(search_result) > 0
    assert any("Having Kids" in result.entry for result in search_result)


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_multiple_agents_with_knowledge_base_and_users(
    default_user2: KhojUser, default_openai_chat_model_option: ChatModel, chat_client, default_user3: KhojUser
):
    full_filename = get_absolute_path("tests/data/markdown/having_kids.markdown")
    await AgentAdapters.aupdate_agent(
        default_user2,
        "Test Agent",
        "Test Personality",
        Agent.PrivacyLevel.PUBLIC,
        "icon",
        "color",
        default_openai_chat_model_option.name,
        [full_filename],
        [],
        [],
    )

    full_filename2 = get_absolute_path("tests/data/markdown/Namita.markdown")
    new_agent2 = await AgentAdapters.aupdate_agent(
        default_user2,
        "Test Agent 2",
        "Test Personality",
        Agent.PrivacyLevel.PUBLIC,
        "icon",
        "color",
        default_openai_chat_model_option.name,
        [full_filename2],
        [],
        [],
    )

    search_result = await execute_search(user=default_user3, q="having kids", agent=new_agent2)
    search_result2 = await execute_search(
        user=default_user3, q="Namita", agent=new_agent2, max_distance=AGENT_KB_ENGLISH_NAME_MAX_DISTANCE
    )

    assert len(search_result) == 0
    assert len(search_result2) > 0
    assert any("Namita" in result.entry for result in search_result2)


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_large_knowledge_base_atomic_update(
    default_user2: KhojUser, default_openai_chat_model_option: ChatModel, chat_client_with_large_kb
):
    """
    The test simulates the scenario where lots of files are synced to an agent's knowledge base,
    and verifies that all files are properly added atomically.
    """
    # The chat_client_with_large_kb fixture has already created and indexed 200 files
    # Get the files that are already in the user's knowledge base
    user_file_objects = await sync_to_async(list)(FileObject.objects.filter(user=default_user2, agent=None))

    # Verify we have the expected large knowledge base from the fixture
    assert len(user_file_objects) >= 150, f"Expected at least 150 files from fixture, got {len(user_file_objects)}"

    # Extract file paths for agent creation
    available_files = [fo.file_name for fo in user_file_objects]
    files_to_test = available_files  # Use all available files for the stress test

    # Create initial agent with smaller set
    initial_files = files_to_test[:20]
    agent = await AgentAdapters.aupdate_agent(
        default_user2,
        "Large KB Agent",
        "Test agent with large knowledge base",
        Agent.PrivacyLevel.PRIVATE,
        "icon",
        "color",
        default_openai_chat_model_option.name,
        initial_files,
        [],
        [],
    )

    # Verify initial state
    initial_entries = await sync_to_async(list)(Entry.objects.filter(agent=agent))
    initial_entries_count = len(initial_entries)

    # Now perform the stress test: update with ALL 180 files at once
    # This is where partial sync issues would occur without atomic transactions
    updated_agent = await AgentAdapters.aupdate_agent(
        default_user2,
        "Large KB Agent Updated",  # Change name to trigger update
        "Test agent with large knowledge base - updated",
        Agent.PrivacyLevel.PRIVATE,
        "icon",
        "color",
        default_openai_chat_model_option.name,
        files_to_test,  # ALL files at once
        [],
        [],
    )

    # Verify atomic update completed successfully
    final_entries = await sync_to_async(list)(Entry.objects.filter(agent=updated_agent))
    final_file_objects = await sync_to_async(list)(FileObject.objects.filter(agent=updated_agent))

    # Critical assertions that would fail with partial sync issues
    expected_file_count = len(files_to_test)
    actual_file_count = len(final_file_objects)

    assert actual_file_count == expected_file_count, (
        f"Partial sync detected! Expected {expected_file_count} files, "
        f"but got {actual_file_count}. This indicates non-atomic update."
    )

    # Verify all files are properly represented
    file_paths_in_db = {fo.file_name for fo in final_file_objects}
    expected_file_paths = set(files_to_test)

    missing_files = expected_file_paths - file_paths_in_db
    assert not missing_files, f"Missing files in knowledge base: {missing_files}"

    # Verify entries were created (should have significantly more than initial)
    assert len(final_entries) > initial_entries_count, "Should have more entries after update"

    # With 180 files, we should have many entries (each file creates multiple entries)
    assert len(final_entries) >= expected_file_count, (
        f"Expected at least {expected_file_count} entries, got {len(final_entries)}"
    )

    # Verify no partial state - all entries should correspond to the final file set
    entry_file_paths = {entry.file_path for entry in final_entries}

    # All file objects should have corresponding entries
    assert file_paths_in_db.issubset(entry_file_paths), (
        "All file objects should have corresponding entries - atomic update verification"
    )

    # Additional stress test: verify referential integrity
    # Count entries per file to ensure no partial file processing
    entries_per_file = Counter(entry.file_path for entry in final_entries)

    # Ensure every file has at least one entry (no files were partially processed and lost)
    files_without_entries = set(files_to_test) - set(entries_per_file.keys())
    assert not files_without_entries, f"Files with no entries (partial sync): {files_without_entries}"

    # Test that search works with the updated knowledge base
    search_result = await execute_search(user=default_user2, q="test", agent=updated_agent)
    assert len(search_result) > 0, "Should be able to search the updated knowledge base"


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_concurrent_agent_updates_atomicity(
    default_user2: KhojUser, default_openai_chat_model_option: ChatModel, chat_client_with_large_kb
):
    """
    Test that concurrent updates to the same agent don't result in partial syncs.
    This simulates the race condition that could occur with non-atomic updates.
    """
    # The chat_client_with_large_kb fixture has already created and indexed 200 files
    # Get the files that are already in the user's knowledge base
    user_file_objects = await sync_to_async(list)(FileObject.objects.filter(user=default_user2, agent=None))

    # Extract file paths for agent creation
    available_files = [fo.file_name for fo in user_file_objects]
    test_files = available_files  # Use all available files for the stress test

    # Create initial agent
    await AgentAdapters.aupdate_agent(
        default_user2,
        "Concurrent Test Agent",
        "Test concurrent updates",
        Agent.PrivacyLevel.PRIVATE,
        "icon",
        "color",
        default_openai_chat_model_option.name,
        test_files[:10],
        [],
        [],
    )

    # Create two concurrent update tasks with different file sets
    async def update_agent_with_files(file_subset, name_suffix, delay=0):
        if delay > 0:
            await asyncio.sleep(delay)
        return await AgentAdapters.aupdate_agent(
            default_user2,
            f"Concurrent Test Agent {name_suffix}",
            f"Test concurrent updates {name_suffix}",
            Agent.PrivacyLevel.PRIVATE,
            "icon",
            "color",
            default_openai_chat_model_option.name,
            file_subset,
            [],
            [],
        )

    # Run concurrent updates
    initial_split_size = 20
    large_split_size = 80
    try:
        results = await asyncio.gather(
            update_agent_with_files(test_files[initial_split_size : initial_split_size + large_split_size], "Second"),
            update_agent_with_files(test_files[:initial_split_size], "First"),
            return_exceptions=True,
        )

        # At least one should succeed with atomic updates
        successful_updates = [r for r in results if not isinstance(r, Exception)]
        assert len(successful_updates) >= 1, "At least one concurrent update should succeed"

        # Verify the final state is consistent
        final_agent = successful_updates[0]
        final_file_objects = await sync_to_async(list)(FileObject.objects.filter(agent=final_agent))
        final_entries = await sync_to_async(list)(Entry.objects.filter(agent=final_agent))

        # The agent should have a consistent state (all files from the successful update)
        assert len(final_file_objects) == large_split_size, "Should have file objects after concurrent update"
        assert len(final_entries) >= large_split_size, "Should have entries after concurrent update"

        # Verify referential integrity
        entry_file_paths = {entry.file_path for entry in final_entries}
        file_object_paths = {fo.file_name for fo in final_file_objects}

        # All entries should have corresponding file objects
        assert entry_file_paths.issubset(file_object_paths), (
            "All entries should have corresponding file objects - indicates atomic update worked"
        )

    except Exception as e:
        # If we get database integrity errors, that's actually expected behavior
        # with proper atomic transactions - they should fail cleanly rather than
        # allowing partial updates
        assert "database" in str(e).lower() or "integrity" in str(e).lower(), (
            f"Expected database/integrity error with concurrent updates, got: {e}"
        )
