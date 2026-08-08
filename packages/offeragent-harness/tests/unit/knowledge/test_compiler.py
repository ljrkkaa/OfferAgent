from __future__ import annotations

from pathlib import Path

import pytest

from offeragent_harness.knowledge import (
    CatalogSnapshot,
    KnowledgeCatalogStore,
    KnowledgeCompilationError,
    KnowledgeCompiler,
    KnowledgeDiscovery,
    KnowledgeObjectStore,
    KnowledgePreparationService,
    KnowledgePreparationStore,
    KnowledgePublisher,
    PageIndexSummaryPatch,
    WikiCitation,
    WikiPagePatch,
    WikiPageType,
    WikiPatchPlan,
)


class _Cancellation:
    def checkpoint(self) -> None:
        return


def _components(tmp_path: Path) -> tuple[KnowledgePreparationService, KnowledgeCompiler, KnowledgeCatalogStore]:
    state = tmp_path / ".offeragent" / "knowledge"
    catalog = KnowledgeCatalogStore(state)
    objects = KnowledgeObjectStore(state)
    preparations = KnowledgePreparationStore(state, objects)
    discovery = KnowledgeDiscovery(workspace_id="ws-test", vault_root=tmp_path)
    publisher = KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects)
    preparation = KnowledgePreparationService(
        vault_root=tmp_path,
        discovery=discovery,
        catalog=catalog,
        objects=objects,
        preparations=preparations,
        binary_parser=None,
    )
    compiler = KnowledgeCompiler(
        discovery=discovery,
        catalog=catalog,
        preparations=preparations,
        publisher=publisher,
    )
    return preparation, compiler, catalog


def _plan(preparation: object) -> WikiPatchPlan:
    from offeragent_harness.knowledge import KnowledgePreparation

    assert isinstance(preparation, KnowledgePreparation)
    source = preparation.prepared_sources[0]
    summaries = tuple(
        PageIndexSummaryPatch(
            source.source.source_id,
            source.source.content_hash,
            node.node_id,
            f"Observed structure: {node.title}",
        )
        for node in source.page_index.nodes
    )
    leaf = source.page_index.nodes[-1]
    return WikiPatchPlan(
        preparation.ingestion_id,
        preparation.base_revision,
        summaries,
        (
            WikiPagePatch(
                "summary-observed",
                WikiPageType.SUMMARY,
                "summaries/observed.md",
                "Observed",
                (),
                "A synthesis supported by the prepared evidence.",
                (
                    WikiCitation(
                        source.source.source_id,
                        source.source.content_hash,
                        leaf.node_id,
                        leaf.start_page,
                        leaf.end_page,
                    ),
                ),
                (),
            ),
        ),
    )


async def test_compiler_publishes_a_prepared_markdown_source_end_to_end(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "input.md").write_text("# Novel topic\n\nA grounded observation.", encoding="utf-8")
    preparation_service, compiler, catalog = _components(tmp_path)
    candidate = preparation_service.status().candidates[0]
    preparation = await preparation_service.prepare(
        candidate_ids=(candidate.candidate_id,), cancellation=_Cancellation()
    )

    committed = compiler.publish(ingestion_id=preparation.ingestion_id, plan=_plan(preparation))

    assert committed == CatalogSnapshot(1, (candidate.source,))
    assert "supported" in (tmp_path / "knowledge" / "summaries" / "observed.md").read_text(encoding="utf-8")
    assert catalog.load() == committed


async def test_compiler_rejects_a_source_changed_after_preparation(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "input.md"
    source.write_text("# Before\n\nOriginal evidence.", encoding="utf-8")
    preparation_service, compiler, catalog = _components(tmp_path)
    candidate = preparation_service.status().candidates[0]
    preparation = await preparation_service.prepare(
        candidate_ids=(candidate.candidate_id,), cancellation=_Cancellation()
    )
    source.write_text("# After\n\nReplacement evidence.", encoding="utf-8")

    with pytest.raises(KnowledgeCompilationError, match="changed after preparation"):
        compiler.publish(ingestion_id=preparation.ingestion_id, plan=_plan(preparation))

    assert catalog.load().revision == 0
    assert not (tmp_path / "knowledge").exists()
