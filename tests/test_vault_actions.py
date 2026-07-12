import os
import uuid
from datetime import timedelta
from threading import Event, Thread

import pytest
from django.db import close_old_connections
from django.db.models.signals import pre_delete
from django.utils import timezone

from khoj.database.adapters import ConversationAdapters
from khoj.database.models import Conversation, VaultActionBatch
from khoj.processor.conversation import vault_actions as vault_action_module
from khoj.processor.conversation.vault_actions import (
    VaultActionError,
    apply_vault_action_batch,
    cancel_vault_action_batch,
    create_vault_action_batch,
    delete_conversations_with_vault_protection,
    expire_vault_action_batch_if_pending,
    prepare_vault_action_batch,
    recover_vault_action_batch,
)


def test_prepare_create_file_batch_is_non_mutating(tmp_path):
    result = prepare_vault_action_batch(
        tmp_path,
        [
            {
                "op": "create_file",
                "path": "daily/2026-07-10.md",
                "content": "# 2026-07-10 每日计划\n",
                "mode": "create_only",
            }
        ],
    )

    assert result.snapshots == {"daily/2026-07-10.md": {"exists": False, "sha256": None}}
    assert result.final_contents == {"daily/2026-07-10.md": "# 2026-07-10 每日计划\n"}
    assert result.previews[0]["path"] == "daily/2026-07-10.md"
    assert "+# 2026-07-10 每日计划" in result.previews[0]["diff"]
    assert result.previews[0]["truncated"] is False
    assert not (tmp_path / "daily" / "2026-07-10.md").exists()


def test_prepare_append_file_inserts_under_named_heading(tmp_path):
    target = tmp_path / "daily.md"
    target.write_text("# Daily\n\n## 今日计划\n\n- old\n\n## 复盘\n\n", encoding="utf-8")

    result = prepare_vault_action_batch(
        tmp_path,
        [
            {
                "op": "append_file",
                "path": "daily.md",
                "content": "- new",
                "heading": "今日计划",
                "mode": "append",
            }
        ],
    )

    assert result.final_contents["daily.md"] == "# Daily\n\n## 今日计划\n\n- old\n\n- new\n## 复盘\n\n"
    assert target.read_text(encoding="utf-8") == "# Daily\n\n## 今日计划\n\n- old\n\n## 复盘\n\n"


