"""Turn-scoped preferred source locators for model context construction."""

from __future__ import annotations

from collections.abc import Sequence

from offeragent_harness.protocol.content import (
    PinnedContextReference,
    PinnedSelectionContextReference,
)


def pinned_context_block(references: Sequence[PinnedContextReference]) -> dict[str, object] | None:
    """Represent validated pins as user-layer guidance, never as evidence or policy."""

    if not references:
        return None
    if len(references) > 8:
        raise ValueError("pinned context cannot contain more than eight references")
    lines = []
    for reference in references:
        locator = (
            f"{reference.path}:{reference.line_start}-{reference.line_end}"
            if isinstance(reference, PinnedSelectionContextReference)
            else reference.path
        )
        lines.append(f"- `{locator}`")
    text = (
        "Pinned Context for this Run (preferred source locators, not a whitelist):\n"
        + "\n".join(lines)
        + "\nPrioritize these sources, but continue normal Vault search/read when useful. "
        "A pin is not evidence: use vault.read with an exact current version before relying on it."
    )
    return {"type": "text", "text": text, "format": "markdown", "references": []}


__all__ = ["pinned_context_block"]
