"""Stable provider-neutral error codes shared by domain and wire adapters."""

from enum import Enum


class PolicyDeniedCause:
    """Marker for a domain exception that must cross the wire as policy denial."""

    __slots__ = ()


class ResourceNotFoundCause:
    """Marker for a domain exception that must cross the wire as not-found."""

    __slots__ = ()


class ResourceConflictCause:
    """Marker for a domain exception that must cross the wire as state conflict."""

    __slots__ = ()
    conflict_reason = "resource_state_conflict"
    conflict_user_message = "requested operation conflicts with current resource state"


class RuntimeNotReadyCause:
    """Marker for a domain exception that must cross the wire as not-ready."""

    __slots__ = ()


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
    PROVIDER_RATE_LIMITED = "provider.rate_limited"
    PROVIDER_PROTOCOL_ERROR = "provider.protocol_error"
    AUTH_REQUIRED = "provider.auth_required"
    PROVIDER_CONTEXT_OVERFLOW = "provider.context_overflow"
    PROVIDER_IMAGE_UNSUPPORTED = "provider.image_unsupported"
    PROVIDER_UNSUPPORTED = "provider.unsupported"
    INPUT_IMAGE_INVALID = "input.image_invalid"
    INTERNAL_ERROR = "internal.error"


__all__ = [
    "ErrorCode",
    "PolicyDeniedCause",
    "ResourceConflictCause",
    "ResourceNotFoundCause",
    "RuntimeNotReadyCause",
]
