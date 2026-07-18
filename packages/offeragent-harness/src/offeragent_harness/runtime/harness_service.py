from __future__ import annotations

import asyncio
import heapq
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, cast

from pydantic import TypeAdapter

from offeragent_harness.agent import (
    BudgetCheckpoint,
    BudgetExceeded,
    BudgetLedger,
    RunBudget,
    RunPreparationFailure,
    RunPreparationPort,
    safe_preparation_failure_details,
)
from offeragent_harness.agent.loop import AgentLoopFailure, RecoveredToolBatch, ToolKernel, run_agent_loop
from offeragent_harness.agent.planner import Planner
from offeragent_harness.agent.state import ALLOWED_PHASE_TRANSITIONS, RunPhase, RunState
from offeragent_harness.config import HarnessConfig
from offeragent_harness.error_codes import ErrorCode, ResourceConflictCause, ResourceNotFoundCause
from offeragent_harness.hooks import HookExecutionContext
from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json, thaw_json
from offeragent_harness.permissions import ApprovalResolution
from offeragent_harness.ports import (
    CancellationToken,
    Clock,
    EntityRecord,
    EntityRevisionConflict,
    EntityStore,
    EventSink,
    HookLifecyclePort,
    IdGenerator,
    NewEvent,
    OperationCancelled,
    StoredEvent,
    UnitOfWorkFactory,
)
from offeragent_harness.ports.subagents import ChildRunExecution, RootCancellationRegistry, SubagentTreeController
from offeragent_harness.protocol._base import validate_wire
from offeragent_harness.protocol.common import RunConfigSnapshot
from offeragent_harness.protocol.content import ContentBlock
from offeragent_harness.protocol.events import make_domain_event_record, parse_persisted_domain_event
from offeragent_harness.protocol.ids import ProfileId, RunId, SessionId, TurnId, WorkspaceId
from offeragent_harness.sessions import AgentLineage, Run, RunKind, RunStatus, Session, SessionStatus, Turn, TurnStatus
from offeragent_harness.subagents.models import SubagentResult
from offeragent_harness.subagents.reducer import ResultReducer
from offeragent_harness.tools import canonical_json_bytes, canonical_json_sha256

from .approval_manager import ApprovalManager
from .cancellation import CancellationCode, CancellationReason, CancellationScope
from .event_bus import AtomicEntityWrite, DeliveryFailure, UowRunRecorder
from .recovery import RecoveryDisposition
from .recovery_apply import RecoveryApplyResult
from .run_preparation import (
    ContextInputsEnricher,
    PreparedRunContext,
    RunContextProvider,
    RunPreparationLimits,
    RunPreparationRequest,
)
from .session_service import (
    SESSION_OPERATION_COLLECTION,
    SessionDeleteCommand,
    SessionDeleteResult,
    SessionForkCommand,
    SessionForkResult,
    SessionGetCommand,
    SessionGetResult,
    SessionIdempotencyConflict,
    SessionLifecycleService,
    SessionListCommand,
    SessionListResult,
    SessionRenameCommand,
    SessionRenameResult,
)
from .session_service import (
    SessionCreateCommand as LifecycleCreateCommand,
)
from .turn_manager import ActiveRun, RunControlInbox, TurnManager


class HarnessServiceError(RuntimeError):
    pass


class EntityNotFound(HarnessServiceError, ResourceNotFoundCause):
    pass


class IdempotencyKeyConflict(HarnessServiceError, ResourceConflictCause):
    conflict_reason = "idempotency_key_conflict"


class SessionRunConflict(HarnessServiceError, ResourceConflictCause):
    conflict_reason = "session_active_or_mutating"
    conflict_user_message = "the Session already has an active Run or lifecycle operation"


class RecoveryResumeRejected(HarnessServiceError, ResourceConflictCause):
    """A durable recovery result is stale or unsafe to enter the Agent Loop."""

    conflict_reason = "recovery_resume_rejected"

    def __init__(self, code: str, run_id: str, message: str) -> None:
        self.code = code
        self.run_id = run_id
        super().__init__(message)


_WORKSPACE_ID: TypeAdapter[str] = TypeAdapter(WorkspaceId)
_PROFILE_ID: TypeAdapter[str] = TypeAdapter(ProfileId)
_SESSION_ID: TypeAdapter[str] = TypeAdapter(SessionId)
_TURN_ID: TypeAdapter[str] = TypeAdapter(TurnId)
_RUN_ID: TypeAdapter[str] = TypeAdapter(RunId)
_CONTENT_BLOCK: TypeAdapter[Any] = TypeAdapter(ContentBlock)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_COMMAND_JSON_BYTES = 4 * 1024 * 1024


def _validate_idempotency_key(value: str) -> None:
    if _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ValueError("idempotency_key must be a 1-256 character canonical opaque key")


@dataclass(frozen=True, slots=True)
class CreateSessionCommand:
    workspace_id: str
    profile_id: str
    title: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if not self.workspace_id or not self.profile_id or not self.idempotency_key:
            raise ValueError("session command identity fields must not be empty")
        _WORKSPACE_ID.validate_python(self.workspace_id, strict=True)
        _PROFILE_ID.validate_python(self.profile_id, strict=True)
        _validate_idempotency_key(self.idempotency_key)
        if not 1 <= len(self.title) <= 512:
            raise ValueError("session title must contain between 1 and 512 characters")


@dataclass(frozen=True, slots=True)
class SessionReceipt:
    session_id: str
    workspace_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class StartTurnCommand:
    workspace_id: str
    session_id: str
    turn_id: str
    idempotency_key: str
    input_blocks: tuple[Mapping[str, Any], ...]
    run_config: Mapping[str, Any]
    effective_config: HarnessConfig | None = None
    effective_config_fingerprint: str | None = None
    deadline_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.workspace_id or not self.session_id or not self.turn_id or not self.idempotency_key:
            raise ValueError("turn command identity fields must not be empty")
        if not self.input_blocks:
            raise ValueError("turn input cannot be empty")
        _WORKSPACE_ID.validate_python(self.workspace_id, strict=True)
        _SESSION_ID.validate_python(self.session_id, strict=True)
        _TURN_ID.validate_python(self.turn_id, strict=True)
        _validate_idempotency_key(self.idempotency_key)
        if (self.effective_config is None) != (self.effective_config_fingerprint is None):
            raise ValueError("effective config and fingerprint must be present together")
        if self.effective_config_fingerprint is not None and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.effective_config_fingerprint
        ):
            raise ValueError("effective config fingerprint is invalid")
        if len(self.input_blocks) > 256:
            raise ValueError("turn input cannot contain more than 256 content blocks")
        encoded_command = json.dumps(
            {
                "input": thaw_json(self.input_blocks),
                "runConfig": thaw_json(self.run_config),
                "deadline": None if self.deadline_at is None else self.deadline_at.isoformat(),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded_command) > _MAX_COMMAND_JSON_BYTES:
            raise ValueError("turn command exceeds the local Harness command byte limit")
        for block in self.input_blocks:
            _CONTENT_BLOCK.validate_json(json.dumps(thaw_json(block), ensure_ascii=False, allow_nan=False))
        validate_wire(RunConfigSnapshot, thaw_json(self.run_config))
        if self.deadline_at is not None and (self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None):
            raise ValueError("turn deadline must be timezone-aware")
        frozen_blocks = tuple(freeze_json(block) for block in self.input_blocks)
        if any(not isinstance(block, FrozenJsonObject) for block in frozen_blocks):
            raise TypeError("turn input blocks must be JSON objects")
        frozen_config = freeze_json(self.run_config)
        if not isinstance(frozen_config, FrozenJsonObject):
            raise TypeError("run config must be a JSON object")
        object.__setattr__(self, "input_blocks", frozen_blocks)
        object.__setattr__(self, "run_config", frozen_config)


@dataclass(frozen=True, slots=True)
class RetryTurnCommand:
    workspace_id: str
    session_id: str
    turn_id: str
    source_run_id: str
    idempotency_key: str
    run_config: Mapping[str, Any] | None = None
    effective_config: HarnessConfig | None = None
    effective_config_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not all((self.workspace_id, self.session_id, self.turn_id, self.source_run_id, self.idempotency_key)):
            raise ValueError("retry command identity fields must not be empty")
        _WORKSPACE_ID.validate_python(self.workspace_id, strict=True)
        _SESSION_ID.validate_python(self.session_id, strict=True)
        _TURN_ID.validate_python(self.turn_id, strict=True)
        _RUN_ID.validate_python(self.source_run_id, strict=True)
        _validate_idempotency_key(self.idempotency_key)
        if (self.effective_config is None) != (self.effective_config_fingerprint is None):
            raise ValueError("effective config and fingerprint must be present together")
        if self.effective_config_fingerprint is not None and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.effective_config_fingerprint
        ):
            raise ValueError("effective config fingerprint is invalid")
        if self.run_config is not None:
            validate_wire(RunConfigSnapshot, self.run_config)
            frozen = freeze_json(self.run_config)
            if not isinstance(frozen, FrozenJsonObject):
                raise TypeError("retry run_config must be a JSON object")
            object.__setattr__(self, "run_config", frozen)


@dataclass(frozen=True, slots=True)
class TurnReceipt:
    session_id: str
    turn_id: str
    run_id: str
    accepted: bool
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class SessionEventReplayPage:
    """A deterministic merge view with an independent cursor per Run stream."""

    events: tuple[StoredEvent, ...]
    run_cursors: Mapping[str, int]
    has_more: bool

    def __post_init__(self) -> None:
        if any(sequence < 0 for sequence in self.run_cursors.values()):
            raise ValueError("event replay cursors cannot be negative")
        object.__setattr__(self, "run_cursors", dict(sorted(self.run_cursors.items())))


@dataclass(frozen=True, slots=True)
class RunEventReplayPage:
    events: tuple[StoredEvent, ...]
    last_sequence: int
    has_more: bool

    def __post_init__(self) -> None:
        if self.last_sequence < 0:
            raise ValueError("event replay cursor cannot be negative")


@dataclass(frozen=True, slots=True)
class RunComponents:
    planner_factory: PlannerFactory
    tool_kernel_factory: ToolKernelFactory
    budget: RunBudget
    hook_binding_factory: HookBindingFactory | None = None


class PlannerFactory(Protocol):
    def __call__(self, budget: BudgetLedger) -> Planner: ...


class ToolKernelFactory(Protocol):
    """Build the per-Run kernel against the one authoritative budget ledger."""

    def __call__(self, budget: BudgetLedger) -> ToolKernel: ...


@dataclass(frozen=True, slots=True)
class RunHookBinding:
    hooks: HookLifecyclePort | None
    context: HookExecutionContext | None

    def __post_init__(self) -> None:
        if (self.hooks is None) != (self.context is None):
            raise ValueError("Run Hook lifecycle and context must be configured together")


class HookBindingFactory(Protocol):
    def __call__(self, budget: BudgetLedger) -> RunHookBinding: ...


class RuntimeLifecycleHookProvider(Protocol):
    async def session_started(
        self,
        *,
        workspace_id: str,
        session_id: str,
        principal_id: str,
        connection_id: str,
        effective_config: HarnessConfig,
        cancellation: CancellationToken,
    ) -> None: ...


class RunComponentsFactory(Protocol):
    def build(self, command: StartTurnCommand, state: RunState) -> RunComponents: ...


