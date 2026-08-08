from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")


class SourceState(str, Enum):
    NEW = "new"
    CHANGED = "changed"
    OUTDATED = "outdated"
    UNCHANGED = "unchanged"
    MISSING = "missing"


class WikiPageType(str, Enum):
    SUMMARY = "summary"
    CONCEPT = "concept"
    ENTITY = "entity"


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source_id: str
    relative_path: str
    content_hash: str
    media_type: str
    byte_size: int

    def __post_init__(self) -> None:
        if _IDENTIFIER.fullmatch(self.source_id) is None:
            raise ValueError("source_id is not canonical")
        if not self.relative_path or "\\" in self.relative_path or self.relative_path.startswith("/"):
            raise ValueError("relative_path must be a non-empty POSIX path")
        if any(part in {"", ".", ".."} for part in self.relative_path.split("/")):
            raise ValueError("relative_path contains an unsafe segment")
        if _SHA256.fullmatch(self.content_hash) is None:
            raise ValueError("content_hash is not canonical")
        if "/" not in self.media_type or self.byte_size < 0:
            raise ValueError("source media metadata is invalid")


@dataclass(frozen=True, slots=True)
class KnowledgeCandidate:
    candidate_id: str
    catalog_revision: int
    state: SourceState
    source: SourceRecord

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.candidate_id) is None:
            raise ValueError("candidate_id is not canonical")
        if self.catalog_revision < 0:
            raise ValueError("catalog_revision cannot be negative")


@dataclass(frozen=True, slots=True)
class KnowledgeStatus:
    catalog_revision: int
    candidates: tuple[KnowledgeCandidate, ...]
    missing: tuple[SourceRecord, ...]

    def __post_init__(self) -> None:
        if self.catalog_revision < 0:
            raise ValueError("catalog_revision cannot be negative")
        paths = [item.source.relative_path.casefold() for item in self.candidates]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("knowledge candidates must be uniquely path-sorted")


@dataclass(frozen=True, slots=True)
class PageEvidence:
    page_number: int
    text: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.page_number < 1 or _SHA256.fullmatch(self.content_hash) is None:
            raise ValueError("page evidence identity is invalid")
        actual = f"sha256:{hashlib.sha256(self.text.encode('utf-8', errors='strict')).hexdigest()}"
        if self.content_hash != actual:
            raise ValueError("page evidence content hash does not match its text")


@dataclass(frozen=True, slots=True)
class PageIndexNode:
    node_id: str
    parent_id: str | None
    depth: int
    title: str
    summary: str
    start_page: int
    end_page: int
    children: tuple[str, ...]

    def __post_init__(self) -> None:
        if _IDENTIFIER.fullmatch(self.node_id) is None:
            raise ValueError("node_id is not canonical")
        if self.parent_id is not None and _IDENTIFIER.fullmatch(self.parent_id) is None:
            raise ValueError("parent_id is not canonical")
        if self.depth < 0 or not self.title.strip() or self.start_page < 1 or self.end_page < self.start_page:
            raise ValueError("PageIndex node geometry is invalid")
        if len(self.children) != len(set(self.children)):
            raise ValueError("PageIndex node children must be unique")


@dataclass(frozen=True, slots=True)
class PageIndexTree:
    source_id: str
    source_hash: str
    page_count: int
    root_id: str
    nodes: tuple[PageIndexNode, ...]

    def __post_init__(self) -> None:
        if _IDENTIFIER.fullmatch(self.source_id) is None or _SHA256.fullmatch(self.source_hash) is None:
            raise ValueError("PageIndex source identity is invalid")
        if self.page_count < 1 or not self.nodes:
            raise ValueError("PageIndex tree cannot be empty")
        by_id = {node.node_id: node for node in self.nodes}
        if len(by_id) != len(self.nodes) or self.root_id not in by_id:
            raise ValueError("PageIndex node identities are invalid")
        root = by_id[self.root_id]
        if root.parent_id is not None or root.depth != 0 or (root.start_page, root.end_page) != (1, self.page_count):
            raise ValueError("PageIndex root does not cover the document")
        for node in self.nodes:
            if node.end_page > self.page_count:
                raise ValueError("PageIndex node exceeds the document")
            if node.parent_id is None:
                if node.node_id != self.root_id:
                    raise ValueError("PageIndex has multiple roots")
                continue
            parent = by_id.get(node.parent_id)
            if parent is None or node.node_id not in parent.children:
                raise ValueError("PageIndex parent/child relation is inconsistent")
            if node.depth != parent.depth + 1 or not (
                parent.start_page <= node.start_page <= node.end_page <= parent.end_page
            ):
                raise ValueError("PageIndex child is outside its parent")
        seen: set[str] = set()
        active: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in active:
                raise ValueError("PageIndex contains a cycle")
            if node_id in seen:
                return
            active.add(node_id)
            for child_id in by_id[node_id].children:
                if child_id not in by_id:
                    raise ValueError("PageIndex references an unknown child")
                visit(child_id)
            active.remove(node_id)
            seen.add(node_id)

        visit(self.root_id)
        if seen != set(by_id):
            raise ValueError("PageIndex contains unreachable nodes")


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    revision: int
    sources: tuple[SourceRecord, ...]

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("catalog revision cannot be negative")
        source_ids = [source.source_id for source in self.sources]
        paths = [source.relative_path.casefold() for source in self.sources]
        if len(source_ids) != len(set(source_ids)) or len(paths) != len(set(paths)):
            raise ValueError("catalog sources must have unique identities and paths")
        if paths != sorted(paths):
            raise ValueError("catalog sources must be path-sorted")


