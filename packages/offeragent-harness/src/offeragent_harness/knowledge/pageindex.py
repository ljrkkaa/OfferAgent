from __future__ import annotations

import re
from dataclasses import dataclass

from .models import PageEvidence, PageIndexNode, PageIndexTree

_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+){0,5})[.)]?\s+(.+?)\s*$")
_HEADING_TOKEN = re.compile(r"[A-Za-z]+|\d+(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class _Heading:
    level: int
    title: str
    page: int


class StructuralPageIndexBuilder:
    """Build a vocabulary-independent document tree from observable structure.

    The builder deliberately does not infer semantic topics. It consumes Markdown
    heading markers and numbered section syntax, then creates bounded page groups
    when a document has no headings. LLM-authored summaries are a later,
    independently validated compilation step.
    """

    def __init__(self, *, fallback_group_pages: int = 4, maximum_title_characters: int = 160) -> None:
        if fallback_group_pages < 1 or maximum_title_characters < 16:
            raise ValueError("PageIndex builder limits are invalid")
        self._fallback_group_pages = fallback_group_pages
        self._maximum_title_characters = maximum_title_characters

    def build(
        self,
        *,
        source_id: str,
        source_hash: str,
        title: str,
        pages: tuple[PageEvidence, ...],
    ) -> PageIndexTree:
        if not pages or [page.page_number for page in pages] != list(range(1, len(pages) + 1)):
            raise ValueError("PageIndex requires contiguous one-based pages")
        headings = self._headings(pages)
        specs = self._heading_specs(headings, len(pages)) if headings else self._fallback_specs(len(pages))
        root_id = "node-root"
        mutable_children: dict[str, list[str]] = {root_id: []}
        nodes: list[PageIndexNode] = []
        stack: list[tuple[int, str, int, int, str]] = []
        for ordinal, (level, node_title, start_page, end_page) in enumerate(specs, start=1):
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent_id = stack[-1][1] if stack else root_id
            depth = len(stack) + 1
            node_id = f"node-{ordinal:04d}"
            mutable_children.setdefault(parent_id, []).append(node_id)
            mutable_children[node_id] = []
            nodes.append(PageIndexNode(node_id, parent_id, depth, node_title, "", start_page, end_page, ()))
            stack.append((level, node_id, start_page, end_page, node_title))
        expanded_ends = {node.node_id: node.end_page for node in nodes}
        for node in reversed(nodes):
            if mutable_children[node.node_id]:
                expanded_ends[node.node_id] = max(
                    expanded_ends[node.node_id],
                    *(expanded_ends[child] for child in mutable_children[node.node_id]),
                )
        finalized = tuple(
            PageIndexNode(
                node.node_id,
                node.parent_id,
                node.depth,
                node.title,
                node.summary,
                node.start_page,
                expanded_ends[node.node_id],
                tuple(mutable_children[node.node_id]),
            )
            for node in nodes
        )
        root = PageIndexNode(
            root_id, None, 0, title.strip() or "Document", "", 1, len(pages), tuple(mutable_children[root_id])
        )
        return PageIndexTree(source_id, source_hash, len(pages), root_id, (root, *finalized))

    def _headings(self, pages: tuple[PageEvidence, ...]) -> tuple[_Heading, ...]:
        found: list[_Heading] = []
        for page in pages:
            for raw_line in page.text.splitlines():
                line = " ".join(raw_line.strip().split())
                if not line or len(line) > self._maximum_title_characters:
                    continue
                markdown = _MARKDOWN_HEADING.fullmatch(line)
                if markdown:
                    found.append(_Heading(len(markdown.group(1)), markdown.group(2), page.page_number))
                    continue
                numbered = _NUMBERED_HEADING.fullmatch(line)
                if numbered and self._plausible_numbered_heading(numbered.group(1), numbered.group(2)):
                    level = numbered.group(1).count(".") + 1
                    found.append(_Heading(level, f"{numbered.group(1)} {numbered.group(2)}", page.page_number))
        return tuple(found)

    @staticmethod
    def _plausible_numbered_heading(number: str, title: str) -> bool:
        components = number.split(".")
        if any(int(component) > 99 for component in components):
            return False
        tokens = _HEADING_TOKEN.findall(title)
        if not tokens or not any(token.isalpha() for token in tokens):
            return False
        numeric_count = len([token for token in tokens if token[0].isdigit()])
        return numeric_count * 2 <= len(tokens)

    @staticmethod
    def _heading_specs(headings: tuple[_Heading, ...], page_count: int) -> tuple[tuple[int, str, int, int], ...]:
        specs: list[tuple[int, str, int, int]] = []
        for index, heading in enumerate(headings):
            next_page = page_count + 1
            for later in headings[index + 1 :]:
                if later.level <= heading.level:
                    next_page = later.page
                    break
            end_page = max(heading.page, next_page - 1)
            specs.append((heading.level, heading.title, heading.page, min(page_count, end_page)))
        return tuple(specs)

    def _fallback_specs(self, page_count: int) -> tuple[tuple[int, str, int, int], ...]:
        return tuple(
            (
                1,
                f"Pages {start}-{min(page_count, start + self._fallback_group_pages - 1)}",
                start,
                min(page_count, start + self._fallback_group_pages - 1),
            )
            for start in range(1, page_count + 1, self._fallback_group_pages)
        )
