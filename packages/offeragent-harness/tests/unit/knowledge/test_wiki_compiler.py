from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.knowledge import (
    KnowledgeCatalogStore,
    KnowledgeCompiler,
    KnowledgeDiscovery,
    KnowledgeInferenceBudget,
    KnowledgeObjectStore,
    KnowledgePreparationService,
    KnowledgePreparationStore,
    KnowledgePublisher,
    LLMWikiCompiler,
    PageIndexNode,
    PageIndexTree,
    SemanticKnowledgeError,
    StructuredInferenceCache,
)
from offeragent_harness.models import ModelEvent, ModelEventKind, ModelFinishReason, ModelRequest, ModelUsage
from offeragent_harness.testing import DeterministicIdGenerator, ManualCancellationToken


class _SemanticBuilder:
    fingerprint = "sha256:" + "e" * 64

    async def build(self, **kwargs: Any) -> PageIndexTree:
        pages = kwargs["pages"]
        root = PageIndexNode(
            "node-root",
            None,
            0,
            kwargs["title"],
            "A grounded document summary.",
            1,
            len(pages),
            (),
        )
        return PageIndexTree(kwargs["source_id"], kwargs["source_hash"], len(pages), "node-root", (root,))


class _Gateway:
    def __init__(self, output: Mapping[str, Any]) -> None:
        self.output = output
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest, cancellation: Any) -> AsyncIterator[ModelEvent]:
        cancellation.checkpoint()
        self.requests.append(request)
        usage = ModelUsage(200, 80, 30, 0)
        yield ModelEvent(request.request_id, 1, ModelEventKind.STARTED)
        yield ModelEvent(request.request_id, 2, ModelEventKind.STRUCTURED_OUTPUT, data=self.output)
        yield ModelEvent(request.request_id, 3, ModelEventKind.USAGE, usage=usage)
        yield ModelEvent(request.request_id, 4, ModelEventKind.COMPLETED, finish_reason=ModelFinishReason.STOP)


async def _prepared_components(tmp_path: Path) -> tuple[Any, KnowledgeCompiler, KnowledgePreparationStore]:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "input.md").write_text("# Evidence\n\nA grounded observation.", encoding="utf-8")
    state = tmp_path / ".offeragent" / "knowledge"
    catalog = KnowledgeCatalogStore(state)
    objects = KnowledgeObjectStore(state)
    preparations = KnowledgePreparationStore(state, objects)
    discovery = KnowledgeDiscovery(workspace_id="ws-test", vault_root=tmp_path)
    service = KnowledgePreparationService(
        vault_root=tmp_path,
        discovery=discovery,
        catalog=catalog,
        objects=objects,
        preparations=preparations,
        binary_parser=None,
        page_index_builder=_SemanticBuilder(),
    )
    candidate = service.status().candidates[0]
    preparation = await service.prepare(
        candidate_ids=(candidate.candidate_id,),
        run_id="run-test",
        cancellation=ManualCancellationToken(),
    )
    compiler = KnowledgeCompiler(
        discovery=discovery,
        catalog=catalog,
        preparations=preparations,
        publisher=KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects),
    )
    return preparation, compiler, preparations


def _output(preparation: Any, *, cited: bool = True) -> dict[str, object]:
    source = preparation.prepared_sources[0].source
    marker = f"[source:{source.source_id}@{source.content_hash}#node:node-root#pages:1-1]"
    body = f"- A grounded observation. {marker}" if cited else "- A grounded observation without citation."
    return {
        "pages": [
            {
                "aliases": ["Input paper"],
                "body": body,
                "citations": [
                    {
                        "endPage": 1,
                        "nodeId": "node-root",
                        "sourceHash": source.content_hash,
                        "sourceId": source.source_id,
                        "startPage": 1,
                    }
                ],
                "pageType": "summary",
                "relatedWikiIds": [],
                "title": "Input",
                "wikiId": "summary-input",
            }
        ]
    }


@pytest.mark.asyncio
async def test_llm_wiki_compiler_publishes_only_inline_cited_pages(tmp_path: Path) -> None:
    preparation, compiler, preparations = await _prepared_components(tmp_path)
    gateway = _Gateway(_output(preparation))
    semantic = LLMWikiCompiler(
        preparations=preparations,
        compiler=compiler,
        gateway_factory=lambda _: gateway,
        workspace_id="ws-test",
        model="deepseek-v4-flash",
        cache=StructuredInferenceCache((tmp_path / ".offeragent" / "knowledge" / "inference-cache").resolve()),
        ids=DeterministicIdGenerator(),
        budget=KnowledgeInferenceBudget(),
    )

    result = await semantic.compile(
        ingestion_id=preparation.ingestion_id,
        base_revision=preparation.base_revision,
        run_id="run-test",
        cancellation=ManualCancellationToken(),
    )

    assert result.catalog.revision == 1 and result.page_count == 1
    assert result.input_tokens == 200 and result.cached_input_tokens == 30
    assert "A grounded observation" in (tmp_path / "knowledge" / "summaries" / "summary-input.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_llm_wiki_compiler_rejects_uncited_factual_lines_before_publish(tmp_path: Path) -> None:
    preparation, compiler, preparations = await _prepared_components(tmp_path)
    gateway = _Gateway(_output(preparation, cited=False))
    semantic = LLMWikiCompiler(
        preparations=preparations,
        compiler=compiler,
        gateway_factory=lambda _: gateway,
        workspace_id="ws-test",
        model="deepseek-v4-flash",
        cache=StructuredInferenceCache((tmp_path / ".offeragent" / "knowledge" / "inference-cache").resolve()),
        ids=DeterministicIdGenerator(),
        budget=KnowledgeInferenceBudget(),
    )

    with pytest.raises(SemanticKnowledgeError, match="inline source citation"):
        await semantic.compile(
            ingestion_id=preparation.ingestion_id,
            base_revision=preparation.base_revision,
            run_id="run-test",
            cancellation=ManualCancellationToken(),
        )
    assert not (tmp_path / "knowledge").exists()
