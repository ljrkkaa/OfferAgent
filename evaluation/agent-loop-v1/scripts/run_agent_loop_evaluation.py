"""Run the canonical OfferAgent Agent Loop against the real-paper knowledge Vault."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
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
from offeragent_harness.agent.state import RunState
from offeragent_harness.agent.system_rules import offeragent_system_rules
from offeragent_harness.evaluation import (
    AgentEvaluationCase,
    GoldEvidence,
    aggregate_run_results,
    evaluate_run_trace,
)
from offeragent_harness.foundation import NetworkAuditRecord
from offeragent_harness.knowledge import KnowledgeCatalogStore
from offeragent_harness.models import thaw_json
from offeragent_harness.permissions import (
    CapabilityScope,
    PermissionMode,
    PolicyContext,
    RiskClass,
)
from offeragent_harness.permissions.audit import NullPolicyAuditSink
from offeragent_harness.permissions.evaluator import RuleBasedPolicyEvaluator
from offeragent_harness.ports import CancellationToken, Sensitivity
from offeragent_harness.ports.secrets import SecretHandle, SecretKind
from offeragent_harness.ports.vault import VaultTransaction
from offeragent_harness.providers.deepseek_chat import (
    DEEPSEEK_BASE_URL,
    build_deepseek_gateway,
)
from offeragent_harness.providers.openai_responses import StaticModelEndpointPolicy
from offeragent_harness.runtime import CancellationScope
from offeragent_harness.runtime.production_worker_composition import (
    SecureIdGenerator,
    SystemClock,
)
from offeragent_harness.sessions import AgentLineage
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
from offeragent_harness.workspace import (
    CodeToolExecutor,
    VaultFileSystem,
    VaultReadPolicy,
    WorkspacePathPolicy,
)

T = TypeVar("T")
_WORKSPACE_ID = "ws-offeragent-agent-loop-eval-v1"
_SECRET_HANDLE = SecretHandle("secret:v1:" + "1" * 32)
_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True, slots=True)
class RunSpec:
    case: AgentEvaluationCase
    run_index: int

    @property
    def key(self) -> str:
        return f"{self.case.case_id}:{self.run_index}"


class FileSecretResolver:
    """Process-local secret resolver whose plaintext is never serializable or printable."""

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
            handle != _SECRET_HANDLE
            or scope_id != _WORKSPACE_ID
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


class ReadOnlyTransactions:
    async def execute(
        self, transaction: VaultTransaction, cancellation: CancellationToken
    ) -> ToolResult:
        del transaction, cancellation
        raise AssertionError("Agent Loop evaluation exposes read-only tools only")


class StaticSkillAuthority(SkillAuthorityProvider):
    def __init__(self, authority: SkillAuthority) -> None:
        self._authority = authority

    async def authority_for(self, call: ToolCall) -> SkillAuthority:
        if call.workspace_id != _WORKSPACE_ID:
            raise ValueError("skill call crossed the evaluation Workspace boundary")
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
        thawed = thaw_json(payload)
        if not isinstance(thawed, dict):
            raise TypeError("Agent Loop event payload must be a JSON object")
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "eventType": event_type,
                "payload": thawed,
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
    """Content-free provider attempt accounting, partitioned by Run identity."""

    def __init__(self) -> None:
        self._records: dict[str, list[NetworkAuditRecord]] = {}

    async def record(self, audit: NetworkAuditRecord) -> None:
        if audit.run_id is None:
            raise ValueError("Agent evaluation network audit requires a Run identity")
        self._records.setdefault(audit.run_id, []).append(audit)

    def consume_summary(self, run_id: str) -> dict[str, object]:
        records = self._records.pop(run_id, [])
        results = [record for record in records if record.stage == "result"]
        operations = {record.operation_id for record in results}
        retries = sum(record.attempt > 1 for record in results)
        outcomes: dict[str, int] = {}
        for record in results:
            outcomes[record.outcome] = outcomes.get(record.outcome, 0) + 1
        return {
            "logicalRequestCount": len(operations),
            "networkAttemptCount": len(results),
            "networkRetryCount": retries,
            "networkOutcomeCounts": dict(sorted(outcomes.items())),
        }


@dataclass(slots=True)
class EvaluationRuntime:
    vault: Path
    skill_catalog: SkillCatalog
    code_executor: CodeToolExecutor
    skill_executor: SkillToolExecutor
    definitions: tuple[ToolDefinition, ...]
    gateway: Any
    network_audit: EvaluationNetworkAuditSink
    journal: Any
    clock: SystemClock
    ids: SecureIdGenerator
    source_path_by_id: Mapping[str, str]
    skill_prompt: ContextFragment
    config_fingerprint: str
    model: str
    reasoning_effort: str

    async def run(self, spec: RunSpec) -> dict[str, Any]:
        case = spec.case
        session_id = self.ids.new_id("session")
        turn_id = self.ids.new_id("turn")
        run_id = self.ids.new_id("run")
        started_at = self.clock.utcnow()
        budget = BudgetLedger(
            RunBudget(
                max_model_rounds=24,
                max_tool_calls=64,
                max_parallel_reads=4,
                max_wall_seconds=420,
                max_input_tokens=600_000,
                max_output_tokens=64_000,
                max_cost=Decimal("100"),
                max_artifact_bytes=1,
                max_subagents=1,
            ),
            started_at=started_at,
        )
        state = replace(
            RunState(
                _WORKSPACE_ID,
                session_id,
                turn_id,
                run_id,
                AgentLineage.root(run_id),
            ),
            budget_checkpoint=await BudgetCheckpoint.capture(budget, now=started_at),
        )
        context = ContextManager(
            system_rules=offeragent_system_rules(),
            inputs=ContextInputs(
                user_input=(
                    ContextFragment(
                        fragment_id=f"evaluation:{case.case_id}:{spec.run_index}",
                        layer=ContextLayer.USER_INPUT,
                        text=case.prompt,
                        sensitivity=Sensitivity.PUBLIC,
                    ),
                ),
                skills=(self.skill_prompt,),
            ),
            visibility=ContextVisibilityPolicy.cloud_model(),
            budget=ContextBudget.generous_default(),
            local_timezone=timezone.utc,
        )
        planner = ModelPlanner(
            gateway=self.gateway,
            context_manager=context,
            catalog=AgentStepCatalog(self.definitions, max_calls=16),
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
            allowed_risks=frozenset({RiskClass.READ}),
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
                workspace_id=_WORKSPACE_ID,
                session_id=session_id,
                principal_id="agent-loop-evaluator",
                run_id=run_id,
                permission_mode=PermissionMode.READ_ONLY,
                effective_scope=scope,
                workspace_trusted=True,
                now=self.clock.utcnow(),
            )

        router = LocalExecutorRouter(
            (
                (self.code_executor.definitions, self.code_executor),
                (self.skill_executor.definitions, self.skill_executor),
            )
        )
        kernel = UnifiedToolKernel(
            registry=ToolRegistry(f"evaluation-{run_id}", self.definitions),
            validator=ToolValidator(),
            policy=RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink()),
            policy_context=policy_context,
            scheduler=ToolScheduler(clock=self.clock, max_parallel_reads=4),
            dispatcher=ToolDispatcher(local=router),
            journal=self.journal,
            clock=self.clock,
            ids=self.ids,
        )
        recorder = EventRecorder()
        cancellation = CancellationScope(
            name=f"evaluation-{case.case_id}-{spec.run_index}"
        )
        started = time.perf_counter()
        error: dict[str, str] | None = None
        final_state: RunState | None = None
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
                timeout=450,
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
        assistant_text = "" if final_state is None else final_state.assistant_text
        metrics = evaluate_run_trace(
            case,
            events=recorder.events,
            assistant_text=assistant_text,
            elapsed_seconds=elapsed,
            source_path_by_id=self.source_path_by_id,
            run_index=spec.run_index,
        )
        cast(dict[str, Any], metrics["model"]).update(
            self.network_audit.consume_summary(run_id)
        )
        return {
            "schemaVersion": 1,
            "key": spec.key,
            "configFingerprint": self.config_fingerprint,
            "caseId": case.case_id,
            "caseType": case.case_type,
            "requiresKnowledge": case.requires_knowledge,
            "runIndex": spec.run_index,
            "prompt": case.prompt,
            "assistantText": assistant_text,
            "error": error,
            "events": recorder.events,
            "metrics": metrics,
        }


async def build_runtime(
    *,
    repository: Path,
    vault: Path,
    evaluation_inputs: Mapping[str, Path],
    output: Path,
    resolver: FileSecretResolver,
    model: str,
    reasoning_effort: str,
) -> EvaluationRuntime:
    ripgrep = shutil.which("rg.exe") or shutil.which("rg")
    if ripgrep is None:
        raise RuntimeError("ripgrep is required for the real Agent Loop evaluation")
    skill_root = (
        repository / "packages" / "offeragent-harness" / "packaging" / "runtime-skills"
    )
    catalog = SkillCatalog(
        workspace_id=_WORKSPACE_ID,
        roots=(
            SkillRoot(
                _WORKSPACE_ID,
                "runtime-builtin",
                SkillLayer.BUILTIN,
                skill_root.resolve(strict=True),
                workspace_trusted=True,
            ),
        ),
    )
    setup_cancellation = CancellationScope(name="evaluation-setup")
    try:
        reload_result = await catalog.initialize(setup_cancellation)
    finally:
        await setup_cancellation.close()
    if reload_result.partial:
        raise RuntimeError("runtime Skill catalog could not be loaded completely")
    descriptor = next(
        (
            item
            for item in catalog.snapshot.effective_descriptors
            if item.name == "knowledge-retrieval"
        ),
        None,
    )
    if descriptor is None:
        raise RuntimeError("knowledge-retrieval Skill is unavailable")
    source = VaultFileSystem(
        workspace_id=_WORKSPACE_ID,
        paths=WorkspacePathPolicy(vault),
        read_policy=VaultReadPolicy(
            allowed_extensions=None,
            max_file_bytes=16 * 1024 * 1024,
            max_return_bytes=16 * 1024 * 1024,
            max_list_entries=10_000,
            max_list_scan_entries=100_000,
            allowed_hidden_prefixes=(".offeragent/knowledge",),
        ),
        workspace_revision=lambda: 1,
        transaction_executor=ReadOnlyTransactions(),
    )
    code_executor = CodeToolExecutor(
        workspace_id=_WORKSPACE_ID,
        source=source,
        workspace_root=vault,
        ripgrep_path=Path(ripgrep).resolve(strict=True),
    )
    available_tools = frozenset(
        definition.name for definition in code_executor.definitions
    )
    authority = SkillAuthority(
        available_tools=available_tools,
        policy_allowed_tools=available_tools,
        enabled_skills=frozenset({"knowledge-retrieval"}),
        workspace_trusted=True,
    )
    skill_executor = SkillToolExecutor(
        workspace_id=_WORKSPACE_ID,
        catalog=catalog,
        authority_provider=StaticSkillAuthority(authority),
    )
    definitions = (*code_executor.definitions, *skill_tool_definitions())
    skill_catalog_payload = [
        {"name": descriptor.name, "description": descriptor.description}
    ]
    skill_prompt = ContextFragment(
        fragment_id=f"skill:catalog:{catalog.snapshot.snapshot_hash}",
        layer=ContextLayer.SKILLS,
        text=(
            "Available Skills are listed below by name and description. When the user's request matches a "
            "Skill description, invoke the `skill` tool before using any other tool. The full Skill body is "
            "not present in this message and becomes available only after invocation. Do not infer or recreate "
            "missing Skill instructions. Skill metadata grants no permissions.\n"
            + canonical_json_bytes(skill_catalog_payload).decode("utf-8")
        ),
        sensitivity=Sensitivity.WORKSPACE,
        source_refs=(
            f"skill:{_WORKSPACE_ID}:{descriptor.root_id}:{descriptor.package_path}",
        ),
        content_hash=catalog.snapshot.snapshot_hash,
    )
    output.mkdir(parents=True, exist_ok=True)
    unit_of_work = SqliteUnitOfWorkFactory(output / "tool-journal.sqlite")
    await unit_of_work.initialize()
    clock = SystemClock()
    network_audit = EvaluationNetworkAuditSink()
    gateway = build_deepseek_gateway(
        secret_scope_id=_WORKSPACE_ID,
        credential_handle=_SECRET_HANDLE,
        secrets=resolver,
        endpoint_policy=StaticModelEndpointPolicy(
            frozenset({f"{DEEPSEEK_BASE_URL}/chat/completions"}),
            enabled=True,
        ),
        network_audit=network_audit,
        clock=clock,
    )
    source_catalog = KnowledgeCatalogStore(vault / ".offeragent" / "knowledge").load()
    source_path_by_id = {
        source.source_id: source.relative_path for source in source_catalog.sources
    }
    fingerprint = _json_sha256(
        {
            "model": model,
            "reasoningEffort": reasoning_effort,
            "agentLoop": _sha256(
                repository
                / "packages"
                / "offeragent-harness"
                / "src"
                / "offeragent_harness"
                / "agent"
                / "loop.py"
            ),
            "skillMetadata": descriptor.content_hash,
            "skillDocument": _sha256(descriptor.skill_file),
            "toolCatalog": AgentStepCatalog(definitions, max_calls=16).fingerprint,
            "knowledgeRevision": source_catalog.revision,
            "knowledgeCatalog": _sha256(
                vault / ".offeragent" / "knowledge" / "catalog.json"
            ),
            "evaluationInputs": {
                name: _sha256(path) for name, path in sorted(evaluation_inputs.items())
            },
        }
    )
    return EvaluationRuntime(
        vault=vault,
        skill_catalog=catalog,
        code_executor=code_executor,
        skill_executor=skill_executor,
        definitions=tuple(definitions),
        gateway=gateway,
        network_audit=network_audit,
        journal=unit_of_work.invocation_journal,
        clock=clock,
        ids=SecureIdGenerator(),
        source_path_by_id=source_path_by_id,
        skill_prompt=skill_prompt,
        config_fingerprint=fingerprint,
        model=model,
        reasoning_effort=reasoning_effort,
    )


async def execute(args: argparse.Namespace) -> int:
    script = Path(__file__).resolve()
    evaluation_root = script.parents[1]
    repository = evaluation_root.parents[1]
    corpus_root = evaluation_root / "corpus"
    questions_path = corpus_root / "generated" / "questions.jsonl"
    controls_path = evaluation_root / "routing-controls.jsonl"
    plan_path = evaluation_root / "evaluation-plan.json"
    vault = corpus_root / "generated" / "vault"
    output = (
        evaluation_root / "generated" if args.output is None else Path(args.output)
    ).resolve(strict=False)
    secret_path = (
        _discover_secret(repository)
        if args.secret_file is None
        else Path(args.secret_file).resolve(strict=True)
    )
    resolver = FileSecretResolver(secret_path)
    try:
        cases = _load_cases(questions_path, controls_path)
        specs = _run_specs(cases, plan_path, args)
        runtime = await build_runtime(
            repository=repository,
            vault=vault.resolve(strict=True),
            evaluation_inputs={
                "evaluationPlan": plan_path,
                "knowledgeQuestions": questions_path,
                "routingControls": controls_path,
                "sourceManifest": corpus_root / "sources.json",
            },
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
        pending = [spec for spec in specs if spec.key not in existing]
        semaphore = asyncio.Semaphore(args.concurrency)

        async def run_one(spec: RunSpec) -> dict[str, Any]:
            async with semaphore:
                print(f"RUN {spec.key}", flush=True)
                result = await runtime.run(spec)
                print(
                    f"DONE {spec.key} terminal={result['metrics']['terminalEvent']} "
                    f"answer={result['metrics']['answer']['answerCorrect']}",
                    flush=True,
                )
                return result

        for offset in range(0, len(pending), args.concurrency):
            batch = pending[offset : offset + args.concurrency]
            completed = await asyncio.gather(*(run_one(spec) for spec in batch))
            for record in completed:
                _append_secure_jsonl(trace_path, record, resolver)
                existing[str(record["key"])] = record
        selected_records = [existing[spec.key] for spec in specs]
        metrics = [
            cast(Mapping[str, Any], record["metrics"]) for record in selected_records
        ]
        aggregate = aggregate_run_results(metrics)
        aggregate["integrity"] = {
            "agentLoopEntrypoint": "offeragent_harness.agent.loop.run_agent_loop",
            "planner": "offeragent_harness.agent.model_planner.ModelPlanner",
            "provider": "offeragent_harness.providers.deepseek_chat.DeepSeekChatGateway",
            "toolKernel": "offeragent_harness.tools.kernel.UnifiedToolKernel",
            "tools": ["skill", "glob", "grep", "read"],
            "skill": "knowledge-retrieval",
            "executionPath": "canonical-production-agent-loop",
            "goldInjectedIntoPrompt": False,
            "secretLeakDetected": False,
            "configFingerprint": runtime.config_fingerprint,
        }
        aggregate["configuration"] = {
            "model": args.model,
            "reasoningEffort": args.reasoning_effort,
            "concurrency": args.concurrency,
            "plan": args.plan,
        }
        results_path = output / "run-results.jsonl"
        results_payload = b"".join(
            _json_bytes(item) + b"\n"
            for item in sorted(
                metrics, key=lambda value: (value["caseId"], value["runIndex"])
            )
        )
        report_payload = _json_bytes(aggregate, pretty=True) + b"\n"
        if resolver.appears_in(results_payload) or resolver.appears_in(report_payload):
            raise RuntimeError("secret leak guard rejected generated evaluation output")
        _atomic_write(results_path, results_payload)
        _atomic_write(output / "agent-loop-report.json", report_payload)
        print(
            f"REPORT runs={aggregate['runCount']} completion={aggregate['completionRate']} "
            f"answer={aggregate['generation']['answerCorrectness']}",
            flush=True,
        )
        return 0
    finally:
        resolver.close()


def _load_cases(
    questions_path: Path, controls_path: Path
) -> dict[str, AgentEvaluationCase]:
    cases: dict[str, AgentEvaluationCase] = {}
    for item in _read_jsonl(questions_path):
        evidence = tuple(
            GoldEvidence(
                str(value["sourcePath"]), tuple(int(page) for page in value["pages"])
            )
            for value in item["evidence"]
        )
        case = AgentEvaluationCase(
            case_id=str(item["id"]),
            prompt=str(item["question"]),
            case_type=str(item["type"]),
            answer=str(item["answer"]),
            evidence=evidence,
            requires_knowledge=True,
        )
        cases[case.case_id] = case
    for item in _read_jsonl(controls_path):
        case = AgentEvaluationCase(
            case_id=str(item["id"]),
            prompt=str(item["prompt"]),
            case_type=str(item["type"]),
            answer=str(item["answer"]),
            evidence=(),
            requires_knowledge=False,
        )
        cases[case.case_id] = case
    if len(cases) != 110:
        raise ValueError(
            "the frozen Agent Loop evaluation requires exactly 110 unique cases"
        )
    return cases


def _run_specs(
    cases: Mapping[str, AgentEvaluationCase], plan_path: Path, args: argparse.Namespace
) -> list[RunSpec]:
    if args.case_id:
        missing = sorted(set(args.case_id) - set(cases))
        if missing:
            raise ValueError(f"unknown case IDs: {missing}")
        return [
            RunSpec(cases[case_id], run_index)
            for case_id in args.case_id
            for run_index in range(1, args.repetitions + 1)
        ]
    ordered = sorted(cases.values(), key=lambda item: item.case_id)
    if args.plan == "smoke":
        selected = [
            next(
                item
                for item in ordered
                if item.requires_knowledge and item.case_type != "unanswerable"
            ),
            next(item for item in ordered if item.case_type == "unanswerable"),
            next(item for item in ordered if not item.requires_knowledge),
        ]
        return [RunSpec(case, 1) for case in selected]
    plan = _read_json(plan_path)
    repeated_ids = [str(value) for value in plan["repeatability"]["caseIds"]]
    repeats = int(plan["repeatability"]["totalRunsPerSelectedCase"])
    specs = [RunSpec(case, 1) for case in ordered]
    specs.extend(
        RunSpec(cases[case_id], run_index)
        for case_id in repeated_ids
        for run_index in range(2, repeats + 1)
    )
    if len(specs) != int(plan["expectedRunCount"]):
        raise ValueError("frozen full-plan cardinality changed")
    return specs


def _load_existing(path: Path, config_fingerprint: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    records = {str(item["key"]): item for item in _read_jsonl(path)}
    mismatched = [
        key
        for key, item in records.items()
        if item.get("configFingerprint") != config_fingerprint
    ]
    if mismatched:
        raise ValueError(
            "existing traces use another runtime configuration; choose a new output directory"
        )
    return records


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


def _discover_secret(start: Path) -> Path:
    for candidate_root in (start, *start.parents):
        candidate = candidate_root / ".secret"
        if candidate.is_file():
            return candidate.resolve(strict=True)
    raise FileNotFoundError("no .secret file was found in repository ancestors")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not all(isinstance(value, dict) for value in values):
        raise ValueError(f"expected JSON objects: {path}")
    return values


def _json_bytes(value: object, *, pretty: bool = False) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    ).encode("utf-8")


def _json_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(value)).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _zero(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--model", default=_MODEL)
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high"),
        default="medium",
    )
    parser.add_argument("--secret-file")
    parser.add_argument("--output")
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args(argv)
    if not 1 <= args.concurrency <= 8:
        parser.error("--concurrency must be between 1 and 8")
    if not 1 <= args.repetitions <= 10:
        parser.error("--repetitions must be between 1 and 10")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(execute(parse_arguments(argv)))


if __name__ == "__main__":
    sys.exit(main())
