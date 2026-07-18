from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import ApplicationCommandContext
from offeragent_harness.protocol._base import validate_wire
from offeragent_harness.protocol.messages import PluginToolCompleteParams, PluginToolCompleteResult
from offeragent_harness.runtime.plugin_tools import (
    PluginToolBindingMismatch,
    PluginToolCompletion,
    PluginToolCompletionDisposition,
    PluginToolExecutor,
    PluginToolNotPending,
    plugin_tool_completion_handlers,
    plugin_tool_definitions,
)
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import FakeRunCancelled, ManualCancellationCode, ManualCancellationToken
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
    ToolValidationError,
    ToolValidator,
    canonical_json_sha256,
)


def _definition() -> ToolDefinition:
    return ToolDefinition(
        name="agent_contract.read",
        version="1",
        description="Read the current Vault Agent Contract",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={"type": "object"},
        executor_location=ExecutorLocation.PLUGIN,
        risk=RiskClass.READ,
        side_effect_class=SideEffectClass.READ,
        required_capabilities=frozenset({"vault.read"}),
        concurrency_safe=True,
        idempotent=True,
        retryable=True,
        timeout_ms=1_000,
        output_limit_bytes=32_768,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    )


def _call(definition: ToolDefinition) -> ToolCall:
    arguments: dict[str, object] = {}
    return ToolCall(
        tool_call_id="call_agent_contract",
        run_id="run_contract",
        workspace_id="ws_vault",
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="contract-read-1",
        deadline=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
        lineage=AgentLineage.root("run_contract"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


@pytest.mark.asyncio
async def test_plugin_tool_execution_completes_with_the_bound_plugin_result() -> None:
    definition = _definition()
    call = _call(definition)
    result = ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data={"content": "# OfferAgent"},
        user_visible_summary="Read the Vault Agent Contract.",
        artifact_ids=(),
        source_refs=("agent.md",),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state=None,
        error=None,
    )
    executor = PluginToolExecutor()

    execution = asyncio.create_task(executor.execute(call, ManualCancellationToken()))
    await asyncio.sleep(0)
    await executor.complete(PluginToolCompletion.from_call(call, result))

    assert await execution is result


@pytest.mark.asyncio
async def test_plugin_completion_waits_for_the_started_call_to_register() -> None:
    definition = _definition()
    call = _call(definition)
    result = ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data={"content": "# OfferAgent"},
        user_visible_summary="Read the Vault Agent Contract.",
        artifact_ids=(),
        source_refs=("agent.md",),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state=None,
        error=None,
    )
    executor = PluginToolExecutor(registration_timeout_seconds=1.0)

    completion = asyncio.create_task(executor.complete(PluginToolCompletion.from_call(call, result)))
    await asyncio.sleep(0)
    execution = asyncio.create_task(executor.execute(call, ManualCancellationToken()))

    await completion
    assert await execution is result


@pytest.mark.asyncio
async def test_exact_duplicate_plugin_completion_is_idempotent() -> None:
    definition = _definition()
    call = _call(definition)
    result = ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data={"content": "# OfferAgent"},
        user_visible_summary="Read the Vault Agent Contract.",
        artifact_ids=(),
        source_refs=("agent.md",),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state=None,
        error=None,
    )
    completion = PluginToolCompletion.from_call(call, result)
    executor = PluginToolExecutor(registration_timeout_seconds=0.1)

    execution = asyncio.create_task(executor.execute(call, ManualCancellationToken()))
    await asyncio.sleep(0)

    assert await executor.complete(completion) is PluginToolCompletionDisposition.ACCEPTED
    assert await execution is result
    assert await executor.complete(completion) is PluginToolCompletionDisposition.REPLAYED


@pytest.mark.asyncio
async def test_stdio_completion_command_returns_the_plugin_result_to_the_executor() -> None:
    definition = _definition()
    call = _call(definition)
    executor = PluginToolExecutor()
    params = validate_wire(
        PluginToolCompleteParams,
        {
            "workspaceId": call.workspace_id,
            "runId": call.run_id,
            "definitionFingerprint": call.definition_fingerprint,
            "argsHash": call.args_hash,
            "idempotencyKey": call.idempotency_key,
            "result": {
                "toolCallId": call.tool_call_id,
                "status": "succeeded",
                "summary": "Read the Vault Agent Contract.",
                "data": {"content": "# OfferAgent"},
            },
        },
    )
    handler = plugin_tool_completion_handlers(executor=executor)["plugin-tools/complete"]

    execution = asyncio.create_task(executor.execute(call, ManualCancellationToken()))
    response = await handler(
        params,
        ManualCancellationToken(),
        ApplicationCommandContext(transport="stdio", client_id="obsidian-plugin"),
    )

    assert response == PluginToolCompleteResult(accepted=True, replayed=False)
    assert (await execution).data == {"content": "# OfferAgent"}


