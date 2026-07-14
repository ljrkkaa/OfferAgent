"""Production Hook authority frozen into one prepared Agent Run.

The factory owns durable configuration and trust decisions.  The resulting
port is only a lifecycle adapter around the existing Agent/Kernel/Compaction
integration points; it never creates another Agent loop or tool-policy path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from offeragent_harness.config import HarnessConfig
from offeragent_harness.hooks import (
    HookExecutionContext,
    HookInvocation,
    HookLayer,
    HookOutcome,
    HookScope,
)
from offeragent_harness.hooks.state import (
    EntityHookConfigurationStore,
    HookCatalogSnapshot,
    HookConfigurationService,
    HookLayerTrust,
    hook_definition_hash,
    hook_layer_hash,
)
from offeragent_harness.ports import (
    CancellationToken,
    Clock,
    EventSink,
    HookHandler,
    HookLifecyclePort,
    IdGenerator,
    ProcessArtifactBudget,
    ProcessSupervisor,
    UnitOfWorkFactory,
)
from offeragent_harness.subagents.lifecycle import SubagentLifecycleHooks
from offeragent_harness.tools import canonical_json_sha256

from .hook_lifecycle import WorkerLifecycleHooks
from .hook_service import HookHandlerRegistry, HookService, StaticHookLayerSource

_EMPTY_HASH = "sha256:" + "0" * 64
_RECOVERY_SCHEMA_VERSION = 1


class ProductionHookBundleError(RuntimeError):
    """A prepared Hook authority snapshot cannot be safely used."""


@dataclass(frozen=True, slots=True)
class PreparedHookDefinitionEvidence:
    hook_id: str
    definition_hash: str


@dataclass(frozen=True, slots=True)
class PreparedHookLayerEvidence:
    scope: HookScope
    owner_id: str
    content_hash: str
    effective_layer_hash: str
    record_revision: int
    trust: HookLayerTrust
    definitions: tuple[PreparedHookDefinitionEvidence, ...]


@dataclass(frozen=True, slots=True)
class PreparedHookBundle:
    workspace_id: str
    run_id: str
    principal_id: str
    session_id: str
    managed_owner_id: str
    hooks_enabled: bool
    workspace_trusted: bool
    catalog_revision: int
    catalog_snapshot_hash: str
    layers: tuple[HookLayer, ...]
    evidence: tuple[PreparedHookLayerEvidence, ...]
    _factory_nonce: object = field(repr=False, compare=False)

    def recovery_snapshot(self) -> dict[str, Any]:
        """Return the strict non-secret authority proof persisted with a Run."""

        return _recovery_value(self)


@dataclass(frozen=True, slots=True)
class RunBoundHookContextFactory:
    """Create only the exact context frozen by the prepared Run."""

    managed_owner_id: str
    principal_id: str
    workspace_id: str
    session_id: str
    workspace_trusted: bool

    def __call__(self, workspace_id: str, session_id: str, principal_id: str) -> HookExecutionContext:
        if workspace_id != self.workspace_id or session_id != self.session_id or principal_id != self.principal_id:
            raise ProductionHookBundleError("Hook context requested for another Workspace/Session/principal")
        return HookExecutionContext(
            self.managed_owner_id,
            self.principal_id,
            self.workspace_id,
            self.session_id,
            self.workspace_trusted,
        )


class RunBoundHookLifecyclePort(HookLifecyclePort):
    """Reject cross-Run/context use before delegating to the unique HookService."""

    def __init__(
        self,
        *,
        run_id: str,
        context: HookExecutionContext,
        delegate: HookLifecyclePort,
    ) -> None:
        self._run_id = run_id
        self._context = context
        self._delegate = delegate

    async def invoke(self, invocation: HookInvocation, cancellation: CancellationToken) -> HookOutcome:
        if invocation.context != self._context:
            raise ProductionHookBundleError("Hook invocation context differs from its prepared Run")
        if invocation.run_id is not None and invocation.run_id != self._run_id:
            raise ProductionHookBundleError("Hook invocation belongs to another Run")
        return await self._delegate.invoke(invocation, cancellation)


@dataclass(frozen=True, slots=True)
class CompactionHookBinding:
    """Arguments consumed by the existing CompactionService Hook boundary."""

    hooks: HookLifecyclePort
    context: HookExecutionContext


@dataclass(frozen=True, slots=True)
class ProductionHookBundle:
    hooks: HookLifecyclePort | None
    context_factory: RunBoundHookContextFactory | None
    context: HookExecutionContext | None
    worker_lifecycle: WorkerLifecycleHooks | None
    compaction: CompactionHookBinding | None
    subagent_lifecycle: SubagentLifecycleHooks | None
    catalog_revision: int
    catalog_snapshot_hash: str


class ProductionHookBundleFactory:
    """Persist Hook trust and freeze the exact executable projection per Run."""

    def __init__(
        self,
        *,
        workspace_id: str,
        managed_layer: HookLayer,
        builtin_handlers: Mapping[str, HookHandler],
        unit_of_work: UnitOfWorkFactory,
        event_sink: EventSink,
        processes: ProcessSupervisor,
        clock: Clock,
        ids: IdGenerator,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not workspace_id or workspace_id.strip() != workspace_id or "\x00" in workspace_id:
            raise ValueError("Production Hook factory requires a canonical Workspace ID")
        handlers = dict(builtin_handlers)
        if any(not handler_id for handler_id in handlers):
            raise ValueError("production Hook builtin handler IDs must not be empty")
        self.workspace_id = workspace_id
        self._managed_owner_id = managed_layer.owner_id
        self._handlers = handlers
        self._unit_of_work = unit_of_work
        self._event_sink = event_sink
        self._processes = processes
        self._clock = clock
        self._ids = ids
        self._environment = dict(environment or {})
        self._configuration = HookConfigurationService(
            workspace_id=workspace_id,
            managed_layer=managed_layer,
            signed_builtin_handler_ids=frozenset(handlers),
            store=EntityHookConfigurationStore(unit_of_work),
        )
        self._factory_nonce = object()

    @property
    def configuration(self) -> HookConfigurationService:
        return self._configuration

    async def prepare(
        self,
        *,
        run_id: str,
        principal_id: str,
        session_id: str,
        effective_config: HarnessConfig,
        cancellation: CancellationToken,
        durable_snapshot: Mapping[str, Any] | None = None,
    ) -> PreparedHookBundle:
        """Load durable trust and capture exact layers before Run construction."""

        context = HookExecutionContext(
            self._managed_owner_id,
            principal_id,
            self.workspace_id,
            session_id,
            effective_config.policy.workspace_trusted,
        )
        if not run_id or run_id.strip() != run_id or "\x00" in run_id:
            raise ValueError("Production Hook preparation requires a canonical Run ID")
        cancellation.checkpoint()
        if not effective_config.extensibility.hooks_enabled:
            prepared = PreparedHookBundle(
                self.workspace_id,
                run_id,
                principal_id,
                session_id,
                self._managed_owner_id,
                False,
                context.workspace_trusted,
                0,
                _EMPTY_HASH,
                (),
                (),
                self._factory_nonce,
            )
        else:
            snapshot = await self._configuration.initialize(cancellation)
            cancellation.checkpoint()
            layers = self._configuration.effective_layers(
                principal_id=principal_id,
                session_id=session_id,
                workspace_trusted=context.workspace_trusted,
            )
            prepared = PreparedHookBundle(
                self.workspace_id,
                run_id,
                principal_id,
                session_id,
                self._managed_owner_id,
                True,
                context.workspace_trusted,
                snapshot.revision,
                snapshot.snapshot_hash,
                layers,
                _evidence(snapshot, layers),
                self._factory_nonce,
            )
        if durable_snapshot is not None:
            _require_recovery_match(durable_snapshot, prepared)
        return prepared

    def build_prepared(
        self,
        prepared: PreparedHookBundle,
        *,
        artifact_budget: ProcessArtifactBudget | None = None,
    ) -> ProductionHookBundle:
        """Bind the frozen authority to Hook/Agent/Kernel lifecycle adapters."""

        if prepared._factory_nonce is not self._factory_nonce or prepared.workspace_id != self.workspace_id:
            raise ProductionHookBundleError("prepared Hook bundle belongs to another factory/Workspace")
        if not prepared.hooks_enabled:
            return ProductionHookBundle(None, None, None, None, None, None, 0, _EMPTY_HASH)
        if not self._configuration.initialized:
            raise ProductionHookBundleError("prepared Hook configuration catalog is unavailable")
        snapshot = self._configuration.snapshot
        layers = self._configuration.effective_layers(
            principal_id=prepared.principal_id,
            session_id=prepared.session_id,
            workspace_trusted=prepared.workspace_trusted,
        )
        evidence = _evidence(snapshot, layers)
        if (
            snapshot.revision != prepared.catalog_revision
            or snapshot.snapshot_hash != prepared.catalog_snapshot_hash
            or layers != prepared.layers
            or evidence != prepared.evidence
        ):
            raise ProductionHookBundleError("prepared Hook configuration/trust snapshot drifted before binding")
        factory = RunBoundHookContextFactory(
            prepared.managed_owner_id,
            prepared.principal_id,
            prepared.workspace_id,
            prepared.session_id,
            prepared.workspace_trusted,
        )
        context = factory(prepared.workspace_id, prepared.session_id, prepared.principal_id)
        service = HookService(
            layers=StaticHookLayerSource(prepared.layers),
            handlers=HookHandlerRegistry(self._handlers),
            process_supervisor=self._processes,
            unit_of_work=self._unit_of_work,
            event_sink=self._event_sink,
            clock=self._clock,
            ids=self._ids,
            environment=self._environment,
            artifact_budget=artifact_budget,
        )
        hooks = RunBoundHookLifecyclePort(run_id=prepared.run_id, context=context, delegate=service)
        return ProductionHookBundle(
            hooks,
            factory,
            context,
            WorkerLifecycleHooks(hooks, context),
            CompactionHookBinding(hooks, context),
            SubagentLifecycleHooks(hooks),
            prepared.catalog_revision,
            prepared.catalog_snapshot_hash,
        )


def _evidence(
    snapshot: HookCatalogSnapshot,
    layers: Sequence[HookLayer],
) -> tuple[PreparedHookLayerEvidence, ...]:
    records = {(record.layer.scope, record.layer.owner_id): record for record in snapshot.records}
    evidence = []
    for layer in layers:
        try:
            record = records[(layer.scope, layer.owner_id)]
        except KeyError as error:
            raise ProductionHookBundleError("effective Hook layer has no durable trust record") from error
        evidence.append(
            PreparedHookLayerEvidence(
                layer.scope,
                layer.owner_id,
                record.content_hash,
                hook_layer_hash(layer),
                record.revision,
                record.trust,
                tuple(
                    PreparedHookDefinitionEvidence(definition.hook_id, hook_definition_hash(definition))
                    for definition in layer.hooks
                ),
            )
        )
    return tuple(evidence)


def _recovery_value(prepared: PreparedHookBundle) -> dict[str, Any]:
    return {
        "schemaVersion": _RECOVERY_SCHEMA_VERSION,
        "workspaceId": prepared.workspace_id,
        "runId": prepared.run_id,
        "principalId": prepared.principal_id,
        "sessionId": prepared.session_id,
        "managedOwnerId": prepared.managed_owner_id,
        "hooksEnabled": prepared.hooks_enabled,
        "workspaceTrusted": prepared.workspace_trusted,
        "catalogRevision": prepared.catalog_revision,
        "catalogSnapshotHash": prepared.catalog_snapshot_hash,
        "layers": [
            {
                "scope": item.scope.value,
                "ownerId": item.owner_id,
                "contentHash": item.content_hash,
                "effectiveLayerHash": item.effective_layer_hash,
                "recordRevision": item.record_revision,
                "trust": item.trust.value,
                "definitions": [
                    {"hookId": definition.hook_id, "definitionHash": definition.definition_hash}
                    for definition in item.definitions
                ],
            }
            for item in prepared.evidence
        ],
    }


def _require_recovery_match(raw: Mapping[str, Any], prepared: PreparedHookBundle) -> None:
    expected = _recovery_value(prepared)
    try:
        observed_hash = canonical_json_sha256(raw)
        expected_hash = canonical_json_sha256(expected)
    except (TypeError, ValueError) as error:
        raise ProductionHookBundleError("persisted Hook recovery snapshot is not strict JSON") from error
    if dict(raw) != expected or observed_hash != expected_hash:
        raise ProductionHookBundleError("persisted Hook recovery snapshot/configuration drifted")


__all__ = [
    "CompactionHookBinding",
    "PreparedHookBundle",
    "PreparedHookDefinitionEvidence",
    "PreparedHookLayerEvidence",
    "ProductionHookBundle",
    "ProductionHookBundleError",
    "ProductionHookBundleFactory",
    "RunBoundHookContextFactory",
    "RunBoundHookLifecyclePort",
]
