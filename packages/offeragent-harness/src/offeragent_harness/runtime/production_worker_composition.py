"""Production composition for the one Vault-owned OfferAgent Worker.

This module is deliberately the only place that knows concrete adapters.  The
Host starts a process; this root constructs exactly one ``HarnessService`` and
shares its command dispatcher with both local transports.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import sys
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from offeragent_harness import __version__
from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.agent import BudgetLedger, RunBudget
from offeragent_harness.agent.context_manager import (
    ContextBudget,
    ContextFragment,
    ContextInputs,
    ContextLayer,
    ContextManager,
    ContextVisibilityPolicy,
)
from offeragent_harness.agent.loop import ToolKernel
from offeragent_harness.agent.model_composer import ComposerModelConfig, ModelComposer
from offeragent_harness.agent.model_planner import ModelPlanner, PlannerModelConfig, ToolPlanCatalog
from offeragent_harness.agent.state import RunState
from offeragent_harness.app import ApplicationIdentity, HarnessApplication
from offeragent_harness.config import ConfigPatch, ConfigScope, HarnessConfig, ModelProvider, ModelSettings
from offeragent_harness.hooks import HookDecision, HookEvent, HookInvocation, HookLayer, HookScope
from offeragent_harness.models import thaw_json
from offeragent_harness.observability import (
    DiagnosticsService,
    InstrumentedModelGateway,
    LocalJsonLogger,
    LocalRunCorrelationRegistry,
    LogLevel,
    MetricName,
    MetricsRegistry,
    ProductionRunObservability,
    ProductionToolObservability,
    TraceCorrelation,
)
from offeragent_harness.observability.diagnostics import DiagnosticProcess
from offeragent_harness.observability.models import DataClass, LogField
from offeragent_harness.permissions import (
    CapabilityScope,
    PermissionMode,
    RiskClass,
)
from offeragent_harness.permissions.audit import PolicyAuditSink
from offeragent_harness.permissions.evaluator import RuleBasedPolicyEvaluator
from offeragent_harness.permissions.rules import PolicyRule, RuleEffect
from offeragent_harness.ports import (
    ApplicationCommandContext,
    CancellationToken,
    Clock,
    EventSink,
    HookHandler,
    IdGenerator,
    ModelGateway,
    PolicyEvaluator,
    SecretStore,
    StoredEvent,
    ToolExecutor,
    UnitOfWorkFactory,
)
from offeragent_harness.ports.processes import ProcessSupervisor
from offeragent_harness.ports.skills import SkillTrustVerifier
from offeragent_harness.ports.subagents import (
    ChildRunExecution,
    ParentRunAuthority,
    ParentRunAuthorityProvider,
)
from offeragent_harness.ports.worker_runtime import WorkerApplication, WorkerBootstrap, WorkerCompositionRoot
from offeragent_harness.protocol._base import validate_wire
from offeragent_harness.protocol.capabilities import CapabilitySet, ProtocolRange
from offeragent_harness.protocol.common import PermissionMode as WirePermissionMode
from offeragent_harness.protocol.common import RunConfigSnapshot as WireRunConfigSnapshot
from offeragent_harness.protocol.events import stored_event_to_envelope
from offeragent_harness.protocol.messages import RuntimeArch, RuntimeStatusResult, ShutdownResult
from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.providers import compose_model_gateway
from offeragent_harness.runtime.application_dispatcher import (
    RuntimeApplicationCommandDispatcher,
)
from offeragent_harness.runtime.application_domain_handlers import (
    DomainCommandIdentity,
    compose_domain_command_handlers,
)
from offeragent_harness.runtime.application_handlers import (
    ApplicationRuntimeIdentity,
    ApplicationTransportPolicy,
    DiagnosticsOwnerRunAuthorizer,
    RunTransportRoute,
    SubagentArtifactReferenceResolver,
    SubagentCommandAuthority,
    SubagentCommandAuthorityResolver,
    compose_application_command_handlers,
)
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.runtime.backpressure import BufferedEventSink
from offeragent_harness.runtime.cancellation import CancellationScope
from offeragent_harness.runtime.client_tool_bridge import NamedPipeClientToolPort, ReverseRequestChannel
from offeragent_harness.runtime.client_vault_preflight import ClientBoundVaultTransaction
from offeragent_harness.runtime.codex_credentials import CodexFileCredentialSource
from offeragent_harness.runtime.config_service import ConfigService, ConfigUpdateCommand, WorkerConfigActivation
from offeragent_harness.runtime.conversation_controls import (
    CompactionExecution,
    ConversationControlService,
    SessionCompactionRunner,
)
from offeragent_harness.runtime.conversation_projection import UowConversationProjectionService
from offeragent_harness.runtime.extension_management_application_handlers import (
    extension_management_command_handlers,
)
from offeragent_harness.runtime.harness_service import (
    AsyncRunComponentsPreparationPort,
    ChildRunComponentsFactory,
    HarnessService,
    PreparedRunComponents,
    RunComponents,
    RunComponentsFactory,
    RunHookBinding,
    StartTurnCommand,
)
from offeragent_harness.runtime.headless_vault_write import (
    HeadlessAuthorizedVaultTransaction,
    HeadlessVaultWriteAuthority,
    headless_vault_write_handlers,
)
from offeragent_harness.runtime.host_supervisor import SupervisedWorkspaceIdentity
from offeragent_harness.runtime.loopback_gateway import LoopbackGatewayConfig, LoopbackWebGateway
from offeragent_harness.runtime.loopback_server import AsyncioLoopbackServer
from offeragent_harness.runtime.model_management import ProductionModelCommandService
from offeragent_harness.runtime.named_pipe import (
    ConnectionRole,
    DiscoveryMaterialStore,
    DuplexJsonRpcConnection,
    HandshakeReplayGuard,
    authenticate_server_stream,
)
from offeragent_harness.runtime.network_audit import EntityNetworkAuditSink
from offeragent_harness.runtime.policy_audit import EntityPolicyAuditSink
from offeragent_harness.runtime.process_registration import (
    WorkspaceProcessRegistrationService,
    merge_process_registration_snapshot,
)
from offeragent_harness.runtime.process_supervisor import (
    ProcessEnvironmentProfile,
    ProcessExecutableProfile,
    ProcessSupervisorService,
)
from offeragent_harness.runtime.production_hooks import (
    PreparedHookBundle,
    ProductionHookBundle,
    ProductionHookBundleFactory,
)
from offeragent_harness.runtime.production_process_catalog import load_production_process_catalog
from offeragent_harness.runtime.production_shell import (
    PreparedShellBundle,
    ProductionShellBundleFactory,
)
from offeragent_harness.runtime.production_skills import (
    PreparedSkillBundle,
    ProductionSkillBundleFactory,
    ReleaseManifestSkillTrustVerifier,
)
from offeragent_harness.runtime.recovery import RecoveryCoordinator
from offeragent_harness.runtime.recovery_apply import RecoveryPlanApplier
from offeragent_harness.runtime.release_trust import InstalledReleaseManifestTrust
from offeragent_harness.runtime.run_preparation import (
    CompositeRunContextProvider,
    ConversationHistoryRunPreparationAdapter,
    VaultMemoryRunPreparationAdapter,
    WorkspaceInstructionRunPreparationAdapter,
)
from offeragent_harness.runtime.startup import RuntimeStartupCoordinator
from offeragent_harness.runtime.subagent_runtime import (
    HarnessChildCancellationFactory,
    HarnessSubagentRunExecutor,
    ProtocolSubagentEventFactory,
)
from offeragent_harness.runtime.turn_manager import TurnManager
from offeragent_harness.runtime.windows_named_pipe import (
    DpapiCurrentUserProtector,
    Win32NamedPipeListener,
)
from offeragent_harness.runtime.windows_process import WindowsAuthenticodeVerifier
from offeragent_harness.runtime.windows_process_supervisor import (
    PinnedProcessExecutableVerifier,
    ProcessReleaseManifestTrust,
    WindowsSupervisedProcessBackend,
)
from offeragent_harness.runtime.windows_secrets import WindowsDpapiSecretStore
from offeragent_harness.runtime.worker_control import WorkerControlHandler, WorkerControlServer
from offeragent_harness.sessions import Run, Session
from offeragent_harness.shell import PowerShellToolExecutor, ShellCommandProfile
from offeragent_harness.skills import SkillAuthority
from offeragent_harness.skills.tools import skill_tool_definitions
from offeragent_harness.subagents import (
    AgentDefinitionCatalog,
    AgentDefinitionLayer,
    AgentDefinitionRoot,
    ChildRunScheduler,
    CompositeParentRunAuthorityProvider,
    ContextForker,
    DurableMailbox,
    RunRecoverySupervisor,
    ScopeDeriver,
    SubagentBudgetTree,
    SubagentResultArtifactManager,
    SubagentScopePolicy,
    SubagentService,
    SubagentToolExecutor,
    builtin_agent_definitions,
)
from offeragent_harness.subagents.models import AgentBudget, SubagentRunRecord
from offeragent_harness.tools import (
    ExecutorLocation,
    PreflightRegistry,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolValidator,
    canonical_json_bytes,
    canonical_json_sha256,
)
from offeragent_harness.tools.artifacts import ToolArtifactManager
from offeragent_harness.tools.dispatcher import ToolDispatcher
from offeragent_harness.tools.kernel import KeyedLockPool, UnifiedToolKernel
from offeragent_harness.tools.registry import ToolRegistry
from offeragent_harness.tools.scheduler import FairEffectGate, ToolScheduler
from offeragent_harness.vault import (
    VaultCasBarrier,
    VaultTransactionCoordinator,
    client_vault_transaction_definition,
    legacy_public_vault_transaction_definition,
    legacy_vault_transaction_definition,
    vault_transaction_definition,
)
from offeragent_harness.workspace import (
    CodeToolExecutor,
    VaultFileSystem,
    VaultReadPolicy,
    WorkspacePathPolicy,
    WorkspaceRegistry,
    WorkspaceRoot,
    identify_workspace_root,
)
from offeragent_harness.workspace.portable_config import read_portable_workspace_config
from offeragent_harness.workspace.runtime_identity import workspace_database_identity

if TYPE_CHECKING:
    from offeragent_harness.runtime.development_runtime_manifest import InstalledDevelopmentRuntimeTrust
    from offeragent_harness.runtime.host_supervisor import WorkerShutdownReceipt

PRODUCTION_WORKER_COMPOSITION_COMPLETE = True

_WORKER_ARGUMENTS = (
    "--offeragent-runtime-mode",
    "worker",
    "--transport",
    "named-pipe",
    "--workspace-instance-id",
)
_INSTANCE_ID = re.compile(r"^wsi_[0-9a-f-]{36}$")
_LOCAL_PROFILE_ID = "profile_local"
_LOCAL_MANAGED_ID = "managed_local"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class ProductionWorkerError(RuntimeError):
    """Fail-closed Worker bootstrap/composition error."""


class _WorkerProcessSupervisor(ProcessSupervisor, Protocol):
    async def shutdown(self) -> None: ...


class SystemClock:
    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep_until(self, deadline: datetime) -> None:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")
        await asyncio.sleep(max(0.0, (deadline - self.utcnow()).total_seconds()))


class SecureIdGenerator:
    """Opaque production IDs; namespaces remain visible but values never encode data."""

    def new_id(self, namespace: str) -> str:
        if re.fullmatch(r"[a-z][a-z0-9-]*", namespace) is None:
            raise ValueError("ID namespace must be lowercase ASCII")
        aliases = {"event": "evt", "model-request": "req", "artifact": "art", "idempotency": "idem"}
        prefix = aliases.get(namespace, namespace)
        return f"{prefix}_{secrets.token_hex(16)}"


class ModelGatewayFactory(Protocol):
    def __call__(self, settings: ModelSettings) -> ModelGateway: ...


@dataclass(frozen=True, slots=True)
class ProductionWorkerOverrides:
    """Narrow dependency seam for deterministic production-smoke tests."""

    clock: Clock | None = None
    ids: IdGenerator | None = None
    model_gateway_factory: ModelGatewayFactory | None = None
    secret_store: SecretStore | None = None
    start_native_transports: bool = True
    host_pid: int | None = None
    runtime_config: HarnessConfig | None = None
    skill_trust_verifier: SkillTrustVerifier | None = None
    skill_runtime_root: Path | None = None
    ripgrep_path: Path | None = None
    powershell_path: Path | None = None
    skill_user_home: Path | None = None
    process_supervisor: _WorkerProcessSupervisor | None = None
    process_executable_profiles: tuple[ProcessExecutableProfile, ...] = ()
    process_environment_profiles: tuple[ProcessEnvironmentProfile, ...] = ()
    process_release_manifest: ProcessReleaseManifestTrust | None = None
    process_registration_service: WorkspaceProcessRegistrationService | None = None
    signed_shell_profiles: tuple[ShellCommandProfile, ...] = ()
    managed_hook_layer: HookLayer | None = None
    builtin_hook_handlers: Mapping[str, HookHandler] | None = None
    vault_cas_barrier: VaultCasBarrier | None = None


class _EventHub(EventSink):
    def __init__(self) -> None:
        self._connections: set[DuplexJsonRpcConnection] = set()
        self._lock = asyncio.Lock()

    async def publish(self, events: Sequence[StoredEvent]) -> None:
        async with self._lock:
            targets = tuple(self._connections)
        if not targets or not events:
            return
        failed: set[DuplexJsonRpcConnection] = set()
        for connection in targets:
            if not connection.ready:
                continue
            for event in events:
                try:
                    await connection.send_event(stored_event_to_envelope(event))
                except Exception:
                    failed.add(connection)
                    break
        if failed:
            async with self._lock:
                self._connections.difference_update(failed)

    async def add(self, connection: DuplexJsonRpcConnection) -> None:
        async with self._lock:
            self._connections.add(connection)

    async def remove(self, connection: DuplexJsonRpcConnection) -> None:
        async with self._lock:
            self._connections.discard(connection)


@dataclass(slots=True)
class _ReverseChannelEntry:
    channel: ReverseRequestChannel
    lease_count: int = 0
    retiring: bool = False
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    retirement: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        self.drained.set()


class _ReverseChannels:
    def __init__(self, workspace_id: str) -> None:
        self._workspace_id = workspace_id
        self._channels: dict[str, _ReverseChannelEntry] = {}
        self._retiring: dict[str, _ReverseChannelEntry] = {}
        self._membership_lock = asyncio.Lock()
        self._retirement_tasks: set[asyncio.Task[None]] = set()
        self._headless_authority: HeadlessVaultWriteAuthority | None = None

    def bind_headless_authority(self, authority: HeadlessVaultWriteAuthority) -> None:
        if self._headless_authority is not None:
            raise RuntimeError("headless Vault authority is already bound")
        if authority.workspace_id != self._workspace_id:
            raise ValueError("headless Vault authority belongs to another Workspace")
        self._headless_authority = authority

    def channel(self, workspace_id: str) -> ReverseRequestChannel:
        if workspace_id != self._workspace_id or len(self._channels) != 1:
            raise RuntimeError("Client Tool channel requires an explicit owning connection")
        return next(iter(self._channels.values())).channel

    def channel_for(self, workspace_id: str, client_connection_id: str) -> ReverseRequestChannel:
        if workspace_id != self._workspace_id:
            raise RuntimeError("Client Tool channel belongs to a different Workspace")
        entry = self._channels.get(client_connection_id)
        if entry is None:
            raise RuntimeError("authenticated Obsidian Client Tool channel is unavailable")
        return entry.channel

    def binding_snapshot(self, client_connection_id: str) -> _ReverseChannelEntry | None:
        """Freeze the current connection generation, including its absence.

        A run whose authenticated connection disappeared before its Tool Kernel
        was built may still execute unrelated local tools.  Freezing ``None``
        makes every later Client Tool request fail closed and, importantly,
        prevents a new connection reusing the same public id from acquiring the
        abandoned run's authority.
        """

        return self._channels.get(client_connection_id)

    def channel_for_binding(
        self,
        workspace_id: str,
        client_connection_id: str,
        binding: _ReverseChannelEntry,
    ) -> ReverseRequestChannel:
        if workspace_id != self._workspace_id:
            raise RuntimeError("Client Tool channel belongs to a different Workspace")
        entry = self._channels.get(client_connection_id)
        if entry is not binding:
            raise RuntimeError("authenticated Obsidian Client Tool channel binding is unavailable")
        return entry.channel

    @asynccontextmanager
    async def connection_lease_for(
        self,
        workspace_id: str,
        client_connection_id: str,
        binding: _ReverseChannelEntry,
    ) -> AsyncIterator[ReverseRequestChannel]:
        """Linearize a commit lease against disconnect and connection reuse.

        Disconnect retirement removes an entry from all new routing before it
        waits for existing leases.  Therefore either disconnect wins and this
        acquisition fails without a write, or this lease wins and preserves the
        exact authenticated channel authority through commit/rollback/cleanup.
        """

        if workspace_id != self._workspace_id:
            raise RuntimeError("Client Tool channel belongs to a different Workspace")
        async with self._membership_lock:
            entry = self._channels.get(client_connection_id)
            if entry is not binding or entry.retiring:
                raise RuntimeError("authenticated Obsidian Client Tool connection lease is unavailable")
            entry.lease_count += 1
            entry.drained.clear()
        try:
            yield entry.channel
        finally:
            # No await here: lease release remains cancellation-safe and is
            # atomic with the retirement task on this event loop.
            entry.lease_count -= 1
            if entry.lease_count < 0:
                raise AssertionError("Client Tool connection lease count underflow")
            if entry.lease_count == 0:
                entry.drained.set()

    def contains(self, client_connection_id: str) -> bool:
        return client_connection_id in self._channels

    def sole_connection_id(self) -> str | None:
        if len(self._channels) != 1:
            return None
        return next(iter(self._channels))

    async def add(self, client_connection_id: str, value: ReverseRequestChannel) -> None:
        authority = self._headless_authority
        if authority is None:
            raise RuntimeError("headless Vault authority is not bound")
        async with authority.pipe_registration():
            async with self._membership_lock:
                if client_connection_id in self._channels or client_connection_id in self._retiring:
                    raise RuntimeError("authenticated client connection identity is duplicated or retiring")
                if len(self._channels) >= 16:
                    raise RuntimeError("authenticated Pipe connection limit exceeded")
                self._channels[client_connection_id] = _ReverseChannelEntry(value)

    async def remove(self, client_connection_id: str, value: ReverseRequestChannel) -> None:
        retirement: asyncio.Task[None] | None
        async with self._membership_lock:
            entry = self._channels.get(client_connection_id)
            if entry is not None and entry.channel is value:
                # Removal linearizes here.  No new route or lease can observe
                # this authority, while an already-acquired lease retains the
                # exact entry until its critical section finishes.
                del self._channels[client_connection_id]
                entry.retiring = True
                self._retiring[client_connection_id] = entry
                retirement = asyncio.create_task(
                    self._finish_retirement(client_connection_id, entry),
                    name=f"client-channel-retire-{client_connection_id}",
                )
                entry.retirement = retirement
                self._retirement_tasks.add(retirement)
                retirement.add_done_callback(self._retirement_tasks.discard)
            else:
                entry = self._retiring.get(client_connection_id)
                if entry is None or entry.channel is not value:
                    return
                retirement = entry.retirement
        if retirement is not None:
            await asyncio.shield(retirement)

    async def _finish_retirement(self, client_connection_id: str, entry: _ReverseChannelEntry) -> None:
        await entry.drained.wait()
        async with self._membership_lock:
            if self._retiring.get(client_connection_id) is entry:
                del self._retiring[client_connection_id]
            entry.retirement = None

    @property
    def active(self) -> bool:
        return bool(self._channels)

    @property
    def count(self) -> int:
        return len(self._channels)


class _ProductionApplicationTransportPolicy(ApplicationTransportPolicy):
    """Select one authenticated Pipe or one explicit headless authorization."""

    def __init__(self, channels: _ReverseChannels, headless: HeadlessVaultWriteAuthority) -> None:
        self._channels = channels
        self._headless = headless

    async def resolve_run_route(
        self,
        context: ApplicationCommandContext,
        requested_permission: WirePermissionMode,
        *,
        request_hash: str,
        headless_eligible: bool,
    ) -> RunTransportRoute:
        if requested_permission in {WirePermissionMode.READ_ONLY, WirePermissionMode.PLAN}:
            return RunTransportRoute(requested_permission)
        if context.transport == "windows-named-pipe":
            if self._channels.count != 1 or self._channels.sole_connection_id() != context.client_id:
                return RunTransportRoute(WirePermissionMode.READ_ONLY)
            return RunTransportRoute(requested_permission, client_connection_id=context.client_id)
        if context.transport in {"loopback-http", "loopback-websocket"}:
            client_connection_id = self._channels.sole_connection_id()
            if client_connection_id is not None:
                return RunTransportRoute(
                    requested_permission,
                    client_connection_id=client_connection_id,
                )
            if self._channels.count == 0 and headless_eligible:
                grant_id = await self._headless.claim_for_turn(context.client_id, request_hash)
                if grant_id is not None:
                    return RunTransportRoute(
                        requested_permission,
                        local_vault_write_grant_id=grant_id,
                    )
        return RunTransportRoute(WirePermissionMode.READ_ONLY)


class _BoundReverseChannels:
    def __init__(self, channels: _ReverseChannels, client_connection_id: str) -> None:
        self._channels = channels
        self._client_connection_id = client_connection_id
        self._binding = channels.binding_snapshot(client_connection_id)

    def channel(self, workspace_id: str) -> ReverseRequestChannel:
        if self._binding is None:
            raise RuntimeError("authenticated Obsidian Client Tool channel binding is unavailable")
        return self._channels.channel_for_binding(workspace_id, self._client_connection_id, self._binding)

    def connection_lease(
        self,
        workspace_id: str,
    ) -> AbstractAsyncContextManager[ReverseRequestChannel]:
        if self._binding is None:
            raise RuntimeError("authenticated Obsidian Client Tool channel binding is unavailable")
        return self._channels.connection_lease_for(workspace_id, self._client_connection_id, self._binding)


class _CompositeExecutor(ToolExecutor):
    def __init__(self, executors: Sequence[tuple[Sequence[ToolDefinition], ToolExecutor]]) -> None:
        routes: dict[tuple[str, str], tuple[str, ToolExecutor]] = {}
        for definitions, executor in executors:
            for definition in definitions:
                key = (definition.name, definition.version)
                if key in routes:
                    raise ValueError(f"duplicate local executor route {key!r}")
                routes[key] = (definition.fingerprint, executor)
        self._routes = routes

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        route = self._routes.get((call.name, call.version))
        if route is None or route[0] != call.definition_fingerprint:
            raise ValueError("ToolCall is not bound to this immutable executor snapshot")
        return await route[1].execute(call, cancellation)


class _FingerprintDefinitionResolver:
    def __init__(self, definitions: Sequence[ToolDefinition]) -> None:
        grouped: dict[tuple[str, str], list[ToolDefinition]] = {}
        for definition in definitions:
            grouped.setdefault((definition.name, definition.version), []).append(definition)
        self._definitions = {key: tuple(value) for key, value in grouped.items()}

    def resolve_definition(self, call: ToolCall) -> ToolDefinition:
        candidates = self._definitions.get((call.name, call.version), ())
        if not candidates:
            from offeragent_harness.tools.registry import ToolNotFound

            raise ToolNotFound(call.name)
        return next(
            (item for item in candidates if item.fingerprint == call.definition_fingerprint),
            candidates[0],
        )


@dataclass(frozen=True, slots=True)
class _PreparedProductionCapabilities:
    run_id: str
    config: WireRunConfigSnapshot
    inputs: ContextInputs
    effective_config: HarnessConfig
    client_connection_id: str | None
    local_vault_write_grant_id: str | None
    budget: RunBudget
    permission: PermissionMode
    scope: CapabilityScope
    definitions: tuple[ToolDefinition, ...]
    base_definitions: tuple[ToolDefinition, ...]
    skill_definitions: tuple[ToolDefinition, ...]
    shell_definitions: tuple[ToolDefinition, ...]
    skills: PreparedSkillBundle | None
    shell: PreparedShellBundle | None
    hooks: PreparedHookBundle | None
    parent_snapshot_fingerprint: str | None = None


class ProductionRunComponentsFactory(
    RunComponentsFactory,
    ChildRunComponentsFactory,
    AsyncRunComponentsPreparationPort,
):
    """Build per-Run model/context/kernel snapshots without creating another loop."""

    def __init__(
        self,
        *,
        workspace_id: str,
        clock: Clock,
        ids: IdGenerator,
        gateway_factory: ModelGatewayFactory,
        default_config: HarnessConfig,
        approvals: ApprovalManager,
        policy_audit: PolicyAuditSink,
        journal: Any,
        artifacts: LocalArtifactStore,
        local_read: CodeToolExecutor,
        local_transaction: VaultTransactionCoordinator,
        headless_vault_write: HeadlessVaultWriteAuthority,
        channels: _ReverseChannels,
        parent_authorities: ParentRunAuthorityProvider,
        optional_definitions: Sequence[ToolDefinition] = (),
        optional_local_executors: Sequence[tuple[Sequence[ToolDefinition], ToolExecutor]] = (),
        subagent_executor: ToolExecutor | None = None,
        skills: ProductionSkillBundleFactory | None = None,
        shell: ProductionShellBundleFactory | None = None,
        process_root_ids: Sequence[str] = ("vault",),
        hooks: ProductionHookBundleFactory | None = None,
        hook_unit_of_work: UnitOfWorkFactory | None = None,
        lifecycle_budget: BudgetLedger | None = None,
        tool_observability: ProductionToolObservability | None = None,
        run_correlations: LocalRunCorrelationRegistry | None = None,
        run_observability: ProductionRunObservability | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self._clock = clock
        self._ids = ids
        self._gateway_factory = gateway_factory
        self._default_config = default_config
        self._approvals = approvals
        self._policy_audit = policy_audit
        self._journal = journal
        self._artifacts = artifacts
        self._local_read = local_read
        self._local_transaction = local_transaction
        self._headless_vault_write = headless_vault_write
        self._channels = channels
        self._parent_authorities = parent_authorities
        self._optional_definitions = tuple(optional_definitions)
        self._optional_local_executors = tuple(optional_local_executors)
        self._subagent_executor = subagent_executor
        self._skills = skills
        self._shell = shell
        self._process_root_ids = tuple(process_root_ids)
        self._hooks = hooks
        self._hook_unit_of_work = hook_unit_of_work
        self._lifecycle_budget = lifecycle_budget
        self._tool_observability = tool_observability
        self._run_correlations = run_correlations
        self._run_observability = run_observability
        if (hooks is None) != (hook_unit_of_work is None) or (hooks is None) != (lifecycle_budget is None):
            raise ValueError(
                "production Hook factory, principal store and lifecycle budget must be configured together"
            )
        self._registries: dict[str, ToolRegistry] = {}
        self._recent_registries: OrderedDict[str, ToolRegistry] = OrderedDict()
        self._root_ledgers: dict[str, BudgetLedger] = {}
        self._effective_configs: dict[str, HarnessConfig] = {}
        self._client_connections: dict[str, str] = {}
        self._headless_grants: dict[str, str] = {}
        self._prepared_runs: dict[str, _PreparedProductionCapabilities] = {}
        self._bound_hook_bundles: dict[str, ProductionHookBundle] = {}
        self._effect_gate = FairEffectGate(default_config.budgets.max_parallel_reads)
        self._effect_gate_bound = False
        self._lock_pool = KeyedLockPool()
        self._run_budgets: dict[str, RunBudget] = {}

    def budget_root(self, command: StartTurnCommand, state: RunState) -> RunBudget:
        del state
        config = validate_wire(WireRunConfigSnapshot, thaw_json(command.run_config))
        effective_config = command.effective_config or self._default_config
        self._ensure_worker_read_limit(effective_config)
        return _run_budget(
            config,
            effective_config,
            worker_max_parallel_reads=self._effect_gate.max_readers,
        )

    async def prepare_root(
        self,
        command: StartTurnCommand,
        state: RunState,
        cancellation: CancellationScope,
        durable_snapshot: Mapping[str, Any] | None,
    ) -> PreparedRunComponents:
        cancellation.checkpoint()
        config = validate_wire(WireRunConfigSnapshot, thaw_json(command.run_config))
        effective_config = command.effective_config or self._default_config
        self._ensure_worker_read_limit(effective_config)
        permission = _effective_permission(config, effective_config)
        hooks: PreparedHookBundle | None = None
        if self._hooks is not None:
            hook_recovery = _prepared_hook_recovery(durable_snapshot)
            hook_principal_id = (
                await self._principal_id(state.session_id)
                if effective_config.extensibility.hooks_enabled
                else _LOCAL_PROFILE_ID
            )
            hooks = await self._hooks.prepare(
                run_id=state.run_id,
                principal_id=hook_principal_id,
                session_id=state.session_id,
                effective_config=effective_config,
                cancellation=cancellation,
                durable_snapshot=hook_recovery,
            )
        base_definitions = self._base_definitions(
            config,
            effective_config,
            client_connection_id=command.client_connection_id,
            local_vault_write_grant_id=command.local_vault_write_grant_id,
            permission=permission,
        )
        shell: PreparedShellBundle | None = None
        shell_definitions: tuple[ToolDefinition, ...] = ()
        if self._shell is not None:
            shell = await self._shell.prepare(
                effective_config,
                self._process_root_ids,
                cancellation,
            )
            shell_definitions = self._shell.definitions_for(shell)
        skill_candidates = skill_tool_definitions() if self._skills is not None else ()
        candidate_definitions = (
            *base_definitions,
            *skill_candidates,
            *shell_definitions,
        )
        candidate_scope = _effective_capability_scope(candidate_definitions, config, effective_config, permission)
        skills: PreparedSkillBundle | None = None
        skill_definitions: tuple[ToolDefinition, ...] = ()
        if self._skills is not None:
            authority = _skill_authority(candidate_definitions, candidate_scope, config, effective_config)
            skills = await self._skills.prepare(
                effective_config,
                config.enabled_skills,
                cancellation,
                authority_ceiling=authority,
            )
            if skills.active_skill_names:
                skill_definitions = skill_tool_definitions()
        definitions = (
            *base_definitions,
            *skill_definitions,
            *shell_definitions,
        )
        scope = _effective_capability_scope(definitions, config, effective_config, permission)
        prepared = _PreparedProductionCapabilities(
            run_id=state.run_id,
            config=config,
            inputs=_with_skill_prompt_context(_context_inputs(command.input_blocks), skills),
            effective_config=effective_config,
            client_connection_id=command.client_connection_id,
            local_vault_write_grant_id=command.local_vault_write_grant_id,
            budget=_run_budget(
                config,
                effective_config,
                worker_max_parallel_reads=self._effect_gate.max_readers,
            ),
            permission=permission,
            scope=scope,
            definitions=tuple(definitions),
            base_definitions=tuple(base_definitions),
            skill_definitions=skill_definitions,
            shell_definitions=shell_definitions,
            skills=skills,
            shell=shell,
            hooks=hooks,
        )
        self._remember_prepared(state, prepared)
        return PreparedRunComponents(prepared, _prepared_capability_snapshot(prepared))

    def build_prepared_root(
        self,
        command: StartTurnCommand,
        state: RunState,
        prepared: PreparedRunComponents,
    ) -> RunComponents:
        del command
        token = self._prepared_token(state, prepared)
        return self._build(
            token.config,
            state,
            token.inputs,
            effective_config=token.effective_config,
            client_connection_id=token.client_connection_id,
            local_vault_write_grant_id=token.local_vault_write_grant_id,
            definitions_override=token.definitions,
            budget_override=token.budget,
            scope_override=token.scope,
            permission_override=token.permission,
            prepared_capabilities=token,
        )

    def budget_child(self, execution: ChildRunExecution, state: RunState) -> RunBudget:
        del state
        return _child_run_budget(
            execution,
            parent_max_parallel_reads=self._parent_parallel_reads(execution.record),
        )

    async def prepare_child(
        self,
        execution: ChildRunExecution,
        state: RunState,
        cancellation: CancellationScope,
        durable_snapshot: Mapping[str, Any] | None,
    ) -> PreparedRunComponents:
        cancellation.checkpoint()
        root = self._prepared_runs.get(execution.record.root_run_id)
        root_registry = self._registries.get(execution.record.root_run_id)
        if root is None or root_registry is None:
            raise ValueError("child Run parent prepared capability snapshot is unavailable")
        parent_parallel_reads = self._parent_parallel_reads(execution.record)
        if root_registry.snapshot_hash != execution.tool_scope.registry_snapshot_hash:
            raise ValueError("child Run Tool scope refers to a different root Registry snapshot")
        selected = _child_tool_definitions(root_registry.definitions, execution.tool_scope.allowed_versions)
        config = validate_wire(WireRunConfigSnapshot, thaw_json(execution.run_config))
        scope = root.scope.intersect(execution.record.effective_scope)
        permission = execution.record.permission_mode
        effective_config = root.effective_config
        hooks: PreparedHookBundle | None = None
        if self._hooks is not None:
            hook_recovery = _prepared_hook_recovery(durable_snapshot)
            hook_principal_id = (
                await self._principal_id(state.session_id)
                if effective_config.extensibility.hooks_enabled
                else _LOCAL_PROFILE_ID
            )
            hooks = await self._hooks.prepare(
                run_id=state.run_id,
                principal_id=hook_principal_id,
                session_id=state.session_id,
                effective_config=effective_config,
                cancellation=cancellation,
                durable_snapshot=hook_recovery,
            )
        skill_names = {item.name for item in skill_tool_definitions()}
        root_shell_keys = {(item.name, item.version, item.fingerprint) for item in root.shell_definitions}
        base_definitions = tuple(
            item
            for item in selected
            if item.name not in skill_names and (item.name, item.version, item.fingerprint) not in root_shell_keys
        )
        skills: PreparedSkillBundle | None = None
        skill_definitions: tuple[ToolDefinition, ...] = ()
        if self._skills is not None and root.skills is not None:
            declared_skills = execution.context.content.get("agentSkills", [])
            if (
                not isinstance(declared_skills, list)
                or any(not isinstance(item, str) or not item for item in declared_skills)
                or len(declared_skills) != len(set(declared_skills))
            ):
                raise ValueError("child context contains an invalid declared Skill set")
            authority = _skill_authority(selected, scope, config, effective_config)
            skills = self._skills.narrow_prepared(
                root.skills,
                tuple(name for name in config.enabled_skills if name in set(declared_skills)),
                authority_ceiling=authority,
            )
            if skills.active_skill_names:
                skill_definitions = tuple(item for item in selected if item.name in skill_names)
        expected_shell_definitions = tuple(
            item for item in selected if (item.name, item.version, item.fingerprint) in root_shell_keys
        )
        shell: PreparedShellBundle | None = None
        shell_definitions: tuple[ToolDefinition, ...] = ()
        if self._shell is not None and root.shell is not None:
            shell = self._shell.narrow_prepared(
                root.shell,
                expected_shell_definitions,
                allowed_cwd_root_ids=tuple(sorted(root.shell.allowed_cwd_root_ids)),
            )
            shell_definitions = self._shell.definitions_for(shell)
            if shell_definitions != expected_shell_definitions:
                raise ValueError("child Shell capability projection drifted from its parent Tool scope")
        elif expected_shell_definitions:
            raise ValueError("child Shell Tool scope has no production Shell capability factory")
        definitions = tuple(
            item
            for item in selected
            if item in base_definitions or item in skill_definitions or item in shell_definitions
        )
        transaction_selected = any(item.name == "vault.transaction" for item in definitions)
        client_connection_id = root.client_connection_id if transaction_selected else None
        local_vault_write_grant_id = root.local_vault_write_grant_id if transaction_selected else None
        if transaction_selected and (client_connection_id is None) == (local_vault_write_grant_id is None):
            raise ValueError("child Run Vault scope must inherit exactly one root write authority")
        prepared = _PreparedProductionCapabilities(
            run_id=state.run_id,
            config=config,
            inputs=_with_skill_prompt_context(_child_context_inputs(execution), skills),
            effective_config=effective_config,
            client_connection_id=client_connection_id,
            local_vault_write_grant_id=local_vault_write_grant_id,
            budget=_child_run_budget(
                execution,
                parent_max_parallel_reads=parent_parallel_reads,
            ),
            permission=permission,
            scope=scope,
            definitions=definitions,
            base_definitions=base_definitions,
            skill_definitions=skill_definitions,
            shell_definitions=shell_definitions,
            skills=skills,
            shell=shell,
            hooks=hooks,
            parent_snapshot_fingerprint=canonical_json_sha256(_prepared_capability_snapshot(root)),
        )
        self._remember_prepared(state, prepared)
        return PreparedRunComponents(prepared, _prepared_capability_snapshot(prepared))

    def build_prepared_child(
        self,
        execution: ChildRunExecution,
        state: RunState,
        prepared: PreparedRunComponents,
    ) -> RunComponents:
        token = self._prepared_token(state, prepared)
        return self._build(
            token.config,
            state,
            token.inputs,
            effective_config=token.effective_config,
            client_connection_id=token.client_connection_id,
            local_vault_write_grant_id=token.local_vault_write_grant_id,
            definitions_override=token.definitions,
            budget_override=token.budget,
            scope_override=token.scope,
            permission_override=token.permission,
            prepared_capabilities=token,
            child_record=execution.record,
        )

    def release(self, run_id: str) -> asyncio.Task[None] | None:
        registry = self._registries.pop(run_id, None)
        if registry is not None:
            self._recent_registries[run_id] = registry
            self._recent_registries.move_to_end(run_id)
            while len(self._recent_registries) > 32:
                self._recent_registries.popitem(last=False)
        self._prepared_runs.pop(run_id, None)
        self._root_ledgers.pop(run_id, None)
        self._run_budgets.pop(run_id, None)
        self._effective_configs.pop(run_id, None)
        self._client_connections.pop(run_id, None)
        headless_grant_id = self._headless_grants.pop(run_id, None)
        self._bound_hook_bundles.pop(run_id, None)
        if headless_grant_id is None:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._release_run_resources(headless_grant_id, run_id))
            return None
        return loop.create_task(
            self._release_run_resources(headless_grant_id, run_id),
            name=f"release-run-capabilities:{run_id}",
        )

    async def _release_run_resources(
        self,
        headless_grant_id: str | None,
        run_id: str,
    ) -> None:
        if headless_grant_id is not None:
            await self._headless_vault_write.release_claim(
                headless_grant_id,
                reason=f"turn_finished:{run_id}",
            )

    def active_hook_bundle(self, run_id: str) -> ProductionHookBundle | None:
        return self._bound_hook_bundles.get(run_id)

    async def persisted_hook_bundle(
        self,
        run_id: str,
        budget: BudgetLedger,
        cancellation: CancellationToken,
    ) -> ProductionHookBundle | None:
        factory = self._hooks
        unit_of_work = self._hook_unit_of_work
        if factory is None or unit_of_work is None:
            return None
        async with unit_of_work.begin() as transaction:
            run = await transaction.entities.get("runs", run_id)
            capability = await transaction.entities.get("run_capability_snapshots", run_id)
            session = (
                None
                if not isinstance(run, Run)
                else await transaction.entities.get(
                    "sessions",
                    run.session_id,
                )
            )
            effective = (
                None
                if not isinstance(run, Run)
                else await transaction.entities.get(
                    "run_effective_configs",
                    run.lineage.root_run_id,
                )
            )
        if not isinstance(run, Run) or not isinstance(session, Session):
            raise ValueError("persisted Hook Run/Session is unavailable")
        if not isinstance(capability, Mapping) or not isinstance(effective, Mapping):
            raise ValueError("persisted Hook capability/config snapshot is unavailable")
        snapshot = capability.get("snapshot")
        if (
            capability.get("schemaVersion") != 1
            or capability.get("workspaceId") != self.workspace_id
            or capability.get("runId") != run_id
            or not isinstance(snapshot, Mapping)
            or capability.get("snapshotFingerprint") != canonical_json_sha256(snapshot)
        ):
            raise ValueError("persisted Hook capability snapshot is corrupt")
        raw_config = effective.get("config")
        if (
            effective.get("schemaVersion") != 1
            or effective.get("workspaceId") != self.workspace_id
            or not isinstance(raw_config, Mapping)
        ):
            raise ValueError("persisted Hook effective config is corrupt")
        prepared = await factory.prepare(
            run_id=run_id,
            principal_id=session.profile_id,
            session_id=session.session_id,
            effective_config=HarnessConfig.model_validate(dict(raw_config)),
            cancellation=cancellation,
            durable_snapshot=_prepared_hook_recovery(snapshot),
        )
        return factory.build_prepared(prepared, artifact_budget=budget)

    async def session_started(
        self,
        *,
        workspace_id: str,
        session_id: str,
        principal_id: str,
        connection_id: str,
        effective_config: HarnessConfig,
        cancellation: CancellationToken,
    ) -> None:
        if workspace_id != self.workspace_id:
            raise ValueError("SessionStart Hook belongs to another Workspace")
        factory = self._hooks
        budget = self._lifecycle_budget
        if factory is None or budget is None:
            return
        run_id = (
            "lifecycle-" + hashlib.sha256(f"{workspace_id}\0{session_id}\0{connection_id}".encode()).hexdigest()[:32]
        )
        prepared = await factory.prepare(
            run_id=run_id,
            principal_id=principal_id,
            session_id=session_id,
            effective_config=effective_config,
            cancellation=cancellation,
        )
        bundle = factory.build_prepared(prepared, artifact_budget=budget)
        if bundle.worker_lifecycle is not None:
            await bundle.worker_lifecycle.session_start(
                connection_id=connection_id,
                cancellation=cancellation,
            )

    async def runtime_shutdown_hook(
        self,
        *,
        shutdown_id: str,
        reason_code: str,
        effective_config: HarnessConfig,
        cancellation: CancellationToken,
    ) -> None:
        factory = self._hooks
        budget = self._lifecycle_budget
        if factory is None or budget is None:
            return
        prepared = await factory.prepare(
            run_id=f"runtime-shutdown-{shutdown_id}",
            principal_id=_LOCAL_PROFILE_ID,
            session_id="runtime",
            effective_config=effective_config,
            cancellation=cancellation,
        )
        bundle = factory.build_prepared(prepared, artifact_budget=budget)
        if bundle.worker_lifecycle is not None:
            await bundle.worker_lifecycle.runtime_shutdown(
                shutdown_id=shutdown_id,
                reason_code=reason_code,
                cancellation=cancellation,
            )

    def _remember_prepared(self, state: RunState, prepared: _PreparedProductionCapabilities) -> None:
        if prepared.run_id != state.run_id:
            raise ValueError("prepared production capability Run identity drifted")
        existing = self._prepared_runs.get(state.run_id)
        if existing is not None and _prepared_capability_snapshot(existing) != _prepared_capability_snapshot(prepared):
            raise ValueError("per-Run production capability snapshot changed after preparation")
        self._prepared_runs[state.run_id] = prepared
        self._effective_configs[state.run_id] = prepared.effective_config
        if prepared.client_connection_id is not None:
            self._client_connections[state.run_id] = prepared.client_connection_id
        if prepared.local_vault_write_grant_id is not None and prepared.parent_snapshot_fingerprint is None:
            self._headless_grants[state.run_id] = prepared.local_vault_write_grant_id

    async def _principal_id(self, session_id: str) -> str:
        unit_of_work = self._hook_unit_of_work
        if unit_of_work is None:
            raise ValueError("production Hook principal store is unavailable")
        async with unit_of_work.begin() as transaction:
            session = await transaction.entities.get("sessions", session_id)
        if not isinstance(session, Session) or session.session_id != session_id:
            raise ValueError("production Hook session/principal is unavailable")
        return session.profile_id

    def _prepared_token(
        self,
        state: RunState,
        prepared: PreparedRunComponents,
    ) -> _PreparedProductionCapabilities:
        token = prepared.token
        current = self._prepared_runs.get(state.run_id)
        if not isinstance(token, _PreparedProductionCapabilities) or token is not current:
            raise ValueError("prepared production capability token is stale or belongs to another Run")
        if thaw_json(prepared.durable_snapshot) != _prepared_capability_snapshot(token):
            raise ValueError("prepared production capability token differs from its durable proof")
        return token

    def _base_definitions(
        self,
        config: WireRunConfigSnapshot,
        effective_config: HarnessConfig,
        *,
        client_connection_id: str | None,
        local_vault_write_grant_id: str | None,
        permission: PermissionMode,
    ) -> tuple[ToolDefinition, ...]:
        write_allowed = permission not in {PermissionMode.READ_ONLY, PermissionMode.PLAN}
        transaction_definitions: tuple[ToolDefinition, ...] = ()
        if write_allowed:
            authorities = int(client_connection_id is not None) + int(local_vault_write_grant_id is not None)
            if authorities != 1:
                raise ValueError("write-capable production Run requires exactly one Vault authority")
            # The public transaction is always a Worker-local Tool.  An exact
            # Obsidian connection contributes live-editor proof, but never owns
            # the durable filesystem mutation.
            transaction_definitions = (vault_transaction_definition(executor_location=ExecutorLocation.LOCAL),)
        optional_definitions = tuple(
            item
            for item in self._optional_definitions
            if (item.executor_location is not ExecutorLocation.SUBAGENT or effective_config.execution.subagents_enabled)
            and ("shell.execute" not in item.required_capabilities or effective_config.execution.shell_enabled)
        )
        return (
            *self._local_read.definitions,
            *transaction_definitions,
            *optional_definitions,
        )

    def build(self, command: StartTurnCommand, state: RunState) -> RunComponents:
        config = validate_wire(WireRunConfigSnapshot, thaw_json(command.run_config))
        inputs = _context_inputs(command.input_blocks)
        effective_config = command.effective_config or self._default_config
        self._ensure_worker_read_limit(effective_config)
        self._effective_configs[state.run_id] = effective_config
        if command.client_connection_id is not None:
            self._client_connections[state.run_id] = command.client_connection_id
        if command.local_vault_write_grant_id is not None:
            self._headless_grants[state.run_id] = command.local_vault_write_grant_id
        return self._build(
            config,
            state,
            inputs,
            effective_config=effective_config,
            client_connection_id=command.client_connection_id,
            local_vault_write_grant_id=command.local_vault_write_grant_id,
        )

    def build_child(self, execution: Any, state: RunState) -> RunComponents:
        config = validate_wire(WireRunConfigSnapshot, thaw_json(execution.run_config))
        content = canonical_json_bytes(execution.context.content).decode("utf-8")
        inputs = ContextInputs(
            (
                ContextFragment(
                    f"subagent:{execution.record.run_id}:context",
                    ContextLayer.USER_INPUT,
                    content,
                    sensitivity=_workspace_sensitivity(),
                ),
            )
        )
        root_registry = self._registries.get(execution.record.root_run_id)
        if root_registry is None:
            raise ValueError("child Run root Tool Registry snapshot is unavailable")
        if root_registry.snapshot_hash != execution.tool_scope.registry_snapshot_hash:
            raise ValueError("child Run Tool scope refers to a different root Registry snapshot")
        definitions = _child_tool_definitions(root_registry.definitions, execution.tool_scope.allowed_versions)
        root_run_id = execution.record.root_run_id
        try:
            effective_config = self._effective_configs[root_run_id]
        except KeyError as error:
            raise ValueError("child Run effective root configuration is unavailable") from error
        transaction_selected = any(item.name == "vault.transaction" for item in definitions)
        client_connection_id = self._client_connections.get(root_run_id) if transaction_selected else None
        local_vault_write_grant_id = self._headless_grants.get(root_run_id) if transaction_selected else None
        if transaction_selected and (client_connection_id is None) == (local_vault_write_grant_id is None):
            raise ValueError("child Run Vault scope must inherit exactly one root write authority")
        return self._build(
            config,
            state,
            inputs,
            effective_config=effective_config,
            client_connection_id=client_connection_id,
            local_vault_write_grant_id=local_vault_write_grant_id,
            definitions_override=definitions,
            budget_override=_child_run_budget(
                execution,
                parent_max_parallel_reads=self._parent_parallel_reads(execution.record),
            ),
            scope_override=execution.record.effective_scope,
            permission_override=execution.record.permission_mode,
            child_record=execution.record,
        )

    def registry_for_run(self, run_id: str) -> ToolRegistry | None:
        return self._registries.get(run_id) or self._recent_registries.get(run_id)

    def root_ledger(self, root_run_id: str) -> BudgetLedger:
        try:
            return self._root_ledgers[root_run_id]
        except KeyError as error:
            raise KeyError("authoritative root Run budget ledger is unavailable") from error

    def _parent_parallel_reads(self, record: SubagentRunRecord) -> int:
        worker_workspace_id = cast(str, self.__dict__.get("workspace_id", record.workspace_id))
        if record.workspace_id != worker_workspace_id:
            raise ValueError("child Run belongs to another Worker Workspace")
        run_budgets = cast(Mapping[str, RunBudget], self.__dict__.get("_run_budgets", {}))
        parent = run_budgets.get(record.parent_run_id)
        if parent is not None:
            return parent.max_parallel_reads
        prepared_runs = cast(
            Mapping[str, _PreparedProductionCapabilities],
            self.__dict__.get("_prepared_runs", {}),
        )
        prepared_parent = prepared_runs.get(record.parent_run_id)
        if prepared_parent is not None:
            return prepared_parent.budget.max_parallel_reads
        effective_configs = cast(
            Mapping[str, HarnessConfig],
            self.__dict__.get("_effective_configs", {}),
        )
        root_config = effective_configs.get(record.root_run_id)
        if isinstance(root_config, HarnessConfig):
            return root_config.budgets.max_parallel_reads
        raise ValueError("child Run parent budget snapshot is unavailable")

    def bind_worker_read_limit(self, max_parallel_reads: int) -> None:
        if not self._effect_gate_bound:
            self._effect_gate.configure_max_readers(max_parallel_reads)
            self._effect_gate_bound = True
            return
        if max_parallel_reads != self._effect_gate.max_readers:
            raise ValueError("max_parallel_reads changed after Worker startup; restart the Worker to apply it")

    def _ensure_worker_read_limit(self, effective_config: HarnessConfig) -> None:
        # Production startup binds the Workspace-wide snapshot before opening
        # transports.  This fallback keeps direct/recovery factory callers safe.
        if not self._effect_gate_bound:
            self.bind_worker_read_limit(effective_config.budgets.max_parallel_reads)

    def _build(
        self,
        config: WireRunConfigSnapshot,
        state: RunState,
        inputs: ContextInputs,
        *,
        effective_config: HarnessConfig,
        client_connection_id: str | None,
        local_vault_write_grant_id: str | None,
        definitions_override: Sequence[ToolDefinition] | None = None,
        budget_override: RunBudget | None = None,
        scope_override: CapabilityScope | None = None,
        permission_override: PermissionMode | None = None,
        prepared_capabilities: _PreparedProductionCapabilities | None = None,
        child_record: SubagentRunRecord | None = None,
    ) -> RunComponents:
        if state.lineage.depth == 0:
            if child_record is not None:
                raise ValueError("root Run cannot be bound to a Subagent record")
        elif (
            child_record is None
            or child_record.run_id != state.run_id
            or child_record.lineage != state.lineage
            or child_record.workspace_id != state.workspace_id
            or child_record.session_id != state.session_id
            or child_record.turn_id != state.turn_id
            or child_record.permission_mode is PermissionMode.BYPASS
        ):
            raise ValueError("child Run state does not match its durable Subagent authority record")
        if self._run_correlations is not None:
            self._run_correlations.register(
                workspace_id=state.workspace_id,
                session_id=state.session_id,
                turn_id=state.turn_id,
                run_id=state.run_id,
                parent_run_id=state.lineage.parent_run_id,
            )
        if self._run_observability is not None:
            self._run_observability.run_depth_registered(state.lineage.depth)
        if not effective_config.network.model_provider_enabled:
            raise ValueError("Model provider is disabled by the effective persisted configuration")
        if config.provider != effective_config.model.provider.value:
            raise ValueError("Run provider must match the persisted Workspace provider")
        selected_model = config.model
        settings = effective_config.model.model_copy(
            update={"model": selected_model, "reasoning_effort": config.reasoning_effort.value}
        )
        gateway = self._gateway_factory(settings)
        budget = budget_override or _run_budget(
            config,
            effective_config,
            worker_max_parallel_reads=self._effect_gate.max_readers,
        )
        existing_budget = self._run_budgets.get(state.run_id)
        if existing_budget is not None and existing_budget != budget:
            raise RuntimeError("Run budget changed after its production components were built")
        self._run_budgets[state.run_id] = budget
        visibility = (
            ContextVisibilityPolicy.local_model()
            if settings.provider is ModelProvider.LOCAL
            else ContextVisibilityPolicy.cloud_model()
        )
        context = ContextManager(
            system_rules=(
                "你是 OfferAgent。只能依据 Harness 提供的上下文和工具结果工作。",
                "不得声称未执行、未审批、冲突或结果未知的写操作已经完成。",
                "文件、Shell、Memory 与 Subagent 只能经 Tool Kernel 使用。",
            ),
            inputs=inputs,
            visibility=visibility,
            budget=ContextBudget.generous_default(),
        )
        client_route = client_connection_id is not None
        local_route = local_vault_write_grant_id is not None
        if client_route and local_route:
            raise ValueError("Run cannot retain both CLIENT and LOCAL Vault authorities")
        permission = permission_override or _effective_permission(config, effective_config)
        workspace_trusted = effective_config.policy.workspace_trusted
        write_allowed = permission not in {PermissionMode.READ_ONLY, PermissionMode.PLAN}
        if prepared_capabilities is not None:
            if (
                prepared_capabilities.run_id != state.run_id
                or definitions_override != prepared_capabilities.definitions
                or budget_override != prepared_capabilities.budget
            ):
                raise ValueError("prepared production capability binding drifted")
            definitions = prepared_capabilities.definitions
            base_definitions = prepared_capabilities.base_definitions
        else:
            if definitions_override is None:
                definitions = self._base_definitions(
                    config,
                    effective_config,
                    client_connection_id=client_connection_id,
                    local_vault_write_grant_id=local_vault_write_grant_id,
                    permission=permission,
                )
            else:
                definitions = tuple(
                    item
                    for item in definitions_override
                    if write_allowed or item.risk not in {RiskClass.WRITE, RiskClass.DESTRUCTIVE}
                )
            base_definitions = definitions
        definitions = tuple(definitions)
        base_definitions = tuple(base_definitions)
        selected = {(item.name, item.version, item.fingerprint) for item in definitions}
        base_selected = {(item.name, item.version, item.fingerprint) for item in base_definitions}
        if not base_selected <= selected:
            raise ValueError("prepared base Tool definitions exceed the immutable Run definition snapshot")
        read_definitions = tuple(
            item
            for item in self._local_read.definitions
            if (item.name, item.version, item.fingerprint) in base_selected
        )
        local_routes: list[tuple[Sequence[ToolDefinition], ToolExecutor]] = []
        if read_definitions:
            local_routes.append((read_definitions, self._local_read))
        local_transactions = tuple(
            item
            for item in base_definitions
            if item.name == "vault.transaction" and item.executor_location is ExecutorLocation.LOCAL
        )
        if local_transactions and (client_connection_id is None) == (local_vault_write_grant_id is None):
            raise ValueError("LOCAL vault.transaction requires exactly one client-bound or headless authority")
        for route_definitions, executor in self._optional_local_executors:
            narrowed = tuple(
                item for item in route_definitions if (item.name, item.version, item.fingerprint) in base_selected
            )
            if narrowed:
                local_routes.append((narrowed, executor))
        scope = scope_override or _effective_capability_scope(definitions, config, effective_config, permission)

        def policy_context(call: ToolCall) -> Any:
            from offeragent_harness.permissions import PolicyContext

            return PolicyContext(
                workspace_id=call.workspace_id,
                session_id=state.session_id,
                run_id=call.run_id,
                principal_id=_LOCAL_PROFILE_ID,
                permission_mode=permission,
                workspace_trusted=workspace_trusted,
                effective_scope=scope,
                now=self._clock.utcnow(),
            )

        automatic_rules: tuple[PolicyRule, ...] = ()
        if not effective_config.policy.approve_vault_writes:
            automatic_rules = (
                PolicyRule(
                    rule_id="workspace-user-approved-vault-writes",
                    effect=RuleEffect.ALLOW,
                    tool_names=frozenset({"vault.transaction"}),
                    permission_modes=frozenset({PermissionMode.NORMAL, PermissionMode.TRUSTED_WORKSPACE}),
                    workspace_trusted=True,
                    reason="用户已在本地受信任 Workspace 中明确关闭 Vault 写入逐次审批。",
                    audit_tags=frozenset({"user-configured", "vault-write-auto-approved"}),
                ),
            )
        downstream_policy = RuleBasedPolicyEvaluator(
            rules=automatic_rules,
            audit_sink=self._policy_audit,
            grant_store=self._approvals.grants,
        )
        policy: PolicyEvaluator = downstream_policy
        if child_record is not None:
            policy = SubagentScopePolicy(
                child_record,
                self._parent_authorities,
                downstream_policy,
                audit_sink=self._policy_audit,
            )
        expected_budget = budget
        bound_ledger: BudgetLedger | None = None
        bound_kernel: ToolKernel | None = None
        bound_hook_ledger: BudgetLedger | None = None
        bound_hook_bundle: ProductionHookBundle | None = None

        def hook_binding(budget: BudgetLedger) -> RunHookBinding:
            nonlocal bound_hook_bundle, bound_hook_ledger
            normalized_active = replace(
                budget.budget,
                max_wall_seconds=expected_budget.max_wall_seconds,
            )
            if (
                normalized_active != expected_budget
                or budget.budget.max_wall_seconds > expected_budget.max_wall_seconds
            ):
                raise ValueError("Harness supplied a Hook ledger with a different Run budget")
            if bound_hook_bundle is not None:
                if bound_hook_ledger is not budget:
                    raise RuntimeError("per-Run Hook port is already bound to another BudgetLedger")
                return RunHookBinding(bound_hook_bundle.hooks, bound_hook_bundle.context)
            if prepared_capabilities is None or prepared_capabilities.hooks is None:
                return RunHookBinding(None, None)
            if self._hooks is None:
                raise ValueError("prepared Hook snapshot has no production Hook factory")
            bound_hook_bundle = self._hooks.build_prepared(
                prepared_capabilities.hooks,
                artifact_budget=budget,
            )
            bound_hook_ledger = budget
            self._bound_hook_bundles[state.run_id] = bound_hook_bundle
            return RunHookBinding(bound_hook_bundle.hooks, bound_hook_bundle.context)

        def tool_kernel(budget: BudgetLedger) -> ToolKernel:
            nonlocal bound_kernel, bound_ledger
            normalized_active = replace(budget.budget, max_wall_seconds=expected_budget.max_wall_seconds)
            if (
                normalized_active != expected_budget
                or budget.budget.max_wall_seconds > expected_budget.max_wall_seconds
            ):
                raise ValueError("Harness supplied a Tool Kernel ledger with a different Run budget")
            if bound_kernel is not None:
                if bound_ledger is not budget:
                    raise RuntimeError("per-Run Tool Kernel is already bound to another BudgetLedger")
                return bound_kernel
            if state.lineage.depth == 0:
                existing = self._root_ledgers.get(state.run_id)
                if existing is not None and existing is not budget:
                    raise RuntimeError("root Run budget ledger is already bound to another active Run")
                self._root_ledgers[state.run_id] = budget
            active_hooks = hook_binding(budget)
            preflight_providers: list[Any] = []
            active_local_routes = list(local_routes)
            if local_transactions:
                if client_route:
                    assert client_connection_id is not None
                    client_preview = NamedPipeClientToolPort(
                        workspace_id=self.workspace_id,
                        channels=_BoundReverseChannels(self._channels, client_connection_id),
                        clock=self._clock,
                    )
                    authorized_transaction: ToolExecutor = ClientBoundVaultTransaction(
                        workspace_id=self.workspace_id,
                        client=client_preview,
                        transaction=self._local_transaction,
                        clock=self._clock,
                    )
                else:
                    assert local_vault_write_grant_id is not None
                    authorized_transaction = HeadlessAuthorizedVaultTransaction(
                        authority=self._headless_vault_write,
                        grant_id=local_vault_write_grant_id,
                        transaction=self._local_transaction,
                    )
                preflight_providers.append(authorized_transaction)
                active_local_routes.append((local_transactions, authorized_transaction))
            artifacts = ToolArtifactManager(self._artifacts, self._clock, self._ids, budget)
            if prepared_capabilities is not None and prepared_capabilities.skill_definitions:
                if self._skills is None or prepared_capabilities.skills is None:
                    raise ValueError("prepared Skill definitions have no production Skill factory")
                built_skills = self._skills.build_prepared(prepared_capabilities.skills)
                built_by_key = {(item.name, item.version, item.fingerprint): item for item in built_skills.definitions}
                if (
                    any(
                        (item.name, item.version, item.fingerprint) not in built_by_key
                        for item in prepared_capabilities.skill_definitions
                    )
                    or built_skills.executor is None
                ):
                    raise ValueError("Run-bound Skill bundle differs from the prepared definitions")
                active_local_routes.append((prepared_capabilities.skill_definitions, built_skills.executor))
            if prepared_capabilities is not None and prepared_capabilities.shell_definitions:
                if self._shell is None or prepared_capabilities.shell is None:
                    raise ValueError("prepared Shell definitions have no production Shell factory")
                built_shell = self._shell.build_prepared(
                    prepared_capabilities.shell,
                    artifacts,
                )
                built_shell_keys = {(item.name, item.version, item.fingerprint) for item in built_shell.definitions}
                if (
                    any(
                        (item.name, item.version, item.fingerprint) not in built_shell_keys
                        for item in prepared_capabilities.shell_definitions
                    )
                    or built_shell.executor is None
                ):
                    raise ValueError("Run-bound Shell bundle differs from the prepared definitions")
                active_local_routes.append((prepared_capabilities.shell_definitions, built_shell.executor))
            registry = ToolRegistry(
                f"run-{state.run_id}",
                definitions,
                preflight_provider_ids=frozenset({self._local_transaction.provider_id}),
            )
            existing_registry = self._registries.get(state.run_id)
            if existing_registry is not None and existing_registry.snapshot_hash != registry.snapshot_hash:
                raise RuntimeError("per-Run Tool Registry changed after active ledger binding")
            self._registries[state.run_id] = registry
            dispatcher = ToolDispatcher(
                clock=self._clock,
                local=_CompositeExecutor(active_local_routes),
                client=None,
                subagent=self._subagent_executor,
            )
            bound_ledger = budget
            bound_kernel = UnifiedToolKernel(
                registry=registry,
                validator=ToolValidator(),
                policy=policy,
                policy_context=policy_context,
                scheduler=ToolScheduler(
                    clock=self._clock,
                    max_parallel_reads=expected_budget.max_parallel_reads,
                    effect_gate=self._effect_gate,
                ),
                dispatcher=dispatcher,
                journal=self._journal,
                clock=self._clock,
                ids=self._ids,
                approvals=self._approvals,
                artifacts=artifacts,
                preflights=PreflightRegistry(preflight_providers),
                hooks=active_hooks.hooks,
                observability=self._tool_observability,
                lock_pool=self._lock_pool,
                managed_hook_owner_id=(
                    prepared_capabilities.hooks.managed_owner_id
                    if prepared_capabilities is not None and prepared_capabilities.hooks is not None
                    else _LOCAL_MANAGED_ID
                ),
            )
            return bound_kernel

        catalog = ToolPlanCatalog(definitions, max_calls=max(1, budget.max_tool_calls))
        planner_config = PlannerModelConfig(
            model=selected_model,
            max_output_tokens=min(16_384, budget.max_output_tokens),
            reasoning_effort=config.reasoning_effort.value,
            temperature=settings.temperature,
        )
        composer_config = ComposerModelConfig(
            model=selected_model,
            max_output_tokens=min(32_768, budget.max_output_tokens),
            reasoning_effort=config.reasoning_effort.value,
            temperature=settings.temperature,
        )
        return RunComponents(
            planner_factory=lambda active: ModelPlanner(
                gateway=gateway,
                context_manager=context,
                catalog=catalog,
                config=planner_config,
                clock=self._clock,
                ids=self._ids,
                budget=active,
            ),
            tool_kernel_factory=tool_kernel,
            composer=ModelComposer(
                gateway=gateway,
                context_manager=context,
                config=composer_config,
                ids=self._ids,
            ),
            budget=budget,
            hook_binding_factory=(
                hook_binding if prepared_capabilities is not None and prepared_capabilities.hooks is not None else None
            ),
        )


def _prepared_capability_snapshot(prepared: _PreparedProductionCapabilities) -> dict[str, Any]:
    skills: dict[str, Any] | None = None
    if prepared.skills is not None:
        status = prepared.skills.catalog_status
        authority = prepared.skills.authority
        skills = {
            "enabled": prepared.skills.skills_enabled,
            "workspaceTrusted": prepared.skills.workspace_trusted,
            "enabledNames": sorted(prepared.skills.enabled_skill_names),
            "activeNames": sorted(prepared.skills.active_skill_names),
            "catalogRevision": prepared.skills.catalog_revision,
            "catalogSnapshotHash": prepared.skills.catalog_snapshot_hash,
            "catalogStatus": {
                "revision": status.revision,
                "snapshotHash": status.snapshot_hash,
                "discoveredCount": status.discovered_count,
                "enabledCount": status.enabled_count,
                "partial": status.partial,
                "diagnostics": [
                    {
                        "severity": item.severity.value,
                        "code": item.code.value,
                        "message": item.message,
                        "rootId": item.root_id,
                        "path": item.path,
                    }
                    for item in status.diagnostics
                ],
            },
            "authority": {
                "availableTools": sorted(authority.available_tools),
                "policyAllowedTools": sorted(authority.policy_allowed_tools),
                "enabledSkills": None if authority.enabled_skills is None else sorted(authority.enabled_skills),
                "workspaceTrusted": authority.workspace_trusted,
            },
            "trustEvidence": [
                {
                    "rootId": item.root_id,
                    "packagePath": item.package_path,
                    "name": item.name,
                    "layer": item.layer.value,
                    "metadataHash": item.metadata_hash,
                    "trustState": item.trust_state,
                    "trustTokenHash": item.trust_token_hash,
                }
                for item in prepared.skills.trust_evidence
            ],
            "promptDescriptors": [
                {
                    "rootId": item.root_id,
                    "packagePath": item.package_path,
                    "name": item.name,
                    "description": item.description,
                    "allowedTools": list(item.allowed_tools),
                    "metadataHash": item.metadata_hash,
                }
                for item in prepared.skills.prompt_descriptors
            ],
        }
    shell: dict[str, Any] | None = None
    if prepared.shell is not None:
        shell = {
            "enabled": prepared.shell.shell_enabled,
            "workspaceTrusted": prepared.shell.workspace_trusted,
            "readOnly": prepared.shell.read_only,
            "allowedCwdRootIds": sorted(prepared.shell.allowed_cwd_root_ids),
            "catalogRevision": prepared.shell.catalog_revision,
            "catalogSnapshotHash": prepared.shell.catalog_snapshot_hash,
            "profiles": [
                {
                    "profileId": item.profile_id,
                    "contentHash": item.content_hash,
                    "executableProfileFingerprint": item.executable_profile_fingerprint,
                    "recordRevision": item.record_revision,
                    "trust": item.trust.value,
                    "definition": _definition_proofs((item.definition,))[0],
                }
                for item in prepared.shell.profiles
            ],
        }
    return {
        "schemaVersion": 1,
        "runId": prepared.run_id,
        "localVaultWriteGrantId": prepared.local_vault_write_grant_id,
        "effectiveConfigFingerprint": canonical_json_sha256(prepared.effective_config.model_dump(mode="json")),
        "runConfigFingerprint": canonical_json_sha256(prepared.config.to_wire()),
        "permissionMode": prepared.permission.value,
        "scope": {
            "allowedTools": sorted(prepared.scope.allowed_tools),
            "deniedTools": sorted(prepared.scope.denied_tools),
            "allowedRisks": sorted(item.value for item in prepared.scope.allowed_risks),
            "rootCapabilities": sorted(prepared.scope.root_capabilities),
            "allowNetwork": prepared.scope.allow_network,
            "allowSecretHandles": prepared.scope.allow_secret_handles,
        },
        "budget": _budget_snapshot(prepared.budget),
        "definitions": _definition_proofs(prepared.definitions),
        "baseDefinitions": _definition_proofs(prepared.base_definitions),
        "skillDefinitions": _definition_proofs(prepared.skill_definitions),
        "shellDefinitions": _definition_proofs(prepared.shell_definitions),
        "skills": skills,
        "shell": shell,
        "hooks": None if prepared.hooks is None else prepared.hooks.recovery_snapshot(),
        "parentSnapshotFingerprint": prepared.parent_snapshot_fingerprint,
    }


def _prepared_hook_recovery(
    durable_snapshot: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if durable_snapshot is None:
        return None
    value = durable_snapshot.get("hooks")
    if not isinstance(value, Mapping):
        raise ValueError("persisted production Hook recovery snapshot is missing or invalid")
    return value


def _definition_proofs(definitions: Sequence[ToolDefinition]) -> list[dict[str, str]]:
    return [
        {
            "name": item.name,
            "version": item.version,
            "fingerprint": item.fingerprint,
            "risk": item.risk.value,
            "executorLocation": item.executor_location.value,
            "resultSensitivity": item.result_sensitivity.value,
        }
        for item in definitions
    ]


def _budget_snapshot(value: RunBudget) -> dict[str, Any]:
    return {
        "maxModelRounds": value.max_model_rounds,
        "maxToolCalls": value.max_tool_calls,
        "maxParallelReads": value.max_parallel_reads,
        "maxWallSeconds": value.max_wall_seconds,
        "maxInputTokens": value.max_input_tokens,
        "maxOutputTokens": value.max_output_tokens,
        "maxCost": format(value.max_cost, "f"),
        "maxArtifactBytes": value.max_artifact_bytes,
        "maxSubagents": value.max_subagents,
    }


def _effective_permission(
    config: WireRunConfigSnapshot,
    effective_config: HarnessConfig,
) -> PermissionMode:
    permission = _permission_mode(config.permission_mode)
    if permission is PermissionMode.PLAN:
        return permission
    if effective_config.policy.read_only or not effective_config.policy.workspace_trusted:
        return PermissionMode.READ_ONLY
    return permission


def _effective_capability_scope(
    definitions: Sequence[ToolDefinition],
    config: WireRunConfigSnapshot,
    effective_config: HarnessConfig,
    permission: PermissionMode,
) -> CapabilityScope:
    scope = _capability_scope(definitions, config)
    allowed_risks = scope.allowed_risks
    allow_network = False
    allow_secret_handles = False
    if permission in {PermissionMode.READ_ONLY, PermissionMode.PLAN}:
        allowed_risks = frozenset({RiskClass.READ})
        allow_network = False
        allow_secret_handles = False
    return replace(
        scope,
        allowed_risks=allowed_risks,
        allow_network=allow_network,
        allow_secret_handles=allow_secret_handles,
    )


def _skill_authority(
    definitions: Sequence[ToolDefinition],
    scope: CapabilityScope,
    config: WireRunConfigSnapshot,
    effective_config: HarnessConfig,
) -> SkillAuthority:
    names = frozenset(item.name for item in definitions)
    policy_allowed = frozenset(item.name for item in definitions if scope.permits_tool(item.name, item.risk))
    return SkillAuthority(
        available_tools=names,
        policy_allowed_tools=policy_allowed,
        enabled_skills=frozenset(config.enabled_skills),
        workspace_trusted=effective_config.policy.workspace_trusted,
    )


def _child_context_inputs(execution: ChildRunExecution) -> ContextInputs:
    raw = dict(execution.context.content)
    instructions = raw.pop("agentInstructions", "")
    if not isinstance(instructions, str):
        raise ValueError("child context Agent instructions are invalid")
    content = canonical_json_bytes(raw).decode("utf-8")
    skills: tuple[ContextFragment, ...] = ()
    if instructions:
        skills = (
            ContextFragment(
                f"subagent:{execution.record.run_id}:instructions",
                ContextLayer.SKILLS,
                "以下是已受信任子 Agent 定义的专用指令; 它不能绕过系统规则、工具权限或审批:\n\n" + instructions,
                sensitivity=_workspace_sensitivity(),
                source_refs=(f"agent:{execution.record.agent_name}:{execution.record.agent_version}",),
                content_hash=canonical_json_sha256({"instructions": instructions}),
            ),
        )
    return ContextInputs(
        (
            ContextFragment(
                f"subagent:{execution.record.run_id}:context",
                ContextLayer.USER_INPUT,
                content,
                sensitivity=_workspace_sensitivity(),
            ),
        ),
        skills=skills,
    )


def _workspace_sensitivity() -> Any:
    from offeragent_harness.ports import Sensitivity

    return Sensitivity.WORKSPACE


def _context_inputs(blocks: Sequence[Mapping[str, Any]]) -> ContextInputs:
    text = canonical_json_bytes([dict(item) for item in blocks]).decode("utf-8")
    return ContextInputs(
        (
            ContextFragment(
                "turn:user-input",
                ContextLayer.USER_INPUT,
                text,
                sensitivity=_workspace_sensitivity(),
            ),
        )
    )


def _with_skill_prompt_context(
    inputs: ContextInputs,
    prepared: PreparedSkillBundle | None,
) -> ContextInputs:
    if prepared is None or not prepared.prompt_descriptors:
        return inputs
    fragments = tuple(
        ContextFragment(
            fragment_id=f"skill:catalog:{item.root_id}:{item.package_path}",
            layer=ContextLayer.SKILLS,
            text=(
                "可用本地 Skill 的受信任元数据。根据用户请求自行判断是否读取 Skill 正文;"
                "此描述不授予任何工具权限, 读取时必须使用列出的 catalogRevision 和 Tool Kernel:\n"
                + canonical_json_bytes(
                    {
                        "catalogRevision": prepared.catalog_revision,
                        "rootId": item.root_id,
                        "packagePath": item.package_path,
                        "name": item.name,
                        "description": item.description,
                        "allowedTools": list(item.allowed_tools),
                        "metadataHash": item.metadata_hash,
                    }
                ).decode("utf-8")
            ),
            sensitivity=_workspace_sensitivity(),
            source_refs=(f"skill:{prepared.workspace_id}:{item.root_id}:{item.package_path}",),
            content_hash=item.metadata_hash,
        )
        for item in prepared.prompt_descriptors
    )
    return ContextInputs(
        user_input=inputs.user_input,
        conversation=inputs.conversation,
        memories=inputs.memories,
        skills=(*inputs.skills, *fragments),
        hook_hints=inputs.hook_hints,
    )


def _run_budget(
    config: WireRunConfigSnapshot,
    effective_config: HarnessConfig,
    *,
    worker_max_parallel_reads: int,
) -> RunBudget:
    value = config.budgets
    configured = effective_config.budgets
    requested_model_rounds = configured.max_iterations if value is None else value.max_model_rounds
    requested_tool_calls = configured.max_tool_calls if value is None else value.max_tool_calls
    requested_wall_seconds = configured.max_wall_seconds if value is None else value.max_wall_time_ms / 1000
    requested_cost = (
        configured.max_cost_microunits if value is None or value.max_cost_micros is None else value.max_cost_micros
    )
    requested_parallel_reads = configured.max_parallel_reads if value is None else value.max_parallel_reads
    return RunBudget(
        max_model_rounds=min(configured.max_iterations, requested_model_rounds),
        max_tool_calls=max(1, min(configured.max_tool_calls, requested_tool_calls)),
        max_parallel_reads=min(
            worker_max_parallel_reads,
            configured.max_parallel_reads,
            requested_parallel_reads,
        ),
        max_wall_seconds=min(configured.max_wall_seconds, requested_wall_seconds),
        max_input_tokens=400_000 if value is None or value.max_input_tokens is None else value.max_input_tokens,
        max_output_tokens=64_000 if value is None or value.max_output_tokens is None else value.max_output_tokens,
        max_cost=Decimal(min(configured.max_cost_microunits, requested_cost)) / Decimal(1_000_000),
        max_artifact_bytes=64 * 1024 * 1024 if value is None else max(1, value.max_artifact_bytes),
        max_subagents=max(1, effective_config.execution.max_subagents_per_vault),
    )


def _child_run_budget(
    execution: Any,
    *,
    parent_max_parallel_reads: int,
) -> RunBudget:
    value = execution.record.budget_limit
    config = validate_wire(WireRunConfigSnapshot, thaw_json(execution.run_config))
    requested_parallel_reads = (
        parent_max_parallel_reads if config.budgets is None else config.budgets.max_parallel_reads
    )
    return RunBudget(
        max_model_rounds=value.model_calls,
        max_tool_calls=max(1, value.tool_calls),
        max_parallel_reads=min(parent_max_parallel_reads, requested_parallel_reads),
        max_wall_seconds=value.wall_time_seconds,
        max_input_tokens=value.input_tokens,
        max_output_tokens=value.output_tokens,
        max_cost=Decimal(value.cost_micros) / Decimal(1_000_000),
        max_artifact_bytes=value.artifact_bytes,
        max_subagents=max(1, value.child_count),
    )


def _remaining_agent_budget(budget: RunBudget, snapshot: Any) -> AgentBudget:
    allocated = snapshot.used + snapshot.reserved
    return AgentBudget(
        max(0, budget.max_input_tokens - allocated.input_tokens),
        max(0, budget.max_output_tokens - allocated.output_tokens),
        max(0, budget.max_model_rounds - allocated.model_rounds),
        max(0, budget.max_tool_calls - allocated.tool_calls),
        max(0.0, budget.max_wall_seconds - snapshot.elapsed_seconds),
        max(0, budget.max_artifact_bytes - allocated.artifact_bytes),
        max(0, budget.max_subagents - allocated.subagents),
        max(0, int((budget.max_cost - allocated.cost) * Decimal(1_000_000))),
    )


def _child_tool_definitions(
    root_definitions: Sequence[ToolDefinition],
    allowed_versions: Mapping[str, Sequence[str]],
) -> tuple[ToolDefinition, ...]:
    selected = tuple(
        definition
        for definition in root_definitions
        if definition.name in allowed_versions and definition.version in allowed_versions[definition.name]
    )
    represented = {(item.name, item.version) for item in selected}
    requested = {(name, version) for name, versions in allowed_versions.items() for version in versions}
    if represented != requested:
        raise ValueError("child Run Tool scope contains a definition absent from the root Registry snapshot")
    return selected


def _permission_mode(value: WirePermissionMode) -> PermissionMode:
    mapping = {
        WirePermissionMode.READ_ONLY: PermissionMode.READ_ONLY,
        WirePermissionMode.NORMAL: PermissionMode.NORMAL,
        WirePermissionMode.TRUSTED_WORKSPACE: PermissionMode.TRUSTED_WORKSPACE,
        WirePermissionMode.PLAN: PermissionMode.PLAN,
    }
    try:
        return mapping[value]
    except KeyError as error:
        raise PermissionError("BYPASS permission mode is unavailable to local UI Run requests") from error


def _capability_scope(
    definitions: Sequence[ToolDefinition],
    config: WireRunConfigSnapshot,
) -> CapabilityScope:
    names = frozenset(item.name for item in definitions)
    risks = set(RiskClass)
    permission = WirePermissionMode(config.permission_mode)
    if permission in {WirePermissionMode.READ_ONLY, WirePermissionMode.PLAN}:
        risks = {RiskClass.READ}
    capabilities = frozenset(capability for item in definitions for capability in item.required_capabilities)
    return CapabilityScope(
        allowed_tools=names,
        denied_tools=frozenset(),
        allowed_risks=frozenset(risks),
        root_capabilities=capabilities,
        allow_network=RiskClass.NETWORK in risks,
        allow_secret_handles=False,
    )


class _LateToolExecutor(ToolExecutor):
    def __init__(self) -> None:
        self._target: ToolExecutor | None = None

    def bind(self, target: ToolExecutor) -> None:
        if self._target is not None:
            raise RuntimeError("late Tool executor is already bound")
        self._target = target

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        target = self._target
        if target is None:
            raise RuntimeError("Tool executor composition has not completed")
        return await target.execute(call, cancellation)


class _LateParentRunAuthorityProvider(ParentRunAuthorityProvider):
    """Break the composition cycle without ever granting fallback authority."""

    def __init__(self) -> None:
        self._target: ParentRunAuthorityProvider | None = None

    def bind(self, target: ParentRunAuthorityProvider) -> None:
        if self._target is not None:
            raise RuntimeError("parent authority provider is already bound")
        if target is self:
            raise ValueError("parent authority provider cannot bind itself")
        self._target = target

    async def authority_for(self, run_id: str) -> ParentRunAuthority:
        target = self._target
        if target is None:
            raise RuntimeError("parent authority composition has not completed")
        return await target.authority_for(run_id)


class _LateSubagentTree:
    def __init__(self) -> None:
        self._target: SubagentService | None = None

    def bind(self, target: SubagentService) -> None:
        if self._target is not None:
            raise RuntimeError("Subagent tree is already bound")
        self._target = target

    async def cancel_descendants(self, parent_run_id: str, reason: str) -> tuple[str, ...]:
        if self._target is None:
            raise RuntimeError("Subagent tree composition has not completed")
        return cast(tuple[str, ...], await self._target.cancel_descendants(parent_run_id, reason))

    async def parent_finished(self, parent_run_id: str, *, turn_finished: bool, reason: str) -> tuple[str, ...]:
        if self._target is None:
            raise RuntimeError("Subagent tree composition has not completed")
        return cast(
            tuple[str, ...],
            await self._target.parent_finished(parent_run_id, turn_finished=turn_finished, reason=reason),
        )


class _ProductionSubagentLifecycleBindings:
    def __init__(self, components: ProductionRunComponentsFactory) -> None:
        self._components = components

    def binding_for(self, parent_run_id: str) -> Any:
        bundle = self._components.active_hook_bundle(parent_run_id)
        if bundle is None or bundle.subagent_lifecycle is None or bundle.context is None:
            return None
        return bundle.subagent_lifecycle, bundle.context


class _RootAuthorityProvider:
    def __init__(
        self,
        *,
        workspace_id: str,
        unit_of_work: SqliteUnitOfWorkFactory,
        components: ProductionRunComponentsFactory,
        clock: Clock,
    ) -> None:
        self._workspace_id = workspace_id
        self._unit_of_work = unit_of_work
        self._components = components
        self._clock = clock

    async def authority_for(self, run_id: str) -> ParentRunAuthority:
        async with self._unit_of_work.begin() as uow:
            run = await uow.entities.get("runs", run_id)
            state = await uow.entities.get("run_states", run_id)
            turn = None if not isinstance(run, Run) else await uow.entities.get("turns", run.turn_id)
        if (
            not isinstance(run, Run)
            or not isinstance(state, RunState)
            or run.workspace_id != self._workspace_id
            or state.workspace_id != run.workspace_id
            or state.session_id != run.session_id
            or state.turn_id != run.turn_id
            or state.run_id != run.run_id
            or state.lineage != run.lineage
            or state.phase.value != run.status.value
        ):
            raise ValueError("parent Run authority is unavailable")
        registry = self._components.registry_for_run(run_id)
        if registry is None:
            raise ValueError("parent Run Tool Registry snapshot is unavailable")
        config = validate_wire(WireRunConfigSnapshot, thaw_json(run.config_snapshot))
        if run.status.is_terminal:
            remaining = AgentBudget(0, 0, 0, 0, 0.0, 0, 0, 0)
            deadline = run.deadline_at or run.updated_at
        else:
            ledger = self._components.root_ledger(run_id)
            snapshot = await ledger.snapshot(now=self._clock.utcnow())
            remaining = _remaining_agent_budget(ledger.budget, snapshot)
            deadline = run.deadline_at or ledger.started_at + timedelta(seconds=ledger.budget.max_wall_seconds)
        input_value = getattr(turn, "input_blocks", ())
        context = {"input": [dict(item) for item in input_value]}
        scope = _capability_scope(registry.definitions, config)
        return ParentRunAuthority(
            workspace_id=run.workspace_id,
            session_id=run.session_id,
            turn_id=run.turn_id,
            lineage=run.lineage,
            permission_mode=_permission_mode(config.permission_mode),
            effective_scope=scope,
            tool_definitions=registry.definitions,
            registry_snapshot_hash=registry.snapshot_hash,
            remaining_budget=remaining,
            deadline_at=deadline,
            context=context,
            run_config=run.config_snapshot,
            can_spawn_children=(
                "agent.spawn" in scope.allowed_tools
                and remaining.child_count > 0
                and remaining.model_calls > 0
                and not run.status.is_terminal
            ),
            active=not run.status.is_terminal,
        )


class _SubagentAuthorityResolver(SubagentCommandAuthorityResolver):
    def __init__(self, uow: SqliteUnitOfWorkFactory) -> None:
        self._uow = uow

    async def resolve(
        self,
        context: ApplicationCommandContext,
        target_run_id: str,
    ) -> SubagentCommandAuthority:
        del context
        async with self._uow.begin() as uow:
            raw = await uow.entities.get("subagent_runs", target_run_id)
            run = await uow.entities.get("runs", target_run_id)
            sequence = await uow.events.latest_sequence(target_run_id)
        if raw is None or not isinstance(run, Run):
            raise ValueError("Subagent Run is unavailable")
        from offeragent_harness.subagents.serialization import run_record_from_value

        record = run_record_from_value(raw)
        return SubagentCommandAuthority(
            requester_run_id=record.parent_run_id,
            session_id=record.session_id,
            turn_id=record.turn_id,
            started_at=record.created_at,
            last_sequence=sequence,
        )


class _SubagentArtifactResolver(SubagentArtifactReferenceResolver):
    def __init__(self, artifacts: LocalArtifactStore) -> None:
        self._artifacts = artifacts

    async def resolve(
        self,
        *,
        requester_run_id: str,
        owner_run_id: str,
        artifact_ids: Sequence[str],
    ) -> tuple[Any, ...]:
        del requester_run_id
        from offeragent_harness.protocol.content import ArtifactRef, ArtifactSensitivity, ArtifactState

        output: list[ArtifactRef] = []
        for artifact_id in artifact_ids:
            metadata = await self._artifacts.metadata(artifact_id)
            if metadata is None or metadata.owner_run_id != owner_run_id:
                raise ValueError("Subagent Artifact ownership mismatch")
            output.append(
                ArtifactRef(
                    artifact_id=metadata.artifact_id,
                    content_hash=metadata.sha256,
                    media_type=metadata.mime_type,
                    size_bytes=metadata.byte_length,
                    sensitivity=ArtifactSensitivity(metadata.sensitivity.value),
                    state=ArtifactState(metadata.state.value),
                )
            )
        return tuple(output)


class _DiagnosticsOwnerAuthorizer(DiagnosticsOwnerRunAuthorizer):
    def __init__(self, workspace_id: str, uow: SqliteUnitOfWorkFactory) -> None:
        self._workspace_id = workspace_id
        self._uow = uow

    async def authorize(self, context: ApplicationCommandContext, owner_run_id: str) -> str:
        del context
        async with self._uow.begin() as uow:
            run = await uow.entities.get("runs", owner_run_id)
        if not isinstance(run, Run) or run.workspace_id != self._workspace_id:
            raise PermissionError("diagnostic Artifact owner Run is outside this Workspace")
        return owner_run_id


class _RuntimeDiagnostics:
    def __init__(self, application: ProductionWorkerApplication | None = None) -> None:
        self.application = application

    async def snapshot(self) -> Mapping[str, LogField]:
        application = self.application
        if application is None:
            raise ProductionWorkerError("runtime diagnostics are not bound to the production Worker")
        return {
            "ready": LogField(application.ready, DataClass.PUBLIC),
            "state": LogField("ready" if application.ready else "starting", DataClass.PUBLIC),
            "workerPid": LogField(os.getpid(), DataClass.PUBLIC),
            "runtimeVersion": LogField(application.runtime_version, DataClass.PUBLIC),
            "coreVersion": LogField(_semantic_version(__version__), DataClass.PUBLIC),
            "protocolVersion": LogField(PROTOCOL_VERSION, DataClass.PUBLIC),
            "schemaHash": LogField(schema_hash(), DataClass.PUBLIC),
            "databaseIdentity": LogField(application.database_identity, DataClass.IDENTIFIER),
        }


class _ProcessDiagnostics:
    def __init__(self, host_pid: int, supervisor: _WorkerProcessSupervisor) -> None:
        self._host_pid = host_pid
        self._supervisor = supervisor

    async def processes(self) -> Sequence[DiagnosticProcess]:
        base = (
            DiagnosticProcess("host", self._host_pid, "running", True),
            DiagnosticProcess("worker", os.getpid(), "running", True),
        )
        snapshot = getattr(self._supervisor, "active_processes", None)
        if not callable(snapshot):
            return base
        try:
            active = await snapshot()
        except Exception:
            return base
        children: list[DiagnosticProcess] = []
        for item in active:
            pid = getattr(item, "pid", None)
            owner = getattr(getattr(item, "owner_kind", None), "value", None)
            state = getattr(getattr(item, "state", None), "value", None)
            if (
                not isinstance(pid, int)
                or pid < 1
                or not isinstance(owner, str)
                or owner not in {"shell", "parser", "hook"}
                or not isinstance(state, str)
            ):
                continue
            diagnostic_state = {"starting": "starting", "running": "running", "terminating": "stopping"}.get(
                state,
                "failed",
            )
            children.append(DiagnosticProcess(owner, pid, diagnostic_state, True))
        return (*base, *sorted(children, key=lambda value: (value.role, value.pid)))


class _LosslessCompactionRunner(SessionCompactionRunner):
    """Persist an exact bounded event manifest before replacing context projection."""

    def __init__(
        self,
        artifacts: LocalArtifactStore,
        clock: Clock,
        ids: IdGenerator,
        *,
        components: ProductionRunComponentsFactory | None = None,
        hook_budget: BudgetLedger | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._clock = clock
        self._ids = ids
        self._components = components
        self._hook_budget = hook_budget

    async def compact(
        self,
        *,
        workspace_id: str,
        session_id: str,
        through_turn_id: str,
        selected_run: Run,
        events: Sequence[StoredEvent],
        force: bool,
        cancellation: CancellationToken,
    ) -> CompactionExecution:
        del through_turn_id, force
        cancellation.checkpoint()
        payload = canonical_json_bytes(
            {
                "schemaVersion": 1,
                "runId": selected_run.run_id,
                "events": [
                    {
                        "sequence": event.sequence,
                        "eventId": event.event_id,
                        "eventType": event.event_type,
                        "payload": event.payload,
                    }
                    for event in events
                ],
            }
        )
        if self._components is not None and self._hook_budget is not None:
            bundle = await self._components.persisted_hook_bundle(
                selected_run.run_id,
                self._hook_budget,
                cancellation,
            )
            if bundle is not None and bundle.compaction is not None:
                binding = bundle.compaction
                outcome = await binding.hooks.invoke(
                    HookInvocation(
                        invocation_id=(f"compact:{selected_run.run_id}:{events[0].sequence}:{events[-1].sequence}"),
                        chain_id=f"agent:{selected_run.run_id}",
                        event=HookEvent.BEFORE_COMPACT,
                        context=binding.context,
                        run_id=selected_run.run_id,
                        facts={
                            "recordCount": len(events),
                            "estimatedBytes": len(payload),
                            "sequenceStart": events[0].sequence,
                            "sequenceEnd": events[-1].sequence,
                            "sessionId": session_id,
                        },
                    ),
                    cancellation,
                )
                if outcome.decision is not HookDecision.CONTINUE:
                    raise ProductionWorkerError(f"BeforeCompact Hook returned {outcome.decision.value}")
        from offeragent_harness.ports import ArtifactMetadata, ArtifactState, Sensitivity

        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        metadata = ArtifactMetadata(
            artifact_id=self._ids.new_id("artifact"),
            workspace_id=workspace_id,
            owner_run_id=selected_run.run_id,
            mime_type="application/vnd.offeragent.compaction-manifest+json",
            byte_length=len(payload),
            sha256=digest,
            sensitivity=Sensitivity.WORKSPACE,
            state=ArtifactState.COMPLETE,
            created_at=self._clock.utcnow(),
            attributes={"kind": "lossless-context-boundary", "originalEventsRetained": True},
        )
        stored = await self._artifacts.put(
            metadata,
            payload,
            idempotency_key=f"compaction:{selected_run.run_id}:{events[-1].sequence}:{digest}",
        )
        return CompactionExecution(
            summary_artifact=stored,
            replaced_turn_count=1,
            replaced_sequence_start=events[0].sequence,
            replaced_sequence_end=events[-1].sequence,
            model="lossless-context-boundary-v1",
        )


class _KernelOnlyVaultTransactions:
    """Deny the legacy VaultPort write surface; ToolKernel owns all writes."""

    async def execute(self, transaction: Any, cancellation: CancellationToken) -> ToolResult:
        del transaction
        cancellation.checkpoint()
        raise PermissionError("Vault transactions must enter through the unified Tool Kernel")


class _ProductionNamedPipeServer:
    def __init__(
        self,
        *,
        state_directory: Path,
        dispatcher: RuntimeApplicationCommandDispatcher,
        event_hub: _EventHub,
        channels: _ReverseChannels,
        clock: Clock,
        response_flushed: Callable[[str], None] | None = None,
        request_finalized: Callable[[str], None] | None = None,
    ) -> None:
        self._store = DiscoveryMaterialStore(
            state_directory / "transport",
            protector=DpapiCurrentUserProtector(),
        )
        self._dispatcher = dispatcher
        self._event_hub = event_hub
        self._channels = channels
        self._clock = clock
        self._response_flushed = response_flushed
        self._request_finalized = request_finalized
        self._material: Any = None
        self._listener: Win32NamedPipeListener | None = None
        self._accept_task: asyncio.Task[None] | None = None
        self._connections: set[DuplexJsonRpcConnection] = set()
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._replay = HandshakeReplayGuard()

    @property
    def accept_task(self) -> asyncio.Task[None] | None:
        return self._accept_task

    @property
    def healthy(self) -> bool:
        return self._listener is not None and self._accept_task is not None and not self._accept_task.done()

    async def start(self) -> None:
        self._material = self._store.issue(now=self._clock.utcnow())
        self._listener = Win32NamedPipeListener(self._material.pipe_name)
        self._accept_task = asyncio.create_task(self._accept_loop(), name="offeragent-worker-pipe")

    async def stop(self) -> None:
        failures: list[BaseException] = []
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                await listener.close()
            except BaseException as error:
                failures.append(error)
        task = self._accept_task
        self._accept_task = None
        if task is not None:
            task.cancel()
            # Closing the native listener completes a pending accept with
            # ERROR_OPERATION_ABORTED on Windows.  That task result is an
            # expected consequence of revocation, not a cleanup failure.
            await asyncio.gather(task, return_exceptions=True)
        connections = tuple(self._connections)
        self._connections.clear()
        connection_results = await asyncio.gather(
            *(connection.close() for connection in connections),
            return_exceptions=True,
        )
        failures.extend(result for result in connection_results if isinstance(result, BaseException))
        clients = tuple(self._client_tasks)
        self._client_tasks.clear()
        current = asyncio.current_task()
        pending_clients = tuple(client for client in clients if client is not current)
        for client in pending_clients:
            client.cancel()
        await asyncio.gather(*pending_clients, return_exceptions=True)
        try:
            # Discovery is an ingress capability.  Revoke it even when an
            # earlier listener/connection cleanup reports an error so a dead
            # Worker can never remain discoverable.
            self._store.remove()
        except BaseException as error:
            failures.append(error)
        if failures:
            raise ProductionWorkerError("Worker Named Pipe shutdown was incomplete") from failures[0]

    async def _accept_loop(self) -> None:
        listener = self._listener
        material = self._material
        if listener is None or material is None:
            raise RuntimeError("Named Pipe listener was not composed")
        while True:
            stream = await listener.accept()
            task = asyncio.create_task(self._serve(stream, material), name="offeragent-worker-pipe-client")
            self._client_tasks.add(task)
            task.add_done_callback(self._client_finished)

    def _client_finished(self, task: asyncio.Task[None]) -> None:
        self._client_tasks.discard(task)
        # Authentication failures and peer disconnects are connection-local,
        # but their task exceptions must still be observed so they cannot
        # become process-level "Task exception was never retrieved" noise.
        if not task.cancelled():
            task.exception()

    async def _serve(self, stream: Any, material: Any) -> None:
        connection: DuplexJsonRpcConnection | None = None
        try:
            await authenticate_server_stream(
                stream,
                material,
                now=self._clock.utcnow,
                replay_guard=self._replay,
            )
            connection = DuplexJsonRpcConnection(
                stream,
                role=ConnectionRole.SERVER,
                dispatcher=self._dispatcher,
                response_flushed=self._response_flushed,
                request_finalized=self._request_finalized,
            )
            self._connections.add(connection)
            await connection.start()
            await connection.wait_ready()
            await self._channels.add(connection.connection_id, connection)
            await self._event_hub.add(connection)
            await connection.wait_closed()
        finally:
            if connection is not None:
                self._connections.discard(connection)
                await self._channels.remove(connection.connection_id, connection)
                await self._event_hub.remove(connection)
            else:
                await stream.close()


class _WorkerControl(WorkerControlHandler):
    def __init__(self, application: ProductionWorkerApplication) -> None:
        self._application = application

    def readiness(self) -> Any:
        from offeragent_harness.runtime.host_supervisor import WorkerReadiness

        if not self._application.ready:
            raise ProductionWorkerError("Worker recovery/readiness gate is not open")
        return WorkerReadiness(
            pid=os.getpid(),
            runtime_version=self._application.runtime_version,
            workspace_instance_id=self._application.workspace_instance_id,
            canonical_root_identity=self._application.canonical_root_identity,
            database_identity=self._application.database_identity,
        )

    async def revalidate(self) -> Any:
        from offeragent_harness.runtime.approval_manager import ApprovalRecord
        from offeragent_harness.runtime.host_supervisor import ResumeValidation

        valid_root = identify_workspace_root(self._application.vault_root).identity_hash
        valid_db = workspace_database_identity(self._application.workspace_instance_id)
        if valid_db != self._application.database_identity:
            return ResumeValidation(False, False, False, False, False)
        active_runs: list[Run] = []
        bindings: dict[str, str] = {}
        approval_records: list[ApprovalRecord] = []
        async with self._application.unit_of_work.begin() as uow:
            after_id: str | None = None
            while True:
                page = await uow.entities.list("runs", after_id=after_id, limit=100)
                if not page:
                    break
                for record in page:
                    if isinstance(record.value, Run) and not record.value.status.is_terminal:
                        active_runs.append(record.value)
                after_id = page[-1].entity_id
            for run in active_runs:
                binding = await uow.entities.get("run_client_bindings", run.run_id)
                if isinstance(binding, Mapping) and isinstance(binding.get("clientConnectionId"), str):
                    bindings[run.run_id] = cast(str, binding["clientConnectionId"])
            after_id = None
            while True:
                page = await uow.entities.list("approvals", after_id=after_id, limit=100)
                if not page:
                    break
                approval_records.extend(record.value for record in page if isinstance(record.value, ApprovalRecord))
                after_id = page[-1].entity_id
        now = self._application.clock.utcnow()
        deadlines_valid = all(run.deadline_at is not None and now < run.deadline_at for run in active_runs)
        pipe_valid = all(self._application.channels.contains(client_id) for client_id in bindings.values())
        approvals_valid = True
        for run in active_runs:
            if run.status.value != "awaiting_approval":
                continue
            matching = tuple(
                record
                for record in approval_records
                if record.request.binding.run_id == run.run_id
                and record.request.binding.expires_at > now
                and record.resolution is None
            )
            if not matching:
                approvals_valid = False
                break
        return ResumeValidation(
            deadlines_valid=deadlines_valid,
            vault_hash_valid=valid_root == self._application.canonical_root_identity,
            named_pipe_client_valid=pipe_valid,
            # No provider adapter currently exposes a resumable connection
            # health proof.  Active model work therefore fails closed.
            model_connection_valid=not active_runs,
            approvals_valid=approvals_valid,
        )

    async def reject_new_runs(self) -> None:
        self._application.reject_new_runs = True

    async def graceful_shutdown(self) -> WorkerShutdownReceipt:
        from offeragent_harness.runtime.host_supervisor import WorkerShutdownReceipt

        self._application.reject_new_runs = True
        self._application.begin_shutdown_delivery()
        receipt = await self._application.commit_shutdown()
        if not isinstance(receipt, WorkerShutdownReceipt):
            raise ProductionWorkerError("Worker shutdown did not produce a verified receipt")
        if not receipt.safely_committed:
            raise ProductionWorkerError("Worker shutdown receipt does not prove durable state")
        return receipt


@dataclass(slots=True)
class ProductionWorkerApplication(WorkerApplication):
    workspace_id: str
    workspace_instance_id: str
    canonical_root_identity: str
    database_identity: str
    vault_root: Path
    state_directory: Path
    host_pid: int
    runtime_version: str
    runtime_config: HarnessConfig
    config_service: ConfigService
    config_activation: WorkerConfigActivation
    approvals: ApprovalManager
    clock: Clock
    logger: LocalJsonLogger
    harness_application: HarnessApplication
    dispatcher: RuntimeApplicationCommandDispatcher
    gateway: LoopbackWebGateway | None
    loopback: AsyncioLoopbackServer | None
    channels: _ReverseChannels
    local_vault_transaction: VaultTransactionCoordinator
    headless_vault_write: HeadlessVaultWriteAuthority
    event_hub: _EventHub
    unit_of_work: SqliteUnitOfWorkFactory
    subagents: SubagentService
    components: ProductionRunComponentsFactory
    scheduler: ChildRunScheduler
    turn_manager: TurnManager
    process_supervisor: _WorkerProcessSupervisor
    native_transports: bool
    _pipe: _ProductionNamedPipeServer | None = None
    _control: WorkerControlServer | None = None
    _control_task: asyncio.Task[None] | None = None
    _ready: bool = False
    _shutdown_task: asyncio.Task[None] | None = None
    _shutdown_committed: bool = False
    _shutdown_commit_task: asyncio.Task[WorkerShutdownReceipt] | None = None
    _shutdown_delivery_started: bool = False
    _shutdown_delivery_finalized: asyncio.Event = field(default_factory=asyncio.Event)
    _shutdown_delivery_task: asyncio.Task[None] | None = None
    _stopped: bool = False
    _transport_shutdown_task: asyncio.Task[None] | None = None
    _fatal_error: ProductionWorkerError | None = None
    reject_new_runs: bool = False
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    _background_tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def ready(self) -> bool:
        transport_healthy = self.loopback is None or self.loopback.healthy
        if self.native_transports:
            transport_healthy = transport_healthy and (
                self._pipe is not None
                and self._pipe.healthy
                and self._control is not None
                and self._control_task is not None
                and not self._control_task.done()
            )
        return self._ready and not self._stopped and self._fatal_error is None and transport_healthy

    @property
    def harness(self) -> HarnessService:
        return self.harness_application.require_ready()

    @property
    def database_path(self) -> Path:
        return self.state_directory / "state.sqlite"

    @property
    def worker_pid(self) -> int:
        return os.getpid()

    @property
    def loopback_worker_pid(self) -> int:
        gateway = self.gateway
        if gateway is None:
            raise ProductionWorkerError("Loopback Web is disabled for this Worker")
        return gateway.config.worker_pid

    @property
    def protocol_capabilities(self) -> CapabilitySet:
        """Expose the fixed Runtime protocol surface, independent of Workspace policy."""

        return _protocol_capabilities()

    async def _emit_runtime_log(
        self,
        level: LogLevel,
        event: str,
        message: str,
        **metrics: int | float | bool | str,
    ) -> None:
        correlation = TraceCorrelation(
            trace_id=f"trace_runtime_{hashlib.sha256(self.workspace_id.encode()).hexdigest()[:24]}",
            workspace_id=self.workspace_id,
        )
        fields = {
            key: LogField(value, DataClass.METRIC if isinstance(value, (int, float, bool)) else DataClass.PUBLIC)
            for key, value in metrics.items()
        }
        try:
            await self.logger.emit(
                level,
                event,
                message,
                correlation,
                fields,
                occurred_at=self.clock.utcnow(),
            )
        except Exception:
            return

    def _track_background_task(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_finished)

    def _background_task_finished(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _transport_listener_finished(self, component: str, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        # Observe the listener result before scheduling process-fatal cleanup.
        error = task.exception()
        if self._shutdown_committed:
            return
        self._schedule_fatal_shutdown(component=component, error=error)

    def _schedule_fatal_shutdown(self, *, component: str, error: BaseException | None) -> None:
        if self._fatal_error is not None:
            return
        self._fatal_error = ProductionWorkerError(f"Worker {component} component terminated unexpectedly")
        self.reject_new_runs = True

        async def fail_worker() -> None:
            await self._emit_runtime_log(
                LogLevel.ERROR,
                "runtime.required_component_failed",
                "A required Worker component terminated unexpectedly.",
                component=component,
                errorType=type(error).__name__ if error is not None else "UnexpectedCompletion",
            )
            try:
                await self.commit_shutdown()
            except BaseException as commit_error:
                await self._emit_runtime_log(
                    LogLevel.ERROR,
                    "runtime.fatal_shutdown_commit_failed",
                    "Fatal Worker shutdown could not commit all durable state.",
                    errorType=type(commit_error).__name__,
                )
            try:
                await self._finish_transport_shutdown()
            except BaseException:
                # ``wait_stopped`` awaits the same single-flight task and
                # reports its authoritative teardown failure to the process.
                pass
            finally:
                # Commit failure can precede creation/completion of a normal
                # success signal.  A fatal listener loss must still wake the
                # process main loop so it exits instead of claiming readiness.
                self._shutdown_event.set()

        self._track_background_task(asyncio.create_task(fail_worker(), name=f"offeragent-{component}-fatal-shutdown"))

    def begin_shutdown_delivery(self) -> None:
        """Arm one terminal continuation before a transport starts shutdown.

        The continuation is independent of the request handler.  It therefore
        survives peer cancellation and observes the shared commit Task even if
        the original waiter disappears.
        """

        if self._shutdown_delivery_started:
            return
        self._shutdown_delivery_started = True
        task = asyncio.create_task(
            self._run_shutdown_delivery_terminal(),
            name="offeragent-worker-shutdown-delivery",
        )
        self._shutdown_delivery_task = task
        self._track_background_task(task)

    def finalize_shutdown_delivery(self) -> None:
        """Record that the shutdown reply was flushed or cannot be delivered."""

        if self._shutdown_delivery_started:
            self._shutdown_delivery_finalized.set()

    async def _run_shutdown_delivery_terminal(self) -> None:
        await self._shutdown_delivery_finalized.wait()
        commit_task = self._shutdown_commit_task
        if commit_task is None:
            if self._fatal_error is None:
                self._fatal_error = ProductionWorkerError(
                    "Worker shutdown delivery ended before durable shutdown commit began"
                )
        else:
            try:
                receipt = await asyncio.shield(commit_task)
                if not receipt.safely_committed and self._fatal_error is None:
                    self._fatal_error = ProductionWorkerError(
                        "Worker shutdown commit did not prove durable interrupted state"
                    )
            except BaseException as error:
                if self._fatal_error is None:
                    self._fatal_error = ProductionWorkerError("Worker shutdown commit failed after delivery began")
                await self._emit_runtime_log(
                    LogLevel.ERROR,
                    "runtime.shutdown_delivery_commit_failed",
                    "A transport shutdown request ended after its durable commit failed.",
                    errorType=type(error).__name__,
                )
        self.reject_new_runs = True
        try:
            await self._finish_transport_shutdown()
        except BaseException:
            # The process waiter observes the same transport Task and reports
            # its authoritative failure.  This continuation must stay consumed.
            pass

    def _application_request_finalized(self, method: str) -> None:
        if method == "shutdown":
            self.finalize_shutdown_delivery()

    async def _start_loopback_web(self, *, enabled: bool) -> None:
        if not enabled:
            return
        if self.gateway is not None or self.loopback is not None:
            raise ProductionWorkerError("Loopback Web listener is already configured")
        gateway = LoopbackWebGateway(
            config=LoopbackGatewayConfig(
                workspace_id=self.workspace_id,
                workspace_instance_id=self.workspace_instance_id,
                worker_pid=os.getpid(),
            ),
            clock=self.clock,
            dispatcher=self.dispatcher,
        )
        loopback = AsyncioLoopbackServer(
            gateway,
            request_finalized=self._application_request_finalized,
            terminal_response=lambda method: method == "shutdown",
        )
        self.gateway = gateway
        self.loopback = loopback
        try:
            await loopback.start()
        except BaseException:
            self.loopback = None
            self.gateway = None
            raise
        loopback_closed_task = loopback.closed_task
        if loopback_closed_task is None:
            raise ProductionWorkerError("Worker loopback listener did not start")
        loopback_closed_task.add_done_callback(
            lambda completed: self._transport_listener_finished("loopback", completed)
        )

    async def start(self) -> object:
        if self._ready or self._stopped:
            raise ProductionWorkerError("Worker application can only start once")
        await self._emit_runtime_log(
            LogLevel.INFO,
            "runtime.starting",
            "Worker startup began.",
            workerPid=os.getpid(),
        )
        # File-CAS manifests are reconciled before any component can expose or mutate the Vault.
        vault_recovery = await self.local_vault_transaction.recover_after_restart()
        if vault_recovery.manual_review_paths:
            await self._emit_runtime_log(
                LogLevel.ERROR,
                "runtime.vault_recovery_blocked",
                "Worker readiness is blocked by unresolved durable Vault transactions.",
                blockedPathCount=len(vault_recovery.manual_review_paths),
            )
            raise ProductionWorkerError("Worker readiness is blocked by unresolved durable Vault transaction manifests")
        await self.headless_vault_write.recover_after_restart()
        layer = await self.config_service.layer(ConfigScope.WORKSPACE, self.workspace_id)
        if layer.revision == 0:
            await self.config_service.update(
                ConfigUpdateCommand(
                    scope=ConfigScope.WORKSPACE,
                    owner_id=self.workspace_id,
                    expected_revision=0,
                    idempotency_key="production-worker-bootstrap",
                    actor_id="config-bootstrap",
                    patch=ConfigPatch.model_validate(self.runtime_config.model_dump(mode="python")),
                )
            )
        worker_config = await self.config_service.snapshot(
            managed_owner_id=_LOCAL_MANAGED_ID,
            profile_id=_LOCAL_PROFILE_ID,
            workspace_id=self.workspace_id,
        )
        # Ingress opens only after the new Worker freezes the durable effective
        # values that are active for this process lifetime.  Later config writes
        # can request a restart, but cannot rebind this baseline in-process.
        self.config_activation.freeze(worker_config.config)
        self.components.bind_worker_read_limit(worker_config.config.budgets.max_parallel_reads)
        report = await self.harness_application.start()
        await self._start_loopback_web(enabled=worker_config.config.ui.loopback_web_enabled)
        if self.native_transports:
            self._pipe = _ProductionNamedPipeServer(
                state_directory=self.state_directory,
                dispatcher=self.dispatcher,
                event_hub=self.event_hub,
                channels=self.channels,
                clock=SystemClock(),
                request_finalized=self._application_request_finalized,
            )
            await self._pipe.start()
            pipe_accept_task = self._pipe.accept_task
            if pipe_accept_task is None:
                raise ProductionWorkerError("Worker Named Pipe listener did not start")
            pipe_accept_task.add_done_callback(
                lambda completed: self._transport_listener_finished("named_pipe", completed)
            )
            protector = DpapiCurrentUserProtector()
            self._control = WorkerControlServer(
                store=DiscoveryMaterialStore(self.state_directory / "control", protector=protector),
                handler=_WorkerControl(self),
                shutdown_request_finalized=lambda: self._application_request_finalized("shutdown"),
            )
            await self._control.start()
            self._control_task = asyncio.create_task(
                self._control.serve_forever(),
                name="offeragent-worker-control",
            )
            self._control_task.add_done_callback(
                lambda completed: self._transport_listener_finished("control", completed)
            )
        self._ready = True
        await self._emit_runtime_log(
            LogLevel.INFO,
            "runtime.ready",
            "Worker is ready.",
            workerPid=os.getpid(),
            nativeTransports=self.native_transports,
        )
        return report

    async def shutdown(self, *, grace_seconds: float = 10.0) -> None:
        if self._stopped:
            return
        task = self._shutdown_task
        if task is None:
            task = asyncio.create_task(
                self._run_direct_shutdown(grace_seconds=grace_seconds),
                name="offeragent-worker-direct-shutdown",
            )
            self._shutdown_task = task
            task.add_done_callback(self._direct_shutdown_finished)
        await asyncio.shield(task)

    def _direct_shutdown_finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _run_direct_shutdown(self, *, grace_seconds: float) -> None:
        commit_error: BaseException | None = None
        try:
            receipt = await self.commit_shutdown(grace_seconds=grace_seconds)
            if not receipt.safely_committed:
                raise ProductionWorkerError("Worker direct shutdown did not prove durable interrupted state")
        except BaseException as error:
            commit_error = error
            if self._fatal_error is None:
                self._fatal_error = ProductionWorkerError("Worker direct shutdown commit failed")
        transport_error: BaseException | None = None
        try:
            await self._finish_transport_shutdown()
        except BaseException as error:
            transport_error = error
        if transport_error is not None:
            raise transport_error
        if commit_error is not None:
            raise commit_error

    async def commit_shutdown(self, *, grace_seconds: float = 10.0) -> WorkerShutdownReceipt:
        """Persist/cancel Runtime state before acknowledging Host shutdown.

        The first caller owns the one commit Task.  Every concurrent or later
        caller awaits that exact Task, so both a receipt and a failure remain
        authoritative rather than being reconstructed from mutable flags.
        """

        task = self._shutdown_commit_task
        if task is None:
            task = asyncio.create_task(
                self._commit_shutdown_once(grace_seconds=grace_seconds),
                name="offeragent-worker-shutdown-commit",
            )
            self._shutdown_commit_task = task
            self._shutdown_committed = True
            task.add_done_callback(self._shutdown_commit_finished)
        return await asyncio.shield(task)

    def _shutdown_commit_finished(self, task: asyncio.Task[WorkerShutdownReceipt]) -> None:
        if not task.cancelled():
            task.exception()

    async def _commit_shutdown_once(self, *, grace_seconds: float) -> WorkerShutdownReceipt:
        await self._emit_runtime_log(
            LogLevel.INFO,
            "runtime.stopping",
            "Worker shutdown began.",
            graceSeconds=grace_seconds,
        )
        try:
            self.reject_new_runs = True
            hook_scope = CancellationScope(name="runtime-shutdown-hook")
            try:
                hook_config = await self.config_service.snapshot(
                    managed_owner_id=_LOCAL_MANAGED_ID,
                    profile_id=_LOCAL_PROFILE_ID,
                    workspace_id=self.workspace_id,
                    session_id=None,
                )
                await self.components.runtime_shutdown_hook(
                    shutdown_id=self.workspace_instance_id,
                    reason_code="worker_shutdown",
                    effective_config=hook_config.config,
                    cancellation=hook_scope,
                )
            except Exception as error:
                self.harness_application._harness.diagnostics.cleanup_failures.append(
                    f"RuntimeShutdown Hook failed: {type(error).__name__}"
                )
            finally:
                await hook_scope.close()
            active_before = tuple(item.run_id for item in await self.turn_manager.active_runs())
            await self.scheduler.shutdown()
            await self.harness_application.shutdown(grace_seconds=grace_seconds)
            await self.process_supervisor.shutdown()
            self._ready = False
            active_after = await self.turn_manager.active_runs()
            persisted = True
            for run_id in active_before:
                try:
                    async with self.unit_of_work.begin() as uow:
                        run = await uow.entities.get("runs", run_id)
                except Exception:
                    persisted = False
                    break
                if not isinstance(run, Run) or not run.status.is_terminal:
                    persisted = False
                    break
            from offeragent_harness.runtime.host_supervisor import WorkerShutdownReceipt

            receipt = WorkerShutdownReceipt(
                new_runs_rejected=self.reject_new_runs,
                active_runs_cancelled=not active_after,
                interrupted_state_persisted=persisted,
                worker_state_flushed=persisted and not active_after,
            )
            await self._emit_runtime_log(
                LogLevel.INFO,
                "runtime.shutdown_committed",
                "Worker shutdown state was committed.",
                activeRunsCancelled=not active_after,
                interruptedStatePersisted=persisted,
            )
            return receipt
        except BaseException as error:
            await self._emit_runtime_log(
                LogLevel.ERROR,
                "runtime.shutdown_failed",
                "Worker shutdown failed before a receipt was committed.",
                errorType=type(error).__name__,
            )
            raise

    async def _finish_transport_shutdown(self) -> None:
        task = self._ensure_transport_shutdown_task()
        # Caller cancellation must not interrupt process-wide capability
        # revocation.  Every concurrent shutdown observes the same result.
        await asyncio.shield(task)

    def _ensure_transport_shutdown_task(self) -> asyncio.Task[None]:
        task = self._transport_shutdown_task
        if task is None:
            # Always run teardown in its own task.  A caller can be a Pipe or
            # control request handler that teardown itself must close; making
            # that handler the teardown owner would let it cancel itself.
            task = asyncio.create_task(
                self._run_transport_shutdown(),
                name="offeragent-worker-transport-teardown",
            )
            self._transport_shutdown_task = task
            task.add_done_callback(self._transport_shutdown_finished)
        return task

    def _transport_shutdown_finished(self, task: asyncio.Task[None]) -> None:
        # ``wait_stopped`` will await the same Task and re-raise its result to
        # the process main loop.  Retrieving it here also prevents an
        # unobserved-task warning if the Host terminates before that waiter runs.
        if not task.cancelled():
            task.exception()

    async def _run_transport_shutdown(self) -> None:
        try:
            failures: list[BaseException] = []
            pipe = self._pipe
            self._pipe = None
            if pipe is not None:
                try:
                    await pipe.stop()
                except BaseException as error:
                    failures.append(error)
            loopback = self.loopback
            self.loopback = None
            self.gateway = None
            if loopback is not None:
                try:
                    await loopback.stop()
                except BaseException as error:
                    failures.append(error)
            # Keep the Host-owned control plane available until every
            # application ingress capability has been revoked.  Host stop-all
            # can then join an in-progress application shutdown instead of
            # mistaking a disappearing control endpoint for a hung Worker and
            # killing the Job before discovery cleanup completes.
            control = self._control
            self._control = None
            if control is not None:
                try:
                    await control.close()
                except BaseException as error:
                    failures.append(error)
            task = self._control_task
            self._control_task = None
            current = asyncio.current_task()
            if task is not None and task is not current:
                task.cancel()
                # Listener close intentionally aborts a pending native accept.
                # The authoritative cleanup result is control.close() above.
                await asyncio.gather(task, return_exceptions=True)
            if failures:
                await self._emit_runtime_log(
                    LogLevel.ERROR,
                    "runtime.transport_shutdown_failed",
                    "Worker transport shutdown was incomplete.",
                    failureCount=len(failures),
                )
                raise ProductionWorkerError("Worker transport shutdown was incomplete") from failures[0]
            # This is a completion flag, not a claim/lock.  Set it only after
            # all ingress capabilities and loopback listeners are removed.
            self._stopped = True
            await self._emit_runtime_log(
                LogLevel.INFO,
                "runtime.stopped",
                "Worker transports stopped.",
                workerPid=os.getpid(),
            )
        finally:
            # Completion and success are distinct.  Failures must wake the
            # process waiter, which then awaits this exact Task and exits nonzero.
            self._shutdown_event.set()

    async def wait_stopped(self) -> None:
        await self._shutdown_event.wait()
        task = self._transport_shutdown_task
        if task is not None:
            await asyncio.shield(task)
        if self._fatal_error is not None:
            raise self._fatal_error
        if not self._stopped:
            raise ProductionWorkerError("Worker stopped without completing transport revocation")

    def schedule_transport_shutdown(self) -> None:
        # Callers invoke this only from an explicit response-flushed hook.  Task
        # creation yields no control, so the request handler returns before the
        # independent teardown can cancel connection tasks.
        self._ensure_transport_shutdown_task()


class ProductionWorkerCompositionRoot(WorkerCompositionRoot):
    """Concrete single-use Worker composition root."""

    def __init__(
        self,
        *,
        canonical_root_identity: str,
        database_identity: str,
        runtime_version: str,
        build_commit: str,
        overrides: ProductionWorkerOverrides | None = None,
    ) -> None:
        self._canonical_root_identity = canonical_root_identity
        self._database_identity = database_identity
        self._runtime_version = _semantic_version(runtime_version)
        if re.fullmatch(r"[0-9a-f]{7,64}", build_commit) is None:
            raise ProductionWorkerError("Worker build commit identity is invalid")
        self._build_commit = build_commit
        self._overrides = overrides or ProductionWorkerOverrides()
        self._consumed = False

    def build(self, bootstrap: WorkerBootstrap) -> WorkerApplication:
        if self._consumed:
            raise ProductionWorkerError("production Worker composition root is single-use")
        self._consumed = True
        return self._build(bootstrap)

    def _build(self, bootstrap: WorkerBootstrap) -> ProductionWorkerApplication:
        root_identity = identify_workspace_root(bootstrap.canonical_root)
        if root_identity.identity_hash != self._canonical_root_identity:
            raise ProductionWorkerError("Worker canonical Vault identity differs from Host bootstrap")
        expected_database = workspace_database_identity(bootstrap.workspace_instance_id)
        if expected_database != self._database_identity:
            raise ProductionWorkerError("Worker database identity differs from Host bootstrap")
        portable = read_portable_workspace_config(bootstrap.canonical_root)
        workspace_id = portable.portable_workspace_id
        state_directory = bootstrap.state_directory.resolve(strict=False)
        state_directory.mkdir(parents=True, exist_ok=True)
        if not state_directory.is_dir():
            raise ProductionWorkerError("Worker state directory is unavailable")
        database_path = state_directory / "state.sqlite"
        uow = SqliteUnitOfWorkFactory(database_path)
        clock = self._overrides.clock or SystemClock()
        ids = self._overrides.ids or SecureIdGenerator()
        logger = LocalJsonLogger(
            state_directory / "logs",
            workspace_instance_id=bootstrap.workspace_instance_id,
            allowed_root=state_directory,
        )
        metrics = MetricsRegistry()
        correlations = LocalRunCorrelationRegistry()
        run_observability = ProductionRunObservability(clock=clock, metrics=metrics)
        tool_observability = ProductionToolObservability(
            clock=clock,
            metrics=metrics,
            logger=logger,
            correlations=correlations,
        )
        network_audit = EntityNetworkAuditSink(uow.entity_store, ids)
        host_pid = self._overrides.host_pid or os.getppid()
        if host_pid < 1 or host_pid == os.getpid():
            raise ProductionWorkerError("Worker parent Host PID is invalid")
        config = self._overrides.runtime_config or HarnessConfig()
        secret_store = self._overrides.secret_store
        if secret_store is None:
            if os.name != "nt":
                raise ProductionWorkerError("production SecretStore requires Windows DPAPI")
            secret_store = WindowsDpapiSecretStore(state_directory / "secrets")

        def configured_gateway_factory(settings: ModelSettings, network_enabled: bool) -> ModelGateway:
            custom = self._overrides.model_gateway_factory
            if custom is not None:
                gateway = custom(settings)
            else:
                gateway = compose_model_gateway(
                    settings,
                    secret_scope_id=workspace_id,
                    secrets=secret_store,
                    network_enabled=network_enabled,
                    network_audit=network_audit,
                    clock=clock,
                    codex_credential_source=(
                        CodexFileCredentialSource()
                        if settings.provider is ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL
                        else None
                    ),
                )
            return InstrumentedModelGateway(
                gateway,
                provider_id=settings.provider.value,
                workspace_id=workspace_id,
                clock=clock,
                metrics=metrics,
                logger=logger,
                correlations=correlations,
            )

        def gateway_factory(settings: ModelSettings) -> ModelGateway:
            return configured_gateway_factory(settings, config.network.model_provider_enabled)

        artifacts = LocalArtifactStore(state_directory / "artifacts", workspace_id=workspace_id)
        runtime_budget = BudgetLedger(
            RunBudget(
                100_000,
                1_000_000,
                64,
                365 * 24 * 3600,
                2_000_000_000,
                2_000_000_000,
                Decimal("1000000"),
                4 * 1024**3,
                100_000,
            ),
            started_at=clock.utcnow(),
        )
        local_transaction = VaultTransactionCoordinator(
            workspace_id=workspace_id,
            vault_root=bootstrap.canonical_root,
            artifacts=artifacts,
            artifact_budget=runtime_budget,
            clock=clock,
            manifest_directory=state_directory / "vault-transactions",
            manifest_state_root=state_directory,
            journal=uow.invocation_journal,
            cas_barrier=self._overrides.vault_cas_barrier,
        )
        process_scratch_root = _prepare_process_scratch_root(state_directory)
        paths = WorkspacePathPolicy(
            bootstrap.canonical_root,
            additional_roots=(WorkspaceRoot("process-scratch", process_scratch_root),),
        )
        process_supervisor = self._overrides.process_supervisor
        if process_supervisor is None:
            if os.name != "nt":
                raise ProductionWorkerError("production ProcessSupervisor requires Windows")
            process_supervisor = cast(
                _WorkerProcessSupervisor,
                ProcessSupervisorService(
                    workspace_paths=paths,
                    executable_profiles=self._overrides.process_executable_profiles,
                    environment_profiles=self._overrides.process_environment_profiles,
                    backend=WindowsSupervisedProcessBackend(
                        workspace=SupervisedWorkspaceIdentity(
                            bootstrap.workspace_instance_id,
                            self._canonical_root_identity,
                            self._database_identity,
                        ),
                        verifier=PinnedProcessExecutableVerifier(
                            authenticode=WindowsAuthenticodeVerifier(),
                            release_manifest=self._overrides.process_release_manifest,
                        ),
                        sandbox_state_directory=state_directory / "process-sandbox",
                    ),
                    artifacts=artifacts,
                    clock=clock,
                    workspace_id=workspace_id,
                ),
            )
        executable_profiles = self._overrides.process_executable_profiles
        environment_profiles = self._overrides.process_environment_profiles
        allowed_cwd_roots = frozenset(
            root_id for profile in executable_profiles for root_id in profile.allowed_cwd_roots
        )
        executable_by_id = {item.executable_id: item for item in executable_profiles}
        environment_ids = {item.profile_id for item in environment_profiles}
        for profile in self._overrides.signed_shell_profiles:
            executable = executable_by_id.get(profile.executable_id)
            if (
                executable is None
                or executable.fingerprint != profile.executable_profile_fingerprint
                or profile.cwd_root_id not in executable.allowed_cwd_roots
                or profile.environment_profile_id not in executable.environment_profiles
                or profile.environment_profile_id not in environment_ids
            ):
                raise ProductionWorkerError(
                    f"Shell profile {profile.profile_id!r} is outside the shared process catalog"
                )
        shell_capabilities = ProductionShellBundleFactory(
            workspace_id=workspace_id,
            signed_builtin_profiles=self._overrides.signed_shell_profiles,
            unit_of_work=uow,
            processes=process_supervisor,
            clock=clock,
        )
        vault_fs = VaultFileSystem(
            workspace_id=workspace_id,
            paths=paths,
            read_policy=VaultReadPolicy(
                None,
                16 * 1024 * 1024,
                16 * 1024 * 1024,
                10_000,
                100_000,
                allowed_hidden_prefixes=(".claude", ".offeragent/memory"),
            ),
            transaction_executor=_KernelOnlyVaultTransactions(),
        )
        read_executor = CodeToolExecutor(
            workspace_id=workspace_id,
            source=vault_fs,
            workspace_root=bootstrap.canonical_root,
            ripgrep_path=_require_ripgrep_path(self._overrides.ripgrep_path),
        )
        powershell_executor = PowerShellToolExecutor(
            workspace_id=workspace_id,
            workspace_root=bootstrap.canonical_root,
            executable=_require_powershell_path(self._overrides.powershell_path),
        )
        event_hub = _EventHub()
        buffered = BufferedEventSink(
            event_hub,
            queue_length_observer=lambda length: metrics.set_gauge(MetricName.EVENT_QUEUE_LENGTH, length),
        )
        turn_manager = TurnManager(observer=run_observability)
        approvals = ApprovalManager(unit_of_work=uow, clock=clock)
        config_service = ConfigService(
            unit_of_work=uow,
            event_sink=buffered,
            clock=clock,
            ids=ids,
        )
        config_activation = WorkerConfigActivation()
        channels = _ReverseChannels(workspace_id)
        headless_vault_write = HeadlessVaultWriteAuthority(
            workspace_id=workspace_id,
            workspace_instance_id=bootstrap.workspace_instance_id,
            root_identity=self._canonical_root_identity,
            database_identity=self._database_identity,
            unit_of_work=uow,
            clock=clock,
            identity_probe=lambda: (
                identify_workspace_root(bootstrap.canonical_root).identity_hash,
                workspace_database_identity(bootstrap.workspace_instance_id),
            ),
            pipe_connection_count=lambda: channels.count,
        )
        channels.bind_headless_authority(headless_vault_write)
        from offeragent_harness.subagents.tools import subagent_tool_definitions

        subagent_definitions = subagent_tool_definitions()
        late_subagent = _LateToolExecutor()
        late_parent_authorities = _LateParentRunAuthorityProvider()
        configured_user_home = self._overrides.skill_user_home or Path.home()
        skill_runtime_root = self._overrides.skill_runtime_root or Path(sys.executable).resolve().parent
        skill_trust_verifier = self._overrides.skill_trust_verifier or ReleaseManifestSkillTrustVerifier(
            skill_runtime_root
        )
        skill_factory = ProductionSkillBundleFactory(
            workspace_id=workspace_id,
            workspace_root=bootstrap.canonical_root,
            runtime_root=skill_runtime_root,
            user_home=configured_user_home,
            unit_of_work=uow,
            trust_verifier=skill_trust_verifier,
            clock=clock,
            ids=ids,
        )
        managed_hook_layer = self._overrides.managed_hook_layer or HookLayer(
            HookScope.MANAGED,
            _LOCAL_MANAGED_ID,
            1,
        )
        for definition in managed_hook_layer.hooks:
            command = definition.command
            if command is None:
                continue
            executable = executable_by_id.get(command.executable_id)
            if (
                executable is None
                or command.executable_profile_fingerprint != executable.fingerprint
                or command.cwd_root_id not in executable.allowed_cwd_roots
                or command.environment_profile_id not in executable.environment_profiles
                or command.environment_profile_id not in environment_ids
            ):
                raise ProductionWorkerError(f"Hook {definition.hook_id!r} is outside the shared process catalog")
        hook_capabilities = ProductionHookBundleFactory(
            workspace_id=workspace_id,
            managed_layer=managed_hook_layer,
            builtin_handlers=self._overrides.builtin_hook_handlers or {},
            unit_of_work=uow,
            event_sink=buffered,
            processes=process_supervisor,
            clock=clock,
            ids=ids,
        )
        components = ProductionRunComponentsFactory(
            workspace_id=workspace_id,
            clock=clock,
            ids=ids,
            gateway_factory=gateway_factory,
            default_config=config,
            approvals=approvals,
            policy_audit=EntityPolicyAuditSink(uow),
            journal=uow.invocation_journal,
            artifacts=artifacts,
            local_read=read_executor,
            local_transaction=local_transaction,
            headless_vault_write=headless_vault_write,
            channels=channels,
            parent_authorities=late_parent_authorities,
            optional_definitions=(*powershell_executor.definitions, *subagent_definitions),
            optional_local_executors=((powershell_executor.definitions, powershell_executor),),
            subagent_executor=late_subagent if subagent_definitions else None,
            skills=skill_factory,
            shell=shell_capabilities,
            process_root_ids=tuple(sorted(allowed_cwd_roots)),
            hooks=hook_capabilities,
            hook_unit_of_work=uow,
            lifecycle_budget=runtime_budget,
            tool_observability=tool_observability,
            run_correlations=correlations,
            run_observability=run_observability,
        )
        cancellations = HarnessChildCancellationFactory()
        late_tree = _LateSubagentTree()
        harness = HarnessService(
            unit_of_work=uow,
            event_sink=buffered,
            clock=clock,
            ids=ids,
            components=components,
            async_components=components,
            turn_manager=turn_manager,
            approval_manager=approvals,
            child_components=components,
            root_cancellations=cancellations,
            subagent_tree=late_tree,
            run_context_provider=CompositeRunContextProvider(
                ConversationHistoryRunPreparationAdapter(
                    workspace_id=workspace_id,
                    unit_of_work=uow,
                ),
                WorkspaceInstructionRunPreparationAdapter(
                    workspace_id=workspace_id,
                    vault=vault_fs,
                ),
                VaultMemoryRunPreparationAdapter(
                    workspace_id=workspace_id,
                    vault=vault_fs,
                ),
            ),
            lifecycle_hooks=components,
        )
        base_definitions = (
            *read_executor.definitions,
            *powershell_executor.definitions,
            vault_transaction_definition(),
            *subagent_definitions,
        )
        base_scope = CapabilityScope(
            frozenset(item.name for item in base_definitions),
            frozenset(),
            frozenset(RiskClass),
            frozenset(capability for item in base_definitions for capability in item.required_capabilities),
            False,
            False,
        )
        catalog = AgentDefinitionCatalog(
            workspace_id=workspace_id,
            builtins=builtin_agent_definitions(
                available_tools=base_scope.allowed_tools,
                root_capabilities=base_scope.root_capabilities,
            ),
            roots=(
                AgentDefinitionRoot(
                    "user",
                    AgentDefinitionLayer.USER,
                    configured_user_home / ".claude" / "agents",
                    workspace_trusted=True,
                ),
                AgentDefinitionRoot(
                    "workspace",
                    AgentDefinitionLayer.WORKSPACE,
                    bootstrap.canonical_root / ".claude" / "agents",
                    workspace_trusted=config.policy.workspace_trusted,
                ),
            ),
        )
        catalog.rescan(expected_revision=0)
        root_authorities = _RootAuthorityProvider(
            workspace_id=workspace_id,
            unit_of_work=uow,
            components=components,
            clock=clock,
        )
        authorities = CompositeParentRunAuthorityProvider(uow, catalog, root_authorities)
        late_parent_authorities.bind(authorities)
        scheduler = ChildRunScheduler(cancellations)
        budget_tree = SubagentBudgetTree(
            components.root_ledger,
            retained_final_budget=AgentBudget(1, 1, 1, 0, 1.0, 1_024, 0, 0),
        )
        subagents = SubagentService(
            workspace_id=workspace_id,
            worker_id=f"worker-{os.getpid()}",
            unit_of_work=uow,
            event_sink=buffered,
            clock=clock,
            ids=ids,
            catalog=catalog,
            authorities=authorities,
            context_forker=ContextForker(ids, clock),
            scope_deriver=ScopeDeriver(base_scope),
            budget_tree=budget_tree,
            scheduler=scheduler,
            mailbox=DurableMailbox(uow, clock),
            runner=HarnessSubagentRunExecutor(harness),
            result_artifacts=SubagentResultArtifactManager(artifacts, clock, ids),
            event_factory=ProtocolSubagentEventFactory(),
            lifecycle_bindings=_ProductionSubagentLifecycleBindings(components),
        )
        late_tree.bind(subagents)
        if subagent_definitions:
            late_subagent.bind(SubagentToolExecutor(workspace_id, subagents))

        recovery_registry = ToolRegistry(
            "worker-recovery",
            base_definitions,
            preflight_provider_ids=frozenset({local_transaction.provider_id}),
        )
        startup = RuntimeStartupCoordinator(
            recovery=RecoveryCoordinator(
                unit_of_work=uow,
                registry=recovery_registry,
                definition_resolver=_FingerprintDefinitionResolver(
                    (
                        *base_definitions,
                        client_vault_transaction_definition(),
                        legacy_public_vault_transaction_definition(),
                        legacy_vault_transaction_definition(),
                        legacy_vault_transaction_definition(executor_location=ExecutorLocation.CLIENT),
                    )
                ),
                clock=clock,
            ),
            applier=RecoveryPlanApplier(unit_of_work=uow, clock=clock, ids=ids),
            harness=harness,
            subagent_recovery=RunRecoverySupervisor(subagents, cancellations),
        )
        identity = ApplicationIdentity(
            runtime_version=self._runtime_version,
            core_version=_semantic_version(__version__),
            protocol_version=PROTOCOL_VERSION,
            schema_hash=schema_hash(),
        )
        harness_application = HarnessApplication(identity, harness, buffered, startup)
        projections = UowConversationProjectionService(workspace_id=workspace_id, unit_of_work=uow)
        controls = ConversationControlService(
            workspace_id=workspace_id,
            unit_of_work=uow,
            event_sink=buffered,
            clock=clock,
            ids=ids,
            turn_manager=turn_manager,
            compaction_runner=_LosslessCompactionRunner(
                artifacts,
                clock,
                ids,
                components=components,
                hook_budget=runtime_budget,
            ),
        )
        runtime_diagnostics = _RuntimeDiagnostics()
        diagnostics = DiagnosticsService(
            workspace_id=workspace_id,
            runtime=runtime_diagnostics,
            processes=_ProcessDiagnostics(host_pid, process_supervisor),
            logger=logger,
            metrics=metrics,
            artifacts=artifacts,
            clock=clock,
            ids=ids,
        )
        application_holder: dict[str, ProductionWorkerApplication] = {}
        transport_policy = _ProductionApplicationTransportPolicy(channels, headless_vault_write)

        async def runtime_status(
            raw: Any,
            cancellation: CancellationToken,
            context: ApplicationCommandContext,
        ) -> RuntimeStatusResult:
            del raw, context
            cancellation.checkpoint()
            return await _runtime_status(application_holder["application"], catalog, self._runtime_version)

        domain = dict(
            compose_domain_command_handlers(
                identity=DomainCommandIdentity(workspace_id, _LOCAL_PROFILE_ID, _LOCAL_MANAGED_ID, "actor_local"),
                clock=clock,
                harness=harness,
                config=config_service,
                config_activation=config_activation,
                models=ProductionModelCommandService(
                    config=config_service,
                    managed_owner_id=_LOCAL_MANAGED_ID,
                    profile_id=_LOCAL_PROFILE_ID,
                    workspace_id=workspace_id,
                    secrets=secret_store,
                    gateway_factory=configured_gateway_factory,
                    clock=clock,
                    ids=ids,
                ),
                projections=projections,
                artifacts=artifacts,
                secrets=secret_store,
                controls=controls,
                subagents=subagents,
                subagent_authorities=_SubagentAuthorityResolver(uow),
                subagent_artifacts=_SubagentArtifactResolver(artifacts),
                diagnostics=diagnostics,
                diagnostics_owner_runs=_DiagnosticsOwnerAuthorizer(workspace_id, uow),
                gateway_provider=lambda: application_holder["application"].gateway,
                transport_policy=transport_policy,
                administrative_approvals=headless_vault_write,
                headless_vault_write_handlers=headless_vault_write_handlers(headless_vault_write),
                extension_management_handlers=extension_management_command_handlers(
                    workspace_id=workspace_id,
                    profile_id=_LOCAL_PROFILE_ID,
                    managed_owner_id=_LOCAL_MANAGED_ID,
                    config=config_service,
                    harness=harness,
                    skills=skill_factory,
                    shell=shell_capabilities.profiles,
                    hooks=hook_capabilities.configuration,
                    unit_of_work=uow,
                    executable_profiles=executable_profiles,
                    environment_profiles=environment_profiles,
                    builtin_hook_handler_ids=tuple((self._overrides.builtin_hook_handlers or {}).keys()),
                    process_registrations=self._overrides.process_registration_service,
                ),
            )
        )

        for method in ("turn/start", "turn/retry"):
            original = domain[method]

            async def reject_when_quiescing(
                raw: Any,
                cancellation: CancellationToken,
                context: ApplicationCommandContext,
                *,
                _original: Any = original,
            ) -> Any:
                if application_holder["application"].reject_new_runs:
                    raise ProductionWorkerError("Worker is quiescing and rejects new Runs")
                return await _original(raw, cancellation, context)

            domain[method] = reject_when_quiescing

        async def shutdown_after_receipt(
            raw: Any,
            cancellation: CancellationToken,
            context: ApplicationCommandContext,
        ) -> Any:
            del context
            cancellation.checkpoint()
            # Snapshot the user-visible cancellation result before shutdown
            # begins.  This read is side-effect free, so a disconnect here can
            # safely abandon the request without creating a half-committed
            # Worker.  Once delivery is armed, commit is the very next await.
            active = await projections.active_run_ids()
            application = application_holder["application"]
            application.begin_shutdown_delivery()
            receipt = await application.commit_shutdown(grace_seconds=raw.grace_period_ms / 1000)
            if not receipt.safely_committed:
                raise ProductionWorkerError("Worker could not prove shutdown state was flushed")
            return ShutdownResult(accepted=True, active_runs_cancel_requested=list(active))

        domain["shutdown"] = shutdown_after_receipt
        runtime_identity = ApplicationRuntimeIdentity(
            runtime_version=self._runtime_version,
            core_version=_semantic_version(__version__),
            protocol_version=PROTOCOL_VERSION,
            supported_protocol_range=ProtocolRange(minimum=PROTOCOL_VERSION, maximum=PROTOCOL_VERSION),
            schema_hash=schema_hash(),
            workspace_id=workspace_id,
            workspace_instance_id=bootstrap.workspace_instance_id,
            host_pid=host_pid,
            worker_pid=os.getpid(),
            runtime_arch=_runtime_arch(),
            capabilities=_protocol_capabilities(),
            build_commit=self._build_commit,
            capabilities_provider=lambda: application_holder["application"].protocol_capabilities,
        )
        handlers = compose_application_command_handlers(
            identity=runtime_identity,
            clock=clock,
            runtime_status=runtime_status,
            domain_handlers=domain,
        )
        dispatcher = RuntimeApplicationCommandDispatcher(application=harness_application, handlers=handlers)
        application = ProductionWorkerApplication(
            workspace_id=workspace_id,
            workspace_instance_id=bootstrap.workspace_instance_id,
            canonical_root_identity=self._canonical_root_identity,
            database_identity=self._database_identity,
            vault_root=bootstrap.canonical_root,
            state_directory=state_directory,
            host_pid=host_pid,
            runtime_version=self._runtime_version,
            runtime_config=config,
            config_service=config_service,
            config_activation=config_activation,
            approvals=approvals,
            clock=clock,
            logger=logger,
            harness_application=harness_application,
            dispatcher=dispatcher,
            gateway=None,
            loopback=None,
            channels=channels,
            local_vault_transaction=local_transaction,
            headless_vault_write=headless_vault_write,
            event_hub=event_hub,
            unit_of_work=uow,
            subagents=subagents,
            components=components,
            scheduler=scheduler,
            turn_manager=turn_manager,
            process_supervisor=process_supervisor,
            native_transports=self._overrides.start_native_transports,
        )
        application_holder["application"] = application
        runtime_diagnostics.application = application
        return application


async def _runtime_status(
    application: ProductionWorkerApplication,
    catalog: AgentDefinitionCatalog,
    runtime_version: str,
) -> RuntimeStatusResult:
    from offeragent_harness.protocol.messages import (
        RuntimeState,
        SkillCatalogStatusSnapshot,
    )

    active = await UowConversationProjectionService(
        workspace_id=application.workspace_id,
        unit_of_work=application.unit_of_work,
    ).active_run_ids()
    return RuntimeStatusResult(
        state=RuntimeState.READY if application.ready else RuntimeState.STARTING,
        workspace_id=application.workspace_id,
        workspace_instance_id=application.workspace_instance_id,
        host_pid=application.host_pid,
        worker_pid=os.getpid(),
        runtime_version=runtime_version,
        core_version=_semantic_version(__version__),
        protocol_version=PROTOCOL_VERSION,
        schema_hash=schema_hash(),
        database_identity=application.database_identity,
        active_run_ids=list(active),
        skills=SkillCatalogStatusSnapshot(
            revision=catalog.revision,
            snapshot_hash=catalog.snapshot_hash,
            discovered_count=len(catalog.descriptors),
            enabled_count=len(tuple(item for item in catalog.descriptors if item.trust.enabled)),
            partial=False,
            diagnostics=[],
        ),
        warnings=[],
    )


def _protocol_capabilities() -> CapabilitySet:
    return CapabilitySet(
        client_tools=True,
        event_replay=True,
        multi_session=True,
        approvals=True,
        skills=True,
        shell=True,
        hooks=True,
        headless_vault_write=True,
        subagents=True,
        artifacts=True,
        loopback_web=True,
        reverse_requests=True,
        content_blocks=True,
        cancellation=True,
        diagnostics=True,
    )


def _runtime_arch() -> RuntimeArch:
    from offeragent_harness.runtime.release_manifest import native_windows_architecture

    architecture = native_windows_architecture()
    if architecture == "x64":
        return RuntimeArch.WIN_X64
    if architecture == "arm64":
        return RuntimeArch.WIN_ARM64
    raise ProductionWorkerError("Worker architecture is unsupported")


def _semantic_version(value: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:a(\d+))?", value)
    if match is not None:
        major, minor, patch, alpha = match.groups()
        return f"{major}.{minor}.{patch}" if alpha is None else f"{major}.{minor}.{patch}-alpha.{alpha}"
    if re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?",
        value,
    ):
        return value
    raise ProductionWorkerError("Runtime version is not semantic")


@dataclass(frozen=True, slots=True)
class WorkerCommandLine:
    workspace_instance_id: str
    canonical_root_identity: str
    database_identity: str
    runtime_version: str


def parse_worker_arguments(arguments: Sequence[str]) -> WorkerCommandLine:
    values = list(arguments)
    if len(values) != 12 or values[:5] != list(_WORKER_ARGUMENTS):
        raise ProductionWorkerError("Worker arguments do not match the fixed Host contract")
    expected_flags = (
        "--canonical-root-identity",
        "--database-identity",
        "--runtime-version",
    )
    if (values[6], values[8], values[10]) != expected_flags:
        raise ProductionWorkerError("Worker arguments are out of order")
    instance_id, root_identity, database_identity, runtime_version = (
        values[5],
        values[7],
        values[9],
        values[11],
    )
    if _INSTANCE_ID.fullmatch(instance_id) is None:
        raise ProductionWorkerError("Worker Workspace instance identity is invalid")
    digest = re.compile(r"^sha256:[0-9a-f]{64}$")
    if digest.fullmatch(root_identity) is None or digest.fullmatch(database_identity) is None:
        raise ProductionWorkerError("Worker Host identity digests are invalid")
    _semantic_version(runtime_version)
    return WorkerCommandLine(instance_id, root_identity, database_identity, runtime_version)


def _resolve_worker_bootstrap(command: WorkerCommandLine) -> WorkerBootstrap:
    if os.name != "nt":
        raise ProductionWorkerError("production Worker requires Windows")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ProductionWorkerError("LOCALAPPDATA is unavailable")
    local_root = (Path(local_app_data) / "OfferAgent").resolve(strict=True)
    registry = WorkspaceRegistry(local_root / "workspace-registry.json")
    matches = tuple(item for item in registry.list() if item.workspace_instance_id == command.workspace_instance_id)
    if len(matches) != 1:
        raise ProductionWorkerError("Workspace instance is absent or duplicated in the current-user registry")
    record = matches[0]
    if record.root_identity.identity_hash != command.canonical_root_identity:
        raise ProductionWorkerError("Workspace registry identity differs from signed Host launch")
    canonical_root = Path(record.root_identity.canonical_path).resolve(strict=True)
    observed = identify_workspace_root(canonical_root)
    if observed != record.root_identity:
        raise ProductionWorkerError("Vault identity changed after Host launch")
    if workspace_database_identity(command.workspace_instance_id) != command.database_identity:
        raise ProductionWorkerError("database identity is not derived from this Workspace instance")
    state_parent = local_root / "workspaces"
    state_directory = state_parent / command.workspace_instance_id
    try:
        state_parent.mkdir(parents=False, exist_ok=True)
        parent_info = state_parent.lstat()
        if (
            not state_parent.is_dir()
            or state_parent.is_symlink()
            or getattr(parent_info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ProductionWorkerError("Worker state parent is not a local directory")
        expected_parent = state_parent.resolve(strict=True)
        if expected_parent.parent != local_root:
            raise ProductionWorkerError("Worker state parent escaped the current-user Runtime root")
        state_directory.mkdir(parents=False, exist_ok=True)
        state_info = state_directory.lstat()
        if (
            not state_directory.is_dir()
            or state_directory.is_symlink()
            or getattr(state_info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            or state_directory.resolve(strict=True).parent != expected_parent
        ):
            raise ProductionWorkerError("Worker state directory escaped the current-user Runtime root")
    except OSError as error:
        raise ProductionWorkerError("Worker state directory is unavailable") from error
    return WorkerBootstrap(command.workspace_instance_id, canonical_root, state_directory)


def _verified_packaged_ripgrep(
    release: InstalledReleaseManifestTrust | InstalledDevelopmentRuntimeTrust,
) -> Path:
    """Return the Runtime-pinned ripgrep image used exclusively by ``grep``."""

    executable = release.version_directory / "tools" / "rg.exe"
    if not release.verify_file(executable):
        raise ProductionWorkerError("Runtime ripgrep image is absent or differs from the trusted manifest")
    return executable.resolve(strict=True)


def _require_ripgrep_path(value: Path | None) -> Path:
    if value is None:
        raise ProductionWorkerError("Worker composition has no Runtime-pinned ripgrep image")
    try:
        executable = value.resolve(strict=True)
    except OSError as error:
        raise ProductionWorkerError("Runtime ripgrep image is unavailable") from error
    if not executable.is_file() or executable.name.casefold() != "rg.exe":
        raise ProductionWorkerError("Runtime ripgrep image is invalid")
    return executable


def _trusted_windows_powershell() -> Path:
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise ProductionWorkerError("SystemRoot is unavailable in the trusted Worker environment")
    try:
        root = Path(system_root).resolve(strict=True)
        executable = (root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe").resolve(strict=True)
        executable.relative_to(root)
    except (OSError, ValueError) as error:
        raise ProductionWorkerError("trusted Windows PowerShell is unavailable") from error
    if not executable.is_file() or executable.name.casefold() != "powershell.exe":
        raise ProductionWorkerError("trusted Windows PowerShell is invalid")
    return executable


def _require_powershell_path(value: Path | None) -> Path:
    if value is None:
        raise ProductionWorkerError("Worker composition has no trusted Windows PowerShell image")
    try:
        executable = value.resolve(strict=True)
    except OSError as error:
        raise ProductionWorkerError("trusted Windows PowerShell is unavailable") from error
    if not executable.is_file() or executable.name.casefold() != "powershell.exe":
        raise ProductionWorkerError("trusted Windows PowerShell is invalid")
    return executable


def _installed_release_trust() -> InstalledReleaseManifestTrust:
    from offeragent_harness.runtime.release_manifest import (
        ReleaseKeyring,
        ReleaseVerificationError,
        native_windows_architecture,
    )
    from offeragent_harness.runtime.release_trust import InstalledReleaseManifestTrust, load_embedded_release_keys

    version_directory = Path(sys.executable).resolve(strict=True).parent
    trust = InstalledReleaseManifestTrust(
        version_directory,
        keyring=ReleaseKeyring(load_embedded_release_keys()),
    )
    if trust.manifest.platform.architecture != native_windows_architecture():
        raise ReleaseVerificationError(
            "architecture_mismatch",
            "signed Worker Runtime architecture does not match native Windows",
        )
    return trust


def _prepare_process_scratch_root(state_directory: Path) -> Path:
    root = state_directory / "process-workspace"
    working = root / "working"
    try:
        root.mkdir(exist_ok=True)
        working.mkdir(exist_ok=True)
        for item in (root, working):
            info = item.lstat()
            if item.is_symlink() or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise ProductionWorkerError("process scratch root contains a reparse point")
        canonical_state = state_directory.resolve(strict=True)
        canonical_root = root.resolve(strict=True)
        canonical_working = working.resolve(strict=True)
        canonical_root.relative_to(canonical_state)
        canonical_working.relative_to(canonical_root)
    except (OSError, ValueError) as error:
        raise ProductionWorkerError("process scratch root is unavailable") from error
    return canonical_root


async def _run_worker(
    command: WorkerCommandLine,
    *,
    development_trust: InstalledDevelopmentRuntimeTrust | None = None,
) -> None:
    bootstrap = _resolve_worker_bootstrap(command)
    release: InstalledReleaseManifestTrust | InstalledDevelopmentRuntimeTrust
    if development_trust is None:
        release = _installed_release_trust()
        catalog = load_production_process_catalog(release.version_directory, manifest_trust=release)
        skill_trust_verifier: SkillTrustVerifier | None = None
    else:
        from offeragent_harness.runtime.production_process_catalog import load_development_process_catalog

        release = development_trust
        catalog = load_development_process_catalog(
            release.version_directory,
            manifest_trust=development_trust,
        )
        skill_trust_verifier = ReleaseManifestSkillTrustVerifier(
            release.version_directory,
            manifest_trust=development_trust,
        )
    if release.manifest.runtime_version != command.runtime_version:
        raise ProductionWorkerError("Runtime version differs from Host launch identity")
    ripgrep_path = _verified_packaged_ripgrep(release)
    powershell_path = _trusted_windows_powershell()
    clock = SystemClock()
    ids = SecureIdGenerator()
    registration_uow = SqliteUnitOfWorkFactory(bootstrap.state_directory / "state.sqlite")
    registration_loader = WorkspaceProcessRegistrationService(
        workspace_id=read_portable_workspace_config(bootstrap.canonical_root).portable_workspace_id,
        unit_of_work=registration_uow,
        clock=clock,
        ids=ids,
        authenticode=WindowsAuthenticodeVerifier(),
        builtin_executables=catalog.executable_profiles,
        builtin_environments=catalog.environment_profiles,
        allowed_workspace_root_ids=frozenset({"vault", "process-scratch"}),
    )
    registration_snapshot = await registration_loader.runtime_snapshot()
    executable_profiles, environment_profiles = merge_process_registration_snapshot(
        catalog.executable_profiles,
        catalog.environment_profiles,
        registration_snapshot,
    )
    process_registrations = WorkspaceProcessRegistrationService(
        workspace_id=registration_snapshot.catalog.workspace_id,
        unit_of_work=registration_uow,
        clock=clock,
        ids=ids,
        authenticode=WindowsAuthenticodeVerifier(),
        builtin_executables=catalog.executable_profiles,
        builtin_environments=catalog.environment_profiles,
        allowed_workspace_root_ids=frozenset({"vault", "process-scratch"}),
        active_catalog_revision=registration_snapshot.catalog.revision,
    )
    root = ProductionWorkerCompositionRoot(
        canonical_root_identity=command.canonical_root_identity,
        database_identity=command.database_identity,
        runtime_version=command.runtime_version,
        build_commit=release.manifest.build_commit,
        overrides=ProductionWorkerOverrides(
            clock=clock,
            ids=ids,
            process_executable_profiles=executable_profiles,
            process_environment_profiles=environment_profiles,
            process_release_manifest=catalog.manifest_trust,
            process_registration_service=process_registrations,
            signed_shell_profiles=catalog.signed_shell_profiles,
            skill_runtime_root=release.version_directory,
            skill_trust_verifier=skill_trust_verifier,
            ripgrep_path=ripgrep_path,
            powershell_path=powershell_path,
        ),
    )
    from offeragent_harness.runtime.worker_entrypoint import WorkerEntrypoint

    entrypoint = WorkerEntrypoint(root)
    application = cast(ProductionWorkerApplication, await entrypoint.start(bootstrap))
    try:
        await application.wait_stopped()
    finally:
        await entrypoint.shutdown()


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        command = parse_worker_arguments(sys.argv[1:] if arguments is None else arguments)
        asyncio.run(_run_worker(command))
    except BaseException:
        try:
            os.write(2, b"offeragent-worker: startup failed\n")
        except OSError:
            pass
        return 2
    return 0


__all__ = [
    "PRODUCTION_WORKER_COMPOSITION_COMPLETE",
    "ProductionRunComponentsFactory",
    "ProductionWorkerApplication",
    "ProductionWorkerCompositionRoot",
    "ProductionWorkerError",
    "ProductionWorkerOverrides",
    "SecureIdGenerator",
    "SystemClock",
    "WorkerCommandLine",
    "main",
    "parse_worker_arguments",
]