@pytest.mark.asyncio
async def test_cross_workspace_plugin_completion_is_rejected() -> None:
    definition = _definition()
    call = _call(definition)
    result = ToolResult(
        call.tool_call_id,
        ToolResultStatus.SUCCEEDED,
        {"content": "# OfferAgent"},
        "Read the Vault Agent Contract.",
        (),
        (),
        (),
        False,
        None,
        None,
        None,
    )
    cancellation = ManualCancellationToken()
    executor = PluginToolExecutor()
    execution = asyncio.create_task(executor.execute(call, cancellation))
    await asyncio.sleep(0)

    with pytest.raises(PluginToolBindingMismatch):
        await executor.complete(replace(PluginToolCompletion.from_call(call, result), workspace_id="ws_other"))

    cancellation.cancel()
    with pytest.raises(FakeRunCancelled):
        await execution


@pytest.mark.asyncio
async def test_conflicting_duplicate_plugin_completion_is_rejected() -> None:
    definition = _definition()
    call = _call(definition)
    result = ToolResult(
        call.tool_call_id,
        ToolResultStatus.SUCCEEDED,
        {"content": "# OfferAgent"},
        "Read the Vault Agent Contract.",
        (),
        (),
        (),
        False,
        None,
        None,
        None,
    )
    completion = PluginToolCompletion.from_call(call, result)
    executor = PluginToolExecutor()
    execution = asyncio.create_task(executor.execute(call, ManualCancellationToken()))
    await asyncio.sleep(0)
    await executor.complete(completion)
    await execution

    with pytest.raises(PluginToolBindingMismatch):
        await executor.complete(
            replace(completion, result=replace(result, user_visible_summary="Conflicting duplicate."))
        )


@pytest.mark.asyncio
async def test_completed_plugin_call_reexecutes_only_as_an_exact_local_replay() -> None:
    definition = _definition()
    call = _call(definition)
    result = ToolResult(
        call.tool_call_id,
        ToolResultStatus.SUCCEEDED,
        {"content": "# OfferAgent"},
        "Read the Vault Agent Contract.",
        (),
        (),
        (),
        False,
        None,
        None,
        None,
    )
    executor = PluginToolExecutor()
    execution = asyncio.create_task(executor.execute(call, ManualCancellationToken()))
    await asyncio.sleep(0)
    await executor.complete(PluginToolCompletion.from_call(call, result))
    assert await execution == result

    assert await executor.execute(call, ManualCancellationToken()) == result
    with pytest.raises(PluginToolBindingMismatch):
        await executor.execute(replace(call, workspace_id="ws_other"), ManualCancellationToken())


@pytest.mark.asyncio
async def test_shutdown_cancels_disconnected_read_and_rejects_its_late_completion() -> None:
    definition = _definition()
    call = _call(definition)
    result = ToolResult(
        call.tool_call_id,
        ToolResultStatus.SUCCEEDED,
        {"content": "# OfferAgent"},
        "Read the Vault Agent Contract.",
        (),
        (),
        (),
        False,
        None,
        None,
        None,
    )
    cancellation = ManualCancellationToken()
    executor = PluginToolExecutor(registration_timeout_seconds=0.01)
    execution = asyncio.create_task(executor.execute(call, cancellation))
    await asyncio.sleep(0)

    cancellation.cancel(ManualCancellationCode.SHUTDOWN, "stdio peer disconnected")
    with pytest.raises(FakeRunCancelled):
        await execution
    with pytest.raises(PluginToolNotPending):
        await executor.complete(PluginToolCompletion.from_call(call, result))


