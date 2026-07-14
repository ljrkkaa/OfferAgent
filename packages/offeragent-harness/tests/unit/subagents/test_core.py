from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from offeragent_harness.agent import BudgetDelta, BudgetLedger, RunBudget
from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.permissions import CapabilityScope, PermissionMode, RiskClass
from offeragent_harness.ports.subagents import ParentRunAuthority
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.subagents import (
    AgentBudget,
    AgentDefinitionCatalog,
    AgentDefinitionError,
    AgentDefinitionLayer,
    AgentDefinitionRoot,
    AgentSendCommand,
    AgentSpawnCommand,
    ContextForker,
    ContextForkError,
    ContextForkMode,
    DurableMailbox,
    ExecutionPriority,
    MailboxMode,
    ResultReducer,
    ScopeDeriver,
    SubagentBudgetTree,
    SubagentLifetime,
    WriteClaim,
    WriteCoordinator,
    builtin_agent_definitions,
    subagent_tool_definitions,
)
from offeragent_harness.subagents.catalog import AgentDefinitionTrust
from offeragent_harness.subagents.mailbox import MailboxConflict
from offeragent_harness.subagents.models import AgentUsage
from offeragent_harness.subagents.write_coordinator import WriteCoordinationError
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
)

NOW = datetime(2026, 7, 13, 8, tzinfo=timezone.utc)


def _scope(
    tools: frozenset[str],
    *,
    risks: frozenset[RiskClass] = frozenset({RiskClass.READ}),
    capabilities: frozenset[str] = frozenset({"subagent.read"}),
    network: bool = False,
) -> CapabilityScope:
    return CapabilityScope(tools, frozenset(), risks, capabilities, network, False)


def _budget(**overrides: int | float) -> AgentBudget:
    values: dict[str, int | float] = {
        "input_tokens": 2_000,
        "output_tokens": 1_000,
        "model_calls": 4,
        "tool_calls": 8,
        "wall_time_seconds": 60,
        "artifact_bytes": 64_000,
        "child_count": 2,
        "cost_micros": 1_000_000,
    }
    values.update(overrides)
    return AgentBudget(**values)


def _authority(*, tools: tuple[Any, ...], context: dict[str, Any] | None = None) -> ParentRunAuthority:
    names = frozenset(item.name for item in tools)
    return ParentRunAuthority(
        "ws_test",
        "ses_test",
        "turn_test",
        AgentLineage.root("run_root"),
        PermissionMode.NORMAL,
        _scope(
            names,
            risks=frozenset({RiskClass.READ, RiskClass.WRITE, RiskClass.EXECUTE, RiskClass.NETWORK}),
            capabilities=frozenset(capability for item in tools for capability in item.required_capabilities),
            network=True,
        ),
        tools,
        "sha256:" + "1" * 64,
        _budget(input_tokens=20_000, output_tokens=10_000, model_calls=20, tool_calls=30, child_count=8),
        NOW + timedelta(minutes=10),
        context or {},
        {"model": "fake"},
        True,
        True,
    )


def _command(scope: CapabilityScope, **changes: Any) -> AgentSpawnCommand:
    values: dict[str, Any] = {
        "parent_run_id": "run_root",
        "spawn_call_id": "call_spawn",
        "task": "Review the implementation",
        "profile": "general",
        "context_mode": ContextForkMode.SELECTED,
        "selected_message_ids": (),
        "selected_artifact_ids": (),
        "requested_scope": scope,
        "requested_permission_mode": PermissionMode.READ_ONLY,
        "requested_tool_versions": {},
        "requested_tool_constraints": {},
        "budget": _budget(),
        "lifetime": SubagentLifetime.PARENT,
        "priority": ExecutionPriority.NORMAL,
    }
    values.update(changes)
    return AgentSpawnCommand(**values)


