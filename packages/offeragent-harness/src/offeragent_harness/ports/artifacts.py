"""Large-content persistence boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json


class ArtifactState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Sensitivity(str, Enum):
    PUBLIC = "public"
    WORKSPACE = "workspace"
    PRIVATE = "private"
    SECRET = "secret"


@dataclass(frozen=True)
class ArtifactMetadata:
    artifact_id: str
    workspace_id: str
    owner_run_id: str
    mime_type: str
    byte_length: int
    sha256: str
    sensitivity: Sensitivity
    state: ArtifactState
    created_at: datetime
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.byte_length < 0:
            raise ValueError("artifact byte length cannot be negative")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("artifact timestamp must be timezone-aware")
        attributes = freeze_json(self.attributes)
        if not isinstance(attributes, FrozenJsonObject):
            raise TypeError("artifact attributes must be a JSON object")
        object.__setattr__(self, "attributes", attributes)


@runtime_checkable
class ArtifactStore(Protocol):
    async def put(self, metadata: ArtifactMetadata, content: bytes, *, idempotency_key: str) -> ArtifactMetadata: ...

    async def metadata(self, artifact_id: str) -> ArtifactMetadata | None: ...

    def read(self, artifact_id: str, *, offset: int = 0, limit: int | None = None) -> AsyncIterator[bytes]: ...


__all__ = ["ArtifactMetadata", "ArtifactState", "ArtifactStore", "Sensitivity"]