def test_vault_evidence_definitions_preserve_the_plugin_read_boundary() -> None:
    definitions = {definition.name: definition for definition in plugin_tool_definitions()}

    assert {
        "agent_contract.read",
        "skill.read",
        "daily_note.context",
        "planning_memory.list",
        "planning_memory.read",
        "interview_catalog.search",
        "research_browser.navigate",
        "vault.list",
        "vault.search",
        "vault.read",
        "project.list",
        "project.search",
        "project.read",
        "vault.changes.apply",
    } <= definitions.keys()
    capabilities = {
        "agent_contract.read": "agent_contract.read",
        "skill.read": "skill.read",
        "daily_note.context": "daily_note.read",
        "planning_memory.list": "vault.read",
        "planning_memory.read": "vault.read",
        "interview_catalog.search": "vault.read",
        "research_browser.navigate": "research.browser",
        "vault.list": "vault.read",
        "vault.search": "vault.read",
        "vault.read": "vault.read",
        "project.list": "project.read",
        "project.search": "project.read",
        "project.read": "project.read",
        "vault.changes.apply": "vault.write",
    }
    for name, definition in definitions.items():
        assert definition.executor_location is ExecutorLocation.PLUGIN
        expected_risk = (
            RiskClass.WRITE
            if name == "vault.changes.apply"
            else RiskClass.NETWORK
            if name == "research_browser.navigate"
            else RiskClass.READ
        )
        expected_effect = (
            SideEffectClass.WRITE
            if name == "vault.changes.apply"
            else SideEffectClass.NETWORK
            if name == "research_browser.navigate"
            else SideEffectClass.READ
        )
        assert definition.risk is expected_risk
        assert definition.side_effect_class is expected_effect
        assert definition.required_capabilities == frozenset({capabilities[name]})
        assert definition.result_sensitivity is ResultSensitivity.WORKSPACE
        assert definition.idempotent is (name != "research_browser.navigate")
        assert definition.retryable is (name != "vault.changes.apply" and name != "research_browser.navigate")
    assert definitions["vault.read"].output_limit_bytes == 65_536
    assert definitions["vault.search"].output_limit_bytes == 65_536
    assert definitions["planning_memory.list"].output_schema["properties"]["topics"]["maxItems"] == 100
    memory_selection = definitions["planning_memory.read"].input_schema["properties"]["topics"]
    assert memory_selection["maxItems"] == 5
    assert memory_selection["items"]["required"] == (
        "path",
        "expectedModifiedVersion",
        "expectedContentHash",
    )
    browser_actions = definitions["research_browser.navigate"].input_schema["oneOf"]
    assert {variant["properties"]["action"].get("const") for variant in browser_actions} >= {
        "open",
        "follow",
        "paginate",
    }
    interview_catalog = definitions["interview_catalog.search"]
    catalog_input = interview_catalog.input_schema["properties"]
    assert catalog_input["sourceUrls"]["maxItems"] == 8
    assert catalog_input["orderedImageContentHashes"]["maxItems"] == 20
    assert catalog_input["orderedImageContentHashes"]["items"]["pattern"] == r"^sha256:[0-9a-f]{64}$"
    assert set(catalog_input) == {
        "sourceUrls",
        "orderedImageContentHashes",
        "company",
        "role",
        "questionTerms",
    }
    catalog_output = interview_catalog.output_schema["properties"]
    normalized_source = catalog_output["normalizedSource"]
    assert normalized_source["required"] == (
        "canonicalUrls",
        "sourceFingerprint",
        "orderedImageContentHashes",
    )
    assert normalized_source["properties"]["canonicalUrls"]["maxItems"] == 8
    assert normalized_source["properties"]["orderedImageContentHashes"]["maxItems"] == 20
    assert normalized_source["properties"]["sourceFingerprint"]["anyOf"][1] == {"type": "null"}
    indexes = catalog_output["indexes"]
    assert (indexes["minItems"], indexes["maxItems"]) == (2, 2)
    assert catalog_output["questionCandidates"]["maxItems"] == 100
    write = definitions["vault.changes.apply"]
    operations = write.input_schema["properties"]["operations"]
    assert operations["maxItems"] == 20
    assert {item["properties"]["op"]["const"] for item in operations["items"]["oneOf"]} == {
        "create",
        "append",
        "replace",
        "patch",
        "delete",
    }


