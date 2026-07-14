from __future__ import annotations

import pytest
from pydantic import ValidationError

from offeragent_harness.protocol.events import (
    EventType,
    SkillCatalogUpdatedPayload,
    SkillTrustChangedPayload,
    make_domain_event_record,
    parse_persisted_domain_event,
)
from offeragent_harness.protocol.messages import SkillCatalogStatusSnapshot

HASH = "sha256:" + "a" * 64


def test_skill_catalog_status_is_typed_and_count_coherent() -> None:
    status = SkillCatalogStatusSnapshot.model_validate(
        {
            "revision": 3,
            "snapshotHash": HASH,
            "discoveredCount": 2,
            "enabledCount": 1,
            "partial": True,
            "diagnostics": [
                {
                    "severity": "warning",
                    "code": "trust_confirmation_required",
                    "message": "confirmation required",
                    "rootId": "workspace",
                    "path": "answer/SKILL.md",
                }
            ],
        }
    )
    assert status.revision == 3 and status.diagnostics[0].root_id == "workspace"

    with pytest.raises(ValidationError, match="enabledCount"):
        SkillCatalogStatusSnapshot.model_validate(
            {
                "revision": 1,
                "snapshotHash": HASH,
                "discoveredCount": 0,
                "enabledCount": 1,
                "partial": False,
                "diagnostics": [],
            }
        )


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            EventType.SKILL_CATALOG_UPDATED,
            SkillCatalogUpdatedPayload(
                revision=2,
                snapshot_hash=HASH,
                record_revision=3,
                discovered_count=4,
                enabled_count=2,
                partial=False,
            ),
        ),
        (
            EventType.SKILL_TRUST_CHANGED,
            SkillTrustChangedPayload(
                root_id="workspace",
                package_path="answer",
                name="answer",
                metadata_hash=HASH,
                confirmed=True,
                record_revision=2,
            ),
        ),
    ],
)
def test_skill_events_round_trip_as_persisted_domain_facts(
    event_type: EventType,
    payload: SkillCatalogUpdatedPayload | SkillTrustChangedPayload,
) -> None:
    record = make_domain_event_record(
        event_type=event_type,
        payload=payload,
        trace_id="trace_skills",
        workspace_id="ws_skills",
        session_id=None,
        turn_id=None,
        run_id=None,
        root_run_id=None,
        parent_run_id=None,
        state_revision=3,
    )
    parsed = parse_persisted_domain_event(record.to_wire())
    assert parsed.type is event_type
    assert type(parsed.payload) is type(payload)
