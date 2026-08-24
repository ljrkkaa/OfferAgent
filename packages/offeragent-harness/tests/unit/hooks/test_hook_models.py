from __future__ import annotations

from offeragent_harness.hooks import (
    HookDecision,
    HookDefinition,
    HookEvent,
    HookExecutionContext,
    HookImplementation,
    HookInvocation,
    HookLayer,
    HookOutput,
    HookScope,
    HookToolInput,
    merge_argument_patch,
    resolve_hook_plan,
)
from offeragent_harness.tools import canonical_json_sha256


def _invocation(event: HookEvent = HookEvent.TURN_START) -> HookInvocation:
    return HookInvocation(
        "hook-invocation-1",
        "hook-chain-1",
        event,
        HookExecutionContext("system", "user-1", "workspace-1", "session-1", True),
        "run-1",
    )


def _hook(scope: HookScope, owner: str, hook_id: str, *, priority: int = 0) -> HookDefinition:
    return HookDefinition(
        hook_id,
        scope,
        owner,
        HookEvent.TURN_START,
        HookImplementation.BUILTIN,
        priority=priority,
        handler_id=hook_id,
    )


def test_layer_resolution_is_managed_user_workspace_session_then_priority() -> None:
    layers = (
        HookLayer(HookScope.SESSION, "session-1", 1, (_hook(HookScope.SESSION, "session-1", "session"),)),
        HookLayer(
            HookScope.USER,
            "user-1",
            2,
            (
                _hook(HookScope.USER, "user-1", "user-low"),
                _hook(HookScope.USER, "user-1", "user-high", priority=10),
            ),
        ),
        HookLayer(HookScope.MANAGED, "system", 3, (_hook(HookScope.MANAGED, "system", "managed"),)),
        HookLayer(
            HookScope.WORKSPACE,
            "workspace-1",
            4,
            (_hook(HookScope.WORKSPACE, "workspace-1", "workspace"),),
        ),
    )

    plan = resolve_hook_plan(layers, _invocation())

    assert [hook.hook_id for hook in plan.hooks] == [
        "managed",
        "user-high",
        "user-low",
        "workspace",
        "session",
    ]


def test_managed_denial_cannot_be_overridden_and_can_block_hook_ids() -> None:
    layers = (
        HookLayer(
            HookScope.MANAGED,
            "system",
            1,
            denied_events=frozenset({HookEvent.TURN_START}),
            denied_hook_ids=frozenset({"workspace"}),
        ),
        HookLayer(
            HookScope.WORKSPACE,
            "workspace-1",
            1,
            (_hook(HookScope.WORKSPACE, "workspace-1", "workspace"),),
        ),
    )

    plan = resolve_hook_plan(layers, _invocation())

    assert plan.managed_denied
    assert not plan.hooks


def test_merge_patch_is_bounded_and_does_not_mutate_original() -> None:
    original = {"path": "old", "options": {"keep": True, "remove": 1}}

    merged = merge_argument_patch(original, {"path": "new", "options": {"remove": None, "add": 2}})

    assert dict(original) == {"path": "old", "options": {"keep": True, "remove": 1}}
    assert dict(merged) == {"path": "new", "options": {"keep": True, "add": 2}}


def test_pre_tool_input_requires_canonical_args_hash() -> None:
    arguments = {"path": "notes/a.md"}
    tool = HookToolInput(
        "call-1",
        "workspace.read",
        "1",
        canonical_json_sha256({"definition": 1}),
        arguments,
        canonical_json_sha256(arguments),
        "idem-1",
    )

    invocation = HookInvocation(
        "hook-invocation-1",
        "hook-chain-1",
        HookEvent.PRE_TOOL_USE,
        HookExecutionContext("system", "user-1", "workspace-1", "session-1", False),
        "run-1",
        tool=tool,
    )

    assert invocation.tool == tool
    assert HookOutput(HookDecision.ASK).decision is HookDecision.ASK