def test_interview_catalog_definition_validates_normalized_sources_and_fixed_indexes() -> None:
    definition = {item.name: item for item in plugin_tool_definitions()}["interview_catalog.search"]
    validator = ToolValidator()
    digest = "sha256:" + "a" * 64
    output: dict[str, Any] = {
        "normalizedSource": {
            "canonicalUrls": ["https://example.com/interview"],
            "sourceFingerprint": digest,
            "orderedImageContentHashes": [digest],
        },
        "experienceCandidates": [],
        "questionCandidates": [],
        "indexes": [
            {
                "kind": "experience",
                "path": "experiences/index.md",
                "exists": True,
                "modifiedVersion": "mtime:1:size:24",
                "contentHash": digest,
            },
            {
                "kind": "question",
                "path": "interview/index.md",
                "exists": False,
                "modifiedVersion": "missing",
            },
        ],
        "truncated": False,
    }

    assert validator.validate_output(definition, output) is not None
    with pytest.raises(ToolValidationError):
        validator.validate_output(
            definition,
            {
                **output,
                "indexes": [
                    output["indexes"][0],
                    {
                        "kind": "question",
                        "path": "interviews/questions/INDEX.md",
                        "exists": False,
                        "modifiedVersion": "missing",
                    },
                ],
            },
        )


def test_vault_changes_definition_requires_versions_for_interview_submission_targets() -> None:
    definition = {item.name: item for item in plugin_tool_definitions()}["vault.changes.apply"]
    validator = ToolValidator()
    digest = "sha256:" + "a" * 64
    interview: dict[str, Any] = {
        "batchId": "batch_interview",
        "task": "Persist one Interview Submission",
        "changeKind": "interview_submission",
        "sourceBindings": [],
        "interviewSubmission": {
            "sourceKind": "text",
            "capturedOn": "2026-07-18",
            "canonicalUrls": [],
            "orderedImageContentHashes": [],
            "sourceFingerprint": None,
        },
        "operations": [
            {
                "op": "create",
                "path": "experiences/acme.md",
                "content": "# Acme\n",
                "expectedContentHash": "absent",
                "expectedModifiedVersion": "missing",
            },
            {
                "op": "append",
                "path": "interview/index.md",
                "content": "- [[experiences/acme]]\n",
                "expectedContentHash": digest,
                "expectedModifiedVersion": "mtime:1:size:13",
            },
        ],
    }

    assert validator.validate_arguments(definition, interview) is not None

    create_without_version = {**interview["operations"][0]}
    del create_without_version["expectedModifiedVersion"]
    with pytest.raises(ToolValidationError):
        validator.validate_arguments(
            definition,
            {**interview, "operations": [create_without_version, interview["operations"][1]]},
        )

    with pytest.raises(ToolValidationError):
        validator.validate_arguments(
            definition,
            {
                **interview,
                "operations": [
                    {**interview["operations"][0], "expectedModifiedVersion": "absent"},
                    interview["operations"][1],
                ],
            },
        )

    with pytest.raises(ToolValidationError):
        validator.validate_arguments(
            definition,
            {
                **interview,
                "operations": [
                    interview["operations"][0],
                    {**interview["operations"][1], "expectedModifiedVersion": "missing"},
                ],
            },
        )

    with pytest.raises(ToolValidationError):
        validator.validate_arguments(
            definition,
            {
                **interview,
                "operations": [
                    interview["operations"][0],
                    {**interview["operations"][1], "expectedModifiedVersion": "absent"},
                ],
            },
        )

    with pytest.raises(ToolValidationError):
        validator.validate_arguments(
            definition,
            {
                **interview,
                "sourceBindings": [
                    {
                        "path": "interview/source.md",
                        "expectedModifiedVersion": "absent",
                        "expectedContentHash": digest,
                    }
                ],
            },
        )

    general: dict[str, Any] = {
        "batchId": "batch_general",
        "task": "Append a note",
        "changeKind": "general",
        "sourceBindings": [],
        "interviewSubmission": None,
        "operations": [
            {
                "op": "append",
                "path": "notes/a.md",
                "content": "next\n",
                "expectedContentHash": digest,
                "expectedModifiedVersion": "mtime:2:size:6",
            }
        ],
    }
    assert validator.validate_arguments(definition, general) is not None
    general_without_version = {**general["operations"][0]}
    del general_without_version["expectedModifiedVersion"]
    with pytest.raises(ToolValidationError):
        validator.validate_arguments(definition, {**general, "operations": [general_without_version]})
    with pytest.raises(ToolValidationError):
        validator.validate_arguments(
            definition,
            {**general, "interviewSubmission": interview["interviewSubmission"]},
        )
    with pytest.raises(ToolValidationError):
        validator.validate_arguments(
            definition,
            {key: value for key, value in general.items() if key != "interviewSubmission"},
        )
    with pytest.raises(ToolValidationError):
        validator.validate_arguments(definition, {**interview, "interviewSubmission": None})
