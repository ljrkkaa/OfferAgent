"""Remove the retired plugin-owned Vault execution authority."""

from __future__ import annotations

VERSION = 4
NAME = "remove_client_vault_authority"

STATEMENTS = (
    """
    DELETE FROM entities
    WHERE collection IN (
        'run_client_bindings',
        'run_headless_vault_bindings',
        'headless_vault_authorizations'
    )
    """,
    "DELETE FROM entities WHERE collection = 'approvals' AND entity_id LIKE 'apr_headless_%'",
    "DELETE FROM events WHERE payload_json LIKE '%apr_headless_%' OR payload_json LIKE '%op_headless_%'",
    "DELETE FROM event_streams WHERE stream_id LIKE 'headless-vault:%'",
)