def test_prepare_replace_text_requires_one_exact_match(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("Redis old answer\n", encoding="utf-8")

    result = prepare_vault_action_batch(
        tmp_path,
        [
            {
                "op": "replace_text",
                "path": "notes.md",
                "find": "old answer",
                "replace": "final answer",
                "mode": "replace",
                "reason": "finish the card",
            }
        ],
    )

    assert result.final_contents == {"notes.md": "Redis final answer\n"}
    assert "-Redis old answer" in result.previews[0]["diff"]
    assert "+Redis final answer" in result.previews[0]["diff"]


def test_prepare_replays_multiple_actions_on_the_same_file(tmp_path):
    result = prepare_vault_action_batch(
        tmp_path,
        [
            {
                "op": "create_file",
                "path": "daily/2026-07-10.md",
                "content": "# Daily\n",
                "mode": "create_only",
            },
            {
                "op": "append_file",
                "path": "daily/2026-07-10.md",
                "content": "- [ ] Redis",
                "mode": "append",
            },
        ],
    )

    assert result.final_contents == {"daily/2026-07-10.md": "# Daily\n- [ ] Redis\n"}


@pytest.mark.parametrize("path", ["/tmp/a.md", "../a.md", ".hidden/a.md", r"C:\a.md", "daily/a.pdf"])
def test_prepare_rejects_unsafe_or_unsupported_paths(tmp_path, path):
    with pytest.raises(VaultActionError):
        prepare_vault_action_batch(
            tmp_path,
            [{"op": "create_file", "path": path, "content": "x", "mode": "create_only"}],
        )


def test_prepare_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside-vault-action.md"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "escape.md").symlink_to(outside)

    with pytest.raises(VaultActionError, match="escapes|regular file"):
        prepare_vault_action_batch(
            tmp_path,
            [{"op": "append_file", "path": "escape.md", "content": "x", "mode": "append"}],
        )


def test_prepare_rejects_unknown_fields_and_ambiguous_replace(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("same same", encoding="utf-8")

    with pytest.raises(VaultActionError, match="Invalid vault action batch"):
        prepare_vault_action_batch(
            tmp_path,
            [{"op": "append_file", "path": "notes.md", "content": "x", "mode": "append", "guess": True}],
        )
    with pytest.raises(VaultActionError, match="found 2"):
        prepare_vault_action_batch(
            tmp_path,
            [{"op": "replace_text", "path": "notes.md", "find": "same", "replace": "new", "mode": "replace"}],
        )


def test_prepare_bounds_action_count_content_and_preview(tmp_path, monkeypatch):
    too_many = [
        {"op": "create_file", "path": f"{index}.md", "content": "x", "mode": "create_only"} for index in range(21)
    ]
    with pytest.raises(VaultActionError, match="between 1 and 20"):
        prepare_vault_action_batch(tmp_path, too_many)

    with pytest.raises(VaultActionError, match="content exceeds"):
        prepare_vault_action_batch(
            tmp_path,
            [{"op": "create_file", "path": "large.md", "content": "x" * (1024 * 1024 + 1), "mode": "create_only"}],
        )

    monkeypatch.setattr("khoj.processor.conversation.vault_actions.MAX_PREVIEW_BYTES", 20)
    bounded = prepare_vault_action_batch(
        tmp_path,
        [{"op": "create_file", "path": "preview.md", "content": "long preview content\n", "mode": "create_only"}],
    )
    assert bounded.previews[0]["truncated"] is True
    assert len(bounded.previews[0]["diff"].encode("utf-8")) <= 20


def test_vault_action_batch_persists_and_cascades(default_user):
    conversation = Conversation.objects.create(user=default_user)
    turn_id = uuid.uuid4()
    batch = VaultActionBatch.objects.create(
        user=default_user,
        conversation=conversation,
        turn_id=turn_id,
        actions=[{"op": "create_file", "path": "daily/2026-07-10.md", "content": "plan", "mode": "create_only"}],
        snapshots={"daily/2026-07-10.md": {"exists": False, "sha256": None}},
        previews=[{"op": "create_file", "path": "daily/2026-07-10.md", "diff": "+plan", "truncated": False}],
        root_fingerprint="a" * 64,
        action_digest="b" * 64,
        expires_at=timezone.now() + timedelta(minutes=30),
    )

    assert batch.id
    assert batch.turn_id == turn_id
    assert batch.status == VaultActionBatch.Status.PENDING
    assert batch.rollback_journal == {}
    assert batch.result == {}

    conversation.delete()

    assert not VaultActionBatch.objects.filter(id=batch.id).exists()


def test_create_vault_action_batch_records_server_owned_actions(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    conversation = Conversation.objects.create(user=default_user)

    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "create_file", "path": "daily.md", "content": "# Daily\n", "mode": "create_only"}],
    )

    assert batch.status == VaultActionBatch.Status.PENDING
    assert batch.actions == [{"op": "create_file", "path": "daily.md", "content": "# Daily\n", "mode": "create_only"}]
    assert batch.expires_at > timezone.now()
    assert batch.previews[0]["path"] == "daily.md"
    assert len(batch.root_fingerprint) == 64
    assert len(batch.action_digest) == 64


def test_create_vault_action_batch_is_idempotent_per_turn(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    conversation = Conversation.objects.create(user=default_user)
    turn_id = uuid.uuid4()
    actions = [{"op": "create_file", "path": "daily.md", "content": "# Daily\n", "mode": "create_only"}]

    first = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=turn_id,
        actions=actions,
    )
    applied = apply_vault_action_batch(first.id, default_user)
    repeated = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=turn_id,
        actions=actions,
    )

    assert repeated.id == first.id
    assert applied.status == VaultActionBatch.Status.APPLIED
    assert repeated.status == VaultActionBatch.Status.APPLIED
    assert VaultActionBatch.objects.filter(conversation=conversation, turn_id=turn_id).count() == 1

    with pytest.raises(VaultActionError, match="different vault action batch"):
        create_vault_action_batch(
            user=default_user,
            conversation=conversation,
            turn_id=turn_id,
            actions=[{"op": "create_file", "path": "other.md", "content": "other", "mode": "create_only"}],
        )


