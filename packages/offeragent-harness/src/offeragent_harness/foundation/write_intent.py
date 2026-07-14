"""Canonical identity for an explicit, path-scoped Vault write intent."""

from __future__ import annotations

from collections.abc import Sequence

from .canonical import canonical_json_sha256


def vault_write_intent_hash(target_paths: Sequence[str]) -> str:
    """Hash the protocol-defined write-intent binding without normalizing it.

    Callers must validate and canonicalize the path sequence first.  Refusing to
    sort or deduplicate here is deliberate: a non-canonical wire value must not
    be silently rebound to a different user intent.
    """

    paths = list(target_paths)
    if not all(isinstance(path, str) for path in paths):
        raise TypeError("Vault write intent paths must be strings")
    return canonical_json_sha256(
        {
            "kind": "vault_write_required",
            "targetPaths": paths,
        }
    )


__all__ = ["vault_write_intent_hash"]
