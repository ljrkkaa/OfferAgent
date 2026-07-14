from __future__ import annotations

import pytest
from pydantic import ValidationError

from offeragent_harness.protocol._base import validate_wire
from offeragent_harness.protocol.common import ClientContextSnapshot


def test_client_context_metadata_and_backlinks_round_trip_with_closed_schema() -> None:
    context = validate_wire(
        ClientContextSnapshot,
        {
            "activeFile": "notes/target.md",
            "activeFileHash": f"sha256:{'a' * 64}",
            "activeFileRevision": 2,
            "selection": None,
            "selectionRevision": None,
            "cursorOffset": 15,
            "hasUnsavedChanges": True,
            "metadataCacheRevision": 9,
            "metadata": {
                "frontmatter": {"status": "ready", "priority": 2},
                "tags": ["#interview", "#offer"],
                "links": ["notes/company.md"],
                "unresolvedLinks": ["Missing Note"],
            },
            "backlinks": [{"path": "notes/source.md", "count": 3}],
        },
    )

    assert context.metadata is not None
    assert context.metadata.frontmatter["status"] == "ready"
    assert context.backlinks is not None
    assert context.backlinks[0].count == 3
    assert context.to_wire()["metadata"]["unresolvedLinks"] == ["Missing Note"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("metadata", {"frontmatter": {}, "tags": [], "links": ["../escape.md"], "unresolvedLinks": []}),
        ("backlinks", [{"path": "notes/source.md", "count": 0}]),
    ],
)
def test_client_context_metadata_fails_closed_on_unsafe_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        validate_wire(ClientContextSnapshot, {field: value})