class ChildRunComponentsFactory(Protocol):
    """Build child components using the same Planner and Kernel implementations."""

    def build_child(self, execution: ChildRunExecution, state: RunState) -> RunComponents: ...


@dataclass(frozen=True, slots=True)
class PreparedRunComponents:
    """Opaque in-memory preparation token plus its non-secret durable proof."""

    token: object
    durable_snapshot: Mapping[str, Any]

    def __post_init__(self) -> None:
        frozen = freeze_json(self.durable_snapshot)
        if not isinstance(frozen, FrozenJsonObject):
            raise TypeError("prepared Run component snapshot must be a JSON object")
        object.__setattr__(self, "durable_snapshot", frozen)


class AsyncRunComponentsPreparationPort(Protocol):
    """Optional two-phase capability preparation for factories that need async I/O."""

    def budget_root(self, command: StartTurnCommand, state: RunState) -> RunBudget: ...

    async def prepare_root(
        self,
        command: StartTurnCommand,
        state: RunState,
        cancellation: CancellationScope,
        durable_snapshot: Mapping[str, Any] | None,
    ) -> PreparedRunComponents: ...

    def build_prepared_root(
        self,
        command: StartTurnCommand,
        state: RunState,
        prepared: PreparedRunComponents,
    ) -> RunComponents: ...

    def budget_child(self, execution: ChildRunExecution, state: RunState) -> RunBudget: ...

    async def prepare_child(
        self,
        execution: ChildRunExecution,
        state: RunState,
        cancellation: CancellationScope,
        durable_snapshot: Mapping[str, Any] | None,
    ) -> PreparedRunComponents: ...

    def build_prepared_child(
        self,
        execution: ChildRunExecution,
        state: RunState,
        prepared: PreparedRunComponents,
    ) -> RunComponents: ...

    def release(self, run_id: str) -> Awaitable[None] | None: ...


class HookContextFactory(Protocol):
    def __call__(self, workspace_id: str, session_id: str, principal_id: str) -> HookExecutionContext: ...


@dataclass(slots=True)
class HarnessDiagnostics:
    delivery_failures: list[DeliveryFailure] = field(default_factory=list)
    cleanup_failures: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _PreparedRecoveredRun:
    result: RecoveryApplyResult
    budget: BudgetLedger
    planner: Planner | None
    tool_kernel: ToolKernel | None
    recorder: UowRunRecorder
    recovered_batch: RecoveredToolBatch
    hooks: HookLifecyclePort | None
    hook_context: HookExecutionContext | None
    run_preparation: RunPreparationPort | None
    preparation_error: BaseException | None = None