def test_apply_rejects_batch_when_configured_vault_changes(tmp_path, monkeypatch, default_user):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(first_root))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    conversation = Conversation.objects.create(user=default_user)
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "create_file", "path": "daily.md", "content": "# Daily\n", "mode": "create_only"}],
    )

    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(second_root))
    rejected = apply_vault_action_batch(batch.id, default_user)

    assert rejected.status == VaultActionBatch.Status.CONFLICT
    assert rejected.result["error"] == "Configured vault changed after this batch was prepared."
    assert not (first_root / "daily.md").exists()
    assert not (second_root / "daily.md").exists()


def test_apply_vault_action_batch_is_atomic_and_idempotent(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    target = tmp_path / "daily.md"
    target.write_text("# Daily\n", encoding="utf-8")
    conversation = Conversation.objects.create(user=default_user)
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "append_file", "path": "daily.md", "content": "- [ ] Redis", "mode": "append"}],
    )

    applied = apply_vault_action_batch(batch.id, default_user)
    applied_again = apply_vault_action_batch(batch.id, default_user)

    assert applied.status == VaultActionBatch.Status.APPLIED
    assert applied.result["files"] == ["daily.md"]
    assert len(applied.result["recovery_artifacts"]) == 1
    recovery_artifact = tmp_path / applied.result["recovery_artifacts"][0]
    assert recovery_artifact.read_text(encoding="utf-8") == "# Daily\n"
    assert applied.rollback_journal == {}
    assert applied_again.status == VaultActionBatch.Status.APPLIED
    assert target.read_text(encoding="utf-8") == "# Daily\n- [ ] Redis\n"


def test_cancel_vault_action_batch_is_idempotent(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    conversation = Conversation.objects.create(user=default_user)
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "create_file", "path": "daily.md", "content": "# Daily\n", "mode": "create_only"}],
    )

    cancelled = cancel_vault_action_batch(batch.id, default_user)
    cancelled_again = cancel_vault_action_batch(batch.id, default_user)

    assert cancelled.status == VaultActionBatch.Status.CANCELLED
    assert cancelled_again.status == VaultActionBatch.Status.CANCELLED
    assert not (tmp_path / "daily.md").exists()


def test_apply_detects_expiry_and_file_drift(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    target = tmp_path / "daily.md"
    target.write_text("original\n", encoding="utf-8")
    conversation = Conversation.objects.create(user=default_user)
    conflict = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "append_file", "path": "daily.md", "content": "planned", "mode": "append"}],
    )
    target.write_text("changed by user\n", encoding="utf-8")

    conflicted = apply_vault_action_batch(conflict.id, default_user)

    assert conflicted.status == VaultActionBatch.Status.CONFLICT
    assert target.read_text(encoding="utf-8") == "changed by user\n"

    expired = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "create_file", "path": "expired.md", "content": "x", "mode": "create_only"}],
    )
    expired.expires_at = timezone.now() - timedelta(seconds=1)
    expired.save(update_fields=["expires_at"])

    expired = apply_vault_action_batch(expired.id, default_user)

    assert expired.status == VaultActionBatch.Status.EXPIRED
    assert not (tmp_path / "expired.md").exists()


def test_apply_preserves_external_edit_made_immediately_before_replace(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    target = tmp_path / "daily.md"
    target.write_text("original\n", encoding="utf-8")
    conversation = Conversation.objects.create(user=default_user)
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "append_file", "path": "daily.md", "content": "planned", "mode": "append"}],
    )
    real_atomic_write = vault_action_module._atomic_write_text

    def external_edit_then_write(path, content, *, expected=None, recovery_id=None):
        path.write_text("external edit\n", encoding="utf-8")
        real_atomic_write(path, content, expected=expected, recovery_id=recovery_id)

    monkeypatch.setattr(vault_action_module, "_atomic_write_text", external_edit_then_write)
    result = apply_vault_action_batch(batch.id, default_user)

    assert result.status == VaultActionBatch.Status.MANUAL_REVIEW_REQUIRED
    assert target.read_text(encoding="utf-8") == "external edit\n"


