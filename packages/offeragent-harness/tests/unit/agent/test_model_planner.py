from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

import pytest

from offeragent_harness.agent.budgets import BudgetDelta, BudgetLedger, RunBudget
from offeragent_harness.agent.context_manager import (
    ContextBudget,
    ContextFragment,
    ContextInputs,
    ContextLayer,
    ContextManager,
    ContextProjection,
    ContextVisibilityPolicy,
)
from offeragent_harness.agent.model_planner import (
    AgentStepCatalog,
    ModelInvalidOutput,
    ModelPlanner,
    ModelProviderFailure,
    ModelStreamProtocolError,
    PlannerModelConfig,
    RunCallFence,
    SchemaRepairFailed,
    SchemaRepairUnavailable,
    collect_structured_response,
)
from offeragent_harness.agent.planner import PlanningAttemptOutcome
from offeragent_harness.agent.state import RunState
from offeragent_harness.models import (
    FrozenJsonObject,
    ModelCitation,
    ModelContinuation,
    ModelError,
    ModelEvent,
    ModelEventKind,
    ModelFinishReason,
    ModelHostedSearch,
    ModelHostedSearchPhase,
    ModelHostedTool,
    ModelRequest,
    ModelUsage,
    freeze_json,
    thaw_json,
)
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import ModelGateway, Sensitivity
from offeragent_harness.runtime.plugin_tools import plugin_tool_definitions
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.skills import skill_tool_definitions
from offeragent_harness.testing import (
    ControlledBarrier,
    DeterministicIdGenerator,
    FakeRunCancelled,
    ManualCancellationToken,
    ManualClock,
    ModelScriptStep,
    ScriptedModelEvent,
    ScriptedModelGateway,
)
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffectClass,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
    canonical_json_bytes,
    canonical_json_sha256,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
USAGE = ModelUsage(11, 7, 2, 1, Decimal("0.03"), "USD")


def _definition(name: str = "workspace.read", version: str = "1") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version=version,
        description="Read a canonical Vault path",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "options": {"$ref": "#/$defs/options"},
            },
            "required": ["path"],
            "additionalProperties": False,
            "$defs": {
                "options": {
                    "type": "object",
                    "properties": {"includeHash": {"type": "boolean"}},
                    "additionalProperties": False,
                }
            },
        },
        output_schema={},
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.READ,
        side_effect_class=SideEffectClass.READ,
        required_capabilities=frozenset({"workspace.read"}),
        concurrency_safe=True,
        idempotent=True,
        retryable=True,
        timeout_ms=2_500,
        output_limit_bytes=4_096,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def _context() -> ContextManager:
    return ContextManager(
        system_rules=("Only use the typed tool catalog.",),
        inputs=ContextInputs(
            user_input=(
                ContextFragment(
                    "user-1",
                    ContextLayer.USER_INPUT,
                    "read notes/a.md",
                    Sensitivity.PUBLIC,
                ),
            )
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget.generous_default(),
    )


def _state() -> RunState:
    return RunState("ws", "session", "turn", "run", AgentLineage.root("run"))


def _budget(*, rounds: int = 4) -> BudgetLedger:
    return BudgetLedger(
        RunBudget(
            max_model_rounds=rounds,
            max_tool_calls=10,
            max_parallel_reads=4,
            max_wall_seconds=60,
            max_input_tokens=1_000,
            max_output_tokens=1_000,
            max_cost=Decimal("10"),
            max_artifact_bytes=100_000,
            max_subagents=2,
        ),
        started_at=NOW,
    )


def _planner(
    gateway: ModelGateway,
    *,
    catalog: AgentStepCatalog | None = None,
    ids: DeterministicIdGenerator | None = None,
    budget: BudgetLedger | None = None,
    context_manager: ContextManager | None = None,
    hosted_search: bool = False,
) -> ModelPlanner:
    return ModelPlanner(
        gateway=gateway,
        context_manager=context_manager or _context(),
        catalog=catalog or AgentStepCatalog((_definition(),), max_calls=3),
        config=PlannerModelConfig(
            "scripted-model",
            512,
            model_instructions="catalog-owned model baseline",
            use_responses_lite=True,
            seed=17,
            hosted_tools=(ModelHostedTool.WEB_SEARCH,) if hosted_search else (),
        ),
        clock=ManualClock(NOW),
        ids=ids or DeterministicIdGenerator(),
        budget=budget or _budget(),
    )


def test_memory_context_enrichment_changes_messages_but_not_tool_catalog_or_output_schema() -> None:
    planner = _planner(ScriptedModelGateway(()))
    before = planner.create_request(_state())
    assert before.model_instructions == "catalog-owned model baseline"
    assert before.use_responses_lite is True
    memory = ContextFragment(
        "memory:workspace:mem_planner",
        ContextLayer.MEMORY,
        "planner local Memory evidence",
        Sensitivity.WORKSPACE,
        source_refs=("memory:workspace:mem_planner",),
        content_hash="sha256:" + "c" * 64,
    )

    planner.add_memory_context((memory, memory))
    after = planner.create_request(_state())

    assert "planner local Memory evidence" not in repr(before.messages)
    assert repr(after.messages).count("planner local Memory evidence") == 1
    assert after.output_schema == before.output_schema
    assert thaw_json(after.metadata)["toolCatalogHash"] == thaw_json(before.metadata)["toolCatalogHash"]


def test_catalog_authorized_hosted_search_is_explicit_on_every_planner_request() -> None:
    unsupported = _planner(ScriptedModelGateway(())).create_request(_state())
    supported = _planner(ScriptedModelGateway(()), hosted_search=True).create_request(_state())

    assert unsupported.hosted_tools == ()
    assert supported.hosted_tools == (ModelHostedTool.WEB_SEARCH,)


@pytest.mark.asyncio
async def test_planner_preserves_unique_hosted_search_citations_on_the_final_step() -> None:
    request = _planner(ScriptedModelGateway(()), hosted_search=True).create_request(_state())
    citation = ModelCitation(
        provider_id="codex-subscription",
        model=request.model,
        request_id=request.request_id,
        url="https://example.com/source",
        title="Example source",
        start_index=5,
        end_index=12,
    )
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep(
                expected_request=request,
                events=(
                    ScriptedModelEvent(ModelEvent(request.request_id, 1, ModelEventKind.STARTED)),
                    ScriptedModelEvent(
                        ModelEvent(
                            request.request_id,
                            2,
                            ModelEventKind.HOSTED_SEARCH,
                            hosted_search=ModelHostedSearch("search_1", ModelHostedSearchPhase.STARTED),
                        )
                    ),
                    ScriptedModelEvent(
                        ModelEvent(
                            request.request_id,
                            3,
                            ModelEventKind.HOSTED_SEARCH,
                            hosted_search=ModelHostedSearch("search_1", ModelHostedSearchPhase.COMPLETED),
                        )
                    ),
                    ScriptedModelEvent(ModelEvent(request.request_id, 4, ModelEventKind.CITATION, citation=citation)),
                    ScriptedModelEvent(
                        ModelEvent(
                            request.request_id,
                            5,
                            ModelEventKind.STRUCTURED_OUTPUT,
                            data={"requiresWriteOutcome": False, "calls": [], "finalResponse": "Cited answer"},
                        )
                    ),
                    ScriptedModelEvent(ModelEvent(request.request_id, 6, ModelEventKind.USAGE, usage=USAGE)),
                    ScriptedModelEvent(
                        ModelEvent(
                            request.request_id, 7, ModelEventKind.COMPLETED, finish_reason=ModelFinishReason.STOP
                        )
                    ),
                ),
            ),
        )
    )
    planner = _planner(gateway, hosted_search=True)

    step = await planner.plan(_state(), ManualCancellationToken())

    assert step.final_response == "Cited answer"
    assert step.citations == (citation,)


