"""Provider-neutral Tool Kernel domain contracts."""

from .canonical import CanonicalJsonError, canonical_json_bytes, canonical_json_sha256
from .definitions import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffectClass,
    ToolCall,
    ToolDefinition,
    ToolDefinitionError,
)
from .preflight import (
    PreflightConflict,
    PreflightError,
    PreflightEvidence,
    PreflightProvider,
    PreflightProviderUnavailable,
    PreflightRegistry,
)
from .recovery_contract import (
    invocation_journal_scope,
    invocation_request_fingerprint,
    is_safe_crash_replay,
    is_side_effect_free,
)
from .results import (
    MAX_TOOL_RESULT_SOURCE_REFERENCES,
    SideEffect,
    SideEffectKind,
    SideEffectState,
    ToolError,
    ToolResult,
    ToolResultStatus,
)
from .source_references import vault_source_reference
from .validator import ToolValidationError, ToolValidator, ValidatedArguments, ValidationIssue

__all__ = [
    "MAX_TOOL_RESULT_SOURCE_REFERENCES",
    "ApprovalEvidence",
    "CanonicalJsonError",
    "ExecutorLocation",
    "PreflightConflict",
    "PreflightError",
    "PreflightEvidence",
    "PreflightMode",
    "PreflightProvider",
    "PreflightProviderUnavailable",
    "PreflightRegistry",
    "ResultSensitivity",
    "SideEffect",
    "SideEffectClass",
    "SideEffectKind",
    "SideEffectState",
    "ToolCall",
    "ToolDefinition",
    "ToolDefinitionError",
    "ToolError",
    "ToolResult",
    "ToolResultStatus",
    "ToolValidationError",
    "ToolValidator",
    "ValidatedArguments",
    "ValidationIssue",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "invocation_journal_scope",
    "invocation_request_fingerprint",
    "is_safe_crash_replay",
    "is_side_effect_free",
    "vault_source_reference",
]
