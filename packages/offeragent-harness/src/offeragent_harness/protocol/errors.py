"""Stable protocol error codes and JSON-serializable error details."""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from ._base import JsonObject, WireModel
from .ids import TraceId


class ErrorCode(str, Enum):
    # Transport / JSON-RPC
    PROTOCOL_INVALID_JSON = "protocol.invalid_json"
    PROTOCOL_INVALID_UTF8 = "protocol.invalid_utf8"
    PROTOCOL_INVALID_REQUEST = "protocol.invalid_request"
    PROTOCOL_METHOD_NOT_FOUND = "protocol.method_not_found"
    PROTOCOL_INVALID_PARAMS = "protocol.invalid_params"
    PROTOCOL_MESSAGE_TOO_LARGE = "protocol.message_too_large"
    PROTOCOL_DUPLICATE_REQUEST_ID = "protocol.duplicate_request_id"
    PROTOCOL_SCHEMA_MISMATCH = "protocol.schema_mismatch"
    PROTOCOL_INCOMPATIBLE_VERSION = "protocol.incompatible_version"
    PROTOCOL_MISSING_CAPABILITY = "protocol.missing_capability"
    PROTOCOL_INTERNAL_ERROR = "protocol.internal_error"
    # Runtime / application
    RUNTIME_NOT_READY = "runtime.not_ready"
    RUNTIME_SHUTTING_DOWN = "runtime.shutting_down"
    RUNTIME_INTERRUPTED = "runtime.interrupted"
    REQUEST_CANCELLED = "request.cancelled"
    REQUEST_DEADLINE_EXCEEDED = "request.deadline_exceeded"
    RESOURCE_NOT_FOUND = "resource.not_found"
    RESOURCE_CONFLICT = "resource.conflict"
    POLICY_DENIED = "policy.denied"
    APPROVAL_EXPIRED = "approval.expired"
    TOOL_FAILED = "tool.failed"
    TOOL_UNKNOWN_OUTCOME = "tool.unknown_outcome"
    PROVIDER_UNREACHABLE = "provider.unreachable"
    AUTH_REQUIRED = "provider.auth_required"
    INTERNAL_ERROR = "internal.error"


class ErrorEnvelope(WireModel):
    """Provider-neutral error details used by every transport adapter."""

    code: ErrorCode
    retryable: bool
    cancelled: bool
    user_visible_message: str = Field(min_length=1, max_length=4096)
    details: JsonObject = Field(default_factory=dict)
    retry_after_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    trace_id: TraceId | None = None

    @model_validator(mode="after")
    def _retry_delay_requires_retryability(self) -> ErrorEnvelope:
        if self.retry_after_ms is not None and not self.retryable:
            raise ValueError("retryAfterMs is only valid for retryable errors")
        return self


class ProtocolViolation(ValueError):
    """Raised before a malformed message is allowed to reach the Agent Core."""

    def __init__(self, error: ErrorEnvelope) -> None:
        super().__init__(error.user_visible_message)
        self.error = error


def protocol_error(
    code: ErrorCode,
    message: str,
    *,
    details: JsonObject | None = None,
    retryable: bool = False,
    cancelled: bool = False,
    retry_after_ms: int | None = None,
    trace_id: TraceId | None = None,
) -> ProtocolViolation:
    return ProtocolViolation(
        ErrorEnvelope(
            code=code,
            retryable=retryable,
            cancelled=cancelled,
            user_visible_message=message,
            details=details or {},
            retry_after_ms=retry_after_ms,
            trace_id=trace_id,
        )
    )


__all__ = ["ErrorCode", "ErrorEnvelope", "ProtocolViolation", "protocol_error"]
