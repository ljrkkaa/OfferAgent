from __future__ import annotations

import hashlib

import pytest

from offeragent_harness.knowledge import (
    CatalogSnapshot,
    PageEvidence,
    PageIndexSummaryPatch,
    PageIndexTree,
    SourceRecord,
    StructuralPageIndexBuilder,
    WikiCitation,
    WikiPagePatch,
    WikiPageType,
    WikiPatchPlan,
    WikiPlanValidationError,
    WikiPlanValidator,
)


def _fixture() -> tuple[CatalogSnapshot, PageIndexTree]:
    source = SourceRecord("src-1234567890abcdef", "raw/random.md", "sha256:" + "a" * 64, "text/markdown", 10)
    text = "# Unseen structure\nA source fact."
    page = PageEvidence(1, text, "sha256:" + hashlib.sha256(text.encode()).hexdigest())
    tree = StructuralPageIndexBuilder().build(
        source_id=source.source_id, source_hash=source.content_hash, title="Random", pages=(page,)
    )
    return CatalogSnapshot(3, (source,)), tree


def _plan(*, page_end: int = 1, related: tuple[str, ...] = (), include_summaries: bool = True) -> WikiPatchPlan:
    summaries = (
        (
            PageIndexSummaryPatch("src-1234567890abcdef", "sha256:" + "a" * 64, "node-root", "Document overview"),
            PageIndexSummaryPatch("src-1234567890abcdef", "sha256:" + "a" * 64, "node-0001", "Section summary"),
        )
        if include_summaries
        else ()
    )
    return WikiPatchPlan(
        "ingestion-123",
        3,
        summaries,
        (
            WikiPagePatch(
                "concept-random",
                WikiPageType.CONCEPT,
                "concepts/random.md",
                "Random",
                ("arbitrary alias",),
                "A grounded synthesis.",
                (WikiCitation("src-1234567890abcdef", "sha256:" + "a" * 64, "node-0001", 1, page_end),),
                related,
            ),
        ),
    )


def test_wiki_validator_renders_only_grounded_structured_pages() -> None:
    catalog, tree = _fixture()
    validated = WikiPlanValidator().validate(plan=_plan(), catalog=catalog, page_indexes=(tree,))
    path, payload = validated.rendered_pages[0]
    assert path == "concepts/random.md"
    assert b"knowledge_revision: 4" in payload
    assert b"#node:node-0001#pages:1-1" in payload


def test_wiki_validator_rejects_page_ranges_and_unresolved_links() -> None:
    catalog, tree = _fixture()
    with pytest.raises(WikiPlanValidationError, match="outside"):
        WikiPlanValidator().validate(plan=_plan(page_end=2), catalog=catalog, page_indexes=(tree,))
    with pytest.raises(WikiPlanValidationError, match="unresolved"):
        WikiPlanValidator().validate(
            plan=_plan(related=("concept-does-not-exist",)), catalog=catalog, page_indexes=(tree,)
        )
    with pytest.raises(WikiPlanValidationError, match="summarize every"):
        WikiPlanValidator().validate(plan=_plan(include_summaries=False), catalog=catalog, page_indexes=(tree,))


def test_entity_pages_use_the_canonical_entities_projection() -> None:
    citation = WikiCitation("src-1234567890abcdef", "sha256:" + "a" * 64, "node-0001", 1, 1)
    page = WikiPagePatch(
        "entity-random",
        WikiPageType.ENTITY,
        "entities/random.md",
        "Random",
        (),
        "Grounded.",
        (citation,),
        (),
    )

    assert page.path == "entities/random.md"
    with pytest.raises(ValueError, match="generated page type root"):
        WikiPagePatch(
            "entity-random",
            WikiPageType.ENTITY,
            "entitys/random.md",
            "Random",
            (),
            "Grounded.",
            (citation,),
            (),
        )
