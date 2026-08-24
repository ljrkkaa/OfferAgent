"""Remove the retired diagnostic stdio flag from durable configuration facts."""

from __future__ import annotations

VERSION = 3
NAME = "remove_obsolete_runtime_config"

STATEMENTS = (
    """
    UPDATE entities
    SET value_json = json_remove(
        value_json,
        '$.payload.config.runtime.diagnostic_stdio'
    )
    WHERE json_type(
        value_json,
        '$.payload.config.runtime.diagnostic_stdio'
    ) IS NOT NULL
    """,
    """
    UPDATE entities AS target
    SET value_json = json_set(
        value_json,
        '$.payload.changedFields',
        json((
            SELECT json_group_array(item.value)
            FROM json_each(target.value_json, '$.payload.changedFields') AS item
            WHERE item.value <> 'runtime.diagnostic_stdio'
        ))
    )
    WHERE collection = 'config_receipts'
      AND EXISTS (
          SELECT 1
          FROM json_each(target.value_json, '$.payload.changedFields') AS item
          WHERE item.value = 'runtime.diagnostic_stdio'
      )
    """,
    """
    UPDATE events AS target
    SET payload_json = json_set(
        payload_json,
        '$.changedFields',
        json((
            SELECT json_group_array(item.value)
            FROM json_each(target.payload_json, '$.changedFields') AS item
            WHERE item.value <> 'runtime.diagnostic_stdio'
        ))
    )
    WHERE event_type = 'config.changed'
      AND EXISTS (
          SELECT 1
          FROM json_each(target.payload_json, '$.changedFields') AS item
          WHERE item.value = 'runtime.diagnostic_stdio'
      )
    """,
)
