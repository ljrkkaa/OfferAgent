"""Remove obsolete persisted Skill catalog snapshots from the clean baseline."""

from __future__ import annotations

VERSION = 2
NAME = "remove_legacy_skill_state"

STATEMENTS = (
    "DELETE FROM entities WHERE collection IN ('skill_catalog_snapshots', 'skill_trust_decisions')",
    "DELETE FROM events WHERE event_type IN ('skill.catalog_updated', 'skill.trust_changed')",
    "DELETE FROM event_streams WHERE stream_id LIKE 'skill-catalog:%' OR stream_id LIKE 'skill-trust:%'",
)
