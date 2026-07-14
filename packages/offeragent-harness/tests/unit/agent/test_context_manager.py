from __future__ import annotations

from dataclasses import replace

import pytest

from offeragent_harness.agent.context_manager import (
    ContextBudget,
    ContextBudgetExceeded,
    ContextCompactionRequired,
    ContextFragment,
    ContextInputs,
    ContextLayer,
    ContextManager,
    ContextProjection,
    ContextVisibilityPolicy,
)
from offeragent_harness.agent.state import (
    PendingWork,
    RunControlMessage,
    RunPhase,
    RunState,
    VaultWriteIntentBinding,
)
from offeragent_harness.foundation import vault_write_intent_hash
from offeragent_harness.models import ModelPurpose, ModelRole, thaw_json
from offeragent_harness.ports import Sensitivity
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools import ResultSensitivity, ToolResult, ToolResultStatus


def _fragment(
    fragment_id: str,
    layer: ContextLayer,
    text: str,
    sensitivity: Sensitivity,
    *,
    source_refs: tuple[str, ...] = (),
) -> ContextFragment:
    return ContextFragment(
        fragment_id=fragment_id,
        layer=layer,
        text=text,
        sensitivity=sensitivity,
        source_refs=source_refs,
        content_hash="sha256:" + "a" * 64,
    )


def _result(tool_call_id: str, text: str) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data={"text": text},
        user_visible_summary=f"result {tool_call_id}",
        artifact_ids=(f"artifact-{tool_call_id}",),
        source_refs=(f"source-{tool_call_id}",),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state={"hash": "sha256:" + "b" * 64},
        error=None,
    )


def _state() -> RunState:
    base = RunState("ws", "session", "turn", "run", AgentLineage.root("run"))
    return replace(
        base,
        phase=RunPhase.PLANNING,
        revision=7,
        model_rounds=2,
        tool_calls=3,
        pending=PendingWork(
            tool_call_ids=frozenset({"call-z", "call-a"}),
            approval_ids=frozenset({"approval-1"}),
            client_invocation_ids=frozenset({"client-1"}),
            child_run_ids=frozenset({"child-1"}),
        ),
        write_obligation=base.write_obligation.require("必须写入用户指定的笔记"),
        tool_results=(_result("visible", "visible tool evidence"), _result("unknown", "must stay hidden")),
        tool_result_sensitivities={"visible": ResultSensitivity.WORKSPACE},
    )


def test_context_has_fixed_layers_snapshot_sources_and_fail_closed_filtering() -> None:
    inputs = ContextInputs(
        user_input=(_fragment("user-1", ContextLayer.USER_INPUT, "please help", Sensitivity.PUBLIC),),
        memories=(
            _fragment(
                "memory-visible",
                ContextLayer.MEMORY,
                "workspace memory",
                Sensitivity.WORKSPACE,
                source_refs=("vault:notes/a.md",),
            ),
            _fragment("memory-private", ContextLayer.MEMORY, "private memory", Sensitivity.PRIVATE),
        ),
        skills=(
            _fragment("skill-secret", ContextLayer.SKILLS, "secret skill", Sensitivity.SECRET),
            _fragment("skill-visible", ContextLayer.SKILLS, "visible skill", Sensitivity.PUBLIC),
        ),
    )
    manager = ContextManager(
        system_rules=("系统规则一", "系统规则二"),
        inputs=inputs,
        visibility=ContextVisibilityPolicy.cloud_model(),
        budget=ContextBudget.generous_default(),
    )

    window = manager.build(_state(), purpose=ModelPurpose.PLANNING)

    assert tuple(message.name for message in window.messages) == (
        "offeragent-system-rules",
        "offeragent-run-snapshot",
        "offeragent-user_input",
        "offeragent-memory",
        "offeragent-skills",
        "visible",
    )
    assert tuple(message.role for message in window.messages) == (
        ModelRole.SYSTEM,
        ModelRole.SYSTEM,
        ModelRole.USER,
        ModelRole.USER,
        ModelRole.USER,
        ModelRole.TOOL,
    )
    assert window.included_context_ids == (
        "system:rules",
        "run:run:snapshot",
        "user-1",
        "memory-visible",
        "skill-visible",
        "tool:visible",
    )
    assert {(item.context_id, item.reason) for item in window.omitted} == {
        ("memory-private", "sensitivity_policy"),
        ("skill-secret", "sensitivity_policy"),
        ("tool:unknown", "unclassified_fail_closed"),
    }

    snapshot = thaw_json(window.messages[1].content[0].data)
    assert snapshot["writeObligation"] == {
        "required": True,
        "reasons": ["必须写入用户指定的笔记"],
        "satisfied": False,
        "intent": None,
        "outcomes": [],
    }
    assert snapshot["pending"] == {
        "toolCallIds": ["call-a", "call-z"],
        "approvalIds": ["approval-1"],
        "clientInvocationIds": ["client-1"],
        "childRunIds": ["child-1"],
    }
    memory = thaw_json(window.messages[3].content[0].data)
    assert memory["sourceRefs"] == ["vault:notes/a.md"]
    tool = thaw_json(window.messages[-1].content[0].data)
    assert tool["artifactIds"] == ["artifact-visible"]
    assert tool["sourceRefs"] == ["source-visible"]
    assert tool["afterState"] == {"hash": "sha256:" + "b" * 64}

    serialized = repr(window.messages)
    assert "private memory" not in serialized
    assert "secret skill" not in serialized
    assert "must stay hidden" not in serialized


