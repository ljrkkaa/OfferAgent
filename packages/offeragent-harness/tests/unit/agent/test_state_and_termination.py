from __future__ import annotations

from dataclasses import replace

import pytest

from offeragent_harness.agent.state import PendingWork, RunState, VaultWriteIntentBinding
from offeragent_harness.agent.termination import StopReason, evaluate_termination
from offeragent_harness.foundation import canonical_json_sha256, vault_write_intent_hash
from offeragent_harness.permissions import RiskClass
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffect,
    SideEffectClass,
    SideEffectKind,
    SideEffectState,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
)


def definition(side_effect: SideEffectClass) -> ToolDefinition:
    return ToolDefinition(
        name="vault.transaction" if side_effect is SideEffectClass.WRITE else "workspace.read",
        version="1",
        description="test",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={},
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.WRITE if side_effect is SideEffectClass.WRITE else RiskClass.READ,
        side_effect_class=side_effect,
        required_capabilities=frozenset({"vault"}),
        concurrency_safe=side_effect is SideEffectClass.READ,
        idempotent=True,
        retryable=side_effect is SideEffectClass.READ,
        timeout_ms=1_000,
        output_limit_bytes=1_024,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def state() -> RunState:
    return RunState(
        workspace_id="ws",
        session_id="session",
        turn_id="turn",
        run_id="run",
        lineage=AgentLineage.root("run"),
    )


def result(
    status: ToolResultStatus,
    *,
    tool_call_id: str = "call",
    side_effects: tuple[SideEffect, ...] = (),
) -> ToolResult:
    error = None
    if status is not ToolResultStatus.SUCCEEDED:
        error = ToolError(code=status.value, message=status.value, retryable=False, cancelled=False)
    return ToolResult(
        tool_call_id=tool_call_id,
        status=status,
        data=None,
        user_visible_summary=status.value,
        artifact_ids=(),
        source_refs=(),
        side_effects=side_effects,
        retryable=False,
        before_state=None,
        after_state=None,
        error=error,
    )


def vault_call(tool_call_id: str, *paths: str) -> ToolCall:
    return vault_call_with_operations(
        tool_call_id,
        [{"op": "replace", "path": path} for path in paths],
    )


def vault_call_with_operations(tool_call_id: str, operations: list[dict[str, str]]) -> ToolCall:
    write_definition = definition(SideEffectClass.WRITE)
    arguments: dict[str, object] = {"operations": operations}
    return call_for_definition(write_definition, tool_call_id, arguments)


def call_for_definition(
    tool_definition: ToolDefinition,
    tool_call_id: str,
    arguments: dict[str, object] | None = None,
) -> ToolCall:
    arguments = {} if arguments is None else arguments
    return ToolCall(
        tool_call_id=tool_call_id,
        run_id="run",
        workspace_id="ws",
        name=tool_definition.name,
        version=tool_definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=f"idem-{tool_call_id}",
        deadline=None,
        lineage=AgentLineage.root("run"),
        definition_fingerprint=tool_definition.fingerprint,
        result_sensitivity=tool_definition.result_sensitivity,
    )


def with_pending_call(current: RunState, call: ToolCall) -> RunState:
    return current.accept_tool_calls((call,))


def test_write_obligation_requires_real_terminal_tool_result() -> None:
    current = state().require_write_outcome("canonical plan requires a write outcome")
    decision = evaluate_termination(current, reason=StopReason.MODEL_FINISHED)
    assert not decision.can_compose
    assert decision.blockers == ("write_outcome_required",)

    read_definition = definition(SideEffectClass.READ)
    current = with_pending_call(current, call_for_definition(read_definition, "call")).record_tool_result(
        read_definition,
        result(ToolResultStatus.SUCCEEDED),
    )
    assert not evaluate_termination(current, reason=StopReason.MODEL_FINISHED).can_compose

    write_definition = definition(SideEffectClass.WRITE)
    current = with_pending_call(
        current,
        call_for_definition(write_definition, "write-call", {"operations": []}),
    ).record_tool_result(
        write_definition,
        result(ToolResultStatus.DENIED, tool_call_id="write-call"),
    )
    assert evaluate_termination(current, reason=StopReason.MODEL_FINISHED).can_compose


def test_tool_result_binding_is_exactly_once_and_call_ids_cannot_be_reused() -> None:
    tool_definition = definition(SideEffectClass.READ)
    call = call_for_definition(tool_definition, "bound-call")
    accepted = state().accept_tool_calls((call,))
    completed_result = result(ToolResultStatus.SUCCEEDED, tool_call_id=call.tool_call_id)

    completed = accepted.record_tool_result(tool_definition, completed_result)
    assert completed.record_tool_result(tool_definition, completed_result) is completed

    with pytest.raises(ValueError, match="conflicting duplicate ToolResult"):
        completed.record_tool_result(
            tool_definition,
            replace(completed_result, user_visible_summary="different result"),
        )
    with pytest.raises(ValueError, match="cannot reuse completed or previously bound state"):
        completed.accept_tool_calls((call,))

    orphan_binding = replace(
        state(),
        tool_result_sensitivities={call.tool_call_id: ResultSensitivity.WORKSPACE},
    )
    with pytest.raises(ValueError, match="cannot reuse completed or previously bound state"):
        orphan_binding.accept_tool_calls((call,))


@pytest.mark.parametrize(
    ("status", "effect_state"),
    [
        (ToolResultStatus.SUCCEEDED, SideEffectState.COMMITTED),
        (ToolResultStatus.CONFLICTED, SideEffectState.ROLLED_BACK),
        (ToolResultStatus.DENIED, SideEffectState.ATTEMPTED),
        (ToolResultStatus.FAILED, SideEffectState.ROLLED_BACK),
    ],
)
def test_structured_write_intent_requires_bound_call_and_real_target_side_effects(
    status: ToolResultStatus,
    effect_state: SideEffectState,
) -> None:
    intent = VaultWriteIntentBinding(
        request_hash="sha256:" + ("1" * 64),
        intent_hash=vault_write_intent_hash(("notes/offer.md",)),
        target_paths=("notes/offer.md",),
    )
    required = state().require_write_outcome("turn.start.explicit_write_intent", intent=intent)
    call = vault_call("bound-write", "notes/offer.md")

    empty = with_pending_call(required, call).record_tool_result(
        definition(SideEffectClass.WRITE),
        result(status, tool_call_id=call.tool_call_id),
    )
    assert not empty.write_obligation.satisfied

    unrelated = with_pending_call(required, call).record_tool_result(
        definition(SideEffectClass.WRITE),
        result(
            status,
            tool_call_id=call.tool_call_id,
            side_effects=(
                SideEffect(
                    kind=SideEffectKind.FILE_WRITE,
                    state=effect_state,
                    resource_id="vault:ws:notes/unrelated.md",
                    before_state=None,
                    after_state=None,
                ),
            ),
        ),
    )
    assert not unrelated.write_obligation.satisfied

    matching = with_pending_call(required, call).record_tool_result(
        definition(SideEffectClass.WRITE),
        result(
            status,
            tool_call_id=call.tool_call_id,
            side_effects=(
                SideEffect(
                    kind=SideEffectKind.FILE_WRITE,
                    state=effect_state,
                    resource_id="vault:ws:notes/offer.md",
                    before_state=None,
                    after_state=None,
                ),
            ),
        ),
    )
    assert matching.write_obligation.satisfied
    assert matching.write_obligation.outcomes[0].status is status


@pytest.mark.parametrize(
    ("status", "effect_state"),
    [
        (ToolResultStatus.CANCELLED, SideEffectState.ROLLED_BACK),
        (ToolResultStatus.TIMED_OUT, SideEffectState.ROLLED_BACK),
        (ToolResultStatus.PARTIAL, SideEffectState.COMMITTED),
        (ToolResultStatus.UNKNOWN_OUTCOME, SideEffectState.UNKNOWN),
    ],
)
def test_structured_write_intent_rejects_non_terminal_or_unknown_outcomes(
    status: ToolResultStatus,
    effect_state: SideEffectState,
) -> None:
    intent = VaultWriteIntentBinding(
        request_hash="sha256:" + ("1" * 64),
        intent_hash=vault_write_intent_hash(("notes/offer.md",)),
        target_paths=("notes/offer.md",),
    )
    call = vault_call("bound-write", "notes/offer.md")
    current = with_pending_call(
        state().require_write_outcome("turn.start.explicit_write_intent", intent=intent),
        call,
    ).record_tool_result(
        definition(SideEffectClass.WRITE),
        result(
            status,
            tool_call_id=call.tool_call_id,
            side_effects=(
                SideEffect(
                    kind=SideEffectKind.FILE_WRITE,
                    state=effect_state,
                    resource_id="vault:ws:notes/offer.md",
                    before_state=None,
                    after_state=None,
                ),
            ),
        ),
    )
    assert not current.write_obligation.satisfied
    assert current.write_obligation.outcomes == ()


@pytest.mark.parametrize(
    "effect_state",
    [SideEffectState.OBSERVED, SideEffectState.PARTIAL, SideEffectState.UNKNOWN],
)
def test_structured_write_intent_rejects_non_terminal_effect_state(effect_state: SideEffectState) -> None:
    intent = VaultWriteIntentBinding(
        request_hash="sha256:" + ("1" * 64),
        intent_hash=vault_write_intent_hash(("notes/offer.md",)),
        target_paths=("notes/offer.md",),
    )
    call = vault_call("bound-write", "notes/offer.md")
    current = with_pending_call(
        state().require_write_outcome("turn.start.explicit_write_intent", intent=intent),
        call,
    ).record_tool_result(
        definition(SideEffectClass.WRITE),
        result(
            ToolResultStatus.SUCCEEDED,
            tool_call_id=call.tool_call_id,
            side_effects=(
                SideEffect(
                    kind=SideEffectKind.FILE_WRITE,
                    state=effect_state,
                    resource_id="vault:ws:notes/offer.md",
                    before_state=None,
                    after_state=None,
                ),
            ),
        ),
    )
    assert not current.write_obligation.satisfied


@pytest.mark.parametrize(
    ("status", "effect_state"),
    [
        (ToolResultStatus.SUCCEEDED, SideEffectState.ROLLED_BACK),
        (ToolResultStatus.CONFLICTED, SideEffectState.COMMITTED),
        (ToolResultStatus.DENIED, SideEffectState.COMMITTED),
        (ToolResultStatus.FAILED, SideEffectState.COMMITTED),
    ],
)
def test_structured_write_intent_rejects_effect_state_inconsistent_with_outcome(
    status: ToolResultStatus,
    effect_state: SideEffectState,
) -> None:
    intent = VaultWriteIntentBinding(
        request_hash="sha256:" + ("1" * 64),
        intent_hash=vault_write_intent_hash(("notes/offer.md",)),
        target_paths=("notes/offer.md",),
    )
    call = vault_call("bound-write", "notes/offer.md")
    current = with_pending_call(
        state().require_write_outcome("turn.start.explicit_write_intent", intent=intent),
        call,
    ).record_tool_result(
        definition(SideEffectClass.WRITE),
        result(
            status,
            tool_call_id=call.tool_call_id,
            side_effects=(
                SideEffect(
                    kind=SideEffectKind.FILE_WRITE,
                    state=effect_state,
                    resource_id="vault:ws:notes/offer.md",
                    before_state=None,
                    after_state=None,
                ),
            ),
        ),
    )
    assert not current.write_obligation.satisfied
    assert current.write_obligation.outcomes == ()


def test_structured_write_intent_requires_operation_kind_and_covers_rename_destination() -> None:
    target = "notes/renamed.md"
    intent = VaultWriteIntentBinding(
        request_hash="sha256:" + ("1" * 64),
        intent_hash=vault_write_intent_hash((target,)),
        target_paths=(target,),
    )
    call = vault_call_with_operations(
        "rename",
        [{"op": "rename", "path": "notes/original.md", "destination": target}],
    )
    required = state().require_write_outcome("turn.start.explicit_write_intent", intent=intent)
    wrong_kind = with_pending_call(required, call).record_tool_result(
        definition(SideEffectClass.WRITE),
        result(
            ToolResultStatus.SUCCEEDED,
            tool_call_id=call.tool_call_id,
            side_effects=(
                SideEffect(
                    kind=SideEffectKind.FILE_WRITE,
                    state=SideEffectState.COMMITTED,
                    resource_id="vault:ws:notes/original.md",
                    before_state=None,
                    after_state=None,
                ),
            ),
        ),
    )
    assert not wrong_kind.write_obligation.satisfied

    renamed = with_pending_call(required, call).record_tool_result(
        definition(SideEffectClass.WRITE),
        result(
            ToolResultStatus.SUCCEEDED,
            tool_call_id=call.tool_call_id,
            side_effects=(
                SideEffect(
                    kind=SideEffectKind.FILE_RENAME,
                    state=SideEffectState.COMMITTED,
                    resource_id="vault:ws:notes/original.md",
                    before_state=None,
                    after_state=None,
                ),
            ),
        ),
    )
    assert renamed.write_obligation.satisfied


def test_structured_write_intent_accumulates_verified_path_coverage_across_transactions() -> None:
    targets = ("notes/offer.md", "notes/summary.md")
    intent = VaultWriteIntentBinding(
        request_hash="sha256:" + ("1" * 64),
        intent_hash=vault_write_intent_hash(targets),
        target_paths=targets,
    )
    current = state().require_write_outcome("turn.start.explicit_write_intent", intent=intent)
    for index, target in enumerate(targets, start=1):
        call = vault_call(f"write-{index}", target)
        current = with_pending_call(current, call).record_tool_result(
            definition(SideEffectClass.WRITE),
            result(
                ToolResultStatus.SUCCEEDED,
                tool_call_id=call.tool_call_id,
                side_effects=(
                    SideEffect(
                        kind=SideEffectKind.FILE_WRITE,
                        state=SideEffectState.COMMITTED,
                        resource_id=f"vault:ws:{target}",
                        before_state=None,
                        after_state=None,
                    ),
                ),
            ),
        )
        assert current.write_obligation.satisfied is (index == len(targets))
    assert tuple(outcome.covered_paths for outcome in current.write_obligation.outcomes) == (
        ("notes/offer.md",),
        ("notes/summary.md",),
    )


def test_vault_write_intent_binding_rejects_hash_drift_and_unsafe_persisted_paths() -> None:
    with pytest.raises(ValueError, match="hash does not match"):
        VaultWriteIntentBinding(
            request_hash="sha256:" + ("1" * 64),
            intent_hash="sha256:" + ("2" * 64),
            target_paths=("notes/offer.md",),
        )
    unsafe = "notes/CON.md"
    with pytest.raises(ValueError, match="unsafe relative path"):
        VaultWriteIntentBinding(
            request_hash="sha256:" + ("1" * 64),
            intent_hash=vault_write_intent_hash((unsafe,)),
            target_paths=(unsafe,),
        )


def test_pending_work_blocks_composer_even_without_write_obligation() -> None:
    current = replace(state(), pending=PendingWork(child_run_ids=frozenset({"child"})))
    decision = evaluate_termination(current, reason=StopReason.MODEL_FINISHED)
    assert not decision.can_compose
    assert decision.blockers == ("child_runs_pending",)


def test_unknown_write_outcome_allows_only_partial_manual_review_composition() -> None:
    current = state().require_write_outcome("write")
    unknown = result(
        ToolResultStatus.UNKNOWN_OUTCOME,
        side_effects=(
            SideEffect(
                kind=SideEffectKind.FILE_WRITE,
                state=SideEffectState.UNKNOWN,
                resource_id="vault:note.md",
                before_state=None,
                after_state=None,
            ),
        ),
    )
    write_definition = definition(SideEffectClass.WRITE)
    current = with_pending_call(
        current,
        call_for_definition(write_definition, "call", {"operations": []}),
    ).record_tool_result(write_definition, unknown)
    decision = evaluate_termination(current, reason=StopReason.MODEL_FINISHED)
    assert decision.can_compose
    assert decision.partial
