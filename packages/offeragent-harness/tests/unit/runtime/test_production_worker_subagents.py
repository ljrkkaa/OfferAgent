from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.agent import BudgetLedger
from offeragent_harness.agent.state import RunState
from offeragent_harness.config import HarnessConfig
from offeragent_harness.permissions import CapabilityScope, PermissionMode, RiskClass
from offeragent_harness.permissions.audit import PolicyAuditRecord
from offeragent_harness.ports import CancellationToken, InvocationRecord, JournalState
from offeragent_harness.ports.subagents import ChildRunExecution, ParentRunAuthority
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.runtime.harness_service import PreparedRunComponents, RunComponents, StartTurnCommand
from offeragent_harness.runtime.production_worker_composition import ProductionRunComponentsFactory
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.subagents.models import (
    AgentBudget,
    AgentUsage,
    ContextForkMode,
    ContextSnapshot,
    EffectiveToolScope,
    ExecutionPriority,
    SubagentLifetime,
    SubagentRunRecord,
    SubagentRunStatus,
)
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
)
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffectClass,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
)
from offeragent_harness.tools.kernel import UnifiedToolKernel
from offeragent_harness.tools.registry import ToolRegistry
from offeragent_harness.vault import vault_transaction_definition

NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)


class _CapturingFactory(ProductionRunComponentsFactory):
    def __init__(self, registry: ToolRegistry) -> None:
        self._registries = {"run_root": registry}
        self._effective_configs = {"run_root": HarnessConfig()}
        self.captured: dict[str, Any] = {}

    def _build(self, config: Any, state: Any, inputs: Any, **kwargs: Any) -> RunComponents:
        self.captured = {"config": config, "state": state, "inputs": inputs, **kwargs}
        return cast(RunComponents, object())

    def _prepared_token(self, state: RunState, prepared: PreparedRunComponents) -> Any:
        del state
        return prepared.token


class _MutableParentAuthorities:
    def __init__(self, authority: ParentRunAuthority) -> None:
        self.authority = authority
        self.available = True
        self.calls: list[str] = []

    async def authority_for(self, run_id: str) -> ParentRunAuthority:
        self.calls.append(run_id)
        if not self.available:
            raise RuntimeError("parent authority store unavailable")
        if run_id != self.authority.lineage.run_id:
            raise KeyError(run_id)
        return self.authority


class _CountingExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        self.calls.append(call)
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.SUCCEEDED,
            {"value": call.arguments["value"]},
            "executed",
            (),
            (),
            (),
            False,
            None,
            None,
            None,
        )


class _RecordingJournal:
    def __init__(self) -> None:
        self.gets = 0
        self.starts = 0
        self.completes = 0
        self.unknowns = 0
        self._records: dict[tuple[str, str], InvocationRecord] = {}

    async def get(self, scope: str, idempotency_key: str) -> InvocationRecord | None:
        self.gets += 1
        return self._records.get((scope, idempotency_key))

    async def start(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        started_at: datetime,
    ) -> InvocationRecord:
        self.starts += 1
        record = InvocationRecord(
            scope,
            idempotency_key,
            request_hash,
            JournalState.STARTED,
            started_at,
            None,
            None,
        )
        self._records[(scope, idempotency_key)] = record
        return record

    async def complete(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        result: ToolResult,
        completed_at: datetime,
    ) -> InvocationRecord:
        self.completes += 1
        record = InvocationRecord(
            scope,
            idempotency_key,
            request_hash,
            JournalState.COMPLETED,
            self._records[(scope, idempotency_key)].started_at,
            completed_at,
            result,
        )
        self._records[(scope, idempotency_key)] = record
        return record

    async def mark_unknown(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        completed_at: datetime,
    ) -> InvocationRecord:
        self.unknowns += 1
        record = InvocationRecord(
            scope,
            idempotency_key,
            request_hash,
            JournalState.UNKNOWN,
            self._records[(scope, idempotency_key)].started_at,
            completed_at,
            None,
        )
        self._records[(scope, idempotency_key)] = record
        return record