@pytest.mark.asyncio
async def test_structured_collector_rejects_undeclared_or_unfinished_hosted_search() -> None:
    unsupported = _planner(ScriptedModelGateway(())).create_request(_state())
    undeclared_gateway = ScriptedModelGateway(
        (
            ModelScriptStep(
                expected_request=unsupported,
                events=(
                    ScriptedModelEvent(ModelEvent(unsupported.request_id, 1, ModelEventKind.STARTED)),
                    ScriptedModelEvent(
                        ModelEvent(
                            unsupported.request_id,
                            2,
                            ModelEventKind.HOSTED_SEARCH,
                            hosted_search=ModelHostedSearch("search_1", ModelHostedSearchPhase.STARTED),
                        )
                    ),
                ),
            ),
        )
    )
    with pytest.raises(ModelStreamProtocolError, match="undeclared hosted search"):
        await collect_structured_response(undeclared_gateway, unsupported, ManualCancellationToken())

    supported = _planner(ScriptedModelGateway(()), hosted_search=True).create_request(_state())
    unfinished_gateway = ScriptedModelGateway(
        (
            ModelScriptStep(
                expected_request=supported,
                events=(
                    ScriptedModelEvent(ModelEvent(supported.request_id, 1, ModelEventKind.STARTED)),
                    ScriptedModelEvent(
                        ModelEvent(
                            supported.request_id,
                            2,
                            ModelEventKind.HOSTED_SEARCH,
                            hosted_search=ModelHostedSearch("search_2", ModelHostedSearchPhase.STARTED),
                        )
                    ),
                    ScriptedModelEvent(
                        ModelEvent(
                            supported.request_id,
                            3,
                            ModelEventKind.STRUCTURED_OUTPUT,
                            data={"requiresWriteOutcome": False, "calls": [], "finalResponse": "answer"},
                        )
                    ),
                    ScriptedModelEvent(ModelEvent(supported.request_id, 4, ModelEventKind.USAGE, usage=USAGE)),
                    ScriptedModelEvent(
                        ModelEvent(
                            supported.request_id,
                            5,
                            ModelEventKind.COMPLETED,
                            finish_reason=ModelFinishReason.STOP,
                        )
                    ),
                ),
            ),
        )
    )
    with pytest.raises(ModelStreamProtocolError, match="unfinished hosted search"):
        await collect_structured_response(unfinished_gateway, supported, ManualCancellationToken())


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ("model", "request"))
async def test_structured_collector_rejects_hosted_citation_identity_mismatch(mismatch: str) -> None:
    request = _planner(ScriptedModelGateway(()), hosted_search=True).create_request(_state())
    citation = ModelCitation(
        provider_id="codex-subscription",
        model="another-model" if mismatch == "model" else request.model,
        request_id="another-request" if mismatch == "request" else request.request_id,
        url="https://example.com/source",
        title="Mismatched source",
        start_index=0,
        end_index=1,
    )
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep(
                expected_request=request,
                events=(
                    ScriptedModelEvent(ModelEvent(request.request_id, 1, ModelEventKind.STARTED)),
                    ScriptedModelEvent(
                        ModelEvent(
                            request.request_id,
                            2,
                            ModelEventKind.HOSTED_SEARCH,
                            hosted_search=ModelHostedSearch("search_1", ModelHostedSearchPhase.STARTED),
                        )
                    ),
                    ScriptedModelEvent(
                        ModelEvent(
                            request.request_id,
                            3,
                            ModelEventKind.HOSTED_SEARCH,
                            hosted_search=ModelHostedSearch("search_1", ModelHostedSearchPhase.COMPLETED),
                        )
                    ),
                    ScriptedModelEvent(ModelEvent(request.request_id, 4, ModelEventKind.CITATION, citation=citation)),
                ),
            ),
        )
    )

    with pytest.raises(ModelStreamProtocolError, match="citation identity mismatch"):
        await collect_structured_response(gateway, request, ManualCancellationToken())


