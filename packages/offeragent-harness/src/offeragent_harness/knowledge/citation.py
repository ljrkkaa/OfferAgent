"""Vocabulary-independent scoring for page-level citation evidence."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

_QUOTED_SPAN = re.compile(r'"([^"]{2,})"')
_PLACEHOLDER = re.compile(r"(?:_{2,}|\.{3,}|\[?blank\]?)", re.IGNORECASE)
_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CitationEvidence:
    """A retrieved evidence unit that may be selected as a citation."""

    evidence_id: str
    source_id: str
    text: str
    retrieval_score: float

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.source_id:
            raise ValueError("citation evidence identity must be non-empty")
        if not self.text.strip():
            raise ValueError("citation evidence text must be non-empty")


@dataclass(frozen=True, slots=True)
class EvidenceSupport:
    """Observable evidence-alignment features used by the citation ranker."""

    claim_direct_span_tokens: int
    claim_contiguous_span_tokens: int
    claim_token_coverage: float
    direct_span_tokens: int
    contiguous_span_tokens: int
    token_coverage: float
    anchor_direct_span_tokens: int
    anchor_contiguous_span_tokens: int
    anchor_token_coverage: float

    @property
    def has_direct_support(self) -> bool:
        """Return whether the page contains an intact assertion or source anchor."""

        return self.claim_direct_span_tokens > 0 or self.direct_span_tokens > 0 or self.anchor_direct_span_tokens > 0

    def ranking_key(self, *, has_claim_spans: bool, has_assertion_spans: bool) -> tuple[float, ...]:
        """Return a deterministic key that prioritizes direct assertion evidence."""

        claim = (
            float(self.claim_direct_span_tokens),
            float(self.claim_contiguous_span_tokens),
            self.claim_token_coverage,
        )
        assertion = (
            float(self.direct_span_tokens),
            float(self.contiguous_span_tokens),
            self.token_coverage,
        )
        anchor = (
            float(self.anchor_direct_span_tokens),
            float(self.anchor_contiguous_span_tokens),
            self.anchor_token_coverage,
        )
        if has_claim_spans:
            return (*claim, *assertion, *anchor)
        if has_assertion_spans:
            return (*assertion, *anchor, *claim)
        return (*anchor, *assertion, *claim)


@dataclass(frozen=True, slots=True)
class RankedCitationEvidence:
    """Evidence together with its independently inspectable support features."""

    evidence: CitationEvidence
    support: EvidenceSupport


class CitationEvidenceReranker:
    """Rerank evidence by literal claim support, without domain-specific vocabulary."""

    def rank(
        self,
        question: str,
        candidates: Sequence[CitationEvidence],
        *,
        source_anchors: Mapping[str, Iterable[str]] | None = None,
        navigation_spans: Iterable[str] = (),
        verified_claims: Iterable[str] = (),
    ) -> tuple[RankedCitationEvidence, ...]:
        """Rank candidates while separating navigation terms from answer assertions.

        ``navigation_spans`` are query fragments resolved through Wiki identity
        metadata, such as an alias or a document title. They locate a source but
        should not outweigh a longer quoted assertion that only one page supports.
        ``source_anchors`` provide canonical, source-specific text for queries that
        contain only navigation spans. ``verified_claims`` are assertions already
        produced by the answer stage; direct support for those claims takes
        precedence without changing upstream retrieval scores.
        """

        if not question.strip():
            raise ValueError("citation question must be non-empty")
        anchors = source_anchors or {}
        navigation = {_tokens(value) for value in navigation_spans if _tokens(value)}
        query_spans = _query_spans(question)
        assertion_spans = tuple(span for span in query_spans if span not in navigation)
        claim_spans = _deduplicated_token_spans(verified_claims)
        has_claim_spans = bool(claim_spans)
        has_assertion_spans = bool(assertion_spans)
        ranked = tuple(
            RankedCitationEvidence(
                evidence=candidate,
                support=_evidence_support(
                    claim_spans,
                    assertion_spans,
                    _deduplicated_token_spans(anchors.get(candidate.source_id, ())),
                    _tokens(candidate.text),
                ),
            )
            for candidate in candidates
        )
        return tuple(
            sorted(
                ranked,
                key=lambda item: (
                    *(
                        -value
                        for value in item.support.ranking_key(
                            has_claim_spans=has_claim_spans,
                            has_assertion_spans=has_assertion_spans,
                        )
                    ),
                    -item.evidence.retrieval_score,
                    item.evidence.evidence_id,
                ),
            )
        )


def _query_spans(question: str) -> tuple[tuple[str, ...], ...]:
    spans = []
    for value in _QUOTED_SPAN.findall(question):
        cleaned = _PLACEHOLDER.sub(" ", value).strip(" \t\r\n:;,.?!-")
        tokens = _tokens(cleaned)
        if tokens:
            spans.append(tokens)
    return _deduplicated_token_spans(spans)


def _deduplicated_token_spans(
    values: Iterable[str | tuple[str, ...]],
) -> tuple[tuple[str, ...], ...]:
    result: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for value in values:
        tokens = value if isinstance(value, tuple) else _tokens(value)
        if not tokens or tokens in seen:
            continue
        seen.add(tokens)
        result.append(tokens)
    return tuple(result)


def _tokens(text: str) -> tuple[str, ...]:
    compatible = unicodedata.normalize("NFKC", text).replace("\u00ad", "")
    dehyphenated = re.sub(r"(?<=\w)-\s*[\r\n]+\s*(?=\w)", "", compatible)
    return tuple(match.group(0).casefold() for match in _TOKEN.finditer(dehyphenated))


def _evidence_support(
    claim_spans: tuple[tuple[str, ...], ...],
    assertion_spans: tuple[tuple[str, ...], ...],
    anchor_spans: tuple[tuple[str, ...], ...],
    document: tuple[str, ...],
) -> EvidenceSupport:
    claim_direct, claim_contiguous, claim_coverage = _span_support(claim_spans, document)
    direct, contiguous, coverage = _span_support(assertion_spans, document)
    anchor_direct, anchor_contiguous, anchor_coverage = _span_support(anchor_spans, document)
    return EvidenceSupport(
        claim_direct,
        claim_contiguous,
        claim_coverage,
        direct,
        contiguous,
        coverage,
        anchor_direct,
        anchor_contiguous,
        anchor_coverage,
    )


def _span_support(spans: tuple[tuple[str, ...], ...], document: tuple[str, ...]) -> tuple[int, int, float]:
    direct = 0
    contiguous = 0
    coverage = 0.0
    available = set(document)
    for span in spans:
        longest = _longest_contiguous_match(span, document)
        contiguous = max(contiguous, longest)
        if longest == len(span):
            direct = max(direct, len(span))
        coverage = max(coverage, sum(token in available for token in span) / len(span))
    return direct, contiguous, coverage


def _longest_contiguous_match(span: tuple[str, ...], document: tuple[str, ...]) -> int:
    if not span or not document:
        return 0
    previous = [0] * (len(document) + 1)
    longest = 0
    for span_token in span:
        current = [0] * (len(document) + 1)
        for index, document_token in enumerate(document, start=1):
            if span_token != document_token:
                continue
            current[index] = previous[index - 1] + 1
            longest = max(longest, current[index])
        previous = current
    return longest
