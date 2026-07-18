"""Authority-free, content-free identities for every allowed network category."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .canonical import canonical_json_sha256

_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_AUDIT_PHASE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class NetworkCategory(str, Enum):
    MODEL = "model"
    SIGNED_UPDATE = "signed_update"


class NetworkOperationPurpose(str, Enum):
    MODEL_INFERENCE = "model_inference"


@dataclass(frozen=True, slots=True)
class NetworkOperationIdentity:
    """Content-free identity for one logical network operation and retry attempt."""

    workspace_id: str
    operation_id: str
    purpose: NetworkOperationPurpose
    attempt: int = 1
    run_id: str | None = None
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if not self.workspace_id or _OPERATION_ID.fullmatch(self.operation_id) is None or self.attempt < 1:
            raise ValueError("network operation identity is invalid")
        if self.run_id is None or self.purpose is not NetworkOperationPurpose.MODEL_INFERENCE:
            raise ValueError("model network operations require an immutable Run identity")
        if self.tool_call_id is not None:
            raise ValueError("model inference is not a remote Tool operation")
        for value in (self.run_id, self.tool_call_id):
            if value is not None and (len(value) > 128 or "\x00" in value):
                raise ValueError("network operation correlation identity is invalid")

    def with_attempt(
        self,
        attempt: int,
        *,
        purpose: NetworkOperationPurpose | None = None,
    ) -> NetworkOperationIdentity:
        return NetworkOperationIdentity(
            self.workspace_id,
            self.operation_id,
            purpose or self.purpose,
            attempt,
            self.run_id,
            self.tool_call_id,
        )


@dataclass(frozen=True, slots=True)
class NetworkAuditRecord:
    category: NetworkCategory
    workspace_id: str
    run_id: str | None
    tool_call_id: str | None
    tool_name: str | None
    provider_id: str | None
    host: str
    port: int
    outcome: str
    status_code: int | None
    sent_bytes: int
    received_bytes: int
    redirect_count: int
    attempt: int
    approval_source: str | None
    occurred_at: datetime
    operation_id: str | None = None
    operation_purpose: NetworkOperationPurpose | None = None
    phase: str | None = None
    stage: str | None = None
    sequence: int = 0
    event_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.host
            or self.port < 1
            or min(self.sent_bytes, self.received_bytes, self.redirect_count, self.sequence) < 0
            or not self.outcome
            or len(self.outcome) > 128
            or "\x00" in self.outcome
        ):
            raise ValueError("network audit fields are invalid")
        if self.attempt < 1 or self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("network audit attempt/timestamp is invalid")
        if self.category is NetworkCategory.MODEL:
            if (
                not self.provider_id
                or self.operation_id is None
                or _OPERATION_ID.fullmatch(self.operation_id) is None
                or self.operation_purpose is None
                or self.phase is None
                or _AUDIT_PHASE.fullmatch(self.phase) is None
                or self.stage not in {"intent", "result"}
                or self.event_id is None
                or _OPERATION_ID.fullmatch(self.event_id) is None
            ):
                raise ValueError("network audit operation identity is incomplete")
            NetworkOperationIdentity(
                self.workspace_id,
                self.operation_id,
                self.operation_purpose,
                self.attempt,
                self.run_id,
                self.tool_call_id,
            )
        elif self.run_id is None:
            raise ValueError("non-model network audit requires a Run identity")


def network_audit_event_id(
    operation: NetworkOperationIdentity,
    endpoint_id: str,
    phase: str,
    stage: str,
    sequence: int,
) -> str:
    digest = canonical_json_sha256(
        {
            "workspaceId": operation.workspace_id,
            "operationId": operation.operation_id,
            "purpose": operation.purpose.value,
            "runId": operation.run_id,
            "toolCallId": operation.tool_call_id,
            "endpointId": endpoint_id,
            "attempt": operation.attempt,
            "phase": phase,
            "stage": stage,
            "sequence": sequence,
        }
    )
    return f"net_{digest.removeprefix('sha256:')[:32]}"


__all__ = [
    "NetworkAuditRecord",
    "NetworkCategory",
    "NetworkOperationIdentity",
    "NetworkOperationPurpose",
    "network_audit_event_id",
]
