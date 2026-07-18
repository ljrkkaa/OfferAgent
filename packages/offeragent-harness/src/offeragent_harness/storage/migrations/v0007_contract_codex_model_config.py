"""Remove retired model and loopback-control decisions from durable config facts."""

from __future__ import annotations

VERSION = 7
NAME = "contract_codex_model_config"

_RETIRED_CHANGED_FIELDS = (
    "model.allow_remote_https",
    "model.base_url",
    "model.credential_handle",
    "model.organization_id",
    "model.project_id",
    "model.provider",
    "model.service_tier",
    "model.temperature",
    "model.wire_api",
    "network.update_network_enabled",
    "ui.loopback_web_enabled",
    "ui.persistent_web_lease",
    "update.automatic_check",
    "update.automatic_install",
    "update.channel",
)
_RETIRED_SQL = ", ".join(f"'{item}'" for item in _RETIRED_CHANGED_FIELDS)

STATEMENTS = (
    """
    INSERT OR IGNORE INTO entities(collection, entity_id, revision, value_json)
    SELECT
        'config_migration_reports',
        'v0007:' || source.entity_id,
        1,
        json_object(
            'schemaVersion', 1,
            'codec', 'json',
            'payload', json_object(
                'schemaVersion', 1,
                'migration', 'contract_codex_model_config',
                'ownerId', json_extract(source.value_json, '$.payload.ownerId'),
                'retiredFields', json((
                    SELECT json_group_array(field)
                    FROM (
                        SELECT 'model.allow_remote_https' AS field
                        WHERE json_type(source.value_json, '$.payload.config.model.allow_remote_https') IS NOT NULL
                        UNION ALL SELECT 'model.base_url'
                        WHERE json_type(source.value_json, '$.payload.config.model.base_url') IS NOT NULL
                        UNION ALL SELECT 'model.credential_handle'
                        WHERE json_type(source.value_json, '$.payload.config.model.credential_handle') IS NOT NULL
                        UNION ALL SELECT 'model.model'
                        WHERE json_type(source.value_json, '$.payload.config.model.model') IS NOT NULL
                          AND json_extract(source.value_json, '$.payload.schemaVersion') IN (2, 3)
                        UNION ALL SELECT 'model.account_binding'
                        WHERE json_type(source.value_json, '$.payload.config.model.account_binding') IS NOT NULL
                          AND json_extract(source.value_json, '$.payload.schemaVersion') IN (2, 3)
                        UNION ALL SELECT 'model.organization_id'
                        WHERE json_type(source.value_json, '$.payload.config.model.organization_id') IS NOT NULL
                        UNION ALL SELECT 'model.project_id'
                        WHERE json_type(source.value_json, '$.payload.config.model.project_id') IS NOT NULL
                        UNION ALL SELECT 'model.provider'
                        WHERE json_type(source.value_json, '$.payload.config.model.provider') IS NOT NULL
                        UNION ALL SELECT 'model.service_tier'
                        WHERE json_type(source.value_json, '$.payload.config.model.service_tier') IS NOT NULL
                        UNION ALL SELECT 'model.temperature'
                        WHERE json_type(source.value_json, '$.payload.config.model.temperature') IS NOT NULL
                        UNION ALL SELECT 'model.wire_api'
                        WHERE json_type(source.value_json, '$.payload.config.model.wire_api') IS NOT NULL
                        UNION ALL SELECT 'network.update_network_enabled'
                        WHERE json_type(
                            source.value_json,
                            '$.payload.config.network.update_network_enabled'
                        ) IS NOT NULL
                        UNION ALL SELECT 'ui.loopback_web_enabled'
                        WHERE json_type(source.value_json, '$.payload.config.ui.loopback_web_enabled') IS NOT NULL
                        UNION ALL SELECT 'ui.persistent_web_lease'
                        WHERE json_type(source.value_json, '$.payload.config.ui.persistent_web_lease') IS NOT NULL
                        UNION ALL SELECT 'update'
                        WHERE json_type(source.value_json, '$.payload.config.update') IS NOT NULL
                    )
                )),
                'retiredProviderIds', json(
                    CASE
                        WHEN json_type(source.value_json, '$.payload.config.model.provider') = 'text'
                         AND length(json_extract(source.value_json, '$.payload.config.model.provider')) BETWEEN 1 AND 64
                         AND json_extract(source.value_json, '$.payload.config.model.provider') GLOB '[a-z]*'
                         AND json_extract(source.value_json, '$.payload.config.model.provider')
                             NOT GLOB '*[^a-z0-9_.-]*'
                        THEN json_array(json_extract(source.value_json, '$.payload.config.model.provider'))
                        ELSE json_array()
                    END
                )
            )
        )
    FROM entities AS source
    WHERE source.collection = 'config_layers'
      AND json_extract(source.value_json, '$.payload.schemaVersion') IN (2, 3, 4)
    """,
    """
    UPDATE entities
    SET value_json = json_remove(
        value_json,
        '$.payload.config.model.allow_remote_https',
        '$.payload.config.model.base_url',
        '$.payload.config.model.credential_handle',
        '$.payload.config.model.organization_id',
        '$.payload.config.model.project_id',
        '$.payload.config.model.provider',
        '$.payload.config.model.service_tier',
        '$.payload.config.model.temperature',
        '$.payload.config.model.wire_api',
        '$.payload.config.network.update_network_enabled',
        '$.payload.config.ui.loopback_web_enabled',
        '$.payload.config.ui.persistent_web_lease',
        '$.payload.config.update'
    )
    WHERE collection = 'config_layers'
      AND json_extract(value_json, '$.payload.schemaVersion') IN (2, 3, 4)
    """,
    """
    UPDATE entities
    SET value_json = json_remove(
        value_json,
        '$.payload.config.model.model',
        '$.payload.config.model.account_binding'
    )
    WHERE collection = 'config_layers'
      AND json_extract(value_json, '$.payload.schemaVersion') IN (2, 3)
    """,
    """
    UPDATE entities
    SET value_json = json_set(value_json, '$.payload.schemaVersion', 5)
    WHERE collection = 'config_layers'
      AND json_extract(value_json, '$.payload.schemaVersion') IN (2, 3, 4)
    """,
    f"""
    UPDATE entities AS target
    SET value_json = json_set(
        value_json,
        '$.payload.changedFields',
        json((
            SELECT json_group_array(item.value)
            FROM json_each(target.value_json, '$.payload.changedFields') AS item
            WHERE item.value NOT IN ({_RETIRED_SQL})
        ))
    )
    WHERE collection = 'config_receipts'
      AND EXISTS (
          SELECT 1
          FROM json_each(target.value_json, '$.payload.changedFields') AS item
          WHERE item.value IN ({_RETIRED_SQL})
      )
    """,
    f"""
    UPDATE events AS target
    SET payload_json = json_set(
        payload_json,
        '$.changedFields',
        json((
            SELECT json_group_array(item.value)
            FROM json_each(target.payload_json, '$.changedFields') AS item
            WHERE item.value NOT IN ({_RETIRED_SQL})
        ))
    )
    WHERE event_type = 'config.changed'
      AND EXISTS (
          SELECT 1
          FROM json_each(target.payload_json, '$.changedFields') AS item
          WHERE item.value IN ({_RETIRED_SQL})
      )
    """,
)