def test_atomic_exchange_preserves_edit_after_the_last_precheck(tmp_path, monkeypatch):
    target = tmp_path / "daily.md"
    target.write_text("original\n", encoding="utf-8")
    real_exchange = vault_action_module._exchange_paths

    def external_edit_then_exchange(left, right):
        right.write_text("external edit in final window\n", encoding="utf-8")
        real_exchange(left, right)

    monkeypatch.setattr(vault_action_module, "_exchange_paths", external_edit_then_exchange)

    with pytest.raises(VaultActionError, match="changed during atomic apply"):
        vault_action_module._atomic_write_text(
            target,
            "planned\n",
            expected={"exists": True, "sha256": vault_action_module._sha256("original\n")},
        )

    assert target.read_text(encoding="utf-8") == "external edit in final window\n"


def test_rollback_delete_preserves_late_write_through_open_descriptor(tmp_path):
    target = tmp_path / "created.md"
    target.write_text("batch content\n", encoding="utf-8")
    open_file = target.open("r+", encoding="utf-8")
    try:
        recovery_artifact = vault_action_module._atomic_delete_if_matches(
            target,
            vault_action_module._sha256("batch content\n"),
        )
        open_file.seek(0)
        open_file.write("late external edit\n")
        open_file.truncate()
        open_file.flush()
        os.fsync(open_file.fileno())
    finally:
        open_file.close()

    assert not target.exists()
    assert recovery_artifact.read_text(encoding="utf-8") == "late external edit\n"


def test_recovery_artifact_name_stays_within_filesystem_limit_for_long_target(tmp_path):
    target = tmp_path / f"{'a' * 180}.md"
    target.write_text("original\n", encoding="utf-8")

    recovery_artifact = vault_action_module._atomic_write_text(
        target,
        "planned\n",
        expected={"exists": True, "sha256": vault_action_module._sha256("original\n")},
        recovery_id=str(uuid.uuid4()),
    )

    assert target.read_text(encoding="utf-8") == "planned\n"
    assert recovery_artifact is not None
    assert len(recovery_artifact.name.encode("utf-8")) <= 255
    assert recovery_artifact.read_text(encoding="utf-8") == "original\n"


def test_deleting_turn_cancels_batch_before_it_can_apply(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    turn_id = str(uuid.uuid4())
    conversation = Conversation.objects.create(
        user=default_user,
        conversation_log={"chat": [{"by": "you", "message": "write", "turnId": turn_id}]},
    )
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=turn_id,
        actions=[{"op": "create_file", "path": "daily.md", "content": "plan", "mode": "create_only"}],
    )

    deleted = ConversationAdapters.delete_message_by_turn_id(default_user, str(conversation.id), turn_id)

    batch.refresh_from_db()
    assert deleted is True
    assert batch.status == VaultActionBatch.Status.CANCELLED
    with pytest.raises(VaultActionError, match="status cancelled"):
        apply_vault_action_batch(batch.id, default_user)
    assert not (tmp_path / "daily.md").exists()


def test_deleting_conversation_cancels_pending_batch_before_cascade(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    conversation = Conversation.objects.create(user=default_user)
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "create_file", "path": "daily.md", "content": "plan", "mode": "create_only"}],
    )
    observed_statuses = []

    def record_batch_status_before_conversation_delete(**_):
        observed_statuses.append(VaultActionBatch.objects.get(id=batch.id).status)

    pre_delete.connect(
        record_batch_status_before_conversation_delete,
        sender=Conversation,
        dispatch_uid="test_pending_vault_batch_cancelled_before_conversation_delete",
        weak=False,
    )
    try:
        deleted_count, _ = delete_conversations_with_vault_protection(
            user=default_user,
            conversation_id=conversation.id,
        )
    finally:
        pre_delete.disconnect(
            sender=Conversation,
            dispatch_uid="test_pending_vault_batch_cancelled_before_conversation_delete",
        )

    assert deleted_count > 0
    assert observed_statuses == [VaultActionBatch.Status.CANCELLED]
    assert not Conversation.objects.filter(id=conversation.id).exists()
    assert not (tmp_path / "daily.md").exists()


