"""Opaque local Runtime identities used by isolated Worker composition."""

from __future__ import annotations

import hashlib
import re


def workspace_database_identity(workspace_instance_id: str) -> str:
    pattern = r"wsi_[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    if re.fullmatch(pattern, workspace_instance_id) is None:
        raise ValueError("workspace instance ID is invalid")
    digest = hashlib.sha256(f"OfferAgent.DatabaseIdentity.v1\0{workspace_instance_id}".encode("ascii")).hexdigest()
    return f"sha256:{digest}"


__all__ = ["workspace_database_identity"]