class HarnessService:
    """The only application entrypoint shared by stdio and Loopback adapters."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        event_sink: EventSink,
        clock: Clock,
        ids: IdGenerator,
        components: RunComponentsFactory,
        async_components: AsyncRunComponentsPreparationPort | None = None,
        turn_manager: TurnManager | None = None,
        approval_manager: ApprovalManager | None = None,
        session_service: SessionLifecycleService | None = None,
        hooks: HookLifecyclePort | None = None,
        hook_context_factory: HookContextFactory | None = None,
        lifecycle_hooks: RuntimeLifecycleHookProvider | None = None,
        child_components: ChildRunComponentsFactory | None = None,
        root_cancellations: RootCancellationRegistry | None = None,
        subagent_tree: SubagentTreeController | None = None,
        run_context_provider: RunContextProvider | None = None,
        context_enricher: ContextInputsEnricher | None = None,
        run_preparation_limits: RunPreparationLimits | None = None,
        required_root_initial_tool: str | None = None,
    ) -> None:
        if (hooks is None) != (hook_context_factory is None):
            raise ValueError("hooks and hook_context_factory must be configured together")
        if required_root_initial_tool is not None and (
            not required_root_initial_tool.strip() or len(required_root_initial_tool) > 256
        ):
            raise ValueError("required root initial Tool name must be a non-empty bounded string")
        self._unit_of_work = unit_of_work
        self._event_sink = event_sink
        self._clock = clock
        self._ids = ids
        self._components = components
        self._async_components = async_components
        self._child_components = child_components
        self._root_cancellations = root_cancellations
        self._subagent_tree = subagent_tree
        self._hooks = hooks
        self._hook_context_factory = hook_context_factory
        self._lifecycle_hooks = lifecycle_hooks
        self._run_context_provider = run_context_provider
        self._context_enricher = context_enricher or ContextInputsEnricher()
        self._run_preparation_limits = run_preparation_limits or RunPreparationLimits()
        self._required_root_initial_tool = required_root_initial_tool
        self._turn_manager = turn_manager or TurnManager()
        self._approval_manager = approval_manager or ApprovalManager(unit_of_work=unit_of_work, clock=clock)
        self._command_lock = asyncio.Lock()
        self.diagnostics = HarnessDiagnostics()
        self._session_service = session_service or SessionLifecycleService(
            unit_of_work=unit_of_work,
            event_sink=event_sink,
            clock=clock,
            ids=ids,
            turn_manager=self._turn_manager,
            approval_manager=self._approval_manager,
            delivery_failures=self.diagnostics.delivery_failures,
        )

    async def create_session(self, command: CreateSessionCommand) -> SessionReceipt:
        try:
            result = await self._session_service.create(
                LifecycleCreateCommand(
                    workspace_id=command.workspace_id,
                    profile_id=command.profile_id,
                    title=command.title,
                    idempotency_key=command.idempotency_key,
                )
            )
        except SessionIdempotencyConflict as error:
            raise IdempotencyKeyConflict(str(error)) from error
        return SessionReceipt(result.session.session_id, result.session.workspace_id, result.created)

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
        provider = self._lifecycle_hooks
        if provider is None:
            return
        await provider.session_started(
            workspace_id=workspace_id,
            session_id=session_id,
            principal_id=principal_id,
            connection_id=connection_id,
            effective_config=effective_config,
            cancellation=cancellation,
        )

    async def start_turn(self, command: StartTurnCommand) -> TurnReceipt:
        request_hash = canonical_json_sha256(
            {
                "workspaceId": command.workspace_id,
                "sessionId": command.session_id,
                "turnId": command.turn_id,
                "input": thaw_json(command.input_blocks),
                "runConfig": thaw_json(command.run_config),
                "effectiveConfigFingerprint": command.effective_config_fingerprint,
                "deadline": None if command.deadline_at is None else command.deadline_at.isoformat(),
            }
        )
        idempotency_id = f"{command.session_id}:{command.idempotency_key}"
        async with self._command_lock:
            async with self._unit_of_work.begin() as uow:
                existing = await uow.entities.get("turn_idempotency", idempotency_id)
                session = await uow.entities.get("sessions", command.session_id)
                session_operation = await uow.entities.get(SESSION_OPERATION_COLLECTION, command.session_id)
            if existing is not None:
                stored_hash, stored_receipt = _decode_receipt_record(existing, expected_kind="turn")
                if stored_hash != request_hash:
                    raise IdempotencyKeyConflict("turn idempotency key is bound to a different request")
                assert isinstance(stored_receipt, TurnReceipt)
                return replace(stored_receipt, duplicate=True)
            if session_operation is not None:
                raise SessionRunConflict(f"session {command.session_id!r} has a lifecycle operation in progress")
            if not isinstance(session, Session):
                raise EntityNotFound(f"session {command.session_id!r} does not exist")
            if session.workspace_id != command.workspace_id:
                raise HarnessServiceError("session belongs to a different workspace")
            if session.status is not SessionStatus.ACTIVE:
                raise HarnessServiceError("session is not active")

            run_id = self._ids.new_id("run")
            state = RunState(
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                turn_id=command.turn_id,
                run_id=run_id,
                lineage=AgentLineage.root(run_id),
            )
            deferred_components = self._async_components
            components = self._components.build(command, state) if deferred_components is None else None
            configured_budget = (
                components.budget
                if components is not None
                else cast(AsyncRunComponentsPreparationPort, deferred_components).budget_root(command, state)
            )
            started_at = self._clock.utcnow()
            budget_deadline = started_at + timedelta(seconds=configured_budget.max_wall_seconds)
            deadline_at = min(budget_deadline, command.deadline_at or budget_deadline)
            effective_wall_seconds = (deadline_at - started_at).total_seconds()
            if effective_wall_seconds <= 0:
                raise HarnessServiceError("turn deadline has already expired")
            trace_id = self._ids.new_id("trace")
            effective_budget = replace(configured_budget, max_wall_seconds=effective_wall_seconds)
            budget = BudgetLedger(effective_budget, started_at=started_at)
            initial_hook_binding = (
                None if components is None else self._hook_binding_for_components(components, budget, session)
            )
            state = replace(
                state,
                budget_checkpoint=await BudgetCheckpoint.capture(budget, now=started_at),
            )
            initial_tool_kernel = None if components is None else components.tool_kernel_factory(budget)
            initial_planner = None if components is None else components.planner_factory(budget)
            initial_run_preparation = (
                None
                if components is None or initial_planner is None
                else self._prepare_run_context(
                    state=state,
                    profile_id=session.profile_id,
                    query_text=_context_query(command.input_blocks),
                    active_file=_explicit_instruction_scope(command.input_blocks),
                    effective_config=command.effective_config,
                    planner=initial_planner,
                )
            )
            ready: asyncio.Future[UowRunRecorder] = asyncio.get_running_loop().create_future()
            controls = RunControlInbox()

            async def execute(cancellation: CancellationScope) -> RunState:
                recorder = await ready
                current_state = state
                run_components = components
                tool_kernel = initial_tool_kernel
                planner = initial_planner
                hook_binding = initial_hook_binding
                run_preparation = initial_run_preparation
                if deferred_components is not None:
                    try:
                        prepared = await deferred_components.prepare_root(
                            command,
                            current_state,
                            cancellation,
                            None,
                        )
                        current_state = await self._commit_prepared_components(
                            current_state,
                            recorder,
                            prepared,
                        )
                        run_components = deferred_components.build_prepared_root(
                            command,
                            current_state,
                            prepared,
                        )
                        if run_components.budget != configured_budget:
                            raise HarnessServiceError(
                                "prepared root component budget differs from its pure budget_root result"
                            )
                    except (OperationCancelled, asyncio.CancelledError) as error:
                        terminal = await self._terminalize_component_preparation_failure(
                            current_state,
                            recorder,
                            budget,
                            error,
                        )
                        await self._release_prepared_components(current_state.run_id)
                        return terminal
                    except BaseException as error:
                        terminal = await self._terminalize_component_preparation_failure(
                            current_state,
                            recorder,
                            budget,
                            error,
                        )
                        await self._release_prepared_components(current_state.run_id)
                        raise AgentLoopFailure(terminal, error) from error
                    tool_kernel = run_components.tool_kernel_factory(budget)
                    hook_binding = self._hook_binding_for_components(
                        run_components,
                        budget,
                        session,
                    )
                    planner = run_components.planner_factory(budget)
                    run_preparation = self._prepare_run_context(
                        state=current_state,
                        profile_id=session.profile_id,
                        query_text=_context_query(command.input_blocks),
                        active_file=_explicit_instruction_scope(command.input_blocks),
                        effective_config=command.effective_config,
                        planner=planner,
                    )
                assert (
                    run_components is not None
                    and tool_kernel is not None
                    and planner is not None
                    and hook_binding is not None
                )
                return await self._execute_agent_run(
                    state=current_state,
                    planner=planner,
                    tool_kernel=tool_kernel,
                    recorder=recorder,
                    budget=budget,
                    deadline_at=deadline_at,
                    cancellation=cancellation,
                    recovered_batch=None,
                    hooks=hook_binding.hooks,
                    hook_context=hook_binding.context,
                    control_inbox=controls,
                    run_preparation=run_preparation,
                )

            active = await self._start_managed_run(
                session_id=command.session_id,
                run_id=run_id,
                factory=execute,
                controls=controls,
            )
            self._register_root_cancellation(active)
            persist_task = asyncio.create_task(
                self._persist_turn_start(
                    command=command,
                    request_hash=request_hash,
                    idempotency_id=idempotency_id,
                    session=session,
                    state=state,
                    trace_id=trace_id,
                    started_at=started_at,
                    deadline_at=deadline_at,
                    budget=budget,
                ),
                name=f"persist-turn-start:{run_id}",
            )
            try:
                receipt, recorder = await asyncio.shield(persist_task)
            except asyncio.CancelledError:
                # Once start persistence begins, caller/transport disconnect
                # must not leave a committed Run without releasing its loop.
                # Finish the atomic receipt, wake the active Run, then preserve
                # cancellation for the caller; an idempotent retry recovers the
                # same receipt.
                try:
                    receipt, recorder = await persist_task
                except BaseException as error:
                    await self._abort_turn_start(ready, active, error)
                    raise
                ready.set_result(recorder)
                raise
            except Exception as error:
                await self._abort_turn_start(ready, active, error)
                raise
            ready.set_result(recorder)
            return receipt

    async def retry_turn(self, command: RetryTurnCommand) -> TurnReceipt:
        """Create a new root Run for an existing Turn without replaying effects."""

        request_hash = canonical_json_sha256(
            {
                "workspaceId": command.workspace_id,
                "sessionId": command.session_id,
                "turnId": command.turn_id,
                "sourceRunId": command.source_run_id,
                "runConfig": command.run_config,
                "effectiveConfigFingerprint": command.effective_config_fingerprint,
            }
        )
        idempotency_id = f"{command.session_id}:{command.idempotency_key}"
        async with self._command_lock:
            async with self._unit_of_work.begin() as uow:
                existing = await uow.entities.get("turn_retry_idempotency", idempotency_id)
                session = await uow.entities.get("sessions", command.session_id)
                turn = await uow.entities.get("turns", command.turn_id)
                source_run = await uow.entities.get("runs", command.source_run_id)
                source_state = await uow.entities.get("run_states", command.source_run_id)
                source_effective = await uow.entities.get("run_effective_configs", command.source_run_id)
                session_operation = await uow.entities.get(SESSION_OPERATION_COLLECTION, command.session_id)
            if existing is not None:
                stored_hash, stored_receipt = _decode_receipt_record(existing, expected_kind="turn")
                if stored_hash != request_hash:
                    raise IdempotencyKeyConflict("retry idempotency key is bound to a different request")
                assert isinstance(stored_receipt, TurnReceipt)
                return replace(stored_receipt, duplicate=True)
            if session_operation is not None:
                raise SessionRunConflict(f"session {command.session_id!r} has a lifecycle operation in progress")
            if not isinstance(session, Session) or not isinstance(turn, Turn) or not isinstance(source_run, Run):
                raise EntityNotFound("retry Session, Turn, or source Run does not exist")
            if (
                session.workspace_id != command.workspace_id
                or source_run.workspace_id != command.workspace_id
                or turn.session_id != command.session_id
                or source_run.session_id != command.session_id
                or source_run.turn_id != command.turn_id
            ):
                raise HarnessServiceError("retry authority belongs to a different Workspace/Session/Turn")
            if session.status is not SessionStatus.ACTIVE or not source_run.status.is_terminal:
                raise HarnessServiceError("retry requires an active Session and terminal source Run")
            if not isinstance(source_state, RunState) or not source_state.phase.terminal:
                raise HarnessServiceError("retry source Run has no durable terminal state")
            if any(
                effect.state.value in {"attempted", "committed", "partial", "unknown"}
                for result in source_state.tool_results
                for effect in result.side_effects
            ):
                raise HarnessServiceError("retry is denied because the source Run may have external side effects")

            effective_config = command.run_config or source_run.config_snapshot
            preparation_config = command.effective_config or _effective_config_from_record(
                source_effective,
                workspace_id=command.workspace_id,
                run_id=command.source_run_id,
            )
            start_command = StartTurnCommand(
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                turn_id=command.turn_id,
                idempotency_key=command.idempotency_key,
                input_blocks=tuple(thaw_json(turn.input_blocks)),
                run_config=thaw_json(effective_config),
                effective_config=preparation_config,
                effective_config_fingerprint=(
                    None
                    if preparation_config is None
                    else command.effective_config_fingerprint
                    or canonical_json_sha256(preparation_config.model_dump(mode="json"))
                ),
            )
            run_id = self._ids.new_id("run")
            state = RunState(
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                turn_id=command.turn_id,
                run_id=run_id,
                lineage=AgentLineage.root(run_id),
                write_obligation=source_state.write_obligation.inherited_for_retry(),
            )
            deferred_components = self._async_components
            components = self._components.build(start_command, state) if deferred_components is None else None
            configured_budget = (
                components.budget
                if components is not None
                else cast(AsyncRunComponentsPreparationPort, deferred_components).budget_root(start_command, state)
            )
            started_at = self._clock.utcnow()
            deadline_at = started_at + timedelta(seconds=configured_budget.max_wall_seconds)
            trace_id = self._ids.new_id("trace")
            budget = BudgetLedger(configured_budget, started_at=started_at)
            initial_hook_binding = (
                None if components is None else self._hook_binding_for_components(components, budget, session)
            )
            state = replace(state, budget_checkpoint=await BudgetCheckpoint.capture(budget, now=started_at))
            initial_tool_kernel = None if components is None else components.tool_kernel_factory(budget)
            initial_planner = None if components is None else components.planner_factory(budget)
            initial_run_preparation = (
                None
                if components is None or initial_planner is None
                else self._prepare_run_context(
                    state=state,
                    profile_id=session.profile_id,
                    query_text=_context_query(start_command.input_blocks),
                    effective_config=preparation_config,
                    planner=initial_planner,
                )
            )
            ready: asyncio.Future[UowRunRecorder] = asyncio.get_running_loop().create_future()
            controls = RunControlInbox()

            async def execute(cancellation: CancellationScope) -> RunState:
                recorder = await ready
                current_state = state
                run_components = components
                tool_kernel = initial_tool_kernel
                planner = initial_planner
                hook_binding = initial_hook_binding
                run_preparation = initial_run_preparation
                if deferred_components is not None:
                    try:
                        prepared = await deferred_components.prepare_root(
                            start_command,
                            current_state,
                            cancellation,
                            None,
                        )
                        current_state = await self._commit_prepared_components(
                            current_state,
                            recorder,
                            prepared,
                        )
                        run_components = deferred_components.build_prepared_root(
                            start_command,
                            current_state,
                            prepared,
                        )
                        if run_components.budget != configured_budget:
                            raise HarnessServiceError(
                                "prepared root component budget differs from its pure budget_root result"
                            )
                        hook_binding = self._hook_binding_for_components(
                            run_components,
                            budget,
                            session,
                        )
                        tool_kernel = run_components.tool_kernel_factory(budget)
                        planner = run_components.planner_factory(budget)
                        run_preparation = self._prepare_run_context(
                            state=current_state,
                            profile_id=session.profile_id,
                            query_text=_context_query(start_command.input_blocks),
                            effective_config=preparation_config,
                            planner=planner,
                        )
                    except BaseException as error:
                        terminal = await self._terminalize_component_preparation_failure(
                            current_state,
                            recorder,
                            budget,
                            error,
                        )
                        await self._release_prepared_components(current_state.run_id)
                        raise AgentLoopFailure(terminal, error) from error
                assert (
                    run_components is not None
                    and tool_kernel is not None
                    and planner is not None
                    and hook_binding is not None
                )
                return await self._execute_agent_run(
                    state=current_state,
                    planner=planner,
                    tool_kernel=tool_kernel,
                    recorder=recorder,
                    budget=budget,
                    deadline_at=deadline_at,
                    cancellation=cancellation,
                    recovered_batch=None,
                    hooks=hook_binding.hooks,
                    hook_context=hook_binding.context,
                    control_inbox=controls,
                    run_preparation=run_preparation,
                )

            active = await self._start_managed_run(
                session_id=command.session_id,
                run_id=run_id,
                factory=execute,
                controls=controls,
            )
            self._register_root_cancellation(active)
            persist_task = asyncio.create_task(
                self._persist_turn_retry(
                    command=command,
                    start_command=start_command,
                    request_hash=request_hash,
                    idempotency_id=idempotency_id,
                    session=session,
                    turn=turn,
                    source_run=source_run,
                    state=state,
                    trace_id=trace_id,
                    started_at=started_at,
                    deadline_at=deadline_at,
                    budget=budget,
                ),
                name=f"persist-turn-retry:{run_id}",
            )
            try:
                receipt, recorder = await asyncio.shield(persist_task)
            except asyncio.CancelledError:
                try:
                    receipt, recorder = await persist_task
                except BaseException as error:
                    await self._abort_turn_start(ready, active, error)
                    raise
                ready.set_result(recorder)
                raise
            except Exception as error:
                await self._abort_turn_start(ready, active, error)
                raise
            ready.set_result(recorder)
            return receipt

    async def execute_child_agent(
        self,
        execution: ChildRunExecution,
        cancellation: CancellationScope,
        control_inbox: RunControlInbox,
    ) -> SubagentResult:
        """Execute a persisted child through this HarnessService's only Agent Loop."""

        if self._child_components is None:
            raise HarnessServiceError("child Run components are not configured")
        async with self._unit_of_work.begin() as uow:
            state_value = await uow.entities.get("run_states", execution.record.run_id)
            run_value = await uow.entities.get("runs", execution.record.run_id)
            session_value = await uow.entities.get("sessions", execution.record.session_id)
            parent_state_value = await uow.entities.get("run_states", execution.record.parent_run_id)
            effective_value = await uow.entities.get("run_effective_configs", execution.record.root_run_id)
            capability_value = await uow.entities.get("run_capability_snapshots", execution.record.run_id)
            latest_sequence = await uow.events.latest_sequence(execution.record.run_id)
        if (
            not isinstance(state_value, RunState)
            or not isinstance(run_value, Run)
            or not isinstance(session_value, Session)
            or not isinstance(parent_state_value, RunState)
            or parent_state_value.budget_checkpoint is None
        ):
            raise HarnessServiceError("persisted child Run state is missing or corrupt")
        if (
            state_value.lineage != execution.record.lineage
            or run_value.kind is not RunKind.SUBAGENT
            or latest_sequence != execution.event_sequence
            or session_value.session_id != execution.record.session_id
            or session_value.workspace_id != execution.record.workspace_id
        ):
            raise HarnessServiceError("child Run execution cursor/lineage is stale")
        child_budget = _subagent_run_budget(
            execution,
            parent_max_parallel_reads=parent_state_value.budget_checkpoint.budget.max_parallel_reads,
        )
        started_at = self._clock.utcnow()
        budget = BudgetLedger(child_budget, started_at=started_at)
        state = replace(state_value, budget_checkpoint=await BudgetCheckpoint.capture(budget, now=started_at))
        effective_config = _effective_config_from_record(
            effective_value,
            workspace_id=execution.record.workspace_id,
            run_id=execution.record.root_run_id,
        )
        recorder = UowRunRecorder(
            unit_of_work=self._unit_of_work,
            event_sink=self._event_sink,
            clock=self._clock,
            ids=self._ids,
            run_id=state.run_id,
            trace_id=execution.trace_id,
            budget=budget,
            expected_entity_revision=execution.state_entity_revision,
            expected_event_sequence=execution.event_sequence,
            expected_run_revision=execution.run_entity_revision,
        )
        deferred_components = self._async_components
        components: RunComponents | None = None
        if deferred_components is None:
            components = self._child_components.build_child(execution, state)
        else:
            configured_budget = deferred_components.budget_child(execution, state)
            if configured_budget != child_budget:
                raise HarnessServiceError("pure child component budget differs from the reserved child budget")
            try:
                durable_snapshot = self._capability_snapshot_payload(
                    capability_value,
                    state,
                    required=False,
                )
                prepared = await deferred_components.prepare_child(
                    execution,
                    state,
                    cancellation,
                    durable_snapshot,
                )
                if durable_snapshot is None:
                    state = await self._commit_prepared_components(state, recorder, prepared)
                else:
                    self._assert_prepared_snapshot_matches(state, prepared, capability_value)
                components = deferred_components.build_prepared_child(execution, state, prepared)
            except BaseException as error:
                terminal = await self._terminalize_component_preparation_failure(
                    state,
                    recorder,
                    budget,
                    error,
                )
                await self._release_prepared_components(state.run_id)
                raise AgentLoopFailure(terminal, error) from error
        assert components is not None
        if components.budget != child_budget:
            budget_error = HarnessServiceError("child component budget differs from the reserved child budget")
            terminal = await self._terminalize_component_preparation_failure(state, recorder, budget, budget_error)
            await self._release_prepared_components(state.run_id)
            raise AgentLoopFailure(terminal, budget_error) from budget_error
        hook_binding = self._hook_binding_for_components(components, budget, session_value)
        tool_kernel = components.tool_kernel_factory(budget)
        planner = components.planner_factory(budget)
        run_preparation = self._prepare_run_context(
            state=state,
            profile_id=session_value.profile_id,
            query_text=_context_query(execution.context.content),
            effective_config=effective_config,
            planner=planner,
        )
        result = await self._execute_agent_run(
            state=state,
            planner=planner,
            tool_kernel=tool_kernel,
            recorder=recorder,
            budget=budget,
            deadline_at=execution.record.deadline_at,
            cancellation=cancellation,
            recovered_batch=None,
            hooks=hook_binding.hooks,
            hook_context=hook_binding.context,
            control_inbox=control_inbox,
            run_preparation=run_preparation,
        )
        snapshot = await budget.snapshot(now=self._clock.utcnow())
        return ResultReducer().reduce(
            execution.record,
            status=result.phase.value,
            assistant_text=result.assistant_text,
            tool_results=result.tool_results,
            usage={
                "inputTokens": snapshot.used.input_tokens,
                "outputTokens": snapshot.used.output_tokens,
                "modelCalls": snapshot.used.model_rounds,
                "toolCalls": snapshot.used.tool_calls,
                "wallTimeSeconds": snapshot.elapsed_seconds,
                "artifactBytes": snapshot.used.artifact_bytes,
                "childCount": snapshot.used.subagents,
                "costMicros": int(snapshot.used.cost * Decimal(1_000_000)),
            },
        )

    async def _execute_agent_run(
        self,
        *,
        state: RunState,
        planner: Planner,
        tool_kernel: ToolKernel,
        recorder: UowRunRecorder,
        budget: BudgetLedger,
        deadline_at: datetime,
        cancellation: CancellationScope,
        recovered_batch: RecoveredToolBatch | None,
        hooks: HookLifecyclePort | None,
        hook_context: HookExecutionContext | None,
        control_inbox: RunControlInbox,
        run_preparation: RunPreparationPort | None,
    ) -> RunState:
        async def cancel_at_deadline() -> None:
            try:
                await self._clock.sleep_until(deadline_at)
                await cancellation.cancel(
                    CancellationReason(
                        code=CancellationCode.DEADLINE,
                        message="run wall-time budget exceeded",
                        requested_at=self._clock.utcnow(),
                    )
                )
            except asyncio.CancelledError:
                return

        deadline = asyncio.create_task(cancel_at_deadline(), name=f"run-deadline:{state.run_id}")
        terminal_committed = False
        try:
            if self._clock.utcnow() >= deadline_at:
                await cancellation.cancel(
                    CancellationReason(
                        code=CancellationCode.DEADLINE,
                        message="run wall-time budget exceeded before Loop dispatch",
                        requested_at=self._clock.utcnow(),
                    )
                )
            result = await run_agent_loop(
                state,
                planner=planner,
                tool_kernel=tool_kernel,
                recorder=recorder,
                budget=budget,
                cancellation=cancellation,
                now=self._clock.utcnow,
                recovered_tool_batch=recovered_batch,
                hooks=hooks,
                hook_context=hook_context,
                control_inbox=control_inbox,
                run_preparation=run_preparation,
                required_root_initial_tool=self._required_root_initial_tool,
            )
            if result.phase.terminal:
                persisted = await self._authoritative_terminal_state(result.run_id)
                if persisted is None:
                    raise AgentLoopFailure(
                        result,
                        RuntimeError("Agent Loop returned terminal state without one authoritative terminal commit"),
                    )
                terminal_committed = True
                return persisted
            return result
        except AgentLoopFailure as error:
            persisted = await self._authoritative_terminal_state(error.state.run_id)
            terminal_committed = persisted is not None
            if persisted is not None:
                cause = error.__cause__ if error.__cause__ is not None else error
                raise AgentLoopFailure(persisted, cause) from error
            raise
        finally:
            deadline.cancel()
            await asyncio.gather(deadline, return_exceptions=True)
            self.diagnostics.delivery_failures.extend(recorder.delivery_failures)
            if terminal_committed:
                await self._release_prepared_components(state.run_id)
                if self._subagent_tree is not None:
                    try:
                        if state.lineage.depth == 0 and state.phase.value != "completed":
                            await self._subagent_tree.cancel_descendants(
                                state.run_id,
                                "root Run terminated; cascading cancellation",
                            )
                        else:
                            await self._subagent_tree.parent_finished(
                                state.run_id,
                                turn_finished=state.lineage.depth == 0,
                                reason="parent Run reached a durable terminal state",
                            )
                    except Exception as error:
                        self.diagnostics.cleanup_failures.append(
                            f"Subagent lifetime cleanup failed for {state.run_id}: {type(error).__name__}: {error}"
                        )
                if state.lineage.depth == 0:
                    try:
                        await self._approval_manager.revoke_run_grants(
                            state.lineage.root_run_id,
                            reason="root Run reached a durable terminal state",
                        )
                    except Exception as error:
                        self.diagnostics.cleanup_failures.append(
                            f"approval grant revocation failed for {state.lineage.root_run_id}: "
                            f"{type(error).__name__}: {error}"
                        )

    async def _authoritative_terminal_state(self, run_id: str) -> RunState | None:
        try:
            async with self._unit_of_work.begin() as uow:
                state = await uow.entities.get("run_states", run_id)
                run = await uow.entities.get("runs", run_id)
                terminal = await uow.events.terminal_event(run_id)
                latest_sequence = await uow.events.latest_sequence(run_id)
                turn = None if not isinstance(run, Run) else await uow.entities.get("turns", run.turn_id)
                lease = None if not isinstance(run, Run) else await uow.entities.get("active_root_runs", run.session_id)
        except Exception as error:
            self.diagnostics.cleanup_failures.append(
                f"terminal authority read failed for {run_id}: {type(error).__name__}: {error}"
            )
            return None
        if (
            not isinstance(state, RunState)
            or not state.phase.terminal
            or not isinstance(run, Run)
            or not run.status.is_terminal
            or run.status.value != state.phase.value
            or not isinstance(turn, Turn)
            or turn.status.value != state.phase.value
            or terminal is None
            or not terminal.terminal
            or terminal.sequence != latest_sequence
            or run.event_sequence != latest_sequence
            or lease is not None
        ):
            return None
        return state

    async def publish_recovery_results(self, results: Sequence[RecoveryApplyResult]) -> None:
        """Best-effort delivery for already durable recovery facts."""

        for result in results:
            try:
                await self._event_sink.publish(result.events)
            except Exception as error:
                self.diagnostics.delivery_failures.append(
                    DeliveryFailure(
                        event_ids=tuple(event.event_id for event in result.events),
                        error_type=type(error).__name__,
                        message=str(error),
                    )
                )

    async def resume_recovered_run(self, result: RecoveryApplyResult) -> ActiveRun:
        """Resume exactly one applied Run without invoking a fresh Planner first."""

        active = await self.resume_recovered_runs((result,))
        return active[0]

    async def resume_recovered_runs(
        self,
        results: Sequence[RecoveryApplyResult],
        *,
        before_release: Callable[[], Awaitable[object]] | None = None,
    ) -> tuple[ActiveRun, ...]:
        """Validate an entire recovery batch before releasing any canonical Loop."""

        recovered = tuple(results)
        run_ids = tuple(result.run.run_id for result in recovered)
        session_ids = tuple(result.run.session_id for result in recovered)
        if len(run_ids) != len(set(run_ids)) or len(session_ids) != len(set(session_ids)):
            raise RecoveryResumeRejected(
                "duplicate_recovery_identity",
                run_ids[0] if run_ids else "run_unknown",
                "Recovery resume batch contains duplicate Run or Session identities.",
            )

        prepared_items: list[_PreparedRecoveredRun] = []
        for result in recovered:
            prepared_items.append(await self._prepare_recovered_run(result))
        prepared = tuple(prepared_items)
        for item in prepared:
            await self._validate_recovery_authority(item.result)
            self._validate_recovery_deadline(item.result)

        release: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        active_runs: list[ActiveRun] = []
        try:
            for item in prepared:
                controls = RunControlInbox()

                async def execute(
                    cancellation: CancellationScope,
                    prepared_run: _PreparedRecoveredRun = item,
                    control_inbox: RunControlInbox = controls,
                ) -> RunState:
                    await release
                    if prepared_run.preparation_error is not None:
                        terminal = await self._terminalize_component_preparation_failure(
                            prepared_run.result.state,
                            prepared_run.recorder,
                            prepared_run.budget,
                            prepared_run.preparation_error,
                        )
                        await self._release_prepared_components(prepared_run.result.run.run_id)
                        return terminal
                    run = prepared_run.result.run
                    deadline_at = run.deadline_at
                    assert deadline_at is not None
                    assert prepared_run.planner is not None
                    assert prepared_run.tool_kernel is not None
                    return await self._execute_agent_run(
                        state=prepared_run.result.state,
                        planner=prepared_run.planner,
                        tool_kernel=prepared_run.tool_kernel,
                        recorder=prepared_run.recorder,
                        budget=prepared_run.budget,
                        deadline_at=deadline_at,
                        cancellation=cancellation,
                        recovered_batch=prepared_run.recovered_batch,
                        hooks=prepared_run.hooks,
                        hook_context=prepared_run.hook_context,
                        control_inbox=control_inbox,
                        run_preparation=prepared_run.run_preparation,
                    )

                active = await self._start_managed_run(
                    session_id=item.result.run.session_id,
                    run_id=item.result.run.run_id,
                    factory=execute,
                    controls=controls,
                )
                self._register_root_cancellation(active)
                active_runs.append(active)
        except BaseException as error:
            release.cancel()
            reason = CancellationReason.now(CancellationCode.START_FAILED, "recovery registration failed")
            await asyncio.gather(*(item.cancellation.cancel(reason) for item in active_runs), return_exceptions=True)
            await asyncio.gather(*(item.task for item in active_runs), return_exceptions=True)
            if isinstance(error, asyncio.CancelledError):
                raise
            run_id = prepared[len(active_runs)].result.run.run_id if len(active_runs) < len(prepared) else "run_unknown"
            raise RecoveryResumeRejected(
                "turn_manager_registration_failed",
                run_id,
                f"TurnManager rejected recovered Run registration: {type(error).__name__}",
            ) from error
        if before_release is not None:
            try:
                await before_release()
            except BaseException as error:
                release.cancel()
                reason = CancellationReason.now(
                    CancellationCode.START_FAILED,
                    "subagent recovery failed before root Run release",
                )
                await asyncio.gather(
                    *(item.cancellation.cancel(reason) for item in active_runs),
                    return_exceptions=True,
                )
                await asyncio.gather(*(item.task for item in active_runs), return_exceptions=True)
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise RecoveryResumeRejected(
                    "subagent_recovery_failed",
                    run_ids[0] if run_ids else "run_unknown",
                    f"Subagent recovery failed before root Run release: {type(error).__name__}",
                ) from error
        release.set_result(None)
        return tuple(active_runs)

    async def _start_managed_run(
        self,
        *,
        session_id: str,
        run_id: str,
        factory: Any,
        controls: RunControlInbox,
    ) -> ActiveRun:
        """Preserve custom TurnManager adapters compiled before control inboxes."""

        settle_terminal = getattr(self._turn_manager, "settle_terminal", None)
        if callable(settle_terminal):

            async def is_durably_terminal(active_run_id: str) -> bool:
                return await self._authoritative_terminal_state(active_run_id) is not None

            await settle_terminal(session_id, is_durably_terminal)
        parameters = inspect.signature(self._turn_manager.start).parameters
        if "controls" in parameters:
            return await self._turn_manager.start(
                session_id=session_id,
                run_id=run_id,
                factory=factory,
                controls=controls,
            )
        return await self._turn_manager.start(session_id=session_id, run_id=run_id, factory=factory)

    async def _prepare_recovered_run(self, result: RecoveryApplyResult) -> _PreparedRecoveredRun:
        if result.disposition is not RecoveryDisposition.RESUME:
            raise RecoveryResumeRejected(
                "recovery_disposition_not_resumable",
                result.run.run_id,
                "Harness recovery entry only accepts RESUME results.",
            )
        self._validate_recovery_deadline(result)
        await self._validate_recovery_authority(result)
        checkpoint = result.state.budget_checkpoint
        if not isinstance(checkpoint, BudgetCheckpoint):
            raise RecoveryResumeRejected(
                "budget_checkpoint_missing",
                result.run.run_id,
                "Recovered Run lacks a strict BudgetCheckpoint.",
            )
        async with self._unit_of_work.begin() as uow:
            effective_record = await uow.entities.get("run_effective_configs", result.run.run_id)
            capability_record = await uow.entities.get("run_capability_snapshots", result.run.run_id)
            session_value = await uow.entities.get("sessions", result.run.session_id)
        if (
            not isinstance(session_value, Session)
            or session_value.workspace_id != result.run.workspace_id
            or session_value.session_id != result.run.session_id
        ):
            raise RecoveryResumeRejected(
                "recovery_session_missing",
                result.run.run_id,
                "Recovered Run has no authoritative Session for context scope resolution.",
            )
        effective_config: HarnessConfig | None = None
        effective_fingerprint: str | None = None
        try:
            if effective_record is not None:
                if not isinstance(effective_record, Mapping):
                    raise TypeError("Run effective config is not an object")
                if (
                    effective_record.get("schemaVersion") != 1
                    or effective_record.get("workspaceId") != result.run.workspace_id
                    or effective_record.get("runId") != result.run.run_id
                ):
                    raise ValueError("Run effective config identity is invalid")
                raw_fingerprint = effective_record.get("fingerprint")
                raw_config = effective_record.get("config")
                if not isinstance(raw_fingerprint, str) or not isinstance(raw_config, Mapping):
                    raise TypeError("Run effective config payload is invalid")
                effective_config = HarnessConfig.model_validate(dict(raw_config))
                effective_fingerprint = raw_fingerprint
            raw_input_blocks = thaw_json(result.turn.input_blocks)
            raw_run_config = thaw_json(result.run.config_snapshot)
            if not isinstance(raw_input_blocks, (list, tuple)) or not all(
                isinstance(block, Mapping) for block in raw_input_blocks
            ):
                raise TypeError("persisted Turn input is not a sequence of objects")
            if not isinstance(raw_run_config, Mapping):
                raise TypeError("persisted Run config is not an object")
            command = StartTurnCommand(
                workspace_id=result.run.workspace_id,
                session_id=result.run.session_id,
                turn_id=result.run.turn_id,
                idempotency_key=f"recovery:{result.run.run_id}",
                input_blocks=tuple(dict(block) for block in raw_input_blocks),
                run_config=dict(raw_run_config),
                effective_config=effective_config,
                effective_config_fingerprint=effective_fingerprint,
            )
        except (TypeError, ValueError) as error:
            failure = RecoveryResumeRejected(
                "persisted_run_config_invalid",
                result.run.run_id,
                "Persisted Turn input or Run config no longer passes the command schema.",
            )
            failure.__cause__ = error
            return self._failed_recovered_run(result, checkpoint, failure)
        deferred_components = self._async_components
        try:
            if deferred_components is None:
                components = self._components.build(command, result.state)
            else:
                configured_budget = deferred_components.budget_root(command, result.state)
                if configured_budget != checkpoint.budget:
                    raise RecoveryResumeRejected(
                        "budget_config_drift",
                        result.run.run_id,
                        "Current pure root budget differs from the persisted BudgetCheckpoint.",
                    )
                durable_snapshot = self._capability_snapshot_payload(
                    capability_record,
                    result.state,
                    required=True,
                )
                assert durable_snapshot is not None
                cancellation = CancellationScope(name=f"recovery-components:{result.run.run_id}")
                prepared = await deferred_components.prepare_root(
                    command,
                    result.state,
                    cancellation,
                    durable_snapshot,
                )
                self._assert_prepared_snapshot_matches(result.state, prepared, capability_record)
                components = deferred_components.build_prepared_root(command, result.state, prepared)
        except RecoveryResumeRejected:
            await self._release_prepared_components(result.run.run_id)
            raise
        except Exception as error:
            await self._release_prepared_components(result.run.run_id)
            failure = RecoveryResumeRejected(
                "component_build_failed",
                result.run.run_id,
                f"Recovered Run components could not be prepared/built: {type(error).__name__}",
            )
            failure.__cause__ = error
            return self._failed_recovered_run(result, checkpoint, failure)
        if components.budget != checkpoint.budget:
            raise RecoveryResumeRejected(
                "budget_config_drift",
                result.run.run_id,
                "Current RunComponents budget differs from the persisted BudgetCheckpoint.",
            )
        budget = checkpoint.restore_ledger()
        try:
            await budget.enforce_wall_time(now=self._clock.utcnow())
        except BudgetExceeded as error:
            raise RecoveryResumeRejected(
                "recovery_deadline_expired",
                result.run.run_id,
                "Recovered Run has exhausted its original absolute wall-time budget.",
            ) from error
        try:
            hook_binding = self._hook_binding_for_components(components, budget, session_value)
            tool_kernel = components.tool_kernel_factory(budget)
            planner = components.planner_factory(budget)
            parsed_event = parse_persisted_domain_event(result.event.payload)
        except Exception as error:
            await self._release_prepared_components(result.run.run_id)
            failure = RecoveryResumeRejected(
                "component_factory_failed",
                result.run.run_id,
                f"Recovered Planner/ToolKernel factory failed: {type(error).__name__}",
            )
            failure.__cause__ = error
            return self._failed_recovered_run(result, checkpoint, failure)
        run_preparation = self._prepare_run_context(
            state=result.state,
            profile_id=session_value.profile_id,
            query_text=_context_query(command.input_blocks),
            effective_config=effective_config,
            planner=planner,
        )
        recorder = UowRunRecorder(
            unit_of_work=self._unit_of_work,
            event_sink=self._event_sink,
            clock=self._clock,
            ids=self._ids,
            run_id=result.run.run_id,
            trace_id=parsed_event.trace_id,
            budget=budget,
            expected_entity_revision=result.state_entity_revision,
            expected_event_sequence=result.run.event_sequence,
            expected_run_revision=result.run_entity_revision,
        )
        return _PreparedRecoveredRun(
            result=result,
            budget=budget,
            planner=planner,
            tool_kernel=tool_kernel,
            recorder=recorder,
            recovered_batch=RecoveredToolBatch(result.accepted_tool_call_ids, result.replay_calls),
            hooks=hook_binding.hooks,
            hook_context=hook_binding.context,
            run_preparation=run_preparation,
        )

    def _failed_recovered_run(
        self,
        result: RecoveryApplyResult,
        checkpoint: BudgetCheckpoint,
        error: BaseException,
    ) -> _PreparedRecoveredRun:
        """Degrade one incompatible historical Run without blocking Runtime readiness."""

        budget = checkpoint.restore_ledger()
        parsed_event = parse_persisted_domain_event(result.event.payload)
        recorder = UowRunRecorder(
            unit_of_work=self._unit_of_work,
            event_sink=self._event_sink,
            clock=self._clock,
            ids=self._ids,
            run_id=result.run.run_id,
            trace_id=parsed_event.trace_id,
            budget=budget,
            expected_entity_revision=result.state_entity_revision,
            expected_event_sequence=result.run.event_sequence,
            expected_run_revision=result.run_entity_revision,
        )
        return _PreparedRecoveredRun(
            result=result,
            budget=budget,
            planner=None,
            tool_kernel=None,
            recorder=recorder,
            recovered_batch=RecoveredToolBatch(result.accepted_tool_call_ids, result.replay_calls),
            hooks=None,
            hook_context=None,
            run_preparation=None,
            preparation_error=error,
        )

    async def _commit_prepared_components(
        self,
        state: RunState,
        recorder: UowRunRecorder,
        prepared: PreparedRunComponents,
    ) -> RunState:
        snapshot = self._prepared_snapshot_value(state, prepared)
        updated = replace(state, revision=state.revision + 1)
        await recorder.commit(
            updated,
            event_type="phase.changed",
            payload={
                "previousPhase": state.phase.value,
                "phase": state.phase.value,
                "reason": "per-Run capabilities prepared",
            },
            entity_writes=(
                AtomicEntityWrite(
                    "run_capability_snapshots",
                    state.run_id,
                    snapshot,
                    expected_revision=0,
                ),
            ),
        )
        return updated

    def _prepared_snapshot_value(
        self,
        state: RunState,
        prepared: PreparedRunComponents,
    ) -> Mapping[str, Any]:
        payload = thaw_json(prepared.durable_snapshot)
        if not isinstance(payload, Mapping):
            raise HarnessServiceError("prepared Run capability snapshot is not a JSON object")
        materialized = dict(payload)
        return {
            "schemaVersion": 1,
            "workspaceId": state.workspace_id,
            "sessionId": state.session_id,
            "turnId": state.turn_id,
            "runId": state.run_id,
            "rootRunId": state.lineage.root_run_id,
            "parentRunId": state.lineage.parent_run_id,
            "snapshotFingerprint": canonical_json_sha256(materialized),
            "snapshot": materialized,
        }

    def _capability_snapshot_payload(
        self,
        value: Any,
        state: RunState,
        *,
        required: bool,
    ) -> Mapping[str, Any] | None:
        if value is None:
            if required:
                raise HarnessServiceError("durable Run capability snapshot is missing")
            return None
        if not isinstance(value, Mapping):
            raise HarnessServiceError("durable Run capability snapshot is corrupt")
        payload = value.get("snapshot")
        if (
            value.get("schemaVersion") != 1
            or value.get("workspaceId") != state.workspace_id
            or value.get("sessionId") != state.session_id
            or value.get("turnId") != state.turn_id
            or value.get("runId") != state.run_id
            or value.get("rootRunId") != state.lineage.root_run_id
            or value.get("parentRunId") != state.lineage.parent_run_id
            or not isinstance(payload, Mapping)
            or value.get("snapshotFingerprint") != canonical_json_sha256(dict(payload))
        ):
            raise HarnessServiceError("durable Run capability snapshot identity/fingerprint is invalid")
        return dict(payload)

    def _assert_prepared_snapshot_matches(
        self,
        state: RunState,
        prepared: PreparedRunComponents,
        durable_value: Any,
    ) -> None:
        if self._prepared_snapshot_value(state, prepared) != durable_value:
            raise HarnessServiceError("prepared Run capabilities drifted from their durable snapshot")

    async def _terminalize_component_preparation_failure(
        self,
        state: RunState,
        recorder: UowRunRecorder,
        budget: BudgetLedger,
        cause: BaseException,
    ) -> RunState:
        if state.phase.terminal:
            return state
        if isinstance(cause, (OperationCancelled, asyncio.CancelledError)):
            snapshot = await budget.snapshot(now=self._clock.utcnow())
            usage = {
                "inputTokens": snapshot.used.input_tokens,
                "outputTokens": snapshot.used.output_tokens,
                "modelCalls": state.model_rounds,
                "toolCalls": state.tool_calls,
                "wallTimeMs": max(0, int(snapshot.elapsed_seconds * 1_000)),
                "costMicros": int(snapshot.used.cost * Decimal(1_000_000)),
            }
            reason = cause.reason if isinstance(cause, OperationCancelled) else None
            code = reason.code.value if reason is not None else "runtime"
            message = reason.message if reason is not None else "Run capability preparation was interrupted"
            if code == CancellationCode.DEADLINE.value:
                terminal = state.transition(RunPhase.FAILED)
                await recorder.commit(
                    terminal,
                    event_type="turn.failed",
                    payload={
                        "error": {
                            "code": ErrorCode.REQUEST_DEADLINE_EXCEEDED.value,
                            "retryable": False,
                            "cancelled": True,
                            "userVisibleMessage": message,
                            "details": {"cancellationCode": code, "failureCategory": "budget"},
                            "retryAfterMs": None,
                            "traceId": None,
                        },
                        "usage": usage,
                        "partialContent": [],
                    },
                    terminal=True,
                )
                return terminal
            should_interrupt = (
                reason is None
                or code
                in {
                    CancellationCode.SHUTDOWN.value,
                    CancellationCode.START_FAILED.value,
                }
                or (code == CancellationCode.PARENT.value and state.lineage.depth == 0)
            )
            target = RunPhase.INTERRUPTED if should_interrupt else RunPhase.CANCELLED
            if target not in ALLOWED_PHASE_TRANSITIONS[state.phase]:
                previous = state.phase
                state = state.transition(RunPhase.CANCELLING)
                await recorder.commit(
                    state,
                    event_type="phase.changed",
                    payload={
                        "previousPhase": previous.value,
                        "phase": RunPhase.CANCELLING.value,
                        "reason": "cancelled during Run capability preparation",
                    },
                )
            terminal = state.transition(target)
            if terminal.phase is RunPhase.CANCELLED:
                event_type = "turn.cancelled"
                payload: Mapping[str, Any] = {
                    "reason": message,
                    "code": code,
                    "usage": usage,
                    "partialContent": [],
                }
            else:
                event_type = "turn.interrupted"
                payload = {
                    "error": {
                        "code": ErrorCode.RUNTIME_INTERRUPTED.value,
                        "retryable": True,
                        "cancelled": True,
                        "userVisibleMessage": message,
                        "details": {"cancellationCode": code},
                        "retryAfterMs": None,
                        "traceId": None,
                    },
                    "usage": usage,
                    "partialContent": [],
                    "safeCheckpointAvailable": state.pending.empty,
                }
            await recorder.commit(terminal, event_type=event_type, payload=payload, terminal=True)
            return terminal
        terminal = state.transition(RunPhase.FAILED)
        snapshot = await budget.snapshot(now=self._clock.utcnow())
        if isinstance(cause, RunPreparationFailure):
            error_code = cause.error_code
            retryable = cause.retryable
            user_message = str(cause)
            details = {
                "errorType": type(cause).__name__,
                "failureCategory": cause.failure_category,
                "preparationErrorCode": cause.code,
                **safe_preparation_failure_details(cause),
            }
        else:
            error_code = ErrorCode.INTERNAL_ERROR
            retryable = False
            user_message = "Run capability preparation failed closed"
            details = {
                "errorType": type(cause).__name__,
                "failureCategory": "runtime",
                "preparationStage": "run_capabilities",
            }
        await recorder.commit(
            terminal,
            event_type="turn.failed",
            payload={
                "error": {
                    "code": error_code.value,
                    "retryable": retryable,
                    "cancelled": isinstance(cause, asyncio.CancelledError),
                    "userVisibleMessage": user_message,
                    "details": details,
                    "retryAfterMs": None,
                    "traceId": None,
                },
                "usage": {
                    "inputTokens": snapshot.used.input_tokens,
                    "outputTokens": snapshot.used.output_tokens,
                    "cachedInputTokens": 0,
                    "reasoningTokens": 0,
                    "modelCalls": state.model_rounds,
                    "toolCalls": state.tool_calls,
                    "costMicros": int(snapshot.used.cost * Decimal(1_000_000)),
                    "wallTimeMs": max(0, int(snapshot.elapsed_seconds * 1_000)),
                },
                "partialContent": [],
            },
            terminal=True,
        )
        return terminal

    async def _release_prepared_components(self, run_id: str) -> None:
        prepared = self._async_components
        if prepared is None:
            return
        try:
            released = prepared.release(run_id)
            if inspect.isawaitable(released):
                await released
        except Exception as error:
            self.diagnostics.cleanup_failures.append(
                f"Run capability cleanup failed for {run_id}: {type(error).__name__}: {error}"
            )

    def _prepare_run_context(
        self,
        *,
        state: RunState,
        profile_id: str,
        query_text: str,
        effective_config: HarnessConfig | None,
        planner: Planner,
        active_file: str | None = None,
    ) -> RunPreparationPort | None:
        provider = self._run_context_provider
        if provider is None or effective_config is None:
            return None
        request = RunPreparationRequest(
            profile_id=profile_id,
            workspace_id=state.workspace_id,
            session_id=state.session_id,
            turn_id=state.turn_id,
            run_id=state.run_id,
            lineage=state.lineage,
            query_text=query_text,
            memory_enabled=effective_config.memory.memory_enabled,
            active_file=active_file,
        )
        return PreparedRunContext(
            request=request,
            provider=provider,
            planner=planner,
            limits=self._run_preparation_limits,
            enricher=self._context_enricher,
        )

    def _context_for_session(self, session: Session) -> HookExecutionContext | None:
        if self._hooks is None:
            return None
        factory = self._hook_context_factory
        assert factory is not None
        context = factory(session.workspace_id, session.session_id, session.profile_id)
        if (
            context.workspace_id != session.workspace_id
            or context.session_id != session.session_id
            or context.principal_id != session.profile_id
        ):
            raise HarnessServiceError("Hook context factory returned a cross-session identity")
        return context

    def _hook_binding_for_components(
        self,
        components: RunComponents,
        budget: BudgetLedger,
        session: Session,
    ) -> RunHookBinding:
        factory = components.hook_binding_factory
        if factory is None:
            return RunHookBinding(self._hooks, self._context_for_session(session))
        binding = factory(budget)
        context = binding.context
        if context is not None and (
            context.workspace_id != session.workspace_id
            or context.session_id != session.session_id
            or context.principal_id != session.profile_id
        ):
            raise HarnessServiceError("Run Hook binding returned a cross-session identity")
        return binding

    def _validate_recovery_deadline(self, result: RecoveryApplyResult) -> None:
        checkpoint = result.state.budget_checkpoint
        deadline_at = result.run.deadline_at
        if not isinstance(checkpoint, BudgetCheckpoint) or deadline_at is None:
            raise RecoveryResumeRejected(
                "absolute_deadline_missing",
                result.run.run_id,
                "Recovered Run lacks its BudgetCheckpoint or absolute deadline.",
            )
        expected_deadline = checkpoint.started_at + timedelta(seconds=checkpoint.budget.max_wall_seconds)
        now = self._clock.utcnow()
        if result.run.created_at != checkpoint.started_at or deadline_at != expected_deadline:
            raise RecoveryResumeRejected(
                "absolute_deadline_drift",
                result.run.run_id,
                "Recovered Run deadline does not match its original budget start and wall-time limit.",
            )
        if checkpoint.captured_at > now:
            raise RecoveryResumeRejected(
                "budget_checkpoint_from_future",
                result.run.run_id,
                "Recovered BudgetCheckpoint was captured after the current Worker time.",
            )
        if now >= deadline_at:
            raise RecoveryResumeRejected(
                "recovery_deadline_expired",
                result.run.run_id,
                "Recovered Run has reached its original absolute deadline.",
            )

    async def _validate_recovery_authority(self, result: RecoveryApplyResult) -> None:
        run = result.run
        state = result.state
        turn = result.turn
        allowed_phases = {
            "validating_calls",
            "checking_policy",
            "awaiting_approval",
            "executing_tools",
            "recording_results",
        }
        if (
            run.kind is not RunKind.ROOT
            or run.status.is_terminal
            or run.termination_reason is not None
            or state.phase.value not in allowed_phases
            or turn.status is not TurnStatus.RUNNING
            or RunStatus(state.phase.value) is not run.status
        ):
            raise RecoveryResumeRejected(
                "recovery_projection_not_active",
                run.run_id,
                "Recovered Run/State/Turn is not one coherent active root Run.",
            )
        async with self._unit_of_work.begin() as uow:
            run_record = await _find_entity_record(uow.entities, "runs", run.run_id)
            state_record = await _find_entity_record(uow.entities, "run_states", run.run_id)
            turn_record = await _find_entity_record(uow.entities, "turns", turn.turn_id)
            lease_record = await _find_entity_record(uow.entities, "active_root_runs", run.session_id)
            session = await uow.entities.get("sessions", run.session_id)
            events = await uow.events.read(
                run.run_id,
                after_sequence=result.events[0].sequence - 1,
                limit=len(result.events) + 1,
            )
            latest_sequence = await uow.events.latest_sequence(run.run_id)
            terminal = await uow.events.terminal_event(run.run_id)
        if (
            run_record is None
            or run_record.revision != result.run_entity_revision
            or run_record.value != run
            or state_record is None
            or state_record.revision != result.state_entity_revision
            or state_record.value != state
            or turn_record is None
            or turn_record.revision != result.turn_entity_revision
            or turn_record.value != turn
        ):
            raise RecoveryResumeRejected(
                "recovery_authority_changed",
                run.run_id,
                "Authoritative Run, RunState, or Turn changed after recovery apply.",
            )
        if (
            not isinstance(session, Session)
            or session.workspace_id != run.workspace_id
            or session.status is not SessionStatus.ACTIVE
        ):
            raise RecoveryResumeRejected(
                "recovery_session_invalid",
                run.run_id,
                "Recovered Run no longer belongs to one active authoritative Session.",
            )
        lease = None if lease_record is None else lease_record.value
        if not isinstance(lease, Mapping) or (
            lease.get("schemaVersion") != 1
            or lease.get("workspaceId") != run.workspace_id
            or lease.get("sessionId") != run.session_id
            or lease.get("runId") != run.run_id
        ):
            raise RecoveryResumeRejected(
                "recovery_lease_invalid",
                run.run_id,
                "Recovered Run lease is missing, corrupt, or owned by another Run.",
            )
        if events != result.events or latest_sequence != run.event_sequence or terminal is not None:
            raise RecoveryResumeRejected(
                "recovery_event_cursor_changed",
                run.run_id,
                "Recovery events or authoritative event cursor changed before Loop registration.",
            )

    async def _abort_turn_start(
        self,
        ready: asyncio.Future[UowRunRecorder],
        active: ActiveRun,
        error: BaseException,
    ) -> None:
        if not ready.done():
            ready.set_exception(error)
        await active.cancellation.cancel(CancellationReason.now(CancellationCode.START_FAILED, "turn start failed"))
        await asyncio.gather(active.task, return_exceptions=True)

    async def _persist_turn_start(
        self,
        *,
        command: StartTurnCommand,
        request_hash: str,
        idempotency_id: str,
        session: Session,
        state: RunState,
        trace_id: str,
        started_at: datetime,
        deadline_at: datetime,
        budget: BudgetLedger,
    ) -> tuple[TurnReceipt, UowRunRecorder]:
        now = self._clock.utcnow()
        receipt = TurnReceipt(command.session_id, command.turn_id, state.run_id, True)
        turn = Turn(
            turn_id=command.turn_id,
            session_id=command.session_id,
            ordinal=session.revision,
            status=TurnStatus.RUNNING,
            input_blocks=command.input_blocks,
            created_at=started_at,
            updated_at=now,
        )
        run = Run(
            run_id=state.run_id,
            session_id=state.session_id,
            turn_id=state.turn_id,
            workspace_id=state.workspace_id,
            lineage=state.lineage,
            kind=RunKind.ROOT,
            status=RunStatus.STARTING,
            attempt=1,
            event_sequence=1,
            config_snapshot=command.run_config,
            created_at=started_at,
            updated_at=now,
            deadline_at=deadline_at,
        )
        updated_session = replace(session, updated_at=now, revision=session.revision + 1)
        record = make_domain_event_record(
            event_type="turn.started",
            payload={
                "input": thaw_json(command.input_blocks),
                "runConfig": thaw_json(command.run_config),
                "attempt": 1,
            },
            trace_id=trace_id,
            workspace_id=command.workspace_id,
            session_id=command.session_id,
            turn_id=command.turn_id,
            run_id=state.run_id,
            root_run_id=state.run_id,
            parent_run_id=None,
            state_revision=state.revision,
        )
        event = NewEvent(
            event_id=self._ids.new_id("evt"),
            event_type="turn.started",
            payload=record.to_wire(),
            occurred_at=started_at,
            terminal=False,
            idempotency_key=f"{state.run_id}:1:turn.started",
        )
        async with self._unit_of_work.begin() as uow:
            session_operation = await uow.entities.get(SESSION_OPERATION_COLLECTION, command.session_id)
            if session_operation is not None:
                raise SessionRunConflict(f"session {command.session_id!r} has a lifecycle operation in progress")
            existing_lease = await uow.entities.get("active_root_runs", command.session_id)
            if existing_lease is not None:
                raise SessionRunConflict(f"session {command.session_id!r} already owns an active root Run")
            try:
                await uow.entities.put(
                    "active_root_runs",
                    command.session_id,
                    {
                        "schemaVersion": 1,
                        "workspaceId": command.workspace_id,
                        "sessionId": command.session_id,
                        "runId": state.run_id,
                        "acquiredAt": started_at.isoformat(),
                    },
                    expected_revision=0,
                )
            except EntityRevisionConflict as error:
                raise SessionRunConflict(
                    f"session {command.session_id!r} concurrently acquired an active root Run"
                ) from error
            await uow.entities.put(
                "sessions",
                session.session_id,
                updated_session,
                expected_revision=session.revision,
            )
            await uow.entities.put("turns", command.turn_id, turn, expected_revision=0)
            run_revision = await uow.entities.put("runs", state.run_id, run, expected_revision=0)
            entity_revision = await uow.entities.put("run_states", state.run_id, state, expected_revision=0)
            if command.effective_config is not None:
                await uow.entities.put(
                    "run_effective_configs",
                    state.run_id,
                    {
                        "schemaVersion": 1,
                        "workspaceId": command.workspace_id,
                        "runId": state.run_id,
                        "fingerprint": command.effective_config_fingerprint,
                        "config": command.effective_config.model_dump(mode="json"),
                    },
                    expected_revision=0,
                )
            await uow.entities.put(
                "turn_idempotency",
                idempotency_id,
                _encode_receipt_record(request_hash, receipt),
                expected_revision=0,
            )
            stored = await uow.events.append(state.run_id, 0, (event,))
            await uow.commit()

        try:
            await self._event_sink.publish(stored)
        except Exception as error:
            self.diagnostics.delivery_failures.append(
                DeliveryFailure(
                    event_ids=tuple(item.event_id for item in stored),
                    error_type=type(error).__name__,
                    message=str(error),
                )
            )
        recorder = UowRunRecorder(
            unit_of_work=self._unit_of_work,
            event_sink=self._event_sink,
            clock=self._clock,
            ids=self._ids,
            run_id=state.run_id,
            trace_id=trace_id,
            budget=budget,
            expected_entity_revision=entity_revision,
            expected_event_sequence=stored[-1].sequence,
            expected_run_revision=run_revision,
        )
        return receipt, recorder

    async def _persist_turn_retry(
        self,
        *,
        command: RetryTurnCommand,
        start_command: StartTurnCommand,
        request_hash: str,
        idempotency_id: str,
        session: Session,
        turn: Turn,
        source_run: Run,
        state: RunState,
        trace_id: str,
        started_at: datetime,
        deadline_at: datetime,
        budget: BudgetLedger,
    ) -> tuple[TurnReceipt, UowRunRecorder]:
        now = self._clock.utcnow()
        receipt = TurnReceipt(command.session_id, command.turn_id, state.run_id, True)
        updated_turn = replace(
            turn,
            status=TurnStatus.RUNNING,
            updated_at=now,
            revision=turn.revision + 1,
        )
        run = Run(
            run_id=state.run_id,
            session_id=state.session_id,
            turn_id=state.turn_id,
            workspace_id=state.workspace_id,
            lineage=state.lineage,
            kind=RunKind.ROOT,
            status=RunStatus.STARTING,
            attempt=source_run.attempt + 1,
            event_sequence=1,
            config_snapshot=start_command.run_config,
            created_at=started_at,
            updated_at=now,
            deadline_at=deadline_at,
        )
        updated_session = replace(session, updated_at=now, revision=session.revision + 1)
        record = make_domain_event_record(
            event_type="turn.started",
            payload={
                "input": thaw_json(start_command.input_blocks),
                "runConfig": thaw_json(start_command.run_config),
                "attempt": run.attempt,
            },
            trace_id=trace_id,
            workspace_id=command.workspace_id,
            session_id=command.session_id,
            turn_id=command.turn_id,
            run_id=state.run_id,
            root_run_id=state.run_id,
            parent_run_id=None,
            state_revision=state.revision,
        )
        event = NewEvent(
            event_id=self._ids.new_id("evt"),
            event_type="turn.started",
            payload=record.to_wire(),
            occurred_at=started_at,
            terminal=False,
            idempotency_key=f"{state.run_id}:1:turn.started",
        )
        async with self._unit_of_work.begin() as uow:
            if await uow.entities.get(SESSION_OPERATION_COLLECTION, command.session_id) is not None:
                raise SessionRunConflict(f"session {command.session_id!r} has a lifecycle operation in progress")
            if await uow.entities.get("active_root_runs", command.session_id) is not None:
                raise SessionRunConflict(f"session {command.session_id!r} already owns an active root Run")
            await uow.entities.put(
                "active_root_runs",
                command.session_id,
                {
                    "schemaVersion": 1,
                    "workspaceId": command.workspace_id,
                    "sessionId": command.session_id,
                    "runId": state.run_id,
                    "acquiredAt": started_at.isoformat(),
                },
                expected_revision=0,
            )
            await uow.entities.put("sessions", session.session_id, updated_session, expected_revision=session.revision)
            await uow.entities.put("turns", turn.turn_id, updated_turn, expected_revision=turn.revision)
            run_revision = await uow.entities.put("runs", state.run_id, run, expected_revision=0)
            entity_revision = await uow.entities.put("run_states", state.run_id, state, expected_revision=0)
            if start_command.effective_config is not None:
                await uow.entities.put(
                    "run_effective_configs",
                    state.run_id,
                    {
                        "schemaVersion": 1,
                        "workspaceId": command.workspace_id,
                        "runId": state.run_id,
                        "fingerprint": start_command.effective_config_fingerprint,
                        "config": start_command.effective_config.model_dump(mode="json"),
                    },
                    expected_revision=0,
                )
            await uow.entities.put(
                "turn_retry_idempotency",
                idempotency_id,
                _encode_receipt_record(request_hash, receipt),
                expected_revision=0,
            )
            stored = await uow.events.append(state.run_id, 0, (event,))
            await uow.commit()
        try:
            await self._event_sink.publish(stored)
        except Exception as error:
            self.diagnostics.delivery_failures.append(
                DeliveryFailure(
                    event_ids=tuple(item.event_id for item in stored),
                    error_type=type(error).__name__,
                    message=str(error),
                )
            )
        recorder = UowRunRecorder(
            unit_of_work=self._unit_of_work,
            event_sink=self._event_sink,
            clock=self._clock,
            ids=self._ids,
            run_id=state.run_id,
            trace_id=trace_id,
            budget=budget,
            expected_entity_revision=entity_revision,
            expected_event_sequence=stored[-1].sequence,
            expected_run_revision=run_revision,
        )
        return receipt, recorder

    async def cancel_turn(self, run_id: str, *, reason: str = "cancelled by user") -> bool:
        return await self._turn_manager.cancel(
            run_id,
            CancellationReason.now(CancellationCode.USER, reason),
        )

    def _register_root_cancellation(self, active: ActiveRun) -> None:
        if self._root_cancellations is None:
            return
        self._root_cancellations.register_root(active.run_id, active.cancellation)
        registry = self._root_cancellations
        active.task.add_done_callback(lambda _task: registry.unregister_root(active.run_id))

    async def resolve_approval(self, resolution: ApprovalResolution) -> ApprovalResolution:
        return await self._approval_manager.resolve(resolution)

    @property
    def approvals(self) -> ApprovalManager:
        return self._approval_manager

    @property
    def sessions(self) -> SessionLifecycleService:
        return self._session_service

    async def list_sessions(self, command: SessionListCommand) -> SessionListResult:
        return await self._session_service.list(command)

    async def get_session(self, command: SessionGetCommand) -> SessionGetResult:
        return await self._session_service.get(command)

    async def rename_session(self, command: SessionRenameCommand) -> SessionRenameResult:
        return await self._session_service.rename(command)

    async def delete_session(self, command: SessionDeleteCommand) -> SessionDeleteResult:
        return await self._session_service.soft_delete(command)

    async def fork_session(self, command: SessionForkCommand) -> SessionForkResult:
        return await self._session_service.fork(command)

    async def replay_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        async with self._unit_of_work.begin() as uow:
            return await uow.events.read(run_id, after_sequence=after_sequence, limit=limit)

    async def replay_run_event_page(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
        event_types: frozenset[str] = frozenset(),
    ) -> RunEventReplayPage:
        _RUN_ID.validate_python(run_id, strict=True)
        if after_sequence < 0 or not 1 <= limit <= 10_000:
            raise ValueError("invalid Run event replay cursor or limit")
        async with self._unit_of_work.begin() as uow:
            run = await uow.entities.get("runs", run_id)
            if not isinstance(run, Run):
                raise EntityNotFound(f"run {run_id!r} does not exist")
            selected: list[StoredEvent] = []
            scan_cursor = after_sequence
            exhausted = False
            while len(selected) <= limit:
                page = await uow.events.read(run_id, after_sequence=scan_cursor, limit=100)
                if not page:
                    exhausted = True
                    break
                for event in page:
                    scan_cursor = event.sequence
                    if not event_types or event.event_type in event_types:
                        selected.append(event)
                        if len(selected) > limit:
                            break
                if len(selected) > limit:
                    break
                if len(page) < 100:
                    exhausted = True
                    break
            returned = tuple(selected[:limit])
            last_sequence = scan_cursor if exhausted else returned[-1].sequence
            return RunEventReplayPage(returned, last_sequence, not exhausted)

    async def replay_session_events(
        self,
        session_id: str,
        *,
        run_cursors: Mapping[str, int],
        limit: int = 1000,
        event_types: frozenset[str] = frozenset(),
    ) -> SessionEventReplayPage:
        """Merge Session Run streams without inventing a Session sequence.

        Every returned/filtered event advances only its owning Run cursor.  A
        stable ``(timestamp, runId, sequence)`` order makes each page
        deterministic while the cursor map preserves unambiguous resume state.
        """

        _SESSION_ID.validate_python(session_id, strict=True)
        if not 1 <= limit <= 10_000:
            raise ValueError("event replay limit must be between 1 and 10000")
        if any(sequence < 0 for sequence in run_cursors.values()):
            raise ValueError("event replay cursors cannot be negative")

        async with self._unit_of_work.begin() as uow:
            session = await uow.entities.get("sessions", session_id)
            if not isinstance(session, Session):
                raise EntityNotFound(f"session {session_id!r} does not exist")
            run_ids: list[str] = []
            after_id: str | None = None
            while True:
                page = await uow.entities.list("runs", after_id=after_id, limit=1000)
                if not page:
                    break
                run_ids.extend(
                    record.entity_id
                    for record in page
                    if isinstance(record.value, Run) and record.value.session_id == session_id
                )
                after_id = page[-1].entity_id

            known = frozenset(run_ids)
            unknown = frozenset(run_cursors) - known
            if unknown:
                raise SessionRunConflict(
                    f"session replay cursors reference Runs outside the Session: {sorted(unknown)!r}"
                )
            cursors = {run_id: run_cursors.get(run_id, 0) for run_id in sorted(known)}
            pending: list[tuple[datetime, str, int, StoredEvent]] = []

            async def queue_next(run_id: str, after_sequence: int) -> None:
                scan_cursor = after_sequence
                while True:
                    page = await uow.events.read(run_id, after_sequence=scan_cursor, limit=100)
                    if not page:
                        cursors[run_id] = scan_cursor
                        return
                    for event in page:
                        if not event_types or event.event_type in event_types:
                            cursors[run_id] = scan_cursor
                            heapq.heappush(
                                pending,
                                (event.occurred_at, run_id, event.sequence, event),
                            )
                            return
                        scan_cursor = event.sequence
                    cursors[run_id] = scan_cursor
                    if len(page) < 100:
                        return

            for run_id in sorted(known):
                await queue_next(run_id, cursors[run_id])

            selected: list[StoredEvent] = []
            while pending and len(selected) < limit:
                _timestamp, run_id, _sequence, event = heapq.heappop(pending)
                selected.append(event)
                cursors[run_id] = event.sequence
                await queue_next(run_id, event.sequence)

            return SessionEventReplayPage(tuple(selected), cursors, bool(pending))

    async def get_run_state(self, run_id: str) -> RunState:
        async with self._unit_of_work.begin() as uow:
            state = await uow.entities.get("run_states", run_id)
        if not isinstance(state, RunState):
            raise EntityNotFound(f"run {run_id!r} does not exist")
        return state

    async def get_run(self, run_id: str) -> Run:
        async with self._unit_of_work.begin() as uow:
            run = await uow.entities.get("runs", run_id)
        if not isinstance(run, Run):
            raise EntityNotFound(f"run {run_id!r} does not exist")
        return run

    async def get_turn(self, turn_id: str) -> Turn:
        async with self._unit_of_work.begin() as uow:
            turn = await uow.entities.get("turns", turn_id)
        if not isinstance(turn, Turn):
            raise EntityNotFound(f"turn {turn_id!r} does not exist")
        return turn

    async def shutdown(self, *, grace_seconds: float = 10.0) -> None:
        await self._turn_manager.shutdown(grace_seconds=grace_seconds)


