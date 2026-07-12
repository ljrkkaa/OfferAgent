import importlib

import pytest
from django.db import connection

from khoj.database.adapters import AgentAdapters, ConversationAdapters
from khoj.database.models import Agent, Entry, FileObject


@pytest.mark.django_db
def test_new_conversation_uses_default_agent(default_user, default_openai_chat_model_option):
    AgentAdapters.create_default_agent()
    conversation = ConversationAdapters.create_conversation_session(default_user)

    assert conversation.agent.slug == AgentAdapters.DEFAULT_AGENT_SLUG
    assert conversation.agent.name == AgentAdapters.DEFAULT_AGENT_NAME


@pytest.mark.django_db
def test_custom_agent_api_is_removed(client):
    assert client.get("/api/agents").status_code == 404


@pytest.mark.django_db(transaction=True)
def test_hard_delete_migration_preserves_content_owned_by_legacy_custom_agent(
    default_user,
    default_openai_chat_model_option,
):
    custom_agent = Agent.objects.create(
        name="Legacy custom agent",
        slug="legacy-custom-agent",
        chat_model=default_openai_chat_model_option,
    )
    file_object = FileObject.objects.create(file_name="legacy.md", raw_text="legacy", user=default_user)
    entry = Entry.objects.create(
        user=default_user,
        raw="legacy",
        compiled="legacy",
        file_name="legacy.md",
        file_path="legacy.md",
        hashed_value="legacy-hash",
        file_object=file_object,
    )

    migration = importlib.import_module("khoj.database.migrations.0008_hard_delete_unused_surfaces")
    statements = migration.Migration.operations[0].sql
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE database_fileobject ADD COLUMN agent_id bigint REFERENCES database_agent(id) ON DELETE CASCADE"
        )
        cursor.execute(
            "ALTER TABLE database_entry ADD COLUMN agent_id bigint REFERENCES database_agent(id) ON DELETE CASCADE"
        )
        cursor.execute("UPDATE database_fileobject SET agent_id = %s WHERE id = %s", [custom_agent.id, file_object.id])
        cursor.execute("UPDATE database_entry SET agent_id = %s WHERE id = %s", [custom_agent.id, entry.id])
        for statement in statements:
            cursor.execute(statement)

    assert FileObject.objects.filter(id=file_object.id).exists()
    assert Entry.objects.filter(id=entry.id).exists()
