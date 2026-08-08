"""LLM-assisted PageIndex construction with exact evidence anchors and caching."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
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
    ModelUsage,
    TraceContext,
    thaw_json,
)
from offeragent_harness.ports import CancellationToken, IdGenerator, ModelGateway

from .models import PageEvidence, PageIndexNode, PageIndexTree

SEMANTIC_PAGEINDEX_PROMPT_VERSION = "semantic-pageindex-v3"


class SemanticKnowledgeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KnowledgeInferenceLimits:
    max_input_tokens: int = 1_100_000
    max_output_tokens: int = 100_000

    def __post_init__(self) -> None:
        if min(self.max_input_tokens, self.max_output_tokens) < 1:
            raise ValueError("knowledge inference limits must be positive")


class KnowledgeInferenceBudget:
    """In-memory hard guard; evaluation adds a second process-level cost guard."""

    def __init__(self, limits: KnowledgeInferenceLimits | None = None) -> None:
        self.limits = limits or KnowledgeInferenceLimits()
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_input_tokens = 0

    def ensure_estimate(self, estimated_input_tokens: int, maximum_output_tokens: int) -> None:
        if estimated_input_tokens < 1 or maximum_output_tokens < 1:
            raise ValueError("knowledge inference estimates must be positive")
        if self.input_tokens + estimated_input_tokens > self.limits.max_input_tokens:
            raise SemanticKnowledgeError("knowledge inference input Token budget is exhausted")
        if self.output_tokens + maximum_output_tokens > self.limits.max_output_tokens:
            raise SemanticKnowledgeError("knowledge inference output Token budget is exhausted")

    def charge(self, usage: ModelUsage) -> None:
        if self.input_tokens + usage.input_tokens > self.limits.max_input_tokens:
            raise SemanticKnowledgeError("knowledge inference exceeded its input Token budget")
        if self.output_tokens + usage.output_tokens > self.limits.max_output_tokens:
            raise SemanticKnowledgeError("knowledge inference exceeded its output Token budget")
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cached_input_tokens += usage.cached_input_tokens


class StructuredInferenceCache:
    """Canonical content-hash cache; corrupt entries fail closed."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("structured inference cache root must be absolute")
        self._root = root

    def get(self, key: str) -> Mapping[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SemanticKnowledgeError("structured inference cache entry is unreadable") from error
        if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
            raise SemanticKnowledgeError("structured inference cache entry is not canonical")
        return cast(Mapping[str, Any], value)

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        payload = canonical_json_bytes(value)
        path = self._path(key)
        if path.exists():
            if path.read_bytes() != payload:
                raise SemanticKnowledgeError("structured inference cache identity collision")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix="inference-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8", errors="strict")).hexdigest()
        return self._root / digest[:2] / f"{digest}.json"


@dataclass(frozen=True, slots=True)
class SemanticPageIndexPolicy:
    pages_per_window: int = 12
    maximum_page_characters: int = 4_000
    maximum_request_tokens: int = 200_000
    maximum_output_tokens: int = 8_192
    maximum_sections_per_window: int = 64
    maximum_validation_attempts: int = 2

    def __post_init__(self) -> None:
        if (
            min(
                self.pages_per_window,
                self.maximum_page_characters,
                self.maximum_request_tokens,
                self.maximum_output_tokens,
                self.maximum_sections_per_window,
                self.maximum_validation_attempts,
            )
            < 1
        ):
            raise ValueError("semantic PageIndex policy limits must be positive")


