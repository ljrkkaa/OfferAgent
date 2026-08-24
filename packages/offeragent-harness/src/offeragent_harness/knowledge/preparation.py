from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast, runtime_checkable

from offeragent_harness.foundation.canonical import canonical_json_bytes, canonical_json_sha256
from offeragent_harness.ports import CancellationToken

from .catalog import KnowledgeCatalogStore
from .discovery import KnowledgeDiscovery
from .models import CatalogSnapshot, KnowledgeStatus, PageEvidence, PageIndexTree, SourceRecord, SourceState
from .naming import SOURCE_ID_PATTERN
from .objects import KnowledgeObjectStore, PreparedKnowledgeSource
from .pageindex import StructuralPageIndexBuilder
from .semantic import combined_parser_fingerprint

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_ID = re.compile(SOURCE_ID_PATTERN)
_INGESTION_ID = re.compile(r"^ing-[0-9a-f]{32}$")
_PREPARATION_SCHEMA_VERSION = 2
_MARKDOWN_PARSER_FINGERPRINT = canonical_json_sha256(
    {"mediaType": "text/markdown", "pagePolicy": "whole-document", "schemaVersion": 1}
)


class KnowledgePreparationError(RuntimeError):
    pass


@runtime_checkable
class KnowledgeCancellation(Protocol):
    def checkpoint(self) -> None: ...


@dataclass(frozen=True, slots=True)
class KnowledgeParseResult:
    source_id: str
    source_hash: str
    pages: tuple[PageEvidence, ...]
    parser_fingerprint: str

    def __post_init__(self) -> None:
        if _SOURCE_ID.fullmatch(self.source_id) is None or _SHA256.fullmatch(self.source_hash) is None:
            raise ValueError("knowledge parser source identity is invalid")
        if not self.pages or [page.page_number for page in self.pages] != list(range(1, len(self.pages) + 1)):
            raise ValueError("knowledge parser pages must be contiguous and one-based")
        if _SHA256.fullmatch(self.parser_fingerprint) is None:
            raise ValueError("knowledge parser fingerprint is invalid")


@runtime_checkable
class KnowledgeBinaryParser(Protocol):
    @property
    def parser_fingerprint(self) -> str: ...

    async def parse(
        self,
        *,
        source: SourceRecord,
        absolute_path: Path,
        cancellation: KnowledgeCancellation,
    ) -> KnowledgeParseResult: ...


@runtime_checkable
class KnowledgePageIndexBuilder(Protocol):
    @property
    def fingerprint(self) -> str: ...

    async def build(
        self,
        *,
        source_id: str,
        source_hash: str,
        title: str,
        pages: tuple[PageEvidence, ...],
        run_id: str,
        cancellation: CancellationToken,
    ) -> PageIndexTree: ...


@dataclass(frozen=True, slots=True)
class KnowledgePreparation:
    ingestion_id: str
    base_revision: int
    candidate_ids: tuple[str, ...]
    prepared_sources: tuple[PreparedKnowledgeSource, ...]
    target_sources: tuple[SourceRecord, ...]
    removed_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _INGESTION_ID.fullmatch(self.ingestion_id) is None or self.base_revision < 0:
            raise ValueError("knowledge preparation identity is invalid")
        if not self.candidate_ids or any(_SHA256.fullmatch(item) is None for item in self.candidate_ids):
            raise ValueError("knowledge preparation candidate identities are invalid")
        if tuple(sorted(set(self.candidate_ids))) != self.candidate_ids:
            raise ValueError("knowledge preparation candidates must be uniquely sorted")
        prepared_ids = [item.source.source_id for item in self.prepared_sources]
        if (
            not prepared_ids
            or len(prepared_ids) != len(set(prepared_ids))
            or len(prepared_ids) != len(self.candidate_ids)
        ):
            raise ValueError("knowledge preparation sources must be non-empty and unique")
        CatalogSnapshot(self.base_revision, self.target_sources)
        if tuple(sorted(set(self.removed_source_ids))) != self.removed_source_ids or any(
            _SOURCE_ID.fullmatch(item) is None for item in self.removed_source_ids
        ):
            raise ValueError("knowledge preparation removals must be uniquely sorted")
        target_ids = {source.source_id for source in self.target_sources}
        if not set(prepared_ids) <= target_ids or set(self.removed_source_ids) & target_ids:
            raise ValueError("knowledge preparation target catalog is inconsistent")
        expected_id = _ingestion_id(
            self.base_revision,
            self.candidate_ids,
            self.removed_source_ids,
            self.prepared_sources,
            self.target_sources,
        )
        if self.ingestion_id != expected_id:
            raise ValueError("knowledge preparation content does not match its ingestion identity")