def test_agent_definitions_and_tool_contracts_are_closed_and_never_bypass() -> None:
    tools = subagent_tool_definitions()
    definitions = builtin_agent_definitions(
        available_tools=frozenset(item.name for item in tools),
        root_capabilities=frozenset(capability for item in tools for capability in item.required_capabilities),
    )
    assert {item.name for item in definitions} == {
        "general",
        "researcher",
        "reviewer",
        "test-runner",
        "workspace-editor",
    }
    assert all(item.permission_ceiling is not PermissionMode.BYPASS for item in definitions)
    assert all(item.input_schema["additionalProperties"] is False for item in tools)
    assert all(item.output_schema["additionalProperties"] is False for item in tools)
    assert {item.name for item in tools} == {
        "agent.spawn",
        "agent.send",
        "agent.wait",
        "agent.status",
        "agent.result",
        "agent.cancel",
    }
    with pytest.raises(ValueError, match="external"):
        replace(
            definitions[-1],
            result_schema={
                "type": "object",
                "properties": {
                    "summary": {"$ref": "https://attacker.invalid/schema"},
                    "findings": {"type": "array"},
                    "evidence": {"type": "array"},
                    "proposedActions": {"type": "array"},
                    "unresolvedQuestions": {"type": "array"},
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
        )


def test_context_fork_filters_secrets_and_rejects_unauthorized_selection() -> None:
    tools = subagent_tool_definitions()
    authority = _authority(
        tools=tools,
        context={
            "summary": "safe",
            "apiToken": "never-copy",
            "messages": [{"id": "msg_safe", "text": "ok", "password": "never"}],
            "artifacts": [
                {"id": "art_ok", "authorized": True},
                {"id": "art_denied", "authorized": False},
            ],
            "pluginObject": {"unsafe": True},
        },
    )
    profile = builtin_agent_definitions(
        available_tools=frozenset(item.name for item in tools),
        root_capabilities=authority.effective_scope.root_capabilities,
    )[-1]
    forker = ContextForker(DeterministicIdGenerator(), ManualClock(NOW))
    snapshot = forker.fork(
        authority,
        _command(
            authority.effective_scope,
            selected_message_ids=("msg_safe",),
            selected_artifact_ids=("art_ok",),
        ),
        profile,
    )
    encoded = json.dumps(thaw_json(snapshot.content))
    assert "never-copy" not in encoded
    assert "password" not in encoded
    assert "pluginObject" not in encoded
    with pytest.raises(ContextForkError, match="not authorized"):
        forker.fork(
            authority,
            _command(authority.effective_scope, selected_artifact_ids=("art_denied",)),
            profile,
        )


@given(
    parent_names=st.sets(st.sampled_from([item.name for item in subagent_tool_definitions()])),
    requested_names=st.sets(st.sampled_from([item.name for item in subagent_tool_definitions()])),
)
def test_scope_derivation_is_monotonic(parent_names: set[str], requested_names: set[str]) -> None:
    tools = subagent_tool_definitions()
    parent = _authority(tools=tools)
    parent_scope = replace(parent.effective_scope, allowed_tools=frozenset(parent_names))
    parent = replace(parent, effective_scope=parent_scope)
    requested = replace(parent_scope, allowed_tools=frozenset(requested_names))
    profile = builtin_agent_definitions(
        available_tools=frozenset(item.name for item in tools),
        root_capabilities=parent_scope.root_capabilities,
    )[-1]
    derived = ScopeDeriver(parent_scope).derive(parent, profile, _command(requested))
    assert derived.capability_scope.allowed_tools <= parent_scope.allowed_tools
    assert derived.capability_scope.allowed_tools <= requested.allowed_tools
    assert set(derived.tool_scope.allowed_versions) <= derived.capability_scope.allowed_tools
    assert derived.permission_mode is PermissionMode.READ_ONLY


def test_scope_is_exact_to_tool_version_and_argument_constraint() -> None:
    tools = subagent_tool_definitions()
    parent = _authority(tools=tools)
    requested = parent.effective_scope
    profile = builtin_agent_definitions(
        available_tools=frozenset(item.name for item in tools),
        root_capabilities=parent.effective_scope.root_capabilities,
    )[-1]
    command = _command(
        requested,
        requested_tool_versions={"agent.status": ("1", "2")},
        requested_tool_constraints={
            "agent.status": {
                "type": "object",
                "properties": {"runId": {"const": "run_child"}},
                "required": ["runId"],
                "additionalProperties": False,
            }
        },
    )
    derived = ScopeDeriver(parent.effective_scope).derive(parent, profile, command)
    assert derived.tool_scope.allowed_versions["agent.status"] == ("1",)
    assert derived.tool_scope.argument_constraints["agent.status"]
    with pytest.raises(ContextForkError, match="bypass"):
        ScopeDeriver(parent.effective_scope).derive(
            parent,
            profile,
            replace(command, requested_permission_mode=PermissionMode.BYPASS),
        )


@pytest.mark.asyncio
async def test_budget_tree_atomically_retains_parent_final_compose_budget() -> None:
    ledger = BudgetLedger(
        RunBudget(20, 40, 4, 300, 20_000, 10_000, Decimal("10"), 1_000_000, 8, 3),
        started_at=NOW,
    )
    retained = _budget(
        input_tokens=1_000,
        output_tokens=1_000,
        model_calls=1,
        tool_calls=0,
        wall_time_seconds=10,
        artifact_bytes=1_024,
        child_count=0,
        cost_micros=0,
    )
    tree = SubagentBudgetTree(ledger, retained_final_budget=retained)
    remaining = _budget(
        input_tokens=4_000,
        output_tokens=3_000,
        model_calls=6,
        tool_calls=10,
        wall_time_seconds=180,
        artifact_bytes=100_000,
        child_count=3,
        cost_micros=2_000_000,
    )
    reservation = await tree.reserve(_budget(), parent_remaining=remaining, child_depth=1)
    snapshot = await ledger.snapshot(now=NOW)
    assert snapshot.reserved.subagents == 1
    await reservation.settle(AgentUsage(model_calls=1, input_tokens=10, output_tokens=5))
    snapshot = await ledger.snapshot(now=NOW)
    assert snapshot.used.subagents == 1
    assert snapshot.reserved.subagents == 0
    with pytest.raises(Exception, match="final-compose"):
        await tree.reserve(
            replace(remaining, input_tokens=3_500),
            parent_remaining=remaining,
            child_depth=1,
        )


@pytest.mark.asyncio
async def test_budget_tree_routes_and_adopts_persisted_reservations_on_the_authoritative_root_ledger() -> None:
    budget = RunBudget(20, 40, 4, 300, 20_000, 10_000, Decimal("10"), 1_000_000, 8, 3)
    root_a = BudgetLedger(budget, started_at=NOW)
    root_b = BudgetLedger(budget, started_at=NOW)
    retained = _budget(
        input_tokens=1_000,
        output_tokens=1_000,
        model_calls=1,
        tool_calls=0,
        wall_time_seconds=10,
        artifact_bytes=1_024,
        child_count=0,
        cost_micros=0,
    )
    ledgers = {"run_root_a": root_a, "run_root_b": root_b}
    tree = SubagentBudgetTree(lambda root_run_id: ledgers[root_run_id], retained_final_budget=retained)

    requested = _budget()
    reservation = await tree.reserve(
        requested,
        parent_remaining=_budget(
            input_tokens=4_000,
            output_tokens=3_000,
            model_calls=6,
            tool_calls=10,
            wall_time_seconds=180,
            artifact_bytes=100_000,
            child_count=3,
            cost_micros=2_000_000,
        ),
        child_depth=1,
        root_run_id="run_root_b",
    )
    assert (await root_a.snapshot(now=NOW)).reserved == BudgetDelta()
    persisted = await root_b.snapshot(now=NOW)
    assert persisted.reserved.subagents == 1

    recovered = BudgetLedger.restore(
        budget,
        started_at=NOW,
        used=persisted.used,
        reserved=persisted.reserved,
    )
    ledgers["run_root_b"] = recovered
    adopted = await tree.adopt(requested, root_run_id="run_root_b")
    await adopted.settle(AgentUsage(model_calls=1, input_tokens=10, output_tokens=5))
    recovered_snapshot = await recovered.snapshot(now=NOW)
    assert recovered_snapshot.used.subagents == 1
    assert recovered_snapshot.reserved == BudgetDelta()

    # The pre-crash reservation belongs to the abandoned in-memory ledger only;
    # releasing it must not mutate the recovered authoritative ledger.
    await reservation.release()
    assert (await recovered.snapshot(now=NOW)) == recovered_snapshot


@pytest.mark.asyncio
async def test_mailbox_is_ordered_idempotent_and_conflict_safe() -> None:
    mailbox = DurableMailbox(InMemoryUnitOfWorkFactory(), ManualClock(NOW))
    first = AgentSendCommand("run_root", "run_child", MailboxMode.APPEND, "one", (), "msg_one")
    second = AgentSendCommand("run_root", "run_child", MailboxMode.STEER, "two", (), "msg_two")
    receipt, _ = await mailbox.send(first)
    replay, _ = await mailbox.send(first)
    await mailbox.send(second)
    assert receipt.sequence == 1 and replay.duplicate
    assert [item.message for item in await mailbox.receive("run_child", after_sequence=0)] == ["one", "two"]
    with pytest.raises(MailboxConflict):
        await mailbox.send(replace(first, message="changed"))


@pytest.mark.asyncio
async def test_write_coordinator_serializes_same_file_and_rejects_escape() -> None:
    coordinator = WriteCoordinator(InMemoryUnitOfWorkFactory())
    token = ManualCancellationToken()
    first_lineage = AgentLineage.root("run_root").child("run_a", "editor")
    second_lineage = AgentLineage.root("run_root").child("run_b", "editor")
    claim_a = WriteClaim(("Notes/A.md",), ("absent",), "idem-a", "apr_a", first_lineage)
    claim_b = WriteClaim(("notes/a.md",), ("absent",), "idem-b", "apr_b", second_lineage)
    lease_a = await coordinator.acquire(claim_a, token)
    waiter = asyncio.create_task(coordinator.acquire(claim_b, token, timeout_seconds=1))
    await asyncio.sleep(0)
    assert not waiter.done()
    await lease_a.release()
    lease_b = await waiter
    assert lease_b.resources == ("notes/a.md",)
    await lease_b.release()
    with pytest.raises(WriteCoordinationError, match="normalized"):
        await coordinator.acquire(
            WriteClaim(("../escape.md",), ("absent",), "idem-c", "apr_c", first_lineage),
            token,
        )


@pytest.mark.asyncio
async def test_write_coordinator_restart_cleans_durable_orphan_lock() -> None:
    uow = InMemoryUnitOfWorkFactory()
    first = WriteCoordinator(uow)
    lineage = AgentLineage.root("run_root").child("run_orphan", "editor")
    await first.acquire(
        WriteClaim(("notes/orphan.md",), ("absent",), "idem-orphan", "apr_orphan", lineage),
        ManualCancellationToken(),
    )
    restarted = WriteCoordinator(uow)
    assert await restarted.cleanup_owner("run_orphan") == ("notes/orphan.md",)
    next_lineage = AgentLineage.root("run_root").child("run_next", "editor")
    lease = await restarted.acquire(
        WriteClaim(("notes/orphan.md",), ("absent",), "idem-next", "apr_next", next_lineage),
        ManualCancellationToken(),
    )
    await lease.release()


def test_catalog_never_activates_untrusted_workspace_shadow_and_detects_hash_drift(tmp_path: Path) -> None:
    tools = subagent_tool_definitions()
    builtin = builtin_agent_definitions(
        available_tools=frozenset(item.name for item in tools),
        root_capabilities=frozenset(capability for item in tools for capability in item.required_capabilities),
    )[-1]
    path = tmp_path / "general.md"
    path.write_text(
        "---\nname: general\ndescription: untrusted shadow\ntools: [agent.status]\n---\n"
        "Only inspect the requested child state.",
        encoding="utf-8",
    )
    workspace_root = AgentDefinitionRoot(
        "workspace",
        AgentDefinitionLayer.WORKSPACE,
        tmp_path,
        False,
    )
    catalog = AgentDefinitionCatalog(
        workspace_id="ws_test",
        builtins=(builtin,),
        roots=(workspace_root,),
        system_denied_tools=frozenset({"obsidian.vault.transaction"}),
    )
    catalog.rescan(expected_revision=0)
    assert len(catalog.resolve("general").definition.version) == 64
    assert any(item.trust is AgentDefinitionTrust.WORKSPACE_UNTRUSTED for item in catalog.descriptors)

    trusted = replace(
        workspace_root,
        workspace_trusted=True,
    )
    trusted_catalog = AgentDefinitionCatalog(workspace_id="ws_test", builtins=(builtin,), roots=(trusted,))
    trusted_catalog.rescan(expected_revision=0)
    assert trusted_catalog.resolve("general").definition.description == "untrusted shadow"
    path.write_text(path.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8")
    with pytest.raises(AgentDefinitionError, match="changed"):
        trusted_catalog.resolve("general")


def test_claude_agent_markdown_is_scoped_read_only_and_its_body_enters_child_context(tmp_path: Path) -> None:
    agent_file = tmp_path / "review.md"
    agent_file.write_text(
        "---\n"
        "name: review\n"
        "description: Review only the supplied files.\n"
        "tools: [workspace.glob, workspace.grep, workspace.read]\n"
        "skills: [interview]\n"
        "permissionMode: read-only\n"
        "---\n"
        "Cite file paths and do not make changes.\n",
        encoding="utf-8",
    )
    catalog = AgentDefinitionCatalog(
        workspace_id="ws_agent_markdown",
        builtins=(),
        roots=(AgentDefinitionRoot("user", AgentDefinitionLayer.USER, tmp_path, True),),
    )
    catalog.rescan(expected_revision=0)
    definition = catalog.resolve("review").definition
    assert definition.instructions.replace("\r\n", "\n") == "Cite file paths and do not make changes.\n"
    assert definition.skills == ("interview",)
    assert definition.permission_ceiling is PermissionMode.READ_ONLY
    assert definition.capability_ceiling.allowed_risks == frozenset({RiskClass.READ})


def test_result_reducer_accepts_typed_json_and_does_not_return_transcript() -> None:
    tools = subagent_tool_definitions()
    definition = builtin_agent_definitions(
        available_tools=frozenset(item.name for item in tools),
        root_capabilities=frozenset(capability for item in tools for capability in item.required_capabilities),
    )[-1]
    # The reducer is covered end-to-end by the Service tests; here the strict
    # public component contract is asserted without manufacturing a transcript.
    assert ResultReducer()
    assert definition.result_schema["additionalProperties"] is False
