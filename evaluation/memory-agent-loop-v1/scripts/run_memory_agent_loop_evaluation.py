"""Evaluate durable personal memory through the canonical production Agent Loop."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar, cast

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.agent.budget_checkpoint import BudgetCheckpoint
from offeragent_harness.agent.budgets import BudgetLedger, RunBudget
from offeragent_harness.agent.context_manager import (
    ContextBudget,
    ContextFragment,
    ContextInputs,
    ContextLayer,
    ContextManager,
    ContextVisibilityPolicy,
)
from offeragent_harness.agent.loop import AgentLoopFailure, run_agent_loop
from offeragent_harness.agent.model_planner import (
    AgentStepCatalog,
    ModelPlanner,
    PlannerModelConfig,
)
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.agent.system_rules import offeragent_system_rules
from offeragent_harness.foundation import NetworkAuditRecord
from offeragent_harness.memory import (
    MEMORY_ITEMS_COLLECTION,
    MemoryRepository,
    MemoryToolExecutor,
    memory_item_from_json,
)
from offeragent_harness.models import thaw_json
from offeragent_harness.permissions import (
    ApprovalDecisionReceipt,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalScope,
    ApprovalState,
    CapabilityScope,
    PermissionMode,
    PolicyContext,
    RiskClass,
)
from offeragent_harness.permissions.audit import NullPolicyAuditSink
from offeragent_harness.permissions.evaluator import RuleBasedPolicyEvaluator
from offeragent_harness.ports import ApprovalObserver, CancellationToken, Sensitivity
from offeragent_harness.ports.secrets import SecretHandle, SecretKind
from offeragent_harness.providers.deepseek_chat import (
    DEEPSEEK_BASE_URL,
    build_deepseek_gateway,
)
from offeragent_harness.providers.openai_responses import StaticModelEndpointPolicy
from offeragent_harness.runtime import CancellationScope
from offeragent_harness.runtime.memory_preparation import (
    StructuredMemoryRunPreparationAdapter,
)
from offeragent_harness.runtime.production_worker_composition import (
    SecureIdGenerator,
    SystemClock,
)
from offeragent_harness.runtime.run_preparation import (
    ConversationHistoryRunPreparationAdapter,
    RunPreparationRequest,
)
from offeragent_harness.sessions import (
    AgentLineage,
    Run,
    RunKind,
    RunStatus,
    Session,
    SessionStatus,
    TerminationReason,
    Turn,
    TurnStatus,
)
from offeragent_harness.skills import (
    SkillAuthority,
    SkillAuthorityProvider,
    SkillCatalog,
    SkillLayer,
    SkillRoot,
    SkillToolExecutor,
    skill_tool_definitions,
)
from offeragent_harness.tools import (
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolValidator,
    canonical_json_bytes,
)
from offeragent_harness.tools.dispatcher import ToolDispatcher
from offeragent_harness.tools.kernel import UnifiedToolKernel
from offeragent_harness.tools.registry import ToolRegistry
from offeragent_harness.tools.scheduler import ToolScheduler

T = TypeVar("T")
WORKSPACE_ID = "ws-offeragent-memory-agent-loop-eval-v1"
SECRET_HANDLE = SecretHandle("secret:v1:" + "2" * 32)
DEFAULT_MODEL = "deepseek-v4-flash"
_CITATION = re.compile(
    r"\[memory:(memory_[A-Za-z0-9_-]+);\s*source:([^/\]]+)/([^\]]+)\]"
)


@dataclass(frozen=True, slots=True)
class TurnSpec:
    turn_id: str
    profile_alias: str
    session_alias: str
    prompt: str
    expected: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    scenario_id: str
    run_index: int
    turns: tuple[TurnSpec, ...]

    @property
    def key(self) -> str:
        return f"{self.scenario_id}:{self.run_index}"


class FileSecretResolver:
    """Process-local resolver that cannot serialize or print the API key."""

    def __init__(self, path: Path) -> None:
        value = bytearray(path.read_bytes().strip())
        if not value or b"\x00" in value:
            _zero(value)
            raise ValueError("secret file is empty or invalid")
        self._value = value
        self._closed = False

    def consume(
        self,
        handle: SecretHandle,
        *,
        scope_id: str,
        expected_kind: SecretKind,
        expected_provider_id: str,
        consumer: Callable[[memoryview], T],
    ) -> T:
        if self._closed:
            raise RuntimeError("secret resolver is closed")
        if (
            handle != SECRET_HANDLE
            or scope_id != WORKSPACE_ID
            or expected_kind is not SecretKind.MODEL_PROVIDER
            or expected_provider_id != "deepseek"
        ):
            raise ValueError("secret binding mismatch")
        return consumer(memoryview(self._value))

    def appears_in(self, payload: bytes) -> bool:
        return bool(self._value and bytes(self._value) in payload)

    def close(self) -> None:
        _zero(self._value)
        self._value.clear()
        self._closed = True

    def __repr__(self) -> str:
        return "<FileSecretResolver redacted>"


class StaticSkillAuthority(SkillAuthorityProvider):
    def __init__(self, authority: SkillAuthority) -> None:
        self._authority = authority

    async def authority_for(self, call: ToolCall) -> SkillAuthority:
        if call.workspace_id != WORKSPACE_ID:
            raise ValueError("Skill call crossed the evaluation Workspace boundary")
        return self._authority


class LocalExecutorRouter:
    def __init__(self, routes: Sequence[tuple[Sequence[ToolDefinition], Any]]) -> None:
        self._routes = {
            (definition.name, definition.version): executor
            for definitions, executor in routes
            for definition in definitions
        }

    async def execute(
        self, call: ToolCall, cancellation: CancellationToken
    ) -> ToolResult:
        executor = self._routes.get((call.name, call.version))
        if executor is None:
            raise ValueError(f"no local executor for {call.name}@{call.version}")
        return await executor.execute(call, cancellation)


class EvaluationApprovalPort:
    """Issue one-time approvals inside the isolated synthetic-memory benchmark."""

    def __init__(self, clock: SystemClock) -> None:
        self._clock = clock
        self.requests: list[ApprovalRequest] = []

    async def request(
        self,
        approval: ApprovalRequest,
        cancellation: CancellationToken,
        observer: ApprovalObserver | None = None,
    ) -> ApprovalDecisionReceipt:
        cancellation.checkpoint()
        self.requests.append(approval)
        if observer is not None:
            await observer.required(approval)
        resolution = ApprovalResolution(
            approval_id=approval.approval_id,
            state=ApprovalState.APPROVED,
            scope=ApprovalScope.ONCE,
            resolved_at=self._clock.utcnow(),
            resolver_id="memory-evaluation:synthetic-user",
            include_descendants=False,
            reason="isolated benchmark write authorized for this ToolCall only",
        )
        if observer is not None:
            await observer.resolved(approval, resolution)
        return ApprovalDecisionReceipt(approval, resolution)

    async def cancel(self, approval_id: str, reason: str) -> None:
        del approval_id, reason

    async def pending(self, approval_id: str) -> ApprovalRequest | None:
        del approval_id
        return None


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def commit(
        self,
        state: RunState,
        *,
        event_type: str,
        payload: Mapping[str, object],
        terminal: bool = False,
    ) -> None:
        value = thaw_json(payload)
        if not isinstance(value, dict):
            raise TypeError("Agent Loop event payload must be an object")
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "eventType": event_type,
                "payload": value,
                "terminal": terminal,
                "state": {
                    "phase": state.phase.value,
                    "revision": state.revision,
                    "modelRounds": state.model_rounds,
                    "toolCalls": state.tool_calls,
                },
            }
        )


class EvaluationNetworkAuditSink:
    def __init__(self) -> None:
        self._records: dict[str, list[NetworkAuditRecord]] = {}

    async def record(self, audit: NetworkAuditRecord) -> None:
        if audit.run_id is None:
            raise ValueError("memory evaluation network audit requires a Run identity")
        self._records.setdefault(audit.run_id, []).append(audit)

    def consume(self, run_id: str) -> dict[str, Any]:
        records = self._records.pop(run_id, [])
        results = [record for record in records if record.stage == "result"]
        outcomes: dict[str, int] = {}
        for record in results:
            outcomes[record.outcome] = outcomes.get(record.outcome, 0) + 1
        return {
            "logicalRequestCount": len({record.operation_id for record in results}),
            "networkAttemptCount": len(results),
            "networkRetryCount": sum(record.attempt > 1 for record in results),
            "networkOutcomeCounts": dict(sorted(outcomes.items())),
        }


@dataclass(slots=True)
class EvaluationRuntime:
    unit_of_work: SqliteUnitOfWorkFactory
    repository: MemoryRepository
    memory_executor: MemoryToolExecutor
    skill_executor: SkillToolExecutor
    definitions: tuple[ToolDefinition, ...]
    skill_prompt: ContextFragment
    gateway: Any
    network_audit: EvaluationNetworkAuditSink
    clock: SystemClock
    ids: SecureIdGenerator
    model: str
    reasoning_effort: str
    config_fingerprint: str

    async def run_scenario(self, spec: ScenarioSpec) -> list[dict[str, Any]]:
        ordinals: dict[tuple[str, str], int] = {}
        records: list[dict[str, Any]] = []
        for turn in spec.turns:
            profile_id = _stable_id("profile", spec.key, turn.profile_alias)
            session_id = _stable_id(
                "session", spec.key, turn.profile_alias, turn.session_alias
            )
            identity = (profile_id, session_id)
            ordinal = ordinals.get(identity, 0) + 1
            ordinals[identity] = ordinal
            records.append(
                await self.run_turn(spec, turn, profile_id, session_id, ordinal)
            )
        return records

    async def run_turn(
        self,
        scenario: ScenarioSpec,
        spec: TurnSpec,
        profile_id: str,
        session_id: str,
        ordinal: int,
    ) -> dict[str, Any]:
        turn_id = self.ids.new_id("turn")
        run_id = self.ids.new_id("run")
        lineage = AgentLineage.root(run_id)
        started_at = self.clock.utcnow()
        await self._seed_run(
            profile_id=profile_id,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            lineage=lineage,
            ordinal=ordinal,
            prompt=spec.prompt,
            now=started_at,
        )
        budget = BudgetLedger(
            RunBudget(
                max_model_rounds=16,
                max_tool_calls=32,
                max_parallel_reads=4,
                max_wall_seconds=240,
                max_input_tokens=200_000,
                max_output_tokens=32_000,
                max_cost=Decimal("50"),
                max_artifact_bytes=1,
                max_subagents=1,
            ),
            started_at=started_at,
        )
        state = replace(
            RunState(WORKSPACE_ID, session_id, turn_id, run_id, lineage),
            budget_checkpoint=await BudgetCheckpoint.capture(budget, now=started_at),
        )
        cancellation = CancellationScope(
            name=f"memory-eval-{scenario.key}-{spec.turn_id}"
        )
        request = RunPreparationRequest(
            profile_id=profile_id,
            workspace_id=WORKSPACE_ID,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            lineage=lineage,
            query_text=spec.prompt,
            memory_enabled=True,
        )
        history = await ConversationHistoryRunPreparationAdapter(
            workspace_id=WORKSPACE_ID,
            unit_of_work=self.unit_of_work,
        ).context_fragments(request, RunPhase.LOADING_CONTEXT, cancellation)
        memories = await StructuredMemoryRunPreparationAdapter(
            workspace_id=WORKSPACE_ID,
            repository=self.repository,
        ).context_fragments(request, RunPhase.SELECTING_MEMORY, cancellation)
        context = ContextManager(
            system_rules=offeragent_system_rules(),
            inputs=ContextInputs(
                user_input=(
                    ContextFragment(
                        fragment_id=f"evaluation:{scenario.key}:{spec.turn_id}",
                        layer=ContextLayer.USER_INPUT,
                        text=spec.prompt,
                        sensitivity=Sensitivity.PUBLIC,
                    ),
                ),
                conversation=history,
                memories=memories,
                skills=(self.skill_prompt,),
            ),
            visibility=ContextVisibilityPolicy.cloud_model(),
            budget=ContextBudget.generous_default(),
            local_timezone=timezone.utc,
        )
        planner = ModelPlanner(
            gateway=self.gateway,
            context_manager=context,
            catalog=AgentStepCatalog(self.definitions, max_calls=12),
            config=PlannerModelConfig(
                model=self.model,
                max_output_tokens=4096,
                reasoning_effort=self.reasoning_effort,
                temperature=0,
            ),
            clock=self.clock,
            ids=self.ids,
            budget=budget,
        )
        scope = CapabilityScope(
            allowed_tools=frozenset(definition.name for definition in self.definitions),
            denied_tools=frozenset(),
            allowed_risks=frozenset({RiskClass.READ, RiskClass.WRITE}),
            root_capabilities=frozenset(
                capability
                for definition in self.definitions
                for capability in definition.required_capabilities
            ),
            allow_network=False,
            allow_secret_handles=False,
        )

        def policy_context(call: ToolCall) -> PolicyContext:
            return PolicyContext(
                workspace_id=WORKSPACE_ID,
                session_id=session_id,
                principal_id=profile_id,
                run_id=run_id,
                permission_mode=PermissionMode.NORMAL,
                effective_scope=scope,
                workspace_trusted=True,
                now=self.clock.utcnow(),
            )

        router = LocalExecutorRouter(
            (
                (self.memory_executor.definitions, self.memory_executor),
                (self.skill_executor.definitions, self.skill_executor),
            )
        )
        kernel = UnifiedToolKernel(
            registry=ToolRegistry(f"memory-evaluation-{run_id}", self.definitions),
            validator=ToolValidator(),
            policy=RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink()),
            policy_context=policy_context,
            scheduler=ToolScheduler(clock=self.clock, max_parallel_reads=4),
            dispatcher=ToolDispatcher(local=router),
            journal=self.unit_of_work.invocation_journal,
            clock=self.clock,
            ids=self.ids,
            approvals=EvaluationApprovalPort(self.clock),
        )
        recorder = EventRecorder()
        started = time.perf_counter()
        final_state: RunState = state
        error: dict[str, str] | None = None
        try:
            final_state = await asyncio.wait_for(
                run_agent_loop(
                    state,
                    planner=planner,
                    tool_kernel=kernel,
                    recorder=recorder,
                    budget=budget,
                    cancellation=cancellation,
                    now=self.clock.utcnow,
                ),
                timeout=270,
            )
        except AgentLoopFailure as failure:
            final_state = failure.state
            cause = failure.__cause__ or failure
            error = {"type": type(cause).__name__, "message": str(cause)}
        except Exception as failure:
            error = {"type": type(failure).__name__, "message": str(failure)}
        finally:
            await cancellation.close()
        elapsed = time.perf_counter() - started
        await self._complete_run(
            turn_id=turn_id,
            run_id=run_id,
            final_state=final_state,
            completed=final_state.phase is RunPhase.COMPLETED,
        )
        metrics = _score_turn(
            spec,
            recorder.events,
            final_state.assistant_text,
            elapsed,
            self.network_audit.consume(run_id),
        )
        return {
            "schemaVersion": 1,
            "key": f"{scenario.key}:{spec.turn_id}",
            "scenarioId": scenario.scenario_id,
            "scenarioRunIndex": scenario.run_index,
            "turnId": spec.turn_id,
            "profileAlias": spec.profile_alias,
            "sessionAlias": spec.session_alias,
            "prompt": spec.prompt,
            "assistantText": final_state.assistant_text,
            "error": error,
            "events": recorder.events,
            "metrics": metrics,
            "configFingerprint": self.config_fingerprint,
        }

    async def _seed_run(
        self,
        *,
        profile_id: str,
        session_id: str,
        turn_id: str,
        run_id: str,
        lineage: AgentLineage,
        ordinal: int,
        prompt: str,
        now: Any,
    ) -> None:
        async with self.unit_of_work.begin() as uow:
            session = await uow.entities.get("sessions", session_id)
            if session is None:
                await uow.entities.put(
                    "sessions",
                    session_id,
                    Session(
                        session_id,
                        WORKSPACE_ID,
                        profile_id,
                        "Memory evaluation",
                        SessionStatus.ACTIVE,
                        now,
                        now,
                        1,
                    ),
                    expected_revision=0,
                )
            elif not isinstance(session, Session) or session.profile_id != profile_id:
                raise ValueError("evaluation Session identity drifted")
            await uow.entities.put(
                "turns",
                turn_id,
                Turn(
                    turn_id,
                    session_id,
                    ordinal,
                    TurnStatus.RUNNING,
                    ({"type": "text", "text": prompt},),
                    now,
                    now,
                ),
                expected_revision=0,
            )
            await uow.entities.put(
                "runs",
                run_id,
                Run(
                    run_id,
                    session_id,
                    turn_id,
                    WORKSPACE_ID,
                    lineage,
                    RunKind.ROOT,
                    RunStatus.PLANNING,
                    1,
                    0,
                    {"evaluation": "memory-agent-loop-v1"},
                    now,
                    now,
                    None,
                ),
                expected_revision=0,
            )
            await uow.commit()

    async def _complete_run(
        self,
        *,
        turn_id: str,
        run_id: str,
        final_state: RunState,
        completed: bool,
    ) -> None:
        now = self.clock.utcnow()
        async with self.unit_of_work.begin() as uow:
            turn = await uow.entities.get("turns", turn_id)
            run = await uow.entities.get("runs", run_id)
            if not isinstance(turn, Turn) or not isinstance(run, Run):
                raise ValueError("evaluation Run persistence is missing")
            await uow.entities.put(
                "turns",
                turn_id,
                replace(
                    turn,
                    status=TurnStatus.COMPLETED if completed else TurnStatus.FAILED,
                    updated_at=now,
                    revision=turn.revision + 1,
                ),
                expected_revision=1,
            )
            await uow.entities.put(
                "runs",
                run_id,
                replace(
                    run,
                    status=RunStatus.COMPLETED if completed else RunStatus.FAILED,
                    updated_at=now,
                    termination_reason=(
                        TerminationReason.COMPLETED
                        if completed
                        else TerminationReason.MODEL_ERROR
                    ),
                ),
                expected_revision=1,
            )
            await uow.entities.put(
                "run_states", run_id, final_state, expected_revision=0
            )
            await uow.commit()


async def build_runtime(
    *,
    repository_root: Path,
    scenarios_path: Path,
    output: Path,
    resolver: FileSecretResolver,
    model: str,
    reasoning_effort: str,
) -> EvaluationRuntime:
    skill_root = (
        repository_root
        / "packages"
        / "offeragent-harness"
        / "packaging"
        / "runtime-skills"
    )
    catalog = SkillCatalog(
        workspace_id=WORKSPACE_ID,
        roots=(
            SkillRoot(
                WORKSPACE_ID,
                "runtime-builtin",
                SkillLayer.BUILTIN,
                skill_root.resolve(strict=True),
                workspace_trusted=True,
            ),
        ),
    )
    setup = CancellationScope(name="memory-evaluation-setup")
    try:
        reload_result = await catalog.initialize(setup)
    finally:
        await setup.close()
    if reload_result.partial:
        raise RuntimeError("runtime Skill catalog could not be loaded completely")
    descriptor = next(
        (
            item
            for item in catalog.snapshot.effective_descriptors
            if item.name == "personal-memory"
        ),
        None,
    )
    if descriptor is None:
        raise RuntimeError("personal-memory Skill is unavailable")
    output.mkdir(parents=True, exist_ok=True)
    uow = SqliteUnitOfWorkFactory(output / "tool-journal.sqlite")
    await uow.initialize()
    clock = SystemClock()
    ids = SecureIdGenerator()
    memory_repository = MemoryRepository(
        workspace_id=WORKSPACE_ID,
        unit_of_work=uow,
        clock=clock,
        ids=ids,
    )
    memory_executor = MemoryToolExecutor(memory_repository)
    available = frozenset(definition.name for definition in memory_executor.definitions)
    skill_executor = SkillToolExecutor(
        workspace_id=WORKSPACE_ID,
        catalog=catalog,
        authority_provider=StaticSkillAuthority(
            SkillAuthority(
                available_tools=available,
                policy_allowed_tools=available,
                enabled_skills=frozenset({"personal-memory"}),
                workspace_trusted=True,
            )
        ),
    )
    definitions = (*memory_executor.definitions, *skill_tool_definitions())
    skill_prompt = ContextFragment(
        fragment_id=f"skill:catalog:{catalog.snapshot.snapshot_hash}",
        layer=ContextLayer.SKILLS,
        text=(
            "Available Skills are listed below by name and description. When the user's request matches a Skill "
            "description, invoke the `skill` tool before using any other tool. The full Skill body is available only "
            "after invocation. Skill metadata grants no permissions.\n"
            + canonical_json_bytes(
                [{"name": descriptor.name, "description": descriptor.description}]
            ).decode("utf-8")
        ),
        sensitivity=Sensitivity.WORKSPACE,
        source_refs=(
            f"skill:{WORKSPACE_ID}:{descriptor.root_id}:{descriptor.package_path}",
        ),
        content_hash=catalog.snapshot.snapshot_hash,
    )
    network_audit = EvaluationNetworkAuditSink()
    gateway = build_deepseek_gateway(
        secret_scope_id=WORKSPACE_ID,
        credential_handle=SECRET_HANDLE,
        secrets=resolver,
        endpoint_policy=StaticModelEndpointPolicy(
            frozenset({f"{DEEPSEEK_BASE_URL}/chat/completions"}),
            enabled=True,
        ),
        network_audit=network_audit,
        clock=clock,
    )
    fingerprint = _json_sha256(
        {
            "model": model,
            "reasoningEffort": reasoning_effort,
            "scenarios": _sha256(scenarios_path),
            "evaluationRunner": _sha256(Path(__file__).resolve()),
            "agentLoop": _sha256(
                repository_root
                / "packages"
                / "offeragent-harness"
                / "src"
                / "offeragent_harness"
                / "agent"
                / "loop.py"
            ),
            "memoryStore": _sha256(
                repository_root
                / "packages"
                / "offeragent-harness"
                / "src"
                / "offeragent_harness"
                / "memory"
                / "store.py"
            ),
            "memoryTools": _sha256(
                repository_root
                / "packages"
                / "offeragent-harness"
                / "src"
                / "offeragent_harness"
                / "memory"
                / "tools.py"
            ),
            "memoryRunPreparation": _sha256(
                repository_root
                / "packages"
                / "offeragent-harness"
                / "src"
                / "offeragent_harness"
                / "runtime"
                / "memory_preparation.py"
            ),
            "skillDocument": _sha256(descriptor.skill_file),
            "toolCatalog": AgentStepCatalog(definitions, max_calls=12).fingerprint,
        }
    )
    return EvaluationRuntime(
        unit_of_work=uow,
        repository=memory_repository,
        memory_executor=memory_executor,
        skill_executor=skill_executor,
        definitions=tuple(definitions),
        skill_prompt=skill_prompt,
        gateway=gateway,
        network_audit=network_audit,
        clock=clock,
        ids=ids,
        model=model,
        reasoning_effort=reasoning_effort,
        config_fingerprint=fingerprint,
    )


def _score_turn(
    spec: TurnSpec,
    events: Sequence[Mapping[str, Any]],
    assistant_text: str,
    elapsed: float,
    network: Mapping[str, Any],
) -> dict[str, Any]:
    accepted: list[Mapping[str, Any]] = []
    batches: list[tuple[tuple[str, str], ...]] = []
    started_ids: set[str] = set()
    results: list[Mapping[str, Any]] = []
    for event in events:
        payload = cast(Mapping[str, Any], event["payload"])
        if event["eventType"] == "tool.calls.accepted":
            calls = [
                cast(Mapping[str, Any], value) for value in payload.get("calls", [])
            ]
            accepted.extend(calls)
            batches.append(
                tuple((str(call["name"]), str(call["argsHash"])) for call in calls)
            )
        elif event["eventType"] == "tool.started":
            call = cast(Mapping[str, Any], payload.get("call", {}))
            if "toolCallId" in call:
                started_ids.add(str(call["toolCallId"]))
        elif event["eventType"] in {"tool.completed", "tool.failed"}:
            result = payload.get("result")
            if isinstance(result, Mapping):
                results.append(result)
    names = [str(call["name"]) for call in accepted]
    succeeded = [
        _result_tool_name(result, accepted)
        for result in results
        if result.get("status") == "succeeded"
    ]
    required = tuple(str(value) for value in spec.expected.get("requiredTools", []))
    required_succeeded = tuple(
        str(value) for value in spec.expected.get("requiredSucceededTools", [])
    )
    forbidden = tuple(str(value) for value in spec.expected.get("forbiddenTools", []))
    forbidden_succeeded = tuple(
        str(value) for value in spec.expected.get("forbiddenSucceededTools", [])
    )
    required_recall = (
        1.0
        if not required
        else sum(value in names for value in required) / len(required)
    )
    successful_recall = (
        1.0
        if not required_succeeded
        else sum(value in succeeded for value in required_succeeded)
        / len(required_succeeded)
    )
    first_expected = spec.expected.get("firstTool")
    first_pass = first_expected is None or bool(names and names[0] == first_expected)
    selection_pass = (
        required_recall == 1.0
        and successful_recall == 1.0
        and not set(forbidden).intersection(names)
        and not set(forbidden_succeeded).intersection(succeeded)
        and first_pass
    )
    normalized = assistant_text.casefold()
    contains = tuple(
        str(value).casefold() for value in spec.expected.get("answerContainsAll", [])
    )
    excludes = tuple(
        str(value).casefold() for value in spec.expected.get("answerExcludesAll", [])
    )
    any_of = tuple(
        str(value).casefold() for value in spec.expected.get("answerAnyOf", [])
    )
    answer_evaluated = bool(contains or excludes or any_of)
    answer_pass = (
        all(value in normalized for value in contains)
        and all(value not in normalized for value in excludes)
        and (not any_of or any(value in normalized for value in any_of))
    )
    search_results = [
        result
        for result in results
        if _result_tool_name(result, accepted) == "memory.search"
        and result.get("status") == "succeeded"
    ]
    search_memories = [
        memory for result in search_results for memory in _result_memories(result)
    ]
    raw_search_expected = spec.expected.get("expectedSearchNonEmpty")
    if raw_search_expected is not None and type(raw_search_expected) is not bool:
        raise ValueError("expectedSearchNonEmpty must be a boolean")
    search_expected = cast(bool | None, raw_search_expected)
    search_executed = bool(search_results)
    search_result_pass = (
        None
        if search_expected is None or not search_executed
        else bool(search_memories) is search_expected
    )
    search_path_pass = search_expected is None or (
        search_executed and search_result_pass is True
    )
    citations = _CITATION.findall(assistant_text)
    valid_citations = 0
    for memory_id, session_id, turn_id in citations:
        if any(
            memory.get("memoryId") == memory_id
            and cast(Mapping[str, Any], memory.get("source", {})).get("sessionId")
            == session_id
            and cast(Mapping[str, Any], memory.get("source", {})).get("turnId")
            == turn_id
            for memory in search_memories
        ):
            valid_citations += 1
    citation_required = bool(contains and "memory.search" in required)
    citation_pass = not citation_required or valid_citations > 0
    supersession_pass = True
    supersession_evaluated = spec.expected.get("writeMustSupersede") is True
    if supersession_evaluated:
        written = [
            memory
            for result in results
            if _result_tool_name(result, accepted) == "memory.remember"
            and result.get("status") == "succeeded"
            for memory in _result_memories(result)
        ]
        supersession_pass = any(
            memory.get("supersedesId") is not None for memory in written
        )
    identities = [(str(call["name"]), str(call["argsHash"])) for call in accepted]
    duplicate_calls = len(identities) - len(set(identities))
    repeated_batches = len(batches) - len(set(batches))
    terminal = next(
        (
            str(event["eventType"])
            for event in reversed(events)
            if event.get("terminal")
        ),
        None,
    )
    completed = terminal == "turn.completed"
    grounded = (
        completed
        and selection_pass
        and search_path_pass
        and citation_pass
        and supersession_pass
        and answer_pass
    )
    return {
        "turnId": spec.turn_id,
        "completed": completed,
        "terminalEvent": terminal,
        "elapsedSeconds": round(elapsed, 6),
        "routing": {
            "toolNames": names,
            "succeededToolNames": succeeded,
            "requiredToolRecall": required_recall,
            "requiredSuccessfulToolRecall": successful_recall,
            "firstToolPass": first_pass,
            "selectionPass": selection_pass,
        },
        "tools": {
            "acceptedCallCount": len(accepted),
            "startedCallCount": len(started_ids),
            "resultCount": len(results),
            "successfulResultCount": sum(
                result.get("status") == "succeeded" for result in results
            ),
            "argumentConformanceRate": None
            if not accepted
            else len(started_ids) / len(accepted),
            "executionSuccessRate": None
            if not results
            else sum(result.get("status") == "succeeded" for result in results)
            / len(results),
            "redundantExactCallCount": duplicate_calls,
            "repeatedBatchCount": repeated_batches,
            "approvalRequiredCount": sum(
                event["eventType"] == "approval.required" for event in events
            ),
            "approvalResolvedCount": sum(
                event["eventType"] == "approval.resolved" for event in events
            ),
        },
        "memory": {
            "searchResultCount": len(search_memories),
            "searchExpectation": search_expected,
            "searchExecuted": search_executed,
            "searchResultPass": search_result_pass,
            "searchPathPass": search_path_pass,
            "supersessionPass": supersession_pass,
            "supersessionEvaluated": supersession_evaluated,
        },
        "answer": {
            "evaluated": answer_evaluated,
            "correct": answer_pass,
            "citationRequired": citation_required,
            "citationCount": len(citations),
            "validCitationCount": valid_citations,
            "citationPass": citation_pass,
            "groundedCorrect": grounded,
        },
        "model": {
            "attemptCount": sum(
                event["eventType"] == "model.attempt" for event in events
            ),
            **network,
        },
    }


def _result_tool_name(
    result: Mapping[str, Any], accepted: Sequence[Mapping[str, Any]]
) -> str:
    call_id = str(result.get("toolCallId", ""))
    call = next((item for item in accepted if item.get("toolCallId") == call_id), None)
    return "" if call is None else str(call["name"])


def _result_memories(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = result.get("data")
    if not isinstance(data, Mapping):
        return []
    if isinstance(data.get("memory"), Mapping):
        return [cast(Mapping[str, Any], data["memory"])]
    values = data.get("memories")
    if not isinstance(values, list):
        return []
    memories: list[Mapping[str, Any]] = []
    for value in values:
        if isinstance(value, Mapping) and isinstance(value.get("memory"), Mapping):
            memories.append(cast(Mapping[str, Any], value["memory"]))
        elif isinstance(value, Mapping):
            memories.append(value)
    return memories


async def _storage_audit(runtime: EvaluationRuntime) -> dict[str, Any]:
    records = await runtime.unit_of_work.list_entities(
        MEMORY_ITEMS_COLLECTION, limit=1_000
    )
    items = [memory_item_from_json(record.value) for record in records]
    active_keys: list[tuple[str, str | None, str, str]] = []
    status_counts: dict[str, int] = {}
    for item in items:
        status_counts[item.status.value] = status_counts.get(item.status.value, 0) + 1
        if item.status.value == "confirmed":
            active_keys.append(
                (item.profile_id, item.session_id, item.scope.value, item.key)
            )
    return {
        "recordCount": len(items),
        "statusCounts": dict(sorted(status_counts.items())),
        "activeKeyUniqueness": len(active_keys) == len(set(active_keys)),
        "credentialPatternPresent": any(
            re.search(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]", item.content)
            is not None
            for item in items
        ),
    }


def _aggregate(
    records: Sequence[Mapping[str, Any]], storage: Mapping[str, Any]
) -> dict[str, Any]:
    metrics = [cast(Mapping[str, Any], record["metrics"]) for record in records]
    routing = [cast(Mapping[str, Any], value["routing"]) for value in metrics]
    tools = [cast(Mapping[str, Any], value["tools"]) for value in metrics]
    answers = [cast(Mapping[str, Any], value["answer"]) for value in metrics]
    memories = [cast(Mapping[str, Any], value["memory"]) for value in metrics]
    required_skill_turns = [
        (record, route)
        for record, route in zip(records, routing, strict=True)
        if record["scenarioId"] != "no-memory-control"
    ]
    activated_skill_turns = [
        route for route in routing if "skill" in cast(list[str], route["toolNames"])
    ]
    required_activated = sum(
        "skill" in cast(list[str], route["toolNames"])
        for _, route in required_skill_turns
    )
    answer_evaluated = [value for value in answers if value["evaluated"]]
    citation_required = [value for value in answers if value["citationRequired"]]
    total_results = sum(int(value["resultCount"]) for value in tools)
    total_success = sum(int(value["successfulResultCount"]) for value in tools)
    total_accepted = sum(int(value["acceptedCallCount"]) for value in tools)
    total_started = sum(int(value["startedCallCount"]) for value in tools)
    search_evaluated = [
        value for value in memories if value["searchExpectation"] is not None
    ]
    executed_searches = [value for value in search_evaluated if value["searchExecuted"]]
    supersession_evaluated = [
        value for value in memories if value["supersessionEvaluated"]
    ]
    repeat_groups: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for record, metric in zip(records, metrics, strict=True):
        repeat_groups.setdefault(
            (str(record["scenarioId"]), str(record["turnId"])), []
        ).append(
            (
                metric["completed"],
                cast(Mapping[str, Any], metric["routing"])["toolNames"],
                cast(Mapping[str, Any], metric["routing"])["selectionPass"],
                cast(Mapping[str, Any], metric["answer"])["correct"],
            )
        )
    repeated = [values for values in repeat_groups.values() if len(values) > 1]
    return {
        "schemaVersion": 1,
        "turnRunCount": len(records),
        "scenarioExecutionCount": len(
            {(record["scenarioId"], record["scenarioRunIndex"]) for record in records}
        ),
        "completionRate": _rate(
            sum(bool(value["completed"]) for value in metrics), len(metrics)
        ),
        "routing": {
            "skillActivationRecall": _rate(
                required_activated,
                len(required_skill_turns),
            ),
            "skillActivationPrecision": _rate(
                required_activated, len(activated_skill_turns)
            ),
            "skillFirstAccuracy": _rate(
                sum(bool(route["firstToolPass"]) for _, route in required_skill_turns),
                len(required_skill_turns),
            ),
            "toolSelectionAccuracy": _rate(
                sum(bool(value["selectionPass"]) for value in routing), len(routing)
            ),
            "meanRequiredToolRecall": _mean(
                float(value["requiredToolRecall"]) for value in routing
            ),
            "meanRequiredSuccessfulToolRecall": _mean(
                float(value["requiredSuccessfulToolRecall"]) for value in routing
            ),
        },
        "tools": {
            "acceptedCallCount": total_accepted,
            "argumentConformanceRate": _rate(total_started, total_accepted),
            "executionSuccessRate": _rate(total_success, total_results),
            "redundantExactCallCount": sum(
                int(value["redundantExactCallCount"]) for value in tools
            ),
            "repeatedBatchCount": sum(
                int(value["repeatedBatchCount"]) for value in tools
            ),
            "approvalRequiredCount": sum(
                int(value["approvalRequiredCount"]) for value in tools
            ),
            "approvalResolvedCount": sum(
                int(value["approvalResolvedCount"]) for value in tools
            ),
        },
        "memory": {
            "searchExecutionRate": _rate(
                sum(bool(value["searchExecuted"]) for value in search_evaluated),
                len(search_evaluated),
            ),
            "executedSearchResultAccuracy": _rate(
                sum(value["searchResultPass"] is True for value in executed_searches),
                len(executed_searches),
            ),
            "endToEndSearchPathAccuracy": _rate(
                sum(bool(value["searchPathPass"]) for value in search_evaluated),
                len(search_evaluated),
            ),
            "supersessionAccuracy": _rate(
                sum(
                    bool(value["supersessionPass"]) for value in supersession_evaluated
                ),
                len(supersession_evaluated),
            ),
            "storageAudit": dict(storage),
        },
        "generation": {
            "answerAssertionAccuracy": _rate(
                sum(bool(value["correct"]) for value in answer_evaluated),
                len(answer_evaluated),
            ),
            "citationPresenceAndPrecisionAccuracy": _rate(
                sum(bool(value["citationPass"]) for value in citation_required),
                len(citation_required),
            ),
            "groundedAnswerAccuracy": _rate(
                sum(bool(value["groundedCorrect"]) for value in answer_evaluated),
                len(answer_evaluated),
            ),
        },
        "repeatability": {
            "comparedTurnCount": len(repeated),
            "exactOutcomeAgreement": _rate(
                sum(
                    all(value == values[0] for value in values[1:])
                    for values in repeated
                ),
                len(repeated),
            ),
        },
        "model": {
            "networkAttemptCount": sum(
                int(cast(Mapping[str, Any], value["model"])["networkAttemptCount"])
                for value in metrics
            ),
            "networkRetryCount": sum(
                int(cast(Mapping[str, Any], value["model"])["networkRetryCount"])
                for value in metrics
            ),
        },
    }


async def execute(args: argparse.Namespace) -> int:
    evaluation_root = Path(__file__).resolve().parents[1]
    repository_root = evaluation_root.parents[1]
    scenarios_path = evaluation_root / "scenarios.json"
    output = (
        evaluation_root / "generated" / "full-v1"
        if args.output is None
        else args.output
    ).resolve()
    if output.exists() and any(output.iterdir()) and not args.resume:
        raise FileExistsError(
            f"evaluation output already exists: {output}; use a new directory or --resume"
        )
    secret_path = (
        _discover_secret(repository_root)
        if args.secret_file is None
        else args.secret_file.resolve(strict=True)
    )
    resolver = FileSecretResolver(secret_path)
    try:
        scenarios = _load_scenarios(scenarios_path, full=args.plan == "full")
        runtime = await build_runtime(
            repository_root=repository_root,
            scenarios_path=scenarios_path,
            output=output,
            resolver=resolver,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
        )
        trace_path = output / "raw-traces.jsonl"
        existing = (
            _load_existing(trace_path, runtime.config_fingerprint)
            if args.resume
            else {}
        )
        semaphore = asyncio.Semaphore(args.concurrency)

        async def run_one(spec: ScenarioSpec) -> list[dict[str, Any]]:
            keys = [f"{spec.key}:{turn.turn_id}" for turn in spec.turns]
            if all(key in existing for key in keys):
                return [existing[key] for key in keys]
            if any(key in existing for key in keys):
                raise ValueError(
                    f"cannot resume partially completed scenario {spec.key}"
                )
            async with semaphore:
                print(f"RUN {spec.key} turns={len(spec.turns)}", flush=True)
                values = await runtime.run_scenario(spec)
                print(
                    f"DONE {spec.key} completed={sum(value['metrics']['completed'] for value in values)}/"
                    f"{len(values)}",
                    flush=True,
                )
                return values

        scenario_records = await asyncio.gather(*(run_one(spec) for spec in scenarios))
        records: list[dict[str, Any]] = []
        for values in scenario_records:
            for value in values:
                if value["key"] not in existing:
                    _append_secure_jsonl(trace_path, value, resolver)
                    existing[value["key"]] = value
                records.append(existing[value["key"]])
        storage = await _storage_audit(runtime)
        aggregate = _aggregate(records, storage)
        aggregate["integrity"] = {
            "agentLoopEntrypoint": "offeragent_harness.agent.loop.run_agent_loop",
            "planner": "offeragent_harness.agent.model_planner.ModelPlanner",
            "provider": "offeragent_harness.providers.deepseek_chat.DeepSeekChatGateway",
            "toolKernel": "offeragent_harness.tools.kernel.UnifiedToolKernel",
            "runPreparation": [
                "ConversationHistoryRunPreparationAdapter",
                "StructuredMemoryRunPreparationAdapter",
            ],
            "tools": [definition.name for definition in runtime.definitions],
            "skill": "personal-memory",
            "executionPath": "canonical-production-agent-loop",
            "goldInjectedIntoPrompt": False,
            "offlineAnswerSubstitution": False,
            "approvalMode": "one-time-synthetic-user-approval",
            "secretLeakDetected": False,
            "configFingerprint": runtime.config_fingerprint,
        }
        aggregate["configuration"] = {
            "model": args.model,
            "reasoningEffort": args.reasoning_effort,
            "concurrency": args.concurrency,
            "plan": args.plan,
        }
        compact = [
            {
                "key": value["key"],
                "scenarioId": value["scenarioId"],
                "scenarioRunIndex": value["scenarioRunIndex"],
                "turnId": value["turnId"],
                "assistantText": value["assistantText"],
                "error": value["error"],
                "metrics": value["metrics"],
                "configFingerprint": value["configFingerprint"],
            }
            for value in records
        ]
        results_payload = b"".join(_json_bytes(value) + b"\n" for value in compact)
        report_payload = _json_bytes(aggregate, pretty=True) + b"\n"
        if resolver.appears_in(results_payload) or resolver.appears_in(report_payload):
            raise RuntimeError("secret leak guard rejected generated evaluation output")
        _atomic_write(output / "run-results.jsonl", results_payload)
        _atomic_write(output / "agent-loop-report.json", report_payload)
        print(
            f"REPORT turns={aggregate['turnRunCount']} completion={aggregate['completionRate']} "
            f"grounded={aggregate['generation']['groundedAnswerAccuracy']}",
            flush=True,
        )
        return 0
    finally:
        resolver.close()


def _load_scenarios(path: Path, *, full: bool) -> list[ScenarioSpec]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or raw.get("schemaVersion") != 1
        or not isinstance(raw.get("scenarios"), list)
    ):
        raise ValueError("memory scenario dataset is incompatible")
    scenarios: dict[str, tuple[TurnSpec, ...]] = {}
    for scenario in raw["scenarios"]:
        if not isinstance(scenario, dict):
            raise ValueError("scenario must be an object")
        scenario_id = str(scenario["id"])
        default_profile = str(scenario["profileAlias"])
        turns = tuple(
            TurnSpec(
                turn_id=str(turn["id"]),
                profile_alias=str(turn.get("profileAlias", default_profile)),
                session_alias=str(turn["sessionAlias"]),
                prompt=str(turn["prompt"]),
                expected=cast(Mapping[str, Any], turn["expected"]),
            )
            for turn in scenario["turns"]
        )
        if not turns or scenario_id in scenarios:
            raise ValueError("scenario identities/turns are invalid")
        scenarios[scenario_id] = turns
    if not full:
        selected = (
            "profile-cross-session",
            "proposal-confirmation",
            "no-memory-control",
        )
        return [ScenarioSpec(value, 1, scenarios[value]) for value in selected]
    specs = [ScenarioSpec(key, 1, value) for key, value in scenarios.items()]
    repeatability = cast(Mapping[str, Any], raw["repeatability"])
    repeats = int(repeatability["totalRunsPerSelectedScenario"])
    specs.extend(
        ScenarioSpec(str(scenario_id), run_index, scenarios[str(scenario_id)])
        for scenario_id in repeatability["scenarioIds"]
        for run_index in range(2, repeats + 1)
    )
    return specs


def _load_existing(path: Path, fingerprint: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    records = {str(value["key"]): value for value in _read_jsonl(path)}
    if any(value.get("configFingerprint") != fingerprint for value in records.values()):
        raise ValueError("existing traces use another runtime configuration")
    return records


def _stable_id(namespace: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{namespace}_{digest}"


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def _mean(values: Sequence[float] | Any) -> float | None:
    materialized = list(values)
    return None if not materialized else round(sum(materialized) / len(materialized), 6)


def _json_sha256(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_json_bytes(value)).hexdigest()}"


def _sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    ).encode("utf-8")


def _append_secure_jsonl(
    path: Path, value: Mapping[str, Any], resolver: FileSecretResolver
) -> None:
    payload = _json_bytes(value) + b"\n"
    if resolver.appears_in(payload):
        raise RuntimeError("secret leak guard rejected an Agent Loop trace")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} must contain an object")
            values.append(value)
    return values


def _discover_secret(start: Path) -> Path:
    for root in (start, *start.parents):
        candidate = root / ".secret"
        if candidate.is_file():
            return candidate.resolve(strict=True)
    raise FileNotFoundError("no .secret file found in repository ancestors")


def _zero(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", choices=("smoke", "full"), default="full")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default="none")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--secret-file", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.concurrency < 1 or args.concurrency > 4:
        parser.error("--concurrency must be between 1 and 4")
    raise SystemExit(asyncio.run(execute(args)))


if __name__ == "__main__":
    main()
