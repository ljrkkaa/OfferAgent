"""Revisioned personal-memory repository over the existing Unit of Work."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from offeragent_harness.ports import CancellationToken, Clock, EntityRecord, IdGenerator, UnitOfWorkFactory
from offeragent_harness.sessions import Run, Session, Turn
from offeragent_harness.tools import ToolCall

from .models import (
    MemoryEvent,
    MemoryEventType,
    MemoryItem,
    MemoryKind,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    memory_content_hash,
    memory_event_from_json,
    memory_event_to_json,
    memory_item_from_json,
    memory_item_to_json,
)

MEMORY_ITEMS_COLLECTION = "memory_items"
MEMORY_EVENTS_COLLECTION = "memory_events"
_MAX_SCAN = 100_000
_PAGE_SIZE = 500
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", re.IGNORECASE)
_SECRET = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+\-/]+=*|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|cookie|client[_-]?secret|secret)"
    r"\s*(?::|=|\bis\b)\s*\S+)"
)


class MemoryStoreError(RuntimeError):
    pass


class MemoryNotFound(MemoryStoreError):
    pass


class MemoryScopeViolation(MemoryStoreError):
    pass


class MemoryEvidenceError(MemoryStoreError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryActor:
    workspace_id: str
    profile_id: str
    session_id: str
    turn_id: str
    run_id: str
    user_texts: tuple[str, ...]
    root_run: bool


@dataclass(frozen=True, slots=True)
class MemorySearchHit:
    item: MemoryItem
    score: float


class MemoryRepository:
    """Keep canonical memory and its append-only lifecycle events in one SQLite UoW."""

    def __init__(
        self,
        *,
        workspace_id: str,
        unit_of_work: UnitOfWorkFactory,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        if not workspace_id or workspace_id.strip() != workspace_id:
            raise ValueError("memory repository requires a canonical Workspace ID")
        self.workspace_id = workspace_id
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._ids = ids

    async def actor(self, call: ToolCall, cancellation: CancellationToken) -> MemoryActor:
        cancellation.checkpoint()
        if call.workspace_id != self.workspace_id:
            raise MemoryScopeViolation("memory ToolCall belongs to another Workspace")
        async with self._unit_of_work.begin() as uow:
            run = await uow.entities.get("runs", call.run_id)
            if not isinstance(run, Run) or run.workspace_id != self.workspace_id or run.lineage != call.lineage:
                raise MemoryScopeViolation("memory ToolCall has no authoritative Run")
            session = await uow.entities.get("sessions", run.session_id)
            turn = await uow.entities.get("turns", run.turn_id)
        if not isinstance(session, Session) or session.workspace_id != self.workspace_id:
            raise MemoryScopeViolation("memory ToolCall has no authoritative Session")
        if not isinstance(turn, Turn) or turn.session_id != session.session_id:
            raise MemoryScopeViolation("memory ToolCall has no authoritative Turn")
        texts = _turn_texts(turn.input_blocks)
        if not texts:
            raise MemoryEvidenceError("memory write authority requires a textual user input")
        return MemoryActor(
            self.workspace_id,
            session.profile_id,
            session.session_id,
            turn.turn_id,
            run.run_id,
            texts,
            call.lineage.depth == 0,
        )

    async def remember(
        self,
        call: ToolCall,
        *,
        key: str,
        kind: MemoryKind,
        scope: MemoryScope,
        content: str,
        evidence_quote: str,
        pinned: bool,
        importance: int,
        expires_at: datetime | None,
        cancellation: CancellationToken,
    ) -> MemoryItem:
        actor = await self.actor(call, cancellation)
        self._require_root_write(actor)
        _require_evidence(actor, evidence_quote)
        if content not in evidence_quote:
            raise MemoryEvidenceError("confirmed memory content must be an exact span of the cited user quote")
        _reject_secret(content)
        _reject_secret(evidence_quote)
        return await self._create(
            actor,
            key=key,
            kind=kind,
            scope=scope,
            content=content,
            evidence_quote=evidence_quote,
            pinned=pinned,
            importance=importance,
            expires_at=expires_at,
            status=MemoryStatus.CONFIRMED,
            event_type=MemoryEventType.CONFIRMED,
            cancellation=cancellation,
        )

    async def propose(
        self,
        call: ToolCall,
        *,
        key: str,
        kind: MemoryKind,
        scope: MemoryScope,
        content: str,
        evidence_quote: str,
        pinned: bool,
        importance: int,
        expires_at: datetime | None,
        cancellation: CancellationToken,
    ) -> MemoryItem:
        actor = await self.actor(call, cancellation)
        self._require_root_write(actor)
        _require_evidence(actor, evidence_quote)
        _reject_secret(content)
        _reject_secret(evidence_quote)
        return await self._create(
            actor,
            key=key,
            kind=kind,
            scope=scope,
            content=content,
            evidence_quote=evidence_quote,
            pinned=pinned,
            importance=importance,
            expires_at=expires_at,
            status=MemoryStatus.PROPOSED,
            event_type=MemoryEventType.PROPOSED,
            cancellation=cancellation,
        )

    async def confirm(
        self,
        call: ToolCall,
        *,
        memory_id: str,
        evidence_quote: str,
        cancellation: CancellationToken,
    ) -> MemoryItem:
        actor = await self.actor(call, cancellation)
        self._require_root_write(actor)
        _require_evidence(actor, evidence_quote)
        now = self._clock.utcnow()
        async with self._unit_of_work.begin() as uow:
            records = await _all_records(uow.entities, MEMORY_ITEMS_COLLECTION, cancellation)
            record, item = _owned_record(records, memory_id, actor)
            if item.status is not MemoryStatus.PROPOSED:
                raise MemoryStoreError("only proposed memory can be confirmed")
            superseded = _confirmed_same_key(records, item, exclude_id=item.memory_id)
            for previous_record, previous in superseded:
                updated = previous.transition(MemoryStatus.SUPERSEDED, now=now)
                await _put_item(uow.entities, previous_record, updated)
                await self._append_event(
                    uow.entities,
                    updated,
                    MemoryEventType.SUPERSEDED,
                    actor,
                    now,
                )
            confirmed = item.transition(
                MemoryStatus.CONFIRMED,
                now=now,
                supersedes_id=_latest_id(superseded),
            )
            await _put_item(uow.entities, record, confirmed)
            await self._append_event(uow.entities, confirmed, MemoryEventType.CONFIRMED, actor, now)
            await uow.commit()
        return confirmed

    async def forget(
        self,
        call: ToolCall,
        *,
        memory_id: str,
        evidence_quote: str,
        cancellation: CancellationToken,
    ) -> MemoryItem:
        actor = await self.actor(call, cancellation)
        self._require_root_write(actor)
        _require_evidence(actor, evidence_quote)
        now = self._clock.utcnow()
        async with self._unit_of_work.begin() as uow:
            records = await _all_records(uow.entities, MEMORY_ITEMS_COLLECTION, cancellation)
            record, item = _owned_record(records, memory_id, actor)
            if item.status not in {MemoryStatus.PROPOSED, MemoryStatus.CONFIRMED}:
                raise MemoryStoreError("memory is already inactive")
            forgotten = item.transition(MemoryStatus.FORGOTTEN, now=now)
            await _put_item(uow.entities, record, forgotten)
            await self._append_event(uow.entities, forgotten, MemoryEventType.FORGOTTEN, actor, now)
            await uow.commit()
        return forgotten

    async def get(
        self,
        call: ToolCall,
        memory_id: str,
        cancellation: CancellationToken,
    ) -> MemoryItem:
        actor = await self.actor(call, cancellation)
        records = await self._records(cancellation)
        _, item = _owned_record(records, memory_id, actor)
        return item

    async def search(
        self,
        call: ToolCall,
        *,
        query: str,
        limit: int,
        cancellation: CancellationToken,
    ) -> tuple[MemorySearchHit, ...]:
        actor = await self.actor(call, cancellation)
        return await self.search_for_scope(
            profile_id=actor.profile_id,
            session_id=actor.session_id,
            query=query,
            limit=limit,
            pinned_only=False,
            cancellation=cancellation,
        )

    async def search_for_scope(
        self,
        *,
        profile_id: str,
        session_id: str,
        query: str,
        limit: int,
        pinned_only: bool,
        cancellation: CancellationToken,
    ) -> tuple[MemorySearchHit, ...]:
        if not query or query.strip() != query or not 1 <= limit <= 50:
            raise ValueError("memory query/limit is invalid")
        now = self._clock.utcnow()
        query_terms = _terms(query)
        records = await self._records(cancellation)
        hits: list[MemorySearchHit] = []
        for record in records:
            item = memory_item_from_json(record.value)
            if item.workspace_id != self.workspace_id or not item.visible_at(
                now,
                profile_id=profile_id,
                session_id=session_id,
            ):
                continue
            if pinned_only and not item.pinned:
                continue
            score = _score(query, query_terms, item, now)
            if score > 0:
                hits.append(MemorySearchHit(item, score))
        hits.sort(
            key=lambda hit: (-hit.score, -hit.item.importance, -hit.item.updated_at.timestamp(), hit.item.memory_id)
        )
        return tuple(hits[:limit])

    async def pinned(
        self,
        *,
        profile_id: str,
        session_id: str,
        limit: int,
        cancellation: CancellationToken,
    ) -> tuple[MemoryItem, ...]:
        now = self._clock.utcnow()
        records = await self._records(cancellation)
        items: list[MemoryItem] = []
        for record in records:
            item = memory_item_from_json(record.value)
            if (
                item.workspace_id == self.workspace_id
                and item.pinned
                and item.visible_at(now, profile_id=profile_id, session_id=session_id)
            ):
                items.append(item)
        items.sort(key=lambda item: (-item.importance, -item.updated_at.timestamp(), item.memory_id))
        return tuple(items[:limit])

    async def pending(
        self,
        call: ToolCall,
        *,
        limit: int,
        cancellation: CancellationToken,
    ) -> tuple[MemoryItem, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("pending memory limit is invalid")
        actor = await self.actor(call, cancellation)
        now = self._clock.utcnow()
        records = await self._records(cancellation)
        items: list[MemoryItem] = []
        for record in records:
            item = memory_item_from_json(record.value)
            in_scope = item.scope is MemoryScope.PROFILE or item.session_id == actor.session_id
            if (
                item.workspace_id == actor.workspace_id
                and item.profile_id == actor.profile_id
                and in_scope
                and item.status is MemoryStatus.PROPOSED
                and (item.expires_at is None or item.expires_at > now)
            ):
                items.append(item)
        items.sort(key=lambda item: (-item.importance, -item.updated_at.timestamp(), item.memory_id))
        return tuple(items[:limit])

    async def history(
        self,
        call: ToolCall,
        memory_id: str,
        cancellation: CancellationToken,
    ) -> tuple[MemoryEvent, ...]:
        actor = await self.actor(call, cancellation)
        records = await self._records(cancellation)
        _owned_record(records, memory_id, actor)
        async with self._unit_of_work.begin() as uow:
            event_records = await _all_records(uow.entities, MEMORY_EVENTS_COLLECTION, cancellation)
        events = [
            memory_event_from_json(record.value)
            for record in event_records
            if isinstance(record.value, dict) and record.value.get("memoryId") == memory_id
        ]
        events.sort(key=lambda event: (event.revision, event.occurred_at, event.event_id))
        return tuple(events)

    async def _create(
        self,
        actor: MemoryActor,
        *,
        key: str,
        kind: MemoryKind,
        scope: MemoryScope,
        content: str,
        evidence_quote: str,
        pinned: bool,
        importance: int,
        expires_at: datetime | None,
        status: MemoryStatus,
        event_type: MemoryEventType,
        cancellation: CancellationToken,
    ) -> MemoryItem:
        now = self._clock.utcnow()
        if expires_at is not None and expires_at <= now:
            raise ValueError("new memory expiry must be in the future")
        source = MemorySource(actor.session_id, actor.turn_id, actor.run_id, evidence_quote)
        session_id = actor.session_id if scope is MemoryScope.SESSION else None
        memory_id = self._ids.new_id("memory")
        item = MemoryItem(
            memory_id=memory_id,
            workspace_id=self.workspace_id,
            profile_id=actor.profile_id,
            session_id=session_id,
            key=key,
            kind=kind,
            scope=scope,
            status=status,
            content=content,
            pinned=pinned,
            importance=importance,
            source=source,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            supersedes_id=None,
            content_hash=memory_content_hash(
                workspace_id=self.workspace_id,
                profile_id=actor.profile_id,
                session_id=session_id,
                key=key,
                kind=kind,
                scope=scope,
                content=content,
                source=source,
            ),
            revision=1,
        )
        async with self._unit_of_work.begin() as uow:
            records = await _all_records(uow.entities, MEMORY_ITEMS_COLLECTION, cancellation)
            if status is MemoryStatus.CONFIRMED:
                superseded = _confirmed_same_key(records, item)
                for previous_record, previous in superseded:
                    updated = previous.transition(MemoryStatus.SUPERSEDED, now=now)
                    await _put_item(uow.entities, previous_record, updated)
                    await self._append_event(
                        uow.entities,
                        updated,
                        MemoryEventType.SUPERSEDED,
                        actor,
                        now,
                    )
                latest = _latest_id(superseded)
                if latest is not None:
                    item = replace(item, supersedes_id=latest)
            await uow.entities.put(
                MEMORY_ITEMS_COLLECTION, item.memory_id, memory_item_to_json(item), expected_revision=0
            )
            await self._append_event(uow.entities, item, event_type, actor, now)
            await uow.commit()
        return item

    async def _append_event(
        self,
        entities: Any,
        item: MemoryItem,
        event_type: MemoryEventType,
        actor: MemoryActor,
        now: datetime,
    ) -> None:
        event = MemoryEvent(
            event_id=self._ids.new_id("memory-event"),
            memory_id=item.memory_id,
            workspace_id=item.workspace_id,
            profile_id=item.profile_id,
            event_type=event_type,
            status=item.status,
            occurred_at=now,
            actor_run_id=actor.run_id,
            source_turn_id=actor.turn_id,
            revision=item.revision,
        )
        await entities.put(MEMORY_EVENTS_COLLECTION, event.event_id, memory_event_to_json(event), expected_revision=0)

    async def _records(self, cancellation: CancellationToken) -> tuple[EntityRecord, ...]:
        async with self._unit_of_work.begin() as uow:
            return await _all_records(uow.entities, MEMORY_ITEMS_COLLECTION, cancellation)

    @staticmethod
    def _require_root_write(actor: MemoryActor) -> None:
        if not actor.root_run:
            raise MemoryScopeViolation("Subagents cannot create, confirm, supersede, or forget personal memory")


async def _all_records(entities: Any, collection: str, cancellation: CancellationToken) -> tuple[EntityRecord, ...]:
    selected: list[EntityRecord] = []
    after: str | None = None
    while len(selected) < _MAX_SCAN:
        cancellation.checkpoint()
        page = await entities.list(collection, after_id=after, limit=min(_PAGE_SIZE, _MAX_SCAN - len(selected)))
        if not page:
            return tuple(selected)
        selected.extend(page)
        after = page[-1].entity_id
        if len(page) < _PAGE_SIZE:
            return tuple(selected)
    extra = await entities.list(collection, after_id=after, limit=1)
    if extra:
        raise MemoryStoreError(f"{collection} exceeds the bounded scan limit")
    return tuple(selected)


def _owned_record(
    records: Sequence[EntityRecord],
    memory_id: str,
    actor: MemoryActor,
) -> tuple[EntityRecord, MemoryItem]:
    record = next((value for value in records if value.entity_id == memory_id), None)
    if record is None:
        raise MemoryNotFound(f"memory {memory_id!r} does not exist")
    item = memory_item_from_json(record.value)
    if item.workspace_id != actor.workspace_id or item.profile_id != actor.profile_id:
        raise MemoryScopeViolation("memory belongs to another profile or Workspace")
    if item.scope is MemoryScope.SESSION and item.session_id != actor.session_id:
        raise MemoryScopeViolation("session memory belongs to another Session")
    if item.revision != record.revision:
        raise MemoryStoreError("memory entity and domain revisions diverged")
    return record, item


def _confirmed_same_key(
    records: Sequence[EntityRecord],
    item: MemoryItem,
    *,
    exclude_id: str | None = None,
) -> tuple[tuple[EntityRecord, MemoryItem], ...]:
    selected: list[tuple[EntityRecord, MemoryItem]] = []
    for record in records:
        candidate = memory_item_from_json(record.value)
        if (
            candidate.memory_id != exclude_id
            and candidate.workspace_id == item.workspace_id
            and candidate.profile_id == item.profile_id
            and candidate.scope is item.scope
            and candidate.session_id == item.session_id
            and candidate.key == item.key
            and candidate.status is MemoryStatus.CONFIRMED
        ):
            selected.append((record, candidate))
    selected.sort(key=lambda pair: (pair[1].updated_at, pair[1].memory_id))
    return tuple(selected)


def _latest_id(values: Sequence[tuple[EntityRecord, MemoryItem]]) -> str | None:
    return values[-1][1].memory_id if values else None


async def _put_item(entities: Any, record: EntityRecord, item: MemoryItem) -> None:
    if item.revision != record.revision + 1:
        raise MemoryStoreError("memory update revision is not monotonic")
    await entities.put(
        MEMORY_ITEMS_COLLECTION,
        item.memory_id,
        memory_item_to_json(item),
        expected_revision=record.revision,
    )


def _require_evidence(actor: MemoryActor, evidence_quote: str) -> None:
    if (
        not evidence_quote
        or evidence_quote.strip() != evidence_quote
        or len(evidence_quote.encode("utf-8")) > 4096
        or not any(evidence_quote in text for text in actor.user_texts)
    ):
        raise MemoryEvidenceError("memory evidenceQuote must be an exact span of the current user input")


def _reject_secret(content: str) -> None:
    if _SECRET.search(content):
        raise MemoryEvidenceError("credentials and secrets must not be persisted as personal memory")


def _turn_texts(blocks: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    texts: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type")
        text = block.get("text")
        if kind == "text" and isinstance(text, str) and text:
            texts.append(text)
    return tuple(texts)


def _terms(text: str) -> frozenset[str]:
    folded = text.casefold()
    terms: set[str] = set()
    for match in _WORD.finditer(folded):
        token = match.group(0)
        terms.add(token)
        terms.update(part for part in re.split(r"[-_.]+", token) if part)
    for match in _CJK.finditer(folded):
        value = match.group(0)
        if len(value) == 1:
            terms.add(value)
        else:
            terms.update(value[index : index + 2] for index in range(len(value) - 1))
    return frozenset(terms)


def _score(query: str, query_terms: frozenset[str], item: MemoryItem, now: datetime) -> float:
    searchable = f"{item.key} {item.kind.value} {item.content}"
    item_terms = _terms(searchable)
    overlap = query_terms & item_terms
    if not overlap:
        return 0.0
    coverage = len(overlap) / max(1, len(query_terms))
    specificity = len(overlap) / max(1, len(item_terms))
    folded_query = " ".join(query.casefold().split())
    folded_content = " ".join(item.content.casefold().split())
    phrase = 1.0 if folded_query in folded_content or folded_content in folded_query else 0.0
    age_days = max(0.0, (now - item.updated_at).total_seconds() / 86_400)
    recency = math.exp(-age_days / 365)
    score = 0.55 * coverage + 0.2 * specificity + 0.1 * phrase + 0.1 * (item.importance / 5) + 0.05 * recency
    return round(score, 6)


__all__ = [
    "MEMORY_EVENTS_COLLECTION",
    "MEMORY_ITEMS_COLLECTION",
    "MemoryActor",
    "MemoryEvidenceError",
    "MemoryNotFound",
    "MemoryRepository",
    "MemoryScopeViolation",
    "MemorySearchHit",
    "MemoryStoreError",
]
