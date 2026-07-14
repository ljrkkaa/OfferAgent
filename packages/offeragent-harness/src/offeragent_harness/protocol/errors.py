"""Stable protocol error codes and JSON-serializable error details."""

from __future__ import annotations

from pydantic import Field, model_validator

from offeragent_harness.error_codes import ErrorCode

from ._base import JsonObject, WireModel
from .ids import TraceId


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