@dataclass(frozen=True, slots=True)
class WikiCitation:
    source_id: str
    source_hash: str
    node_id: str
    start_page: int
    end_page: int

    def __post_init__(self) -> None:
        if _IDENTIFIER.fullmatch(self.source_id) is None or _SHA256.fullmatch(self.source_hash) is None:
            raise ValueError("wiki citation source identity is invalid")
        if _IDENTIFIER.fullmatch(self.node_id) is None:
            raise ValueError("wiki citation node identity is invalid")
        if self.start_page < 1 or self.end_page < self.start_page:
            raise ValueError("wiki citation page range is invalid")


@dataclass(frozen=True, slots=True)
class PageIndexSummaryPatch:
    source_id: str
    source_hash: str
    node_id: str
    summary: str

    def __post_init__(self) -> None:
        if _IDENTIFIER.fullmatch(self.source_id) is None or _SHA256.fullmatch(self.source_hash) is None:
            raise ValueError("PageIndex summary source identity is invalid")
        if _IDENTIFIER.fullmatch(self.node_id) is None:
            raise ValueError("PageIndex summary node identity is invalid")
        if not self.summary.strip() or len(self.summary) > 2_000:
            raise ValueError("PageIndex summary must be non-empty and bounded")


@dataclass(frozen=True, slots=True)
class WikiPagePatch:
    wiki_id: str
    page_type: WikiPageType
    path: str
    title: str
    aliases: tuple[str, ...]
    body: str
    citations: tuple[WikiCitation, ...]
    related_wiki_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if _IDENTIFIER.fullmatch(self.wiki_id) is None:
            raise ValueError("wiki_id is not canonical")
        path = PurePosixPath(self.path)
        expected_root = {
            WikiPageType.SUMMARY: "summaries",
            WikiPageType.CONCEPT: "concepts",
            WikiPageType.ENTITY: "entities",
        }[self.page_type]
        if (
            path.is_absolute()
            or path.suffix.casefold() != ".md"
            or len(path.parts) != 2
            or path.parts[0] != expected_root
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("wiki page path is outside its generated page type root")
        if not self.title.strip() or not self.body.strip():
            raise ValueError("wiki page title and body cannot be empty")
        normalized_aliases = tuple(alias.strip().casefold() for alias in self.aliases)
        if any(not alias for alias in normalized_aliases) or len(normalized_aliases) != len(set(normalized_aliases)):
            raise ValueError("wiki aliases must be non-empty and unique")
        if not self.citations:
            raise ValueError("every wiki page requires at least one structured citation")
        if len(self.related_wiki_ids) != len(set(self.related_wiki_ids)):
            raise ValueError("related wiki identities must be unique")
        if any(_IDENTIFIER.fullmatch(item) is None or item == self.wiki_id for item in self.related_wiki_ids):
            raise ValueError("related wiki identity is invalid")


@dataclass(frozen=True, slots=True)
class WikiPatchPlan:
    ingestion_id: str
    base_revision: int
    page_index_summaries: tuple[PageIndexSummaryPatch, ...]
    pages: tuple[WikiPagePatch, ...]
    deleted_wiki_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _IDENTIFIER.fullmatch(self.ingestion_id) is None or self.base_revision < 0:
            raise ValueError("wiki patch plan identity is invalid")
        wiki_ids = [page.wiki_id for page in self.pages]
        paths = [page.path.casefold() for page in self.pages]
        if (
            (not self.pages and not self.deleted_wiki_ids)
            or len(wiki_ids) != len(set(wiki_ids))
            or len(paths) != len(set(paths))
        ):
            raise ValueError("wiki patch must contain changes with unique identities and paths")
        if (
            len(self.deleted_wiki_ids) != len(set(self.deleted_wiki_ids))
            or any(_IDENTIFIER.fullmatch(item) is None for item in self.deleted_wiki_ids)
            or set(wiki_ids) & set(self.deleted_wiki_ids)
        ):
            raise ValueError("wiki patch deletion identities are invalid")
        summary_keys = [(item.source_id, item.source_hash, item.node_id) for item in self.page_index_summaries]
        if len(summary_keys) != len(set(summary_keys)):
            raise ValueError("PageIndex summary patches must have unique identities")
