from __future__ import annotations

import pytest

from offeragent_harness.tools import PreflightEvidence


def evidence(**overrides: object) -> PreflightEvidence:
    values: dict[str, object] = {
        "provider_id": "vault.transaction.v1",
        "state_hash": "sha256:" + "1" * 64,
        "artifact_ids": ("art_diff",),
        "lock_keys": ("notes/example.md",),
        "token": "opaque-token",
        "facts": {"operationCount": 1},
    }
    values.update(overrides)
    return PreflightEvidence(**values)  # type: ignore[arg-type]


def test_preflight_evidence_requires_canonical_bounded_security_identity() -> None:
    assert evidence().provider_id == "vault.transaction.v1"

    for invalid_hash in ("sha256:" + "G" * 64, "sha256:short", "md5:" + "1" * 64):
        with pytest.raises(ValueError, match="canonical sha256"):
            evidence(state_hash=invalid_hash)
    with pytest.raises(ValueError, match="provider ID"):
        evidence(provider_id="Vault Transaction")
    with pytest.raises(ValueError, match="token"):
        evidence(token="x" * 513)
    with pytest.raises(ValueError, match="lock keys"):
        evidence(lock_keys=tuple(f"notes/{index}.md" for index in range(257)))
    with pytest.raises(ValueError, match="case-insensitively unique"):
        evidence(lock_keys=("Notes/A.md", "notes/a.md"))
    with pytest.raises(ValueError, match="artifact IDs"):
        evidence(artifact_ids=("art_same", "art_same"))
    with pytest.raises(ValueError, match="artifact IDs"):
        evidence(artifact_ids=("artifact_not_canonical",))
