"""Durable trust boundary for locally discovered Skills."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SkillTrustVerificationRequest:
    workspace_id: str
    root_id: str
    layer: str
    package_path: str
    name: str
    metadata_hash: str


@dataclass(frozen=True, slots=True)
class SkillTrustVerificationResult:
    verified: bool
    verifier_id: str
    trust_token: str | None
    reason: str | None

    def __post_init__(self) -> None:
        if not self.verifier_id:
            raise ValueError("Skill trust verification requires a verifier_id")
        if self.verified != (self.trust_token is not None) or self.verified == (self.reason is not None):
            raise ValueError("Skill trust verification result is inconsistent")


@dataclass(frozen=True, slots=True)
class SkillTrustDecision:
    workspace_id: str
    root_id: str
    package_path: str
    name: str
    metadata_hash: str
    confirmed: bool


@dataclass(frozen=True, slots=True)
class SkillTrustRecord:
    decision: SkillTrustDecision
    revision: int
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.revision < 1 or not self.idempotency_key:
            raise ValueError("Skill trust record revision/key are invalid")


@runtime_checkable
class SkillTrustVerifier(Protocol):
    async def verify(self, request: SkillTrustVerificationRequest) -> SkillTrustVerificationResult: ...


@runtime_checkable
class SkillStateStore(Protocol):
    async def get_trust(self, workspace_id: str, root_id: str, package_path: str) -> SkillTrustRecord | None: ...

    async def put_trust(
        self,
        decision: SkillTrustDecision,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> SkillTrustRecord: ...


__all__ = [
    "SkillStateStore",
    "SkillTrustDecision",
    "SkillTrustRecord",
    "SkillTrustVerificationRequest",
    "SkillTrustVerificationResult",
    "SkillTrustVerifier",
]