def test_context_exposes_the_exact_bound_write_targets_to_the_planner() -> None:
    targets = ("notes/offer.md", "notes/summary.md")
    binding = VaultWriteIntentBinding(
        request_hash="sha256:" + ("1" * 64),
        intent_hash=vault_write_intent_hash(targets),
        target_paths=targets,
    )
    current = _state().require_write_outcome("turn.start.vault_write_required", intent=binding)
    manager = ContextManager(
        system_rules=("system",),
        inputs=ContextInputs(
            user_input=(_fragment("user", ContextLayer.USER_INPUT, "write", Sensitivity.PUBLIC),),
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget.generous_default(),
    )

    window = manager.build(current, purpose=ModelPurpose.PLANNING)
    snapshot = thaw_json(window.messages[1].content[0].data)
    assert snapshot["writeObligation"]["intent"] == {
        "intentHash": vault_write_intent_hash(targets),
        "targetPaths": list(targets),
    }


def test_current_user_input_is_never_displaced_by_optional_conversation_history() -> None:
    history = ContextFragment(
        "conversation:previous:assistant",
        ContextLayer.CONVERSATION,
        "previous answer " * 100,
        Sensitivity.WORKSPACE,
        role=ModelRole.ASSISTANT,
    )
    manager = ContextManager(
        system_rules=("system",),
        inputs=ContextInputs(
            user_input=(_fragment("user", ContextLayer.USER_INPUT, "current request", Sensitivity.PUBLIC),),
            conversation=(history,),
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget(
            max_messages=3,
            max_total_bytes=10_000,
            max_estimated_tokens=4_000,
            max_item_bytes=4_000,
        ),
    )

    window = manager.build(_state(), purpose=ModelPurpose.PLANNING)

    assert "user" in window.included_context_ids
    assert ("conversation:previous:assistant", "context_budget") in {
        (item.context_id, item.reason) for item in window.omitted
    }
    assert not any(reason.startswith("user:") for reason in window.compaction_reasons)


def test_hook_context_hints_are_private_untrusted_and_lowest_priority() -> None:
    manager = ContextManager(
        system_rules=("system",),
        inputs=ContextInputs(
            user_input=(_fragment("user", ContextLayer.USER_INPUT, "request", Sensitivity.PUBLIC),),
            memories=(_fragment("memory", ContextLayer.MEMORY, "memory", Sensitivity.PUBLIC),),
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget.generous_default(),
    ).with_hook_hints(("format as a table",))

    window = manager.build(_state(), purpose=ModelPurpose.PLANNING)
    hint = window.messages[-1]
    data = thaw_json(hint.content[0].data)

    assert hint.name == "offeragent-hook_hints"
    assert data["layer"] == "hook_hints"
    assert data["sensitivity"] == "private"
    assert data["untrustedData"] is True
    assert window.included_context_ids[-1].startswith("hook-hint:")


def test_sensitivity_is_typed_metadata_not_a_keyword_heuristic() -> None:
    manager = ContextManager(
        system_rules=("rule",),
        inputs=ContextInputs(
            user_input=(
                _fragment(
                    "public-keyword",
                    ContextLayer.USER_INPUT,
                    "SECRET API KEY PASSWORD are merely user words",
                    Sensitivity.PUBLIC,
                ),
            ),
            memories=(_fragment("secret-innocent", ContextLayer.MEMORY, "hello", Sensitivity.SECRET),),
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget.generous_default(),
    )

    window = manager.build(_state(), purpose=ModelPurpose.COMPOSING)

    assert "public-keyword" in window.included_context_ids
    assert "secret-innocent" not in window.included_context_ids
    assert "SECRET API KEY PASSWORD" in repr(window.messages)
    assert "hello" not in repr(window.messages)


def test_context_input_invariants_reject_layer_mismatch_duplicate_ids_and_bad_hash() -> None:
    with pytest.raises(ValueError, match="matching layer"):
        ContextInputs(user_input=(_fragment("wrong", ContextLayer.MEMORY, "text", Sensitivity.PUBLIC),))

    duplicate = _fragment("same", ContextLayer.USER_INPUT, "text", Sensitivity.PUBLIC)
    with pytest.raises(ValueError, match="unique"):
        ContextInputs(
            user_input=(duplicate,),
            memories=(_fragment("same", ContextLayer.MEMORY, "memory", Sensitivity.PUBLIC),),
        )

    with pytest.raises(ValueError, match="sha256"):
        ContextFragment(
            "bad-hash",
            ContextLayer.USER_INPUT,
            "text",
            Sensitivity.PUBLIC,
            content_hash="not-a-hash",
        )


def test_oversized_content_is_replaced_by_artifact_refs_without_losing_sources_or_hashes() -> None:
    huge_user = ContextFragment(
        "large-user",
        ContextLayer.USER_INPUT,
        "USER-BODY-" + "x" * 5_000,
        Sensitivity.PUBLIC,
        source_refs=("source:user",),
        artifact_ids=("artifact-user",),
        content_hash="sha256:" + "c" * 64,
    )
    huge_result = _result("large-tool", "TOOL-BODY-" + "y" * 5_000)
    state = replace(
        _state(),
        tool_results=(huge_result,),
        tool_result_sensitivities={"large-tool": ResultSensitivity.WORKSPACE},
    )
    manager = ContextManager(
        system_rules=("rule",),
        inputs=ContextInputs(
            user_input=(huge_user,),
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget(
            max_messages=8,
            max_total_bytes=10_000,
            max_estimated_tokens=4_000,
            max_item_bytes=1_000,
        ),
    )

    window = manager.build(state, purpose=ModelPurpose.PLANNING)

    assert not window.compaction_required
    assert window.messages[2].content[0].kind == "context_reference"
    assert window.messages[3].content[0].kind == "tool_result_reference"
    user_ref = thaw_json(window.messages[2].content[0].data)
    tool_ref = thaw_json(window.messages[3].content[0].data)
    assert user_ref["artifactIds"] == ["artifact-user"]
    assert user_ref["sourceRefs"] == ["source:user"]
    assert user_ref["contentHash"] == "sha256:" + "c" * 64
    assert tool_ref["artifactIds"] == ["artifact-large-tool"]
    assert tool_ref["sourceRefs"] == ["source-large-tool"]
    assert tool_ref["criticalHashes"] == {"$.afterState.hash": "sha256:" + "b" * 64}
    assert "USER-BODY-" not in repr(window.messages)
    assert "TOOL-BODY-" not in repr(window.messages)


def test_missing_artifact_or_total_capacity_requires_compaction_with_stable_priority() -> None:
    manager = ContextManager(
        system_rules=("rule",),
        inputs=ContextInputs(
            user_input=(
                _fragment("user", ContextLayer.USER_INPUT, "keep user", Sensitivity.PUBLIC),
                ContextFragment(
                    "large-no-artifact",
                    ContextLayer.USER_INPUT,
                    "z" * 5_000,
                    Sensitivity.PUBLIC,
                ),
            ),
            memories=(_fragment("memory", ContextLayer.MEMORY, "memory", Sensitivity.PUBLIC),),
            skills=(_fragment("skill", ContextLayer.SKILLS, "skill", Sensitivity.PUBLIC),),
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget(
            max_messages=4,
            max_total_bytes=10_000,
            max_estimated_tokens=4_000,
            max_item_bytes=1_000,
        ),
    )
    state = replace(_state(), tool_results=(_result("visible", "evidence"),))

    window = manager.build(state, purpose=ModelPurpose.PLANNING)

    assert window.included_context_ids == (
        "system:rules",
        "run:run:snapshot",
        "user",
        "tool:visible",
    )
    assert {(item.context_id, item.reason) for item in window.omitted} == {
        ("large-no-artifact", "artifactization_required"),
        ("memory", "context_budget"),
        ("skill", "context_budget"),
    }
    # Optional Memory/Skill omissions are explicit but do not make an otherwise
    # safe window unready; the irreplaceable current user input still does.
    assert window.compaction_reasons == ("large-no-artifact:artifactization_required",)
    with pytest.raises(ContextCompactionRequired) as caught:
        window.ensure_model_ready()
    assert caught.value.window is window


def test_system_snapshot_is_never_silently_truncated_to_fit_an_impossible_budget() -> None:
    manager = ContextManager(
        system_rules=("rule",),
        inputs=ContextInputs(user_input=(_fragment("user", ContextLayer.USER_INPUT, "text", Sensitivity.PUBLIC),)),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget(
            max_messages=2,
            max_total_bytes=10,
            max_estimated_tokens=10,
            max_item_bytes=10,
        ),
    )

    with pytest.raises(ContextBudgetExceeded, match="system rules"):
        manager.build(_state(), purpose=ModelPurpose.PLANNING)


def test_overflow_reference_projection_preserves_control_and_structural_tool_evidence() -> None:
    user = ContextFragment("user", ContextLayer.USER_INPUT, "current request", Sensitivity.PUBLIC)
    memory = ContextFragment(
        "memory-ref",
        ContextLayer.MEMORY,
        "MEMORY-BODY-MUST-BE-OMITTED",
        Sensitivity.WORKSPACE,
        source_refs=("vault:notes/source.md",),
    )
    optional_without_reference = ContextFragment(
        "skill-no-ref",
        ContextLayer.SKILLS,
        "SKILL-BODY-MUST-BE-OMITTED",
        Sensitivity.PUBLIC,
    )
    result = _result("visible", "TOOL-BODY-MUST-BE-OMITTED")
    state = replace(
        _state(),
        tool_results=(result,),
        control_messages=(RunControlMessage("steer-1", ({"type": "text", "text": "keep this"},), "steer", 4),),
    )
    manager = ContextManager(
        system_rules=("rule",),
        inputs=ContextInputs(
            user_input=(user,),
            memories=(memory,),
            skills=(optional_without_reference,),
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget.generous_default(),
    )

    window = manager.build(
        state,
        purpose=ModelPurpose.PLANNING,
        projection=ContextProjection.OVERFLOW_REFERENCES,
    )

    window.ensure_model_ready()
    assert window.projection is ContextProjection.OVERFLOW_REFERENCES
    assert window.projection_hash.startswith("sha256:")
    assert {message.name for message in window.messages} >= {
        "offeragent-user_input",
        "offeragent-run-control",
        "offeragent-memory-reference",
        "visible",
    }
    assert ("skill-no-ref", "overflow_projection_no_reference") in {
        (item.context_id, item.reason) for item in window.omitted
    }
    rendered = repr(window.messages)
    assert "current request" in rendered
    assert "keep this" in rendered
    assert "MEMORY-BODY-MUST-BE-OMITTED" not in rendered
    assert "SKILL-BODY-MUST-BE-OMITTED" not in rendered
    assert "TOOL-BODY-MUST-BE-OMITTED" not in rendered
    tool = next(message for message in window.messages if message.name == "visible")
    assert tool.content[0].kind == "tool_result_reference"
    data = thaw_json(tool.content[0].data)
    assert data["summary"] == "result visible"
    assert data["status"] == "succeeded"
    assert data["artifactIds"] == ["artifact-visible"]
    assert data["sourceRefs"] == ["source-visible"]
    assert data["criticalHashes"] == {"$.afterState.hash": "sha256:" + "b" * 64}
    assert data["stateHashes"]["afterState"].startswith("sha256:")
