"""Durable, append-audited trust decisions; catalog metadata stays in memory."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping

from offeragent_harness.ports.events import NewEvent
from offeragent_harness.ports.skills import SkillStateStore, SkillTrustDecision, SkillTrustRecord
from offeragent_harness.ports.storage import EntityRevisionConflict
from offeragent_harness.ports.system import Clock, IdGenerator
from offeragent_harness.ports.unit_of_work import UnitOfWorkFactory
from offeragent_harness.protocol.events import EventType, SkillTrustChangedPayload, make_domain_event_record

_COLLECTION = "skill_trust_decisions"


class InMemorySkillStateStore(SkillStateStore):
    def __init__(self) -> None:
        self._values: dict[tuple[str, str, str], SkillTrustRecord] = {}
        self._idempotency: dict[str, SkillTrustRecord] = {}
        self._lock = asyncio.Lock()

    async def get_trust(self, workspace_id: str, root_id: str, package_path: str) -> SkillTrustRecord | None:
        async with self._lock:
            return self._values.get((workspace_id, root_id, package_path))

    async def put_trust(
        self,
        decision: SkillTrustDecision,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> SkillTrustRecord:
        async with self._lock:
            replay = self._idempotency.get(idempotency_key)
            if replay is not None:
                if replay.decision != decision:
                    raise ValueError("Skill trust idempotency key is bound to another decision")
                return replay
            key = (decision.workspace_id, decision.root_id, decision.package_path)
            current = self._values.get(key)
            revision = 0 if current is None else current.revision
            if revision != expected_revision:
                raise EntityRevisionConflict(_COLLECTION, _entity_id(*key), expected_revision, revision)
            record = SkillTrustRecord(decision, revision + 1, idempotency_key)
            self._values[key] = record
            self._idempotency[idempotency_key] = record
            return record


class EntitySkillStateStore(SkillStateStore):
    """Trust decisions are the only persisted Skill state; no catalog shadow copy."""

    def __init__(self, factory: UnitOfWorkFactory, clock: Clock, ids: IdGenerator) -> None:
        self._factory = factory
        self._clock = clock
        self._ids = ids

    async def get_trust(self, workspace_id: str, root_id: str, package_path: str) -> SkillTrustRecord | None:
        async with self._factory.begin() as unit_of_work:
            raw = await unit_of_work.entities.get(_COLLECTION, _entity_id(workspace_id, root_id, package_path))
        return None if raw is None else _parse_record(raw)

    async def put_trust(
        self,
        decision: SkillTrustDecision,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> SkillTrustRecord:
        entity_id = _entity_id(decision.workspace_id, decision.root_id, decision.package_path)
        record = SkillTrustRecord(decision, expected_revision + 1, idempotency_key)
        try:
            async with self._factory.begin() as unit_of_work:
                revision = await unit_of_work.entities.put(
                    _COLLECTION,
                    entity_id,
                    _record_value(record),
                    expected_revision=expected_revision,
                )
                if revision != record.revision:
                    raise ValueError("Skill trust revision is inconsistent")
                event = make_domain_event_record(
                    event_type=EventType.SKILL_TRUST_CHANGED,
                    payload=SkillTrustChangedPayload(
                        root_id=decision.root_id,
                        package_path=decision.package_path,
                        name=decision.name,
                        metadata_hash=decision.metadata_hash,
                        confirmed=decision.confirmed,
                        record_revision=revision,
                    ),
                    trace_id=f"trace_{hashlib.sha256(idempotency_key.encode()).hexdigest()}",
                    workspace_id=decision.workspace_id,
                    session_id=None,
                    turn_id=None,
                    run_id=None,
                    root_run_id=None,
                    parent_run_id=None,
                    state_revision=revision,
                )
                stream = f"skill-trust:{decision.workspace_id}"
                sequence = await unit_of_work.events.latest_sequence(stream)
                await unit_of_work.events.append(
                    stream,
                    sequence,
                    (
                        NewEvent(
                            event_id=self._ids.new_id("evt"),
                            event_type=EventType.SKILL_TRUST_CHANGED.value,
                            payload=event.to_wire(),
                            occurred_at=self._clock.utcnow(),
                            terminal=False,
                            idempotency_key=idempotency_key,
                        ),
                    ),
                )
                await unit_of_work.commit()
            return record
        except EntityRevisionConflict:
            current = await self.get_trust(decision.workspace_id, decision.root_id, decision.package_path)
            if current is not None and current.decision == decision and current.idempotency_key == idempotency_key:
                return current
            raise


def _entity_id(workspace_id: str, root_id: str, package_path: str) -> str:
    digest = hashlib.sha256("\0".join((workspace_id, root_id, package_path)).encode()).hexdigest()
    return f"skill-trust-{digest}"


def _record_value(record: SkillTrustRecord) -> dict[str, object]:
    decision = record.decision
    return {
        "schemaVersion": 2,
        "recordRevision": record.revision,
        "idempotencyKey": record.idempotency_key,
        "workspaceId": decision.workspace_id,
        "rootId": decision.root_id,
        "packagePath": decision.package_path,
        "name": decision.name,
        "metadataHash": decision.metadata_hash,
        "confirmed": decision.confirmed,
    }


def _parse_record(raw: object) -> SkillTrustRecord:
    if not isinstance(raw, Mapping):
        raise ValueError("persisted Skill trust record is not an object")
    expected = {
        "schemaVersion",
        "recordRevision",
        "idempotencyKey",
        "workspaceId",
        "rootId",
        "packagePath",
        "name",
        "metadataHash",
        "confirmed",
    }
    if set(raw) != expected or raw["schemaVersion"] != 2:
        raise ValueError("persisted Skill trust record schema is unsupported")
    strings = ("idempotencyKey", "workspaceId", "rootId", "packagePath", "name", "metadataHash")
    if any(not isinstance(raw[key], str) or not raw[key] for key in strings):
        raise ValueError("persisted Skill trust strings are invalid")
    revision = raw["recordRevision"]
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(raw["confirmed"], bool)
    ):
        raise ValueError("persisted Skill trust types are invalid")
    return SkillTrustRecord(
        SkillTrustDecision(
            str(raw["workspaceId"]),
            str(raw["rootId"]),
            str(raw["packagePath"]),
            str(raw["name"]),
            str(raw["metadataHash"]),
            bool(raw["confirmed"]),
        ),
        revision,
        str(raw["idempotencyKey"]),
    )


__all__ = ["EntitySkillStateStore", "InMemorySkillStateStore"]
