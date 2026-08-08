from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest

from offeragent_harness.foundation.canonical import canonical_json_bytes
from offeragent_harness.knowledge import (
    CatalogSnapshot,
    KnowledgeCatalogStore,
    KnowledgeObjectStore,
    KnowledgePublisher,
    PageEvidence,
    PageIndexSummaryPatch,
    PreparedKnowledgeSource,
    PublicationStage,
    SourceRecord,
    StructuralPageIndexBuilder,
    WikiCitation,
    WikiPagePatch,
    WikiPageType,
    WikiPatchPlan,
    WikiPlanValidator,
)
from offeragent_harness.knowledge.publisher import KnowledgePublicationError
from offeragent_harness.knowledge.wiki import ValidatedWikiPlan


def _publication(
    *,
    revision: int,
    marker: str,
    body: str,
    objects: KnowledgeObjectStore,
    wiki_id: str = "concept-general",
    path: str = "concepts/general.md",
    deleted_wiki_ids: tuple[str, ...] = (),
    existing_wiki_ids: frozenset[str] = frozenset(),
) -> tuple[SourceRecord, ValidatedWikiPlan]:
    text = f"# Document {marker}\n{body}"
    source = SourceRecord(
        "src-1234567890abcdef",
        "raw/document.md",
        "sha256:" + marker * 64,
        "text/markdown",
        len(text.encode()),
    )
    page = PageEvidence(1, text, "sha256:" + hashlib.sha256(text.encode()).hexdigest())
    tree = StructuralPageIndexBuilder().build(
        source_id=source.source_id, source_hash=source.content_hash, title="Document", pages=(page,)
    )
    summaries = tuple(
        PageIndexSummaryPatch(source.source_id, source.content_hash, node.node_id, f"Summary for {node.title}")
        for node in tree.nodes
    )
    plan = WikiPatchPlan(
        "ingestion-" + marker * 8,
        revision,
        summaries,
        (
            WikiPagePatch(
                wiki_id,
                WikiPageType.CONCEPT,
                path,
                "General",
                (),
                body,
                (WikiCitation(source.source_id, source.content_hash, "node-0001", 1, 1),),
                (),
            ),
        ),
        deleted_wiki_ids,
    )
    validated = WikiPlanValidator().validate(
        plan=plan,
        catalog=CatalogSnapshot(revision, (source,)),
        page_indexes=(tree,),
        existing_wiki_ids=existing_wiki_ids,
    )
    objects.put(PreparedKnowledgeSource(source, (page,), validated.enriched_page_indexes[0], "sha256:" + "f" * 64))
    return source, validated


def test_publisher_commits_a_complete_revision_and_reuses_existing_pages(tmp_path: Path) -> None:
    state = tmp_path / ".offeragent" / "knowledge"
    catalog = KnowledgeCatalogStore(state)
    objects = KnowledgeObjectStore(state)
    source, validated = _publication(revision=0, marker="a", body="First grounded body.", objects=objects)
    publisher = KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects)
    committed = publisher.publish(validated=validated, target_sources=(source,))
    assert committed.revision == 1
    assert (tmp_path / "knowledge" / "concepts" / "general.md").read_text(encoding="utf-8").find(
        "First grounded body."
    ) > 0
    assert (tmp_path / "knowledge" / "sources" / f"{source.source_id}.md").exists()
    source_page = (tmp_path / "knowledge" / "sources" / f"{source.source_id}.md").read_text(encoding="utf-8")
    assert "- Citation source: `raw/document.md`" in source_page
    assert (tmp_path / "knowledge" / "pageindexes" / f"{source.source_id}.json").exists()
    assert publisher.existing_wiki_ids() == frozenset({"concept-general"})
    assert not (state / "active-publication.json").exists()


