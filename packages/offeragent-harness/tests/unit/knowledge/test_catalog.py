from __future__ import annotations

import json
from pathlib import Path

import pytest

from offeragent_harness.foundation.canonical import canonical_json_bytes
from offeragent_harness.knowledge import KnowledgeCatalogError, KnowledgeCatalogStore, SourceRecord


def _source(path: str, marker: str) -> SourceRecord:
    return SourceRecord(
        source_id=f"src-{marker * 32}",
        relative_path=path,
        content_hash="sha256:" + marker * 64,
        media_type="text/markdown",
        byte_size=17,
    )


def test_catalog_commit_is_canonical_sorted_and_compare_and_swap(tmp_path: Path) -> None:
    store = KnowledgeCatalogStore(tmp_path / ".offeragent" / "knowledge")
    assert store.load().revision == 0
    committed = store.commit(expected_revision=0, sources=(_source("raw/z.md", "b"), _source("raw/a.md", "a")))
    assert committed.revision == 1
    assert [item.relative_path for item in committed.sources] == ["raw/a.md", "raw/z.md"]
    raw = store.path.read_bytes()
    assert raw == canonical_json_bytes(json.loads(raw))
    assert store.load() == committed
    with pytest.raises(KnowledgeCatalogError, match="revision changed"):
        store.commit(expected_revision=0, sources=committed.sources)


def test_catalog_rejects_noncanonical_or_unknown_schema(tmp_path: Path) -> None:
    store = KnowledgeCatalogStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    store.path.write_text('{"schemaVersion": 1, "revision": 0, "sources": []}', encoding="utf-8")
    with pytest.raises(KnowledgeCatalogError, match="not canonical"):
        store.load()
    store.path.write_bytes(canonical_json_bytes({"schemaVersion": 2, "revision": 0, "sources": []}))
    with pytest.raises(KnowledgeCatalogError, match="unsupported"):
        store.load()
