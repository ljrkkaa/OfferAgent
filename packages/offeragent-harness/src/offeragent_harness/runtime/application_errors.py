"""One sanitized exception boundary for every local Application Command transport.

Domain services deliberately keep precise Python exceptions for their internal
transactions and tests.  None of those exception messages are a wire contract:
they can contain resource identifiers, paths, or persistence details.  This
module is the only place where an exception crossing the Application Command
boundary becomes a stable :class:`~offeragent_harness.protocol.errors.ErrorEnvelope`.
"""

from __future__ import annotations

import asyncio

from offeragent_harness.error_codes import (
    ErrorCode,
    PolicyDeniedCause,
    ResourceConflictCause,
    ResourceNotFoundCause,
    RuntimeNotReadyCause,
)
from offeragent_harness.ports.cancellation import OperationCancelled
from offeragent_harness.protocol.errors import ProtocolViolation, protocol_error

from .attachment_errors import AttachmentError


def map_application_exception(error: BaseException) -> ProtocolViolation:
    """Return the stable, sanitized protocol representation of ``error``.

    The mapping is intentionally based on explicit exception types.  It never
    uses exception class names or message text as a heuristic, and it never
    copies ``str(error)`` into the envelope.
    """

    if isinstance(error, ProtocolViolation):
        return error
    if isinstance(error, AttachmentError):
        if error.code in {
            "attachment_corrupt",
            "attachment_too_large",
            "invalid_image",
            "invalid_order",
            "metadata_conflict",
            "ownership_mismatch",
            "submission_too_large",
        }:
            return protocol_error(
                ErrorCode.INPUT_IMAGE_INVALID,
                "Conversation image input is invalid",
                details={"reason": error.code},
            )
        if error.code == "attachment_unavailable":
            return protocol_error(
                ErrorCode.RESOURCE_NOT_FOUND,
                "Conversation attachment is unavailable",
                details={"reason": error.code},
            )
        if error.code in {
            "attachment_claimed",
            "attachment_committed",
            "capacity_exceeded",
            "chunk_conflict",
            "commit_conflict",
            "idempotency_conflict",
            "upload_incomplete",
        }:
            return protocol_error(
                ErrorCode.RESOURCE_CONFLICT,
                str(error),
                details={"reason": error.code},
            )
        return protocol_error(
            ErrorCode.PROTOCOL_INVALID_PARAMS,
            str(error),
            details={"reason": error.code},
        )
    if isinstance(error, (OperationCancelled, asyncio.CancelledError)):
        reason = error.reason if isinstance(error, OperationCancelled) else None
        reason_code = getattr(getattr(reason, "code", None), "value", None)
        if reason_code == "deadline":
            return protocol_error(
                ErrorCode.REQUEST_DEADLINE_EXCEEDED,
                "request deadline was exceeded",
                retryable=True,
                cancelled=True,
            )
        return protocol_error(
            ErrorCode.REQUEST_CANCELLED,
            "request was cancelled",
            cancelled=True,
        )
    if isinstance(error, (PermissionError, PolicyDeniedCause)):
        return protocol_error(
            ErrorCode.POLICY_DENIED,
            "request is not permitted by local policy",
        )
    if isinstance(error, (FileNotFoundError, ResourceNotFoundCause)):
        return protocol_error(
            ErrorCode.RESOURCE_NOT_FOUND,
            "requested resource was not found",
        )
    if isinstance(error, ResourceConflictCause):
        return protocol_error(
            ErrorCode.RESOURCE_CONFLICT,
            error.conflict_user_message,
            details={"reason": error.conflict_reason},
        )
    if isinstance(error, TimeoutError):
        return protocol_error(
            ErrorCode.REQUEST_DEADLINE_EXCEEDED,
            "request deadline was exceeded",
            retryable=True,
            cancelled=True,
        )
    if isinstance(error, RuntimeNotReadyCause):
        return protocol_error(
            ErrorCode.RUNTIME_NOT_READY,
            "local Runtime is not ready to accept commands",
            retryable=True,
        )
    return protocol_error(
        ErrorCode.INTERNAL_ERROR,
        "application command failed inside the local Runtime",
    )


__all__ = ["map_application_exception"]