@pytest.mark.parametrize(
    ("failure_stage", "expected_revision", "expected_body"),
    [
        (PublicationStage.PREPARED, 1, "First grounded body."),
        (PublicationStage.OLD_MOVED, 1, "First grounded body."),
        (PublicationStage.NEW_MOVED, 1, "First grounded body."),
        (PublicationStage.COMMITTED, 2, "Second grounded body."),
    ],
)
def test_publisher_recovers_to_a_whole_old_or_new_revision(
    tmp_path: Path, failure_stage: PublicationStage, expected_revision: int, expected_body: str
) -> None:
    state = tmp_path / ".offeragent" / "knowledge"
    catalog = KnowledgeCatalogStore(state)
    objects = KnowledgeObjectStore(state)
    first_source, first = _publication(revision=0, marker="a", body="First grounded body.", objects=objects)
    KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects).publish(
        validated=first, target_sources=(first_source,)
    )
    second_source, second = _publication(revision=1, marker="b", body="Second grounded body.", objects=objects)

    def fail_at(stage: PublicationStage) -> None:
        if stage is failure_stage:
            raise RuntimeError(f"injected failure at {stage.value}")

    publisher = KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects, fault_injector=fail_at)
    with pytest.raises(RuntimeError, match="injected failure"):
        publisher.publish(validated=second, target_sources=(second_source,))
    assert catalog.load().revision == expected_revision
    wiki = (tmp_path / "knowledge" / "concepts" / "general.md").read_text(encoding="utf-8")
    assert expected_body in wiki
    assert not (state / "active-publication.json").exists()


def test_publisher_rejects_untracked_files_in_the_current_snapshot(tmp_path: Path) -> None:
    state = tmp_path / ".offeragent" / "knowledge"
    catalog = KnowledgeCatalogStore(state)
    objects = KnowledgeObjectStore(state)
    first_source, first = _publication(revision=0, marker="a", body="First grounded body.", objects=objects)
    publisher = KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects)
    publisher.publish(validated=first, target_sources=(first_source,))
    (tmp_path / "knowledge" / "untracked.md").write_text("not in manifest", encoding="utf-8")
    second_source, second = _publication(revision=1, marker="b", body="Second grounded body.", objects=objects)

    with pytest.raises(KnowledgePublicationError, match="does not match snapshot"):
        publisher.publish(validated=second, target_sources=(second_source,))

    assert catalog.load().revision == 1


def test_recovery_rejects_a_noncanonical_transaction_identifier(tmp_path: Path) -> None:
    state = tmp_path / ".offeragent" / "knowledge"
    state.mkdir(parents=True)
    journal = {
        "baseRevision": 0,
        "hadCurrent": False,
        "ingestionId": "../outside",
        "schemaVersion": 1,
        "stage": PublicationStage.PREPARED.value,
        "targetRevision": 1,
    }
    (state / "active-publication.json").write_bytes(canonical_json_bytes(journal))
    publisher = KnowledgePublisher(
        vault_root=tmp_path,
        catalog=KnowledgeCatalogStore(state),
        objects=KnowledgeObjectStore(state),
    )

    with pytest.raises(KnowledgePublicationError, match="ingestionId is invalid"):
        publisher.recover()


