"""Complete application command composition over existing Harness services and ports."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from offeragent_harness.config import ConfigPatch
from offeragent_harness.config import ConfigScope as DomainConfigScope
from offeragent_harness.hooks import HookDecision, HookEvent
from offeragent_harness.models import thaw_json
from offeragent_harness.observability import DiagnosticsService
from offeragent_harness.permissions import ApprovalResolution, ApprovalScope, ApprovalState
from offeragent_harness.ports import (
    ApplicationCommandContext,
    ArtifactStore,
    CancellationToken,
    Clock,
    SecretHandle,
    SecretInput,
    SecretKind,
    SecretMetadata,
    SecretStore,
)
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.common import PermissionMode, TurnSnapshot
from offeragent_harness.protocol.content import ImageContentBlock
from offeragent_harness.protocol.messages import (
    COMMAND_REGISTRY,
    ApprovalResolveParams,
    ApprovalResolveResult,
    ArtifactEncoding,
    ArtifactReadParams,
    ArtifactReadResult,
    AttachmentAbortParams,
    AttachmentAbortResult,
    AttachmentBeginParams,
    AttachmentBeginResult,
    AttachmentChunkParams,
    AttachmentChunkResult,
    AttachmentCommitParams,
    AttachmentCommitResult,
    AttachmentReadParams,
    AttachmentReadResult,
    ConfigGetParams,
    ConfigScope,
    ConfigSnapshot,
    ConfigUpdateParams,
    ConfigUpdateResult,
    ModelsHealthParams,
    ModelsHealthResult,
    ModelsListParams,
    ModelsListResult,
    SecretMetadataSnapshot,
    SecretsDeleteParams,
    SecretsDeleteResult,
    SecretsListParams,
    SecretsListResult,
    SecretsPutParams,
    SecretsPutResult,
    SessionCreateParams,
    SessionCreateResult,
    SessionDeleteParams,
    SessionDeleteResult,
    SessionDetail,
    SessionForkParams,
    SessionForkResult,
    SessionGetParams,
    SessionGetResult,
    SessionListParams,
    SessionListResult,
    SessionRenameParams,
    SessionRenameResult,
    ShutdownParams,
    ShutdownResult,
    TurnCancelParams,
    TurnCancelResult,
    TurnGetParams,
    TurnGetResult,
    TurnStartParams,
    TurnStartResult,
)
from offeragent_harness.subagents.service import SubagentService
from offeragent_harness.tools import canonical_json_sha256

from .application_dispatcher import ApplicationCommandHandler, CommandHandlerConfigurationError
from .application_handlers import (
    ApplicationTransportPolicy,
    DiagnosticsOwnerRunAuthorizer,
    SubagentArtifactReferenceResolver,
    SubagentCommandAuthorityResolver,
    conversation_control_handlers,
    diagnostics_command_handlers,
    event_replay_handlers,
    subagent_command_handlers,
    web_launch_handlers,
)
from .approval_manager import ApprovalManager, ApprovalNotFound
from .config_service import ConfigService, ConfigUpdateCommand, WorkerConfigActivation
from .conversation_attachments import (
    AttachmentClaim,
    AttachmentUploadRequest,
    ConversationAttachmentStore,
)
from .conversation_controls import ConversationControlService
from .harness_service import HarnessService, StartTurnCommand
from .hook_lifecycle import LifecycleHookDenied
from .loopback_gateway import LoopbackWebGateway
from .pinned_context import pinned_context_block
from .session_service import (
    SessionCreateCommand as LifecycleSessionCreateCommand,
)
from .session_service import SessionCreateState
from .session_service import (
    SessionDeleteCommand as LifecycleSessionDeleteCommand,
)
from .session_service import (
    SessionForkCommand as LifecycleSessionForkCommand,
)
from .session_service import (
    SessionGetCommand as LifecycleSessionGetCommand,
)
from .session_service import (
    SessionListCommand as LifecycleSessionListCommand,
)
from .session_service import (
    SessionRenameCommand as LifecycleSessionRenameCommand,
)


@dataclass(frozen=True, slots=True)
class DomainCommandIdentity:
    workspace_id: str
    profile_id: str
    managed_owner_id: str
    actor_id: str

    def __post_init__(self) -> None:
        if not all((self.workspace_id, self.profile_id, self.managed_owner_id, self.actor_id)):
            raise ValueError("domain command identity is incomplete")


class ModelCommandService(Protocol):
    async def list_models(
        self,
        params: ModelsListParams,
        cancellation: CancellationToken,
    ) -> ModelsListResult: ...

    async def health(
        self,
        params: ModelsHealthParams,
        cancellation: CancellationToken,
    ) -> ModelsHealthResult: ...


class ConversationProjectionService(Protocol):
    async def turns(self, session_id: str, cancellation: CancellationToken) -> tuple[TurnSnapshot, ...]: ...

    async def turn(
        self,
        session_id: str,
        turn_id: str,
        cancellation: CancellationToken,
    ) -> TurnSnapshot: ...

    async def resolve_run_id(
        self,
        session_id: str,
        turn_id: str,
        requested_run_id: str | None,
        cancellation: CancellationToken,
    ) -> str: ...

    async def active_run_ids(self) -> tuple[str, ...]: ...


def compose_domain_command_handlers(
    *,
    identity: DomainCommandIdentity,
    clock: Clock,
    harness: HarnessService,
    config: ConfigService,
    config_activation: WorkerConfigActivation,
    models: ModelCommandService,
    projections: ConversationProjectionService,
    artifacts: ArtifactStore,
    attachments: ConversationAttachmentStore,
    secrets: SecretStore,
    controls: ConversationControlService,
    subagents: SubagentService,
    subagent_authorities: SubagentCommandAuthorityResolver,
    subagent_artifacts: SubagentArtifactReferenceResolver,
    diagnostics: DiagnosticsService,
    diagnostics_owner_runs: DiagnosticsOwnerRunAuthorizer,
    gateway_provider: Callable[[], LoopbackWebGateway | None],
    transport_policy: ApplicationTransportPolicy,
    extension_management_handlers: Mapping[str, ApplicationCommandHandler],
    plugin_tool_handlers: Mapping[str, ApplicationCommandHandler],
) -> Mapping[str, ApplicationCommandHandler]:
    """Return every non-identity command exactly once or fail composition."""

    handlers: dict[str, ApplicationCommandHandler] = {}

    def add(values: Mapping[str, ApplicationCommandHandler]) -> None:
        overlap = handlers.keys() & values.keys()
        if overlap:
            raise CommandHandlerConfigurationError(f"duplicate domain command handlers: {sorted(overlap)!r}")
        handlers.update(values)

    add(_config_handlers(identity=identity, config=config, activation=config_activation))
    add(extension_management_handlers)
    add(plugin_tool_handlers)
    add(_model_handlers(models=models))
    add(
        _session_handlers(
            identity=identity,
            harness=harness,
            projections=projections,
            config=config,
            attachments=attachments,
        )
    )
    add(
        _turn_handlers(
            identity=identity,
            harness=harness,
            projections=projections,
            config=config,
            transport_policy=transport_policy,
            attachments=attachments,
        )
    )
    add(
        _approval_handlers(
            identity=identity,
            approvals=harness.approvals,
            clock=clock,
        )
    )
    add(_artifact_handlers(identity=identity, artifacts=artifacts))
    add(_attachment_handlers(workspace_id=identity.workspace_id, harness=harness, attachments=attachments))
    add(secret_command_handlers(identity=identity, secrets=secrets))
    add(_shutdown_handlers(harness=harness, projections=projections))
    add(
        conversation_control_handlers(
            workspace_id=identity.workspace_id,
            profile_id=identity.profile_id,
            managed_owner_id=identity.managed_owner_id,
            harness=harness,
            controls=controls,
            config=config,
            transport_policy=transport_policy,
        )
    )
    add(event_replay_handlers(harness=harness))
    add(
        subagent_command_handlers(
            service=subagents,
            authorities=subagent_authorities,
            artifacts=subagent_artifacts,
        )
    )
    add(
        diagnostics_command_handlers(
            service=diagnostics,
            owner_runs=diagnostics_owner_runs,
        )
    )
    add(web_launch_handlers(gateway_provider=gateway_provider))
    expected = frozenset(COMMAND_REGISTRY) - {"initialize", "runtime/ping", "runtime/status"}
    actual = frozenset(handlers)
    if actual != expected:
        raise CommandHandlerConfigurationError(
            f"domain command factory is incomplete: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return handlers


def _config_handlers(
    *,
    identity: DomainCommandIdentity,
    config: ConfigService,
    activation: WorkerConfigActivation,
) -> Mapping[str, ApplicationCommandHandler]:
    async def restart_pending(params: ConfigGetParams | ConfigUpdateParams) -> bool:
        desired = await config.snapshot(
            managed_owner_id=identity.managed_owner_id,
            profile_id=identity.profile_id,
            workspace_id=identity.workspace_id,
            session_id=params.session_id if params.scope is ConfigScope.SESSION else None,
        )
        return activation.restart_pending(desired.config)

    async def get(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        cancellation.checkpoint()
        params = cast(ConfigGetParams, raw)
        scope, owner = _config_owner(identity, params.scope, params.session_id)
        layer = await config.layer(scope, owner)
        pending = await restart_pending(params)
        return ConfigSnapshot(
            scope=params.scope,
            revision=layer.revision,
            values=layer.patch.payload(),
            restart_pending=pending,
        )

    async def update(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        cancellation.checkpoint()
        params = cast(ConfigUpdateParams, raw)
        scope, owner = _config_owner(identity, params.scope, params.session_id)
        patch = ConfigPatch.model_validate(thaw_json(params.patch))
        idempotency_key = "config-" + canonical_json_sha256(
            {
                "scope": params.scope.value,
                "owner": owner,
                "revision": params.expected_revision,
                "patch": params.patch,
            }
        ).removeprefix("sha256:")
        await config.update(
            ConfigUpdateCommand(
                scope=scope,
                owner_id=owner,
                expected_revision=params.expected_revision,
                idempotency_key=idempotency_key,
                actor_id=identity.actor_id,
                patch=patch,
            )
        )
        layer = await config.layer(scope, owner)
        pending = await restart_pending(params)
        return ConfigUpdateResult(
            status="restart_required" if pending else "applied",
            snapshot=ConfigSnapshot(
                scope=params.scope,
                revision=layer.revision,
                values=layer.patch.payload(),
                restart_pending=pending,
            ),
        )

    return {"config/get": get, "config/update": update}


def _config_owner(
    identity: DomainCommandIdentity,
    scope: ConfigScope,
    session_id: str | None,
) -> tuple[DomainConfigScope, str]:
    if scope is ConfigScope.USER:
        return DomainConfigScope.USER, identity.profile_id
    if scope is ConfigScope.WORKSPACE:
        return DomainConfigScope.WORKSPACE, identity.workspace_id
    assert session_id is not None
    return DomainConfigScope.SESSION, f"{identity.workspace_id}:{session_id}"


def _model_handlers(*, models: ModelCommandService) -> Mapping[str, ApplicationCommandHandler]:
    async def list_models(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        del context
        return await models.list_models(cast(ModelsListParams, raw), cancellation)

    async def health(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        return await models.health(cast(ModelsHealthParams, raw), cancellation)

    return {"models/list": list_models, "models/health": health}


def _session_handlers(
    *,
    identity: DomainCommandIdentity,
    harness: HarnessService,
    projections: ConversationProjectionService,
    config: ConfigService,
    attachments: ConversationAttachmentStore | None = None,
) -> Mapping[str, ApplicationCommandHandler]:
    async def create(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        cancellation.checkpoint()
        params = cast(SessionCreateParams, raw)
        prepared = await harness.sessions.begin_create(
            LifecycleSessionCreateCommand(
                workspace_id=identity.workspace_id,
                profile_id=identity.profile_id,
                title=params.title or "新会话",
                idempotency_key=params.client_request_id,
            ),
            connection_id=context.client_id or context.transport,
        )
        if prepared.state is SessionCreateState.ACTIVE:
            assert prepared.result is not None
            return SessionCreateResult(session=prepared.result.session, created=False)
        if prepared.state is SessionCreateState.ABORTED:
            assert prepared.hook_decision is not None
            raise LifecycleHookDenied(HookEvent.SESSION_START, HookDecision(prepared.hook_decision))

        snapshot = await config.snapshot(
            managed_owner_id=identity.managed_owner_id,
            profile_id=identity.profile_id,
            workspace_id=identity.workspace_id,
            session_id=prepared.session_id,
        )
        try:
            await harness.session_started(
                workspace_id=identity.workspace_id,
                session_id=prepared.session_id,
                principal_id=identity.profile_id,
                connection_id=prepared.connection_id,
                effective_config=snapshot.config,
                cancellation=cancellation,
            )
        except LifecycleHookDenied as error:
            aborted = await harness.sessions.abort_create(
                prepared,
                hook_decision=error.decision.value,
                hook_reason_code=f"session_start_hook_{error.decision.value}",
            )
            assert aborted.hook_decision is not None
            raise LifecycleHookDenied(HookEvent.SESSION_START, HookDecision(aborted.hook_decision)) from None
        value = await harness.sessions.activate_create(
            prepared,
            hook_reason_code="session_start_hook_continue",
        )
        return SessionCreateResult(session=value.session, created=value.created)

    async def list_sessions(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        del context
        cancellation.checkpoint()
        params = cast(SessionListParams, raw)
        value = await harness.sessions.list(
            LifecycleSessionListCommand(identity.workspace_id, params.cursor, params.limit, params.include_deleted)
        )
        return SessionListResult(sessions=list(value.sessions), next_cursor=value.next_cursor)

    async def get(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        cancellation.checkpoint()
        params = cast(SessionGetParams, raw)
        value = await harness.sessions.get(LifecycleSessionGetCommand(identity.workspace_id, params.session_id))
        turns = list(await projections.turns(params.session_id, cancellation)) if params.include_turns else []
        return SessionGetResult(session=SessionDetail(summary=value.session, turns=turns))

    async def rename(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        cancellation.checkpoint()
        params = cast(SessionRenameParams, raw)
        current = await harness.sessions.get(LifecycleSessionGetCommand(identity.workspace_id, params.session_id))
        if params.expected_updated_at is not None and _timestamp(params.expected_updated_at) != _timestamp(
            current.session.updated_at
        ):
            raise ValueError("Session updatedAt changed before rename")
        value = await harness.sessions.rename(
            LifecycleSessionRenameCommand(
                identity.workspace_id,
                params.session_id,
                params.title,
                current.revision,
                _derived_idempotency("rename", params.to_wire()),
            )
        )
        return SessionRenameResult(session=value.session)

    async def delete(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        cancellation.checkpoint()
        params = cast(SessionDeleteParams, raw)
        if params.hard_delete:
            raise ValueError("hard Session deletion is forbidden by the local retention policy")
        current = await harness.sessions.get(LifecycleSessionGetCommand(identity.workspace_id, params.session_id))
        value = await harness.sessions.soft_delete(
            LifecycleSessionDeleteCommand(
                identity.workspace_id,
                params.session_id,
                current.revision,
                _derived_idempotency("delete", params.to_wire()),
            )
        )
        if attachments is None:
            raise CommandHandlerConfigurationError("Session deletion requires Conversation attachment storage")
        await attachments.delete_conversation(params.session_id, cancellation)
        return SessionDeleteResult(
            session_id=value.session_id,
            deleted=value.deleted,
            active_runs_cancel_requested=list(value.active_runs_cancel_requested),
        )

    async def fork(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        cancellation.checkpoint()
        params = cast(SessionForkParams, raw)
        current = await harness.sessions.get(LifecycleSessionGetCommand(identity.workspace_id, params.session_id))
        value = await harness.sessions.fork(
            LifecycleSessionForkCommand(
                workspace_id=identity.workspace_id,
                source_session_id=params.session_id,
                fork_turn_id=params.fork_turn_id,
                expected_revision=current.revision,
                idempotency_key=params.client_request_id,
                fork_run_id=params.fork_run_id,
                title=params.title,
            )
        )
        return SessionForkResult(
            session=value.session,
            source_session_id=value.source_session_id,
            fork_turn_id=value.fork_turn_id,
        )

    return {
        "session/create": create,
        "session/list": list_sessions,
        "session/get": get,
        "session/rename": rename,
        "session/delete": delete,
        "session/fork": fork,
    }


def _turn_handlers(
    *,
    identity: DomainCommandIdentity,
    harness: HarnessService,
    projections: ConversationProjectionService,
    config: ConfigService,
    transport_policy: ApplicationTransportPolicy,
    attachments: ConversationAttachmentStore,
) -> Mapping[str, ApplicationCommandHandler]:
    async def start(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        cancellation.checkpoint()
        params = cast(TurnStartParams, raw)
        snapshot = await config.snapshot(
            managed_owner_id=identity.managed_owner_id,
            profile_id=identity.profile_id,
            workspace_id=identity.workspace_id,
            session_id=params.session_id,
        )
        if params.run_config.provider != snapshot.config.model.provider.value:
            raise ValueError("Run provider differs from the effective persisted configuration")
        requested_mode = params.run_config.permission_mode
        if requested_mode is PermissionMode.BYPASS and not snapshot.config.policy.allow_bypass:
            raise PermissionError("bypass permission is disabled by the persisted Workspace policy")
        effective_mode = requested_mode
        # PLAN is itself a fail-closed read surface and must remain visible to
        # the Agent.  Every other non-read mode is reduced to READ_ONLY until
        # the Workspace has been explicitly trusted (or when managed policy is
        # read-only).
        if requested_mode is not PermissionMode.PLAN and (
            snapshot.config.policy.read_only or not snapshot.config.policy.workspace_trusted
        ):
            effective_mode = PermissionMode.READ_ONLY
        route = await transport_policy.resolve_run_route(
            context,
            effective_mode,
        )
        run_config = params.run_config.model_copy(
            update={
                "provider": snapshot.config.model.provider.value,
                "model": snapshot.config.model.model or params.run_config.model,
                "permission_mode": route.permission_mode,
            }
        )
        pinned = pinned_context_block(params.pinned_context)
        input_blocks = tuple(item.to_wire() for item in params.input)
        if pinned is not None:
            input_blocks = (*input_blocks, pinned)
        attachment_claims = tuple(
            AttachmentClaim(
                artifact_id=block.artifact.artifact_id,
                order=order,
                content_hash=block.artifact.content_hash,
                media_type=block.artifact.media_type,
                byte_length=block.artifact.size_bytes,
            )
            for order, block in enumerate(item for item in params.input if isinstance(item, ImageContentBlock))
        )
        attachment_claim_created = False
        if attachment_claims:
            attachment_claim_created = (
                await attachments.claim_submission_with_receipt(
                    params.session_id,
                    params.turn_id,
                    attachment_claims,
                    cancellation,
                )
            ).created
        try:
            receipt = await harness.start_turn(
                StartTurnCommand(
                    workspace_id=identity.workspace_id,
                    session_id=params.session_id,
                    turn_id=params.turn_id,
                    idempotency_key=params.idempotency_key,
                    input_blocks=input_blocks,
                    run_config=run_config.to_wire(),
                    effective_config=snapshot.config,
                    effective_config_fingerprint=snapshot.fingerprint,
                    deadline_at=None if params.deadline is None else _timestamp(params.deadline),
                )
            )
        except BaseException:
            if attachment_claim_created:
                await attachments.release_turn_claim(params.turn_id, _NeverCancelled())
            raise
        return TurnStartResult(
            session_id=receipt.session_id,
            turn_id=receipt.turn_id,
            run_id=receipt.run_id,
            accepted=receipt.accepted,
            duplicate=receipt.duplicate,
        )

    async def get(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        params = cast(TurnGetParams, raw)
        return TurnGetResult(turn=await projections.turn(params.session_id, params.turn_id, cancellation))

    async def cancel(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        params = cast(TurnCancelParams, raw)
        run_id = await projections.resolve_run_id(
            params.session_id,
            params.turn_id,
            params.run_id,
            cancellation,
        )
        run = await harness.get_run(run_id)
        if run.session_id != params.session_id or run.turn_id != params.turn_id:
            raise ValueError("turn/cancel Run does not belong to the requested Turn")
        already_terminal = run.status.is_terminal
        accepted = False if already_terminal else await harness.cancel_turn(run_id, reason=params.reason)
        return TurnCancelResult(run_id=run_id, accepted=accepted, already_terminal=already_terminal)

    return {"turn/start": start, "turn/get": get, "turn/cancel": cancel}


def _approval_handlers(
    *,
    identity: DomainCommandIdentity,
    approvals: ApprovalManager,
    clock: Clock,
) -> Mapping[str, ApplicationCommandHandler]:
    async def resolve(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        cancellation.checkpoint()
        params = cast(ApprovalResolveParams, raw)
        record = await approvals.get(params.approval_id)
        if record is None:
            raise ApprovalNotFound(params.approval_id)
        if record.request.binding.args_hash != params.expected_args_hash:
            raise ValueError("approval args hash no longer matches the presented request")
        state, scope = _approval_intent(params)
        already = record.resolution is not None
        result = await approvals.resolve(
            ApprovalResolution(
                approval_id=params.approval_id,
                state=state,
                scope=scope,
                resolved_at=clock.utcnow(),
                resolver_id=identity.actor_id,
                include_descendants=params.include_descendants,
                reason=params.comment,
            )
        )
        status = "already_resolved" if already else result.state.value
        return ApprovalResolveResult.model_validate(
            {
                "approvalId": params.approval_id,
                "status": status,
                "runId": record.request.binding.run_id,
                "resumed": not already and result.state is ApprovalState.APPROVED,
            }
        )

    return {"approval/resolve": resolve}


def _approval_intent(params: ApprovalResolveParams) -> tuple[ApprovalState, ApprovalScope]:
    decision = params.decision.value
    expected_scope = {
        "deny": "once",
        "allow_once": "once",
        "allow_run": "run",
        "allow_session": "session",
        "allow_persistent": "persistent",
    }[decision]
    if params.scope.value != expected_scope:
        raise ValueError("approval decision and scope are inconsistent")
    state = ApprovalState.DENIED if decision == "deny" else ApprovalState.APPROVED
    return state, ApprovalScope(params.scope.value)


def _artifact_handlers(
    *,
    identity: DomainCommandIdentity,
    artifacts: ArtifactStore,
) -> Mapping[str, ApplicationCommandHandler]:
    async def read(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        cancellation.checkpoint()
        params = cast(ArtifactReadParams, raw)
        metadata = await artifacts.metadata(params.artifact_id)
        if metadata is None or metadata.workspace_id != identity.workspace_id:
            raise FileNotFoundError("Artifact is unavailable in this Workspace")
        remaining = max(0, metadata.byte_length - params.offset)
        requested = min(params.max_bytes, remaining)
        chunks = [chunk async for chunk in artifacts.read(params.artifact_id, offset=params.offset, limit=requested)]
        cancellation.checkpoint()
        content = b"".join(chunks)
        if len(content) > requested:
            raise ValueError("Artifact Store exceeded the bounded read request")
        try:
            encoded = content.decode("utf-8")
            encoding = ArtifactEncoding.UTF8
        except UnicodeDecodeError:
            encoded = base64.b64encode(content).decode("ascii")
            encoding = ArtifactEncoding.BASE64
        next_offset = params.offset + len(content)
        from .application_handlers import _artifact_metadata_ref

        return ArtifactReadResult(
            artifact=_artifact_metadata_ref(metadata),
            offset=params.offset,
            next_offset=next_offset,
            encoding=encoding,
            content=encoded,
            eof=next_offset >= metadata.byte_length,
        )

    return {"artifact/read": read}


def _attachment_handlers(
    *,
    workspace_id: str,
    harness: HarnessService,
    attachments: ConversationAttachmentStore,
) -> Mapping[str, ApplicationCommandHandler]:
    async def require_session(session_id: str, cancellation: CancellationToken) -> None:
        cancellation.checkpoint()
        await harness.sessions.get(LifecycleSessionGetCommand(workspace_id, session_id))

    async def begin(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        params = cast(AttachmentBeginParams, raw)
        await require_session(params.session_id, cancellation)
        receipt = await attachments.begin(
            AttachmentUploadRequest(
                session_id=params.session_id,
                client_request_id=params.client_request_id,
                file_name=params.file_name,
                media_type=params.media_type,
                byte_length=params.byte_length,
                content_hash=params.content_hash,
            ),
            cancellation,
        )
        return AttachmentBeginResult(
            upload_id=receipt.upload_id,
            artifact_id=receipt.artifact_id,
            max_chunk_bytes=65_536,
            next_offset=receipt.next_offset,
            duplicate=receipt.duplicate,
        )

    async def chunk(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        params = cast(AttachmentChunkParams, raw)
        await require_session(params.session_id, cancellation)
        try:
            content = base64.b64decode(params.content_base64, validate=True)
        except ValueError as error:
            raise ValueError("attachment chunk is not canonical base64") from error
        content_hash = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if content_hash != params.content_hash:
            raise ValueError("attachment chunk hash does not match its bytes")
        receipt = await attachments.append(
            params.upload_id,
            params.offset,
            content,
            cancellation,
            session_id=params.session_id,
        )
        return AttachmentChunkResult(
            upload_id=receipt.upload_id,
            received_bytes=receipt.next_offset,
            duplicate=receipt.duplicate,
        )

    async def commit(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        params = cast(AttachmentCommitParams, raw)
        await require_session(params.session_id, cancellation)
        receipt = await attachments.commit(params.upload_id, cancellation, session_id=params.session_id)
        return AttachmentCommitResult(
            upload_id=receipt.upload_id,
            artifact=receipt.artifact,
            duplicate=receipt.duplicate,
        )

    async def abort(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        params = cast(AttachmentAbortParams, raw)
        await require_session(params.session_id, cancellation)
        await attachments.abort(params.upload_id, cancellation, session_id=params.session_id)
        return AttachmentAbortResult(upload_id=params.upload_id, aborted=True)

    async def read(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        params = cast(AttachmentReadParams, raw)
        await require_session(params.session_id, cancellation)
        receipt = await attachments.read_for_conversation(
            params.session_id,
            params.artifact_id,
            params.offset,
            params.max_bytes,
            cancellation,
        )
        return AttachmentReadResult(
            artifact=receipt.artifact,
            offset=receipt.offset,
            next_offset=receipt.next_offset,
            content_base64=base64.b64encode(receipt.content).decode("ascii"),
            eof=receipt.complete,
        )

    return {
        "attachments/begin": begin,
        "attachments/chunk": chunk,
        "attachments/commit": commit,
        "attachments/abort": abort,
        "attachments/read": read,
    }


def secret_command_handlers(
    *,
    identity: DomainCommandIdentity,
    secrets: SecretStore,
) -> Mapping[str, ApplicationCommandHandler]:
    def require_direct_stdio(context: ApplicationCommandContext) -> None:
        if context.transport != "stdio":
            raise PermissionError("Secret input is accepted only over the direct plugin stdio connection")

    async def list_secrets(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        require_direct_stdio(context)
        cancellation.checkpoint()
        params = cast(SecretsListParams, raw)
        values = await asyncio.to_thread(secrets.list_metadata, scope_id=identity.workspace_id)
        filtered = tuple(
            value
            for value in values
            if (params.kind is None or value.kind.value == params.kind)
            and (params.provider_id is None or value.provider_id == params.provider_id)
        )
        return SecretsListResult(secrets=[_secret_metadata(value) for value in filtered])

    async def put(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        require_direct_stdio(context)
        cancellation.checkpoint()
        params = cast(SecretsPutParams, raw)
        plaintext = bytearray(params.secret.get_secret_value().encode("utf-8"))
        secret_input: SecretInput | None = None
        try:
            secret_input = SecretInput(plaintext)
            if params.handle is None:
                value = await asyncio.to_thread(
                    secrets.create,
                    scope_id=identity.workspace_id,
                    kind=SecretKind(params.kind),
                    provider_id=params.provider_id,
                    secret=secret_input,
                )
                created = True
            else:
                assert params.expected_version is not None
                handle = SecretHandle(params.handle)
                current = await asyncio.to_thread(secrets.metadata, handle, scope_id=identity.workspace_id)
                if current.kind.value != params.kind or current.provider_id != params.provider_id:
                    raise ValueError("Secret rotation kind/provider must match the opaque handle metadata")
                value = await asyncio.to_thread(
                    secrets.rotate,
                    handle,
                    scope_id=identity.workspace_id,
                    expected_version=params.expected_version,
                    secret=secret_input,
                )
                created = False
            cancellation.checkpoint()
            return SecretsPutResult(secret=_secret_metadata(value), created=created)
        finally:
            for index in range(len(plaintext)):
                plaintext[index] = 0
            plaintext.clear()
            if secret_input is not None:
                secret_input.close()

    async def delete(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        require_direct_stdio(context)
        cancellation.checkpoint()
        params = cast(SecretsDeleteParams, raw)
        await asyncio.to_thread(
            secrets.delete,
            SecretHandle(params.handle),
            scope_id=identity.workspace_id,
            expected_version=params.expected_version,
        )
        return SecretsDeleteResult(handle=params.handle, deleted=True)

    return {"secrets/list": list_secrets, "secrets/put": put, "secrets/delete": delete}


def _secret_metadata(value: SecretMetadata) -> SecretMetadataSnapshot:
    return SecretMetadataSnapshot(
        handle=str(value.handle),
        kind=value.kind.value,
        provider_id=value.provider_id,
        version=value.version,
        created_at=value.created_at.isoformat(),
        rotated_at=value.rotated_at.isoformat(),
    )


def _shutdown_handlers(
    *,
    harness: HarnessService,
    projections: ConversationProjectionService,
) -> Mapping[str, ApplicationCommandHandler]:
    async def shutdown(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        del context
        cancellation.checkpoint()
        params = cast(ShutdownParams, raw)
        active = await projections.active_run_ids()
        await harness.shutdown(grace_seconds=params.grace_period_ms / 1000)
        return ShutdownResult(accepted=True, active_runs_cancel_requested=list(active))

    return {"shutdown": shutdown}


def _derived_idempotency(operation: str, value: Mapping[str, object]) -> str:
    return f"application-{operation}-{canonical_json_sha256(value).removeprefix('sha256:')}"


class _NeverCancelled:
    cancelled = False
    reason = None

    async def wait(self) -> Any:
        await asyncio.Future()

    def checkpoint(self) -> None:
        return


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return result


__all__ = [
    "ConversationProjectionService",
    "DomainCommandIdentity",
    "ModelCommandService",
    "compose_domain_command_handlers",
    "secret_command_handlers",
]
