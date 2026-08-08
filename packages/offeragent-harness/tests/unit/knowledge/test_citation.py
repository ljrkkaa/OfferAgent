from __future__ import annotations

from offeragent_harness.knowledge import CitationEvidence, CitationEvidenceReranker


def _candidate(
    evidence_id: str,
    text: str,
    *,
    source_id: str = "source-a",
    retrieval_score: float = 1.0,
) -> CitationEvidence:
    return CitationEvidence(evidence_id, source_id, text, retrieval_score)


def test_longer_literal_assertion_outranks_navigation_title() -> None:
    question = (
        'In "A Rare and Very Long Canonical Document Title", complete this passage: '
        '"We introduce a general method for checking evidence ____"'
    )
    title_page = _candidate(
        "page-1",
        "A Rare and Very Long Canonical Document Title. Abstract follows.",
        retrieval_score=100.0,
    )
    assertion_page = _candidate(
        "page-3",
        "We introduce a general method for checking evidence against source pages.",
    )

    ranked = CitationEvidenceReranker().rank(
        question,
        (title_page, assertion_page),
        source_anchors={"source-a": ("A Rare and Very Long Canonical Document Title",)},
        navigation_spans=("A Rare and Very Long Canonical Document Title",),
    )

    assert ranked[0].evidence.evidence_id == "page-3"
    assert ranked[0].support.direct_span_tokens == 8
    assert ranked[0].support.has_direct_support


def test_canonical_source_anchor_selects_supporting_page_for_alias_lookup() -> None:
    question = 'What is the canonical name of "short alias"?'
    body_page = _candidate(
        "page-2",
        "The short alias is discussed throughout this section.",
        retrieval_score=20.0,
    )
    title_page = _candidate(
        "page-1",
        "A Canonical Source Name",
        retrieval_score=1.0,
    )

    ranked = CitationEvidenceReranker().rank(
        question,
        (body_page, title_page),
        source_anchors={"source-a": ("A Canonical Source Name",)},
        navigation_spans=("short alias",),
    )

    assert ranked[0].evidence.evidence_id == "page-1"
    assert ranked[0].support.anchor_direct_span_tokens == 4


def test_source_anchors_are_isolated_for_multi_source_selection() -> None:
    candidates = (
        _candidate("a-body", "Alpha material", source_id="source-a", retrieval_score=9.0),
        _candidate("a-title", "Canonical Alpha", source_id="source-a"),
        _candidate("b-body", "Beta material", source_id="source-b", retrieval_score=8.0),
        _candidate("b-title", "Canonical Beta", source_id="source-b"),
    )

    ranked = CitationEvidenceReranker().rank(
        'Compare "alpha alias" and "beta alias".',
        candidates,
        source_anchors={
            "source-a": ("Canonical Alpha",),
            "source-b": ("Canonical Beta",),
        },
        navigation_spans=("alpha alias", "beta alias"),
    )
    best_by_source = {
        source_id: next(item.evidence.evidence_id for item in ranked if item.evidence.source_id == source_id)
        for source_id in ("source-a", "source-b")
    }

    assert best_by_source == {"source-a": "a-title", "source-b": "b-title"}


def test_compatibility_normalization_and_line_wraps_preserve_direct_support() -> None:
    ranked = CitationEvidenceReranker().rank(
        'Continue "task-speciﬁc evi-\n dence improves results ____".',
        (_candidate("page", "Task-specific evidence improves results in practice."),),
    )

    assert ranked[0].support.direct_span_tokens == 4


def test_verified_claim_resolves_duplicate_question_prefixes() -> None:
    candidates = (
        _candidate(
            "abstract",
            "We propose a reusable approach to leverage models as optimizers.",
            retrieval_score=20.0,
        ),
        _candidate(
            "body",
            "We propose a reusable approach to utilize models as optimizers.",
            retrieval_score=10.0,
        ),
    )

    question_only = CitationEvidenceReranker().rank('Complete "We propose a reusable approach to ____".', candidates)
    verified = CitationEvidenceReranker().rank(
        'Complete "We propose a reusable approach to ____".',
        candidates,
        verified_claims=("utilize models as optimizers",),
    )

    assert question_only[0].evidence.evidence_id == "abstract"
    assert verified[0].evidence.evidence_id == "body"
    assert verified[0].support.claim_direct_span_tokens == 4
