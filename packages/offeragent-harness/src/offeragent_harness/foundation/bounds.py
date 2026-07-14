"""Protocol-wide structural limits.

These limits bound values that cross a durable or wire boundary.  They are not
relevance scores or retrieval heuristics: a caller either fits the protocol or
must return a truncated result.
"""

MAX_AUDIT_EFFECTS = 256
MAX_ARTIFACT_REFERENCES = 256
MAX_SOURCE_REFERENCES = 512


__all__ = ["MAX_ARTIFACT_REFERENCES", "MAX_AUDIT_EFFECTS", "MAX_SOURCE_REFERENCES"]
