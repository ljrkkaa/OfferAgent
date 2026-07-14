"""Command and reverse-request DTOs for protocol v1."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import PureWindowsPath
from types import MappingProxyType
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, JsonValue, SecretStr, ValidationError, field_validator, model_validator

from offeragent_harness.foundation import vault_write_intent_hash

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
from .events import EventEnvelope, EventType, PersistedSideEffectFact
from .ids import (
    ApprovalId,
    ArtifactId,
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


class SkillDiagnosticSnapshot(WireModel):
    severity: Literal["warning", "error"]
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    message: str = Field(min_length=1, max_length=8192)
    root_id: str | None = Field(default=None, min_length=1, max_length=128)
    path: str | None = Field(default=None, min_length=1, max_length=2048)


class SkillCatalogStatusSnapshot(WireModel):
    revision: int = Field(ge=0)
    snapshot_hash: Sha256Digest
    discovered_count: int = Field(ge=0, le=100_000)
    enabled_count: int = Field(ge=0, le=100_000)
    partial: bool
    diagnostics: list[SkillDiagnosticSnapshot] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def _enabled_is_discovered(self) -> SkillCatalogStatusSnapshot:
        if self.enabled_count > self.discovered_count:
            raise ValueError("enabledCount cannot exceed discoveredCount")
        return self


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
    skills: SkillCatalogStatusSnapshot
    warnings: list[ErrorEnvelope] = Field(default_factory=list, max_length=256)


class WebLaunchParams(EmptyParams):
    pass


class WebLaunchResult(WireModel):
    url: str = Field(min_length=32, max_length=2048)
    worker_pid: int = Field(ge=1)
    workspace_instance_id: WorkspaceInstanceId
    expires_at: Rfc3339DateTime

    @model_validator(mode="after")
    def _launch_url_is_loopback_fragment_only(self) -> WebLaunchResult:
        parsed = urlsplit(self.url)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("Web launch URL port is invalid") from error
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or port is None
            or not 1024 <= port <= 65535
            or parsed.path != "/"
            or parsed.query
            or not parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Web launch URL must be a numeric loopback URL with a fragment-only token")
        return self


SecretKindValue = Literal["model-provider", "update"]


class SecretMetadataSnapshot(WireModel):
    handle: str = Field(pattern=r"^secret:v1:[0-9a-f]{32}$")
    kind: SecretKindValue
    provider_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    version: int = Field(ge=1)
    created_at: Rfc3339DateTime
    rotated_at: Rfc3339DateTime


class SecretsListParams(WireModel):
    kind: SecretKindValue | None = None
    provider_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")


class SecretsListResult(WireModel):
    secrets: list[SecretMetadataSnapshot] = Field(max_length=1024)


class SecretsPutParams(WireModel):
    provider_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    kind: SecretKindValue
    secret: SecretStr
    handle: str | None = Field(default=None, pattern=r"^secret:v1:[0-9a-f]{32}$")
    expected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _create_or_rotate_is_unambiguous(self) -> SecretsPutParams:
        if (self.handle is None) != (self.expected_version is None):
            raise ValueError("handle and expectedVersion are required together only for rotation")
        if not self.secret.get_secret_value() or "\x00" in self.secret.get_secret_value():
            raise ValueError("secret must be non-empty and cannot contain NUL")
        if len(self.secret.get_secret_value().encode("utf-8")) > 1_048_576:
            raise ValueError("secret exceeds the protected IPC input bound")
        return self


class SecretsPutResult(WireModel):
    secret: SecretMetadataSnapshot
    created: bool


class SecretsDeleteParams(WireModel):
    handle: str = Field(pattern=r"^secret:v1:[0-9a-f]{32}$")
    expected_version: int = Field(ge=1)


class SecretsDeleteResult(WireModel):
    handle: str = Field(pattern=r"^secret:v1:[0-9a-f]{32}$")
    deleted: Literal[True]


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


# Local extension administration ---------------------------------------------------


class SkillsListParams(EmptyParams):
    pass


class SkillSnapshot(WireModel):
    root_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9._-]{0,63}$")
    package_path: str = Field(min_length=1, max_length=2048)
    layer: Literal["builtin", "user", "workspace"]
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    description: str = Field(min_length=1, max_length=2048)
    metadata_hash: Sha256Digest
    trust_state: Literal["verified", "confirmed", "confirmation_required"]
    enabled: bool
    allowed_tools: list[str] = Field(default_factory=list, max_length=256)


class SkillsListResult(WireModel):
    workspace_id: WorkspaceId
    revision: int = Field(ge=0)
    snapshot_hash: Sha256Digest
    skills: list[SkillSnapshot] = Field(default_factory=list, max_length=1000)
    diagnostics: list[SkillDiagnosticSnapshot] = Field(default_factory=list, max_length=1000)


class SkillsStatusParams(EmptyParams):
    pass


class SkillsStatusResult(WireModel):
    status: SkillCatalogStatusSnapshot


class SkillsRescanParams(WireModel):
    client_request_id: RequestId
    expected_revision: int = Field(ge=0)


class SkillsConfirmTrustParams(WireModel):
    client_request_id: RequestId
    root_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9._-]{0,63}$")
    package_path: str = Field(min_length=1, max_length=2048)
    expected_metadata_hash: Sha256Digest
    expected_revision: int = Field(ge=0)
    confirmed: bool


class SkillsMutationResult(WireModel):
    client_request_id: RequestId
    applied: bool
    status: SkillCatalogStatusSnapshot


class ShellProfileInput(WireModel):
    profile_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=512)
    executable_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    executable_profile_fingerprint: Sha256Digest
    fixed_arguments: list[str] = Field(default_factory=list, max_length=128)
    minimum_variable_arguments: int = Field(default=0, ge=0, le=128)
    maximum_variable_arguments: int = Field(default=16, ge=0, le=128)
    variable_argument_pattern: str = Field(default=r"^[^\x00-\x1f\x7f&|<>^;`]{0,4096}$", min_length=1, max_length=4096)
    cwd_root_id: str = Field(default="vault", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    environment_profile_id: str = Field(
        default="minimal", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$"
    )
    timeout_ms: int = Field(default=30_000, ge=1, le=300_000)
    inline_output_limit_bytes: int = Field(default=65_536, ge=1, le=1_048_576)
    artifact_output_limit_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    allow_network: bool = False
    risk: Literal["read", "network", "write", "execute", "destructive", "external_path", "secret_access"]
    side_effect_class: Literal["none", "read", "network", "write", "execute", "destructive", "unknown"]
    concurrency_safe: bool = False
    idempotent: bool = False
    retryable: bool = False
    version: SemanticVersion = "1.0.0"

    @model_validator(mode="after")
    def _shell_profile_limits_are_coherent(self) -> ShellProfileInput:
        if self.minimum_variable_arguments > self.maximum_variable_arguments:
            raise ValueError("minimumVariableArguments cannot exceed maximumVariableArguments")
        if self.inline_output_limit_bytes > self.artifact_output_limit_bytes:
            raise ValueError("inline output limit cannot exceed Artifact output limit")
        if self.allow_network or self.risk == "network" or self.side_effect_class == "network":
            raise ValueError("Shell profiles cannot request network access")
        return self


class ShellProfileSnapshot(WireModel):
    profile: ShellProfileInput
    source: Literal["signed_builtin", "user"]
    trust: Literal["signed", "confirmed", "confirmation_required"]
    enabled: bool
    revision: int = Field(ge=1)
    content_hash: Sha256Digest


class ShellExecutableRegistrationSnapshot(WireModel):
    executable_id: str = Field(min_length=1, max_length=64)
    fingerprint: Sha256Digest
    fixed_arguments: list[str] = Field(default_factory=list, max_length=128)
    minimum_variable_arguments: int = Field(ge=0, le=128)
    maximum_variable_arguments: int = Field(ge=0, le=128)
    variable_argument_pattern: str = Field(min_length=1, max_length=4096)
    allowed_cwd_root_ids: list[str] = Field(min_length=1, max_length=64)
    environment_profile_ids: list[str] = Field(min_length=1, max_length=64)
    allowed_stdin_modes: list[Literal["closed", "fixed_payload", "duplex"]] = Field(
        min_length=1,
        max_length=3,
    )
    allow_network: bool


class ShellEnvironmentRegistrationSnapshot(WireModel):
    profile_id: str = Field(min_length=1, max_length=64)
    allowed_names: list[str] = Field(default_factory=list, max_length=256)
    allowed_secret_names: list[str] = Field(default_factory=list, max_length=256)


class ShellListParams(WireModel):
    include_disabled: bool = True


class ShellListResult(WireModel):
    workspace_id: WorkspaceId
    revision: int = Field(ge=1)
    snapshot_hash: Sha256Digest
    profiles: list[ShellProfileSnapshot] = Field(default_factory=list, max_length=256)
    executables: list[ShellExecutableRegistrationSnapshot] = Field(default_factory=list, max_length=128)
    environments: list[ShellEnvironmentRegistrationSnapshot] = Field(default_factory=list, max_length=128)


class ShellInstallParams(WireModel):
    client_request_id: RequestId
    expected_revision: int = Field(ge=0)
    profile: ShellProfileInput

    @model_validator(mode="after")
    def _user_profile_stays_approval_gated(self) -> ShellInstallParams:
        profile = self.profile
        if (
            profile.risk == "read"
            or profile.risk == "secret_access"
            or profile.side_effect_class in {"none", "read"}
            or profile.concurrency_safe
            or profile.retryable
        ):
            raise ValueError("user Shell profiles must remain approval-gated and non-concurrent")
        return self


class ShellConfirmParams(WireModel):
    client_request_id: RequestId
    profile_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    content_hash: Sha256Digest
    expected_revision: int = Field(ge=1)


class ShellSetEnabledParams(WireModel):
    client_request_id: RequestId
    profile_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    enabled: bool
    expected_revision: int = Field(ge=1)


class ShellMutationResult(WireModel):
    client_request_id: RequestId
    catalog_revision: int = Field(ge=1)
    snapshot_hash: Sha256Digest
    profile: ShellProfileSnapshot


class ProcessFilesystemRegistrationInput(WireModel):
    root_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    relative_path: str = Field(min_length=1, max_length=1024)
    access: Literal["read", "read_write"]

    @field_validator("relative_path")
    @classmethod
    def _narrow_relative_path(cls, value: str) -> str:
        if (
            "\\" in value
            or value.startswith("/")
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("Process filesystem registration requires a narrow root-relative path")
        return value


class ProcessExecutableRegistrationInput(WireModel):
    executable_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    executable_path: str = Field(min_length=3, max_length=32_767)
    fixed_arguments: list[str] = Field(default_factory=list, max_length=128)
    minimum_variable_arguments: int = Field(default=0, ge=0, le=128)
    maximum_variable_arguments: int = Field(default=0, ge=0, le=128)
    variable_argument_pattern: str = Field(
        default=r"^[^\x00-\x1f\x7f&|<>^;`]{0,4096}$",
        min_length=1,
        max_length=4096,
    )
    environment_profile_ids: list[str] = Field(min_length=1, max_length=128)
    allowed_stdin_modes: list[Literal["closed", "fixed_payload", "duplex"]] = Field(min_length=1, max_length=3)
    allowed_cwd_root_ids: list[str] = Field(min_length=1, max_length=128)
    appcontainer_filesystem: list[ProcessFilesystemRegistrationInput] = Field(min_length=1, max_length=128)
    allow_network: Literal[False] = False
    expected_revision: int = Field(default=0, ge=0)
    expected_content_hash: Sha256Digest | None = None

    @model_validator(mode="after")
    def _executable_registration_is_closed(self) -> ProcessExecutableRegistrationInput:
        path = PureWindowsPath(self.executable_path)
        if (
            not path.is_absolute()
            or not path.drive
            or path.root != "\\"
            or self.executable_path.startswith(("\\\\", "//"))
            or path.suffix.lower() != ".exe"
            or any(part in {"", ".", ".."} for part in path.parts[1:])
            or path.parent == PureWindowsPath(path.anchor)
        ):
            raise ValueError("Process executablePath must be an explicit local absolute .exe")
        if self.minimum_variable_arguments > self.maximum_variable_arguments:
            raise ValueError("minimumVariableArguments cannot exceed maximumVariableArguments")
        if any(not isinstance(item, str) or len(item) > 4096 or "\x00" in item for item in self.fixed_arguments):
            raise ValueError("Process fixedArguments are invalid")
        identity_lists = (
            self.environment_profile_ids,
            self.allowed_cwd_root_ids,
        )
        if any(
            len(set(values)) != len(values)
            or any(re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", item) is None for item in values)
            for values in identity_lists
        ):
            raise ValueError("Process registration profile IDs must be unique canonical IDs")
        if len(set(self.allowed_stdin_modes)) != len(self.allowed_stdin_modes):
            raise ValueError("Process allowedStdinModes must be unique")
        grants = {(item.root_id, item.relative_path) for item in self.appcontainer_filesystem}
        if len(grants) != len(self.appcontainer_filesystem):
            raise ValueError("Process AppContainer filesystem grants must be unique")
        if not set(self.allowed_cwd_root_ids) <= {item.root_id for item in self.appcontainer_filesystem}:
            raise ValueError("every Process cwd root requires a narrow AppContainer grant")
        if (self.expected_revision == 0) != (self.expected_content_hash is None):
            raise ValueError("expected Process executable revision/hash pair is invalid")
        return self


class ProcessEnvironmentRegistrationInput(WireModel):
    profile_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    allowed_names: list[str] = Field(default_factory=list, max_length=128)
    allowed_secret_names: list[str] = Field(default_factory=list, max_length=128)
    expected_revision: int = Field(default=0, ge=0)
    expected_content_hash: Sha256Digest | None = None

    @model_validator(mode="after")
    def _environment_registration_is_closed(self) -> ProcessEnvironmentRegistrationInput:
        names = [*self.allowed_names, *self.allowed_secret_names]
        folded = [item.upper() for item in names]
        if len(set(folded)) != len(folded) or any(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", item) is None for item in names
        ):
            raise ValueError("Process environment names must be case-insensitively unique canonical names")
        if (self.expected_revision == 0) != (self.expected_content_hash is None):
            raise ValueError("expected Process environment revision/hash pair is invalid")
        return self


class ProcessRegistrationsListParams(EmptyParams):
    pass


class ProcessRegistrationsProbeParams(WireModel):
    executable: ProcessExecutableRegistrationInput | None = None
    environment: ProcessEnvironmentRegistrationInput | None = None

    @model_validator(mode="after")
    def _one_probe_target(self) -> ProcessRegistrationsProbeParams:
        if (self.executable is None) == (self.environment is None):
            raise ValueError("Process registration probe requires exactly one executable or environment")
        return self


class ProcessExecutableRegistrationSnapshot(WireModel):
    executable_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    revision: int = Field(ge=1)
    content_hash: Sha256Digest
    canonical_path: str = Field(min_length=3, max_length=32_767)
    fixed_root: str = Field(min_length=3, max_length=32_767)
    trust: Literal["fixed_hash", "os_authenticode"]
    authenticode_verified: bool
    file_sha256: Sha256Digest
    file_device: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,31})$")
    file_index: str = Field(pattern=r"^[1-9][0-9]{0,31}$")
    file_size: int = Field(ge=1, le=2_147_483_648)
    profile_fingerprint: Sha256Digest
    fixed_arguments: list[str] = Field(default_factory=list, max_length=128)
    minimum_variable_arguments: int = Field(ge=0, le=128)
    maximum_variable_arguments: int = Field(ge=0, le=128)
    variable_argument_pattern: str = Field(min_length=1, max_length=4096)
    environment_profile_ids: list[str] = Field(min_length=1, max_length=128)
    allowed_stdin_modes: list[Literal["closed", "fixed_payload", "duplex"]] = Field(min_length=1, max_length=3)
    allowed_cwd_root_ids: list[str] = Field(min_length=1, max_length=128)
    appcontainer_filesystem: list[ProcessFilesystemRegistrationInput] = Field(min_length=1, max_length=128)
    allow_network: Literal[False] = False
    available: bool
    unavailable_reason: Literal["file_or_profile_drift", "configuration_drift"] | None = None

    @model_validator(mode="after")
    def _availability_has_reason(self) -> ProcessExecutableRegistrationSnapshot:
        if self.available == (self.unavailable_reason is not None):
            raise ValueError("Process executable availability reason is inconsistent")
        if self.authenticode_verified != (self.trust == "os_authenticode"):
            raise ValueError("Process executable Authenticode status and trust are inconsistent")
        return self


class ProcessEnvironmentRegistrationSnapshot(WireModel):
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    revision: int = Field(ge=1)
    content_hash: Sha256Digest
    allowed_names: list[str] = Field(default_factory=list, max_length=128)
    allowed_secret_names: list[str] = Field(default_factory=list, max_length=128)
    available: bool
    unavailable_reason: Literal["configuration_drift"] | None = None

    @model_validator(mode="after")
    def _availability_has_reason(self) -> ProcessEnvironmentRegistrationSnapshot:
        if self.available == (self.unavailable_reason is not None):
            raise ValueError("Process environment availability reason is inconsistent")
        return self


class ProcessExecutableProbeSnapshot(WireModel):
    executable_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    canonical_path: str = Field(min_length=3, max_length=32_767)
    fixed_root: str = Field(min_length=3, max_length=32_767)
    trust: Literal["fixed_hash", "os_authenticode"]
    authenticode_verified: bool
    file_sha256: Sha256Digest
    file_device: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,31})$")
    file_index: str = Field(pattern=r"^[1-9][0-9]{0,31}$")
    file_size: int = Field(ge=1, le=2_147_483_648)
    profile_fingerprint: Sha256Digest
    fixed_arguments: list[str] = Field(default_factory=list, max_length=128)
    minimum_variable_arguments: int = Field(ge=0, le=128)
    maximum_variable_arguments: int = Field(ge=0, le=128)
    variable_argument_pattern: str = Field(min_length=1, max_length=4096)
    environment_profile_ids: list[str] = Field(min_length=1, max_length=128)
    allowed_stdin_modes: list[Literal["closed", "fixed_payload", "duplex"]] = Field(min_length=1, max_length=3)
    allowed_cwd_root_ids: list[str] = Field(min_length=1, max_length=128)
    appcontainer_filesystem: list[ProcessFilesystemRegistrationInput] = Field(min_length=1, max_length=128)
    allow_network: Literal[False] = False

    @model_validator(mode="after")
    def _probe_argument_bounds_are_ordered(self) -> ProcessExecutableProbeSnapshot:
        if self.minimum_variable_arguments > self.maximum_variable_arguments:
            raise ValueError("Process executable probe variable argument bounds are invalid")
        return self


class ProcessEnvironmentProbeSnapshot(WireModel):
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    allowed_names: list[str] = Field(default_factory=list, max_length=128)
    allowed_secret_names: list[str] = Field(default_factory=list, max_length=128)


class ProcessRegistrationsListResult(WireModel):
    workspace_id: WorkspaceId
    catalog_revision: int = Field(ge=0)
    active_catalog_revision: int = Field(ge=0)
    snapshot_hash: Sha256Digest
    restart_required: bool
    executables: list[ProcessExecutableRegistrationSnapshot] = Field(default_factory=list, max_length=128)
    environments: list[ProcessEnvironmentRegistrationSnapshot] = Field(default_factory=list, max_length=128)


class ProcessRegistrationsProbeResult(WireModel):
    challenge_id: str = Field(pattern=r"^process-probe_[A-Za-z0-9_-]{16,128}$")
    kind: Literal["executable", "environment"]
    registration_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    content_hash: Sha256Digest
    expires_at: Rfc3339DateTime
    executable: ProcessExecutableProbeSnapshot | None = None
    environment: ProcessEnvironmentProbeSnapshot | None = None

    @model_validator(mode="after")
    def _probe_snapshot_matches_kind(self) -> ProcessRegistrationsProbeResult:
        if self.kind == "executable" and (self.executable is None or self.environment is not None):
            raise ValueError("executable Process probe requires exactly one executable snapshot")
        if self.kind == "environment" and (self.environment is None or self.executable is not None):
            raise ValueError("environment Process probe requires exactly one environment snapshot")
        return self


class ProcessRegistrationsConfirmParams(WireModel):
    challenge_id: str = Field(pattern=r"^process-probe_[A-Za-z0-9_-]{16,128}$")
    expected_probe_content_hash: Sha256Digest
    expected_catalog_revision: int = Field(ge=0)
    client_request_id: RequestId


class ProcessRegistrationsDeleteParams(WireModel):
    kind: Literal["executable", "environment"]
    registration_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    expected_catalog_revision: int = Field(ge=0)
    expected_revision: int = Field(ge=1)
    expected_content_hash: Sha256Digest
    client_request_id: RequestId


class ProcessRegistrationsMutationResult(WireModel):
    client_request_id: RequestId
    kind: Literal["executable", "environment"]
    registration_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    catalog_revision: int = Field(ge=1)
    snapshot_hash: Sha256Digest
    record_revision: int = Field(ge=1)
    record_content_hash: Sha256Digest
    deleted: bool
    restart_required: Literal[True]


class HookCommandInput(WireModel):
    executable_id: str = Field(min_length=1, max_length=128)
    arguments: list[str] = Field(default_factory=list, max_length=64)
    allowed_environment: list[str] = Field(default_factory=list, max_length=64)
    executable_profile_fingerprint: Sha256Digest
    cwd_root_id: str = Field(default="vault", min_length=1, max_length=128)
    cwd: str = Field(default="", max_length=1024)
    environment_profile_id: str = Field(default="minimal", min_length=1, max_length=128)
    artifact_output_limit_bytes: int = Field(default=1_048_576, ge=1, le=16_777_216)


class HookDefinitionInput(WireModel):
    hook_id: str = Field(min_length=1, max_length=256)
    event: Literal[
        "SessionStart",
        "TurnStart",
        "BeforeModel",
        "AfterModel",
        "PreToolUse",
        "PostToolUse",
        "ApprovalRequired",
        "SubagentStart",
        "SubagentStop",
        "BeforeCompact",
        "TurnStop",
        "RuntimeShutdown",
    ]
    implementation: Literal["builtin", "command"]
    priority: int = Field(default=0, ge=-1_000_000, le=1_000_000)
    timeout_ms: int = Field(default=5_000, ge=1, le=300_000)
    output_limit_bytes: int = Field(default=65_536, ge=1, le=1_048_576)
    enabled: bool = True
    handler_id: str | None = Field(default=None, min_length=1, max_length=256)
    command: HookCommandInput | None = None

    @model_validator(mode="after")
    def _implementation_is_closed(self) -> HookDefinitionInput:
        if self.implementation == "builtin":
            if self.handler_id is None or self.command is not None:
                raise ValueError("builtin Hook requires handlerId and forbids command")
        elif self.command is None or self.handler_id is not None:
            raise ValueError("command Hook requires command and forbids handlerId")
        return self


class HookLayerInput(WireModel):
    scope: Literal["user", "workspace", "session"]
    owner_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    hooks: list[HookDefinitionInput] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def _hook_ids_are_unique(self) -> HookLayerInput:
        if len({item.hook_id for item in self.hooks}) != len(self.hooks):
            raise ValueError("Hook layer contains duplicate hookId values")
        return self


class HookDefinitionSnapshot(HookDefinitionInput):
    definition_hash: Sha256Digest


class HookLayerSnapshot(WireModel):
    scope: Literal["managed", "user", "workspace", "session"]
    owner_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=0)
    hooks: list[HookDefinitionSnapshot] = Field(default_factory=list, max_length=256)
    denied_events: list[str] = Field(default_factory=list, max_length=32)
    denied_hook_ids: list[str] = Field(default_factory=list, max_length=256)
    trust: Literal["signed", "confirmed", "confirmation_required", "workspace_trust"]
    content_hash: Sha256Digest
    command_confirmations: dict[str, Sha256Digest] = Field(default_factory=dict)
    record_revision: int = Field(ge=1)


class HooksListParams(EmptyParams):
    pass


class HooksListResult(WireModel):
    workspace_id: WorkspaceId
    profile_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    snapshot_hash: Sha256Digest
    layers: list[HookLayerSnapshot] = Field(default_factory=list, max_length=512)
    builtin_handler_ids: list[str] = Field(default_factory=list, max_length=256)


class HooksInstallParams(WireModel):
    client_request_id: RequestId
    expected_revision: int = Field(ge=0)
    layer: HookLayerInput


class HooksConfirmLayerParams(WireModel):
    client_request_id: RequestId
    scope: Literal["user", "session"]
    owner_id: str = Field(min_length=1, max_length=256)
    content_hash: Sha256Digest
    expected_revision: int = Field(ge=1)


class HooksConfirmWorkspaceCommandParams(WireModel):
    client_request_id: RequestId
    owner_id: WorkspaceId
    hook_id: str = Field(min_length=1, max_length=256)
    definition_hash: Sha256Digest
    expected_revision: int = Field(ge=1)


class HooksMutationResult(WireModel):
    client_request_id: RequestId
    catalog_revision: int = Field(ge=1)
    snapshot_hash: Sha256Digest
    layer: HookLayerSnapshot


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
    client_request_id: RequestId
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


class NoWriteIntent(WireModel):
    kind: Literal["none"]


class VaultWriteRequiredIntent(WireModel):
    kind: Literal["vault_write_required"]
    target_paths: list[RelativeVaultPath] = Field(min_length=1, max_length=20)
    intent_hash: Sha256Digest

    @model_validator(mode="after")
    def _binding_is_canonical(self) -> VaultWriteRequiredIntent:
        canonical_paths = sorted(set(self.target_paths))
        if self.target_paths != canonical_paths:
            raise ValueError("write intent targetPaths must be sorted and unique")
        expected = vault_write_intent_hash(canonical_paths)
        if self.intent_hash != expected:
            raise ValueError("write intent intentHash does not match its canonical target binding")
        return self


TurnWriteIntent = Annotated[
    NoWriteIntent | VaultWriteRequiredIntent,
    Field(discriminator="kind"),
]


class TurnStartParams(WireModel):
    session_id: SessionId
    turn_id: TurnId
    idempotency_key: str = Field(min_length=1, max_length=256)
    input: list[ContentBlock] = Field(min_length=1, max_length=256)
    client_context: ClientContextSnapshot | None = None
    run_config: RunConfigSnapshot
    write_intent: TurnWriteIntent
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


class HeadlessVaultWriteState(str, Enum):
    READ_ONLY = "read_only"
    PIPE_CLIENT_TOOL = "pipe_client_tool"
    AMBIGUOUS_PIPE = "ambiguous_pipe"
    BASELINE_UNRELIABLE = "baseline_unreliable"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    ACTIVE = "active"
    CLAIMED = "claimed"
    DENIED = "denied"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CONSUMED = "consumed"


class HeadlessVaultWriteStatusParams(EmptyParams):
    pass


class HeadlessVaultWriteStatusResult(WireModel):
    state: HeadlessVaultWriteState
    pipe_connection_count: int = Field(ge=0, le=16)
    baseline_reliable: bool
    baseline_fingerprint: Sha256Digest
    approval_id: ApprovalId | None = None
    operation_id: str | None = Field(default=None, pattern=r"^op_headless_[0-9a-f]{32}$")
    args_hash: Sha256Digest | None = None
    revision: int = Field(ge=0)
    expires_at: Rfc3339DateTime | None = None
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    user_message: str = Field(min_length=1, max_length=1024)
    can_request: bool
    can_activate: bool
    can_revoke: bool

    @model_validator(mode="after")
    def _authorization_identity_is_complete(self) -> HeadlessVaultWriteStatusResult:
        values = (self.approval_id, self.operation_id, self.args_hash, self.expires_at)
        if any(value is not None for value in values) and not all(value is not None for value in values):
            raise ValueError("headless Vault authorization identity must be complete")
        if self.can_activate and self.state is not HeadlessVaultWriteState.APPROVED:
            raise ValueError("only an approved headless authorization can be activated")
        if self.can_revoke and self.approval_id is None:
            raise ValueError("revocable headless authorization requires an approval identity")
        return self


class HeadlessVaultWriteRequestParams(WireModel):
    client_request_id: RequestId
    confirmation: Literal["obsidian_closed_disk_authoritative"]
    ttl_seconds: int = Field(ge=60, le=3600)
    expected_baseline_fingerprint: Sha256Digest


class HeadlessVaultWriteActivateParams(WireModel):
    client_request_id: RequestId
    approval_id: ApprovalId
    expected_args_hash: Sha256Digest
    expected_revision: int = Field(ge=1)


class HeadlessVaultWriteRevokeParams(WireModel):
    client_request_id: RequestId
    approval_id: ApprovalId
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=512)


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
    run_id: RunId | None = None
    operation_id: str | None = Field(default=None, pattern=r"^op_[a-z][A-Za-z0-9_-]{0,119}$")
    resumed: bool

    @model_validator(mode="after")
    def _one_approval_target(self) -> ApprovalResolveResult:
        if (self.run_id is None) == (self.operation_id is None):
            raise ValueError("approval resolution requires exactly one Run or administrative operation target")
        if self.operation_id is not None and self.resumed:
            raise ValueError("administrative approval resolution cannot resume an Agent Run")
        return self


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
    run_cursors: dict[RunId, int] = Field(default_factory=dict, max_length=10_000)
    limit: int = Field(default=1000, ge=1, le=10_000)
    types: list[EventType] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def _has_replay_scope(self) -> EventsReplayParams:
        if (self.session_id is None) == (self.run_id is None):
            raise ValueError("exactly one of sessionId or runId is required")
        if self.run_id is not None and self.run_cursors:
            raise ValueError("runCursors is valid only for Session replay")
        if self.session_id is not None and self.after_sequence != 0:
            raise ValueError("afterSequence is valid only for one Run stream")
        if len(set(self.types)) != len(self.types):
            raise ValueError("event replay type filters must be unique")
        return self


class EventsReplayResult(WireModel):
    events: list[EventEnvelope] = Field(max_length=10_000)
    last_sequence: int | None = Field(default=None, ge=0)
    run_cursors: dict[RunId, int] = Field(default_factory=dict, max_length=10_000)
    has_more: bool

    @model_validator(mode="after")
    def _cursor_shape_matches_one_scope(self) -> EventsReplayResult:
        if self.last_sequence is not None and self.run_cursors:
            raise ValueError("one-Run replay cannot return Session run cursors")
        return self


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
    include_paths: Literal[False] = False


class DiagnosticProcessSnapshot(WireModel):
    role: Literal["host", "worker", "shell", "parser"]
    pid: int = Field(ge=1)
    state: str = Field(min_length=1, max_length=128)
    owned: bool


class DiagnosticsGetResult(WireModel):
    generated_at: Rfc3339DateTime
    runtime: JsonObject
    processes: list[DiagnosticProcessSnapshot] = Field(max_length=10_000)
    recent_errors: list[JsonObject] = Field(default_factory=list, max_length=1000)
    metrics: list[JsonObject] = Field(default_factory=list, max_length=10_000)


class DiagnosticsSnapshotParams(WireModel):
    include_recent_errors: bool = True


class DiagnosticsSnapshotResult(DiagnosticsGetResult):
    pass


class DiagnosticsExportPreviewParams(WireModel):
    include_recent_errors: bool = True


class DiagnosticsExportPreviewResult(WireModel):
    files: list[str] = Field(min_length=1, max_length=32)
    estimated_bytes: int = Field(ge=0)
    contains_paths: Literal[False]
    contains_content: Literal[False]
    upload_destination: None = None


class DiagnosticsExportParams(WireModel):
    owner_run_id: RunId
    include_recent_errors: bool = True


class DiagnosticsExportResult(WireModel):
    artifact: ArtifactRef
    uploaded: Literal[False] = False


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


class ClientToolPreviewParams(WireModel):
    invocation_id: InvocationId
    tool_call_id: ToolCallId
    run_id: RunId
    name: str = Field(min_length=3, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    arguments: JsonObject
    args_hash: Sha256Digest
    deadline: Rfc3339DateTime
    trace_id: TraceId


class ClientToolPathState(WireModel):
    path: RelativeVaultPath
    before_hash: Sha256Digest | Literal["absent"]
    after_hash: Sha256Digest | Literal["absent"]
    unsaved_editor: bool
    open_editor: bool

    @model_validator(mode="after")
    def _editor_state_is_consistent(self) -> ClientToolPathState:
        if self.unsaved_editor and not self.open_editor:
            raise ValueError("an unsaved Client Tool editor must also be open")
        return self


class ClientToolPreviewResult(WireModel):
    invocation_id: InvocationId
    tool_call_id: ToolCallId
    state_hash: Sha256Digest
    after_state_hash: Sha256Digest
    paths: list[RelativeVaultPath] = Field(min_length=1, max_length=64)
    diff: str = Field(min_length=1, max_length=4 * 1024 * 1024)
    diff_sha256: Sha256Digest
    has_unsaved_editors: bool
    has_open_editors: bool
    path_states: list[ClientToolPathState] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _preview_is_canonical(self) -> ClientToolPreviewResult:
        import hashlib

        if len(set(path.casefold() for path in self.paths)) != len(self.paths):
            raise ValueError("Client Tool preview paths must be case-insensitively unique")
        if [item.path for item in self.path_states] != self.paths:
            raise ValueError("Client Tool preview path states must exactly match its ordered paths")
        if self.has_unsaved_editors != any(item.unsaved_editor for item in self.path_states):
            raise ValueError("Client Tool preview unsaved-editor aggregate is inconsistent")
        if self.has_open_editors != any(item.open_editor for item in self.path_states):
            raise ValueError("Client Tool preview open-editor aggregate is inconsistent")
        digest = f"sha256:{hashlib.sha256(self.diff.encode('utf-8')).hexdigest()}"
        if digest != self.diff_sha256:
            raise ValueError("Client Tool preview diff hash does not match its UTF-8 bytes")
        return self


class ClientToolCommitObserveParams(WireModel):
    invocation_id: InvocationId
    tool_call_id: ToolCallId
    run_id: RunId
    paths: list[RelativeVaultPath] = Field(min_length=1, max_length=64)
    deadline: Rfc3339DateTime
    trace_id: TraceId

    @model_validator(mode="after")
    def _paths_are_unique(self) -> ClientToolCommitObserveParams:
        if len(set(path.casefold() for path in self.paths)) != len(self.paths):
            raise ValueError("Client Tool commit observation paths must be case-insensitively unique")
        return self


class ClientToolCommitPathState(WireModel):
    path: RelativeVaultPath
    observed_hash: Sha256Digest | Literal["absent"]
    unsaved_editor: bool
    open_editor: bool

    @model_validator(mode="after")
    def _editor_state_is_consistent(self) -> ClientToolCommitPathState:
        if self.unsaved_editor and not self.open_editor:
            raise ValueError("an unsaved Client Tool editor must also be open")
        return self


class ClientToolCommitObserveResult(WireModel):
    invocation_id: InvocationId
    tool_call_id: ToolCallId
    paths: list[RelativeVaultPath] = Field(min_length=1, max_length=64)
    has_unsaved_editors: bool
    has_open_editors: bool
    path_states: list[ClientToolCommitPathState] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _observation_is_canonical(self) -> ClientToolCommitObserveResult:
        if len(set(path.casefold() for path in self.paths)) != len(self.paths):
            raise ValueError("Client Tool commit observation paths must be case-insensitively unique")
        if [item.path for item in self.path_states] != self.paths:
            raise ValueError("Client Tool commit observation states must exactly match its ordered paths")
        if self.has_unsaved_editors != any(item.unsaved_editor for item in self.path_states):
            raise ValueError("Client Tool commit observation unsaved-editor aggregate is inconsistent")
        if self.has_open_editors != any(item.open_editor for item in self.path_states):
            raise ValueError("Client Tool commit observation open-editor aggregate is inconsistent")
        return self


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
    invocation_id: InvocationId
    tool_call_id: ToolCallId
    status: ToolCallStatus
    output: JsonValue | None = None
    user_visible_summary: str = Field(min_length=1, max_length=8192)
    before_hash: Sha256Digest | None = None
    after_hash: Sha256Digest | None = None
    before_state: JsonValue | None = None
    after_state: JsonValue | None = None
    workspace_revision: int | None = Field(default=None, ge=0)
    artifact_ids: list[ArtifactId] = Field(default_factory=list, max_length=256)
    source_reference_ids: list[str] = Field(default_factory=list, max_length=512)
    side_effect_facts: list[PersistedSideEffectFact] = Field(default_factory=list, max_length=256)
    actual_operations: list[ClientActualOperation] = Field(default_factory=list, max_length=1024)
    error: ClientToolError | None = None

    @model_validator(mode="after")
    def _status_has_consistent_error(self) -> ClientToolInvokeResult:
        failed = self.status is not ToolCallStatus.SUCCEEDED
        if failed and self.error is None:
            raise ValueError("non-success client tool results require an error")
        if self.status == ToolCallStatus.SUCCEEDED and self.error is not None:
            raise ValueError("succeeded client tool result cannot contain an error")
        if self.status is ToolCallStatus.UNKNOWN_OUTCOME and not any(
            item.state == "unknown" for item in self.side_effect_facts
        ):
            raise ValueError("unknown client tool outcome requires an unknown side-effect fact")
        return self


class ClientToolError(WireModel):
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    message: str = Field(min_length=1, max_length=4096)
    retryable: bool
    cancelled: bool
    details: JsonObject = Field(default_factory=dict)


class ClientToolLookupParams(WireModel):
    invocation_id: InvocationId
    run_id: RunId


class ClientToolLookupResult(WireModel):
    invocation_id: InvocationId
    found: bool
    result: ClientToolInvokeResult | None = None

    @model_validator(mode="after")
    def _found_has_exact_result(self) -> ClientToolLookupResult:
        if self.found != (self.result is not None):
            raise ValueError("lookup result is required exactly when found is true")
        if self.result is not None and self.result.invocation_id != self.invocation_id:
            raise ValueError("lookup invocation identity does not match the durable result")
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
    _spec("web/launch", WebLaunchParams, WebLaunchResult),
    _spec("secrets/list", SecretsListParams, SecretsListResult),
    _spec("secrets/put", SecretsPutParams, SecretsPutResult),
    _spec("secrets/delete", SecretsDeleteParams, SecretsDeleteResult),
    _spec("config/get", ConfigGetParams, ConfigSnapshot),
    _spec("config/update", ConfigUpdateParams, ConfigUpdateResult),
    _spec("skills/list", SkillsListParams, SkillsListResult, capability=CapabilityName.SKILLS),
    _spec("skills/status", SkillsStatusParams, SkillsStatusResult, capability=CapabilityName.SKILLS),
    _spec("skills/rescan", SkillsRescanParams, SkillsMutationResult, capability=CapabilityName.SKILLS),
    _spec(
        "skills/confirm-trust",
        SkillsConfirmTrustParams,
        SkillsMutationResult,
        capability=CapabilityName.SKILLS,
    ),
    _spec("shell/list", ShellListParams, ShellListResult, capability=CapabilityName.SHELL),
    _spec("shell/install", ShellInstallParams, ShellMutationResult, capability=CapabilityName.SHELL),
    _spec("shell/confirm", ShellConfirmParams, ShellMutationResult, capability=CapabilityName.SHELL),
    _spec("shell/set-enabled", ShellSetEnabledParams, ShellMutationResult, capability=CapabilityName.SHELL),
    _spec("process/registrations/list", ProcessRegistrationsListParams, ProcessRegistrationsListResult),
    _spec("process/registrations/probe", ProcessRegistrationsProbeParams, ProcessRegistrationsProbeResult),
    _spec(
        "process/registrations/confirm",
        ProcessRegistrationsConfirmParams,
        ProcessRegistrationsMutationResult,
    ),
    _spec(
        "process/registrations/delete",
        ProcessRegistrationsDeleteParams,
        ProcessRegistrationsMutationResult,
    ),
    _spec("hooks/list", HooksListParams, HooksListResult, capability=CapabilityName.HOOKS),
    _spec("hooks/install", HooksInstallParams, HooksMutationResult, capability=CapabilityName.HOOKS),
    _spec(
        "hooks/confirm-layer",
        HooksConfirmLayerParams,
        HooksMutationResult,
        capability=CapabilityName.HOOKS,
    ),
    _spec(
        "hooks/confirm-workspace-command",
        HooksConfirmWorkspaceCommandParams,
        HooksMutationResult,
        capability=CapabilityName.HOOKS,
    ),
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
    _spec(
        "vault/headless/status",
        HeadlessVaultWriteStatusParams,
        HeadlessVaultWriteStatusResult,
        capability=CapabilityName.HEADLESS_VAULT_WRITE,
    ),
    _spec(
        "vault/headless/request",
        HeadlessVaultWriteRequestParams,
        HeadlessVaultWriteStatusResult,
        capability=CapabilityName.HEADLESS_VAULT_WRITE,
    ),
    _spec(
        "vault/headless/activate",
        HeadlessVaultWriteActivateParams,
        HeadlessVaultWriteStatusResult,
        capability=CapabilityName.HEADLESS_VAULT_WRITE,
    ),
    _spec(
        "vault/headless/revoke",
        HeadlessVaultWriteRevokeParams,
        HeadlessVaultWriteStatusResult,
        capability=CapabilityName.HEADLESS_VAULT_WRITE,
    ),
    _spec("approval/resolve", ApprovalResolveParams, ApprovalResolveResult, capability=CapabilityName.APPROVALS),
    _spec("agent/status", AgentStatusParams, AgentStatusResult, capability=CapabilityName.SUBAGENTS),
    _spec("agent/result", AgentResultParams, AgentResultResult, capability=CapabilityName.SUBAGENTS),
    _spec("agent/cancel", AgentCancelParams, AgentCancelResult, capability=CapabilityName.SUBAGENTS),
    _spec("events/replay", EventsReplayParams, EventsReplayResult, capability=CapabilityName.EVENT_REPLAY),
    _spec("artifact/read", ArtifactReadParams, ArtifactReadResult, capability=CapabilityName.ARTIFACTS),
    _spec("diagnostics/get", DiagnosticsGetParams, DiagnosticsGetResult, capability=CapabilityName.DIAGNOSTICS),
    _spec(
        "diagnostics/snapshot",
        DiagnosticsSnapshotParams,
        DiagnosticsSnapshotResult,
        capability=CapabilityName.DIAGNOSTICS,
    ),
    _spec(
        "diagnostics/export-preview",
        DiagnosticsExportPreviewParams,
        DiagnosticsExportPreviewResult,
        capability=CapabilityName.DIAGNOSTICS,
    ),
    _spec(
        "diagnostics/export",
        DiagnosticsExportParams,
        DiagnosticsExportResult,
        capability=CapabilityName.DIAGNOSTICS,
    ),
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
        "client/tool/preview",
        ClientToolPreviewParams,
        ClientToolPreviewResult,
        capability=CapabilityName.CLIENT_TOOLS,
        direction=CommandDirection.WORKER_TO_CLIENT,
    ),
    _spec(
        "client/tool/commit-observe",
        ClientToolCommitObserveParams,
        ClientToolCommitObserveResult,
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
        "client/tool/lookup",
        ClientToolLookupParams,
        ClientToolLookupResult,
        capability=CapabilityName.CLIENT_TOOLS,
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
