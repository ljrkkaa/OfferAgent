"""Translate retired document-extraction facts into the current event protocol."""

from __future__ import annotations

VERSION = 9
NAME = "retire_document_extraction_events"

STATEMENTS = (
    """
    UPDATE events
    SET event_type = 'runtime.warning',
        payload_json = json_set(
            payload_json,
            '$.type',
            'runtime.warning',
            '$.payload',
            json_object(
                'code',
                'legacy.document_extraction_failed',
                'message',
                'A historical document extraction attempt failed before document support was retired.',
                'recommendedAction',
                NULL,
                'disabledCapabilities',
                json('[]')
            )
        )
    WHERE event_type = 'document.extraction_failed'
      AND json_extract(payload_json, '$.type') = 'document.extraction_failed'
    """,
    """
    UPDATE events
    SET payload_json = json_set(
        payload_json,
        '$.payload.error.code',
        'internal.error'
    )
    WHERE event_type = 'turn.failed'
      AND json_extract(payload_json, '$.type') = 'turn.failed'
      AND json_extract(payload_json, '$.payload.error.code') = 'document.extraction_failed'
    """,
)