class KnowledgePreparationStore:
    """Durable binding from an LLM-visible ingestion ID to immutable source objects."""

    def __init__(self, state_root: Path, objects: KnowledgeObjectStore) -> None:
        if not state_root.is_absolute():
            raise ValueError("knowledge preparation state root must be absolute")
        self._root = state_root / "preparations"
        self._objects = objects

    def put(self, preparation: KnowledgePreparation) -> Path:
        for prepared in preparation.prepared_sources:
            self._objects.put(prepared)
        target = self._path(preparation.ingestion_id)
        payload = canonical_json_bytes(_preparation_json(preparation))
        if target.exists():
            if target.read_bytes() != payload:
                raise KnowledgePreparationError("knowledge preparation identity collision")
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        if temporary.exists():
            raise KnowledgePreparationError("knowledge preparation temporary record already exists")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return target

    def load(self, ingestion_id: str) -> KnowledgePreparation:
        path = self._path(ingestion_id)
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8", errors="strict"))
            if canonical_json_bytes(value) != raw:
                raise ValueError("record is not canonical")
            root = _object(
                value,
                {
                    "baseRevision",
                    "candidateIds",
                    "ingestionId",
                    "preparedSources",
                    "removedSourceIds",
                    "schemaVersion",
                    "targetSources",
                },
            )
            if root["schemaVersion"] != _PREPARATION_SCHEMA_VERSION:
                raise ValueError("record schema is invalid")
            refs = tuple(
                _object(item, {"parserFingerprint", "sourceHash", "sourceId"})
                for item in _list(root["preparedSources"])
            )
            prepared = tuple(
                self._objects.load(
                    _string(item["sourceId"]),
                    _string(item["sourceHash"]),
                    _string(item["parserFingerprint"]),
                )
                for item in refs
            )
            result = KnowledgePreparation(
                _string(root["ingestionId"]),
                _integer(root["baseRevision"], minimum=0),
                tuple(_string(item) for item in _list(root["candidateIds"])),
                prepared,
                tuple(_source_from_json(item) for item in _list(root["targetSources"])),
                tuple(_string(item) for item in _list(root["removedSourceIds"])),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise KnowledgePreparationError("knowledge preparation record is invalid") from error
        if result.ingestion_id != ingestion_id:
            raise KnowledgePreparationError("knowledge preparation path identity mismatch")
        return result

    def remove(self, ingestion_id: str) -> None:
        self._path(ingestion_id).unlink(missing_ok=True)

    def _path(self, ingestion_id: str) -> Path:
        if _INGESTION_ID.fullmatch(ingestion_id) is None:
            raise ValueError("knowledge ingestion identity is invalid")
        return self._root / f"{ingestion_id}.json"


class KnowledgePreparationService:
    def __init__(
        self,
        *,
        vault_root: Path,
        discovery: KnowledgeDiscovery,
        catalog: KnowledgeCatalogStore,
        objects: KnowledgeObjectStore,
        preparations: KnowledgePreparationStore,
        binary_parser: KnowledgeBinaryParser | None,
        page_index_builder: KnowledgePageIndexBuilder | None = None,
        maximum_markdown_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if not vault_root.is_absolute() or maximum_markdown_bytes < 1:
            raise ValueError("knowledge preparation configuration is invalid")
        self._vault = vault_root.resolve(strict=True)
        self._discovery = discovery
        self._catalog = catalog
        self._objects = objects
        self._preparations = preparations
        self._binary_parser = binary_parser
        self._page_index_builder = page_index_builder
        self._maximum_markdown_bytes = maximum_markdown_bytes

    def status(self) -> KnowledgeStatus:
        catalog = self._catalog.load()
        discovered = self._discovery.status(catalog_revision=catalog.revision, catalog=catalog.sources)
        candidates = []
        for candidate in discovered.candidates:
            state = candidate.state
            if state is SourceState.UNCHANGED:
                expected = self._expected_fingerprint(candidate.source)
                if expected is not None:
                    expected_path = self._objects.object_path(
                        candidate.source.source_id,
                        candidate.source.content_hash,
                        expected,
                    )
                    legacy_path = self._objects.object_path(
                        candidate.source.source_id,
                        candidate.source.content_hash,
                    )
                    if not expected_path.exists() and legacy_path.exists():
                        state = SourceState.OUTDATED
            candidates.append(
                type(candidate)(candidate.candidate_id, candidate.catalog_revision, state, candidate.source)
            )
        return KnowledgeStatus(discovered.catalog_revision, tuple(candidates), discovered.missing)

    async def prepare(
        self,
        *,
        candidate_ids: tuple[str, ...],
        removed_source_ids: tuple[str, ...] = (),
        run_id: str | None = None,
        cancellation: KnowledgeCancellation,
    ) -> KnowledgePreparation:
        requested = tuple(sorted(set(candidate_ids)))
        removals = tuple(sorted(set(removed_source_ids)))
        if requested != candidate_ids or removals != removed_source_ids or not requested:
            raise KnowledgePreparationError("knowledge preparation inputs must be non-empty, unique, and sorted")
        cancellation.checkpoint()
        current = self._catalog.load()
        status = self.status()
        candidates = {candidate.candidate_id: candidate for candidate in status.candidates}
        if set(requested) != {item for item in requested if item in candidates}:
            raise KnowledgePreparationError("knowledge preparation candidate is stale or unknown")
        selected = tuple(candidates[item] for item in requested)
        if any(candidate.state is SourceState.UNCHANGED for candidate in selected):
            raise KnowledgePreparationError("unchanged sources do not require preparation")
        missing_ids = {source.source_id for source in status.missing}
        if not set(removals) <= missing_ids:
            raise KnowledgePreparationError("knowledge preparation removal is not currently missing")

        prepared: list[PreparedKnowledgeSource] = []
        for candidate in selected:
            cancellation.checkpoint()
            source = candidate.source
            absolute = self._source_path(source)
            if not await asyncio.to_thread(_source_matches, absolute, source):
                raise KnowledgePreparationError("knowledge source changed after discovery")
            parser_fingerprint = (
                _MARKDOWN_PARSER_FINGERPRINT
                if source.media_type == "text/markdown"
                else (self._binary_parser.parser_fingerprint if self._binary_parser is not None else None)
            )
            expected_fingerprint = (
                None
                if parser_fingerprint is None
                else combined_parser_fingerprint(
                    parser_fingerprint,
                    None if self._page_index_builder is None else self._page_index_builder.fingerprint,
                )
            )
            if expected_fingerprint is None:
                raise KnowledgePreparationError("knowledge source parser is unavailable")
            object_path = self._objects.object_path(
                source.source_id,
                source.content_hash,
                expected_fingerprint,
            )
            if object_path.exists():
                cached = await asyncio.to_thread(
                    self._objects.load,
                    source.source_id,
                    source.content_hash,
                    expected_fingerprint,
                )
                if cached.parser_fingerprint != expected_fingerprint:
                    raise KnowledgePreparationError("cached source was built by a different parser profile")
                if cached.source != source:
                    raise KnowledgePreparationError("cached source metadata does not match discovery")
                prepared.append(cached)
                continue
            if source.media_type == "text/markdown":
                parsed = await asyncio.to_thread(self._parse_markdown, source, absolute)
            else:
                if self._binary_parser is None:
                    raise KnowledgePreparationError("binary document parser is unavailable")
                parsed = await self._binary_parser.parse(
                    source=source,
                    absolute_path=absolute,
                    cancellation=cancellation,
                )
            if parsed.source_id != source.source_id or parsed.source_hash != source.content_hash:
                raise KnowledgePreparationError("knowledge parser output does not match the requested source")
            if parser_fingerprint is None or parsed.parser_fingerprint != parser_fingerprint:
                raise KnowledgePreparationError("knowledge parser fingerprint changed during preparation")
            cancellation.checkpoint()
            title = PurePosixPath(source.relative_path).stem
            if self._page_index_builder is None:
                tree = StructuralPageIndexBuilder().build(
                    source_id=source.source_id,
                    source_hash=source.content_hash,
                    title=title,
                    pages=parsed.pages,
                )
            else:
                if run_id is None:
                    raise KnowledgePreparationError(
                        "semantic PageIndex preparation requires the invoking Agent Run identity"
                    )
                tree = await self._page_index_builder.build(
                    source_id=source.source_id,
                    source_hash=source.content_hash,
                    title=title,
                    pages=parsed.pages,
                    run_id=run_id,
                    cancellation=cast(CancellationToken, cancellation),
                )
            item = PreparedKnowledgeSource(
                source,
                parsed.pages,
                tree,
                combined_parser_fingerprint(
                    parsed.parser_fingerprint,
                    None if self._page_index_builder is None else self._page_index_builder.fingerprint,
                ),
            )
            await asyncio.to_thread(self._objects.put, item)
            prepared.append(item)

        target_by_id = {source.source_id: source for source in current.sources}
        for source_id in removals:
            target_by_id.pop(source_id, None)
        for item in prepared:
            target_by_id[item.source.source_id] = item.source
        target = tuple(sorted(target_by_id.values(), key=lambda item: item.relative_path.casefold()))
        ingestion_id = _ingestion_id(current.revision, requested, removals, prepared, target)
        result = KnowledgePreparation(
            ingestion_id,
            current.revision,
            requested,
            tuple(prepared),
            target,
            removals,
        )
        await asyncio.to_thread(self._preparations.put, result)
        return result

    def _expected_fingerprint(self, source: SourceRecord) -> str | None:
        parser = (
            _MARKDOWN_PARSER_FINGERPRINT
            if source.media_type == "text/markdown"
            else (self._binary_parser.parser_fingerprint if self._binary_parser is not None else None)
        )
        if parser is None:
            return None
        return combined_parser_fingerprint(
            parser,
            None if self._page_index_builder is None else self._page_index_builder.fingerprint,
        )

    def _source_path(self, source: SourceRecord) -> Path:
        portable = PurePosixPath(source.relative_path)
        lexical = self._vault.joinpath(*portable.parts)
        try:
            resolved = lexical.resolve(strict=True)
        except OSError as error:
            raise KnowledgePreparationError("knowledge source is no longer readable") from error
        if resolved != lexical or not resolved.is_file() or resolved.is_symlink():
            raise KnowledgePreparationError("knowledge source crossed the workspace boundary")
        return resolved

    def _parse_markdown(self, source: SourceRecord, absolute: Path) -> KnowledgeParseResult:
        content = absolute.read_bytes()
        if len(content) != source.byte_size or len(content) > self._maximum_markdown_bytes:
            raise KnowledgePreparationError("Markdown source size changed or exceeds its limit")
        if _sha256(content) != source.content_hash:
            raise KnowledgePreparationError("Markdown source changed after discovery")
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise KnowledgePreparationError("Markdown source must be strict UTF-8") from error
        if not text.strip():
            raise KnowledgePreparationError("Markdown source contains no evidence text")
        page = PageEvidence(1, text, _sha256(content))
        return KnowledgeParseResult(source.source_id, source.content_hash, (page,), _MARKDOWN_PARSER_FINGERPRINT)


def _ingestion_id(
    revision: int,
    candidates: tuple[str, ...],
    removals: tuple[str, ...],
    prepared: list[PreparedKnowledgeSource] | tuple[PreparedKnowledgeSource, ...],
    target: tuple[SourceRecord, ...],
) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "baseRevision": revision,
                "candidateIds": list(candidates),
                "parsers": [item.parser_fingerprint for item in prepared],
                "removedSourceIds": list(removals),
                "targetSources": [_source_json(item) for item in target],
            }
        )
    ).hexdigest()
    return f"ing-{digest[:32]}"


