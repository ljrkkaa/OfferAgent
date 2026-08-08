from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from offeragent_harness.foundation.canonical import canonical_json_bytes

from .models import CatalogSnapshot, SourceRecord

_SCHEMA_VERSION = 1


class KnowledgeCatalogError(RuntimeError):
    pass


class KnowledgeCatalogStore:
    """Strict, canonical catalog persistence with compare-and-swap revisions."""

    def __init__(self, state_root: Path) -> None:
        if not state_root.is_absolute():
            raise ValueError("knowledge state root must be absolute")
        self._root = state_root
        self._path = state_root / "catalog.json"

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> CatalogSnapshot:
        if not self._path.exists():
            return CatalogSnapshot(0, ())
        try:
            raw = self._path.read_bytes()
            decoded: Any = json.loads(raw.decode("utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise KnowledgeCatalogError("knowledge catalog is unreadable") from error
        if canonical_json_bytes(decoded) != raw:
            raise KnowledgeCatalogError("knowledge catalog is not canonical JSON")
        value = _exact_mapping(decoded, {"schemaVersion", "revision", "sources"})
        if value["schemaVersion"] != _SCHEMA_VERSION:
            raise KnowledgeCatalogError("knowledge catalog schema version is unsupported")
        revision = _integer(value["revision"], "revision")
        sources_raw = value["sources"]
        if not isinstance(sources_raw, list):
            raise KnowledgeCatalogError("knowledge catalog sources must be a list")
        sources = tuple(_decode_source(item) for item in sources_raw)
        try:
            return CatalogSnapshot(revision, sources)
        except ValueError as error:
            raise KnowledgeCatalogError("knowledge catalog invariants are invalid") from error

    def commit(self, *, expected_revision: int, sources: tuple[SourceRecord, ...]) -> CatalogSnapshot:
        current = self.load()
        if current.revision != expected_revision:
            raise KnowledgeCatalogError("knowledge catalog revision changed")
        ordered = tuple(sorted(sources, key=lambda item: item.relative_path.casefold()))
        target = CatalogSnapshot(current.revision + 1, ordered)
        payload = canonical_json_bytes(
            {
                "revision": target.revision,
                "schemaVersion": _SCHEMA_VERSION,
                "sources": [_encode_source(source) for source in target.sources],
            }
        )
        self._root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix="catalog-", suffix=".tmp", dir=self._root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            _fsync_directory(self._root)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return target


def _encode_source(source: SourceRecord) -> dict[str, object]:
    return {
        "byteSize": source.byte_size,
        "contentHash": source.content_hash,
        "mediaType": source.media_type,
        "path": source.relative_path,
        "sourceId": source.source_id,
    }


def _decode_source(raw: Any) -> SourceRecord:
    value = _exact_mapping(raw, {"sourceId", "path", "contentHash", "mediaType", "byteSize"})
    try:
        return SourceRecord(
            source_id=_string(value["sourceId"], "sourceId"),
            relative_path=_string(value["path"], "path"),
            content_hash=_string(value["contentHash"], "contentHash"),
            media_type=_string(value["mediaType"], "mediaType"),
            byte_size=_integer(value["byteSize"], "byteSize"),
        )
    except ValueError as error:
        raise KnowledgeCatalogError("knowledge catalog source is invalid") from error


def _exact_mapping(raw: Any, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != keys or any(not isinstance(key, str) for key in raw):
        raise KnowledgeCatalogError("knowledge catalog object shape is invalid")
    return raw


def _string(raw: Any, field: str) -> str:
    if not isinstance(raw, str):
        raise KnowledgeCatalogError(f"knowledge catalog {field} must be a string")
    return raw


def _integer(raw: object, field: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise KnowledgeCatalogError(f"knowledge catalog {field} must be a non-negative integer")
    return raw


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