def test_estimated_normal_overflow_uses_reference_projection_before_provider_call() -> None:
    evidence = ToolResult(
        tool_call_id="large-read",
        status=ToolResultStatus.SUCCEEDED,
        data={"body": "x" * 8_000, "contentHash": "sha256:" + "d" * 64},
        user_visible_summary="large evidence",
        artifact_ids=("artifact-large-read",),
        source_refs=("vault:notes/large.md",),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state={"contentHash": "sha256:" + "e" * 64},
        error=None,
    )
    context = ContextManager(
        system_rules=("Only use verified evidence.",),
        inputs=ContextInputs(
            user_input=(ContextFragment("user", ContextLayer.USER_INPUT, "summarize", Sensitivity.PUBLIC),),
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget(
            max_messages=8,
            max_total_bytes=3_000,
            max_estimated_tokens=2_000,
            max_item_bytes=20_000,
        ),
    )
    state = replace(
        _state(),
        tool_results=(evidence,),
        tool_result_sensitivities={"large-read": ResultSensitivity.WORKSPACE},
    )

    request = _planner(ScriptedModelGateway(()), context_manager=context).create_request(state)

    metadata = thaw_json(request.metadata)
    assert metadata["projection"] == "overflow_references"
    assert metadata["contextOverflowRetry"] is False
    tool = next(message for message in request.messages if message.name == "large-read")
    assert tool.content[0].kind == "tool_result_reference"
    assert '"body"' not in repr(tool)


def _tool_plan(*, extra_call_fields: Mapping[str, Any] | None = None) -> dict[str, Any]:
    call: dict[str, Any] = {
        "name": "workspace.read",
        "version": "1",
        "argumentsJson": json.dumps(
            {"path": "notes/a.md", "options": {"includeHash": True}},
            separators=(",", ":"),
        ),
        "reason": "the user requested this exact note",
    }
    if extra_call_fields is not None:
        call.update(extra_call_fields)
    return {"requiresWriteOutcome": False, "calls": [call], "finalResponse": None}


def _events(
    request: ModelRequest,
    output: Mapping[str, Any],
    *,
    usage: ModelUsage = USAGE,
    finish: ModelFinishReason = ModelFinishReason.STOP,
    continuation: ModelContinuation | None = None,
) -> tuple[ModelEvent, ...]:
    return (
        ModelEvent(request.request_id, 1, ModelEventKind.STARTED),
        ModelEvent(request.request_id, 2, ModelEventKind.STRUCTURED_OUTPUT, data=output),
        ModelEvent(request.request_id, 3, ModelEventKind.USAGE, usage=usage),
        ModelEvent(
            request.request_id,
            4,
            ModelEventKind.COMPLETED,
            finish_reason=finish,
            continuation=continuation,
        ),
    )


def _frozen_object(value: Mapping[str, Any]) -> FrozenJsonObject:
    frozen = freeze_json(value)
    assert isinstance(frozen, FrozenJsonObject)
    return frozen


def _invalid(
    catalog: AgentStepCatalog,
    request: ModelRequest,
    output: Mapping[str, Any],
    *,
    violations: Sequence[str] | None = None,
) -> ModelInvalidOutput:
    raw = _frozen_object(output)
    _normalized, decoded_violations = catalog.decode_model_step(cast(dict[str, Any], thaw_json(raw)))
    return ModelInvalidOutput(
        request.request_id,
        tuple(violations) if violations is not None else decoded_violations,
        raw_output=raw,
        usage=USAGE,
    )


def test_catalog_is_canonical_immutable_and_excludes_harness_security_fields() -> None:
    first = _definition("workspace.read", "1")
    second = _definition("workspace.stat", "2")
    left = AgentStepCatalog((second, first), max_calls=4)
    right = AgentStepCatalog((first, second), max_calls=4)

    assert left.definitions == (first, second)
    assert left.fingerprint == right.fingerprint
    private_catalog = AgentStepCatalog(
        (replace(first, result_sensitivity=ResultSensitivity.PRIVATE),),
        max_calls=4,
    )
    assert private_catalog.fingerprint != AgentStepCatalog((first,), max_calls=4).fingerprint
    assert left.schema == right.schema
    schema = cast(dict[str, Any], thaw_json(left.schema))
    variants = cast(list[dict[str, Any]], schema["properties"]["calls"]["items"]["oneOf"])
    for variant in variants:
        properties = cast(dict[str, Any], variant["properties"])
        assert set(properties) == {"name", "version", "arguments", "reason"}
        assert not {
            "toolCallId",
            "argsHash",
            "idempotencyKey",
            "deadline",
            "lineage",
            "risk",
        } & set(properties)
    embedded = schema["$defs"]
    assert all(str(value["$id"]).startswith("urn:offeragent:tool-input:") for value in embedded.values())
    with pytest.raises(TypeError):
        left.schema["unsafe"] = "mutation"  # type: ignore[index]


def test_catalog_projects_a_bounded_model_schema_without_weakening_the_execution_schema() -> None:
    catalog = AgentStepCatalog(plugin_tool_definitions(), max_calls=16)

    execution_schema = cast(dict[str, Any], thaw_json(catalog.schema))
    model_schema = cast(dict[str, Any], thaw_json(catalog.model_schema))
    execution_variant = cast(list[dict[str, Any]], execution_schema["properties"]["calls"]["items"]["oneOf"])[0]
    model_call_properties = cast(dict[str, Any], model_schema["properties"]["calls"]["items"]["properties"])

    assert "arguments" in execution_variant["properties"]
    assert "$defs" in execution_schema
    assert set(model_call_properties) == {"name", "version", "argumentsJson", "reason"}
    assert "arguments" not in model_call_properties
    assert len(canonical_json_bytes(model_schema)) < 4_096


def test_planner_binds_the_compact_projection_and_exact_tool_directory_into_one_request() -> None:
    catalog = AgentStepCatalog((_definition(), _definition("workspace.stat", "2")), max_calls=3)
    request = _planner(ScriptedModelGateway(()), catalog=catalog).create_request(_state())

    assert request.output_schema == catalog.model_schema
    system_text = repr(next(message for message in request.messages if message.name == "offeragent-system-rules"))
    assert "argumentsJson" in system_text
    assert "workspace.read" in system_text
    assert "workspace.stat" in system_text
    assert '"inputSchema"' in system_text
    assert "toolCallId" not in system_text
    assert "argsHash" not in system_text


def test_model_tool_directory_publishes_the_interview_submission_markdown_contract() -> None:
    catalog = AgentStepCatalog(plugin_tool_definitions(), max_calls=16)

    instruction = catalog.model_instruction

    assert "type: interview-experience" in instruction
    assert "experience-id" in instruction
    assert "source-kind: ordered_images" in instruction
    assert "captured-on" in instruction
    assert "source-fingerprint" in instruction
    assert "omit source-url" in instruction
    assert "type: interview-question" in instruction
    assert "frequency: 1" in instruction
    assert "answer-state: needs-research" in instruction
    assert "experiences/index.md" in instruction
    assert "interview/index.md" in instruction


@pytest.mark.asyncio
async def test_projected_arguments_json_is_strictly_decoded_before_tool_call_construction() -> None:
    projected = {
        "requiresWriteOutcome": False,
        "calls": [
            {
                "name": "workspace.read",
                "version": "1",
                "argumentsJson": '{"path":"notes/a.md","options":{"includeHash":true}}',
                "reason": "the user requested this exact note",
            }
        ],
        "finalResponse": None,
    }
    builder = _planner(ScriptedModelGateway(()))
    request = builder.create_request(_state())
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(request, _events(request, projected)),))

    step = await _planner(gateway).plan(_state(), ManualCancellationToken())

    assert thaw_json(step.calls[0].arguments) == {
        "path": "notes/a.md",
        "options": {"includeHash": True},
    }


