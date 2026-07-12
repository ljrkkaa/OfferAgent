"""Local knowledge retrieval boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json

from .cancellation import CancellationToken


@dataclass(frozen=True)
class KnowledgeQuery:
    workspace_id: str
    query: str
    top_k: int
    minimum_revision: int
    filters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.top_k <= 0 or self.minimum_revision < 0:
            raise ValueError("top_k must be positive and revision cannot be negative")
        filters = freeze_json(self.filters)
        if not isinstance(filters, FrozenJsonObject):
            raise TypeError("knowledge filters must be a JSON object")
        object.__setattr__(self, "filters", filters)


@dataclass(frozen=True)
class KnowledgeHit:
    source_ref: str
    relative_path: str
    preview: str
    heading: str | None
    line_start: int | None
    line_end: int | None
    score: float
    score_breakdown: Mapping[str, Any]
    content_hash: str
    index_revision: int
    stale: bool

    def __post_init__(self) -> None:
        breakdown = freeze_json(self.score_breakdown)
        if not isinstance(breakdown, FrozenJsonObject):
            raise TypeError("score_breakdown must be a JSON object")
        object.__setattr__(self, "score_breakdown", breakdown)


@runtime_checkable
class KnowledgeSource(Protocol):
    async def search(self, query: KnowledgeQuery, cancellation: CancellationToken) -> tuple[KnowledgeHit, ...]: ...


__all__ = ["KnowledgeHit", "KnowledgeQuery", "KnowledgeSource"]
