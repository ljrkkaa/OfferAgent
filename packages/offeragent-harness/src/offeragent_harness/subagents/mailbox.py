"""Durable ordered parent-to-child mailbox with idempotent message IDs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from offeragent_harness.error_codes import ResourceConflictCause
from offeragent_harness.ports import Clock, UnitOfWorkFactory
from offeragent_harness.tools.canonical import canonical_json_sha256

from .models import AgentSendCommand, MailboxMode, MailboxReceipt


@dataclass(frozen=True, slots=True)
class MailboxMessage:
    run_id: str
    message_id: str
    sequence: int
    mode: MailboxMode
    message: str
    artifact_ids: tuple[str, ...]
    sender_run_id: str
    received_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        if self.sequence < 1 or not self.run_id or not self.message_id or not self.message:
            raise ValueError("Mailbox message identity is invalid")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("Mailbox timestamp must be timezone-aware")
        expected = canonical_json_sha256(
            {
                "runId": self.run_id,
                "messageId": self.message_id,
                "mode": self.mode.value,
                "message": self.message,
                "artifactIds": list(self.artifact_ids),
                "senderRunId": self.sender_run_id,
            }
        )
        if self.content_hash != expected:
            raise ValueError("Mailbox message content hash does not match")


class MailboxConflict(RuntimeError, ResourceConflictCause):
    pass


class DurableMailbox:
    def __init__(self, unit_of_work: UnitOfWorkFactory, clock: Clock, *, max_messages_per_run: int = 1024) -> None:
        if not 1 <= max_messages_per_run <= 100_000:
            raise ValueError("Mailbox limit is invalid")
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._max_messages = max_messages_per_run
        self._locks: dict[str, asyncio.Lock] = {}
        self._notifications: dict[str, asyncio.Condition] = {}
        self._generations: dict[str, int] = {}

    async def send(self, command: AgentSendCommand) -> tuple[MailboxReceipt, MailboxMessage]:
        lock = self._locks.setdefault(command.run_id, asyncio.Lock())
        request_hash = canonical_json_sha256(
            {
                "requesterRunId": command.requester_run_id,
                "runId": command.run_id,
                "mode": command.mode.value,
                "message": command.message,
                "artifactIds": list(command.artifact_ids),
                "messageId": command.message_id,
            }
        )
        receipt_id = f"{command.run_id}:{command.message_id}"
        async with lock:
            async with self._unit_of_work.begin() as uow:
                replay = await uow.entities.get("subagent_mailbox_receipts", receipt_id)
                state = await uow.entities.get("subagent_mailbox_state", command.run_id)
                if replay is not None:
                    receipt, message, stored_hash = _decode_receipt(replay)
                    if stored_hash != request_hash:
                        raise MailboxConflict("messageId is bound to different mailbox content")
                    return receipt, message
                sequence = _next_sequence(state)
                if sequence > self._max_messages:
                    raise MailboxConflict("Subagent mailbox is full")
                message = MailboxMessage(
                    command.run_id,
                    command.message_id,
                    sequence,
                    command.mode,
                    command.message,
                    command.artifact_ids,
                    command.requester_run_id,
                    self._clock.utcnow(),
                    canonical_json_sha256(
                        {
                            "runId": command.run_id,
                            "messageId": command.message_id,
                            "mode": command.mode.value,
                            "message": command.message,
                            "artifactIds": list(command.artifact_ids),
                            "senderRunId": command.requester_run_id,
                        }
                    ),
                )
                receipt = MailboxReceipt(command.run_id, command.message_id, sequence, False)
                expected_state_revision = 0 if state is None else _state_revision(state)
                await uow.entities.put(
                    "subagent_mailbox_messages",
                    f"{command.run_id}:{sequence:08d}",
                    _message_to_value(message),
                    expected_revision=0,
                )
                await uow.entities.put(
                    "subagent_mailbox_state",
                    command.run_id,
                    {"schemaVersion": 1, "lastSequence": sequence, "revision": expected_state_revision + 1},
                    expected_revision=expected_state_revision,
                )
                await uow.entities.put(
                    "subagent_mailbox_receipts",
                    receipt_id,
                    {
                        "schemaVersion": 1,
                        "requestHash": request_hash,
                        "receipt": _receipt_to_value(receipt),
                        "message": _message_to_value(message),
                    },
                    expected_revision=0,
                )
                await uow.commit()
        condition = self._notifications.setdefault(command.run_id, asyncio.Condition())
        async with condition:
            self._generations[command.run_id] = self._generations.get(command.run_id, 0) + 1
            condition.notify_all()
        return receipt, message

    async def receive(self, run_id: str, *, after_sequence: int, limit: int = 100) -> tuple[MailboxMessage, ...]:
        if after_sequence < 0 or not 1 <= limit <= 1000:
            raise ValueError("Mailbox cursor/limit is invalid")
        prefix = f"{run_id}:"
        after_id = f"{run_id}:{after_sequence:08d}" if after_sequence else None
        async with self._unit_of_work.begin() as uow:
            page = await uow.entities.list("subagent_mailbox_messages", after_id=after_id, limit=limit)
        output: list[MailboxMessage] = []
        for item in page:
            if not item.entity_id.startswith(prefix):
                break
            output.append(_message_from_value(item.value))
        return tuple(output)

    async def wait_for_message(self, run_id: str, *, after_sequence: int, timeout: float) -> bool:
        condition = self._notifications.setdefault(run_id, asyncio.Condition())
        async with condition:
            generation = self._generations.get(run_id, 0)
        if await self.receive(run_id, after_sequence=after_sequence, limit=1):
            return True
        try:
            async with condition:
                await asyncio.wait_for(
                    condition.wait_for(lambda: self._generations.get(run_id, 0) != generation),
                    timeout,
                )
        except TimeoutError:
            return False
        return bool(await self.receive(run_id, after_sequence=after_sequence, limit=1))


def _next_sequence(value: Any) -> int:
    if value is None:
        return 1
    if not isinstance(value, Mapping) or value.get("schemaVersion") != 1:
        raise MailboxConflict("persisted mailbox state is corrupt")
    sequence = value.get("lastSequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise MailboxConflict("persisted mailbox sequence is corrupt")
    return sequence + 1


def _state_revision(value: Any) -> int:
    revision = value.get("revision") if isinstance(value, Mapping) else None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise MailboxConflict("persisted mailbox revision is corrupt")
    return revision


def _decode_receipt(value: Any) -> tuple[MailboxReceipt, MailboxMessage, str]:
    if not isinstance(value, Mapping) or set(value) != {"schemaVersion", "requestHash", "receipt", "message"}:
        raise MailboxConflict("persisted mailbox receipt is corrupt")
    receipt = _receipt_from_value(value["receipt"])
    message = _message_from_value(value["message"])
    request_hash = value["requestHash"]
    if value["schemaVersion"] != 1 or not isinstance(request_hash, str):
        raise MailboxConflict("persisted mailbox receipt fields are corrupt")
    return MailboxReceipt(receipt.run_id, receipt.message_id, receipt.sequence, True), message, request_hash


def _message_to_value(value: MailboxMessage) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "runId": value.run_id,
        "messageId": value.message_id,
        "sequence": value.sequence,
        "mode": value.mode.value,
        "message": value.message,
        "artifactIds": list(value.artifact_ids),
        "senderRunId": value.sender_run_id,
        "receivedAt": value.received_at.isoformat(),
        "contentHash": value.content_hash,
    }


def _message_from_value(value: Any) -> MailboxMessage:
    expected = {
        "schemaVersion",
        "runId",
        "messageId",
        "sequence",
        "mode",
        "message",
        "artifactIds",
        "senderRunId",
        "receivedAt",
        "contentHash",
    }
    if not isinstance(value, Mapping) or set(value) != expected or value["schemaVersion"] != 1:
        raise MailboxConflict("persisted mailbox message is corrupt")
    try:
        return MailboxMessage(
            str(value["runId"]),
            str(value["messageId"]),
            int(value["sequence"]),
            MailboxMode(str(value["mode"])),
            str(value["message"]),
            tuple(str(item) for item in value["artifactIds"]),
            str(value["senderRunId"]),
            datetime.fromisoformat(str(value["receivedAt"])),
            str(value["contentHash"]),
        )
    except (TypeError, ValueError) as error:
        raise MailboxConflict("persisted mailbox message fields are corrupt") from error


def _receipt_to_value(value: MailboxReceipt) -> dict[str, Any]:
    return {
        "runId": value.run_id,
        "messageId": value.message_id,
        "sequence": value.sequence,
        "duplicate": value.duplicate,
    }


def _receipt_from_value(value: Any) -> MailboxReceipt:
    if not isinstance(value, Mapping) or set(value) != {"runId", "messageId", "sequence", "duplicate"}:
        raise MailboxConflict("persisted mailbox receipt body is corrupt")
    try:
        return MailboxReceipt(
            str(value["runId"]),
            str(value["messageId"]),
            int(value["sequence"]),
            bool(value["duplicate"]),
        )
    except (TypeError, ValueError) as error:
        raise MailboxConflict("persisted mailbox receipt fields are corrupt") from error


__all__ = ["DurableMailbox", "MailboxConflict", "MailboxMessage"]