@pytest.mark.asyncio
async def test_planner_preserves_provider_continuation_under_an_offeragent_step_identity() -> None:
    builder = _planner(ScriptedModelGateway(()))
    request = builder.create_request(_state())
    continuation = ModelContinuation(
        "codex-subscription",
        request.model,
        request.request_id,
        (
            {"type": "reasoning", "summary": [], "encrypted_content": "opaque"},
            {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": json.dumps(_tool_plan())}],
            },
        ),
    )
    gateway = ScriptedModelGateway(
        (ModelScriptStep.from_events(request, _events(request, _tool_plan(), continuation=continuation)),)
    )

    step = await _planner(gateway).plan(_state(), ManualCancellationToken())

    assert step.continuation == continuation
    assert step.agent_step_id is not None and step.agent_step_id.startswith("agent-step_")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments_json", "expected_violation"),
    [
        ('{"path":"a.md","path":"b.md"}', "duplicate object key"),
        ('["notes/a.md"]', "must decode to a JSON object"),
        ('{"unknown":true}', "is a required property"),
    ],
)
async def test_projected_arguments_json_fails_closed_before_execution(
    arguments_json: str,
    expected_violation: str,
) -> None:
    projected = {
        "requiresWriteOutcome": False,
        "calls": [
            {
                "name": "workspace.read",
                "version": "1",
                "argumentsJson": arguments_json,
                "reason": "attempt the requested read",
            }
        ],
        "finalResponse": None,
    }
    builder = _planner(ScriptedModelGateway(()))
    request = builder.create_request(_state())
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(request, _events(request, projected)),))
    ledger = _budget(rounds=1)
    await ledger.consume(BudgetDelta(model_rounds=1))
    planner = _planner(gateway, budget=ledger)

    with pytest.raises(SchemaRepairUnavailable) as caught:
        await planner.plan(_state(), ManualCancellationToken())

    assert any(expected_violation in violation for violation in caught.value.invalid_output.violations)


