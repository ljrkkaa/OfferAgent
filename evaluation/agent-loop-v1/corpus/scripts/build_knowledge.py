"""Build the real-paper PageIndex and LLM-Wiki projection through product code."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from offeragent_harness.documents import (
    DocumentMediaType,
    DocumentParseRequest,
    DocumentSource,
)
from offeragent_harness.documents.adapters.production import build_bundled_parser
from offeragent_harness.knowledge import (
    KnowledgeCatalogStore,
    KnowledgeCompiler,
    KnowledgeDiscovery,
    KnowledgeObjectStore,
    KnowledgeParseResult,
    KnowledgePreparation,
    KnowledgePreparationService,
    KnowledgePreparationStore,
    KnowledgePublisher,
    PageEvidence,
    PageIndexNode,
    PageIndexSummaryPatch,
    PreparedKnowledgeSource,
    SourceRecord,
    WikiCitation,
    WikiPagePatch,
    WikiPageType,
    WikiPatchPlan,
    stable_readable_id,
    unique_readable_slugs,
)
from offeragent_harness.runtime.document_parser_profile import (
    BUNDLED_DOCUMENT_PARSER_CONFIG,
)


class _Cancellation:
    def checkpoint(self) -> None:
        return


@dataclass(frozen=True, slots=True)
class _ParseTiming:
    source_path: str
    page_count: int
    elapsed_seconds: float
    ocr_page_count: int


class _LiteratureParser:
    def __init__(self) -> None:
        started = time.perf_counter()
        self._parser = build_bundled_parser(BUNDLED_DOCUMENT_PARSER_CONFIG)
        self.initialization_seconds = time.perf_counter() - started
        self.timings: list[_ParseTiming] = []

    @property
    def parser_fingerprint(self) -> str:
        return BUNDLED_DOCUMENT_PARSER_CONFIG.fingerprint

    async def parse(
        self,
        *,
        source: SourceRecord,
        absolute_path: Path,
        cancellation: object,
    ) -> KnowledgeParseResult:
        del cancellation
        request = DocumentParseRequest(
            f"literature-{source.content_hash.removeprefix('sha256:')[:24]}",
            DocumentSource(
                source.source_id,
                absolute_path,
                DocumentMediaType(source.media_type),
                source.content_hash,
            ),
        )
        started = time.perf_counter()
        parsed = await asyncio.to_thread(self._parser.parse, request, _Cancellation())
        elapsed = time.perf_counter() - started
        pages = tuple(
            PageEvidence(
                page.provenance.page_number,
                page.text,
                _sha256(page.text.encode("utf-8", errors="strict")),
            )
            for page in parsed.pages
        )
        self.timings.append(
            _ParseTiming(
                source.relative_path,
                len(parsed.pages),
                elapsed,
                sum(page.extraction_method.value == "ocr" for page in parsed.pages),
            )
        )
        return KnowledgeParseResult(
            source.source_id,
            source.content_hash,
            pages,
            parsed.parser_config_fingerprint,
        )


def _read_metadata(path: Path) -> dict[str, dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    documents = value.get("documents") if isinstance(value, dict) else None
    if value.get("schemaVersion") != 1 or not isinstance(documents, list):
        raise ValueError("source manifest is invalid")
    metadata = {f"raw/{item['filename']}": item for item in documents}
    if len(documents) != 30 or len(metadata) != 30:
        raise ValueError("source manifest cardinality is invalid")
    return metadata


async def _build(vault: Path, source_manifest: Path) -> dict[str, object]:
    if (vault / "knowledge").exists() or (
        vault / ".offeragent" / "knowledge" / "catalog.json"
    ).exists():
        raise RuntimeError("literature Vault already contains a built knowledge base")
    metadata = _read_metadata(source_manifest)
    workspace_id = "ws-offeragent-llm-literature-v1"
    state = vault / ".offeragent" / "knowledge"
    catalog = KnowledgeCatalogStore(state)
    objects = KnowledgeObjectStore(state)
    preparations = KnowledgePreparationStore(state, objects)
    discovery = KnowledgeDiscovery(
        workspace_id=workspace_id,
        vault_root=vault,
        maximum_file_bytes=BUNDLED_DOCUMENT_PARSER_CONFIG.max_file_bytes,
    )
    parser = _LiteratureParser()
    preparation_service = KnowledgePreparationService(
        vault_root=vault,
        discovery=discovery,
        catalog=catalog,
        objects=objects,
        preparations=preparations,
        binary_parser=parser,
    )
    publisher = KnowledgePublisher(vault_root=vault, catalog=catalog, objects=objects)
    compiler = KnowledgeCompiler(
        discovery=discovery,
        catalog=catalog,
        preparations=preparations,
        publisher=publisher,
    )
    status = preparation_service.status()
    candidate_ids = tuple(sorted(item.candidate_id for item in status.candidates))
    if len(candidate_ids) != 30:
        raise RuntimeError("discovery did not find all literature sources")
    started = time.perf_counter()
    preparation = await preparation_service.prepare(
        candidate_ids=candidate_ids,
        cancellation=_Cancellation(),
    )
    prepare_seconds = time.perf_counter() - started
    plan, concept_count = _compile_literature_plan(preparation, metadata=metadata)
    started = time.perf_counter()
    committed = compiler.publish(ingestion_id=preparation.ingestion_id, plan=plan)
    publish_seconds = time.perf_counter() - started
    latencies = [item.elapsed_seconds for item in parser.timings]
    timings_by_path = {item.source_path: item for item in parser.timings}
    return {
        "schemaVersion": 1,
        "compiler": "source-grounded-literature-wiki-v1",
        "strategy": "OpenKB-style source pages, entity pages, concept pages, and structural PageIndex",
        "runtimeParser": {
            "pdf": "PyMuPDF embedded text and CPU rasterization",
            "ocr": "RapidOCR ONNX Runtime CUDA device 0; fail closed",
            "ocrExecutionProvider": BUNDLED_DOCUMENT_PARSER_CONFIG.ocr_execution_provider,
        },
        "catalogRevision": committed.revision,
        "sourceCount": len(committed.sources),
        "pageIndexCount": len(publisher.current_page_indexes()),
        "wikiPageCount": len(publisher.existing_wiki_ids()),
        "conceptPageCount": concept_count,
        "parserInitializationSeconds": round(parser.initialization_seconds, 6),
        "prepareSeconds": round(prepare_seconds, 6),
        "publishSeconds": round(publish_seconds, 6),
        "parseLatencySeconds": {
            "p50": _rounded_percentile(latencies, 50),
            "p95": _rounded_percentile(latencies, 95),
        },
        "ocrPageCount": sum(item.ocr_page_count for item in parser.timings),
        "cacheHitCount": len(preparation.prepared_sources) - len(parser.timings),
        "totalPageCount": sum(len(item.pages) for item in preparation.prepared_sources),
        "sources": [
            {
                "path": prepared.source.relative_path,
                "pageCount": len(prepared.pages),
                "ocrPageCount": (
                    timings_by_path[prepared.source.relative_path].ocr_page_count
                    if prepared.source.relative_path in timings_by_path
                    else 0
                ),
                "elapsedSeconds": (
                    round(
                        timings_by_path[prepared.source.relative_path].elapsed_seconds,
                        6,
                    )
                    if prepared.source.relative_path in timings_by_path
                    else None
                ),
                "cacheHit": prepared.source.relative_path not in timings_by_path,
            }
            for prepared in preparation.prepared_sources
        ],
    }


def _compile_literature_plan(
    preparation: KnowledgePreparation,
    *,
    metadata: dict[str, dict[str, Any]],
) -> tuple[WikiPatchPlan, int]:
    summaries: list[PageIndexSummaryPatch] = []
    paper_ids: dict[str, str] = {}
    summary_ids: dict[str, str] = {}
    concept_members: defaultdict[tuple[str, str], list[PreparedKnowledgeSource]] = (
        defaultdict(list)
    )
    source_slugs = unique_readable_slugs(
        (
            (prepared.source.source_id, str(metadata[prepared.source.relative_path]["title"]))
            for prepared in preparation.prepared_sources
        ),
        fallback="paper",
        max_length=48,
    )
    for prepared in preparation.prepared_sources:
        source = prepared.source
        if source.relative_path not in metadata:
            raise ValueError("prepared source is absent from the source manifest")
        item = metadata[source.relative_path]
        title = str(item["title"])
        paper_ids[source.source_id] = stable_readable_id(
            "paper", title, namespace=source.source_id
        )
        summary_ids[source.source_id] = stable_readable_id(
            "summary", title, namespace=source.source_id
        )
        concept_members[("category", str(item["category"]))].append(prepared)
        for tag in item["tags"]:
            concept_members[("tag", str(tag))].append(prepared)
        evidence_by_page = {page.page_number: page.text for page in prepared.pages}
        for node in prepared.page_index.nodes:
            summaries.append(
                PageIndexSummaryPatch(
                    source.source_id,
                    source.content_hash,
                    node.node_id,
                    _node_summary(node, evidence_by_page),
                )
            )

    concept_keys_by_identity = {f"{key[0]}:{key[1]}": key for key in concept_members}
    concept_slugs = unique_readable_slugs(
        ((identity, identity) for identity in concept_keys_by_identity),
        fallback="concept",
        max_length=48,
    )
    concept_ids = {
        key: stable_readable_id(
            "concept", f"{key[0]}-{key[1]}", namespace=f"{key[0]}:{key[1]}"
        )
        for key in concept_members
    }
    wiki_pages: list[WikiPagePatch] = []
    for prepared in preparation.prepared_sources:
        source = prepared.source
        item = metadata[source.relative_path]
        aliases = _aliases(item["aliases"], title=str(item["title"]))
        concept_keys = [("category", str(item["category"]))] + [
            ("tag", str(tag)) for tag in item["tags"]
        ]
        related_concepts = tuple(sorted(concept_ids[key] for key in concept_keys))
        opening_pages = prepared.pages[: min(3, len(prepared.pages))]
        citations = tuple(
            _citation(prepared, evidence.page_number) for evidence in opening_pages
        )
        opening_text = "\n\n".join(evidence.text for evidence in opening_pages)
        metadata_body = (
            f"- Full title: {item['title']}\n"
            f"- Known aliases: {', '.join(aliases) if aliases else '(none)'}\n"
            f"- arXiv: {item['arxivId']}\n"
            f"- Category: {item['category']}\n"
            f"- Topics: {', '.join(str(tag) for tag in item['tags'])}\n"
            f"- Official page: {item['officialUrl']}"
        )
        wiki_pages.append(
            WikiPagePatch(
                summary_ids[source.source_id],
                WikiPageType.SUMMARY,
                f"summaries/{source_slugs[source.source_id]}.md",
                f"Summary: {item['title']}",
                aliases,
                f"{metadata_body}\n\n## Opening-page evidence\n\n{opening_text[:30_000]}",
                citations,
                tuple(sorted({paper_ids[source.source_id], *related_concepts})),
            )
        )
        wiki_pages.append(
            WikiPagePatch(
                paper_ids[source.source_id],
                WikiPageType.ENTITY,
                f"entities/{source_slugs[source.source_id]}.md",
                str(item["title"]),
                aliases,
                metadata_body,
                (_citation(prepared, 1),),
                tuple(sorted({summary_ids[source.source_id], *related_concepts})),
            )
        )

    for key, members in sorted(concept_members.items()):
        kind, value = key
        concept_id = concept_ids[key]
        member_lines = [
            f"- {metadata[item.source.relative_path]['title']}"
            for item in sorted(members, key=lambda value: value.source.relative_path)
        ]
        citations = tuple(
            _citation(item, 1)
            for item in sorted(members, key=lambda value: value.source.relative_path)
        )
        related = tuple(sorted(paper_ids[item.source.source_id] for item in members))
        wiki_pages.append(
            WikiPagePatch(
                concept_id,
                WikiPageType.CONCEPT,
                f"concepts/{concept_slugs[f'{kind}:{value}']}.md",
                f"{kind.title()}: {value}",
                (),
                (
                    f"Corpus indexing metadata groups the following source papers under {kind} "
                    f'"{value}":\n\n' + "\n".join(member_lines)
                ),
                citations,
                related,
            )
        )
    return (
        WikiPatchPlan(
            preparation.ingestion_id,
            preparation.base_revision,
            tuple(summaries),
            tuple(wiki_pages),
        ),
        len(concept_ids),
    )


def _aliases(value: object, *, title: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError("paper aliases are invalid")
    result: list[str] = []
    seen = {title.strip().casefold()}
    for alias in value:
        normalized = alias.strip()
        if normalized.casefold() in seen:
            continue
        seen.add(normalized.casefold())
        result.append(normalized)
    return tuple(result)


def _citation(prepared: PreparedKnowledgeSource, page_number: int) -> WikiCitation:
    node = _node_for_page(prepared, page_number)
    return WikiCitation(
        prepared.source.source_id,
        prepared.source.content_hash,
        node.node_id,
        page_number,
        page_number,
    )


def _node_for_page(
    prepared: PreparedKnowledgeSource, page_number: int
) -> PageIndexNode:
    candidates = [
        node
        for node in prepared.page_index.nodes
        if node.start_page <= page_number <= node.end_page
    ]
    return max(candidates, key=lambda item: item.depth)


def _node_summary(node: PageIndexNode, pages: dict[int, str]) -> str:
    selected = [pages[node.start_page]]
    if node.end_page != node.start_page:
        selected.append(pages[node.end_page])
    normalized = " ".join(" ".join(selected).split())
    if not normalized:
        raise ValueError("cannot summarize empty PageIndex evidence")
    prefix = f"{node.title}: "
    return prefix + normalized[: 2_000 - len(prefix)]


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile / 100
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _rounded_percentile(values: list[float], percentile: int) -> float | None:
    value = _percentile(values, percentile)
    return None if value is None else round(value, 6)


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(
        _build(
            args.vault.resolve(strict=True),
            args.sources.resolve(strict=True),
        )
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
