"""Cached LLM Wiki planning over validated semantic PageIndex nodes."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from jsonschema import Draft202012Validator

from offeragent_harness.agent.model_planner import collect_structured_response
from offeragent_harness.foundation.canonical import canonical_json_bytes, canonical_json_sha256
from offeragent_harness.models import (
    ModelContentBlock,
    ModelMessage,
    ModelOutputMode,
    ModelPurpose,
    ModelRequest,
    ModelRole,
    TraceContext,
    thaw_json,
)
from offeragent_harness.ports import CancellationToken, IdGenerator, ModelGateway

from .compiler import KnowledgeCompiler
from .models import (
    CatalogSnapshot,
    WikiCitation,
    WikiPagePatch,
    WikiPageType,
    WikiPatchPlan,
)
from .preparation import KnowledgePreparationStore
from .semantic import KnowledgeInferenceBudget, SemanticKnowledgeError, StructuredInferenceCache

LLM_WIKI_PROMPT_VERSION = "llm-wiki-compiler-v1"
_IDENTIFIER = r"^[a-z][a-z0-9_-]{2,127}$"
_HASH = r"^sha256:[0-9a-f]{64}$"
_INLINE_CITATION = re.compile(
    r"\[source:(?P<source>[a-z][a-z0-9_-]{2,127})@(?P<hash>sha256:[0-9a-f]{64})"
    r"#node:(?P<node>[a-z][a-z0-9_-]{2,127})#pages:(?P<start>[1-9][0-9]*)-(?P<end>[1-9][0-9]*)\]"
)


@dataclass(frozen=True, slots=True)
class LLMWikiPolicy:
    maximum_sources: int = 8
    maximum_nodes: int = 512
    maximum_request_tokens: int = 250_000
    maximum_output_tokens: int = 32_768

    def __post_init__(self) -> None:
        if (
            min(
                self.maximum_sources,
                self.maximum_nodes,
                self.maximum_request_tokens,
                self.maximum_output_tokens,
            )
            < 1
        ):
            raise ValueError("LLM Wiki policy limits must be positive")


@dataclass(frozen=True, slots=True)
class LLMWikiCompilationResult:
    catalog: CatalogSnapshot
    page_count: int
    cache_hit: bool
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int


class LLMWikiCompiler:
    """Generate one grounded Wiki patch, then commit through the existing validator/publisher."""

    def __init__(
        self,
        *,
        preparations: KnowledgePreparationStore,
        compiler: KnowledgeCompiler,
        gateway_factory: Callable[[str], ModelGateway],
        workspace_id: str,
        model: str,
        cache: StructuredInferenceCache,
        ids: IdGenerator,
        budget: KnowledgeInferenceBudget,
        policy: LLMWikiPolicy | None = None,
    ) -> None:
        if not model or not workspace_id:
            raise ValueError("LLM Wiki model and Workspace must not be empty")
        self._preparations = preparations
        self._compiler = compiler
        self._gateway_factory = gateway_factory
        self._workspace_id = workspace_id
        self._model = model
        self._cache = cache
        self._ids = ids
        self._budget = budget
        self._policy = policy or LLMWikiPolicy()
        self._validator = Draft202012Validator(_WIKI_OUTPUT_SCHEMA)

    async def compile(
        self,
        *,
        ingestion_id: str,
        base_revision: int,
        run_id: str,
        cancellation: CancellationToken,
    ) -> LLMWikiCompilationResult:
        preparation = self._preparations.load(ingestion_id)
        if preparation.base_revision != base_revision:
            raise SemanticKnowledgeError("LLM Wiki base revision does not match its preparation")
        if len(preparation.prepared_sources) > self._policy.maximum_sources:
            raise SemanticKnowledgeError(
                "LLM Wiki preparation exceeds the bounded source batch; ingest smaller batches"
            )
        node_count = sum(len(item.page_index.nodes) for item in preparation.prepared_sources)
        if node_count > self._policy.maximum_nodes:
            raise SemanticKnowledgeError("LLM Wiki preparation exceeds the bounded PageIndex node batch")
        if any(not node.summary.strip() for item in preparation.prepared_sources for node in item.page_index.nodes):
            raise SemanticKnowledgeError("LLM Wiki compilation requires semantic summaries for every PageIndex node")
        planner_input = {
            "baseRevision": base_revision,
            "existingWikiIds": sorted(self._compiler.current_wiki_ids()),
            "ingestionId": ingestion_id,
            "sources": [
                {
                    "contentHash": item.source.content_hash,
                    "nodes": [
                        {
                            "endPage": node.end_page,
                            "nodeId": node.node_id,
                            "parentId": node.parent_id,
                            "startPage": node.start_page,
                            "summary": node.summary,
                            "title": node.title,
                        }
                        for node in item.page_index.nodes
                    ],
                    "path": item.source.relative_path,
                    "sourceId": item.source.source_id,
                }
                for item in preparation.prepared_sources
            ],
        }
        cache_key = canonical_json_sha256(
            {
                "input": planner_input,
                "model": self._model,
                "promptHash": _wiki_prompt_hash(),
                "promptVersion": LLM_WIKI_PROMPT_VERSION,
            }
        )
        cached = self._cache.get(cache_key)
        cache_hit = cached is not None
        before_input = self._budget.input_tokens
        before_output = self._budget.output_tokens
        before_cached = self._budget.cached_input_tokens
        if cached is None:
            output = await self._invoke(planner_input, cache_key, run_id, cancellation)
            self._cache.put(cache_key, output)
        else:
            output = cached
            _raise_schema_errors(self._validator, output)
        plan = _plan_from_output(ingestion_id, base_revision, output)
        for page in plan.pages:
            _validate_inline_citations(page)
        committed = self._compiler.publish(ingestion_id=ingestion_id, plan=plan)
        return LLMWikiCompilationResult(
            catalog=committed,
            page_count=len(plan.pages),
            cache_hit=cache_hit,
            input_tokens=self._budget.input_tokens - before_input,
            output_tokens=self._budget.output_tokens - before_output,
            cached_input_tokens=self._budget.cached_input_tokens - before_cached,
        )

    async def _invoke(
        self,
        planner_input: Mapping[str, Any],
        cache_key: str,
        run_id: str,
        cancellation: CancellationToken,
    ) -> Mapping[str, Any]:
        request = ModelRequest(
            request_id=self._ids.new_id("model-request"),
            model=self._model,
            purpose=ModelPurpose.GROUNDING,
            messages=(
                ModelMessage(ModelRole.SYSTEM, (ModelContentBlock.text(_WIKI_PROMPT),)),
                ModelMessage(
                    ModelRole.USER,
                    (ModelContentBlock.text(canonical_json_bytes(planner_input).decode("utf-8")),),
                ),
            ),
            output_mode=ModelOutputMode.JSON,
            output_schema=_WIKI_OUTPUT_SCHEMA,
            max_output_tokens=self._policy.maximum_output_tokens,
            reasoning_effort="none",
            temperature=0.0,
            seed=None,
            trace_context=TraceContext(self._ids.new_id("trace")),
            metadata={
                "cacheKey": cache_key,
                "promptHash": _wiki_prompt_hash(),
                "promptVersion": LLM_WIKI_PROMPT_VERSION,
                "runId": run_id,
                "workspaceId": self._workspace_id,
            },
        )
        estimate = max(
            1,
            math.ceil(
                sum(
                    len(canonical_json_bytes(thaw_json(block.data)))
                    for message in request.messages
                    for block in message.content
                )
                / 3
            ),
        )
        if estimate > self._policy.maximum_request_tokens:
            raise SemanticKnowledgeError("LLM Wiki request exceeds its Token ceiling")
        self._budget.ensure_estimate(estimate, self._policy.maximum_output_tokens)
        response = await collect_structured_response(self._gateway_factory(self._model), request, cancellation)
        self._budget.charge(response.usage)
        output = cast(dict[str, Any], thaw_json(response.output))
        _raise_schema_errors(self._validator, output)
        return output


def _plan_from_output(ingestion_id: str, base_revision: int, output: Mapping[str, Any]) -> WikiPatchPlan:
    raw_pages = output.get("pages")
    if not isinstance(raw_pages, list):
        raise SemanticKnowledgeError("LLM Wiki pages are invalid")
    pages = tuple(_page_from_output(item) for item in raw_pages)
    return WikiPatchPlan(ingestion_id, base_revision, (), pages, ())


def _page_from_output(value: object) -> WikiPagePatch:
    if not isinstance(value, Mapping):
        raise SemanticKnowledgeError("LLM Wiki page is not an object")
    raw_citations = value.get("citations")
    if not isinstance(raw_citations, list):
        raise SemanticKnowledgeError("LLM Wiki citations are invalid")
    citations = tuple(
        WikiCitation(
            cast(str, item["sourceId"]),
            cast(str, item["sourceHash"]),
            cast(str, item["nodeId"]),
            cast(int, item["startPage"]),
            cast(int, item["endPage"]),
        )
        for item in raw_citations
        if isinstance(item, Mapping)
    )
    if len(citations) != len(raw_citations):
        raise SemanticKnowledgeError("LLM Wiki citation is not an object")
    wiki_id = cast(str, value["wikiId"])
    page_type = WikiPageType(cast(str, value["pageType"]))
    root = {
        WikiPageType.SUMMARY: "summaries",
        WikiPageType.CONCEPT: "concepts",
        WikiPageType.ENTITY: "entities",
    }[page_type]
    return WikiPagePatch(
        wiki_id,
        page_type,
        f"{root}/{wiki_id}.md",
        cast(str, value["title"]),
        tuple(cast(list[str], value["aliases"])),
        cast(str, value["body"]),
        citations,
        tuple(cast(list[str], value["relatedWikiIds"])),
    )


def _validate_inline_citations(page: WikiPagePatch) -> None:
    allowed = {
        (item.source_id, item.source_hash, item.node_id, item.start_page, item.end_page) for item in page.citations
    }
    lines = [line.strip() for line in page.body.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise SemanticKnowledgeError("LLM Wiki body contains no factual lines")
    for line in lines:
        matches = tuple(_INLINE_CITATION.finditer(line))
        if not matches:
            raise SemanticKnowledgeError("every LLM Wiki factual line requires an inline source citation")
        for match in matches:
            key = (
                match.group("source"),
                match.group("hash"),
                match.group("node"),
                int(match.group("start")),
                int(match.group("end")),
            )
            if key not in allowed:
                raise SemanticKnowledgeError("LLM Wiki inline citation is absent from structured citations")


def _raise_schema_errors(validator: Draft202012Validator, value: Mapping[str, Any]) -> None:
    errors = sorted(error.message for error in validator.iter_errors(value))
    if errors:
        raise SemanticKnowledgeError(f"LLM Wiki output failed Schema validation: {errors[0]}")


_CITATION_SCHEMA = {
    "type": "object",
    "properties": {
        "endPage": {"type": "integer", "minimum": 1},
        "nodeId": {"type": "string", "pattern": _IDENTIFIER},
        "sourceHash": {"type": "string", "pattern": _HASH},
        "sourceId": {"type": "string", "pattern": _IDENTIFIER},
        "startPage": {"type": "integer", "minimum": 1},
    },
    "required": ["sourceId", "sourceHash", "nodeId", "startPage", "endPage"],
    "additionalProperties": False,
}
_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "aliases": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 512},
            "maxItems": 64,
            "uniqueItems": True,
        },
        "body": {"type": "string", "minLength": 1, "maxLength": 100_000},
        "citations": {"type": "array", "items": _CITATION_SCHEMA, "minItems": 1, "maxItems": 128},
        "pageType": {"enum": [item.value for item in WikiPageType]},
        "relatedWikiIds": {
            "type": "array",
            "items": {"type": "string", "pattern": _IDENTIFIER},
            "maxItems": 128,
            "uniqueItems": True,
        },
        "title": {"type": "string", "minLength": 1, "maxLength": 512},
        "wikiId": {"type": "string", "pattern": _IDENTIFIER},
    },
    "required": ["wikiId", "pageType", "title", "aliases", "body", "citations", "relatedWikiIds"],
    "additionalProperties": False,
}
_WIKI_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"pages": {"type": "array", "items": _PAGE_SCHEMA, "minItems": 1, "maxItems": 512}},
    "required": ["pages"],
    "additionalProperties": False,
}

_WIKI_PROMPT = """Build an incremental LLM Wiki from validated PageIndex nodes.
Create at least one summary page for every prepared source and optional concept/entity pages only when supported.
Every non-heading body line must end with one or more exact inline citations in this form:
[source:SOURCE_ID@SOURCE_HASH#node:NODE_ID#pages:START-END]
Every inline citation must also appear in that page's structured citations array and stay within the cited node range.
Wiki pages are navigation, not primary evidence. Do not invent facts, identifiers, relationships, or citations.
Use deterministic lowercase IDs; the runtime derives file paths from pageType and wikiId. Output JSON only."""


def _wiki_prompt_hash() -> str:
    return canonical_json_sha256({"prompt": _WIKI_PROMPT, "schema": _WIKI_OUTPUT_SCHEMA})


__all__ = [
    "LLM_WIKI_PROMPT_VERSION",
    "LLMWikiCompilationResult",
    "LLMWikiCompiler",
    "LLMWikiPolicy",
]
