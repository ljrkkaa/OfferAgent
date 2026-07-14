"""Protocol-shaped source-reference values without a Core -> Protocol import."""

from __future__ import annotations

from typing import Any


def vault_source_reference(
    *,
    workspace_id: str,
    path: str,
    content_hash: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    heading: str | None = None,
    workspace_revision: int | None = None,
    freshness: str = "unknown",
    label: str | None = None,
) -> dict[str, Any]:
    """Build the camelCase value accepted by ``protocol.content.VaultSourceRef``.

    The persisted event boundary remains the authoritative strict validator.
    These checks keep domain producers fail-fast while avoiding a forbidden
    dependency from the Tool domain into the wire Protocol package.
    """

    if not workspace_id or not path:
        raise ValueError("Vault source references require workspace_id and path")
    if line_start is not None and line_start < 1:
        raise ValueError("Vault source reference line_start must be positive")
    if line_end is not None and (line_start is None or line_end < line_start):
        raise ValueError("Vault source reference line range is invalid")
    if workspace_revision is not None and workspace_revision < 0:
        raise ValueError("Vault source reference workspace_revision cannot be negative")
    if freshness not in {"fresh", "stale", "partial", "stale_partial", "unknown"}:
        raise ValueError("Vault source reference freshness is invalid")
    if heading is not None and not heading:
        raise ValueError("Vault source reference heading cannot be empty")
    if label is not None and not label:
        raise ValueError("Vault source reference label cannot be empty")

    file: dict[str, Any] = {"workspaceId": workspace_id, "path": path}
    if content_hash is not None:
        file["contentHash"] = content_hash
    if line_start is not None:
        file["lineStart"] = line_start
    if line_end is not None:
        file["lineEnd"] = line_end
    if heading is not None:
        file["heading"] = heading

    reference: dict[str, Any] = {"type": "vault", "file": file, "freshness": freshness}
    if workspace_revision is not None:
        reference["workspaceRevision"] = workspace_revision
    if label is not None:
        reference["label"] = label
    return reference


__all__ = ["vault_source_reference"]