def _preparation_json(value: KnowledgePreparation) -> dict[str, object]:
    return {
        "baseRevision": value.base_revision,
        "candidateIds": list(value.candidate_ids),
        "ingestionId": value.ingestion_id,
        "preparedSources": [
            {
                "parserFingerprint": item.parser_fingerprint,
                "sourceHash": item.source.content_hash,
                "sourceId": item.source.source_id,
            }
            for item in value.prepared_sources
        ],
        "removedSourceIds": list(value.removed_source_ids),
        "schemaVersion": _PREPARATION_SCHEMA_VERSION,
        "targetSources": [_source_json(item) for item in value.target_sources],
    }


def _source_json(source: SourceRecord) -> dict[str, object]:
    return {
        "byteSize": source.byte_size,
        "contentHash": source.content_hash,
        "mediaType": source.media_type,
        "path": source.relative_path,
        "sourceId": source.source_id,
    }


def _source_from_json(value: object) -> SourceRecord:
    item = _object(value, {"byteSize", "contentHash", "mediaType", "path", "sourceId"})
    return SourceRecord(
        _string(item["sourceId"]),
        _string(item["path"]),
        _string(item["contentHash"]),
        _string(item["mediaType"]),
        _integer(item["byteSize"], minimum=0),
    )


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys or any(not isinstance(key, str) for key in value):
        raise ValueError("knowledge preparation projection shape is invalid")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("knowledge preparation projection list is invalid")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("knowledge preparation projection string is invalid")
    return value


def _integer(value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("knowledge preparation projection integer is invalid")
    return value


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _source_matches(path: Path, source: SourceRecord) -> bool:
    if path.stat().st_size != source.byte_size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}" == source.content_hash


__all__ = [
    "KnowledgeBinaryParser",
    "KnowledgeCancellation",
    "KnowledgeParseResult",
    "KnowledgePreparation",
    "KnowledgePreparationError",
    "KnowledgePreparationService",
    "KnowledgePreparationStore",
]