def test_expire_pending_batch_does_not_overwrite_terminal_status(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    conversation = Conversation.objects.create(user=default_user)
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "create_file", "path": "daily.md", "content": "# Daily\n", "mode": "create_only"}],
    )
    VaultActionBatch.objects.filter(id=batch.id).update(
        status=VaultActionBatch.Status.APPLIED,
        result={"files": ["daily.md"]},
    )

    refreshed = expire_vault_action_batch_if_pending(batch.id, default_user)

    assert refreshed.status == VaultActionBatch.Status.APPLIED
    assert refreshed.result == {"files": ["daily.md"]}


def test_recover_incomplete_batch_rolls_back_partial_commit(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    (tmp_path / "a.md").write_text("old a\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("old b\n", encoding="utf-8")
    conversation = Conversation.objects.create(user=default_user)
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[
            {"op": "replace_text", "path": "a.md", "find": "old a", "replace": "new a", "mode": "replace"},
            {"op": "replace_text", "path": "b.md", "find": "old b", "replace": "new b", "mode": "replace"},
        ],
    )
    real_atomic_write = vault_action_module._atomic_write_text
    call_count = 0

    def crash_on_second_file(target, content, *, expected=None, recovery_id=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise SystemExit("simulated process exit")
        real_atomic_write(target, content, expected=expected, recovery_id=recovery_id)

    monkeypatch.setattr(vault_action_module, "_atomic_write_text", crash_on_second_file)
    with pytest.raises(SystemExit, match="simulated process exit"):
        apply_vault_action_batch(batch.id, default_user)
    batch.refresh_from_db()
    assert batch.status == VaultActionBatch.Status.APPLYING
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "new a\n"
    assert (tmp_path / "b.md").read_text(encoding="utf-8") == "old b\n"

    monkeypatch.setattr(vault_action_module, "_atomic_write_text", real_atomic_write)
    recovered = recover_vault_action_batch(batch)

    assert recovered.status == VaultActionBatch.Status.FAILED
    assert recovered.result["recovery"] == "rolled_back"
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "old a\n"
    assert (tmp_path / "b.md").read_text(encoding="utf-8") == "old b\n"


def test_recovery_rediscovers_artifact_created_before_process_exit(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    target = tmp_path / "daily.md"
    target.write_text("original\n", encoding="utf-8")
    conversation = Conversation.objects.create(user=default_user)
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "append_file", "path": "daily.md", "content": "planned", "mode": "append"}],
    )
    prepared = prepare_vault_action_batch(tmp_path, batch.actions)
    journal = vault_action_module._build_rollback_journal(tmp_path, prepared, str(batch.id))
    artifact = tmp_path / f".offeragent-recovery-{batch.id}-rollback-deadbeef-daily.md"
    artifact.write_text("displaced batch content\n", encoding="utf-8")
    batch.status = VaultActionBatch.Status.APPLYING
    batch.rollback_journal = journal
    batch.save(update_fields=["status", "rollback_journal"])

    recovered = recover_vault_action_batch(batch)

    assert recovered.status == VaultActionBatch.Status.FAILED
    assert recovered.result["recovery_artifacts"] == [artifact.name]
    assert recovered.rollback_journal == {}


def test_recovery_never_uses_a_different_configured_vault(tmp_path, monkeypatch, default_user):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(first_root))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    conversation = Conversation.objects.create(user=default_user)
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "create_file", "path": "daily.md", "content": "# Daily\n", "mode": "create_only"}],
    )
    real_commit = vault_action_module._commit_prepared

    def crash_after_commit(root, prepared, *, recovery_id=None):
        real_commit(root, prepared, recovery_id=recovery_id)
        raise SystemExit("simulated exit after file commit")

    monkeypatch.setattr(vault_action_module, "_commit_prepared", crash_after_commit)
    with pytest.raises(SystemExit, match="simulated exit"):
        apply_vault_action_batch(batch.id, default_user)
    batch.refresh_from_db()
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(second_root))

    recovered = recover_vault_action_batch(batch)

    assert recovered.status == VaultActionBatch.Status.MANUAL_REVIEW_REQUIRED
    assert "Configured vault changed" in recovered.result["error"]
    assert (first_root / "daily.md").read_text(encoding="utf-8") == "# Daily\n"
    assert not (second_root / "daily.md").exists()


