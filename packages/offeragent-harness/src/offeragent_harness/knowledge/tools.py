from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from offeragent_harness.models import thaw_json
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import CancellationToken, OperationCancelled
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffect,
    SideEffectClass,
    SideEffectKind,
    SideEffectState,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
)

from .catalog import KnowledgeCatalogError
from .compiler import KnowledgeCompilationError, KnowledgeCompiler
from .discovery import KnowledgeDiscoveryError
from .models import (
    PageIndexSummaryPatch,
    WikiCitation,
    WikiPagePatch,
    WikiPageType,
    WikiPatchPlan,
)
from .naming import SOURCE_ID_PATTERN
from .objects import KnowledgeObjectError
from .preparation import KnowledgePreparation, KnowledgePreparationError, KnowledgePreparationService
from .publisher import KnowledgePublicationError
from .semantic import SemanticKnowledgeError
from .wiki_compiler import LLMWikiCompiler

KNOWLEDGE_TOOL_VERSION = "1"
_OUTPUT_LIMIT = 2 * 1024 * 1024
_HASH = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
_SOURCE_ID = {"type": "string", "pattern": SOURCE_ID_PATTERN}
_IDENTIFIER = {"type": "string", "pattern": r"^[a-z][a-z0-9_-]{2,127}$"}
_INGESTION_ID = {"type": "string", "pattern": r"^ing-[0-9a-f]{32}$"}


def _object(properties: Mapping[str, Any], required: tuple[str, ...]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_SOURCE = _object(
    {
        "sourceId": _SOURCE_ID,
        "path": {"type": "string", "minLength": 1, "maxLength": 2048},
        "contentHash": _HASH,
        "mediaType": {"type": "string", "minLength": 3, "maxLength": 128},
        "byteSize": {"type": "integer", "minimum": 0},
    },
    ("sourceId", "path", "contentHash", "mediaType", "byteSize"),
)
_CITATION = _object(
    {
        "sourceId": _SOURCE_ID,
        "sourceHash": _HASH,
        "nodeId": _IDENTIFIER,
        "startPage": {"type": "integer", "minimum": 1},
        "endPage": {"type": "integer", "minimum": 1},
    },
    ("sourceId", "sourceHash", "nodeId", "startPage", "endPage"),
)
_SUMMARY_PATCH = _object(
    {
        "sourceId": _SOURCE_ID,
        "sourceHash": _HASH,
        "nodeId": _IDENTIFIER,
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    ("sourceId", "sourceHash", "nodeId", "summary"),
)
_PAGE_PATCH = _object(
    {
        "wikiId": _IDENTIFIER,
        "pageType": {"enum": [item.value for item in WikiPageType]},
        "path": {"type": "string", "minLength": 1, "maxLength": 1024},
        "title": {"type": "string", "minLength": 1, "maxLength": 512},
        "aliases": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 512},
            "maxItems": 128,
            "uniqueItems": True,
        },
        "body": {"type": "string", "minLength": 1, "maxLength": 200_000},
        "citations": {"type": "array", "items": _CITATION, "minItems": 1, "maxItems": 256},
        "relatedWikiIds": {
            "type": "array",
            "items": _IDENTIFIER,
            "maxItems": 256,
            "uniqueItems": True,
        },
    },
    ("wikiId", "pageType", "path", "title", "aliases", "body", "citations", "relatedWikiIds"),
)

