from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offeragent_harness.agent.budget_checkpoint import BudgetCheckpoint
from offeragent_harness.agent.budgets import BudgetDelta, RunBudget
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
    UserImageProvenance,
)
from offeragent_harness.agent.state import (
    PendingWork,
    RunControlMessage,
    RunPhase,
    RunState,
)
from offeragent_harness.models import ModelContentBlock, ModelPurpose, ModelRole, thaw_json
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


def _image_block(
    artifact_id: str,
    content: bytes,
    *,
    width: int = 1,
    height: int = 1,
    detail: str = "high",
) -> ModelContentBlock:
    return ModelContentBlock(
        "image",
        {
            "artifactId": artifact_id,
            "mediaType": "image/png",
            "contentHash": "sha256:" + hashlib.sha256(content).hexdigest(),
            "sizeBytes": len(content),
            "width": width,
            "height": height,
            "detail": detail,
        },
        binary_data=content,
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
        "outcomes": [],
    }
    assert snapshot["pending"] == {
        "toolCallIds": ["call-a", "call-z"],
        "approvalIds": ["approval-1"],
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


def test_run_snapshot_exposes_authoritative_local_date_and_iso_week() -> None:
    started_at = datetime(2026, 7, 15, 2, 30, tzinfo=timezone.utc)
    budget = RunBudget(8, 8, 2, 60, 10_000, 10_000, Decimal("1"), 10_000, 2)
    checkpoint = BudgetCheckpoint(
        budget=budget,
        started_at=started_at,
        used=BudgetDelta(),
        reserved=BudgetDelta(),
        captured_at=started_at,
        elapsed_seconds=0,
    )
    state = replace(_state(), budget_checkpoint=checkpoint)
    manager = ContextManager(
        system_rules=("use the explicit runtime clock",),
        inputs=ContextInputs(
            user_input=(_fragment("user-time", ContextLayer.USER_INPUT, "制定本周计划", Sensitivity.PUBLIC),)
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget.generous_default(),
        local_timezone=timezone(timedelta(hours=8), "China Standard Time"),
    )

    window = manager.build(state, purpose=ModelPurpose.PLANNING)
    snapshot = thaw_json(window.messages[1].content[0].data)

    assert snapshot["time"] == {
        "runStartedAtUtc": "2026-07-15T02:30:00+00:00",
        "localDateTime": "2026-07-15T10:30:00+08:00",
        "localDate": "2026-07-15",
        "utcOffset": "+08:00",
        "timeZoneName": "China Standard Time",
        "isoWeek": {"year": 2026, "week": 29, "startDate": "2026-07-13", "endDate": "2026-07-19"},
    }


def test_current_user_input_is_never_displaced_by_optional_conversation_history() -> None:
    history = (
        ContextFragment(
            "conversation:previous:user",
            ContextLayer.CONVERSATION,
            "previous request",
            Sensitivity.WORKSPACE,
            conversation_turn_id="turn_previous",
        ),
        ContextFragment(
            "conversation:previous:assistant",
            ContextLayer.CONVERSATION,
            "previous answer " * 100,
            Sensitivity.WORKSPACE,
            role=ModelRole.ASSISTANT,
            conversation_turn_id="turn_previous",
        ),
    )
    manager = ContextManager(
        system_rules=("system",),
        inputs=ContextInputs(
            user_input=(_fragment("user", ContextLayer.USER_INPUT, "current request", Sensitivity.PUBLIC),),
            conversation=history,
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
    assert {
        ("conversation:previous:user", "context_budget"),
        ("conversation:previous:assistant", "context_budget"),
    }.issubset({(item.context_id, item.reason) for item in window.omitted})
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

    window = manager.build(_state(), purpose=ModelPurpose.RESPONDING)

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


def test_user_input_keeps_ephemeral_image_bytes_out_of_projection_identity() -> None:
    content = b"\x89PNG\r\n\x1a\nimage"
    image = _image_block("art_one", content)
    fragment = ContextFragment(
        "user",
        ContextLayer.USER_INPUT,
        '{"type":"image","artifactId":"art_one"}',
        Sensitivity.PRIVATE,
        artifact_ids=("art_one",),
        model_blocks=(image,),
        image_provenance=UserImageProvenance.CURRENT_SUBMISSION,
    )
    manager = ContextManager(
        system_rules=("rule",),
        inputs=ContextInputs(user_input=(fragment,)),
        visibility=ContextVisibilityPolicy.cloud_model(),
        budget=ContextBudget.generous_default(),
    )

    window = manager.build(_state(), purpose=ModelPurpose.PLANNING)
    message = next(item for item in window.messages if item.name == "offeragent-user_input")
    assert message.content[-1].kind == "image"
    assert message.content[-1].binary_data == b"\x89PNG\r\n\x1a\nimage"
    assert "binary_data" not in repr(message)


def test_image_dimensions_and_detail_consume_catalog_token_budget() -> None:
    content = b"attested-image"

    def estimated(detail: str) -> int:
        image = _image_block("art_dimensioned", content, width=1_920, height=1_080, detail=detail)
        fragment = ContextFragment(
            f"user-{detail}",
            ContextLayer.USER_INPUT,
            "inspect",
            Sensitivity.PRIVATE,
            artifact_ids=("art_dimensioned",),
            model_blocks=(image,),
            image_provenance=UserImageProvenance.CURRENT_SUBMISSION,
        )
        manager = ContextManager(
            system_rules=("rule",),
            inputs=ContextInputs(user_input=(fragment,)),
            visibility=ContextVisibilityPolicy.cloud_model(),
            budget=ContextBudget.generous_default(),
        )
        window = manager.build(
            replace(_state(), tool_results=(), tool_result_sensitivities={}),
            purpose=ModelPurpose.PLANNING,
        )
        return window.estimated_tokens

    high = estimated("high")
    original = estimated("original")

    assert high >= 4_000
    assert original > high


def test_private_user_image_without_verified_current_attachment_provenance_is_denied() -> None:
    content = b"\x89PNG\r\n\x1a\nunverified"
    image = _image_block("art_unverified", content)

    with pytest.raises(ValueError, match="provenance"):
        ContextFragment(
            "unverified-private-image",
            ContextLayer.USER_INPUT,
            '{"type":"image"}',
            Sensitivity.PRIVATE,
            artifact_ids=("art_unverified",),
            model_blocks=(image,),
        )


def test_oversized_current_user_image_requires_compaction_without_reference_downgrade() -> None:
    image = _image_block("art_large", b"\x89PNG\r\n\x1a\nimage")
    fragment = ContextFragment(
        "large-user-image",
        ContextLayer.USER_INPUT,
        "x" * 300_000,
        Sensitivity.PRIVATE,
        artifact_ids=("art_large",),
        content_hash="sha256:" + "4" * 64,
        model_blocks=(image,),
        image_provenance=UserImageProvenance.CURRENT_SUBMISSION,
    )
    manager = ContextManager(
        system_rules=("rule",),
        inputs=ContextInputs(user_input=(fragment,)),
        visibility=ContextVisibilityPolicy.cloud_model(),
        budget=ContextBudget.generous_default(),
    )

    state = replace(_state(), tool_results=(), tool_result_sensitivities={})
    window = manager.build(state, purpose=ModelPurpose.PLANNING)

    assert "large-user-image" not in window.included_context_ids
    assert any(
        item.context_id == "large-user-image" and item.reason == "artifactization_required" for item in window.omitted
    )
    assert all(block.kind != "context_reference" for message in window.messages for block in message.content)
    with pytest.raises(ContextCompactionRequired):
        window.ensure_model_ready()


def test_secret_user_image_never_bypasses_cloud_visibility() -> None:
    content = b"\x89PNG\r\n\x1a\nsecret"
    image = _image_block("art_secret", content)

    with pytest.raises(ValueError, match="private"):
        ContextFragment(
            "secret-user-image",
            ContextLayer.USER_INPUT,
            '{"type":"image","artifactId":"art_secret"}',
            Sensitivity.SECRET,
            artifact_ids=("art_secret",),
            model_blocks=(image,),
            image_provenance=UserImageProvenance.CURRENT_SUBMISSION,
        )


def test_conversation_history_keeps_the_newest_complete_turn_and_its_user_images() -> None:
    historical_image = _image_block("art_history", b"history-png")
    conversation = (
        ContextFragment(
            "conversation:old:user",
            ContextLayer.CONVERSATION,
            "old question",
            Sensitivity.WORKSPACE,
            conversation_turn_id="turn_old",
        ),
        ContextFragment(
            "conversation:old:assistant",
            ContextLayer.CONVERSATION,
            "old answer",
            Sensitivity.WORKSPACE,
            role=ModelRole.ASSISTANT,
            conversation_turn_id="turn_old",
        ),
        ContextFragment(
            "conversation:new:user",
            ContextLayer.CONVERSATION,
            "new image question",
            Sensitivity.PRIVATE,
            artifact_ids=("art_history",),
            model_blocks=(historical_image,),
            image_provenance=UserImageProvenance.RETAINED_CONVERSATION,
            conversation_turn_id="turn_new",
        ),
        ContextFragment(
            "conversation:new:assistant",
            ContextLayer.CONVERSATION,
            "new image answer",
            Sensitivity.WORKSPACE,
            role=ModelRole.ASSISTANT,
            conversation_turn_id="turn_new",
        ),
    )
    manager = ContextManager(
        system_rules=("system",),
        inputs=ContextInputs(
            user_input=(_fragment("current", ContextLayer.USER_INPUT, "follow up", Sensitivity.PUBLIC),),
            conversation=conversation,
        ),
        visibility=ContextVisibilityPolicy.cloud_model(),
        budget=ContextBudget(
            max_messages=5,
            max_total_bytes=10_000,
            max_estimated_tokens=10_000,
            max_item_bytes=4_000,
            max_images=2,
            max_image_bytes=1_024,
        ),
    )

    state = replace(_state(), tool_results=(), tool_result_sensitivities={})
    window = manager.build(state, purpose=ModelPurpose.PLANNING)

    assert window.included_context_ids == (
        "system:rules",
        "run:run:snapshot",
        "conversation:new:user",
        "conversation:new:assistant",
        "current",
    )
    assert {
        ("conversation:old:user", "context_budget"),
        ("conversation:old:assistant", "context_budget"),
    }.issubset({(item.context_id, item.reason) for item in window.omitted})
    history_user = next(
        message
        for message in window.messages
        if message.role is ModelRole.USER and any(block.kind == "image" for block in message.content)
    )
    assert [block.binary_data for block in history_user.content if block.kind == "image"] == [b"history-png"]
    assert window.used_images == 1
    assert window.used_image_bytes == len(b"history-png")


def test_conversation_context_rejects_duplicate_turn_groups() -> None:
    conversation = tuple(
        ContextFragment(
            f"conversation:{pair_index}:{role.value}",
            ContextLayer.CONVERSATION,
            f"{role.value} {pair_index}",
            Sensitivity.WORKSPACE,
            role=role,
            conversation_turn_id="turn_duplicate",
        )
        for pair_index in range(2)
        for role in (ModelRole.USER, ModelRole.ASSISTANT)
    )

    with pytest.raises(ValueError, match="unique Turn"):
        ContextInputs(
            user_input=(_fragment("current", ContextLayer.USER_INPUT, "follow up", Sensitivity.PUBLIC),),
            conversation=conversation,
        )


def test_rejected_conversation_turn_cuts_off_every_older_turn() -> None:
    conversation = (
        ContextFragment(
            "conversation:old:user",
            ContextLayer.CONVERSATION,
            "old question",
            Sensitivity.WORKSPACE,
            conversation_turn_id="turn_old",
        ),
        ContextFragment(
            "conversation:old:assistant",
            ContextLayer.CONVERSATION,
            "old answer",
            Sensitivity.WORKSPACE,
            role=ModelRole.ASSISTANT,
            conversation_turn_id="turn_old",
        ),
        ContextFragment(
            "conversation:oversized:user",
            ContextLayer.CONVERSATION,
            "x" * 1_000,
            Sensitivity.WORKSPACE,
            conversation_turn_id="turn_oversized",
        ),
        ContextFragment(
            "conversation:oversized:assistant",
            ContextLayer.CONVERSATION,
            "oversized answer",
            Sensitivity.WORKSPACE,
            role=ModelRole.ASSISTANT,
            conversation_turn_id="turn_oversized",
        ),
        ContextFragment(
            "conversation:new:user",
            ContextLayer.CONVERSATION,
            "new question",
            Sensitivity.WORKSPACE,
            conversation_turn_id="turn_new",
        ),
        ContextFragment(
            "conversation:new:assistant",
            ContextLayer.CONVERSATION,
            "new answer",
            Sensitivity.WORKSPACE,
            role=ModelRole.ASSISTANT,
            conversation_turn_id="turn_new",
        ),
    )
    manager = ContextManager(
        system_rules=("system",),
        inputs=ContextInputs(
            user_input=(_fragment("current", ContextLayer.USER_INPUT, "follow up", Sensitivity.PUBLIC),),
            conversation=conversation,
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget(
            max_messages=12,
            max_total_bytes=20_000,
            max_estimated_tokens=20_000,
            max_item_bytes=500,
        ),
    )

    window = manager.build(
        replace(_state(), tool_results=(), tool_result_sensitivities={}),
        purpose=ModelPurpose.PLANNING,
    )

    assert "conversation:new:user" in window.included_context_ids
    assert "conversation:new:assistant" in window.included_context_ids
    assert "conversation:old:user" not in window.included_context_ids
    assert "conversation:old:assistant" not in window.included_context_ids
    assert {
        "conversation:old:user",
        "conversation:old:assistant",
        "conversation:oversized:user",
        "conversation:oversized:assistant",
    }.issubset({item.context_id for item in window.omitted})


def test_conversation_budget_cutoff_does_not_suppress_smaller_non_conversation_context() -> None:
    conversation = (
        ContextFragment(
            "conversation:previous:user",
            ContextLayer.CONVERSATION,
            "previous question",
            Sensitivity.WORKSPACE,
            conversation_turn_id="turn_previous",
        ),
        ContextFragment(
            "conversation:previous:assistant",
            ContextLayer.CONVERSATION,
            "previous answer",
            Sensitivity.WORKSPACE,
            role=ModelRole.ASSISTANT,
            conversation_turn_id="turn_previous",
        ),
    )
    manager = ContextManager(
        system_rules=("system",),
        inputs=ContextInputs(
            user_input=(_fragment("current", ContextLayer.USER_INPUT, "follow up", Sensitivity.PUBLIC),),
            conversation=conversation,
            skills=(_fragment("small-skill", ContextLayer.SKILLS, "small skill", Sensitivity.PUBLIC),),
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget(
            max_messages=4,
            max_total_bytes=20_000,
            max_estimated_tokens=20_000,
            max_item_bytes=4_000,
        ),
    )

    window = manager.build(
        replace(_state(), tool_results=(), tool_result_sensitivities={}),
        purpose=ModelPurpose.PLANNING,
    )

    assert "small-skill" in window.included_context_ids
    assert "conversation:previous:user" not in window.included_context_ids
    assert "conversation:previous:assistant" not in window.included_context_ids


def test_provider_overflow_projection_drops_at_least_the_oldest_complete_conversation_turn() -> None:
    conversation = tuple(
        ContextFragment(
            f"conversation:{turn_id}:{role.value}",
            ContextLayer.CONVERSATION,
            f"{role.value} {turn_id}",
            Sensitivity.WORKSPACE,
            role=role,
            conversation_turn_id=turn_id,
        )
        for turn_id in ("turn_old", "turn_new")
        for role in (ModelRole.USER, ModelRole.ASSISTANT)
    )
    manager = ContextManager(
        system_rules=("system",),
        inputs=ContextInputs(
            user_input=(_fragment("current", ContextLayer.USER_INPUT, "follow up", Sensitivity.PUBLIC),),
            conversation=conversation,
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget.generous_default(),
    )

    window = manager.build(
        replace(_state(), tool_results=(), tool_result_sensitivities={}),
        purpose=ModelPurpose.PLANNING,
        projection=ContextProjection.OVERFLOW_REFERENCES,
    )

    assert "conversation:turn_old:user" not in window.included_context_ids
    assert "conversation:turn_old:assistant" not in window.included_context_ids
    assert "conversation:turn_new:user" in window.included_context_ids
    assert "conversation:turn_new:assistant" in window.included_context_ids


def test_current_multi_image_batch_is_all_or_context_overflow_when_image_budget_cannot_fit() -> None:
    images = tuple(_image_block(f"art_{index}", f"image-{index}".encode()) for index in (1, 2))
    current = ContextFragment(
        "current-images",
        ContextLayer.USER_INPUT,
        "two ordered pages",
        Sensitivity.PRIVATE,
        artifact_ids=("art_1", "art_2"),
        model_blocks=images,
        image_provenance=UserImageProvenance.CURRENT_SUBMISSION,
    )
    manager = ContextManager(
        system_rules=("system",),
        inputs=ContextInputs(user_input=(current,)),
        visibility=ContextVisibilityPolicy.cloud_model(),
        budget=ContextBudget(
            max_messages=8,
            max_total_bytes=10_000,
            max_estimated_tokens=10_000,
            max_item_bytes=4_000,
            max_images=1,
            max_image_bytes=1_024,
        ),
    )

    window = manager.build(_state(), purpose=ModelPurpose.PLANNING)

    assert "current-images" not in window.included_context_ids
    assert all(block.kind != "image" for message in window.messages for block in message.content)
    assert any(item.context_id == "current-images" and item.reason == "context_budget" for item in window.omitted)
    with pytest.raises(ContextCompactionRequired):
        window.ensure_model_ready()


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