@pytest.mark.django_db(transaction=True)
def test_recovery_waits_for_an_active_apply(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    conversation = Conversation.objects.create(user=default_user)
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "create_file", "path": "daily.md", "content": "# Daily\n", "mode": "create_only"}],
    )
    real_commit = vault_action_module._commit_prepared
    commit_started = Event()
    release_commit = Event()
    recovery_finished = Event()
    recovery_attempting = Event()
    results = {}
    errors = []

    def blocking_commit(root, prepared, *, recovery_id=None):
        commit_started.set()
        if not release_commit.wait(timeout=5):
            raise TimeoutError("test did not release active apply")
        return real_commit(root, prepared, recovery_id=recovery_id)

    def apply_in_thread():
        close_old_connections()
        try:
            results["applied"] = apply_vault_action_batch(batch.id, type(default_user).objects.get(id=default_user.id))
        except BaseException as error:
            errors.append(error)
        finally:
            close_old_connections()

    def recover_in_thread():
        close_old_connections()
        try:
            current = VaultActionBatch.objects.select_related("user").get(id=batch.id)
            recovery_attempting.set()
            results["recovered"] = recover_vault_action_batch(current)
        except BaseException as error:
            errors.append(error)
        finally:
            recovery_finished.set()
            close_old_connections()

    monkeypatch.setattr(vault_action_module, "_commit_prepared", blocking_commit)
    apply_thread = Thread(target=apply_in_thread)
    recovery_thread = Thread(target=recover_in_thread)
    apply_thread.start()
    assert commit_started.wait(timeout=5)
    recovery_thread.start()

    assert recovery_attempting.wait(timeout=5)
    assert not recovery_finished.wait(timeout=1)
    release_commit.set()
    apply_thread.join(timeout=5)
    recovery_thread.join(timeout=5)

    assert not apply_thread.is_alive()
    assert not recovery_thread.is_alive()
    assert errors == []
    assert results["applied"].status == VaultActionBatch.Status.APPLIED
    assert results["recovered"].status == VaultActionBatch.Status.APPLIED
    assert (tmp_path / "daily.md").read_text(encoding="utf-8") == "# Daily\n"


@pytest.mark.django_db(transaction=True)
def test_different_batches_for_same_file_cannot_both_apply(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    target = tmp_path / "daily.md"
    target.write_text("# Daily\n", encoding="utf-8")
    conversation = Conversation.objects.create(user=default_user)
    first = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "append_file", "path": "daily.md", "content": "first", "mode": "append"}],
    )
    second = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "append_file", "path": "daily.md", "content": "second", "mode": "append"}],
    )
    real_commit = vault_action_module._commit_prepared
    first_commit_started = Event()
    release_first_commit = Event()
    second_finished = Event()
    commit_calls = 0
    results = []
    errors = []

    def block_first_commit(root, prepared, *, recovery_id=None):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            first_commit_started.set()
            if not release_first_commit.wait(timeout=5):
                raise TimeoutError("test did not release first commit")
        return real_commit(root, prepared, recovery_id=recovery_id)

    def apply_in_thread(batch_id, finished=None):
        close_old_connections()
        try:
            user = type(default_user).objects.get(id=default_user.id)
            results.append(apply_vault_action_batch(batch_id, user))
        except BaseException as error:
            errors.append(error)
        finally:
            if finished:
                finished.set()
            close_old_connections()

    monkeypatch.setattr(vault_action_module, "_commit_prepared", block_first_commit)
    first_thread = Thread(target=apply_in_thread, args=(first.id,))
    second_thread = Thread(target=apply_in_thread, args=(second.id, second_finished))
    first_thread.start()
    assert first_commit_started.wait(timeout=5)
    second_thread.start()

    assert not second_finished.wait(timeout=1)
    release_first_commit.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert errors == []
    assert sorted(result.status for result in results) == [
        VaultActionBatch.Status.APPLIED,
        VaultActionBatch.Status.CONFLICT,
    ]
    assert target.read_text(encoding="utf-8") in {"# Daily\nfirst\n", "# Daily\nsecond\n"}