_DEFINITIONS = (
    ToolDefinition(
        name="knowledge.status",
        version=KNOWLEDGE_TOOL_VERSION,
        description="Inspect content-hashed new, changed, unchanged, and missing sources under the fixed raw/ root.",
        input_schema=_object({}, ()),
        output_schema=_object(
            {
                "catalogRevision": {"type": "integer", "minimum": 0},
                "candidates": {
                    "type": "array",
                    "maxItems": 1024,
                    "items": _object(
                        {
                            "candidateId": _HASH,
                            "state": {"enum": ["new", "changed", "outdated", "unchanged"]},
                            "source": _SOURCE,
                        },
                        ("candidateId", "state", "source"),
                    ),
                },
                "missing": {"type": "array", "items": _SOURCE, "maxItems": 1024},
            },
            ("catalogRevision", "candidates", "missing"),
        ),
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.READ,
        side_effect_class=SideEffectClass.READ,
        required_capabilities=frozenset({"knowledge.read"}),
        concurrency_safe=True,
        idempotent=True,
        retryable=True,
        timeout_ms=60_000,
        output_limit_bytes=_OUTPUT_LIMIT,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    ),
    ToolDefinition(
        name="knowledge.prepare",
        version=KNOWLEDGE_TOOL_VERSION,
        description=(
            "Parse selected status candidate IDs with the fixed PDF/OCR boundary, persist page evidence, and return "
            "a cached, quote-grounded semantic PageIndex. Candidates must be sorted."
        ),
        input_schema=_object(
            {
                "candidateIds": {
                    "type": "array",
                    "items": _HASH,
                    "minItems": 1,
                    "maxItems": 64,
                    "uniqueItems": True,
                },
                "removedSourceIds": {
                    "type": "array",
                    "items": _SOURCE_ID,
                    "maxItems": 64,
                    "uniqueItems": True,
                    "default": [],
                },
            },
            ("candidateIds",),
        ),
        output_schema=_object(
            {
                "ingestionId": _INGESTION_ID,
                "baseRevision": {"type": "integer", "minimum": 0},
                "sources": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": _object(
                        {
                            "source": _SOURCE,
                            "parserFingerprint": _HASH,
                            "pageCount": {"type": "integer", "minimum": 1},
                            "evidencePagesPath": {"type": "string", "minLength": 1, "maxLength": 2048},
                            "nodesPath": {"type": "string", "minLength": 1, "maxLength": 2048},
                            "nodes": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 10_000,
                                "items": _object(
                                    {
                                        "nodeId": _IDENTIFIER,
                                        "parentId": {"anyOf": [_IDENTIFIER, {"type": "null"}]},
                                        "depth": {"type": "integer", "minimum": 0},
                                        "title": {"type": "string", "minLength": 1, "maxLength": 2048},
                                        "summary": {"type": "string", "maxLength": 2000},
                                        "startPage": {"type": "integer", "minimum": 1},
                                        "endPage": {"type": "integer", "minimum": 1},
                                        "children": {"type": "array", "items": _IDENTIFIER, "maxItems": 10_000},
                                        "evidencePreview": {"type": "string", "maxLength": 2400},
                                    },
                                    (
                                        "nodeId",
                                        "parentId",
                                        "depth",
                                        "title",
                                        "summary",
                                        "startPage",
                                        "endPage",
                                        "children",
                                        "evidencePreview",
                                    ),
                                ),
                            },
                        },
                        ("source", "parserFingerprint", "pageCount", "evidencePagesPath", "nodesPath", "nodes"),
                    ),
                },
            },
            ("ingestionId", "baseRevision", "sources"),
        ),
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        required_capabilities=frozenset({"knowledge.write"}),
        concurrency_safe=False,
        idempotent=True,
        retryable=False,
        timeout_ms=15 * 60_000,
        output_limit_bytes=_OUTPUT_LIMIT,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    ),
    ToolDefinition(
        name="knowledge.compile",
        version=KNOWLEDGE_TOOL_VERSION,
        description=(
            "Generate a cached, citation-validated LLM Wiki plan from one semantic PageIndex preparation and "
            "atomically publish it. The Agent Loop remains the sole orchestrator."
        ),
        input_schema=_object(
            {
                "ingestionId": _INGESTION_ID,
                "baseRevision": {"type": "integer", "minimum": 0},
            },
            ("ingestionId", "baseRevision"),
        ),
        output_schema=_object(
            {
                "catalogRevision": {"type": "integer", "minimum": 1},
                "sourceCount": {"type": "integer", "minimum": 0},
                "wikiPageCount": {"type": "integer", "minimum": 1},
                "cacheHit": {"type": "boolean"},
                "inputTokens": {"type": "integer", "minimum": 0},
                "outputTokens": {"type": "integer", "minimum": 0},
                "cachedInputTokens": {"type": "integer", "minimum": 0},
            },
            (
                "catalogRevision",
                "sourceCount",
                "wikiPageCount",
                "cacheHit",
                "inputTokens",
                "outputTokens",
                "cachedInputTokens",
            ),
        ),
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        required_capabilities=frozenset({"knowledge.write"}),
        concurrency_safe=False,
        idempotent=False,
        retryable=False,
        timeout_ms=15 * 60_000,
        output_limit_bytes=_OUTPUT_LIMIT,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    ),
    ToolDefinition(
        name="knowledge.publish",
        version=KNOWLEDGE_TOOL_VERSION,
        description=(
            "Validate and atomically publish the LLM Wiki plan for one durable preparation. Every prepared PageIndex "
            "node requires one summary and every Wiki page requires structured source citations."
        ),
        input_schema=_object(
            {
                "ingestionId": _INGESTION_ID,
                "baseRevision": {"type": "integer", "minimum": 0},
                "pageIndexSummaries": {
                    "type": "array",
                    "items": _SUMMARY_PATCH,
                    "maxItems": 10_000,
                },
                "pages": {"type": "array", "items": _PAGE_PATCH, "maxItems": 1024},
                "deletedWikiIds": {
                    "type": "array",
                    "items": _IDENTIFIER,
                    "maxItems": 1024,
                    "uniqueItems": True,
                    "default": [],
                },
            },
            ("ingestionId", "baseRevision", "pageIndexSummaries", "pages"),
        ),
        output_schema=_object(
            {
                "catalogRevision": {"type": "integer", "minimum": 1},
                "sourceCount": {"type": "integer", "minimum": 0},
                "wikiPageCount": {"type": "integer", "minimum": 0},
            },
            ("catalogRevision", "sourceCount", "wikiPageCount"),
        ),
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        required_capabilities=frozenset({"knowledge.write"}),
        concurrency_safe=False,
        idempotent=False,
        retryable=False,
        timeout_ms=120_000,
        output_limit_bytes=_OUTPUT_LIMIT,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    ),
)