async def _find_entity_record(store: EntityStore, collection: str, entity_id: str) -> EntityRecord | None:
    after_id: str | None = None
    while True:
        page = await store.list(collection, after_id=after_id, limit=100)
        if not page:
            return None
        for record in page:
            if record.entity_id == entity_id:
                return record
            if record.entity_id > entity_id:
                return None
        after_id = page[-1].entity_id


def _encode_receipt_record(
    request_hash: str,
    receipt: SessionReceipt | TurnReceipt,
) -> dict[str, Any]:
    if isinstance(receipt, SessionReceipt):
        kind = "session"
        value: dict[str, Any] = {
            "sessionId": receipt.session_id,
            "workspaceId": receipt.workspace_id,
            "created": receipt.created,
        }
    else:
        kind = "turn"
        value = {
            "sessionId": receipt.session_id,
            "turnId": receipt.turn_id,
            "runId": receipt.run_id,
            "accepted": receipt.accepted,
        }
    return {
        "schemaVersion": 1,
        "requestHash": request_hash,
        "kind": kind,
        "receipt": value,
    }


def _decode_receipt_record(
    value: Any,
    *,
    expected_kind: str,
) -> tuple[str, SessionReceipt | TurnReceipt]:
    if not isinstance(value, Mapping) or set(value) != {"schemaVersion", "requestHash", "kind", "receipt"}:
        raise HarnessServiceError("idempotency receipt envelope is corrupt")
    if value["schemaVersion"] != 1 or value["kind"] != expected_kind:
        raise HarnessServiceError("idempotency receipt schema or kind is incompatible")
    request_hash = value["requestHash"]
    raw_receipt = value["receipt"]
    if not isinstance(request_hash, str) or not isinstance(raw_receipt, Mapping):
        raise HarnessServiceError("idempotency receipt fields are corrupt")
    if expected_kind == "session":
        if set(raw_receipt) != {"sessionId", "workspaceId", "created"}:
            raise HarnessServiceError("session receipt is corrupt")
        session_id = raw_receipt["sessionId"]
        workspace_id = raw_receipt["workspaceId"]
        created = raw_receipt["created"]
        if not isinstance(session_id, str) or not isinstance(workspace_id, str) or not isinstance(created, bool):
            raise HarnessServiceError("session receipt field types are corrupt")
        return request_hash, SessionReceipt(session_id, workspace_id, created)
    if expected_kind == "turn":
        if set(raw_receipt) != {"sessionId", "turnId", "runId", "accepted"}:
            raise HarnessServiceError("turn receipt is corrupt")
        session_id = raw_receipt["sessionId"]
        turn_id = raw_receipt["turnId"]
        run_id = raw_receipt["runId"]
        accepted = raw_receipt["accepted"]
        if not all(isinstance(item, str) for item in (session_id, turn_id, run_id)) or not isinstance(accepted, bool):
            raise HarnessServiceError("turn receipt field types are corrupt")
        return request_hash, TurnReceipt(session_id, turn_id, run_id, accepted)
    raise HarnessServiceError(f"unsupported receipt kind {expected_kind!r}")