def test_skill_body_load_is_a_planning_barrier() -> None:
    catalog = AgentStepCatalog((_definition("skill"), _definition("glob")), max_calls=3)
    skill_read = {
        "name": "skill",
        "version": "1",
        "arguments": {"path": "daily-study-workflow"},
        "reason": "load the selected Skill body before following it",
    }
    glob = {
        "name": "glob",
        "version": "1",
        "arguments": {"path": "daily/*.md"},
        "reason": "locate the daily notes after loading the workflow",
    }

    assert catalog.violations({"requiresWriteOutcome": False, "calls": [skill_read], "finalResponse": None}) == ()
    mixed_violations = catalog.violations(
        {"requiresWriteOutcome": False, "calls": [skill_read, glob], "finalResponse": None}
    )
    duplicate_violations = catalog.violations(
        {"requiresWriteOutcome": False, "calls": [skill_read, skill_read], "finalResponse": None}
    )
    assert len(mixed_violations) == len(duplicate_violations) == 1
    assert "may contain only skill calls" in mixed_violations[0]
    assert "exactly one Skill" in duplicate_violations[0]


def test_only_idempotent_effectful_calls_can_share_a_serial_agent_step() -> None:
    read = _definition("glob")
    write = replace(
        _definition("vault.transaction"),
        risk=RiskClass.WRITE,
        side_effect_class=SideEffectClass.WRITE,
        concurrency_safe=False,
    )
    catalog = AgentStepCatalog((read, write), max_calls=3)
    write_call = {
        "name": "vault.transaction",
        "version": "1",
        "arguments": {"path": "daily/2026-07-13.md"},
        "reason": "create one Daily after observing the previous result",
    }
    plan = {
        "requiresWriteOutcome": True,
        "calls": [write_call, write_call],
        "finalResponse": None,
    }

    assert catalog.violations(plan) == ()

    unsafe = AgentStepCatalog((read, replace(write, idempotent=False, retryable=False)), max_calls=3)
    violations = unsafe.violations(plan)
    assert len(violations) == 1
    assert "never mix reads with effects or batch a non-idempotent tool" in violations[0]


def test_already_activated_skill_cannot_be_loaded_twice_in_one_run() -> None:
    catalog = AgentStepCatalog(skill_tool_definitions(), max_calls=2)
    plan = {
        "requiresWriteOutcome": False,
        "calls": [
            {
                "name": "skill",
                "version": "1",
                "arguments": {"name": "daily-study-workflow", "arguments": "2026-07-15"},
                "reason": "load the matching workflow",
            }
        ],
        "finalResponse": None,
    }

    assert catalog.violations(plan) == ()
    assert catalog.violations(
        plan,
        context_activations=frozenset({"skill:daily-study-workflow"}),
    ) == ("$.calls: an already activated Skill cannot be invoked again in the same Run",)


