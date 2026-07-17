from __future__ import annotations

from offeragent_harness.protocol.content import (
    PinnedDocumentContextReference,
    PinnedSelectionContextReference,
)
from offeragent_harness.runtime.pinned_context import pinned_context_block


def test_pins_are_preferred_locators_not_a_search_whitelist_or_evidence() -> None:
    block = pinned_context_block(
        (
            PinnedDocumentContextReference(kind="document", path="notes/preferred.md"),
            PinnedSelectionContextReference(
                kind="selection",
                path="notes/range.md",
                line_start=4,
                line_end=8,
            ),
        )
    )

    assert block is not None
    text = block["text"]
    assert isinstance(text, str)
    assert "preferred source locators, not a whitelist" in text
    assert "notes/range.md:4-8" in text
    assert "A pin is not evidence" in text
    assert "vault.read" in text
    assert block["references"] == []