def _subagent_run_budget(
    execution: ChildRunExecution,
    *,
    parent_max_parallel_reads: int,
) -> RunBudget:
    value = execution.record.budget_limit
    config = validate_wire(RunConfigSnapshot, thaw_json(execution.run_config))
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


def _context_query(value: Any) -> str:
    return canonical_json_bytes(thaw_json(value)).decode("utf-8")


def _explicit_instruction_scope(blocks: Sequence[Mapping[str, Any]]) -> str | None:
    """Return the sole file explicitly attached to a Turn for path-scoped rules.

    An editor's active tab is never task context.  Scope can change only when
    the caller deliberately sends one typed ``file`` content block.  Multiple
    attachments have no singular scope and therefore use root instructions.
    """

    paths: set[str] = set()
    for block in blocks:
        raw = thaw_json(block)
        if not isinstance(raw, Mapping) or raw.get("type") != "file":
            continue
        file = raw.get("file")
        path = file.get("path") if isinstance(file, Mapping) else None
        if not isinstance(path, str):
            raise TypeError("validated file content block path must remain text")
        paths.add(path)
    return next(iter(paths)) if len(paths) == 1 else None


def _effective_config_from_record(
    value: Any,
    *,
    workspace_id: str,
    run_id: str,
) -> HarnessConfig | None:
    if value is None:
        return None
    if (
        not isinstance(value, Mapping)
        or value.get("schemaVersion") != 1
        or value.get("workspaceId") != workspace_id
        or value.get("runId") != run_id
        or not isinstance(value.get("config"), Mapping)
    ):
        raise HarnessServiceError("Run effective config is corrupt or belongs to another Run")
    return HarnessConfig.model_validate(dict(cast(Mapping[str, Any], value["config"])))


__all__ = [
    "AsyncRunComponentsPreparationPort",
    "ChildRunComponentsFactory",
    "CreateSessionCommand",
    "EntityNotFound",
    "HarnessService",
    "HarnessServiceError",
    "HookBindingFactory",
    "HookContextFactory",
    "IdempotencyKeyConflict",
    "PlannerFactory",
    "PreparedRunComponents",
    "RecoveryResumeRejected",
    "RetryTurnCommand",
    "RunComponents",
    "RunComponentsFactory",
    "RunEventReplayPage",
    "RunHookBinding",
    "RuntimeLifecycleHookProvider",
    "SessionEventReplayPage",
    "SessionReceipt",
    "SessionRunConflict",
    "StartTurnCommand",
    "ToolKernelFactory",
    "TurnReceipt",
]