class SemanticPageIndexBuilder:
    """Build a three-level document tree from model-proposed, quote-grounded sections."""

    def __init__(
        self,
        *,
        gateway_factory: Callable[[str], ModelGateway],
        workspace_id: str,
        model: str,
        cache: StructuredInferenceCache,
        ids: IdGenerator,
        budget: KnowledgeInferenceBudget,
        policy: SemanticPageIndexPolicy | None = None,
    ) -> None:
        if not model or not workspace_id:
            raise ValueError("semantic PageIndex model and Workspace must not be empty")
        self._gateway_factory = gateway_factory
        self._workspace_id = workspace_id
        self._model = model
        self._cache = cache
        self._ids = ids
        self._budget = budget
        self._policy = policy or SemanticPageIndexPolicy()
        self._validator = Draft202012Validator(_PAGEINDEX_OUTPUT_SCHEMA)

    @property
    def fingerprint(self) -> str:
        return canonical_json_sha256(
            {
                "model": self._model,
                "policy": {
                    "maximumPageCharacters": self._policy.maximum_page_characters,
                    "maximumSectionsPerWindow": self._policy.maximum_sections_per_window,
                    "maximumValidationAttempts": self._policy.maximum_validation_attempts,
                    "pagesPerWindow": self._policy.pages_per_window,
                },
                "promptHash": _semantic_prompt_hash(),
                "promptVersion": SEMANTIC_PAGEINDEX_PROMPT_VERSION,
            }
        )

    async def build(
        self,
        *,
        source_id: str,
        source_hash: str,
        title: str,
        pages: tuple[PageEvidence, ...],
        run_id: str,
        cancellation: CancellationToken,
    ) -> PageIndexTree:
        if not pages or [page.page_number for page in pages] != list(range(1, len(pages) + 1)):
            raise ValueError("semantic PageIndex requires contiguous one-based pages")
        groups: list[tuple[int, int, str, tuple[dict[str, Any], ...]]] = []
        for start in range(1, len(pages) + 1, self._policy.pages_per_window):
            end = min(len(pages), start + self._policy.pages_per_window - 1)
            window = pages[start - 1 : end]
            output: Mapping[str, Any] | None = None
            feedback: str | None = None
            cache_hit = False
            for attempt in range(1, self._policy.maximum_validation_attempts + 1):
                output, cache_key, cache_hit = await self._window(
                    source_id=source_id,
                    source_hash=source_hash,
                    title=title,
                    pages=window,
                    run_id=run_id,
                    validation_attempt=attempt,
                    validation_feedback=feedback,
                    previous_output=output,
                    cancellation=cancellation,
                )
                try:
                    sections = _validated_sections(
                        output,
                        anchors=_anchor_catalog(window, self._policy.maximum_page_characters),
                        page_numbers=frozenset(page.page_number for page in window),
                        maximum_sections=self._policy.maximum_sections_per_window,
                    )
                    break
                except SemanticKnowledgeError as error:
                    if cache_hit or attempt == self._policy.maximum_validation_attempts:
                        raise
                    feedback = str(error)
            else:  # pragma: no cover - the bounded loop either breaks or raises
                raise SemanticKnowledgeError("semantic PageIndex validation attempts were exhausted")
            if not cache_hit:
                assert output is not None
                self._cache.put(cache_key, output)
            group_summary = " ".join(cast(str, section["summary"]) for section in sections)
            groups.append((start, end, _bounded(group_summary, 2_000), sections))
        root_id = "node-root"
        root_children: list[str] = []
        nodes: list[PageIndexNode] = []
        ordinal = 0
        for group_index, (start, end, group_summary, sections) in enumerate(groups, start=1):
            ordinal += 1
            group_id = f"node-{ordinal:04d}"
            root_children.append(group_id)
            children: list[str] = []
            section_nodes: list[PageIndexNode] = []
            for section in sections:
                ordinal += 1
                node_id = f"node-{ordinal:04d}"
                children.append(node_id)
                section_nodes.append(
                    PageIndexNode(
                        node_id,
                        group_id,
                        2,
                        cast(str, section["title"]),
                        cast(str, section["summary"]),
                        cast(int, section["startPage"]),
                        cast(int, section["endPage"]),
                        (),
                    )
                )
            nodes.append(
                PageIndexNode(
                    group_id,
                    root_id,
                    1,
                    f"Part {group_index}: pages {start}-{end}",
                    group_summary,
                    start,
                    end,
                    tuple(children),
                )
            )
            nodes.extend(section_nodes)
        root_summary = _bounded(" ".join(node.summary for node in nodes if node.depth == 1), 2_000)
        root = PageIndexNode(
            root_id,
            None,
            0,
            title.strip() or "Document",
            root_summary,
            1,
            len(pages),
            tuple(root_children),
        )
        return PageIndexTree(source_id, source_hash, len(pages), root_id, (root, *nodes))

    async def _window(
        self,
        *,
        source_id: str,
        source_hash: str,
        title: str,
        pages: Sequence[PageEvidence],
        run_id: str,
        validation_attempt: int,
        validation_feedback: str | None,
        previous_output: Mapping[str, Any] | None,
        cancellation: CancellationToken,
    ) -> tuple[Mapping[str, Any], str, bool]:
        evidence = [_page_with_anchors(page, self._policy.maximum_page_characters) for page in pages]
        key_value = {
            "evidence": evidence,
            "model": self._model,
            "promptHash": _semantic_prompt_hash(),
            "promptVersion": SEMANTIC_PAGEINDEX_PROMPT_VERSION,
            "sourceHash": source_hash,
            "sourceId": source_id,
        }
        cache_key = canonical_json_sha256(key_value)
        cached = self._cache.get(cache_key)
        if cached is not None and validation_feedback is None:
            _raise_schema_errors(self._validator, cached, "cached semantic PageIndex")
            return cached, cache_key, True
        request_data: dict[str, Any] = {"documentTitle": title, "pages": evidence}
        if validation_feedback is not None:
            request_data["invalidOutput"] = previous_output
            request_data["validationFeedback"] = validation_feedback
            request_data["repairInstruction"] = (
                "Return a complete corrected object. Select only anchorId values present on the supplied page; "
                "do not invent or move anchors."
            )
        request_payload = canonical_json_bytes(request_data).decode("utf-8")
        request = ModelRequest(
            request_id=self._ids.new_id("model-request"),
            model=self._model,
            purpose=ModelPurpose.GROUNDING,
            messages=(
                ModelMessage(ModelRole.SYSTEM, (ModelContentBlock.text(_PAGEINDEX_PROMPT),)),
                ModelMessage(ModelRole.USER, (ModelContentBlock.text(request_payload),)),
            ),
            output_mode=ModelOutputMode.JSON,
            output_schema=_PAGEINDEX_OUTPUT_SCHEMA,
            max_output_tokens=self._policy.maximum_output_tokens,
            reasoning_effort="none",
            temperature=0.0,
            seed=None,
            trace_context=TraceContext(self._ids.new_id("trace")),
            metadata={
                "cacheKey": cache_key,
                "promptHash": _semantic_prompt_hash(),
                "promptVersion": SEMANTIC_PAGEINDEX_PROMPT_VERSION,
                "runId": run_id,
                "sourceId": source_id,
                "validationAttempt": validation_attempt,
                "workspaceId": self._workspace_id,
            },
        )
        estimate = _estimate_request_tokens(request)
        if estimate > self._policy.maximum_request_tokens:
            raise SemanticKnowledgeError("semantic PageIndex window exceeds its request Token ceiling")
        self._budget.ensure_estimate(estimate, self._policy.maximum_output_tokens)
        response = await collect_structured_response(self._gateway_factory(self._model), request, cancellation)
        self._budget.charge(response.usage)
        output = cast(dict[str, Any], thaw_json(response.output))
        _raise_schema_errors(self._validator, output, "semantic PageIndex")
        return output, cache_key, False


