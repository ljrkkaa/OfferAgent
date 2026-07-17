"""Turn-scoped preferred source locators for model context construction."""

from __future__ import annotations

from collections.abc import Sequence

from offeragent_harness.protocol.content import (
    PinnedContextReference,
)


def pinned_context_block(references: Sequence[PinnedContextReference]) -> dict[str, object] | None:
    """Represent validated pins as user-layer guidance, never as evidence or policy."""

    if not references:
        return None
    if len(references) > 8:
        raise ValueError("pinned context cannot contain more than eight references")
    return {
        "type": "pinnedContext",
        "references": [reference.to_wire() for reference in references],
    }


__all__ = ["pinned_context_block"]
