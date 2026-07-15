"""Delete Run snapshots owned by the retired per-turn write-intent model."""

from __future__ import annotations

VERSION = 5
NAME = "remove_obsolete_write_intent_snapshots"

_OBSOLETE_RUN_IDS = """
    SELECT entity_id
    FROM entities
    WHERE collection = 'run_states'
      AND json_type(value_json, '$.payload.writeObligation.intent') IS NOT NULL
"""

STATEMENTS = (
    f"""
    DELETE FROM entities
    WHERE collection IN ('run_effective_configs', 'run_capability_snapshots')
      AND entity_id IN ({_OBSOLETE_RUN_IDS})
    """,
    """
    DELETE FROM entities
    WHERE collection = 'run_states'
      AND json_type(value_json, '$.payload.writeObligation.intent') IS NOT NULL
    """,
)