def combined_parser_fingerprint(parser_fingerprint: str, pageindex_fingerprint: str | None) -> str:
    return canonical_json_sha256(
        {
            "pageIndex": pageindex_fingerprint or "structural-pageindex-v1",
            "parser": parser_fingerprint,
            "schemaVersion": 1,
        }
    )


def _validated_sections(
    output: Mapping[str, Any],
    *,
    anchors: Mapping[str, tuple[int, str]],
    page_numbers: frozenset[int],
    maximum_sections: int,
) -> tuple[dict[str, Any], ...]:
    raw = output.get("sections")
    if not isinstance(raw, list) or not raw or len(raw) > maximum_sections:
        raise SemanticKnowledgeError("semantic PageIndex returned no bounded sections")
    sections: list[dict[str, Any]] = []
    if not page_numbers:
        raise SemanticKnowledgeError("semantic PageIndex evidence window has no pages")
    if not anchors:
        raise SemanticKnowledgeError("semantic PageIndex evidence window has no usable anchors")
    previous_start = min(page_numbers) - 1
    identities: set[tuple[int, int, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise SemanticKnowledgeError("semantic PageIndex section is not an object")
        start = item.get("startPage")
        end = item.get("endPage")
        title = item.get("title")
        if (
            type(start) is not int
            or type(end) is not int
            or start > end
            or start < previous_start
            or not isinstance(title, str)
        ):
            raise SemanticKnowledgeError("semantic PageIndex sections are unordered or duplicated")
        identity = (start, end, title.strip().casefold())
        if identity in identities:
            raise SemanticKnowledgeError("semantic PageIndex sections are unordered or duplicated")
        if start not in page_numbers or end not in page_numbers:
            raise SemanticKnowledgeError("semantic PageIndex section is outside its evidence window")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise SemanticKnowledgeError("semantic PageIndex section lacks evidence anchors")
        for anchor in evidence:
            if not isinstance(anchor, Mapping) or type(anchor.get("page")) is not int:
                raise SemanticKnowledgeError("semantic PageIndex evidence anchor is invalid")
            page = cast(int, anchor["page"])
            anchor_id = anchor.get("anchorId")
            grounded = anchors.get(anchor_id) if isinstance(anchor_id, str) else None
            if grounded is None or grounded[0] != page or page < start or page > end:
                raise SemanticKnowledgeError("semantic PageIndex evidence anchor is not present on its cited page")
        sections.append(dict(item))
        identities.add(identity)
        previous_start = start
    return tuple(sections)


def _raise_schema_errors(validator: Draft202012Validator, value: Mapping[str, Any], label: str) -> None:
    errors = sorted(error.message for error in validator.iter_errors(value))
    if errors:
        raise SemanticKnowledgeError(f"{label} output failed Schema validation: {errors[0]}")


def _bounded(value: str, maximum: int) -> str:
    normalized = value.strip()
    if len(normalized) <= maximum:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8", errors="strict")).hexdigest()[:12]
    return f"{normalized[: maximum - 32]} ... [sha256:{digest}]"


def _page_with_anchors(page: PageEvidence, maximum_characters: int) -> dict[str, Any]:
    return {
        "anchors": [
            {"anchorId": anchor_id, "text": text}
            for anchor_id, (_, text) in _anchor_catalog((page,), maximum_characters).items()
        ],
        "page": page.page_number,
        "pageHash": page.content_hash,
    }


def _anchor_catalog(pages: Sequence[PageEvidence], maximum_characters: int) -> dict[str, tuple[int, str]]:
    anchors: dict[str, tuple[int, str]] = {}
    for page in pages:
        text = page.text[:maximum_characters]
        cursor = 0
        ordinal = 0
        while cursor < len(text):
            end = min(len(text), cursor + 512)
            if end < len(text):
                boundary = max(text.rfind("\n", cursor + 128, end), text.rfind(" ", cursor + 128, end))
                if boundary > cursor:
                    end = boundary
            fragment = text[cursor:end].strip()
            cursor = max(end, cursor + 1)
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if len(fragment) < 8:
                continue
            ordinal += 1
            anchors[f"p{page.page_number:04d}-a{ordinal:03d}"] = (page.page_number, fragment)
    return anchors


def _estimate_request_tokens(request: ModelRequest) -> int:
    byte_length = sum(
        len(canonical_json_bytes(thaw_json(block.data))) for message in request.messages for block in message.content
    )
    return max(1, math.ceil(byte_length / 3))


_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "minimum": 1},
        "anchorId": {"type": "string", "pattern": "^p[0-9]{4}-a[0-9]{3}$"},
    },
    "required": ["page", "anchorId"],
    "additionalProperties": False,
}
_SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "endPage": {"type": "integer", "minimum": 1},
        "evidence": {"type": "array", "items": _EVIDENCE_SCHEMA, "minItems": 1, "maxItems": 8},
        "startPage": {"type": "integer", "minimum": 1},
        "summary": {"type": "string", "minLength": 1, "maxLength": 2_000},
        "title": {"type": "string", "minLength": 1, "maxLength": 240},
    },
    "required": ["title", "summary", "startPage", "endPage", "evidence"],
    "additionalProperties": False,
}
_PAGEINDEX_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"sections": {"type": "array", "items": _SECTION_SCHEMA, "minItems": 1, "maxItems": 64}},
    "required": ["sections"],
    "additionalProperties": False,
}

_PAGEINDEX_PROMPT = """You build a semantic PageIndex for one bounded document window.
Return sections ordered by non-decreasing start page. Sections may overlap when distinct topics share a page.
Every section must select at least one supplied anchorId from a page inside its inclusive page range.
Do not invent or move anchors, headings, claims, or page numbers. Summaries must describe only facts supported by
the selected anchor texts. Preserve technical names exactly. Output JSON only."""


def _semantic_prompt_hash() -> str:
    return canonical_json_sha256({"prompt": _PAGEINDEX_PROMPT, "schema": _PAGEINDEX_OUTPUT_SCHEMA})


__all__ = [
    "SEMANTIC_PAGEINDEX_PROMPT_VERSION",
    "KnowledgeInferenceBudget",
    "KnowledgeInferenceLimits",
    "SemanticKnowledgeError",
    "SemanticPageIndexBuilder",
    "SemanticPageIndexPolicy",
    "StructuredInferenceCache",
    "combined_parser_fingerprint",
]