def test_run_call_fence_blocks_a_consumed_domain_operation_and_changes_catalog_identity() -> None:
    write = replace(
        _definition("vault.changes.apply"),
        input_schema={
            "type": "object",
            "properties": {"changeKind": {"enum": ["general", "interview_submission"]}},
            "required": ["changeKind"],
            "additionalProperties": False,
        },
    )
    fence = RunCallFence(
        activation="interview_submission.apply_consumed",
        tool_name="vault.changes.apply",
        tool_version="1",
        argument_name="changeKind",
        argument_value="interview_submission",
        violation="the root Run already consumed its one Interview Submission apply proposal",
    )
    catalog = AgentStepCatalog((write,), max_calls=2, run_call_fences=(fence,))
    plan = {
        "requiresWriteOutcome": True,
        "calls": [
            {
                "name": "vault.changes.apply",
                "version": "1",
                "arguments": {"changeKind": "interview_submission"},
                "reason": "repeat the already consumed proposal",
            }
        ],
        "finalResponse": None,
    }

    assert catalog.violations(plan) == ()
    assert catalog.violations(
        plan,
        context_activations=frozenset({"interview_submission.apply_consumed"}),
    ) == ("$.calls: the root Run already consumed its one Interview Submission apply proposal",)
    assert catalog.fingerprint != AgentStepCatalog((write,), max_calls=2).fingerprint
    assert "interview_submission.apply_consumed" in catalog.model_instruction