@pytest.mark.parametrize(
    "failure_stage",
    [
        PublicationStage.PREPARED,
        PublicationStage.OLD_MOVED,
        PublicationStage.NEW_MOVED,
        PublicationStage.COMMITTED,
    ],
)
def test_recovery_is_idempotent_when_cleanup_itself_is_interrupted(
    tmp_path: Path, failure_stage: PublicationStage, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".offeragent" / "knowledge"
    catalog = KnowledgeCatalogStore(state)
    objects = KnowledgeObjectStore(state)
    first_source, first = _publication(revision=0, marker="a", body="First grounded body.", objects=objects)
    KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects).publish(
        validated=first, target_sources=(first_source,)
    )
    second_source, second = _publication(revision=1, marker="b", body="Second grounded body.", objects=objects)

    def fail_at(stage: PublicationStage) -> None:
        if stage is failure_stage:
            raise RuntimeError("first interruption")

    def interrupt_cleanup(transaction_root: Path) -> None:
        if transaction_root.exists():
            shutil.rmtree(transaction_root)
        raise RuntimeError("cleanup interruption")

    publisher = KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects, fault_injector=fail_at)
    monkeypatch.setattr(publisher, "_finish_transaction", interrupt_cleanup)
    with pytest.raises(RuntimeError, match="cleanup interruption"):
        publisher.publish(validated=second, target_sources=(second_source,))
    assert (state / "active-publication.json").exists()

    KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects).recover()

    revision = catalog.load().revision
    assert revision == (2 if failure_stage is PublicationStage.COMMITTED else 1)
    expected_body = "Second grounded body." if revision == 2 else "First grounded body."
    assert expected_body in (tmp_path / "knowledge" / "concepts" / "general.md").read_text(encoding="utf-8")
    assert not (state / "active-publication.json").exists()


@pytest.mark.parametrize("transition", ["old_to_backup", "staged_to_current"])
def test_publisher_recovers_when_a_move_completes_before_its_stage_is_journaled(
    tmp_path: Path, transition: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".offeragent" / "knowledge"
    catalog = KnowledgeCatalogStore(state)
    objects = KnowledgeObjectStore(state)
    first_source, first = _publication(revision=0, marker="a", body="First grounded body.", objects=objects)
    KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects).publish(
        validated=first, target_sources=(first_source,)
    )
    second_source, second = _publication(revision=1, marker="b", body="Second grounded body.", objects=objects)
    original_replace = os.replace
    interrupted = False

    def replace_then_interrupt(source: str | Path, target: str | Path) -> None:
        nonlocal interrupted
        source_path = Path(source)
        target_path = Path(target)
        old_move = source_path == tmp_path / "knowledge" and target_path.name == "backup"
        new_move = source_path.name == "wiki" and target_path == tmp_path / "knowledge"
        should_interrupt = (transition == "old_to_backup" and old_move) or (
            transition == "staged_to_current" and new_move
        )
        original_replace(source, target)
        if should_interrupt and not interrupted:
            interrupted = True
            raise RuntimeError("move completed before journal update")

    monkeypatch.setattr(os, "replace", replace_then_interrupt)
    with pytest.raises(RuntimeError, match="move completed"):
        KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects).publish(
            validated=second, target_sources=(second_source,)
        )

    assert interrupted
    assert catalog.load().revision == 1
    assert "First grounded body." in (tmp_path / "knowledge" / "concepts" / "general.md").read_text(encoding="utf-8")
    assert not (state / "active-publication.json").exists()


def test_incremental_publish_rejects_stale_citations_until_the_page_is_replaced_or_deleted(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".offeragent" / "knowledge"
    catalog = KnowledgeCatalogStore(state)
    objects = KnowledgeObjectStore(state)
    publisher = KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects)
    first_source, first = _publication(revision=0, marker="a", body="First grounded body.", objects=objects)
    publisher.publish(validated=first, target_sources=(first_source,))
    second_source, leaves_stale = _publication(
        revision=1,
        marker="b",
        body="Second grounded body.",
        objects=objects,
        wiki_id="concept-second",
        path="concepts/second.md",
    )

    with pytest.raises(KnowledgePublicationError, match="stale source citation"):
        publisher.publish(validated=leaves_stale, target_sources=(second_source,))

    second_source, deletes_stale = _publication(
        revision=1,
        marker="b",
        body="Second grounded body.",
        objects=objects,
        wiki_id="concept-second",
        path="concepts/second.md",
        deleted_wiki_ids=("concept-general",),
        existing_wiki_ids=frozenset({"concept-general"}),
    )
    publisher.publish(validated=deletes_stale, target_sources=(second_source,))
    assert not (tmp_path / "knowledge" / "concepts" / "general.md").exists()
    assert publisher.existing_wiki_ids() == frozenset({"concept-second"})
