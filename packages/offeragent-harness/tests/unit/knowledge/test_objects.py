from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from offeragent_harness.knowledge import (
    KnowledgeObjectError,
    KnowledgeObjectStore,
    PageEvidence,
    PreparedKnowledgeSource,
    SourceRecord,
    StructuralPageIndexBuilder,
)


def _prepared() -> PreparedKnowledgeSource:
    source = SourceRecord("src-1234567890abcdef", "raw/input.md", "sha256:" + "a" * 64, "text/markdown", 20)
    texts = ("# First\nGrounded fact.", "# Second\nAnother fact.")
    pages = tuple(
        PageEvidence(index, text, "sha256:" + hashlib.sha256(text.encode()).hexdigest())
        for index, text in enumerate(texts, start=1)
    )
    tree = StructuralPageIndexBuilder().build(
        source_id=source.source_id, source_hash=source.content_hash, title="Input", pages=pages
    )
    return PreparedKnowledgeSource(source, pages, tree, "sha256:" + "f" * 64)


def test_object_store_writes_reusable_content_addressed_projections(tmp_path: Path) -> None:
    store = KnowledgeObjectStore(tmp_path)
    prepared = _prepared()
    first = store.put(prepared)
    second = store.put(prepared)
    assert first == second
    assert first.parent.name == prepared.source.source_id
    assert first.name == "rev-" + "a" * 20 + "-idx-" + "f" * 12
    assert (first / "evidence" / "pages" / "0001.md").read_text(encoding="utf-8").endswith(prepared.pages[0].text)
    assert (first / "pageindex" / "nodes.jsonl").read_bytes().endswith(b"\n")
    assert store.load(prepared.source.source_id, prepared.source.content_hash) == prepared


def test_object_store_reads_exact_legacy_revision_directory(tmp_path: Path) -> None:
    store = KnowledgeObjectStore(tmp_path)
    prepared = _prepared()
    current = store.put(prepared)
    legacy = current.parent / ("rev-" + prepared.source.content_hash.removeprefix("sha256:")[:20])
    current.rename(legacy)
    next(current.parent.glob("active-*.json")).unlink()

    assert store.load(prepared.source.source_id, prepared.source.content_hash) == prepared


def test_object_store_rejects_tampered_immutable_content(tmp_path: Path) -> None:
    store = KnowledgeObjectStore(tmp_path)
    root = store.put(_prepared())
    page = root / "evidence" / "pages" / "0001.md"
    page.write_text("tampered", encoding="utf-8")
    with pytest.raises(KnowledgeObjectError, match="integrity mismatch"):
        store.verify(root)


def test_page_evidence_rejects_a_forged_hash() -> None:
    with pytest.raises(ValueError, match="does not match"):
        PageEvidence(1, "actual", "sha256:" + "0" * 64)


def test_object_path_rejects_noncanonical_identity(tmp_path: Path) -> None:
    store = KnowledgeObjectStore(tmp_path)
    with pytest.raises(ValueError, match="identity"):
        store.object_path("src-../../escape", "sha256:" + "a" * 64)


def test_object_store_rejects_untracked_files_and_versions_pageindex_builds(tmp_path: Path) -> None:
    store = KnowledgeObjectStore(tmp_path)
    prepared = _prepared()
    root = store.put(prepared)
    (root / "extra.txt").write_text("not manifested", encoding="utf-8")
    with pytest.raises(KnowledgeObjectError, match="does not match"):
        store.verify(root)
    (root / "extra.txt").unlink()
    changed = PreparedKnowledgeSource(
        prepared.source,
        prepared.pages,
        prepared.page_index,
        "sha256:" + "e" * 64,
    )
    changed_root = store.put(changed)
    assert changed_root != root
    assert store.load(prepared.source.source_id, prepared.source.content_hash) == changed
    assert (
        store.load(
            prepared.source.source_id,
            prepared.source.content_hash,
            prepared.parser_fingerprint,
        )
        == prepared
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path regression")
def test_object_store_reads_projections_beyond_legacy_windows_max_path(
    tmp_path: Path,
) -> None:
    padding = "x" * max(1, 190 - len(str(tmp_path)))
    state = tmp_path / padding
    state.mkdir()
    store = KnowledgeObjectStore(state.resolve())
    prepared = _prepared()

    root = store.put(prepared)

    document_path = root / "evidence" / "document.json"
    assert len(str(document_path)) > 260
    assert store.load(prepared.source.source_id, prepared.source.content_hash) == prepared
