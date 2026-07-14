"""Authority-free primitives shared across OfferAgent domain boundaries."""

from .canonical import CanonicalJsonError, canonical_json_bytes, canonical_json_sha256
from .network_audit import (
    NetworkAuditRecord,
    NetworkCategory,
    NetworkOperationIdentity,
    NetworkOperationPurpose,
    network_audit_event_id,
)
from .write_intent import vault_write_intent_hash

__all__ = [
    "CanonicalJsonError",
    "NetworkAuditRecord",
    "NetworkCategory",
    "NetworkOperationIdentity",
    "NetworkOperationPurpose",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "network_audit_event_id",
    "vault_write_intent_hash",
]