class _RecordingPolicyAudit:
    def __init__(self) -> None:
        self.records: list[PolicyAuditRecord] = []

    async def record(self, audit: PolicyAuditRecord) -> None:
        self.records.append(audit)


def _scope(definition: ToolDefinition) -> CapabilityScope:
    return CapabilityScope(
        allowed_tools=frozenset({definition.name}),
        denied_tools=frozenset(),
        allowed_risks=frozenset({definition.risk}),
        root_capabilities=definition.required_capabilities,
        allow_network=False,
        allow_secret_handles=False,
    )


def _record(
    definition: ToolDefinition,
    registry_snapshot_hash: str,
    *,
    permission: PermissionMode = PermissionMode.NORMAL,
) -> SubagentRunRecord:
    task = "bounded child"
    tool_scope = EffectiveToolScope(
        allowed_versions={definition.name: (definition.version,)},
        argument_constraints={},
        registry_snapshot_hash=registry_snapshot_hash,
    )
    return SubagentRunRecord(
        run_id="run_child",
        root_run_id="run_root",
        parent_run_id="run_root",
        ancestor_run_ids=("run_root",),
        session_id="session_test",
        turn_id="turn_test",
        workspace_id="ws_test",
        trace_id="trace_test",
        spawn_call_id="spawn_test",
        agent_name="worker",
        agent_version="1",
        task=task,
        task_fingerprint=f"sha256:{hashlib.sha256(task.encode()).hexdigest()}",
        depth=1,
        lifetime=SubagentLifetime.PARENT,
        context_snapshot_id="context_test",
        permission_mode=permission,
        effective_scope=_scope(definition),
        tool_scope=tool_scope,
        budget_limit=AgentBudget(2_000, 1_000, 3, 5, 30.0, 8_192, 1, 250_000),
        budget_used=AgentUsage(),
        deadline_at=NOW + timedelta(minutes=10),
        result_schema={"type": "object"},
        status=SubagentRunStatus.RUNNING,
        phase="planning",
        priority=ExecutionPriority.NORMAL,
        created_at=NOW,
        updated_at=NOW,
    )


def _execution(record: SubagentRunRecord) -> ChildRunExecution:
    content = {"task": record.task}
    context = ContextSnapshot(
        snapshot_id=record.context_snapshot_id,
        workspace_id=record.workspace_id,
        parent_run_id=record.parent_run_id,
        mode=ContextForkMode.SUMMARY,
        content=content,
        content_hash=canonical_json_sha256(content),
        created_at=NOW,
    )
    return ChildRunExecution(
        record=record,
        context=context,
        tool_scope=record.tool_scope,
        run_config={
            "provider": "codex",
            "model": "gpt-test",
            "permissionMode": "normal",
        },
        run_entity_revision=1,
        state_entity_revision=1,
        event_sequence=1,
        trace_id=record.trace_id,
    )


def _state(record: SubagentRunRecord) -> RunState:
    return RunState(
        workspace_id=record.workspace_id,
        session_id=record.session_id,
        turn_id=record.turn_id,
        run_id=record.run_id,
        lineage=record.lineage,
    )


