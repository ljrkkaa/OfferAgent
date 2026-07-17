from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from typing_extensions import Never

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.migration import (
    LegacyMigrationError,
    LegacyMigrationRequest,
    migrate_legacy_obsidian,
)
from offeragent_harness.protocol.content import ImageContentBlock, TextContentBlock
from offeragent_harness.protocol.events import stored_event_to_envelope
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.runtime.conversation_attachments import AttachmentLimits, ConversationAttachmentStore
from offeragent_harness.runtime.conversation_projection import UowConversationProjectionService
from offeragent_harness.runtime.session_service import SessionLifecycleService, SessionListCommand
from offeragent_harness.runtime.turn_manager import TurnManager
from offeragent_harness.sessions import Session, SessionStatus
from offeragent_harness.testing import DeterministicIdGenerator, ManualClock, RecordingEventSink

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def _legacy_state(root: Path, *, version: int = 19, corrupt_image: bool = False) -> tuple[Path, Path]:
    state = root / "state.db"
    attachments = root / "attachments"
    attachments.mkdir(parents=True)
    with sqlite3.connect(state) as connection:
        connection.executescript(
            """
            CREATE TABLE conversations (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, model_id TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              title_origin TEXT, archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE agent_runs (
              id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, model_id TEXT NOT NULL,
              status TEXT NOT NULL, user_message_id TEXT, assistant_message_id TEXT,
              error_code TEXT, error_message TEXT, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, last_sequence INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE messages (
              id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, agent_run_id TEXT NOT NULL,
              role TEXT NOT NULL, text TEXT NOT NULL, sequence INTEGER NOT NULL,
              created_at TEXT NOT NULL, citations_json TEXT NOT NULL DEFAULT '[]',
              attachments_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE run_attachments (
              id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, agent_run_id TEXT NOT NULL,
              content_hash TEXT NOT NULL, file_name TEXT NOT NULL, media_type TEXT NOT NULL,
              size INTEGER NOT NULL, created_at TEXT NOT NULL, message_id TEXT,
              message_order INTEGER, owned_at TEXT
            );
            CREATE TABLE run_checkpoints (id TEXT PRIMARY KEY, checkpoint_json TEXT NOT NULL);
            CREATE TABLE tool_calls (id TEXT PRIMARY KEY, arguments_json TEXT NOT NULL);
            CREATE TABLE vault_change_batches (id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE settings_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            """
        )
        connection.execute(f"PRAGMA user_version = {version}")
        connection.executemany(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            [(item, "2026-07-01T00:00:00Z") for item in range(1, version + 1)],
        )
        connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "conv_done",
                "Imported interview",
                "legacy-model",
                "2026-07-01T08:00:00Z",
                "2026-07-01T08:02:00Z",
                "user",
                0,
            ),
        )
        connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("conv_active", "Do not import", "legacy-model", "2026-07-02T08:00:00Z", "2026-07-02T08:02:00Z", "user", 0),
        )
        connection.execute(
            "INSERT INTO agent_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run_done",
                "conv_done",
                "legacy-model",
                "completed",
                "msg_user",
                "msg_agent",
                None,
                None,
                "2026-07-01T08:00:00Z",
                "2026-07-01T08:02:00Z",
                7,
            ),
        )
        connection.execute(
            "INSERT INTO agent_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run_active",
                "conv_active",
                "legacy-model",
                "interrupted",
                "msg_active",
                None,
                None,
                None,
                "2026-07-02T08:00:00Z",
                "2026-07-02T08:02:00Z",
                2,
            ),
        )
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "msg_user",
                "conv_done",
                "run_done",
                "user",
                "Explain this screenshot",
                1,
                "2026-07-01T08:00:00Z",
                "[]",
                "[]",
            ),
        )
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "msg_agent",
                "conv_done",
                "run_done",
                "assistant",
                "It shows a migration boundary.",
                2,
                "2026-07-01T08:02:00Z",
                "[]",
                "[]",
            ),
        )
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("msg_active", "conv_active", "run_active", "user", "unfinished", 1, "2026-07-02T08:00:00Z", "[]", "[]"),
        )
        content = b"not-an-image" if corrupt_image else PNG
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        connection.execute(
            "INSERT INTO run_attachments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy_art",
                "conv_done",
                "run_done",
                digest,
                "screen.png",
                "image/png",
                len(content),
                "2026-07-01T08:00:00Z",
                "msg_user",
                0,
                "2026-07-01T08:00:01Z",
            ),
        )
        connection.execute("INSERT INTO run_checkpoints VALUES ('checkpoint_1', '{}')")
        connection.execute("INSERT INTO tool_calls VALUES ('tool_1', '{}')")
        connection.execute("INSERT INTO vault_change_batches VALUES ('batch_1', 'pending')")
        connection.execute("INSERT INTO settings_metadata VALUES ('provider_token', 'secret', '2026-07-01T00:00:00Z')")
    (attachments / "legacy_art").write_bytes(content)
    return state, attachments


