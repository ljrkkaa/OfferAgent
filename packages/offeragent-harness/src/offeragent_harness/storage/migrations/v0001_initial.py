"""Initial local state, event, and invocation-journal schema."""

from __future__ import annotations

VERSION = 1
NAME = "initial_local_state"

STATEMENTS = (
    """
    CREATE TABLE event_streams (
        stream_id TEXT PRIMARY KEY,
        latest_sequence INTEGER NOT NULL DEFAULT 0 CHECK (latest_sequence >= 0),
        terminal_sequence INTEGER,
        CHECK (
            terminal_sequence IS NULL
            OR (terminal_sequence >= 1 AND terminal_sequence <= latest_sequence)
        )
    ) STRICT
    """,
    """
    CREATE TABLE events (
        stream_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        event_id TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        occurred_at TEXT NOT NULL,
        terminal INTEGER NOT NULL CHECK (terminal IN (0, 1)),
        idempotency_key TEXT NOT NULL,
        PRIMARY KEY (stream_id, sequence),
        UNIQUE (stream_id, idempotency_key),
        FOREIGN KEY (stream_id) REFERENCES event_streams(stream_id) ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE UNIQUE INDEX events_one_terminal_per_stream
    ON events(stream_id)
    WHERE terminal = 1
    """,
    """
    CREATE INDEX events_stream_replay
    ON events(stream_id, sequence)
    """,
    """
    CREATE TABLE entities (
        collection TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        value_json TEXT NOT NULL CHECK (json_valid(value_json)),
        PRIMARY KEY (collection, entity_id)
    ) STRICT
    """,
    """
    CREATE TABLE invocation_journal (
        scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('started', 'completed', 'unknown')),
        started_at TEXT NOT NULL,
        completed_at TEXT,
        result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
        PRIMARY KEY (scope, idempotency_key),
        CHECK (
            (state = 'started' AND completed_at IS NULL AND result_json IS NULL)
            OR (state = 'completed' AND completed_at IS NOT NULL AND result_json IS NOT NULL)
            OR (state = 'unknown' AND completed_at IS NOT NULL AND result_json IS NULL)
        )
    ) STRICT
    """,
    """
    CREATE INDEX invocation_journal_state
    ON invocation_journal(state, started_at)
    """,
)
