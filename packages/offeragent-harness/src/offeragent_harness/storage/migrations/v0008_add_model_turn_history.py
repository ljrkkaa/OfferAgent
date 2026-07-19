"""Add durable stateless model-turn history without inferring lost authority."""

from __future__ import annotations

VERSION = 8
NAME = "add_model_turn_history"

_AFFECTED_RUN_STATES = """
    SELECT entity_id
    FROM entities
    WHERE collection = 'run_states'
      AND json_extract(value_json, '$.schemaVersion') = 5
      AND json_type(value_json, '$.payload.modelTurns') IS NULL
"""

_NONTERMINAL_AFFECTED_RUN_STATES = f"""
    SELECT affected.entity_id
    FROM ({_AFFECTED_RUN_STATES}) AS affected
    JOIN entities AS run
      ON run.collection = 'runs'
     AND run.entity_id = affected.entity_id
    WHERE json_extract(run.value_json, '$.payload.status') NOT IN (
        'completed', 'cancelled', 'failed', 'interrupted', 'orphaned'
    )
"""

STATEMENTS = (
    f"""
    DELETE FROM entities
    WHERE collection IN ('run_effective_configs', 'run_capability_snapshots')
      AND entity_id IN ({_NONTERMINAL_AFFECTED_RUN_STATES})
    """,
    f"""
    DELETE FROM entities
    WHERE collection = 'run_states'
      AND entity_id IN ({_NONTERMINAL_AFFECTED_RUN_STATES})
    """,
    """
    UPDATE entities
    SET value_json = json_set(
        value_json,
        '$.payload.modelTurns',
        json('[]'),
        '$.schemaVersion',
        6
    )
    WHERE collection = 'run_states'
      AND json_extract(value_json, '$.schemaVersion') = 5
      AND json_type(value_json, '$.payload.modelTurns') IS NULL
    """,
)