def _side_effect_definition() -> ToolDefinition:
    return ToolDefinition(
        name="local.mutate",
        version="1",
        description="counted local side effect",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        required_capabilities=frozenset({"local.mutate"}),
        concurrency_safe=False,
        idempotent=True,
        retryable=False,
        timeout_ms=1_000,
        output_limit_bytes=1_024,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def test_child_components_use_durable_budget_scope_and_exact_root_tool_snapshot() -> None:
    definition = vault_transaction_definition()
    registry = ToolRegistry(
        "root-run",
        (definition,),
        preflight_provider_ids=frozenset({cast(str, definition.preflight_provider)}),
    )
    factory = _CapturingFactory(registry)
    record = _record(definition, registry.snapshot_hash, permission=PermissionMode.READ_ONLY)
    execution = _execution(record)

    factory.build_child(execution, _state(record))

    assert factory.captured["definitions_override"] == (definition,)
    assert factory.captured["definitions_override"][0].executor_location is ExecutorLocation.LOCAL
    assert factory.captured["scope_override"] is record.effective_scope
    assert factory.captured["permission_override"] is PermissionMode.READ_ONLY
    assert factory.captured["child_record"] is record
    budget = factory.captured["budget_override"]
    assert budget.max_model_rounds == 3
    assert budget.max_tool_calls == 5
    assert budget.max_input_tokens == 2_000
    assert budget.max_output_tokens == 1_000
    assert budget.max_wall_seconds == 30.0
    assert budget.max_artifact_bytes == 8_192
    assert budget.max_subagents == 1


def test_worker_factory_injects_one_gate_and_lock_pool_across_sessions_and_root_child_kernels(
    tmp_path: Path,
) -> None:
    definition = _side_effect_definition()
    clock = ManualClock(NOW)
    effective_config = HarnessConfig.model_validate(
        {
            "policy": {"workspace_trusted": True, "read_only": False},
            "budgets": {"max_parallel_reads": 6},
        }
    )
    factory = ProductionRunComponentsFactory(
        workspace_id="ws_test",
        clock=clock,
        ids=DeterministicIdGenerator(),
        gateway_factory=lambda _settings: cast(Any, object()),
        # Composition precedes persisted-config reconciliation, so this
        # bootstrap default intentionally differs from the first Run snapshot.
        default_config=HarnessConfig(),
        approvals=ApprovalManager(unit_of_work=InMemoryUnitOfWorkFactory(), clock=clock),
        policy_audit=_RecordingPolicyAudit(),
        journal=_RecordingJournal(),
        artifacts=LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_test"),
        local_transaction=cast(Any, SimpleNamespace(provider_id="vault.transaction")),
        parent_authorities=cast(Any, object()),
        optional_definitions=(definition,),
        optional_local_executors=(((definition,), _CountingExecutor()),),
    )
    factory.bind_worker_read_limit(effective_config.budgets.max_parallel_reads)
    expanded_session_config = effective_config.model_copy(
        update={"budgets": effective_config.budgets.model_copy(update={"max_parallel_reads": 8})}
    )

    def root_kernel(
        session_id: str,
        turn_id: str,
        run_id: str,
        requested_parallel_reads: int,
        run_effective_config: HarnessConfig = effective_config,
    ) -> tuple[UnifiedToolKernel, int]:
        command = StartTurnCommand(
            workspace_id="ws_test",
            session_id=session_id,
            turn_id=turn_id,
            idempotency_key=f"idem-{run_id}",
            input_blocks=({"type": "text", "text": run_id},),
            run_config={
                "provider": "codex",
                "model": "gpt-test",
                "permissionMode": "normal",
                "budgets": {
                    "maxModelRounds": 8,
                    "maxToolCalls": 8,
                    "maxParallelReads": requested_parallel_reads,
                    "maxWallTimeMs": 60_000,
                    "maxArtifactBytes": 8_192,
                },
            },
            effective_config=run_effective_config,
            effective_config_fingerprint=canonical_json_sha256(run_effective_config.model_dump(mode="json")),
        )
        state = RunState(
            workspace_id="ws_test",
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            lineage=AgentLineage.root(run_id),
        )
        components = factory.build(command, state)
        return (
            cast(
                UnifiedToolKernel,
                components.tool_kernel_factory(BudgetLedger(components.budget, started_at=NOW)),
            ),
            components.budget.max_parallel_reads,
        )

    first_root, first_limit = root_kernel("ses_a", "turn_a", "run_root_a", 8)
    expanded_root, expanded_limit = root_kernel(
        "ses_b",
        "turn_b",
        "run_root_b",
        8,
        expanded_session_config,
    )
    narrowed_root, narrowed_limit = root_kernel("ses_c", "turn_c", "run_root_c", 2)
    root_registry = factory.registry_for_run("run_root_a")
    assert root_registry is not None
    record = replace(
        _record(definition, root_registry.snapshot_hash),
        run_id="run_child_a",
        root_run_id="run_root_a",
        parent_run_id="run_root_a",
        ancestor_run_ids=("run_root_a",),
        session_id="ses_a",
        turn_id="turn_a",
        workspace_id="ws_test",
    )
    child_components = factory.build_child(_execution(record), _state(record))
    child = cast(
        UnifiedToolKernel,
        child_components.tool_kernel_factory(BudgetLedger(child_components.budget, started_at=NOW)),
    )

    assert (first_limit, expanded_limit, narrowed_limit, child_components.budget.max_parallel_reads) == (6, 6, 2, 6)
    with pytest.raises(ValueError, match="restart the Worker"):
        factory.bind_worker_read_limit(7)
    for kernel in (first_root, expanded_root, narrowed_root, child):
        assert cast(Any, kernel)._scheduler._effect_gate is factory._effect_gate
        assert cast(Any, kernel)._locks is factory._lock_pool


def test_prepared_child_components_bind_the_complete_durable_subagent_record() -> None:
    definition = vault_transaction_definition()
    registry = ToolRegistry(
        "root-run",
        (definition,),
        preflight_provider_ids=frozenset({cast(str, definition.preflight_provider)}),
    )
    factory = _CapturingFactory(registry)
    record = _record(definition, registry.snapshot_hash, permission=PermissionMode.READ_ONLY)
    execution = _execution(record)
    token = SimpleNamespace(
        config=execution.run_config,
        inputs=object(),
        effective_config=HarnessConfig(),
        definitions=(definition,),
        budget=object(),
        scope=record.effective_scope,
        permission=record.permission_mode,
    )

    factory.build_prepared_child(
        execution,
        _state(record),
        PreparedRunComponents(token=token, durable_snapshot={}),
    )

    assert factory.captured["prepared_capabilities"] is token
    assert factory.captured["child_record"] is record


def test_child_components_fail_closed_on_registry_drift_or_ungranted_tool() -> None:
    definition = vault_transaction_definition()
    registry = ToolRegistry(
        "root-run",
        (definition,),
        preflight_provider_ids=frozenset({cast(str, definition.preflight_provider)}),
    )
    factory = _CapturingFactory(registry)
    execution = SimpleNamespace(
        run_config={"provider": "codex", "model": "gpt-test"},
        context=SimpleNamespace(content={}),
        tool_scope=SimpleNamespace(
            registry_snapshot_hash="sha256:" + "0" * 64,
            allowed_versions={definition.name: (definition.version,)},
        ),
        record=SimpleNamespace(run_id="run_child", root_run_id="run_root"),
    )

    try:
        factory.build_child(execution, cast(Any, SimpleNamespace(run_id="run_child")))
    except ValueError as error:
        assert "different root Registry" in str(error)
    else:
        raise AssertionError("child Registry drift was accepted")

    execution.tool_scope.registry_snapshot_hash = registry.snapshot_hash
    execution.tool_scope.allowed_versions = {"vault.missing": ("1",)}
    try:
        factory.build_child(execution, cast(Any, SimpleNamespace(run_id="run_child")))
    except ValueError as error:
        assert "absent from the root Registry" in str(error)
    else:
        raise AssertionError("ungranted child Tool was accepted")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revocation", "expected_code"),
    [
        ("terminal", "subagent_parent_scope_revoked"),
        ("narrowed", "subagent_parent_scope_revoked"),
        ("permission", "subagent_parent_scope_revoked"),
        ("expired", "subagent_parent_scope_revoked"),
        ("unavailable", "subagent_parent_scope_unavailable"),
    ],
)
async def test_production_child_kernel_rechecks_live_parent_before_local_side_effect(
    tmp_path: Path,
    revocation: str,
    expected_code: str,
) -> None:
    definition = _side_effect_definition()
    registry = ToolRegistry("root-run", (definition,))
    record = _record(definition, registry.snapshot_hash)
    execution = _execution(record)
    state = _state(record)
    parent = ParentRunAuthority(
        workspace_id=record.workspace_id,
        session_id=record.session_id,
        turn_id=record.turn_id,
        lineage=AgentLineage.root(record.parent_run_id),
        permission_mode=PermissionMode.NORMAL,
        effective_scope=record.effective_scope,
        tool_definitions=(definition,),
        registry_snapshot_hash=registry.snapshot_hash,
        remaining_budget=record.budget_limit,
        deadline_at=record.deadline_at,
        context={},
        run_config=execution.run_config,
        can_spawn_children=True,
        active=True,
    )
    authorities = _MutableParentAuthorities(parent)
    executor = _CountingExecutor()
    journal = _RecordingJournal()
    audit = _RecordingPolicyAudit()
    clock = ManualClock(NOW)
    unit_of_work = InMemoryUnitOfWorkFactory()
    effective_config = HarnessConfig.model_validate(
        {
            "policy": {"workspace_trusted": True, "read_only": False},
        }
    )
    factory = ProductionRunComponentsFactory(
        workspace_id=record.workspace_id,
        clock=clock,
        ids=DeterministicIdGenerator(),
        gateway_factory=lambda _settings: cast(Any, object()),
        default_config=effective_config,
        approvals=ApprovalManager(unit_of_work=unit_of_work, clock=clock),
        policy_audit=audit,
        journal=journal,
        artifacts=LocalArtifactStore(tmp_path / "artifacts", workspace_id=record.workspace_id),
        local_transaction=cast(Any, SimpleNamespace(provider_id="vault.transaction")),
        parent_authorities=authorities,
        optional_definitions=(definition,),
        optional_local_executors=(((definition,), executor),),
    )
    factory._registries[record.root_run_id] = registry
    factory._effective_configs[record.root_run_id] = effective_config

    components = factory.build_child(execution, state)
    kernel = components.tool_kernel_factory(BudgetLedger(components.budget, started_at=NOW))
    assert isinstance(kernel, UnifiedToolKernel)
    assert authorities.calls == []

    if revocation == "terminal":
        authorities.authority = replace(parent, active=False)
    elif revocation == "narrowed":
        authorities.authority = replace(
            parent,
            effective_scope=replace(
                parent.effective_scope,
                allowed_tools=frozenset(),
                denied_tools=frozenset({definition.name}),
                root_capabilities=frozenset(),
            ),
        )
    elif revocation == "permission":
        authorities.authority = replace(parent, permission_mode=PermissionMode.READ_ONLY)
    elif revocation == "expired":
        authorities.authority = replace(parent, deadline_at=NOW)
    else:
        authorities.available = False
    arguments = {"value": 7}
    call = ToolCall(
        tool_call_id=f"call_{revocation}",
        run_id=record.run_id,
        workspace_id=record.workspace_id,
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=f"idem_{revocation}",
        deadline=record.deadline_at,
        lineage=record.lineage,
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )

    executions = await kernel.execute_batch((call,), ManualCancellationToken())

    result = executions[0].result
    assert result.status is ToolResultStatus.DENIED
    assert result.error is not None
    assert result.error.code == expected_code
    assert authorities.calls == [record.parent_run_id]
    assert executor.calls == []
    assert (journal.gets, journal.starts, journal.completes, journal.unknowns) == (0, 0, 0, 0)
    assert [item.reason_code for item in audit.records] == [expected_code]
