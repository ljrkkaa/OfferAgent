"""Offline, fail-closed migration from the legacy Obsidian Runtime.

The legacy database is opened read-only and immutable.  A plan is fully
validated before any authoritative target is touched; execution applies that
same plan to a sibling staging tree and switches the target only after a
durable backup and a source recheck exist.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import uuid
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from typing_extensions import Never

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.ports import NewEvent
from offeragent_harness.protocol._base import validate_wire
from offeragent_harness.protocol.common import SessionSummary
from offeragent_harness.protocol.events import (
    AssistantCompletedPayload,
    EventType,
    SessionUpdatedPayload,
    TurnCompletedPayload,
    TurnStartedPayload,
    make_domain_event_record,
)
from offeragent_harness.runtime.conversation_attachments import (
    AttachmentClaim,
    AttachmentLimits,
    AttachmentUploadRequest,
    ConversationAttachmentStore,
)
from offeragent_harness.sessions import (
    AgentLineage,
    Run,
    RunKind,
    RunStatus,
    Session,
    SessionStatus,
    TerminationReason,
    Turn,
    TurnStatus,
)

LEGACY_SCHEMA_VERSION = 19
MIGRATION_SCHEMA_VERSION = 1
MIGRATION_NAME = "obsidan-v19-to-python-harness-v1"
_MAX_ROWS = 100_000
_SHA256 = "sha256:"
_replace = os.replace
_LEGACY_CONTROL_HASHES = frozenset(
    {
        "sha256:70fdfb9c99d375c0eb07b0523149331c8d886dc319dee21fdb1f90bba5b971c2",
        "sha256:d5f6f9437de29a3c6dcb1e96da6458968bfb9401f6d61f7f1ba212eb7002e266",
    }
)

_REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    "conversations": frozenset({"id", "title", "model_id", "created_at", "updated_at", "archived"}),
    "agent_runs": frozenset(
        {
            "id",
            "conversation_id",
            "model_id",
            "status",
            "user_message_id",
            "assistant_message_id",
            "created_at",
            "updated_at",
        }
    ),
    "messages": frozenset({"id", "conversation_id", "agent_run_id", "role", "text", "sequence", "created_at"}),
    "run_attachments": frozenset(
        {
            "id",
            "conversation_id",
            "agent_run_id",
            "content_hash",
            "file_name",
            "media_type",
            "size",
            "created_at",
            "message_id",
            "message_order",
            "owned_at",
        }
    ),
    "schema_migrations": frozenset({"version"}),
}

_DEFAULT_SETTINGS: Mapping[str, Any] = {
    "schemaVersion": 3,
    "proxyUrl": "",
    "model": "",
    "modelAccountBinding": None,
    "reasoningEffort": "medium",
    "permissionMode": "normal",
    "workspaceTrusted": False,
    "autoApproveVaultWrites": False,
    "shellEnabled": False,
    "subagentsEnabled": False,
    "hooksEnabled": False,
    "telemetryEnabled": False,
}
_PLUGIN_SETTINGS_KEYS = frozenset(_DEFAULT_SETTINGS)
_PLUGIN_REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high", "max"})
_PLUGIN_PERMISSION_MODES = frozenset({"read-only", "normal", "trusted-workspace", "plan", "bypass"})
_LOOPBACK_PROXY = re.compile(r"http://(?:127\.0\.0\.1|\[::1\]):([0-9]{1,5})/?")


class LegacyMigrationError(RuntimeError):
    """A source or target failed a closed migration precondition."""


@dataclass(frozen=True, slots=True)
class LegacyMigrationRequest:
    source_state: Path
    source_attachments: Path
    source_plugin_data: Path | None
    target_state_directory: Path
    target_plugin_data: Path
    vault_root: Path
    target_templates: Path
    workspace_id: str
    profile_id: str = "profile_local"
    attachment_limits: AttachmentLimits | None = None


@dataclass(frozen=True, slots=True)
class LegacyMigrationExclusion:
    category: str
    identifier: str
    reason: str


@dataclass(frozen=True, slots=True)
class LegacyMigrationReport:
    source_hash: str
    changed: bool
    conversation_count: int
    turn_count: int
    attachment_count: int
    exclusions: tuple[LegacyMigrationExclusion, ...]
    receipt_path: Path
    backup_path: Path

    def to_wire(self) -> dict[str, Any]:
        return {
            "attachmentCount": self.attachment_count,
            "backupPath": str(self.backup_path),
            "changed": self.changed,
            "conversationCount": self.conversation_count,
            "exclusions": [
                {"category": item.category, "identifier": item.identifier, "reason": item.reason}
                for item in self.exclusions
            ],
            "migration": MIGRATION_NAME,
            "receiptPath": str(self.receipt_path),
            "schemaVersion": MIGRATION_SCHEMA_VERSION,
            "sourceHash": self.source_hash,
            "turnCount": self.turn_count,
        }


@dataclass(frozen=True, slots=True)
class _LegacyAttachment:
    legacy_id: str
    file_name: str
    media_type: str
    size: int
    content_hash: str
    path: Path
    order: int
    artifact_id: str


@dataclass(frozen=True, slots=True)
class _LegacyTurn:
    legacy_run_id: str
    legacy_user_message_id: str
    user_text: str
    assistant_text: str
    model_id: str
    created_at: datetime
    updated_at: datetime
    turn_id: str
    run_id: str
    attachments: tuple[_LegacyAttachment, ...]


@dataclass(frozen=True, slots=True)
class _LegacyConversation:
    legacy_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    archived: bool
    session_id: str
    turns: tuple[_LegacyTurn, ...]


@dataclass(frozen=True, slots=True)
class _Plan:
    request: LegacyMigrationRequest
    source_hash: str
    template_hash: str
    conversations: tuple[_LegacyConversation, ...]
    exclusions: tuple[LegacyMigrationExclusion, ...]
    plugin_data: bytes
    agent_template: bytes
    skill_template: bytes
    target_fingerprint: str
    receipt_path: Path
    backup_path: Path
    journal_path: Path


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
        await asyncio.Future()
        raise AssertionError("unreachable")


class _AttachmentIds:
    def __init__(self, attachments: Sequence[_LegacyAttachment]) -> None:
        self._artifacts = [item.artifact_id for item in attachments]
        self._uploads = [f"upload_mig_{_digest_text(item.legacy_id)[:32]}" for item in attachments]

    def new_id(self, namespace: str) -> str:
        if namespace == "art" and self._artifacts:
            return self._artifacts.pop(0)
        if namespace == "upload" and self._uploads:
            return self._uploads.pop(0)
        raise LegacyMigrationError(f"migration requested an unplanned {namespace!r} identity")


def migrate_legacy_obsidian(
    request: LegacyMigrationRequest,
    *,
    dry_run: bool = False,
) -> LegacyMigrationReport:
    """Validate and optionally execute one source-hash-addressed migration."""

    plan = _build_plan(request)
    if plan.journal_path.exists():
        if dry_run:
            raise LegacyMigrationError("an interrupted migration activation requires execute-mode recovery")
        _recover_interrupted_activation(plan)
        plan = _build_plan(request)
    existing = _read_receipt(plan.receipt_path, plan.source_hash, plan.template_hash)
    if existing:
        return _report(plan, changed=False)
    if dry_run:
        return _report(plan, changed=False)
    try:
        _execute_plan(plan)
    except LegacyMigrationError:
        raise
    except Exception as error:
        raise LegacyMigrationError(f"legacy migration failed: {error}") from error
    return _report(plan, changed=True)


def _report(plan: _Plan, *, changed: bool) -> LegacyMigrationReport:
    turns = tuple(turn for conversation in plan.conversations for turn in conversation.turns)
    return LegacyMigrationReport(
        source_hash=plan.source_hash,
        changed=changed,
        conversation_count=len(plan.conversations),
        turn_count=len(turns),
        attachment_count=sum(len(turn.attachments) for turn in turns),
        exclusions=plan.exclusions,
        receipt_path=plan.receipt_path,
        backup_path=plan.backup_path,
    )


def _build_plan(raw_request: LegacyMigrationRequest) -> _Plan:
    request = _normalize_request(raw_request)
    source_hash = _source_hash(request)
    agent_template = _regular_bytes(request.target_templates / "agent.md", "target Agent Contract")
    skill_template = _regular_bytes(
        request.target_templates / "obsidian-cli" / "SKILL.md",
        "target Vault Skill",
    )
    template_hash = _digest_bytes(agent_template + b"\0" + skill_template)
    exclusions: list[LegacyMigrationExclusion] = []
    plugin_data = _migrated_plugin_data(request, exclusions)
    conversations = _read_legacy_conversations(request, source_hash, exclusions)
    short_source = source_hash[7:39]
    receipt_path = request.target_state_directory / "migration-receipts" / f"legacy-v1-{short_source}.json"
    backup_path = request.target_state_directory.parent / ".migration-backups" / f"legacy-v1-{short_source}"
    journal_path = (
        request.target_state_directory.parent / f".{request.target_state_directory.name}.migration-journal.json"
    )
    if not _read_receipt(receipt_path, source_hash, template_hash):
        _validate_target_controls(request, agent_template, skill_template)
        _validate_target_plugin_data(request, plugin_data)
        _validate_attachments(request, conversations)
    target_fingerprint = _target_fingerprint(request)
    return _Plan(
        request,
        source_hash,
        template_hash,
        conversations,
        tuple(exclusions),
        plugin_data,
        agent_template,
        skill_template,
        target_fingerprint,
        receipt_path,
        backup_path,
        journal_path,
    )


def _normalize_request(request: LegacyMigrationRequest) -> LegacyMigrationRequest:
    def absolute(path: Path, label: str) -> Path:
        result = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
        if not result.is_absolute():
            raise LegacyMigrationError(f"{label} must be absolute")
        return result

    normalized = LegacyMigrationRequest(
        source_state=absolute(request.source_state, "source State"),
        source_attachments=absolute(request.source_attachments, "source attachments"),
        source_plugin_data=(
            None if request.source_plugin_data is None else absolute(request.source_plugin_data, "source plugin data")
        ),
        target_state_directory=absolute(request.target_state_directory, "target State directory"),
        target_plugin_data=absolute(request.target_plugin_data, "target plugin data"),
        vault_root=absolute(request.vault_root, "Vault root"),
        target_templates=absolute(request.target_templates, "target templates"),
        workspace_id=request.workspace_id,
        profile_id=request.profile_id,
        attachment_limits=request.attachment_limits,
    )
    if not normalized.source_state.is_file() or normalized.source_state.is_symlink():
        raise LegacyMigrationError("legacy source State is unavailable or not a regular file")
    if normalized.source_attachments.exists() and (
        not normalized.source_attachments.is_dir() or normalized.source_attachments.is_symlink()
    ):
        raise LegacyMigrationError("legacy attachment directory is unsafe")
    if not normalized.vault_root.is_dir() or normalized.vault_root.is_symlink():
        raise LegacyMigrationError("target Vault root is unavailable")
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(f"{normalized.source_state}{suffix}").exists():
            raise LegacyMigrationError("legacy source State is live or has an unresolved SQLite sidecar")
    if _is_within(normalized.target_state_directory, normalized.source_attachments) or _is_within(
        normalized.target_state_directory, normalized.source_state.parent
    ):
        raise LegacyMigrationError("target State overlaps the read-only legacy source")
    if not normalized.workspace_id.startswith("ws_") or not normalized.profile_id.startswith("profile_"):
        raise LegacyMigrationError("target Workspace/Profile identity is invalid")
    return normalized


def _read_legacy_conversations(
    request: LegacyMigrationRequest,
    source_hash: str,
    exclusions: list[LegacyMigrationExclusion],
) -> tuple[_LegacyConversation, ...]:
    connection = _open_legacy_database(request.source_state)
    try:
        _validate_legacy_schema(connection)
        completed = connection.execute(
            """
            SELECT c.id AS conversation_id, c.title, c.created_at AS conversation_created_at,
                   c.updated_at AS conversation_updated_at, c.archived,
                   r.id AS run_id, r.model_id, r.created_at AS run_created_at,
                   r.updated_at AS run_updated_at,
                   u.id AS user_message_id, u.text AS user_text, u.sequence AS user_sequence,
                   a.id AS assistant_message_id, a.text AS assistant_text, a.sequence AS assistant_sequence
              FROM conversations AS c
              JOIN agent_runs AS r ON r.conversation_id = c.id AND r.status = 'completed'
              JOIN messages AS u ON u.id = r.user_message_id AND u.role = 'user'
              JOIN messages AS a ON a.id = r.assistant_message_id AND a.role = 'assistant'
             WHERE u.conversation_id = c.id AND a.conversation_id = c.id
                AND u.agent_run_id = r.id AND a.agent_run_id = r.id
              ORDER BY c.created_at, c.id, u.sequence, r.created_at, r.id
              LIMIT ?
            """,
            (_MAX_ROWS + 1,),
        ).fetchall()
        if len(completed) > _MAX_ROWS:
            raise LegacyMigrationError("legacy completed message set exceeds the migration bound")
        imported_runs = {str(row[5]) for row in completed}
        imported_conversations = {str(row[0]) for row in completed}
        imported_messages = {str(row[9]) for row in completed} | {str(row[12]) for row in completed}
        _collect_exclusions(connection, imported_runs, imported_conversations, imported_messages, exclusions)

        by_conversation: dict[str, list[_LegacyTurn]] = {}
        metadata: dict[str, tuple[str, datetime, datetime, bool]] = {}
        for row in completed:
            conversation_id = _bounded_id(row[0], "Conversation")
            run_id = _bounded_id(row[5], "Run")
            user_message_id = _bounded_id(row[9], "user message")
            user_sequence = _positive_integer(row[11], "user message sequence")
            assistant_sequence = _positive_integer(row[14], "assistant message sequence")
            if assistant_sequence <= user_sequence:
                raise LegacyMigrationError(f"legacy Run {run_id!r} has reversed message order")
            user_text = _bounded_text(row[10], "user message", allow_empty=True)
            assistant_text = _bounded_text(row[13], "assistant message", allow_empty=False)
            turn_id = _mapped_id("turn", source_hash, run_id)
            mapped_run_id = _mapped_id("run", source_hash, run_id)
            attachments = _attachments_for_message(
                connection,
                request.source_attachments,
                source_hash,
                conversation_id,
                run_id,
                user_message_id,
            )
            if not user_text and not attachments:
                raise LegacyMigrationError(f"legacy Run {run_id!r} has no importable user input")
            created_at = _timestamp(row[7], "Run created_at")
            updated_at = _timestamp(row[8], "Run updated_at")
            if updated_at < created_at:
                raise LegacyMigrationError(f"legacy Run {run_id!r} has reversed timestamps")
            turn = _LegacyTurn(
                run_id,
                user_message_id,
                user_text,
                assistant_text,
                _bounded_text(row[6], "model ID", allow_empty=False, maximum=256),
                created_at,
                updated_at,
                turn_id,
                mapped_run_id,
                attachments,
            )
            by_conversation.setdefault(conversation_id, []).append(turn)
            metadata[conversation_id] = (
                _bounded_text(row[1], "Conversation title", allow_empty=False, maximum=512),
                _timestamp(row[2], "Conversation created_at"),
                _timestamp(row[3], "Conversation updated_at"),
                _legacy_boolean(row[4], "Conversation archived"),
            )

        conversations: list[_LegacyConversation] = []
        for conversation_id, turns in by_conversation.items():
            title, created_at, updated_at, archived = metadata[conversation_id]
            ordered = tuple(turns)
            conversations.append(
                _LegacyConversation(
                    conversation_id,
                    title,
                    created_at,
                    max(updated_at, *(turn.updated_at for turn in ordered)),
                    archived,
                    _mapped_id("ses", source_hash, conversation_id),
                    ordered,
                )
            )
        return tuple(conversations)
    finally:
        connection.close()


def _open_legacy_database(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as error:
        raise LegacyMigrationError("legacy source State cannot be opened as an immutable snapshot") from error


def _validate_legacy_schema(connection: sqlite3.Connection) -> None:
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except (sqlite3.Error, TypeError, ValueError) as error:
        raise LegacyMigrationError("legacy source schema version is unreadable") from error
    if version != LEGACY_SCHEMA_VERSION:
        raise LegacyMigrationError(
            f"legacy source schema {version} is unsupported; expected exactly {LEGACY_SCHEMA_VERSION}"
        )
    tables = {
        str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    for table, required in _REQUIRED_COLUMNS.items():
        if table not in tables:
            raise LegacyMigrationError(f"legacy source schema is missing table {table!r}")
        columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()}
        missing = required - columns
        if missing:
            raise LegacyMigrationError(f"legacy source schema table {table!r} is missing {sorted(missing)}")
    migrations = {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations").fetchall()}
    if migrations != set(range(1, LEGACY_SCHEMA_VERSION + 1)):
        raise LegacyMigrationError("legacy source schema migration history is incomplete or unknown")


def _attachments_for_message(
    connection: sqlite3.Connection,
    root: Path,
    source_hash: str,
    conversation_id: str,
    run_id: str,
    message_id: str,
) -> tuple[_LegacyAttachment, ...]:
    rows = connection.execute(
        """
        SELECT id, file_name, media_type, size, content_hash, message_order, owned_at
          FROM run_attachments
         WHERE conversation_id = ? AND agent_run_id = ? AND message_id = ?
         ORDER BY message_order, id
        """,
        (conversation_id, run_id, message_id),
    ).fetchall()
    if len(rows) > 20:
        raise LegacyMigrationError(f"legacy message {message_id!r} exceeds the 20-image submission bound")
    result: list[_LegacyAttachment] = []
    for expected_order, row in enumerate(rows):
        legacy_id = _bounded_id(row[0], "attachment")
        if row[5] != expected_order or row[6] is None:
            raise LegacyMigrationError(f"legacy message {message_id!r} has unowned or non-contiguous attachments")
        path = root / legacy_id
        if path.parent != root or not path.is_file() or path.is_symlink():
            raise LegacyMigrationError(f"legacy attachment {legacy_id!r} is missing or unsafe")
        size = _positive_integer(row[3], "attachment size")
        content_hash = _bounded_text(row[4], "attachment content hash", allow_empty=False, maximum=71)
        if not content_hash.startswith(_SHA256) or len(content_hash) != 71:
            raise LegacyMigrationError(f"legacy attachment {legacy_id!r} has an unsupported content hash")
        payload = path.read_bytes()
        if len(payload) != size or _digest_bytes(payload) != content_hash:
            raise LegacyMigrationError(f"legacy attachment {legacy_id!r} bytes differ from metadata")
        result.append(
            _LegacyAttachment(
                legacy_id,
                _bounded_text(row[1], "attachment file name", allow_empty=False, maximum=512),
                _bounded_text(row[2], "attachment media type", allow_empty=False, maximum=255),
                size,
                content_hash,
                path,
                expected_order,
                _mapped_id("art", source_hash, legacy_id),
            )
        )
    return tuple(result)


def _collect_exclusions(
    connection: sqlite3.Connection,
    imported_runs: set[str],
    imported_conversations: set[str],
    imported_messages: set[str],
    output: list[LegacyMigrationExclusion],
) -> None:
    for row in connection.execute("SELECT id, status FROM agent_runs ORDER BY id").fetchall():
        identifier, status = str(row[0]), str(row[1])
        if identifier not in imported_runs:
            output.append(
                LegacyMigrationExclusion(
                    "active_or_interrupted_run" if status in {"running", "interrupted"} else "non_completed_run",
                    identifier,
                    f"legacy Run status {status!r} is not a stable completed import boundary",
                )
            )
    for row in connection.execute("SELECT id FROM conversations ORDER BY id").fetchall():
        identifier = str(row[0])
        if identifier not in imported_conversations:
            output.append(LegacyMigrationExclusion("conversation", identifier, "no stable completed Run was imported"))
    for table, category, reason in (
        ("run_checkpoints", "run_checkpoint", "resumable execution state is intentionally not migrated"),
        ("tool_calls", "tool_checkpoint", "tool execution/effect state is intentionally not migrated"),
    ):
        if _table_exists(connection, table):
            for row in connection.execute(f'SELECT id FROM "{table}" ORDER BY id').fetchall():
                output.append(LegacyMigrationExclusion(category, str(row[0]), reason))
    if _table_exists(connection, "vault_change_batches"):
        for row in connection.execute("SELECT id, state FROM vault_change_batches ORDER BY id").fetchall():
            state = str(row[1])
            output.append(
                LegacyMigrationExclusion(
                    "pending_vault_batch" if state in {"pending", "applying"} else "vault_change_batch",
                    str(row[0]),
                    "Vault mutations and effect state are never replayed by migration",
                )
            )
    if _table_exists(connection, "settings_metadata"):
        for row in connection.execute("SELECT key FROM settings_metadata ORDER BY key").fetchall():
            output.append(
                LegacyMigrationExclusion(
                    "secret_metadata",
                    str(row[0]),
                    "legacy settings/provider metadata, credentials, tokens, and secrets are excluded",
                )
            )
    if _table_exists(connection, "run_attachments"):
        rows = connection.execute("SELECT id, message_id FROM run_attachments ORDER BY id").fetchall()
        for row in rows:
            if row[1] is not None and str(row[1]) in imported_messages:
                continue
            output.append(
                LegacyMigrationExclusion(
                    "unretained_attachment",
                    str(row[0]),
                    "attachment is not owned by an imported completed user message",
                )
            )


def _migrated_plugin_data(
    request: LegacyMigrationRequest,
    exclusions: list[LegacyMigrationExclusion],
) -> bytes:
    settings = dict(_DEFAULT_SETTINGS)
    source = request.source_plugin_data
    if source is not None and source.exists():
        raw = _strict_json_file(source, "legacy plugin data")
        if raw.get("schemaVersion") == 2 and isinstance(raw.get("settings"), dict):
            settings = _project_plugin_settings(raw["settings"], exclusions)
            for key in sorted(set(raw) - {"schemaVersion", "settings", "chatTabs"}):
                exclusions.append(
                    LegacyMigrationExclusion(
                        "plugin_setting",
                        key,
                        "unknown plugin data field is excluded without reading it into the current schema",
                    )
                )
            return _canonical_json(
                {
                    "schemaVersion": 2,
                    "settings": settings,
                    "chatTabs": raw.get("chatTabs"),
                }
            )
        mode = raw.get("vaultPermissionMode", "trusted_vault")
        mapped = {
            "read_only": ("read-only", False, False),
            "ask_every_time": ("normal", True, False),
            "trusted_vault": ("trusted-workspace", True, True),
        }.get(mode)
        if mapped is None:
            raise LegacyMigrationError("legacy plugin vaultPermissionMode is unsupported")
        settings["permissionMode"], settings["workspaceTrusted"], settings["autoApproveVaultWrites"] = mapped
        for key in sorted(raw):
            if key == "vaultPermissionMode":
                continue
            reason = (
                "legacy Fast Mode has no closed Python-harness equivalent"
                if key == "fastMode"
                else ("unknown legacy plugin field may contain a credential, token, secret, or retired setting")
            )
            exclusions.append(LegacyMigrationExclusion("plugin_setting", key, reason))
    return _canonical_json({"schemaVersion": 2, "settings": settings, "chatTabs": None})


def _project_plugin_settings(
    raw: Mapping[str, Any],
    exclusions: list[LegacyMigrationExclusion],
) -> dict[str, Any]:
    version = raw.get("schemaVersion")
    if type(version) is not int or version not in {2, 3}:
        raise LegacyMigrationError("legacy plugin settings schema is unsupported")
    result = dict(_DEFAULT_SETTINGS)
    subscription = raw.get("provider") == "codex-subscription-experimental"

    proxy = raw.get("proxyUrl")
    if subscription and isinstance(proxy, str):
        normalized = proxy.strip().rstrip("/")
        match = _LOOPBACK_PROXY.fullmatch(proxy.strip())
        if match is not None and 1 <= int(match.group(1)) <= 65_535:
            result["proxyUrl"] = normalized

    reasoning = raw.get("reasoningEffort")
    if isinstance(reasoning, str) and reasoning in _PLUGIN_REASONING_EFFORTS:
        result["reasoningEffort"] = reasoning

    permission = raw.get("permissionMode")
    if isinstance(permission, str) and permission in _PLUGIN_PERMISSION_MODES:
        result["permissionMode"] = permission
    trusted = raw.get("workspaceTrusted") is True
    result["workspaceTrusted"] = trusted
    if result["permissionMode"] in {"trusted-workspace", "bypass"} and not trusted:
        result["permissionMode"] = "normal"
    result["autoApproveVaultWrites"] = trusted and raw.get("autoApproveVaultWrites") is True
    for key in ("shellEnabled", "subagentsEnabled", "hooksEnabled"):
        result[key] = raw.get(key) is True
    result["telemetryEnabled"] = False

    for key in sorted(set(raw) - _PLUGIN_SETTINGS_KEYS):
        if key == "provider":
            reason = "retired Provider choice was used only to qualify the model candidate"
        else:
            reason = "retired or unknown plugin setting is excluded without exposing its value"
        exclusions.append(LegacyMigrationExclusion("plugin_setting", key, reason))
    return result


def _validate_target_plugin_data(request: LegacyMigrationRequest, migrated: bytes) -> None:
    target = request.target_plugin_data
    if not target.exists():
        return
    existing = _regular_bytes(target, "target plugin data")
    if request.source_plugin_data is not None and _same_path(target, request.source_plugin_data):
        return
    try:
        value = json.loads(existing.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LegacyMigrationError("target plugin data conflicts with the closed migration schema") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"schemaVersion", "settings", "chatTabs"}
        or value.get("schemaVersion") != 2
        or not isinstance(value.get("settings"), dict)
        or set(value["settings"]) != _PLUGIN_SETTINGS_KEYS
        or value["settings"].get("schemaVersion") != 3
    ):
        raise LegacyMigrationError("target plugin data conflicts with the closed migration schema")
    if existing != migrated:
        raise LegacyMigrationError("target plugin settings already contain a different authoritative configuration")


def _validate_target_controls(request: LegacyMigrationRequest, agent: bytes, skill: bytes) -> None:
    targets = (
        (request.vault_root / "agent.md", agent, "Agent Contract"),
        (request.vault_root / ".codex" / "skills" / "obsidian-cli" / "SKILL.md", skill, "Vault Skill"),
    )
    for path, expected, label in targets:
        if path.exists():
            current = _regular_bytes(path, f"target {label}")
            if current != expected and _control_digest(current) not in _LEGACY_CONTROL_HASHES:
                raise LegacyMigrationError(f"target conflict: existing {label} is not a recognized legacy control")


def _validate_attachments(request: LegacyMigrationRequest, conversations: Sequence[_LegacyConversation]) -> None:
    attachments = tuple(
        item for conversation in conversations for turn in conversation.turns for item in turn.attachments
    )
    if not attachments:
        return
    temporary = Path(tempfile.mkdtemp(prefix="offeragent-migration-validation-"))
    store: ConversationAttachmentStore | None = None
    try:
        store = ConversationAttachmentStore(
            temporary,
            workspace_id=request.workspace_id,
            clock=_FixedClock(datetime(2000, 1, 1, tzinfo=timezone.utc)),
            ids=_AttachmentIds(attachments),
            limits=request.attachment_limits,
        )
        _run_async(_validate_attachment_uploads(store, conversations))
    except Exception as error:
        raise LegacyMigrationError(f"legacy image validation failed: {error}") from error
    finally:
        store = None
        gc.collect()
        shutil.rmtree(temporary, ignore_errors=True)


async def _validate_attachment_uploads(
    store: ConversationAttachmentStore,
    conversations: Sequence[_LegacyConversation],
) -> None:
    for conversation in conversations:
        for turn in conversation.turns:
            await _upload_turn_attachments(store, conversation.session_id, turn, claim=False)


def _execute_plan(plan: _Plan) -> None:
    request = plan.request
    if _source_hash(request) != plan.source_hash:
        raise LegacyMigrationError("legacy source changed after planning")
    if _target_fingerprint(request) != plan.target_fingerprint:
        raise LegacyMigrationError("target changed after planning")
    if _read_receipt(plan.receipt_path, plan.source_hash, plan.template_hash):
        return

    request.target_state_directory.parent.mkdir(parents=True, exist_ok=True)
    _create_backup(plan)
    token = uuid.uuid4().hex
    staging_state = request.target_state_directory.parent / f".{request.target_state_directory.name}.migration-{token}"
    rollback_state = request.target_state_directory.parent / f".{request.target_state_directory.name}.rollback-{token}"
    staged_files: list[tuple[Path, Path, Path]] = []
    state_activated = False
    preserve_recovery = False
    try:
        if request.target_state_directory.exists():
            shutil.copytree(request.target_state_directory, staging_state)
        else:
            staging_state.mkdir()
        _run_async(_apply_to_staged_state(plan, staging_state))
        gc.collect()
        receipt = _canonical_json(
            {
                "attachmentCount": sum(
                    len(turn.attachments) for conversation in plan.conversations for turn in conversation.turns
                ),
                "conversationCount": len(plan.conversations),
                "migration": MIGRATION_NAME,
                "schemaVersion": MIGRATION_SCHEMA_VERSION,
                "sourceHash": plan.source_hash,
                "targetTemplateHash": plan.template_hash,
                "turnCount": sum(len(item.turns) for item in plan.conversations),
            }
        )
        staged_receipt = staging_state / "migration-receipts" / plan.receipt_path.name
        staged_receipt.parent.mkdir(parents=True, exist_ok=True)
        _write_new_file(staged_receipt, receipt)

        staged_files = _stage_authority_files(plan, token)
        if _source_hash(request) != plan.source_hash or _target_fingerprint(request) != plan.target_fingerprint:
            raise LegacyMigrationError("source or target changed before atomic activation")

        _write_activation_journal(plan, staging_state, rollback_state)
        for staged, target, rollback in staged_files:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                _replace(target, rollback)
            _replace(staged, target)
        if request.target_state_directory.exists():
            _replace(request.target_state_directory, rollback_state)
        _replace(staging_state, request.target_state_directory)
        state_activated = True
        _fsync_directory(request.target_state_directory.parent)
    except BaseException as error:
        preserve_recovery = True
        rollback_errors = _rollback_activation(
            request,
            staging_state,
            rollback_state,
            staged_files,
            state_activated=state_activated,
        )
        if rollback_errors:
            raise LegacyMigrationError(
                f"migration activation failed and rollback was incomplete; immutable backup: {plan.backup_path}"
            ) from rollback_errors[0]
        preserve_recovery = False
        plan.journal_path.unlink(missing_ok=True)
        if isinstance(error, LegacyMigrationError):
            raise
        raise LegacyMigrationError(f"migration activation failed: {error}") from error
    else:
        if rollback_state.exists():
            shutil.rmtree(rollback_state)
        for _, _, rollback in staged_files:
            rollback.unlink(missing_ok=True)
        plan.journal_path.unlink(missing_ok=True)
    finally:
        if not preserve_recovery:
            if staging_state.exists():
                shutil.rmtree(staging_state, ignore_errors=True)
            for staged, _, _ in staged_files:
                staged.unlink(missing_ok=True)


async def _apply_to_staged_state(plan: _Plan, state: Path) -> None:
    request = plan.request
    factory = SqliteUnitOfWorkFactory(state / "state.sqlite")
    await factory.initialize()
    all_attachments = tuple(
        item for conversation in plan.conversations for turn in conversation.turns for item in turn.attachments
    )
    store = ConversationAttachmentStore(
        state / "conversation-attachments",
        workspace_id=request.workspace_id,
        clock=_FixedClock(datetime.now(timezone.utc)),
        ids=_AttachmentIds(all_attachments),
        limits=request.attachment_limits,
    )
    for conversation in plan.conversations:
        if await factory.get_entity("sessions", conversation.session_id) is not None:
            raise LegacyMigrationError(f"target identity collision for Session {conversation.session_id}")
        for turn in conversation.turns:
            if (
                await factory.get_entity("turns", turn.turn_id) is not None
                or await factory.get_entity("runs", turn.run_id) is not None
            ):
                raise LegacyMigrationError(f"target identity collision for legacy Run {turn.legacy_run_id}")

        session = Session(
            conversation.session_id,
            request.workspace_id,
            request.profile_id,
            conversation.title,
            SessionStatus.ARCHIVED if conversation.archived else SessionStatus.ACTIVE,
            conversation.created_at,
            conversation.updated_at,
            1,
        )
        summary = SessionSummary(
            session_id=session.session_id,
            workspace_id=session.workspace_id,
            title=session.title,
            created_at=session.created_at.isoformat(),
            updated_at=session.updated_at.isoformat(),
            active_run_id=None,
            turn_count=0,
            deleted=False,
        )
        session_record = make_domain_event_record(
            event_type=EventType.SESSION_UPDATED,
            payload=SessionUpdatedPayload(session=summary, changed_fields=["created", "title", "updatedAt"]),
            trace_id=_mapped_id("trace", plan.source_hash, conversation.legacy_id),
            workspace_id=request.workspace_id,
            session_id=session.session_id,
            turn_id=None,
            run_id=None,
            root_run_id=None,
            parent_run_id=None,
            state_revision=1,
        )
        session_event = _event(
            session.session_id,
            1,
            EventType.SESSION_UPDATED.value,
            session_record.to_wire(),
            conversation.updated_at,
            terminal=False,
        )
        async with factory.begin() as uow:
            await uow.entities.put("sessions", session.session_id, session, expected_revision=0)
            await uow.entities.put(
                "session_lifecycle",
                session.session_id,
                {
                    "schemaVersion": 1,
                    "sessionId": session.session_id,
                    "workspaceId": session.workspace_id,
                    "eventSequence": 1,
                    "deletedAt": None,
                    "purgeAfter": None,
                    "forkReference": None,
                },
                expected_revision=0,
            )
            await uow.events.append(session.session_id, 0, (session_event,))
            await uow.commit()

        for ordinal, legacy_turn in enumerate(conversation.turns, start=1):
            artifacts = await _upload_turn_attachments(store, session.session_id, legacy_turn, claim=True)
            input_blocks: list[Mapping[str, Any]] = []
            if legacy_turn.user_text:
                input_blocks.append(
                    {"type": "text", "text": legacy_turn.user_text, "format": "markdown", "references": []}
                )
            input_blocks.extend(
                {
                    "type": "image",
                    "artifact": artifact.to_wire(),
                    "altText": attachment.file_name,
                }
                for attachment, artifact in zip(legacy_turn.attachments, artifacts, strict=True)
            )
            assistant_content = [
                {"type": "text", "text": legacy_turn.assistant_text, "format": "markdown", "references": []}
            ]
            lineage = AgentLineage.root(legacy_turn.run_id)
            domain_turn = Turn(
                legacy_turn.turn_id,
                session.session_id,
                ordinal,
                TurnStatus.COMPLETED,
                tuple(input_blocks),
                legacy_turn.created_at,
                legacy_turn.updated_at,
                1,
            )
            run = Run(
                legacy_turn.run_id,
                session.session_id,
                domain_turn.turn_id,
                request.workspace_id,
                lineage,
                RunKind.ROOT,
                RunStatus.COMPLETED,
                1,
                3,
                {"provider": "legacy-obsidan", "model": legacy_turn.model_id, "permissionMode": "read-only"},
                legacy_turn.created_at,
                legacy_turn.updated_at,
                None,
                TerminationReason.COMPLETED,
            )
            state_value = RunState(
                request.workspace_id,
                session.session_id,
                domain_turn.turn_id,
                run.run_id,
                lineage,
                phase=RunPhase.COMPLETED,
                revision=1,
                assistant_text=legacy_turn.assistant_text,
            )
            trace_id = _mapped_id("trace", plan.source_hash, legacy_turn.legacy_run_id)
            run_config = {
                "provider": "legacy-obsidan",
                "model": legacy_turn.model_id,
                "reasoningEffort": "medium",
                "permissionMode": "read-only",
                "budgets": None,
            }
            payloads = (
                (
                    EventType.TURN_STARTED,
                    validate_wire(
                        TurnStartedPayload,
                        {"input": list(input_blocks), "runConfig": run_config, "attempt": 1},
                    ),
                    legacy_turn.created_at,
                    False,
                ),
                (
                    EventType.ASSISTANT_COMPLETED,
                    validate_wire(
                        AssistantCompletedPayload,
                        {"content": assistant_content, "finishReason": "stop"},
                    ),
                    legacy_turn.updated_at,
                    False,
                ),
                (
                    EventType.TURN_COMPLETED,
                    validate_wire(
                        TurnCompletedPayload,
                        {
                            "reason": "completed",
                            "assistantContent": assistant_content,
                            "usage": {
                                "inputTokens": 0,
                                "outputTokens": 0,
                                "cachedInputTokens": 0,
                                "reasoningTokens": 0,
                                "modelCalls": 0,
                                "toolCalls": 0,
                                "costMicros": None,
                                "wallTimeMs": 0,
                            },
                        },
                    ),
                    legacy_turn.updated_at,
                    True,
                ),
            )
            events: list[NewEvent] = []
            for sequence, (event_type, payload, occurred_at, terminal) in enumerate(payloads, start=1):
                record = make_domain_event_record(
                    event_type=event_type,
                    payload=payload,
                    trace_id=trace_id,
                    workspace_id=request.workspace_id,
                    session_id=session.session_id,
                    turn_id=domain_turn.turn_id,
                    run_id=run.run_id,
                    root_run_id=run.run_id,
                    parent_run_id=None,
                    state_revision=state_value.revision,
                )
                events.append(
                    _event(
                        run.run_id,
                        sequence,
                        event_type.value,
                        record.to_wire(),
                        occurred_at,
                        terminal=terminal,
                    )
                )
            async with factory.begin() as uow:
                await uow.entities.put("turns", domain_turn.turn_id, domain_turn, expected_revision=0)
                await uow.entities.put("runs", run.run_id, run, expected_revision=0)
                await uow.entities.put("run_states", run.run_id, state_value, expected_revision=0)
                await uow.events.append(run.run_id, 0, tuple(events))
                await uow.commit()


async def _upload_turn_attachments(
    store: ConversationAttachmentStore,
    session_id: str,
    turn: _LegacyTurn,
    *,
    claim: bool,
) -> tuple[Any, ...]:
    artifacts: list[Any] = []
    for attachment in turn.attachments:
        begin = await store.begin(
            AttachmentUploadRequest(
                session_id,
                f"req_mig_{_digest_text(attachment.legacy_id)[:32]}",
                attachment.file_name,
                attachment.media_type,
                attachment.size,
                attachment.content_hash,
            ),
            _NeverCancelled(),
        )
        payload = attachment.path.read_bytes()
        offset = 0
        while offset < len(payload):
            chunk = payload[offset : offset + 64 * 1024]
            receipt = await store.append(begin.upload_id, offset, chunk, _NeverCancelled(), session_id=session_id)
            offset = receipt.next_offset
        committed = await store.commit(begin.upload_id, _NeverCancelled(), session_id=session_id)
        artifacts.append(committed.artifact)
    if claim and artifacts:
        await store.claim_submission(
            session_id,
            turn.turn_id,
            tuple(
                AttachmentClaim(
                    artifact.artifact_id,
                    attachment.order,
                    artifact.content_hash,
                    artifact.media_type,
                    artifact.size_bytes,
                )
                for attachment, artifact in zip(turn.attachments, artifacts, strict=True)
            ),
            _NeverCancelled(),
        )
    return tuple(artifacts)


def _event(
    stream_id: str,
    sequence: int,
    event_type: str,
    payload: Mapping[str, Any],
    occurred_at: datetime,
    *,
    terminal: bool,
) -> NewEvent:
    digest = _digest_text(f"{stream_id}:{sequence}:{event_type}:{_canonical_json(payload).decode()}")
    return NewEvent(
        event_id=f"evt_mig_{digest[:32]}",
        event_type=event_type,
        payload=payload,
        occurred_at=occurred_at,
        terminal=terminal,
        idempotency_key=f"migration:{stream_id}:{sequence}",
    )


def _stage_authority_files(plan: _Plan, token: str) -> list[tuple[Path, Path, Path]]:
    result = _authority_activation_paths(plan, token)
    for (target, content), (staged, _, _) in zip(_authority_files(plan), result, strict=True):
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_new_file(staged, content)
    return result


def _authority_files(plan: _Plan) -> tuple[tuple[Path, bytes], ...]:
    request = plan.request
    return (
        (request.target_plugin_data, plan.plugin_data),
        (request.vault_root / "agent.md", plan.agent_template),
        (request.vault_root / ".codex" / "skills" / "obsidian-cli" / "SKILL.md", plan.skill_template),
    )


def _authority_activation_paths(plan: _Plan, token: str) -> list[tuple[Path, Path, Path]]:
    return [
        (
            target.parent / f".{target.name}.migration-{token}",
            target,
            target.parent / f".{target.name}.rollback-{token}",
        )
        for target, _ in _authority_files(plan)
    ]


def _rollback_activation(
    request: LegacyMigrationRequest,
    staging_state: Path,
    rollback_state: Path,
    staged_files: Sequence[tuple[Path, Path, Path]],
    *,
    state_activated: bool,
) -> list[BaseException]:
    errors: list[BaseException] = []
    for staged, target, rollback in reversed(staged_files):
        try:
            if rollback.exists():
                if target.exists():
                    target.unlink()
                _replace(rollback, target)
            elif not staged.exists() and target.exists():
                target.unlink()
        except BaseException as error:
            errors.append(error)
    try:
        if state_activated and request.target_state_directory.exists():
            shutil.rmtree(request.target_state_directory)
        if rollback_state.exists():
            _replace(rollback_state, request.target_state_directory)
    except BaseException as error:
        errors.append(error)
    if not errors and staging_state.exists():
        shutil.rmtree(staging_state, ignore_errors=True)
    return errors


def _write_activation_journal(plan: _Plan, staging_state: Path, rollback_state: Path) -> None:
    if plan.journal_path.exists():
        raise LegacyMigrationError("migration activation journal already exists")
    _write_new_file(
        plan.journal_path,
        _canonical_json(
            {
                "migration": MIGRATION_NAME,
                "rollbackStateName": rollback_state.name,
                "schemaVersion": MIGRATION_SCHEMA_VERSION,
                "sourceHash": plan.source_hash,
                "stagingStateName": staging_state.name,
                "targetStateName": plan.request.target_state_directory.name,
                "targetTemplateHash": plan.template_hash,
            }
        ),
    )
    _fsync_directory(plan.journal_path.parent)


def _recover_interrupted_activation(plan: _Plan) -> None:
    value = _strict_canonical_json_file(plan.journal_path, "migration activation journal")
    expected_keys = {
        "migration",
        "rollbackStateName",
        "schemaVersion",
        "sourceHash",
        "stagingStateName",
        "targetStateName",
        "targetTemplateHash",
    }
    if (
        set(value) != expected_keys
        or value.get("migration") != MIGRATION_NAME
        or value.get("schemaVersion") != MIGRATION_SCHEMA_VERSION
        or value.get("sourceHash") != plan.source_hash
        or value.get("targetTemplateHash") != plan.template_hash
        or value.get("targetStateName") != plan.request.target_state_directory.name
    ):
        raise LegacyMigrationError("migration activation journal conflicts with this source or target")
    staging_name = value.get("stagingStateName")
    rollback_name = value.get("rollbackStateName")
    target_name = plan.request.target_state_directory.name
    prefix = f".{target_name}.migration-"
    if (
        not isinstance(staging_name, str)
        or not isinstance(rollback_name, str)
        or not staging_name.startswith(prefix)
        or Path(staging_name).name != staging_name
        or Path(rollback_name).name != rollback_name
    ):
        raise LegacyMigrationError("migration activation journal contains unsafe recovery paths")
    token = staging_name[len(prefix) :]
    if (
        len(token) != 32
        or any(character not in "0123456789abcdef" for character in token)
        or rollback_name != f".{target_name}.rollback-{token}"
    ):
        raise LegacyMigrationError("migration activation journal contains an invalid activation identity")
    parent = plan.request.target_state_directory.parent
    staging = parent / staging_name
    rollback = parent / rollback_name
    target = plan.request.target_state_directory
    staged_files = _authority_activation_paths(plan, token)

    if target.exists() and _read_receipt(plan.receipt_path, plan.source_hash, plan.template_hash):
        _validate_recovered_authority_files(plan)
        _finish_recovery(plan, staging, rollback, staged_files)
        return
    if staging.exists():
        for (authority_target, expected), activation in zip(_authority_files(plan), staged_files, strict=True):
            _recover_authority_file(authority_target, expected, *activation)
        if target.exists():
            if rollback.exists():
                raise LegacyMigrationError("migration recovery found both target and rollback State")
            _replace(target, rollback)
        _replace(staging, target)
        if not _read_receipt(plan.receipt_path, plan.source_hash, plan.template_hash):
            raise LegacyMigrationError("recovered migration State lacks its source receipt")
        _validate_recovered_authority_files(plan)
        _finish_recovery(plan, staging, rollback, staged_files)
        return
    if target.exists() or not rollback.exists():
        raise LegacyMigrationError("migration recovery has no committed receipt or valid staged State")
    rollback_errors = _rollback_activation(
        plan.request,
        staging,
        rollback,
        staged_files,
        state_activated=False,
    )
    if rollback_errors:
        raise LegacyMigrationError(
            f"migration recovery rollback was incomplete; immutable backup: {plan.backup_path}"
        ) from rollback_errors[0]
    plan.journal_path.unlink()


def _recover_authority_file(
    authority_target: Path,
    expected: bytes,
    staged: Path,
    target: Path,
    rollback: Path,
) -> None:
    if target != authority_target:
        raise LegacyMigrationError("migration recovery authority identity is inconsistent")
    if target.exists() and _regular_bytes(target, "recovered authority file") == expected:
        staged.unlink(missing_ok=True)
        return
    if not staged.exists() or _regular_bytes(staged, "staged authority file") != expected:
        raise LegacyMigrationError("migration recovery lacks a valid staged authority file")
    if target.exists():
        if rollback.exists():
            raise LegacyMigrationError("migration recovery found conflicting authority and rollback files")
        _replace(target, rollback)
    _replace(staged, target)


def _validate_recovered_authority_files(plan: _Plan) -> None:
    for target, expected in _authority_files(plan):
        if not target.exists() or _regular_bytes(target, "recovered authority file") != expected:
            raise LegacyMigrationError("committed migration has an incomplete authority-file switch")


def _finish_recovery(
    plan: _Plan,
    staging_state: Path,
    rollback_state: Path,
    staged_files: Sequence[tuple[Path, Path, Path]],
) -> None:
    if rollback_state.exists():
        shutil.rmtree(rollback_state)
    if staging_state.exists():
        shutil.rmtree(staging_state)
    for staged, _, rollback in staged_files:
        staged.unlink(missing_ok=True)
        rollback.unlink(missing_ok=True)
    _fsync_directory(plan.request.target_state_directory.parent)
    plan.journal_path.unlink()
    _fsync_directory(plan.journal_path.parent)


def _create_backup(plan: _Plan) -> None:
    if plan.backup_path.exists():
        manifest = plan.backup_path / "manifest.json"
        if manifest.is_file():
            value = _strict_canonical_json_file(manifest, "migration backup manifest")
            if value.get("sourceHash") == plan.source_hash and value.get("targetTemplateHash") == plan.template_hash:
                return
        raise LegacyMigrationError("immutable migration backup identity already exists with different content")
    temporary = plan.backup_path.parent / f".{plan.backup_path.name}.create-{uuid.uuid4().hex}"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    try:
        targets: list[dict[str, Any]] = []
        if plan.request.target_state_directory.exists():
            shutil.copytree(plan.request.target_state_directory, temporary / "state")
            targets.append({"name": "state", "present": True})
        else:
            targets.append({"name": "state", "present": False})
        files = (
            ("plugin-data", plan.request.target_plugin_data),
            ("agent-contract", plan.request.vault_root / "agent.md"),
            ("vault-skill", plan.request.vault_root / ".codex" / "skills" / "obsidian-cli" / "SKILL.md"),
        )
        for name, source in files:
            present = source.exists()
            targets.append({"name": name, "present": present})
            if present:
                payload = _regular_bytes(source, f"target {name}")
                _write_new_file(temporary / f"{name}.bin", payload)
        _write_new_file(
            temporary / "manifest.json",
            _canonical_json(
                {
                    "migration": MIGRATION_NAME,
                    "schemaVersion": MIGRATION_SCHEMA_VERSION,
                    "sourceHash": plan.source_hash,
                    "targetTemplateHash": plan.template_hash,
                    "targets": targets,
                }
            ),
        )
        _make_tree_read_only(temporary)
        _replace(temporary, plan.backup_path)
    except BaseException:
        if temporary.exists():
            _make_tree_writable(temporary)
            shutil.rmtree(temporary, ignore_errors=True)
        raise


def _read_receipt(path: Path, source_hash: str, template_hash: str) -> bool:
    if not path.exists():
        return False
    value = _strict_canonical_json_file(path, "migration receipt")
    if (
        value.get("migration") != MIGRATION_NAME
        or value.get("schemaVersion") != MIGRATION_SCHEMA_VERSION
        or value.get("sourceHash") != source_hash
        or value.get("targetTemplateHash") != template_hash
    ):
        raise LegacyMigrationError("migration receipt conflicts with the requested source")
    return True


def _source_hash(request: LegacyMigrationRequest) -> str:
    digest = hashlib.sha256()
    _hash_file(digest, "state", request.source_state)
    if request.source_attachments.exists():
        for entry in sorted(request.source_attachments.iterdir(), key=lambda item: item.name):
            if not entry.is_file() or entry.is_symlink():
                raise LegacyMigrationError("legacy attachment directory contains a non-regular entry")
            _hash_file(digest, f"attachment/{entry.name}", entry)
    else:
        digest.update(b"attachments\0absent\0")
    if request.source_plugin_data is not None and request.source_plugin_data.exists():
        _hash_file(digest, "plugin-data", request.source_plugin_data)
    else:
        digest.update(b"plugin-data\0absent\0")
    return f"sha256:{digest.hexdigest()}"


def _target_fingerprint(request: LegacyMigrationRequest) -> str:
    digest = hashlib.sha256()
    _hash_optional_tree(digest, "state", request.target_state_directory)
    for name, path in (
        ("plugin-data", request.target_plugin_data),
        ("agent-contract", request.vault_root / "agent.md"),
        ("vault-skill", request.vault_root / ".codex" / "skills" / "obsidian-cli" / "SKILL.md"),
    ):
        if path.exists():
            _hash_file(digest, name, path)
        else:
            digest.update(f"{name}\0absent\0".encode())
    return f"sha256:{digest.hexdigest()}"


def _hash_optional_tree(digest: Any, label: str, root: Path) -> None:
    if not root.exists():
        digest.update(f"{label}\0absent\0".encode())
        return
    if not root.is_dir() or root.is_symlink():
        raise LegacyMigrationError("target State directory is not a regular directory")
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise LegacyMigrationError("target State contains a symbolic link")
        if path.is_dir():
            digest.update(f"{label}/{relative}\0directory\0".encode())
        elif path.is_file():
            _hash_file(digest, f"{label}/{relative}", path)
        else:
            raise LegacyMigrationError("target State contains a special file")


def _hash_file(digest: Any, label: str, path: Path) -> None:
    payload = _regular_bytes(path, label)
    digest.update(label.encode())
    digest.update(b"\0")
    digest.update(str(len(payload)).encode())
    digest.update(b"\0")
    digest.update(payload)
    digest.update(b"\0")


def _regular_bytes(path: Path, label: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as error:
        raise LegacyMigrationError(f"{label} is unavailable") from error
    if path.is_symlink() or stat.S_IFMT(info.st_mode) != stat.S_IFREG or info.st_nlink != 1:
        raise LegacyMigrationError(f"{label} is not a unique regular file")
    try:
        return path.read_bytes()
    except OSError as error:
        raise LegacyMigrationError(f"{label} cannot be read") from error


def _strict_json_file(path: Path, label: str) -> dict[str, Any]:
    payload = _regular_bytes(path, label)
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise LegacyMigrationError(f"{label} is malformed") from error
    if not isinstance(value, dict):
        raise LegacyMigrationError(f"{label} must be a JSON object")
    return value


def _strict_canonical_json_file(path: Path, label: str) -> dict[str, Any]:
    value = _strict_json_file(path, label)
    if _regular_bytes(path, label) != _canonical_json(value):
        raise LegacyMigrationError(f"{label} is noncanonical")
    return value


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Never:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        is not None
    )


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise LegacyMigrationError(f"legacy {label} is not text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise LegacyMigrationError(f"legacy {label} is not RFC 3339") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LegacyMigrationError(f"legacy {label} is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _bounded_text(
    value: object,
    label: str,
    *,
    allow_empty: bool,
    maximum: int = 1_048_576,
) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value or (not allow_empty and not value):
        raise LegacyMigrationError(f"legacy {label} is outside the supported text boundary")
    return value


def _bounded_id(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\x00" in value
        or any(item in value for item in "/\\")
    ):
        raise LegacyMigrationError(f"legacy {label} identity is unsafe")
    return value


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise LegacyMigrationError(f"legacy {label} must be a positive integer")
    return value


def _legacy_boolean(value: object, label: str) -> bool:
    if type(value) is not int or value not in {0, 1}:
        raise LegacyMigrationError(f"legacy {label} must be 0 or 1")
    return bool(value)


def _mapped_id(namespace: str, source_hash: str, legacy_id: str) -> str:
    return f"{namespace}_mig_{_digest_text(f'{source_hash}:{namespace}:{legacy_id}')[:32]}"


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _control_digest(value: bytes) -> str:
    return _digest_bytes(value.replace(b"\r\n", b"\n"))


def _write_new_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _make_tree_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IREAD)


def _make_tree_writable(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IREAD | stat.S_IWRITE)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


class _FixedClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def utcnow(self) -> datetime:
        return self._value

    def monotonic(self) -> float:
        return 0.0

    async def sleep_until(self, deadline: datetime) -> None:
        del deadline


T = TypeVar("T")


def _run_async(operation: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(operation)
    result: list[T] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(operation))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run, name="offeragent-legacy-migration", daemon=False)
    worker.start()
    worker.join()
    if errors:
        raise errors[0]
    return result[0]


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy OfferAgent Obsidian State into the Python harness")
    parser.add_argument("--source-state", type=Path, required=True)
    parser.add_argument("--source-attachments", type=Path, required=True)
    parser.add_argument("--source-plugin-data", type=Path)
    parser.add_argument("--target-state-directory", type=Path, required=True)
    parser.add_argument("--target-plugin-data", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--target-templates", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--profile-id", default="profile_local")
    parser.add_argument("--dry-run", action="store_true")
    values = parser.parse_args(arguments)
    report = migrate_legacy_obsidian(
        LegacyMigrationRequest(
            source_state=values.source_state,
            source_attachments=values.source_attachments,
            source_plugin_data=values.source_plugin_data,
            target_state_directory=values.target_state_directory,
            target_plugin_data=values.target_plugin_data,
            vault_root=values.vault_root,
            target_templates=values.target_templates,
            workspace_id=values.workspace_id,
            profile_id=values.profile_id,
        ),
        dry_run=values.dry_run,
    )
    print(_canonical_json(report.to_wire()).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LEGACY_SCHEMA_VERSION",
    "LegacyMigrationError",
    "LegacyMigrationExclusion",
    "LegacyMigrationReport",
    "LegacyMigrationRequest",
    "migrate_legacy_obsidian",
]
