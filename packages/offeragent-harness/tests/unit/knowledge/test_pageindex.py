from __future__ import annotations

import hashlib

from offeragent_harness.knowledge import PageEvidence, StructuralPageIndexBuilder


def _page(number: int, text: str) -> PageEvidence:
    digest = hashlib.sha256(text.encode()).hexdigest()
    return PageEvidence(number, text, f"sha256:{digest}")


def test_pageindex_uses_document_structure_not_topic_vocabulary() -> None:
    pages = (
        _page(1, "# Zeta mechanism\nintro"),
        _page(2, "## 1.1 Novel component\ndetail"),
        _page(3, "# Omega result\nresult"),
    )
    tree = StructuralPageIndexBuilder().build(
        source_id="src-1234567890abcdef",
        source_hash="sha256:" + "a" * 64,
        title="Randomized document",
        pages=pages,
    )
    assert [node.title for node in tree.nodes] == [
        "Randomized document",
        "Zeta mechanism",
        "1.1 Novel component",
        "Omega result",
    ]
    assert tree.nodes[1].end_page == 2
    assert tree.nodes[2].parent_id == tree.nodes[1].node_id


def test_pageindex_fallback_groups_pages_without_semantic_rules() -> None:
    pages = tuple(_page(index, f"opaque payload {index}") for index in range(1, 7))
    tree = StructuralPageIndexBuilder(fallback_group_pages=2).build(
        source_id="src-1234567890abcdef",
        source_hash="sha256:" + "b" * 64,
        title="Opaque",
        pages=pages,
    )
    assert [(node.start_page, node.end_page) for node in tree.nodes[1:]] == [(1, 2), (3, 4), (5, 6)]


def test_pageindex_expands_parent_for_child_on_sibling_boundary_page() -> None:
    pages = (
        _page(1, "1 Root"),
        _page(2, "2 Parent\n2.1 First child"),
        _page(3, "2.2 Boundary child\n3 Sibling"),
    )

    tree = StructuralPageIndexBuilder().build(
        source_id="src-1234567890abcdef",
        source_hash="sha256:" + "c" * 64,
        title="Boundary layout",
        pages=pages,
    )

    parent = next(node for node in tree.nodes if node.title == "2 Parent")
    boundary_child = next(node for node in tree.nodes if node.title == "2.2 Boundary child")
    sibling = next(node for node in tree.nodes if node.title == "3 Sibling")
    assert parent.end_page == 3
    assert boundary_child.parent_id == parent.node_id
    assert sibling.start_page == 3


def test_pageindex_rejects_numeric_table_rows_as_section_headings() -> None:
    pages = (_page(1, "1 512 512 5.29 24.9\n1 Introduction\nbody"),)

    tree = StructuralPageIndexBuilder().build(
        source_id="src-1234567890abcdef",
        source_hash="sha256:" + "d" * 64,
        title="Tabular layout",
        pages=pages,
    )

    assert [node.title for node in tree.nodes] == ["Tabular layout", "1 Introduction"]
