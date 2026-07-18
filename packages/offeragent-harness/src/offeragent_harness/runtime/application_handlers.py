"""Mandatory identity handlers and complete production handler composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.models import thaw_json
from offeragent_harness.observability import DiagnosticsService
from offeragent_harness.ports import ApplicationCommandContext, ArtifactMetadata, CancellationToken, Clock
from offeragent_harness.protocol._base import JsonObject, WireModel, validate_wire
from offeragent_harness.protocol.capabilities import CapabilitySet, ProtocolRange, negotiate_protocol
from offeragent_harness.protocol.common import (
    Finding,
    PermissionMode,
    ProposedAction,
    RunConfigSnapshot,
    RunPhase,
    RunSnapshot,
    RunStatus,
    TerminationReason,
    UsageSnapshot,
)
from offeragent_harness.protocol.common import (
    SubagentResult as ProtocolSubagentResult,
)
from offeragent_harness.protocol.content import ArtifactRef, ArtifactSensitivity, ArtifactState
from offeragent_harness.protocol.errors import ErrorEnvelope
from offeragent_harness.protocol.events import stored_event_to_envelope
from offeragent_harness.protocol.messages import (
    COMMAND_REGISTRY,
    AgentCancelParams,
    AgentCancelResult,
    AgentResultParams,
    AgentResultResult,
    AgentStatusParams,
    AgentStatusResult,
    DiagnosticProcessSnapshot,
    DiagnosticsExportParams,
    DiagnosticsExportPreviewParams,
    DiagnosticsExportPreviewResult,
    DiagnosticsExportResult,
    DiagnosticsGetParams,
    DiagnosticsGetResult,
    DiagnosticsSnapshotParams,
    DiagnosticsSnapshotResult,
    EventsReplayParams,
    EventsReplayResult,
    InitializeParams,
    InitializeResult,
    RuntimeArch,
    RuntimePingParams,
    RuntimePingResult,
    RuntimeStatusParams,
    SessionCompactParams,
    TransportKind,
    TurnRetryParams,
    TurnRetryResult,
    TurnSteerParams,
)
from offeragent_harness.subagents.models import AgentCancelCommand, AgentUsage, SubagentRunStatus
from offeragent_harness.subagents.service import SubagentService

from .application_dispatcher import ApplicationCommandHandler, CommandHandlerConfigurationError
from .config_service import ConfigService
from .conversation_controls import ConversationControlService
from .harness_service import HarnessService, RetryTurnCommand


@dataclass(frozen=True, slots=True)
class ApplicationRuntimeIdentity:
    runtime_version: str
    core_version: str
    protocol_version: str
    supported_protocol_range: ProtocolRange
    schema_hash: str
    workspace_id: str
    workspace_instance_id: str
    parent_pid: int
    worker_pid: int
    runtime_arch: RuntimeArch
    capabilities: CapabilitySet
    build_commit: str
    capabilities_provider: Callable[[], CapabilitySet] | None = None

    def __post_init__(self) -> None:
        # Reuse the wire model as the strict identity invariant checker.
        InitializeResult(
            protocol_version=self.protocol_version,
            supported_protocol_range=self.supported_protocol_range,
            runtime_version=self.runtime_version,
            core_version=self.core_version,
            schema_hash=self.schema_hash,
            workspace_id=self.workspace_id,
            workspace_instance_id=self.workspace_instance_id,
            parent_pid=self.parent_pid,
            worker_pid=self.worker_pid,
            transport=TransportKind.STDIO,
            runtime_arch=self.runtime_arch,
            capabilities=self.capabilities,
            build_commit=self.build_commit,
        )


@dataclass(frozen=True, slots=True)
class RunTransportRoute:
    """Permission mode accepted for an authenticated local Runtime client."""

    permission_mode: PermissionMode


class ApplicationTransportPolicy(Protocol):
    """Authorize commands from transport facts that request JSON cannot forge."""

    async def resolve_run_route(
        self,
        context: ApplicationCommandContext,
        requested_permission: PermissionMode,
    ) -> RunTransportRoute: ...


class RuntimeStatusProvider:
    """Small callable wrapper so status is evaluated at command time."""

    def __init__(self, provider: ApplicationCommandHandler) -> None:
        self.handler = provider


@dataclass(frozen=True, slots=True)
class SubagentCommandAuthority:
    """Authenticated caller and target lineage data supplied by application composition."""

    requester_run_id: str
    session_id: str
    turn_id: str
    started_at: datetime | None
    last_sequence: int

    def __post_init__(self) -> None:
        if not self.requester_run_id or not self.session_id or not self.turn_id or self.last_sequence < 0:
            raise ValueError("Subagent command authority is invalid")
        if self.started_at is not None and (self.started_at.tzinfo is None or self.started_at.utcoffset() is None):
            raise ValueError("Subagent started_at must be timezone-aware")


class SubagentCommandAuthorityResolver(Protocol):
    async def resolve(
        self,
        context: ApplicationCommandContext,
        target_run_id: str,
    ) -> SubagentCommandAuthority: ...


class SubagentArtifactReferenceResolver(Protocol):
    async def resolve(
        self,
        *,
        requester_run_id: str,
        owner_run_id: str,
        artifact_ids: Sequence[str],
    ) -> tuple[ArtifactRef, ...]: ...


class DiagnosticsOwnerRunAuthorizer(Protocol):
    async def authorize(self, context: ApplicationCommandContext, owner_run_id: str) -> str: ...


def compose_application_command_handlers(
    *,
    identity: ApplicationRuntimeIdentity,
    clock: Clock,
    runtime_status: ApplicationCommandHandler,
    domain_handlers: Mapping[str, ApplicationCommandHandler],
) -> Mapping[str, ApplicationCommandHandler]:
    """Build the exact COMMAND_REGISTRY table; absent domain services fail startup."""

    reserved = frozenset({"initialize", "runtime/ping", "runtime/status"})
    expected_domain = frozenset(COMMAND_REGISTRY) - reserved
    actual_domain = frozenset(domain_handlers)
    if actual_domain != expected_domain:
        raise CommandHandlerConfigurationError(
            f"incomplete domain command services: missing={sorted(expected_domain - actual_domain)}, "
            f"extra={sorted(actual_domain - expected_domain)}"
        )

    async def initialize(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        cancellation.checkpoint()
        params = cast(InitializeParams, raw)
        if params.workspace_id != identity.workspace_id:
            raise ValueError("initialize Workspace does not match this Worker")
        server_capabilities = (
            identity.capabilities_provider() if identity.capabilities_provider is not None else identity.capabilities
        )
        negotiated = negotiate_protocol(
            client_preferred=params.protocol_version,
            client_range=params.supported_protocol_range,
            client_capabilities=params.capabilities,
            client_required_capabilities=params.required_capabilities,
            client_schema_hash=params.schema_hash,
            server_preferred=identity.protocol_version,
            server_range=identity.supported_protocol_range,
            server_capabilities=server_capabilities,
            server_schema_hash=identity.schema_hash,
        )
        return InitializeResult(
            protocol_version=negotiated.protocol_version,
            supported_protocol_range=identity.supported_protocol_range,
            runtime_version=identity.runtime_version,
            core_version=identity.core_version,
            schema_hash=identity.schema_hash,
            workspace_id=identity.workspace_id,
            workspace_instance_id=identity.workspace_instance_id,
            parent_pid=identity.parent_pid,
            worker_pid=identity.worker_pid,
            transport=TransportKind(context.transport),
            runtime_arch=identity.runtime_arch,
            capabilities=negotiated.capabilities,
            build_commit=identity.build_commit,
        )

    async def ping(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        del context
        cancellation.checkpoint()
        params = cast(RuntimePingParams, raw)
        return RuntimePingResult(
            nonce=params.nonce, timestamp=clock.utcnow().isoformat(), worker_pid=identity.worker_pid
        )

    async def status(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel | Mapping[str, object]:
        if not isinstance(raw, RuntimeStatusParams):
            raise TypeError("runtime/status params were not validated")
        return await runtime_status(raw, cancellation, context)

    return {
        "initialize": initialize,
        "runtime/ping": ping,
        "runtime/status": status,
        **domain_handlers,
    }


def conversation_control_handlers(
    *,
    workspace_id: str,
    profile_id: str,
    managed_owner_id: str,
    harness: HarnessService,
    controls: ConversationControlService,
    config: ConfigService,
    transport_policy: ApplicationTransportPolicy,
) -> Mapping[str, ApplicationCommandHandler]:
    """Bind compact/retry/steer to their real durable services."""

    async def compact(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        del context
        params = cast(SessionCompactParams, raw)
        return await controls.compact(
            session_id=params.session_id,
            through_turn_id=params.through_turn_id,
            force=params.force,
            cancellation=cancellation,
        )

    async def retry(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        cancellation.checkpoint()
        params = cast(TurnRetryParams, raw)
        snapshot = await config.snapshot(
            managed_owner_id=managed_owner_id,
            profile_id=profile_id,
            workspace_id=workspace_id,
            session_id=params.session_id,
        )
        run_config = params.run_config
        if run_config is None:
            source_run = await harness.get_run(params.source_run_id)
            run_config = validate_wire(RunConfigSnapshot, thaw_json(source_run.config_snapshot))
        if run_config.model != snapshot.config.model.model:
            raise ValueError("Retry model differs from the effective persisted configuration")
        if run_config.permission_mode is PermissionMode.BYPASS and not snapshot.config.policy.allow_bypass:
            raise ValueError("bypass permission is disabled by the persisted Workspace policy")
        effective_mode = run_config.permission_mode
        if snapshot.config.policy.read_only or not snapshot.config.policy.workspace_trusted:
            effective_mode = PermissionMode.READ_ONLY
        route = await transport_policy.resolve_run_route(
            context,
            effective_mode,
        )
        run_config = run_config.model_copy(
            update={
                "model": snapshot.config.model.model,
                "permission_mode": route.permission_mode,
            }
        )
        receipt = await harness.retry_turn(
            RetryTurnCommand(
                workspace_id=workspace_id,
                session_id=params.session_id,
                turn_id=params.turn_id,
                source_run_id=params.source_run_id,
                idempotency_key=params.idempotency_key,
                run_config=run_config.to_wire(),
                effective_config=snapshot.config,
                effective_config_fingerprint=snapshot.fingerprint,
            )
        )
        return TurnRetryResult(
            session_id=receipt.session_id,
            turn_id=receipt.turn_id,
            run_id=receipt.run_id,
            accepted=receipt.accepted,
            duplicate=receipt.duplicate,
        )

    async def steer(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        del context
        params = cast(TurnSteerParams, raw)
        return await controls.steer(
            run_id=params.run_id,
            message_id=params.message_id,
            input_blocks=tuple(item.to_wire() for item in params.input),
            mode=params.mode,
            cancellation=cancellation,
        )

    return {"session/compact": compact, "turn/retry": retry, "turn/steer": steer}


def subagent_command_handlers(
    *,
    service: SubagentService,
    authorities: SubagentCommandAuthorityResolver,
    artifacts: SubagentArtifactReferenceResolver,
) -> Mapping[str, ApplicationCommandHandler]:
    """Bind public commands only through SubagentService's ownership boundary."""

    async def status(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        cancellation.checkpoint()
        params = cast(AgentStatusParams, raw)
        authority = await authorities.resolve(context, params.run_id)
        snapshot = await service.status(authority.requester_run_id, params.run_id)
        cancellation.checkpoint()
        terminal = snapshot.status.terminal
        return AgentStatusResult(
            run=RunSnapshot(
                run_id=snapshot.run_id,
                root_run_id=snapshot.root_run_id,
                parent_run_id=snapshot.parent_run_id,
                session_id=authority.session_id,
                turn_id=authority.turn_id,
                status=_subagent_status(snapshot.status),
                phase=_subagent_phase(snapshot.phase, snapshot.status),
                agent_name=snapshot.agent_name,
                depth=snapshot.depth,
                started_at=None if authority.started_at is None else authority.started_at.isoformat(),
                completed_at=snapshot.updated_at.isoformat() if terminal else None,
                last_sequence=authority.last_sequence,
                usage=_usage(snapshot.budget_used),
                termination_reason=_subagent_termination(snapshot.status),
            ),
            child_run_ids=list(snapshot.child_run_ids),
        )

    async def result(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        cancellation.checkpoint()
        params = cast(AgentResultParams, raw)
        authority = await authorities.resolve(context, params.run_id)
        include = frozenset({*params.include, "summary", "usage"})
        value = await service.result(authority.requester_run_id, params.run_id, include=include)
        artifact_refs: tuple[ArtifactRef, ...] = ()
        if value.artifact_ids:
            artifact_refs = await artifacts.resolve(
                requester_run_id=authority.requester_run_id,
                owner_run_id=params.run_id,
                artifact_ids=value.artifact_ids,
            )
            if tuple(item.artifact_id for item in artifact_refs) != value.artifact_ids:
                raise ValueError("Subagent Artifact resolver returned mismatched identities or order")
        findings = [validate_wire(Finding, thaw_json(item)) for item in value.findings]
        proposed = [validate_wire(ProposedAction, thaw_json(item)) for item in value.proposed_actions]
        evidence = [cast(JsonObject, thaw_json(item)) for item in value.evidence]
        cancellation.checkpoint()
        return AgentResultResult(
            result=ProtocolSubagentResult(
                run_id=value.run_id,
                status=_result_status(value.status),
                summary=value.summary,
                findings=findings,
                evidence=evidence,
                artifacts=list(artifact_refs),
                proposed_actions=proposed,
                unresolved_questions=list(value.unresolved_questions),
                usage=validate_wire(UsageSnapshot, thaw_json(value.usage)),
                error=_subagent_error(value.error),
            )
        )

    async def cancel(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        params = cast(AgentCancelParams, raw)
        authority = await authorities.resolve(context, params.run_id)
        receipt = await service.cancel(
            AgentCancelCommand(
                requester_run_id=authority.requester_run_id,
                run_id=params.run_id,
                reason=params.reason,
                cascade=params.cascade,
            ),
            cancellation,
        )
        return AgentCancelResult(
            run_id=receipt.run_id,
            accepted=receipt.accepted,
            descendant_run_ids=list(receipt.descendant_run_ids),
        )

    return {"agent/status": status, "agent/result": result, "agent/cancel": cancel}


def _usage(value: AgentUsage) -> UsageSnapshot:
    return UsageSnapshot(
        input_tokens=value.input_tokens,
        output_tokens=value.output_tokens,
        model_calls=value.model_calls,
        tool_calls=value.tool_calls,
        cost_micros=value.cost_micros,
        wall_time_ms=int(value.wall_time_seconds * 1000),
    )


def _subagent_status(value: SubagentRunStatus) -> RunStatus:
    direct = {
        SubagentRunStatus.CREATED: RunStatus.CREATED,
        SubagentRunStatus.QUEUED: RunStatus.QUEUED,
        SubagentRunStatus.WAITING_APPROVAL: RunStatus.AWAITING_APPROVAL,
        SubagentRunStatus.WAITING_CHILDREN: RunStatus.WAITING_CHILDREN,
        SubagentRunStatus.CANCEL_REQUESTED: RunStatus.CANCEL_REQUESTED,
        SubagentRunStatus.COMPLETED: RunStatus.COMPLETED,
        SubagentRunStatus.CANCELLED: RunStatus.CANCELLED,
        SubagentRunStatus.FAILED: RunStatus.FAILED,
        SubagentRunStatus.INTERRUPTED: RunStatus.INTERRUPTED,
        SubagentRunStatus.ORPHANED: RunStatus.ORPHANED,
    }
    return direct.get(value, RunStatus.RUNNING)


def _subagent_phase(value: str, status: SubagentRunStatus) -> RunPhase:
    if status.terminal:
        return RunPhase.TERMINAL
    try:
        return RunPhase(value)
    except ValueError:
        return {
            SubagentRunStatus.WAITING_APPROVAL: RunPhase.AWAITING_APPROVAL,
            SubagentRunStatus.WAITING_CHILDREN: RunPhase.WAITING_CHILDREN,
            SubagentRunStatus.WAITING_TOOL: RunPhase.EXECUTING_TOOLS,
            SubagentRunStatus.CANCEL_REQUESTED: RunPhase.CANCELLING,
        }.get(status, RunPhase.PLANNING)


def _subagent_termination(value: SubagentRunStatus) -> TerminationReason | None:
    return {
        SubagentRunStatus.COMPLETED: TerminationReason.COMPLETED,
        SubagentRunStatus.CANCELLED: TerminationReason.CANCELLED_BY_USER,
        SubagentRunStatus.FAILED: TerminationReason.MODEL_ERROR,
        SubagentRunStatus.INTERRUPTED: TerminationReason.RUNTIME_INTERRUPTED,
        SubagentRunStatus.ORPHANED: TerminationReason.RUNTIME_INTERRUPTED,
    }.get(value)


def _result_status(value: str) -> RunStatus:
    try:
        status = RunStatus(value)
    except ValueError as error:
        raise ValueError(f"Subagent result has unsupported terminal status {value!r}") from error
    if status not in {
        RunStatus.COMPLETED,
        RunStatus.CANCELLED,
        RunStatus.FAILED,
        RunStatus.INTERRUPTED,
        RunStatus.ORPHANED,
    }:
        raise ValueError("Subagent result status is not terminal")
    return status


def _subagent_error(value: Mapping[str, object] | None) -> ErrorEnvelope | None:
    if value is None:
        return None
    details = cast(JsonObject, {"subagent": thaw_json(value)})
    return ErrorEnvelope(
        code=ErrorCode.INTERNAL_ERROR,
        retryable=False,
        cancelled=False,
        user_visible_message=str(value.get("message") or "Subagent execution failed"),
        details=details,
    )


def diagnostics_command_handlers(
    *,
    service: DiagnosticsService,
    owner_runs: DiagnosticsOwnerRunAuthorizer,
) -> Mapping[str, ApplicationCommandHandler]:
    """Bind diagnostics commands to the one local-only observability service."""

    async def snapshot(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        del context
        cancellation.checkpoint()
        if isinstance(raw, DiagnosticsGetParams):
            include_recent_errors = raw.include_recent_errors
            result_type = DiagnosticsGetResult
        else:
            params = cast(DiagnosticsSnapshotParams, raw)
            include_recent_errors = params.include_recent_errors
            result_type = DiagnosticsSnapshotResult
        value = await service.snapshot(include_recent_errors=include_recent_errors)
        cancellation.checkpoint()
        return result_type(
            generated_at=value.generated_at.isoformat(),
            runtime=cast(JsonObject, thaw_json(value.runtime)),
            processes=[
                validate_wire(
                    DiagnosticProcessSnapshot,
                    {"role": item.role, "pid": item.pid, "state": item.state, "owned": item.owned},
                )
                for item in value.processes
            ],
            recent_errors=[cast(JsonObject, thaw_json(item)) for item in value.recent_errors],
            metrics=[cast(JsonObject, thaw_json(item)) for item in value.metrics],
        )

    async def preview(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        del context
        cancellation.checkpoint()
        params = cast(DiagnosticsExportPreviewParams, raw)
        value = await service.preview_export(include_recent_errors=params.include_recent_errors)
        cancellation.checkpoint()
        return DiagnosticsExportPreviewResult(
            files=list(value.files),
            estimated_bytes=value.estimated_bytes,
            contains_paths=False,
            contains_content=False,
            upload_destination=None,
        )

    async def export(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        params = cast(DiagnosticsExportParams, raw)
        owner_run_id = await owner_runs.authorize(context, params.owner_run_id)
        if owner_run_id != params.owner_run_id:
            raise ValueError("diagnostics owner Run authorizer changed the requested identity")
        metadata = await service.export(
            owner_run_id=owner_run_id,
            include_recent_errors=params.include_recent_errors,
            cancellation=cancellation,
        )
        if metadata.owner_run_id != owner_run_id or metadata.sensitivity.value != "private":
            raise ValueError("DiagnosticsService returned an Artifact with unsafe ownership or sensitivity")
        return DiagnosticsExportResult(artifact=_artifact_metadata_ref(metadata), uploaded=False)

    return {
        "diagnostics/get": snapshot,
        "diagnostics/snapshot": snapshot,
        "diagnostics/export-preview": preview,
        "diagnostics/export": export,
    }


def _artifact_metadata_ref(value: ArtifactMetadata) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=value.artifact_id,
        content_hash=value.sha256,
        media_type=value.mime_type,
        size_bytes=value.byte_length,
        sensitivity=ArtifactSensitivity(value.sensitivity.value),
        state=ArtifactState(value.state.value),
        title=None,
    )


def event_replay_handlers(*, harness: HarnessService) -> Mapping[str, ApplicationCommandHandler]:
    """Expose either one Run cursor or an explicit per-Run Session cursor map."""

    async def replay(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        del context
        cancellation.checkpoint()
        params = cast(EventsReplayParams, raw)
        event_types = frozenset(item.value for item in params.types)
        if params.run_id is not None:
            run_page = await harness.replay_run_event_page(
                params.run_id,
                after_sequence=params.after_sequence,
                limit=params.limit,
                event_types=event_types,
            )
            cancellation.checkpoint()
            return EventsReplayResult(
                events=[stored_event_to_envelope(event) for event in run_page.events],
                last_sequence=run_page.last_sequence,
                run_cursors={},
                has_more=run_page.has_more,
            )
        assert params.session_id is not None
        session_page = await harness.replay_session_events(
            params.session_id,
            run_cursors=params.run_cursors,
            limit=params.limit,
            event_types=event_types,
        )
        cancellation.checkpoint()
        return EventsReplayResult(
            events=[stored_event_to_envelope(event) for event in session_page.events],
            last_sequence=None,
            run_cursors=dict(session_page.run_cursors),
            has_more=session_page.has_more,
        )

    return {"events/replay": replay}


__all__ = [
    "ApplicationRuntimeIdentity",
    "DiagnosticsOwnerRunAuthorizer",
    "RuntimeStatusProvider",
    "SubagentArtifactReferenceResolver",
    "SubagentCommandAuthority",
    "SubagentCommandAuthorityResolver",
    "compose_application_command_handlers",
    "conversation_control_handlers",
    "diagnostics_command_handlers",
    "event_replay_handlers",
    "subagent_command_handlers",
]
