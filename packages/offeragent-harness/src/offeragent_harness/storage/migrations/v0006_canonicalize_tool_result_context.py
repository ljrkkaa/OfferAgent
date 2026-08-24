"""Move persisted ToolResults to the context-activation canonical shape."""

from __future__ import annotations

VERSION = 6
NAME = "canonicalize_tool_result_context"

_AFFECTED_RUN_STATES = """
    SELECT DISTINCT state.entity_id
    FROM entities AS state
    JOIN json_each(state.value_json, '$.payload.toolResults') AS result
    WHERE state.collection = 'run_states'
      AND json_type(result.value, '$.contextActivations') IS NULL
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
    UPDATE entities AS state
    SET value_json = json_set(
        state.value_json,
        '$.payload.toolResults',
        json((
            SELECT json_group_array(
                json_set(result.value, '$.contextActivations', json('[]'))
            )
            FROM json_each(state.value_json, '$.payload.toolResults') AS result
        ))
    )
    WHERE state.collection = 'run_states'
      AND EXISTS (
          SELECT 1
          FROM json_each(state.value_json, '$.payload.toolResults') AS result
          WHERE json_type(result.value, '$.contextActivations') IS NULL
      )
    """,
    """
    UPDATE entities
    SET value_json = json_set(value_json, '$.schemaVersion', 5)
    WHERE collection = 'run_states'
      AND json_extract(value_json, '$.schemaVersion') = 4
    """,
    """
    UPDATE invocation_journal
    SET result_json = json_set(
        result_json,
        '$.contextActivations',
        json('[]')
    )
    WHERE result_json IS NOT NULL
      AND json_type(result_json, '$.contextActivations') IS NULL
    """,
)
