import pytest

from khoj.database.models import Conversation, VaultActionBatch
from khoj.processor.conversation.vault_actions import create_vault_action_batch


@pytest.mark.django_db(transaction=True)
def test_web_vault_capability_requires_enabled_local_root(client, api_user, tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")

    response = client.get(
        "/api/vault/actions/capabilities",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.json()["review_required"] is True
    assert response.json()["allowed_extensions"] == [".md", ".txt"]
    assert len(response.json()["csrf_token"]) >= 32


@pytest.mark.django_db(transaction=True)
def test_web_vault_api_lists_and_applies_owned_batch(client, api_user, tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    auth = {"Authorization": f"Bearer {api_user.token}"}
    capability = client.get("/api/vault/actions/capabilities", headers=auth).json()
    conversation = Conversation.objects.create(user=api_user.user)
    batch = create_vault_action_batch(
        user=api_user.user,
        conversation=conversation,
        turn_id="00000000-0000-0000-0000-000000000010",
        actions=[{"op": "create_file", "path": "daily.md", "content": "# Daily\n", "mode": "create_only"}],
    )

    listed = client.get(f"/api/vault/actions?conversation_id={conversation.id}", headers=auth)
    missing_csrf = client.post(f"/api/vault/actions/{batch.id}/apply", headers=auth)
    applied = client.post(
        f"/api/vault/actions/{batch.id}/apply",
        headers={**auth, "Origin": "http://testserver", "X-Vault-CSRF": capability["csrf_token"]},
    )
    applied_again = client.post(
        f"/api/vault/actions/{batch.id}/apply",
        headers={**auth, "Origin": "http://testserver", "X-Vault-CSRF": capability["csrf_token"]},
    )

    assert listed.status_code == 200
    assert listed.json()[0]["id"] == str(batch.id)
    assert "root_fingerprint" not in listed.json()[0]
    assert "action_digest" not in listed.json()[0]
    assert missing_csrf.status_code == 403
    assert applied.status_code == 200
    assert applied.json()["status"] == "applied"
    assert applied_again.json()["status"] == "applied"
    assert (tmp_path / "daily.md").read_text(encoding="utf-8") == "# Daily\n"


@pytest.mark.django_db(transaction=True)
def test_web_vault_api_enforces_flag_origin_and_owner(client, api_user, api_user3, tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "false")
    owner_auth = {"Authorization": f"Bearer {api_user.token}"}

    disabled = client.get("/api/vault/actions/capabilities", headers=owner_auth)
    unauthenticated = client.get("/api/vault/actions/capabilities")

    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert unauthenticated.status_code == 403

    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    capability = client.get("/api/vault/actions/capabilities", headers=owner_auth).json()
    conversation = Conversation.objects.create(user=api_user.user)
    batch = create_vault_action_batch(
        user=api_user.user,
        conversation=conversation,
        turn_id="00000000-0000-0000-0000-000000000011",
        actions=[{"op": "create_file", "path": "owner.md", "content": "owner", "mode": "create_only"}],
    )
    csrf = capability["csrf_token"]

    bad_origin = client.post(
        f"/api/vault/actions/{batch.id}/apply",
        headers={**owner_auth, "Origin": "https://evil.example", "X-Vault-CSRF": csrf},
    )
    other_user = client.post(
        f"/api/vault/actions/{batch.id}/apply",
        headers={
            "Authorization": f"Bearer {api_user3.token}",
            "Origin": "http://testserver",
            "X-Vault-CSRF": csrf,
        },
    )

    assert bad_origin.status_code == 403
    assert other_user.status_code == 404
    assert not (tmp_path / "owner.md").exists()


@pytest.mark.django_db(transaction=True)
def test_web_vault_api_cancels_batch_idempotently(client, api_user, tmp_path, monkeypatch):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    auth = {"Authorization": f"Bearer {api_user.token}"}
    capability = client.get("/api/vault/actions/capabilities", headers=auth).json()
    conversation = Conversation.objects.create(user=api_user.user)
    batch = create_vault_action_batch(
        user=api_user.user,
        conversation=conversation,
        turn_id="00000000-0000-0000-0000-000000000012",
        actions=[{"op": "create_file", "path": "cancelled.md", "content": "never", "mode": "create_only"}],
    )
    headers = {
        **auth,
        "Origin": "http://testserver",
        "X-Vault-CSRF": capability["csrf_token"],
    }

    cancelled = client.post(f"/api/vault/actions/{batch.id}/cancel", headers=headers)
    cancelled_again = client.post(f"/api/vault/actions/{batch.id}/cancel", headers=headers)

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled_again.status_code == 200
    assert cancelled_again.json()["status"] == "cancelled"
    assert not (tmp_path / "cancelled.md").exists()


@pytest.mark.parametrize(
    "status",
    [VaultActionBatch.Status.FAILED, VaultActionBatch.Status.MANUAL_REVIEW_REQUIRED],
)
@pytest.mark.django_db(transaction=True)
def test_conversation_delete_preserves_unresolved_vault_batch(client, api_user, tmp_path, monkeypatch, status):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    conversation = Conversation.objects.create(user=api_user.user)
    batch = create_vault_action_batch(
        user=api_user.user,
        conversation=conversation,
        turn_id="00000000-0000-0000-0000-000000000013",
        actions=[{"op": "create_file", "path": "review.md", "content": "review", "mode": "create_only"}],
    )
    batch.status = status
    batch.rollback_journal = {"files": {"review.md": {"final_sha256": "unknown"}}}
    batch.save(update_fields=["status", "rollback_journal"])

    response = client.delete(
        f"/api/chat/history?conversation_id={conversation.id}",
        headers={"Authorization": f"Bearer {api_user.token}"},
    )

    assert response.status_code == 409
    assert Conversation.objects.filter(id=conversation.id).exists()
    batch.refresh_from_db()
    assert batch.status == status
    assert batch.rollback_journal["files"]
