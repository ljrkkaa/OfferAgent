from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

from offeragent_harness.knowledge import (
    KnowledgeInferenceBudget,
    KnowledgeInferenceLimits,
    PageEvidence,
    SemanticKnowledgeError,
    SemanticPageIndexBuilder,
    StructuredInferenceCache,
)
from offeragent_harness.models import ModelEvent, ModelEventKind, ModelFinishReason, ModelRequest, ModelUsage
from offeragent_harness.testing import DeterministicIdGenerator, ManualCancellationToken


def _page(number: int, text: str) -> PageEvidence:
    return PageEvidence(number, text, f"sha256:{hashlib.sha256(text.encode()).hexdigest()}")


class _Gateway:
    def __init__(self, output: Mapping[str, Any], usage: ModelUsage | None = None) -> None:
        self.output = output
        self.usage = usage or ModelUsage(100, 30, 10, 0)
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest, cancellation: Any) -> AsyncIterator[ModelEvent]:
        cancellation.checkpoint()
        self.requests.append(request)
        yield ModelEvent(request.request_id, 1, ModelEventKind.STARTED)
        yield ModelEvent(request.request_id, 2, ModelEventKind.STRUCTURED_OUTPUT, data=self.output)
        yield ModelEvent(request.request_id, 3, ModelEventKind.USAGE, usage=self.usage)
        yield ModelEvent(request.request_id, 4, ModelEventKind.COMPLETED, finish_reason=ModelFinishReason.STOP)


@pytest.mark.asyncio
async def test_semantic_pageindex_requires_source_anchors_and_reuses_content_hash_cache(tmp_path: Any) -> None:
    pages = (
        _page(1, "The transformer uses multi-head self-attention in every encoder layer."),
        _page(2, "Scaled dot-product attention divides logits by the square root of key dimension."),
    )
    output = {
        "sections": [
            {
                "endPage": 2,
                "evidence": [
                    {"page": 1, "anchorId": "p0001-a001"},
                    {"page": 2, "anchorId": "p0002-a001"},
                ],
                "startPage": 1,
                "summary": "Introduces multi-head and scaled dot-product attention.",
                "title": "Attention mechanism",
            }
        ]
    }
    gateway = _Gateway(output)
    budget = KnowledgeInferenceBudget(KnowledgeInferenceLimits(10_000, 10_000))
    builder = SemanticPageIndexBuilder(
        gateway_factory=lambda _: gateway,
        workspace_id="ws-test",
        model="deepseek-v4-flash",
        cache=StructuredInferenceCache((tmp_path / "cache").resolve()),
        ids=DeterministicIdGenerator(),
        budget=budget,
    )
    first = await builder.build(
        source_id="src-transformer",
        source_hash="sha256:" + "a" * 64,
        title="Attention Is All You Need",
        pages=pages,
        run_id="run-test",
        cancellation=ManualCancellationToken(),
    )
    second = await builder.build(
        source_id="src-transformer",
        source_hash="sha256:" + "a" * 64,
        title="Attention Is All You Need",
        pages=pages,
        run_id="run-test",
        cancellation=ManualCancellationToken(),
    )

    assert first == second
    assert first.nodes[-1].title == "Attention mechanism"
    assert first.nodes[-1].summary.startswith("Introduces")
    assert len(gateway.requests) == 1
    assert budget.input_tokens == 100 and budget.cached_input_tokens == 10


