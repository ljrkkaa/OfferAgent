"""Command and reverse-request DTOs for protocol v1."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from ._base import EmptyParams, JsonObject, WireModel, validate_wire
from .capabilities import CapabilityName, CapabilitySet, ProtocolRange
from .common import (
    ApprovalDecision,
    ApprovalScope,
    ClientContextSnapshot,
    RunConfigSnapshot,
    RunSnapshot,
    SessionSummary,
    SubagentResult,
    ToolCallStatus,
    TurnSnapshot,
)
from .content import ArtifactRef, ContentBlock, RelativeVaultPath
from .errors import ErrorCode, ErrorEnvelope, protocol_error
from .events import EventEnvelope, EventType
from .ids import (
    ApprovalId,
    ArtifactId,
    EventId,
    InvocationId,
    MessageId,
    ProtocolVersion,
    RequestId,
    Rfc3339DateTime,
    RunId,
    SemanticVersion,
    SessionId,
    Sha256Digest,
    ToolCallId,
    TraceId,
    TransactionId,
    TurnId,
    WorkspaceId,
    WorkspaceInstanceId,
)


class TransportKind(str, Enum):
    WINDOWS_NAMED_PIPE = "windows-named-pipe"
    LOOPBACK_HTTP = "loopback-http"
    LOOPBACK_WEBSOCKET = "loopback-websocket"
    STDIO_DEV = "stdio-dev"


class RuntimeArch(str, Enum):
    WIN_X64 = "win-x64"
    WIN_ARM64 = "win-arm64"


class RuntimeState(str, Enum):
    COLD = "cold"
    STARTING = "starting"
    INDEXING = "indexing"
    READY = "ready"
    BUSY = "busy"
    IDLE = "idle"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    STOPPING = "stopping"


class InitializeParams(WireModel):
    protocol_version: ProtocolVersion
    client_version: SemanticVersion
    workspace_id: WorkspaceId
    capabilities: CapabilitySet
    supported_protocol_range: ProtocolRange | None = None
    required_capabilities: list[CapabilityName] = Field(default_factory=list, max_length=32)
    schema_hash: Sha256Digest | None = None

    @model_validator(mode="after")
    def _required_capabilities_are_unique(self) -> InitializeParams:
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError("requiredCapabilities cannot contain duplicates")
        return self


class InitializeResult(WireModel):
    protocol_version: ProtocolVersion
    supported_protocol_range: ProtocolRange
    runtime_version: SemanticVersion
    core_version: SemanticVersion
    schema_hash: Sha256Digest
    workspace_id: WorkspaceId
    workspace_instance_id: WorkspaceInstanceId
    host_pid: int = Field(ge=1)
    worker_pid: int = Field(ge=1)
    transport: TransportKind
    runtime_arch: RuntimeArch
    capabilities: CapabilitySet
    build_commit: str = Field(min_length=7, max_length=64, pattern=r"^[0-9a-f]{7,64}$")


class RuntimePingParams(WireModel):
    nonce: RequestId


class RuntimePingResult(WireModel):
    nonce: RequestId
    timestamp: Rfc3339DateTime
    worker_pid: int = Field(ge=1)


class IndexStatusSnapshot(WireModel):
    generation: int | None = Field(default=None, ge=1)
    workspace_revision: int = Field(ge=0)
    state: Literal["not_started", "indexing", "ready", "stale", "failed"]
    document_count: int = Field(ge=0)
    pending_changes: int = Field(ge=0)
    last_error: ErrorEnvelope | None = None


class RuntimeStatusParams(EmptyParams):
    pass


class RuntimeStatusResult(WireModel):
    state: RuntimeState
    workspace_id: WorkspaceId
    workspace_instance_id: WorkspaceInstanceId
    host_pid: int = Field(ge=1)
    worker_pid: int = Field(ge=1)
    runtime_version: SemanticVersion
    core_version: SemanticVersion
    protocol_version: ProtocolVersion
    schema_hash: Sha256Digest
    database_identity: Sha256Digest
    active_run_ids: list[RunId] = Field(default_factory=list, max_length=1024)
    index: IndexStatusSnapshot
    warnings: list[ErrorEnvelope] = Field(default_factory=list, max_length=256)


class ConfigScope(str, Enum):
    USER = "user"
    WORKSPACE = "workspace"
    SESSION = "session"


class ConfigGetParams(WireModel):
    scope: ConfigScope = ConfigScope.WORKSPACE
    session_id: SessionId | None = None

    @model_validator(mode="after")
    def _session_scope_requires_id(self) -> ConfigGetParams:
        if (self.scope == ConfigScope.SESSION) != (self.session_id is not None):
            raise ValueError("sessionId is required exactly when scope is session")
        return self


class ConfigSnapshot(WireModel):
    scope: ConfigScope
    revision: int = Field(ge=0)
    values: JsonObject
    restart_pending: bool = False


class ConfigFieldError(WireModel):
    path: str = Field(min_length=1, max_length=1024)
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4096)


class ConfigUpdateParams(WireModel):
    scope: ConfigScope = ConfigScope.WORKSPACE
    session_id: SessionId | None = None
    expected_revision: int = Field(ge=0)
    patch: JsonObject

    @model_validator(mode="after")
    def _scope_and_patch_are_valid(self) -> ConfigUpdateParams:
        if (self.scope == ConfigScope.SESSION) != (self.session_id is not None):
            raise ValueError("sessionId is required exactly when scope is session")
        if not self.patch:
            raise ValueError("patch must not be empty")
        return self


class ConfigUpdateResult(WireModel):
    status: Literal["applied", "restart_required", "rejected"]
    snapshot: ConfigSnapshot
    field_errors: list[ConfigFieldError] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def _rejection_has_field_errors(self) -> ConfigUpdateResult:
        if self.status == "rejected" and not self.field_errors:
            raise ValueError("a rejected config update requires at least one field error")
        if self.status != "rejected" and self.field_errors:
            raise ValueError("fieldErrors are only valid for rejected config updates")
        return self


class ModelDescriptor(WireModel):
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=512)
    local: bool
    supports_streaming: bool
    supports_structured_output: bool
    max_context_tokens: int | None = Field(default=None, ge=1)
    available: bool


class ModelsListParams(WireModel):
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    include_unavailable: bool = False


class ModelsListResult(WireModel):
    models: list[ModelDescriptor] = Field(max_length=4096)
    config_revision: int = Field(ge=0)


class ModelsHealthParams(WireModel):
    provider: str = Field(min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    deadline: Rfc3339DateTime | None = None


class ModelsHealthResult(WireModel):
    provider: str = Field(min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    status: Literal["healthy", "degraded", "unreachable", "auth_required", "unsupported"]
    checked_at: Rfc3339DateTime
    latency_ms: int | None = Field(default=None, ge=0)
    error: ErrorEnvelope | None = None


class SessionCreateParams(WireModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)
    client_request_id: RequestId


class SessionCreateResult(WireModel):
    session: SessionSummary
    created: bool


class SessionListParams(WireModel):
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)
    limit: int = Field(default=50, ge=1, le=500)
    include_deleted: bool = False


class SessionListResult(WireModel):
    sessions: list[SessionSummary] = Field(max_length=500)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=1024)


class SessionGetParams(WireModel):
    session_id: SessionId
    include_turns: bool = True


class SessionDetail(WireModel):
    summary: SessionSummary
    turns: list[TurnSnapshot] = Field(default_factory=list, max_length=10_000)


class SessionGetResult(WireModel):
    session: SessionDetail


class SessionRenameParams(WireModel):
    session_id: SessionId
    title: str = Field(min_length=1, max_length=512)
    expected_updated_at: Rfc3339DateTime | None = None


class SessionRenameResult(WireModel):
    session: SessionSummary


class SessionDeleteParams(WireModel):
    session_id: SessionId
    hard_delete: bool = False


class SessionDeleteResult(WireModel):
    session_id: SessionId
    deleted: bool
    active_runs_cancel_requested: list[RunId] = Field(default_factory=list, max_length=1024)


class SessionForkParams(WireModel):
    session_id: SessionId
    fork_turn_id: TurnId
    fork_run_id: RunId | None = None
    title: str | None = Field(default=None, min_length=1, max_length=512)
    client_request_id: RequestId


class SessionForkResult(WireModel):
    session: SessionSummary
    source_session_id: SessionId
    fork_turn_id: TurnId


class SessionCompactParams(WireModel):
    session_id: SessionId
    through_turn_id: TurnId | None = None
    force: bool = False


class SessionCompactResult(WireModel):
    session_id: SessionId
    compacted: bool
    boundary_artifact: ArtifactRef | None = None
    replaced_turn_count: int = Field(ge=0)


class TurnStartParams(WireModel):
    session_id: SessionId
    turn_id: TurnId
    idempotency_key: str = Field(min_length=1, max_length=256)
    input: list[ContentBlock] = Field(min_length=1, max_length=256)
    client_context: ClientContextSnapshot | None = None
    run_config: RunConfigSnapshot
    deadline: Rfc3339DateTime | None = None


class TurnStartResult(WireModel):
    session_id: SessionId
    turn_id: TurnId
    run_id: RunId
    accepted: bool
    duplicate: bool = False


class TurnGetParams(WireModel):
    session_id: SessionId
    turn_id: TurnId


class TurnGetResult(WireModel):
    turn: TurnSnapshot


class TurnCancelParams(WireModel):
    session_id: SessionId
    turn_id: TurnId
    run_id: RunId | None = None
    reason: str = Field(min_length=1, max_length=4096)


class TurnCancelResult(WireModel):
    run_id: RunId
    accepted: bool
    already_terminal: bool


class TurnRetryParams(WireModel):
    session_id: SessionId
    turn_id: TurnId
    source_run_id: RunId
    idempotency_key: str = Field(min_length=1, max_length=256)
    run_config: RunConfigSnapshot | None = None


class TurnRetryResult(WireModel):
    session_id: SessionId
    turn_id: TurnId
    run_id: RunId
    accepted: bool
    duplicate: bool = False


class TurnSteerParams(WireModel):
    run_id: RunId
    message_id: MessageId
    input: list[ContentBlock] = Field(min_length=1, max_length=256)
    mode: Literal["append", "steer"] = "steer"


class TurnSteerResult(WireModel):
    run_id: RunId
    accepted: bool
    apply_after_sequence: int = Field(ge=0)


class ApprovalResolveParams(WireModel):
    approval_id: ApprovalId
    decision: ApprovalDecision
    scope: ApprovalScope
    expected_args_hash: Sha256Digest
    include_descendants: bool = False
    comment: str | None = Field(default=None, max_length=4096)


class ApprovalResolveResult(WireModel):
    approval_id: ApprovalId
    status: Literal["approved", "denied", "expired", "cancelled", "already_resolved"]
    run_id: RunId
    resumed: bool


class WorkspaceChangeKind(str, Enum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    RENAME = "rename"


class WorkspaceChange(WireModel):
    change_id: EventId
    kind: WorkspaceChangeKind
    path: RelativeVaultPath
    previous_path: RelativeVaultPath | None = None
    mtime_ns: int | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    content_hash: Sha256Digest | None = None
    transaction_id: TransactionId | None = None

    @model_validator(mode="after")
    def _rename_has_exact_previous_path(self) -> WorkspaceChange:
        if (self.kind == WorkspaceChangeKind.RENAME) != (self.previous_path is not None):
            raise ValueError("previousPath is required exactly for rename changes")
        return self


class WorkspaceDidChangeParams(WireModel):
    base_revision: int = Field(ge=0)
    revision: int = Field(ge=1)
    changes: list[WorkspaceChange] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def _revision_advances(self) -> WorkspaceDidChangeParams:
        if self.revision <= self.base_revision:
            raise ValueError("revision must be greater than baseRevision")
        return self


class WorkspaceDidChangeResult(WireModel):
    accepted_revision: int = Field(ge=0)
    rescan_required: bool
    duplicate_change_ids: list[EventId] = Field(default_factory=list, max_length=10_000)


class WorkspaceRescanParams(WireModel):
    reason: Literal["user", "revision_gap", "watcher_overflow", "corruption", "policy_changed"]
    full: bool = False


class WorkspaceRescanResult(WireModel):
    accepted: bool
    generation: int = Field(ge=1)


class WorkspaceContextChangedParams(WireModel):
    context_revision: int = Field(ge=1)
    context: ClientContextSnapshot


class WorkspaceContextChangedResult(WireModel):
    accepted_revision: int = Field(ge=0)


class AgentStatusParams(WireModel):
    run_id: RunId


class AgentStatusResult(WireModel):
    run: RunSnapshot
    child_run_ids: list[RunId] = Field(default_factory=list, max_length=10_000)


AgentResultInclude = Literal["summary", "findings", "evidence", "artifacts", "proposedActions", "usage"]


def _default_agent_result_include() -> list[AgentResultInclude]:
    return ["summary", "findings", "artifacts", "usage"]


class AgentResultParams(WireModel):
    run_id: RunId
    include: list[AgentResultInclude] = Field(default_factory=_default_agent_result_include, max_length=6)


class AgentResultResult(WireModel):
    result: SubagentResult


class AgentCancelParams(WireModel):
    run_id: RunId
    reason: str = Field(min_length=1, max_length=4096)
    cascade: bool = True


class AgentCancelResult(WireModel):
    run_id: RunId
    accepted: bool
    descendant_run_ids: list[RunId] = Field(default_factory=list, max_length=10_000)


class EventsReplayParams(WireModel):
    session_id: SessionId | None = None
    run_id: RunId | None = None
    after_sequence: int = Field(default=0, ge=0)
    limit: int = Field(default=1000, ge=1, le=10_000)
    types: list[EventType] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def _has_replay_scope(self) -> EventsReplayParams:
        if self.session_id is None and self.run_id is None:
            raise ValueError("sessionId or runId is required")
        return self


class EventsReplayResult(WireModel):
    events: list[EventEnvelope] = Field(max_length=10_000)
    last_sequence: int = Field(ge=0)
    has_more: bool


class ArtifactEncoding(str, Enum):
    UTF8 = "utf8"
    BASE64 = "base64"


class ArtifactReadParams(WireModel):
    artifact_id: ArtifactId
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=262_144, ge=1, le=1_048_576)


class ArtifactReadResult(WireModel):
    artifact: ArtifactRef
    offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    encoding: ArtifactEncoding
    content: str = Field(max_length=1_398_104)
    eof: bool


class DiagnosticsGetParams(WireModel):
    include_recent_errors: bool = True
    include_paths: bool = False


class DiagnosticProcessSnapshot(WireModel):
    role: Literal["host", "worker", "shell", "mcp", "parser"]
    pid: int = Field(ge=1)
    state: str = Field(min_length=1, max_length=128)
    owned: bool


class DiagnosticsGetResult(WireModel):
    generated_at: Rfc3339DateTime
    runtime: RuntimeStatusResult
    processes: list[DiagnosticProcessSnapshot] = Field(max_length=10_000)
    recent_errors: list[ErrorEnvelope] = Field(default_factory=list, max_length=1000)
    log_artifact: ArtifactRef | None = None


class ShutdownParams(WireModel):
    reason: Literal["user", "plugin_disabled", "upgrade", "system_shutdown", "idle_timeout"]
    grace_period_ms: int = Field(default=30_000, ge=0, le=300_000)


class ShutdownResult(WireModel):
    accepted: bool
    active_runs_cancel_requested: list[RunId] = Field(default_factory=list, max_length=10_000)


# Harness -> plugin reverse requests -------------------------------------------------


class ClientContextGetParams(WireModel):
    request_id: RequestId
    run_id: RunId
    fields: list[Literal["activeFile", "selection", "cursor", "metadata", "backlinks", "unsavedState"]] = Field(
        min_length=1,
        max_length=6,
    )
    deadline: Rfc3339DateTime


class ClientContextGetResult(WireModel):
    context: ClientContextSnapshot
    captured_at: Rfc3339DateTime


class ClientToolInvokeParams(WireModel):
    invocation_id: InvocationId
    tool_call_id: ToolCallId
    run_id: RunId
    name: str = Field(min_length=3, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    arguments: JsonObject
    args_hash: Sha256Digest
    idempotency_key: str = Field(min_length=1, max_length=256)
    deadline: Rfc3339DateTime
    trace_id: TraceId


class ClientActualOperation(WireModel):
    operation_id: str = Field(min_length=1, max_length=128)
    kind: Literal[
        "create",
        "append",
        "patch",
        "replace",
        "rename",
        "trash",
        "editor_insert",
        "editor_open",
        "editor_reveal",
    ]
    path: RelativeVaultPath
    destination_path: RelativeVaultPath | None = None
    before_hash: Sha256Digest | None = None
    after_hash: Sha256Digest | None = None
    applied: bool
    summary: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _rename_destination_is_exact(self) -> ClientActualOperation:
        if (self.kind == "rename") != (self.destination_path is not None):
            raise ValueError("destinationPath is required exactly for rename operations")
        return self


class ClientToolInvokeResult(WireModel):
    status: ToolCallStatus
    before_hash: Sha256Digest | None = None
    after_hash: Sha256Digest | None = None
    workspace_revision: int | None = Field(default=None, ge=0)
    artifact_ids: list[ArtifactId] = Field(default_factory=list, max_length=256)
    actual_operations: list[ClientActualOperation] = Field(default_factory=list, max_length=1024)
    error: ErrorEnvelope | None = None

    @model_validator(mode="after")
    def _status_has_consistent_error(self) -> ClientToolInvokeResult:
        failed = self.status in {
            ToolCallStatus.FAILED,
            ToolCallStatus.TIMED_OUT,
            ToolCallStatus.UNKNOWN_OUTCOME,
        }
        if failed and self.error is None:
            raise ValueError("failed/timed-out/unknown client tool results require an error")
        if self.status == ToolCallStatus.SUCCEEDED and self.error is not None:
            raise ValueError("succeeded client tool result cannot contain an error")
        return self


class ClientToolCancelParams(WireModel):
    invocation_id: InvocationId
    run_id: RunId
    reason: str = Field(min_length=1, max_length=4096)


class ClientToolCancelResult(WireModel):
    invocation_id: InvocationId
    accepted: bool
    already_terminal: bool


class ClientApprovalPresentParams(WireModel):
    approval_id: ApprovalId
    run_id: RunId
    title: str = Field(min_length=1, max_length=512)
    explanation: str = Field(min_length=1, max_length=8192)
    expires_at: Rfc3339DateTime
    diff_artifact: ArtifactRef | None = None


class ClientApprovalPresentResult(WireModel):
    approval_id: ApprovalId
    presented: bool


class CommandDirection(str, Enum):
    CLIENT_TO_WORKER = "client_to_worker"
    WORKER_TO_CLIENT = "worker_to_client"


@dataclass(frozen=True)
class CommandSpec:
    method: str
    params_model: type[WireModel]
    result_model: type[WireModel]
    direction: CommandDirection
    required_capability: CapabilityName | None = None


def _spec(
    method: str,
    params: type[WireModel],
    result: type[WireModel],
    *,
    capability: CapabilityName | None = None,
    direction: CommandDirection = CommandDirection.CLIENT_TO_WORKER,
) -> CommandSpec:
    return CommandSpec(method, params, result, direction, capability)


_COMMAND_SPECS = [
    _spec("initialize", InitializeParams, InitializeResult),
    _spec("runtime/ping", RuntimePingParams, RuntimePingResult),
    _spec("runtime/status", RuntimeStatusParams, RuntimeStatusResult),
    _spec("config/get", ConfigGetParams, ConfigSnapshot),
    _spec("config/update", ConfigUpdateParams, ConfigUpdateResult),
    _spec("models/list", ModelsListParams, ModelsListResult),
    _spec("models/health", ModelsHealthParams, ModelsHealthResult),
    _spec("session/create", SessionCreateParams, SessionCreateResult, capability=CapabilityName.MULTI_SESSION),
    _spec("session/list", SessionListParams, SessionListResult, capability=CapabilityName.MULTI_SESSION),
    _spec("session/get", SessionGetParams, SessionGetResult, capability=CapabilityName.MULTI_SESSION),
    _spec("session/rename", SessionRenameParams, SessionRenameResult, capability=CapabilityName.MULTI_SESSION),
    _spec("session/delete", SessionDeleteParams, SessionDeleteResult, capability=CapabilityName.MULTI_SESSION),
    _spec("session/fork", SessionForkParams, SessionForkResult, capability=CapabilityName.MULTI_SESSION),
    _spec("session/compact", SessionCompactParams, SessionCompactResult, capability=CapabilityName.MULTI_SESSION),
    _spec("turn/start", TurnStartParams, TurnStartResult),
    _spec("turn/get", TurnGetParams, TurnGetResult),
    _spec("turn/cancel", TurnCancelParams, TurnCancelResult, capability=CapabilityName.CANCELLATION),
    _spec("turn/retry", TurnRetryParams, TurnRetryResult),
    _spec("turn/steer", TurnSteerParams, TurnSteerResult),
    _spec("approval/resolve", ApprovalResolveParams, ApprovalResolveResult, capability=CapabilityName.APPROVALS),
    _spec(
        "workspace/didChange",
        WorkspaceDidChangeParams,
        WorkspaceDidChangeResult,
        capability=CapabilityName.WORKSPACE_CHANGES,
    ),
    _spec(
        "workspace/rescan", WorkspaceRescanParams, WorkspaceRescanResult, capability=CapabilityName.WORKSPACE_CHANGES
    ),
    _spec(
        "workspace/contextChanged",
        WorkspaceContextChangedParams,
        WorkspaceContextChangedResult,
        capability=CapabilityName.CLIENT_TOOLS,
    ),
    _spec("agent/status", AgentStatusParams, AgentStatusResult, capability=CapabilityName.SUBAGENTS),
    _spec("agent/result", AgentResultParams, AgentResultResult, capability=CapabilityName.SUBAGENTS),
    _spec("agent/cancel", AgentCancelParams, AgentCancelResult, capability=CapabilityName.SUBAGENTS),
    _spec("events/replay", EventsReplayParams, EventsReplayResult, capability=CapabilityName.EVENT_REPLAY),
    _spec("artifact/read", ArtifactReadParams, ArtifactReadResult, capability=CapabilityName.ARTIFACTS),
    _spec("diagnostics/get", DiagnosticsGetParams, DiagnosticsGetResult, capability=CapabilityName.DIAGNOSTICS),
    _spec("shutdown", ShutdownParams, ShutdownResult),
]

_REVERSE_REQUEST_SPECS = [
    _spec(
        "client/context/get",
        ClientContextGetParams,
        ClientContextGetResult,
        capability=CapabilityName.CLIENT_TOOLS,
        direction=CommandDirection.WORKER_TO_CLIENT,
    ),
    _spec(
        "client/tool/invoke",
        ClientToolInvokeParams,
        ClientToolInvokeResult,
        capability=CapabilityName.CLIENT_TOOLS,
        direction=CommandDirection.WORKER_TO_CLIENT,
    ),
    _spec(
        "client/tool/cancel",
        ClientToolCancelParams,
        ClientToolCancelResult,
        capability=CapabilityName.CANCELLATION,
        direction=CommandDirection.WORKER_TO_CLIENT,
    ),
    _spec(
        "client/approval/present",
        ClientApprovalPresentParams,
        ClientApprovalPresentResult,
        capability=CapabilityName.APPROVALS,
        direction=CommandDirection.WORKER_TO_CLIENT,
    ),
]


def _build_registry(specs: list[CommandSpec]) -> Mapping[str, CommandSpec]:
    registry: dict[str, CommandSpec] = {}
    for item in specs:
        if item.method in registry:
            raise RuntimeError(f"duplicate protocol method: {item.method}")
        registry[item.method] = item
    return MappingProxyType(registry)


COMMAND_REGISTRY = _build_registry(_COMMAND_SPECS)
REVERSE_REQUEST_REGISTRY = _build_registry(_REVERSE_REQUEST_SPECS)
ALL_METHOD_REGISTRY: Mapping[str, CommandSpec] = MappingProxyType({**COMMAND_REGISTRY, **REVERSE_REQUEST_REGISTRY})


def _validation_details(error: ValidationError) -> JsonObject:
    return {
        "violations": [
            {
                "path": ".".join(str(part) for part in item["loc"]),
                "type": item["type"],
                "message": item["msg"],
            }
            for item in error.errors(include_input=False, include_url=False)
        ]
    }


def command_spec(method: str, *, include_reverse: bool = True) -> CommandSpec:
    registry = ALL_METHOD_REGISTRY if include_reverse else COMMAND_REGISTRY
    try:
        return registry[method]
    except KeyError:
        raise protocol_error(
            ErrorCode.PROTOCOL_METHOD_NOT_FOUND,
            f"未知协议方法: {method}",
            details={"method": method},
        ) from None


def validate_command_params(method: str, value: object) -> WireModel:
    spec = command_spec(method)
    try:
        return validate_wire(spec.params_model, value)
    except ValidationError as error:
        raise protocol_error(
            ErrorCode.PROTOCOL_INVALID_PARAMS,
            f"方法 {method} 的参数不符合协议 Schema。",
            details=_validation_details(error),
        ) from None


def validate_command_result(method: str, value: object) -> WireModel:
    spec = command_spec(method)
    try:
        return validate_wire(spec.result_model, value)
    except ValidationError as error:
        raise protocol_error(
            ErrorCode.PROTOCOL_SCHEMA_MISMATCH,
            f"方法 {method} 的结果不符合协议 Schema。",
            details=_validation_details(error),
        ) from None


__all__ = (
    [
        "ALL_METHOD_REGISTRY",
        "COMMAND_REGISTRY",
        "REVERSE_REQUEST_REGISTRY",
        "CommandDirection",
        "CommandSpec",
        "command_spec",
        "validate_command_params",
        "validate_command_result",
    ]
    + [spec.params_model.__name__ for spec in ALL_METHOD_REGISTRY.values()]
    + [spec.result_model.__name__ for spec in ALL_METHOD_REGISTRY.values()]
)