@pytest.mark.asyncio
async def test_valid_plan_generates_all_security_identity_from_harness_ports() -> None:
    expected_builder = _planner(ScriptedModelGateway(()))
    expected_request = expected_builder.create_request(_state())
    gateway = ScriptedModelGateway(
        (ModelScriptStep.from_events(expected_request, _events(expected_request, _tool_plan())),)
    )
    ledger = _budget()
    planner = _planner(gateway, budget=ledger)

    step = await planner.plan(_state(), ManualCancellationToken())

    assert len(step.attempts) == 1
    assert step.attempts[0].outcome is PlanningAttemptOutcome.SUCCEEDED
    assert step.attempts[0].usage == USAGE
    assert step.requires_write_outcome is False
    assert step.final_response is None
    assert len(step.calls) == 1
    call = step.calls[0]
    assert call.tool_call_id == "call_0001"
    assert call.idempotency_key == "idempotency_0001"
    assert call.run_id == "run"
    assert call.workspace_id == "ws"
    assert call.lineage == AgentLineage.root("run")
    assert call.result_sensitivity is ResultSensitivity.WORKSPACE
    assert call.args_hash == canonical_json_sha256(thaw_json(call.arguments))
    assert call.deadline == NOW + timedelta(milliseconds=2_500)
    snapshot = await ledger.snapshot(now=NOW)
    assert snapshot.used.input_tokens == USAGE.input_tokens
    assert snapshot.used.output_tokens == USAGE.output_tokens
    assert snapshot.used.cost == USAGE.cost
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_schema_extra_security_field_gets_exactly_one_explicit_repair() -> None:
    catalog = AgentStepCatalog((_definition(),), max_calls=3)
    invalid_plan = _tool_plan(extra_call_fields={"toolCallId": "model-forged-call"})
    builder = _planner(ScriptedModelGateway(()), catalog=catalog)
    first_request = builder.create_request(_state())
    repair_error = _invalid(catalog, first_request, invalid_plan)
    repair_request = builder.create_request(_state(), repair=repair_error)
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep.from_events(first_request, _events(first_request, invalid_plan)),
            ModelScriptStep.from_events(repair_request, _events(repair_request, _tool_plan())),
        )
    )
    ledger = _budget(rounds=2)
    planner = _planner(gateway, catalog=catalog, budget=ledger)

    step = await planner.plan(_state(), ManualCancellationToken())

    assert step.calls[0].tool_call_id == "call_0001"
    assert [attempt.outcome for attempt in step.attempts] == [
        PlanningAttemptOutcome.INVALID,
        PlanningAttemptOutcome.SUCCEEDED,
    ]
    assert thaw_json(gateway.requests[1].metadata)["schemaRepairAttempt"] == 1
    assert gateway.requests[1].messages[-1].name == "offeragent-invalid-agent-step"
    assert "model-forged-call" in repr(gateway.requests[1].messages[-1])
    snapshot = await ledger.snapshot(now=NOW)
    assert snapshot.used.model_rounds == 1
    assert snapshot.used.input_tokens == USAGE.input_tokens * 2
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_completed_stream_without_structured_output_gets_one_private_repair() -> None:
    builder = _planner(ScriptedModelGateway(()))
    first_request = builder.create_request(_state())
    repair_error = ModelInvalidOutput(
        first_request.request_id,
        ("completed stream omitted structured output",),
        raw_output=None,
        usage=USAGE,
    )
    repair_request = builder.create_request(_state(), repair=repair_error)
    omitted_output = (
        ModelEvent(first_request.request_id, 1, ModelEventKind.STARTED),
        ModelEvent(first_request.request_id, 2, ModelEventKind.USAGE, usage=USAGE),
        ModelEvent(
            first_request.request_id,
            3,
            ModelEventKind.COMPLETED,
            finish_reason=ModelFinishReason.STOP,
        ),
    )
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep.from_events(first_request, omitted_output),
            ModelScriptStep.from_events(repair_request, _events(repair_request, _tool_plan())),
        )
    )

    step = await _planner(gateway).plan(_state(), ManualCancellationToken())

    assert [attempt.outcome for attempt in step.attempts] == [
        PlanningAttemptOutcome.INVALID,
        PlanningAttemptOutcome.SUCCEEDED,
    ]
    prior_output = gateway.requests[1].messages[-1]
    assert prior_output.name == "offeragent-invalid-agent-step"
    assert prior_output.content[0].kind == "invalid_structured_output"
    assert prior_output.content[0].data["output"] is None
    assert thaw_json(gateway.requests[1].metadata)["schemaRepairAttempt"] == 1
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_second_invalid_output_fails_without_a_third_model_request() -> None:
    catalog = AgentStepCatalog((_definition(),), max_calls=3)
    invalid_plan = _tool_plan(extra_call_fields={"argsHash": "sha256:" + "0" * 64})
    builder = _planner(ScriptedModelGateway(()), catalog=catalog)
    first = builder.create_request(_state())
    repair = builder.create_request(_state(), repair=_invalid(catalog, first, invalid_plan))
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep.from_events(first, _events(first, invalid_plan)),
            ModelScriptStep.from_events(repair, _events(repair, invalid_plan)),
        )
    )

    with pytest.raises(SchemaRepairFailed) as caught:
        await _planner(gateway, catalog=catalog).plan(_state(), ManualCancellationToken())

    assert len(gateway.requests) == 2
    assert caught.value.first.request_id == first.request_id
    assert caught.value.second.request_id == repair.request_id
    assert [attempt.outcome for attempt in caught.value.planning_attempts] == [
        PlanningAttemptOutcome.INVALID,
        PlanningAttemptOutcome.INVALID,
    ]
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_repair_is_not_started_when_model_round_budget_is_exhausted() -> None:
    catalog = AgentStepCatalog((_definition(),), max_calls=3)
    invalid_plan = _tool_plan(extra_call_fields={"deadline": "2099-01-01T00:00:00Z"})
    builder = _planner(ScriptedModelGateway(()), catalog=catalog)
    request = builder.create_request(_state())
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(request, _events(request, invalid_plan)),))
    ledger = _budget(rounds=1)
    await ledger.consume(BudgetDelta(model_rounds=1))

    with pytest.raises(SchemaRepairUnavailable):
        await _planner(gateway, catalog=catalog, budget=ledger).plan(_state(), ManualCancellationToken())

    assert len(gateway.requests) == 1
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_provider_failure_is_typed_charged_and_never_treated_as_schema_repair() -> None:
    builder = _planner(ScriptedModelGateway(()))
    request = builder.create_request(_state())
    error = ModelError("rate_limited", "try later", True, False)
    events = (
        ModelEvent(request.request_id, 1, ModelEventKind.STARTED),
        ModelEvent(request.request_id, 2, ModelEventKind.USAGE, usage=USAGE),
        ModelEvent(request.request_id, 3, ModelEventKind.ERROR, error=error),
    )
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(request, events),))
    ledger = _budget()

    with pytest.raises(ModelProviderFailure) as caught:
        await _planner(gateway, budget=ledger).plan(_state(), ManualCancellationToken())

    assert caught.value.error is error
    assert caught.value.error.retryable
    assert caught.value.planning_attempts[0].error_code == "rate_limited"
    assert len(gateway.requests) == 1
    snapshot = await ledger.snapshot(now=NOW)
    assert snapshot.used.input_tokens == USAGE.input_tokens
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_context_overflow_retries_once_with_new_request_and_charges_both_attempts() -> None:
    builder = _planner(ScriptedModelGateway(()))
    first = builder.create_request(_state())
    retry = builder.create_request(
        _state(),
        projection=ContextProjection.OVERFLOW_REFERENCES,
        retry_of_request_id=first.request_id,
    )
    overflow = ModelError("context_overflow", "provider context is full", False, False)
    first_events = (
        ModelEvent(first.request_id, 1, ModelEventKind.STARTED),
        ModelEvent(first.request_id, 2, ModelEventKind.USAGE, usage=USAGE),
        ModelEvent(first.request_id, 3, ModelEventKind.ERROR, error=overflow),
    )
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep.from_events(first, first_events),
            ModelScriptStep.from_events(retry, _events(retry, _tool_plan())),
        )
    )
    ledger = _budget()

    step = await _planner(gateway, budget=ledger).plan(_state(), ManualCancellationToken())

    assert [attempt.request_id for attempt in step.attempts] == [first.request_id, retry.request_id]
    assert [attempt.outcome for attempt in step.attempts] == [
        PlanningAttemptOutcome.FAILED,
        PlanningAttemptOutcome.SUCCEEDED,
    ]
    assert step.attempts[0].error_code == "context_overflow"
    assert step.attempts[0].projection == "normal"
    assert step.attempts[1].retry_of_request_id == first.request_id
    assert step.attempts[1].projection == "overflow_references"
    assert step.attempts[1].projection_hash is not None
    retry_metadata = thaw_json(gateway.requests[1].metadata)
    assert retry_metadata["contextOverflowRetry"] is True
    assert retry_metadata["retryOfRequestId"] == first.request_id
    assert retry_metadata["projection"] == "overflow_references"
    assert retry_metadata["projectionHash"].startswith("sha256:")
    snapshot = await ledger.snapshot(now=NOW)
    assert snapshot.used.model_rounds == 1
    assert snapshot.used.input_tokens == USAGE.input_tokens * 2
    assert snapshot.used.output_tokens == USAGE.output_tokens * 2
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_context_overflow_retry_is_bounded_to_one_attempt() -> None:
    builder = _planner(ScriptedModelGateway(()))
    first = builder.create_request(_state())
    retry = builder.create_request(
        _state(),
        projection=ContextProjection.OVERFLOW_REFERENCES,
        retry_of_request_id=first.request_id,
    )
    overflow = ModelError("context_overflow", "provider context is full", False, False)

    def failed(request: ModelRequest) -> tuple[ModelEvent, ...]:
        return (
            ModelEvent(request.request_id, 1, ModelEventKind.STARTED),
            ModelEvent(request.request_id, 2, ModelEventKind.ERROR, error=overflow),
        )

    gateway = ScriptedModelGateway(
        (
            ModelScriptStep.from_events(first, failed(first)),
            ModelScriptStep.from_events(retry, failed(retry)),
        )
    )

    with pytest.raises(ModelProviderFailure) as caught:
        await _planner(gateway).plan(_state(), ManualCancellationToken())

    assert len(gateway.requests) == 2
    assert len(caught.value.planning_attempts) == 2
    assert [attempt.error_code for attempt in caught.value.planning_attempts] == [
        "context_overflow",
        "context_overflow",
    ]
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_length_finish_is_repairable_but_stream_sequence_error_is_not() -> None:
    catalog = AgentStepCatalog((_definition(),), max_calls=3)
    builder = _planner(ScriptedModelGateway(()), catalog=catalog)
    first = builder.create_request(_state())
    length_error = _invalid(
        catalog,
        first,
        _tool_plan(),
        violations=("model finish reason was length, expected stop",),
    )
    repair = builder.create_request(_state(), repair=length_error)
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep.from_events(
                first,
                _events(first, _tool_plan(), finish=ModelFinishReason.LENGTH),
            ),
            ModelScriptStep.from_events(repair, _events(repair, _tool_plan())),
        )
    )
    step = await _planner(gateway, catalog=catalog).plan(_state(), ManualCancellationToken())
    assert len(step.calls) == 1
    gateway.assert_exhausted()

    sequence_builder = _planner(ScriptedModelGateway(()))
    sequence_request = sequence_builder.create_request(_state())
    sequence_events = (
        ModelEvent(sequence_request.request_id, 1, ModelEventKind.STARTED),
        ModelEvent(sequence_request.request_id, 3, ModelEventKind.STRUCTURED_OUTPUT, data=_tool_plan()),
    )
    sequence_gateway = ScriptedModelGateway((ModelScriptStep.from_events(sequence_request, sequence_events),))
    with pytest.raises(ModelStreamProtocolError, match="expected sequence 2"):
        await _planner(sequence_gateway).plan(_state(), ManualCancellationToken())
    assert len(sequence_gateway.requests) == 1