def _request(tmp_path: Path, *, version: int = 19, corrupt_image: bool = False) -> LegacyMigrationRequest:
    source, attachments = _legacy_state(tmp_path / "legacy", version=version, corrupt_image=corrupt_image)
    plugin_data = tmp_path / "legacy-plugin-data.json"
    plugin_data.write_text(
        json.dumps({"fastMode": True, "vaultPermissionMode": "ask_every_time", "apiToken": "drop-me"}), encoding="utf-8"
    )
    templates = tmp_path / "templates"
    (templates / "obsidian-cli").mkdir(parents=True)
    (templates / "agent.md").write_text("# OfferAgent Contract\n", encoding="utf-8")
    (templates / "obsidian-cli" / "SKILL.md").write_text("# OfferAgent Vault Tools\n", encoding="utf-8")
    vault = tmp_path / "Vault"
    vault.mkdir()
    target_plugin_data = vault / ".obsidian" / "plugins" / "offeragent-obsidian-plugin" / "data.json"
    return LegacyMigrationRequest(
        source_state=source,
        source_attachments=attachments,
        source_plugin_data=plugin_data,
        target_state_directory=tmp_path / "local" / "workspaces" / "wsi_test",
        target_plugin_data=target_plugin_data,
        vault_root=vault,
        target_templates=templates,
        workspace_id="ws_test",
        profile_id="profile_local",
    )