@pytest.mark.asyncio
async def test_semantic_pageindex_rejects_fabricated_evidence_anchor(tmp_path: Any) -> None:
    pages = (_page(1, "Observed evidence is present on this page only."),)
    gateway = _Gateway(
        {
            "sections": [
                {
                    "endPage": 1,
                    "evidence": [{"page": 1, "anchorId": "p0001-a999"}],
                    "startPage": 1,
                    "summary": "Unsupported summary.",
                    "title": "Fabricated",
                }
            ]
        }
    )
    builder = SemanticPageIndexBuilder(
        gateway_factory=lambda _: gateway,
        workspace_id="ws-test",
        model="deepseek-v4-flash",
        cache=StructuredInferenceCache((tmp_path / "cache").resolve()),
        ids=DeterministicIdGenerator(),
        budget=KnowledgeInferenceBudget(),
    )

    with pytest.raises(SemanticKnowledgeError, match="not present"):
        await builder.build(
            source_id="src-evidence",
            source_hash="sha256:" + "b" * 64,
            title="Evidence",
            pages=pages,
            run_id="run-test",
            cancellation=ManualCancellationToken(),
        )


@pytest.mark.asyncio
async def test_semantic_pageindex_anchors_pdf_text_without_model_transcription(tmp_path: Any) -> None:
    pages = (_page(1, "The efﬁcient trans-\nformer uses attention."),)
    gateway = _Gateway(
        {
            "sections": [
                {
                    "endPage": 1,
                    "evidence": [{"page": 1, "anchorId": "p0001-a001"}],
                    "startPage": 1,
                    "summary": "Describes an efficient transformer.",
                    "title": "Architecture",
                }
            ]
        }
    )
    builder = SemanticPageIndexBuilder(
        gateway_factory=lambda _: gateway,
        workspace_id="ws-test",
        model="deepseek-v4-flash",
        cache=StructuredInferenceCache((tmp_path / "cache").resolve()),
        ids=DeterministicIdGenerator(),
        budget=KnowledgeInferenceBudget(),
    )

    tree = await builder.build(
        source_id="src-evidence",
        source_hash="sha256:" + "c" * 64,
        title="Evidence",
        pages=pages,
        run_id="run-test",
        cancellation=ManualCancellationToken(),
    )

    assert tree.nodes[-1].title == "Architecture"


@pytest.mark.asyncio
async def test_semantic_pageindex_allows_section_range_to_end_on_empty_pdf_page(tmp_path: Any) -> None:
    pages = (_page(1, "The supported section begins on this page."), _page(2, ""))
    gateway = _Gateway(
        {
            "sections": [
                {
                    "endPage": 2,
                    "evidence": [{"page": 1, "anchorId": "p0001-a001"}],
                    "startPage": 1,
                    "summary": "A section continues through an empty trailing page.",
                    "title": "Section with blank trailing page",
                }
            ]
        }
    )
    builder = SemanticPageIndexBuilder(
        gateway_factory=lambda _: gateway,
        workspace_id="ws-test",
        model="deepseek-v4-flash",
        cache=StructuredInferenceCache((tmp_path / "cache").resolve()),
        ids=DeterministicIdGenerator(),
        budget=KnowledgeInferenceBudget(),
    )

    tree = await builder.build(
        source_id="src-evidence",
        source_hash="sha256:" + "d" * 64,
        title="Evidence",
        pages=pages,
        run_id="run-test",
        cancellation=ManualCancellationToken(),
    )

    assert tree.nodes[-1].end_page == 2


def test_semantic_pageindex_fingerprint_is_stable_and_model_bound(tmp_path: Any) -> None:
    gateway = _Gateway({"sections": []})
    cache = StructuredInferenceCache((tmp_path / "cache").resolve())
    first = SemanticPageIndexBuilder(
        gateway_factory=lambda _: gateway,
        workspace_id="ws-test",
        model="model-a",
        cache=cache,
        ids=DeterministicIdGenerator(),
        budget=KnowledgeInferenceBudget(),
    )
    second = SemanticPageIndexBuilder(
        gateway_factory=lambda _: gateway,
        workspace_id="ws-test",
        model="model-b",
        cache=cache,
        ids=DeterministicIdGenerator(),
        budget=KnowledgeInferenceBudget(),
    )

    assert first.fingerprint == first.fingerprint
    assert first.fingerprint != second.fingerprint
