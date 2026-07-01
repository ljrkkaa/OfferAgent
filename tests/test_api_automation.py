import json

import pytest
from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from django_apscheduler.jobstores import DjangoJobStore
from fastapi.testclient import TestClient

from khoj.database.models import Conversation
from khoj.routers.helpers import schedule_automation, scheduled_chat
from khoj.utils import state
from tests.helpers import AiModelApiFactory, ChatModelFactory, get_chat_api_key


@pytest.fixture(autouse=True)
def setup_scheduler():
    state.scheduler = BackgroundScheduler()
    state.scheduler.add_jobstore(DjangoJobStore(), "default")
    state.scheduler.start()
    yield
    state.scheduler.shutdown()


def create_test_automation(client: TestClient) -> str:
    """Helper function to create a test automation and return its ID."""
    state.anonymous_mode = True
    ChatModelFactory(
        name="gemini-2.5-flash", model_type="google", ai_model_api=AiModelApiFactory(api_key=get_chat_api_key("google"))
    )
    params = {
        "q": "test automation",
        "crontime": "0 0 * * *",
    }
    response = client.post("/api/automation", params=params)
    assert response.status_code == 200
    return response.json()["id"]


@pytest.mark.django_db(transaction=True)
def test_scheduled_chat_tells_chat_api_task_already_triggered(monkeypatch, default_user2):
    state.anonymous_mode = True
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        text = '{"response": "done"}'

        def json(self):
            return {"response": "done"}

    def fake_post(url, headers, json, allow_redirects=False):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr("khoj.routers.helpers.requests.post", fake_post)
    monkeypatch.setattr("khoj.routers.helpers.should_notify", lambda *args, **kwargs: False)

    scheduled_chat(
        query_to_run="/automated_task Remind me to review the edited e2e token.",
        scheduling_request="Every day remind me to review the edited e2e token.",
        subject="edited e2e subject",
        user=default_user2,
        calling_url="http://testserver/api/automation?q=old&stream=true",
    )

    assert captured["url"] == "http://testserver/api/chat?client=khoj"
    assert "This scheduled automation has already triggered" in captured["json"]["q"]
    assert "Perform the task now" in captured["json"]["q"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(not get_chat_api_key("google"), reason="Requires GEMINI_API_KEY to be set")
def test_create_automation(client: TestClient):
    """Test that creating an automation works as expected."""
    # Arrange
    state.anonymous_mode = True
    ChatModelFactory(
        name="gemini-2.5-flash", model_type="google", ai_model_api=AiModelApiFactory(api_key=get_chat_api_key("google"))
    )
    params = {
        "q": "test automation",
        "crontime": "0 0 * * *",
    }

    # Act
    response = client.post("/api/automation", params=params)

    # Assert
    assert response.status_code == 200
    response_json = response.json()
    assert response_json["scheduling_request"] == "test automation"
    assert response_json["crontime"] == "0 0 * * *"


@pytest.mark.django_db(transaction=True)
def test_create_automation_rejects_malformed_crontime(client: TestClient):
    state.anonymous_mode = True

    response = client.post(
        "/api/automation",
        params={
            "q": "test automation",
            "crontime": "bad",
        },
    )

    assert response.status_code == 400
    assert response.text == "Invalid crontime"


@pytest.mark.django_db(transaction=True)
def test_create_automation_cleans_conversation_when_schedule_fails(client: TestClient, monkeypatch, default_user2):
    state.anonymous_mode = True
    monkeypatch.setattr(
        "khoj.routers.api_automation.schedule_query",
        lambda q, chat_history, user: ("0 0 * * *", q, "broken subject"),
    )

    def fail_schedule(*args, **kwargs):
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr("khoj.routers.api_automation.schedule_automation", fail_schedule)

    before = Conversation.objects.filter(user=default_user2).count()
    response = client.post(
        "/api/automation",
        params={
            "q": "broken automation",
            "crontime": "0 0 * * *",
        },
    )

    assert response.status_code == 500
    assert Conversation.objects.filter(user=default_user2).count() == before


@pytest.mark.django_db(transaction=True)
def test_edit_automation_with_invalid_timezone_uses_utc(client: TestClient, monkeypatch, default_user2):
    state.anonymous_mode = True
    automation = schedule_automation(
        query_to_run="/automated_task old automation",
        subject="old subject",
        crontime="0 0 * * *",
        timezone="UTC",
        scheduling_request="old automation",
        user=default_user2,
        calling_url="http://testserver/api/automation",
        conversation_id="test-conversation",
    )
    monkeypatch.setattr(
        "khoj.routers.api_automation.schedule_query",
        lambda q, chat_history, user: ("0 1 * * *", q, "edited subject"),
    )

    response = client.put(
        "/api/automation",
        params={
            "automation_id": automation.id,
            "q": "edited automation",
            "subject": "edited subject",
            "crontime": "0 1 * * *",
            "timezone": "Not/AZone",
        },
    )

    assert response.status_code == 200
    edited_automation = response.json()
    assert edited_automation["crontime"] == "0 1 * * *"
    assert edited_automation["subject"] == "edited subject"


@pytest.mark.django_db(transaction=True)
def test_get_automations_handles_legacy_plain_name(client: TestClient, default_user2):
    state.anonymous_mode = True
    automation = schedule_automation(
        query_to_run="/automated_task legacy automation",
        subject="legacy subject",
        crontime="0 0 * * *",
        timezone="UTC",
        scheduling_request="legacy automation",
        user=default_user2,
        calling_url="http://testserver/api/automation",
        conversation_id="legacy-conversation",
    )
    automation.modify(name="legacy plain name")

    response = client.get("/api/automation")

    assert response.status_code == 200
    item = next(item for item in response.json() if item["id"] == automation.id)
    assert item["subject"] == "legacy subject"
    assert item["scheduling_request"] == "legacy automation"


@pytest.mark.django_db(transaction=True)
def test_edit_automation_repairs_legacy_plain_name(client: TestClient, monkeypatch, default_user2):
    state.anonymous_mode = True
    automation = schedule_automation(
        query_to_run="/automated_task old automation",
        subject="old subject",
        crontime="0 0 * * *",
        timezone="UTC",
        scheduling_request="old automation",
        user=default_user2,
        calling_url="http://testserver/api/automation",
        conversation_id="legacy-conversation",
    )
    automation.modify(name="legacy plain name")
    monkeypatch.setattr(
        "khoj.routers.api_automation.schedule_query",
        lambda q, chat_history, user: ("0 1 * * *", q, "edited subject"),
    )

    response = client.put(
        "/api/automation",
        params={
            "automation_id": automation.id,
            "q": "edited automation",
            "subject": "edited subject",
            "crontime": "0 1 * * *",
            "timezone": "UTC",
        },
    )

    assert response.status_code == 200
    assert response.json()["scheduling_request"] == "edited automation"
    repaired_name = state.scheduler.get_job(automation.id).name
    assert json.loads(repaired_name)["scheduling_request"] == "edited automation"


@pytest.mark.django_db(transaction=True)
def test_edit_automation_cleans_created_conversation_when_modify_fails(
    client: TestClient, monkeypatch, default_user2
):
    state.anonymous_mode = True
    automation = schedule_automation(
        query_to_run="/automated_task old automation",
        subject="old subject",
        crontime="0 0 * * *",
        timezone="UTC",
        scheduling_request="old automation",
        user=default_user2,
        calling_url="http://testserver/api/automation",
        conversation_id="",
    )
    monkeypatch.setattr(
        "khoj.routers.api_automation.schedule_query",
        lambda q, chat_history, user: ("0 1 * * *", q, "edited subject"),
    )

    def fail_modify(self, **changes):
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(Job, "modify", fail_modify)

    before = Conversation.objects.filter(user=default_user2).count()
    response = client.put(
        "/api/automation",
        params={
            "automation_id": automation.id,
            "q": "edited automation",
            "subject": "edited subject",
            "crontime": "0 1 * * *",
            "timezone": "UTC",
        },
    )

    assert response.status_code == 500
    assert Conversation.objects.filter(user=default_user2).count() == before


@pytest.mark.django_db(transaction=True)
def test_trigger_automation_deletes_job_with_missing_conversation(client: TestClient, default_user2):
    state.anonymous_mode = True
    automation = schedule_automation(
        query_to_run="/automated_task stale automation",
        subject="stale subject",
        crontime="0 0 * * *",
        timezone="UTC",
        scheduling_request="stale automation",
        user=default_user2,
        calling_url="http://testserver/api/automation",
        conversation_id="missing-conversation",
    )

    response = client.post(f"/api/automation/trigger?automation_id={automation.id}")

    assert response.status_code == 404
    assert response.text == "Automation conversation not found"
    assert state.scheduler.get_job(automation.id) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(not get_chat_api_key("google"), reason="Requires GEMINI_API_KEY to be set")
def test_get_automations(client: TestClient):
    """Test that getting a list of automations works."""
    automation_id = create_test_automation(client)

    # Act
    response = client.get("/api/automation")

    # Assert
    assert response.status_code == 200
    automations = response.json()
    assert isinstance(automations, list)
    assert len(automations) > 0
    assert any(a["id"] == automation_id for a in automations)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(not get_chat_api_key("google"), reason="Requires GEMINI_API_KEY to be set")
def test_delete_automation(client: TestClient):
    """Test that deleting an automation works."""
    automation_id = create_test_automation(client)

    # Act
    response = client.delete(f"/api/automation?automation_id={automation_id}")

    # Assert
    assert response.status_code == 200

    # Verify it's gone
    response = client.get("/api/automation")
    assert response.status_code == 200
    automations = response.json()
    assert not any(a["id"] == automation_id for a in automations)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(not get_chat_api_key("google"), reason="Requires GEMINI_API_KEY to be set")
def test_edit_automation(client: TestClient):
    """Test that editing an automation works."""
    automation_id = create_test_automation(client)

    edit_params = {
        "automation_id": automation_id,
        "q": "edited automation",
        "crontime": "0 1 * * *",
        "subject": "edited subject",
        "timezone": "UTC",
    }

    # Act
    response = client.put("/api/automation", params=edit_params)

    # Assert
    if response.status_code != 200:
        print(response.text)
    assert response.status_code == 200
    edited_automation = response.json()
    assert edited_automation["scheduling_request"] == "edited automation"
    assert edited_automation["crontime"] == "0 1 * * *"
    assert edited_automation["subject"] == "edited subject"


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(not get_chat_api_key("google"), reason="Requires GEMINI_API_KEY to be set")
def test_trigger_automation(client: TestClient):
    """Test that triggering an automation works."""
    automation_id = create_test_automation(client)

    # Act
    response = client.post(f"/api/automation/trigger?automation_id={automation_id}")

    # Assert
    assert response.status_code == 200
    # NOTE: We are not testing the execution of the triggered job itself,
    # as that would require a more complex test setup with mocking.
    # A 200 response is sufficient to indicate that the trigger was received.
    assert response.text == "Automation triggered"