def test_dry_run_then_transactional_import_is_replayable_and_idempotent(tmp_path: Path) -> None:
    request = _request(tmp_path)

    dry_run = migrate_legacy_obsidian(request, dry_run=True)

    assert dry_run.changed is False
    assert dry_run.conversation_count == 1
    assert dry_run.turn_count == 1
    assert dry_run.attachment_count == 1
    assert {item.category for item in dry_run.exclusions} >= {
        "active_or_interrupted_run",
        "run_checkpoint",
        "tool_checkpoint",
        "pending_vault_batch",
        "secret_metadata",
        "plugin_setting",
    }
    assert not request.target_state_directory.exists()

    applied = migrate_legacy_obsidian(request)
    current = json.loads(request.target_plugin_data.read_text(encoding="utf-8"))
    current["chatTabs"] = {"schemaVersion": 1, "activeTabId": "tab_local", "tabs": []}
    request.target_plugin_data.write_text(json.dumps(current), encoding="utf-8")
    repeated = migrate_legacy_obsidian(request)

    assert applied.changed is True
    assert repeated.changed is False
    assert repeated.receipt_path == applied.receipt_path
    data = json.loads(request.target_plugin_data.read_text(encoding="utf-8"))
    assert data["schemaVersion"] == 2
    assert data["settings"]["permissionMode"] == "normal"
    assert data["settings"]["workspaceTrusted"] is True
    assert data["settings"]["autoApproveVaultWrites"] is False
    assert "apiToken" not in data["settings"]
    assert data["chatTabs"]["activeTabId"] == "tab_local"
    assert (request.vault_root / "agent.md").read_text(encoding="utf-8") == "# OfferAgent Contract\n"
    assert (request.vault_root / ".codex" / "skills" / "obsidian-cli" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_imported_session_projection_events_and_attachment_are_valid(tmp_path: Path) -> None:
    request = _request(tmp_path)
    report = migrate_legacy_obsidian(request)
    factory = SqliteUnitOfWorkFactory(request.target_state_directory / "state.sqlite")
    await factory.initialize()
    sessions = SessionLifecycleService(
        unit_of_work=factory,
        event_sink=RecordingEventSink(),
        clock=ManualClock(),
        ids=DeterministicIdGenerator(),
        turn_manager=TurnManager(),
        approval_manager=ApprovalManager(unit_of_work=factory, clock=ManualClock()),
    )
    listed = await sessions.list(SessionListCommand("ws_test"))
    assert len(listed.sessions) == 1
    session_id = listed.sessions[0].session_id
    projection = UowConversationProjectionService(workspace_id="ws_test", unit_of_work=factory)
    turns = await projection.turns(session_id, _NeverCancelled())
    assistant = turns[0].assistant_content[0]
    assert isinstance(assistant, TextContentBlock)
    assert assistant.text == "It shows a migration boundary."
    assert [item.type for item in turns[0].input] == ["text", "image"]
    run_id = turns[0].runs[0].run_id
    async with factory.begin() as uow:
        events = await uow.events.read(run_id)
    assert [stored_event_to_envelope(item).type.value for item in events] == [
        "turn.started",
        "assistant.completed",
        "turn.completed",
    ]
    image = turns[0].input[1]
    assert isinstance(image, ImageContentBlock)
    store = ConversationAttachmentStore(
        request.target_state_directory / "conversation-attachments",
        workspace_id="ws_test",
        clock=ManualClock(),
        ids=DeterministicIdGenerator(),
    )
    read = await store.read_for_conversation(session_id, image.artifact.artifact_id, 0, len(PNG), _NeverCancelled())
    assert read.content == PNG
    assert report.receipt_path.is_file()


@pytest.mark.parametrize(("version", "message"), [(18, "schema"), (20, "schema")])
def test_unsupported_source_schema_is_refused_before_mutation(tmp_path: Path, version: int, message: str) -> None:
    request = _request(tmp_path, version=version)
    with pytest.raises(LegacyMigrationError, match=message):
        migrate_legacy_obsidian(request)
    assert not request.target_state_directory.exists()
    assert not request.target_plugin_data.exists()


def test_corrupt_image_rolls_back_every_target(tmp_path: Path) -> None:
    request = _request(tmp_path, corrupt_image=True)
    with pytest.raises(LegacyMigrationError, match="image"):
        migrate_legacy_obsidian(request)
    assert not request.target_state_directory.exists()
    assert not request.target_plugin_data.exists()
    assert not (request.vault_root / "agent.md").exists()


def test_missing_image_is_refused_before_backup_or_mutation(tmp_path: Path) -> None:
    request = _request(tmp_path)
    (request.source_attachments / "legacy_art").unlink()

    with pytest.raises(LegacyMigrationError, match="missing"):
        migrate_legacy_obsidian(request)

    assert not request.target_state_directory.exists()
    assert not request.target_plugin_data.exists()
    assert not request.target_state_directory.parent.exists()


@pytest.mark.parametrize(
    ("legacy_mode", "permission", "trusted", "auto_approve"),
    [
        ("read_only", "read-only", False, False),
        ("ask_every_time", "normal", True, False),
        ("trusted_vault", "trusted-workspace", True, True),
    ],
)
def test_every_legacy_permission_mode_has_one_closed_mapping(
    tmp_path: Path,
    legacy_mode: str,
    permission: str,
    trusted: bool,
    auto_approve: bool,
) -> None:
    request = _request(tmp_path)
    assert request.source_plugin_data is not None
    request.source_plugin_data.write_text(
        json.dumps({"fastMode": False, "vaultPermissionMode": legacy_mode}),
        encoding="utf-8",
    )

    migrate_legacy_obsidian(request)

    data = json.loads(request.target_plugin_data.read_text(encoding="utf-8"))
    assert data["settings"]["permissionMode"] == permission
    assert data["settings"]["workspaceTrusted"] is trusted
    assert data["settings"]["autoApproveVaultWrites"] is auto_approve


def test_activation_fault_restores_every_authoritative_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    from offeragent_harness import migration as module

    original = module._replace
    calls = 0

    def fail_agent_switch(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected authority switch failure")
        original(source, target)

    monkeypatch.setattr(module, "_replace", fail_agent_switch)
    with pytest.raises(LegacyMigrationError, match="activation"):
        migrate_legacy_obsidian(request)

    assert not request.target_state_directory.exists()
    assert not request.target_plugin_data.exists()
    assert not (request.vault_root / "agent.md").exists()
    assert not (request.vault_root / ".codex" / "skills" / "obsidian-cli" / "SKILL.md").exists()
    assert tuple(request.target_state_directory.parent.glob(".migration-backups/legacy-v1-*"))


def test_interrupted_final_state_switch_recovers_from_the_verified_staging_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    request.target_state_directory.mkdir(parents=True)
    (request.target_state_directory / "preexisting.txt").write_text("preserve in immutable backup\n", encoding="utf-8")
    from offeragent_harness import migration as module

    original_replace = module._replace
    original_rollback = module._rollback_activation

    class SimulatedPowerLoss(BaseException):
        pass

    def lose_power_before_final_state_switch(source: Path, target: Path) -> None:
        if source.name.startswith(f".{request.target_state_directory.name}.migration-") and (
            target == request.target_state_directory
        ):
            raise OSError("injected final State switch failure")
        original_replace(source, target)

    def interrupt_rollback(*args: object, **kwargs: object) -> list[BaseException]:
        raise SimulatedPowerLoss

    monkeypatch.setattr(module, "_replace", lose_power_before_final_state_switch)
    monkeypatch.setattr(module, "_rollback_activation", interrupt_rollback)
    with pytest.raises(SimulatedPowerLoss):
        migrate_legacy_obsidian(request)

    monkeypatch.setattr(module, "_replace", original_replace)
    monkeypatch.setattr(module, "_rollback_activation", original_rollback)

    async def forbid_rebuilding_verified_staging(*args: object, **kwargs: object) -> None:
        raise AssertionError("recovery must activate the already validated staging plan")

    monkeypatch.setattr(module, "_apply_to_staged_state", forbid_rebuilding_verified_staging)
    recovered = migrate_legacy_obsidian(request)

    assert recovered.changed is False
    assert recovered.receipt_path.is_file()
    assert request.target_plugin_data.is_file()
    assert (request.vault_root / "agent.md").is_file()
    assert not request.target_state_directory.parent.joinpath(
        f".{request.target_state_directory.name}.migration-journal.json"
    ).exists()


def test_interrupted_authority_switch_is_forward_completed_from_the_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    from offeragent_harness import migration as module

    original_replace = module._replace
    original_rollback = module._rollback_activation

    class SimulatedPowerLoss(BaseException):
        pass

    def lose_power_during_authority_switch(source: Path, target: Path) -> None:
        if source.name.startswith(".agent.md.migration-") and target == request.vault_root / "agent.md":
            raise OSError("injected Agent Contract switch failure")
        original_replace(source, target)

    def interrupt_rollback(*args: object, **kwargs: object) -> list[BaseException]:
        raise SimulatedPowerLoss

    monkeypatch.setattr(module, "_replace", lose_power_during_authority_switch)
    monkeypatch.setattr(module, "_rollback_activation", interrupt_rollback)
    with pytest.raises(SimulatedPowerLoss):
        migrate_legacy_obsidian(request)

    monkeypatch.setattr(module, "_replace", original_replace)
    monkeypatch.setattr(module, "_rollback_activation", original_rollback)

    async def forbid_rebuilding_verified_staging(*args: object, **kwargs: object) -> None:
        raise AssertionError("recovery must activate the already validated staging plan")

    monkeypatch.setattr(module, "_apply_to_staged_state", forbid_rebuilding_verified_staging)
    recovered = migrate_legacy_obsidian(request)

    assert recovered.changed is False
    assert recovered.receipt_path.is_file()
    assert request.target_plugin_data.is_file()
    assert (request.vault_root / "agent.md").read_text(encoding="utf-8") == "# OfferAgent Contract\n"
    assert (request.vault_root / ".codex" / "skills" / "obsidian-cli" / "SKILL.md").is_file()


def test_custom_control_conflict_is_refused_before_target_state_mutation(tmp_path: Path) -> None:
    request = _request(tmp_path)
    (request.vault_root / "agent.md").write_text("# My custom contract\n", encoding="utf-8")

    with pytest.raises(LegacyMigrationError, match="target conflict"):
        migrate_legacy_obsidian(request)

    assert not request.target_state_directory.exists()
    assert not request.target_plugin_data.exists()


def test_capacity_failure_is_refused_before_authority_switch(tmp_path: Path) -> None:
    original = _request(tmp_path)
    request = LegacyMigrationRequest(
        source_state=original.source_state,
        source_attachments=original.source_attachments,
        source_plugin_data=original.source_plugin_data,
        target_state_directory=original.target_state_directory,
        target_plugin_data=original.target_plugin_data,
        vault_root=original.vault_root,
        target_templates=original.target_templates,
        workspace_id=original.workspace_id,
        profile_id=original.profile_id,
        attachment_limits=AttachmentLimits(
            max_image_bytes=32,
            max_submission_bytes=32,
            max_conversation_bytes=32,
            max_total_bytes=32,
            max_chunk_bytes=32,
        ),
    )

    with pytest.raises(LegacyMigrationError, match="image"):
        migrate_legacy_obsidian(request)

    assert not request.target_state_directory.exists()
    assert not request.target_plugin_data.exists()


@pytest.mark.asyncio
async def test_mapped_identity_collision_preserves_existing_python_state(tmp_path: Path) -> None:
    request = _request(tmp_path)
    report = migrate_legacy_obsidian(request, dry_run=True)
    from offeragent_harness import migration as module

    session_id = module._mapped_id("ses", report.source_hash, "conv_done")
    request.target_state_directory.mkdir(parents=True)
    factory = SqliteUnitOfWorkFactory(request.target_state_directory / "state.sqlite")
    await factory.initialize()
    existing = Session(
        session_id,
        "ws_test",
        "profile_local",
        "Existing authority",
        SessionStatus.ACTIVE,
        ManualClock().utcnow(),
        ManualClock().utcnow(),
        1,
    )
    async with factory.begin() as uow:
        await uow.entities.put("sessions", session_id, existing, expected_revision=0)
        await uow.commit()

    with pytest.raises(LegacyMigrationError, match="identity collision"):
        migrate_legacy_obsidian(request)

    reopened = SqliteUnitOfWorkFactory(request.target_state_directory / "state.sqlite")
    stored = await reopened.get_entity("sessions", session_id)
    assert stored == existing
    assert not request.target_plugin_data.exists()


class _NeverCancelled:
    @property
    def cancelled(self) -> bool:
        return False

    @property
    def reason(self) -> None:
        return None

    def checkpoint(self) -> None:
        return None

    async def wait(self) -> Never:
        await __import__("asyncio").Future()
        raise AssertionError("unreachable")