@pytest.mark.django_db(transaction=True)
def test_turn_deletion_waits_for_an_apply_that_already_started(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    turn_id = str(uuid.uuid4())
    conversation = Conversation.objects.create(
        user=default_user,
        conversation_log={"chat": [{"by": "you", "message": "write", "turnId": turn_id}]},
    )
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=turn_id,
        actions=[{"op": "create_file", "path": "daily.md", "content": "plan", "mode": "create_only"}],
    )
    real_commit = vault_action_module._commit_prepared
    commit_started = Event()
    release_commit = Event()
    deletion_finished = Event()
    results = {}
    errors = []

    def blocking_commit(root, prepared, *, recovery_id=None):
        commit_started.set()
        if not release_commit.wait(timeout=5):
            raise TimeoutError("test did not release apply")
        return real_commit(root, prepared, recovery_id=recovery_id)

    def apply_in_thread():
        close_old_connections()
        try:
            user = type(default_user).objects.get(id=default_user.id)
            results["applied"] = apply_vault_action_batch(batch.id, user)
        except BaseException as error:
            errors.append(error)
        finally:
            close_old_connections()

    def delete_in_thread():
        close_old_connections()
        try:
            user = type(default_user).objects.get(id=default_user.id)
            results["deleted"] = ConversationAdapters.delete_message_by_turn_id(
                user,
                str(conversation.id),
                turn_id,
            )
        except BaseException as error:
            errors.append(error)
        finally:
            deletion_finished.set()
            close_old_connections()

    monkeypatch.setattr(vault_action_module, "_commit_prepared", blocking_commit)
    apply_thread = Thread(target=apply_in_thread)
    delete_thread = Thread(target=delete_in_thread)
    apply_thread.start()
    assert commit_started.wait(timeout=5)
    delete_thread.start()

    assert not deletion_finished.wait(timeout=1)
    release_commit.set()
    apply_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    conversation.refresh_from_db()
    assert errors == []
    assert results["applied"].status == VaultActionBatch.Status.APPLIED
    assert results["deleted"] is True
    assert conversation.conversation_log["chat"] == []
    assert (tmp_path / "daily.md").read_text(encoding="utf-8") == "plan"


@pytest.mark.django_db(transaction=True)
def test_conversation_deletion_waits_for_an_apply_that_already_started(tmp_path, monkeypatch, default_user):
    monkeypatch.setenv("KHOJ_LOCAL_KB_PATH", str(tmp_path))
    monkeypatch.setenv("KHOJ_ALLOW_VAULT_WRITE", "true")
    conversation = Conversation.objects.create(user=default_user)
    batch = create_vault_action_batch(
        user=default_user,
        conversation=conversation,
        turn_id=uuid.uuid4(),
        actions=[{"op": "create_file", "path": "daily.md", "content": "plan", "mode": "create_only"}],
    )
    real_commit = vault_action_module._commit_prepared
    commit_started = Event()
    release_commit = Event()
    deletion_finished = Event()
    results = {}
    errors = []

    def blocking_commit(root, prepared, *, recovery_id=None):
        commit_started.set()
        if not release_commit.wait(timeout=5):
            raise TimeoutError("test did not release apply")
        return real_commit(root, prepared, recovery_id=recovery_id)

    def apply_in_thread():
        close_old_connections()
        try:
            user = type(default_user).objects.get(id=default_user.id)
            results["applied"] = apply_vault_action_batch(batch.id, user)
        except BaseException as error:
            errors.append(error)
        finally:
            close_old_connections()

    def delete_in_thread():
        close_old_connections()
        try:
            user = type(default_user).objects.get(id=default_user.id)
            results["deleted"] = delete_conversations_with_vault_protection(
                user=user,
                conversation_id=conversation.id,
            )
        except BaseException as error:
            errors.append(error)
        finally:
            deletion_finished.set()
            close_old_connections()

    monkeypatch.setattr(vault_action_module, "_commit_prepared", blocking_commit)
    apply_thread = Thread(target=apply_in_thread)
    delete_thread = Thread(target=delete_in_thread)
    apply_thread.start()
    assert commit_started.wait(timeout=5)
    delete_thread.start()

    assert not deletion_finished.wait(timeout=1)
    release_commit.set()
    apply_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    assert errors == []
    assert results["applied"].status == VaultActionBatch.Status.APPLIED
    assert results["deleted"][0] > 0
    assert not Conversation.objects.filter(id=conversation.id).exists()
    assert (tmp_path / "daily.md").read_text(encoding="utf-8") == "plan"
