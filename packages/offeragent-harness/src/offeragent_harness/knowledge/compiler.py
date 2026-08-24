from __future__ import annotations

from .catalog import KnowledgeCatalogStore
from .discovery import KnowledgeDiscovery
from .models import CatalogSnapshot, SourceRecord, WikiPageType, WikiPatchPlan
from .objects import PreparedKnowledgeSource
from .preparation import KnowledgePreparationStore
from .publisher import KnowledgePublisher
from .wiki import WikiPlanValidationError, WikiPlanValidator


class KnowledgeCompilationError(RuntimeError):
    pass


class KnowledgeCompiler:
    """Commit one durable preparation through the existing Agent Loop's structured plan."""

    def __init__(
        self,
        *,
        discovery: KnowledgeDiscovery,
        catalog: KnowledgeCatalogStore,
        preparations: KnowledgePreparationStore,
        publisher: KnowledgePublisher,
        validator: WikiPlanValidator | None = None,
    ) -> None:
        self._discovery = discovery
        self._catalog = catalog
        self._preparations = preparations
        self._publisher = publisher
        self._validator = validator or WikiPlanValidator()

    def publish(self, *, ingestion_id: str, plan: WikiPatchPlan) -> CatalogSnapshot:
        preparation = self._preparations.load(ingestion_id)
        if plan.ingestion_id != ingestion_id or plan.base_revision != preparation.base_revision:
            raise KnowledgeCompilationError("knowledge Wiki plan is not bound to its preparation")
        current = self._catalog.load()
        if current.revision != preparation.base_revision:
            raise KnowledgeCompilationError("knowledge preparation catalog revision is stale")
        status = self._discovery.status(catalog_revision=current.revision, catalog=current.sources)
        live_by_id = {candidate.source.source_id: candidate for candidate in status.candidates}
        prepared_candidate_ids: set[str] = set()
        for prepared in preparation.prepared_sources:
            candidate = live_by_id.get(prepared.source.source_id)
            if candidate is None or candidate.source != prepared.source:
                raise KnowledgeCompilationError("knowledge source changed after preparation")
            prepared_candidate_ids.add(candidate.candidate_id)
        if prepared_candidate_ids != set(preparation.candidate_ids):
            raise KnowledgeCompilationError("knowledge candidate identities changed after preparation")
        missing_ids = {source.source_id for source in status.missing}
        if not set(preparation.removed_source_ids) <= missing_ids:
            raise KnowledgeCompilationError("knowledge source removal changed after preparation")
        if _target_sources(current.sources, preparation.prepared_sources, preparation.removed_source_ids) != (
            preparation.target_sources
        ):
            raise KnowledgeCompilationError("knowledge preparation target catalog drifted")

        prepared_indexes = tuple(item.page_index for item in preparation.prepared_sources)
        prepared_versions = {(tree.source_id, tree.source_hash) for tree in prepared_indexes}
        summarized_versions = {
            (citation.source_id, citation.source_hash)
            for page in plan.pages
            if page.page_type is WikiPageType.SUMMARY
            for citation in page.citations
        }
        if not prepared_versions <= summarized_versions:
            raise KnowledgeCompilationError("every prepared source requires a grounded summary page")
        existing_indexes = tuple(
            tree
            for tree in self._publisher.current_page_indexes()
            if (tree.source_id, tree.source_hash) not in prepared_versions
        )
        try:
            validated = self._validator.validate(
                plan=plan,
                catalog=CatalogSnapshot(current.revision, preparation.target_sources),
                page_indexes=prepared_indexes,
                citation_page_indexes=existing_indexes,
                existing_wiki_ids=self._publisher.existing_wiki_ids(),
            )
        except WikiPlanValidationError as error:
            raise KnowledgeCompilationError("knowledge Wiki plan failed grounded validation") from error
        return self._publisher.publish(validated=validated, target_sources=preparation.target_sources)

    def current_wiki_ids(self) -> frozenset[str]:
        return self._publisher.existing_wiki_ids()


def _target_sources(
    current: tuple[SourceRecord, ...],
    prepared: tuple[PreparedKnowledgeSource, ...],
    removals: tuple[str, ...],
) -> tuple[SourceRecord, ...]:
    by_id = {source.source_id: source for source in current}
    for source_id in removals:
        by_id.pop(source_id, None)
    for item in prepared:
        by_id[item.source.source_id] = item.source
    return tuple(sorted(by_id.values(), key=lambda source: source.relative_path.casefold()))


__all__ = ["KnowledgeCompilationError", "KnowledgeCompiler"]
