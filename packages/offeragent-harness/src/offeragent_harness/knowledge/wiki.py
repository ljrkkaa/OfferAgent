from __future__ import annotations

from dataclasses import dataclass

from .models import CatalogSnapshot, PageIndexTree, WikiPagePatch, WikiPatchPlan


class WikiPlanValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedWikiPlan:
    plan: WikiPatchPlan
    enriched_page_indexes: tuple[PageIndexTree, ...]
    rendered_pages: tuple[tuple[str, bytes], ...]


class WikiPlanValidator:
    def validate(
        self,
        *,
        plan: WikiPatchPlan,
        catalog: CatalogSnapshot,
        page_indexes: tuple[PageIndexTree, ...],
        citation_page_indexes: tuple[PageIndexTree, ...] = (),
        existing_wiki_ids: frozenset[str] = frozenset(),
    ) -> ValidatedWikiPlan:
        if plan.base_revision != catalog.revision:
            raise WikiPlanValidationError("wiki plan base revision is stale")
        sources = {source.source_id: source for source in catalog.sources}
        all_indexes = (*page_indexes, *citation_page_indexes)
        indexes = {(tree.source_id, tree.source_hash): tree for tree in all_indexes}
        if len(indexes) != len(all_indexes):
            raise WikiPlanValidationError("wiki plan PageIndex inputs contain duplicate source versions")
        summary_by_node = {
            (item.source_id, item.source_hash, item.node_id): item.summary.strip() for item in plan.page_index_summaries
        }
        all_summary_keys = {
            (tree.source_id, tree.source_hash, node.node_id) for tree in page_indexes for node in tree.nodes
        }
        required_summary_keys = {
            (tree.source_id, tree.source_hash, node.node_id)
            for tree in page_indexes
            for node in tree.nodes
            if not node.summary.strip()
        }
        if not required_summary_keys <= set(summary_by_node) or not set(summary_by_node) <= all_summary_keys:
            raise WikiPlanValidationError("wiki plan must summarize every missing prepared PageIndex node exactly once")
        for tree in page_indexes:
            for node in tree.nodes:
                patched = summary_by_node.get((tree.source_id, tree.source_hash, node.node_id))
                if patched is not None and node.summary.strip() and patched != node.summary.strip():
                    raise WikiPlanValidationError("wiki plan cannot replace a grounded PageIndex summary")
        enriched = tuple(
            PageIndexTree(
                tree.source_id,
                tree.source_hash,
                tree.page_count,
                tree.root_id,
                tuple(
                    type(node)(
                        node.node_id,
                        node.parent_id,
                        node.depth,
                        node.title,
                        summary_by_node.get(
                            (tree.source_id, tree.source_hash, node.node_id),
                            node.summary.strip(),
                        ),
                        node.start_page,
                        node.end_page,
                        node.children,
                    )
                    for node in tree.nodes
                ),
            )
            for tree in page_indexes
        )
        plan_ids = {page.wiki_id for page in plan.pages}
        if not set(plan.deleted_wiki_ids) <= existing_wiki_ids:
            raise WikiPlanValidationError("wiki plan deletes an unknown page")
        allowed_links = (existing_wiki_ids - set(plan.deleted_wiki_ids)) | plan_ids
        rendered: list[tuple[str, bytes]] = []
        for page in plan.pages:
            for related in page.related_wiki_ids:
                if related not in allowed_links:
                    raise WikiPlanValidationError("wiki plan contains an unresolved related page")
            for citation in page.citations:
                source = sources.get(citation.source_id)
                if source is None or source.content_hash != citation.source_hash:
                    raise WikiPlanValidationError("wiki citation source version is not cataloged")
                citation_tree = indexes.get((citation.source_id, citation.source_hash))
                if citation_tree is None:
                    raise WikiPlanValidationError("wiki citation has no validated PageIndex")
                citation_node = next(
                    (item for item in citation_tree.nodes if item.node_id == citation.node_id),
                    None,
                )
                if citation_node is None or not (
                    citation_node.start_page <= citation.start_page <= citation.end_page <= citation_node.end_page
                ):
                    raise WikiPlanValidationError("wiki citation is outside its PageIndex node")
            rendered.append((page.path, _render_page(page, catalog.revision + 1)))
        rendered.sort(key=lambda item: item[0].casefold())
        return ValidatedWikiPlan(plan, enriched, tuple(rendered))


def _render_page(page: WikiPagePatch, target_revision: int) -> bytes:
    aliases = ", ".join(_yaml_scalar(item) for item in page.aliases)
    lines = [
        "---",
        f"wiki_id: {_yaml_scalar(page.wiki_id)}",
        f"type: {_yaml_scalar(page.page_type.value)}",
        f"title: {_yaml_scalar(page.title)}",
        f"aliases: [{aliases}]",
        f"knowledge_revision: {target_revision}",
        "---",
        "",
        f"# {page.title.strip()}",
        "",
        page.body.strip(),
        "",
        "## Sources",
        "",
    ]
    lines.extend(
        f"- [source:{item.source_id}@{item.source_hash}#node:{item.node_id}#pages:{item.start_page}-{item.end_page}]"
        for item in page.citations
    )
    if page.related_wiki_ids:
        lines.extend(("", "## Related", ""))
        lines.extend(f"- [[{item}]]" for item in page.related_wiki_ids)
    return ("\n".join(lines) + "\n").encode("utf-8", errors="strict")


def _yaml_scalar(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")
    return f'"{escaped}"'