@pytest.mark.asyncio
async def test_cancellation_at_model_barrier_propagates_without_consuming_later_events() -> None:
    builder = _planner(ScriptedModelGateway(()))
    request = builder.create_request(_state())
    barrier = ControlledBarrier("planner-output")
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep(
                request,
                (
                    ScriptedModelEvent(ModelEvent(request.request_id, 1, ModelEventKind.STARTED)),
                    ScriptedModelEvent(
                        ModelEvent(request.request_id, 2, ModelEventKind.STRUCTURED_OUTPUT, data=_tool_plan()),
                        barrier,
                    ),
                    ScriptedModelEvent(ModelEvent(request.request_id, 3, ModelEventKind.USAGE, usage=USAGE)),
                    ScriptedModelEvent(
                        ModelEvent(
                            request.request_id,
                            4,
                            ModelEventKind.COMPLETED,
                            finish_reason=ModelFinishReason.STOP,
                        )
                    ),
                ),
            ),
        )
    )
    cancellation = ManualCancellationToken()
    task = asyncio.create_task(_planner(gateway).plan(_state(), cancellation))
    await barrier.wait_for_arrivals(1)

    cancellation.cancel()
    with pytest.raises(FakeRunCancelled):
        await task

    assert [event.kind for event in gateway.emitted_events] == [ModelEventKind.STARTED]
    gateway.assert_exhausted()
