from __future__ import annotations

import pytest
from pydantic import ValidationError

from offeragent_harness.protocol._base import validate_wire
from offeragent_harness.protocol.events import SessionUpdatedPayload


def _summary() -> dict[str, object]:
    return {
        "sessionId": "ses_session_event",
        "workspaceId": "ws_session_event",
        "title": "Session",
        "createdAt": "2026-07-13T12:00:00Z",
        "updatedAt": "2026-07-13T12:00:00Z",
        "activeRunId": None,
        "turnCount": 0,
        "deleted": False,
    }


def _fork_reference(*, artifact_link_ids: list[str] | None = None) -> dict[str, object]:
    return {
        "sourceWorkspaceId": "ws_session_event",
        "sourceSessionId": "ses_source",
        "sourceTurnId": "turn_boundary",
        "sourceRunId": "run_boundary",
        "sourceEventSequence": 7,
        "artifactLinkIds": artifact_link_ids or [],
    }


def test_create_and_fork_session_events_are_strict_and_reconstructable() -> None:
    created = validate_wire(
        SessionUpdatedPayload,
        {
            "session": _summary(),
            "changedFields": ["created", "title", "updatedAt"],
            "forkReference": None,
        },
    )
    assert created.fork_reference is None

    forked = validate_wire(
        SessionUpdatedPayload,
        {
            "session": _summary(),
            "changedFields": ["created", "forkReference"],
            "forkReference": _fork_reference(artifact_link_ids=["art_a", "art_b"]),
        },
    )
    assert forked.fork_reference is not None
    assert forked.fork_reference.source_event_sequence == 7
    assert forked.fork_reference.artifact_link_ids == ["art_a", "art_b"]


@pytest.mark.parametrize(
    ("changed_fields", "reference"),
    [
        (["created", "forkReference"], None),
        (["title"], _fork_reference()),
        (["title", "title"], None),
    ],
)
def test_session_event_rejects_fork_presence_drift_and_duplicate_changes(
    changed_fields: list[str],
    reference: dict[str, object] | None,
) -> None:
    with pytest.raises(ValidationError):
        validate_wire(
            SessionUpdatedPayload,
            {
                "session": _summary(),
                "changedFields": changed_fields,
                "forkReference": reference,
            },
        )


@pytest.mark.parametrize("artifact_link_ids", [["art_b", "art_a"], ["art_a", "art_a"]])
def test_fork_event_requires_canonical_sorted_unique_artifact_links(artifact_link_ids: list[str]) -> None:
    with pytest.raises(ValidationError, match="sorted and unique"):
        validate_wire(
            SessionUpdatedPayload,
            {
                "session": _summary(),
                "changedFields": ["created", "forkReference"],
                "forkReference": _fork_reference(artifact_link_ids=artifact_link_ids),
            },
        )