def knowledge_tool_definitions() -> tuple[ToolDefinition, ...]:
    return _DEFINITIONS


class KnowledgeToolExecutor:
    def __init__(
        self,
        *,
        workspace_id: str,
        preparation: KnowledgePreparationService,
        compiler: KnowledgeCompiler,
        semantic_compiler: LLMWikiCompiler | None = None,
    ) -> None:
        if not workspace_id:
            raise ValueError("knowledge tool workspace identity is invalid")
        self._workspace_id = workspace_id
        self._preparation = preparation
        self._compiler = compiler
        self._semantic_compiler = semantic_compiler
        handlers = (self._status, self._prepare, self._compile, self._publish)
        self._operations = MappingProxyType(
            {
                (definition.name, definition.version): (definition, handler)
                for definition, handler in zip(_DEFINITIONS, handlers, strict=True)
            }
        )

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return _DEFINITIONS

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        if call.workspace_id != self._workspace_id:
            return _failure(call, "knowledge_workspace_mismatch", "Knowledge tool belongs to another Workspace")
        operation = self._operations.get((call.name, call.version))
        if operation is None or operation[0].fingerprint != call.definition_fingerprint:
            return _failure(call, "knowledge_tool_unavailable", "Knowledge operation is not registered in this Run")
        try:
            arguments = thaw_json(call.arguments)
            if not isinstance(arguments, Mapping):
                raise ValueError("knowledge arguments must be an object")
            return await operation[1](call, arguments, cancellation)
        except OperationCancelled:
            raise
        except (
            KnowledgeCatalogError,
            KnowledgeCompilationError,
            KnowledgeDiscoveryError,
            KnowledgeObjectError,
            KnowledgePreparationError,
            KnowledgePublicationError,
            SemanticKnowledgeError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            return _failure(call, "knowledge_validation_failed", str(error))
        except OSError:
            return _failure(call, "knowledge_io_failed", "Knowledge storage operation failed safely")

    async def _status(
        self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken
    ) -> ToolResult:
        if arguments:
            raise ValueError("knowledge.status does not accept arguments")
        cancellation.checkpoint()
        status = await asyncio.to_thread(self._preparation.status)
        cancellation.checkpoint()
        data = {
            "catalogRevision": status.catalog_revision,
            "candidates": [
                {
                    "candidateId": item.candidate_id,
                    "state": item.state.value,
                    "source": _source_json(item.source),
                }
                for item in status.candidates
            ],
            "missing": [_source_json(item) for item in status.missing],
        }
        refs = tuple(item.source.relative_path for item in status.candidates)
        return _success(call, data, refs, "已检查 raw 知识来源", SideEffectKind.READ)

    async def _prepare(
        self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken
    ) -> ToolResult:
        candidate_ids = _string_tuple(arguments.get("candidateIds"), "candidateIds")
        removed_ids = _string_tuple(arguments.get("removedSourceIds", []), "removedSourceIds")
        preparation = await self._preparation.prepare(
            candidate_ids=candidate_ids,
            removed_source_ids=removed_ids,
            run_id=call.run_id,
            cancellation=cancellation,
        )
        data = _preparation_json(preparation)
        refs = tuple(item.source.relative_path for item in preparation.prepared_sources)
        return _success(call, data, refs, "已生成可恢复的页级证据与 PageIndex", SideEffectKind.FILE_WRITE)

    async def _publish(
        self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken
    ) -> ToolResult:
        cancellation.checkpoint()
        plan = _plan_from_json(arguments)
        # Publication is a short journaled critical section with no await point:
        # task cancellation cannot strand a background thread after the Tool
        # Kernel has reported cancellation or an unknown outcome.
        committed = self._compiler.publish(ingestion_id=plan.ingestion_id, plan=plan)
        data = {
            "catalogRevision": committed.revision,
            "sourceCount": len(committed.sources),
            "wikiPageCount": len(self._compiler.current_wiki_ids()),
        }
        refs = tuple(source.relative_path for source in committed.sources)
        return _success(call, data, refs, "已原子发布新的知识库 revision", SideEffectKind.FILE_RENAME)

    async def _compile(
        self, call: ToolCall, arguments: Mapping[str, Any], cancellation: CancellationToken
    ) -> ToolResult:
        if self._semantic_compiler is None:
            raise SemanticKnowledgeError("semantic LLM Wiki compiler is unavailable")
        expected = {"ingestionId", "baseRevision"}
        if set(arguments) != expected:
            raise ValueError("knowledge.compile arguments have an invalid shape")
        compiled = await self._semantic_compiler.compile(
            ingestion_id=_required_string(arguments, "ingestionId"),
            base_revision=_required_integer(arguments, "baseRevision"),
            run_id=call.run_id,
            cancellation=cancellation,
        )
        data = {
            "cachedInputTokens": compiled.cached_input_tokens,
            "cacheHit": compiled.cache_hit,
            "catalogRevision": compiled.catalog.revision,
            "inputTokens": compiled.input_tokens,
            "outputTokens": compiled.output_tokens,
            "sourceCount": len(compiled.catalog.sources),
            "wikiPageCount": compiled.page_count,
        }
        refs = tuple(source.relative_path for source in compiled.catalog.sources)
        return _success(call, data, refs, "已生成并原子发布引用校验后的 LLM Wiki", SideEffectKind.FILE_RENAME)


def _preparation_json(preparation: KnowledgePreparation) -> dict[str, object]:
    sources = []
    for prepared in preparation.prepared_sources:
        revision = prepared.source.content_hash.removeprefix("sha256:")[:20]
        build = prepared.parser_fingerprint.removeprefix("sha256:")[:12]
        base = f".offeragent/knowledge/objects/{prepared.source.source_id}/rev-{revision}-idx-{build}"
        nodes = []
        pages = {page.page_number: page for page in prepared.pages}
        for node in prepared.page_index.nodes:
            first = pages[node.start_page].text[:1800]
            last = "" if node.end_page == node.start_page else pages[node.end_page].text[:400]
            preview = first if not last else f"{first}\n…\n{last}"
            nodes.append(
                {
                    "nodeId": node.node_id,
                    "parentId": node.parent_id,
                    "depth": node.depth,
                    "title": node.title,
                    "summary": node.summary,
                    "startPage": node.start_page,
                    "endPage": node.end_page,
                    "children": list(node.children),
                    "evidencePreview": preview,
                }
            )
        sources.append(
            {
                "source": _source_json(prepared.source),
                "parserFingerprint": prepared.parser_fingerprint,
                "pageCount": len(prepared.pages),
                "evidencePagesPath": f"{base}/evidence/pages",
                "nodesPath": f"{base}/pageindex/nodes.jsonl",
                "nodes": nodes,
            }
        )
    return {
        "ingestionId": preparation.ingestion_id,
        "baseRevision": preparation.base_revision,
        "sources": sources,
    }


def _plan_from_json(value: Mapping[str, Any]) -> WikiPatchPlan:
    expected = {"ingestionId", "baseRevision", "pageIndexSummaries", "pages", "deletedWikiIds"}
    if not set(value) <= expected or not {"ingestionId", "baseRevision", "pageIndexSummaries", "pages"} <= set(value):
        raise ValueError("knowledge.publish arguments have an invalid shape")
    summaries = tuple(
        PageIndexSummaryPatch(
            _required_string(item, "sourceId"),
            _required_string(item, "sourceHash"),
            _required_string(item, "nodeId"),
            _required_string(item, "summary"),
        )
        for item in _object_list(value.get("pageIndexSummaries"), "pageIndexSummaries")
    )
    pages = tuple(_page_from_json(item) for item in _object_list(value.get("pages"), "pages"))
    return WikiPatchPlan(
        _required_string(value, "ingestionId"),
        _required_integer(value, "baseRevision"),
        summaries,
        pages,
        _string_tuple(value.get("deletedWikiIds", []), "deletedWikiIds"),
    )


def _page_from_json(value: Mapping[str, Any]) -> WikiPagePatch:
    expected = {"wikiId", "pageType", "path", "title", "aliases", "body", "citations", "relatedWikiIds"}
    if set(value) != expected:
        raise ValueError("knowledge Wiki page has an invalid shape")
    citations = tuple(
        WikiCitation(
            _required_string(item, "sourceId"),
            _required_string(item, "sourceHash"),
            _required_string(item, "nodeId"),
            _required_integer(item, "startPage"),
            _required_integer(item, "endPage"),
        )
        for item in _object_list(value.get("citations"), "citations")
    )
    return WikiPagePatch(
        _required_string(value, "wikiId"),
        WikiPageType(_required_string(value, "pageType")),
        _required_string(value, "path"),
        _required_string(value, "title"),
        _string_tuple(value.get("aliases"), "aliases"),
        _required_string(value, "body"),
        citations,
        _string_tuple(value.get("relatedWikiIds"), "relatedWikiIds"),
    )


def _object_list(value: object, name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{name} must be an array of objects")
    return tuple(value)


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{name} must be an array of non-empty strings")
    return tuple(value)


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _required_integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"{key} must be an integer")
    return item


def _source_json(source: object) -> dict[str, object]:
    from .models import SourceRecord

    if not isinstance(source, SourceRecord):
        raise TypeError("knowledge source projection requires SourceRecord")
    return {
        "sourceId": source.source_id,
        "path": source.relative_path,
        "contentHash": source.content_hash,
        "mediaType": source.media_type,
        "byteSize": source.byte_size,
    }


def _success(
    call: ToolCall,
    data: Mapping[str, Any],
    refs: tuple[str, ...],
    summary: str,
    effect_kind: SideEffectKind,
) -> ToolResult:
    effect = SideEffect(
        effect_kind,
        SideEffectState.OBSERVED if effect_kind is SideEffectKind.READ else SideEffectState.COMMITTED,
        f"workspace:{call.workspace_id}:knowledge",
        None,
        None,
        {"toolCallId": call.tool_call_id, "toolName": call.name, "argsHash": call.args_hash},
    )
    return ToolResult(
        call.tool_call_id,
        ToolResultStatus.SUCCEEDED,
        dict(data),
        summary,
        (),
        refs,
        (effect,),
        False,
        None,
        None,
        None,
        (),
        ("knowledge:prepared",) if call.name == "knowledge.prepare" else (),
    )


def _failure(call: ToolCall, code: str, message: str) -> ToolResult:
    return ToolResult(
        call.tool_call_id,
        ToolResultStatus.FAILED,
        None,
        message,
        (),
        (),
        (),
        False,
        None,
        None,
        ToolError(code, message, False, False, {}),
    )


__all__ = ["KNOWLEDGE_TOOL_VERSION", "KnowledgeToolExecutor", "knowledge_tool_definitions"]
