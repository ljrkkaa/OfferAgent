from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest

from offeragent_harness.ports import ClientToolInvocation, OperationCancelled
from offeragent_harness.runtime.client_tool_bridge import NamedPipeClientToolPort
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import ManualCancellationToken, ManualClock
from offeragent_harness.tools import ResultSensitivity, ToolCall, ToolResultStatus, canonical_json_sha256
from offeragent_harness.tools.dispatcher import InvocationAcknowledgementLost


def _wire_result() -> dict[str, Any]:
    return {
        "invocationId": "inv_1",
        "toolCallId": "call_1",
        "status": "succeeded",
        "output": {"afterHash": "sha256:" + "b" * 64},
        "userVisibleSummary": "applied",
        "beforeHash": "sha256:" + "a" * 64,
        "afterHash": "sha256:" + "b" * 64,
        "beforeState": {"revision": 1},
        "afterState": {"revision": 2},
        "workspaceRevision": 2,
        "artifactIds": [],
        "sourceReferenceIds": ["vault:notes/test.md"],
        "sideEffectFacts": [
            {
                "kind": "file_write",
                "state": "committed",
                "resourceId": "vault:notes/test.md",
                "beforeState": {"hash": "sha256:" + "a" * 64},
                "afterState": {"hash": "sha256:" + "b" * 64},
                "metadata": {"workspaceRevision": 2},
            }
        ],
        "actualOperations": [],
        "error": None,
    }


class Channel:
    def __init__(self, *, lose_ack: bool = False, block: bool = False) -> None:
        self.lose_ack = lose_ack
        self.block = block
        self.calls: list[str] = []
        self.params: list[Any] = []
        self.cancelled = asyncio.Event()

    async def request(self, method: str, params: Any, *, timeout_seconds: float | None = None) -> object:
        self.calls.append(method)
        self.params.append(params)
        if method == "client/tool/invoke":
            if self.block:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise
            if self.lose_ack:
                raise TimeoutError("ACK lost")
            return _wire_result()
        if method == "client/tool/preview":
            diff = "--- a/notes/test.md\n+++ b/notes/test.md\n+hello\n"
            return {
                "invocationId": "inv_1",
                "toolCallId": "call_1",
                "stateHash": "sha256:" + "a" * 64,
                "afterStateHash": "sha256:" + "b" * 64,
                "paths": ["notes/test.md"],
                "diff": diff,
                "diffSha256": f"sha256:{hashlib.sha256(diff.encode()).hexdigest()}",
                "hasUnsavedEditors": True,
                "hasOpenEditors": True,
                "pathStates": [
                    {
                        "path": "notes/test.md",
                        "beforeHash": "absent",
                        "afterHash": "sha256:" + "b" * 64,
                        "unsavedEditor": True,
                        "openEditor": True,
                    }
                ],
            }
        if method == "client/tool/commit-observe":
            return {
                "invocationId": "inv_1",
                "toolCallId": "call_1",
                "paths": ["notes/test.md"],
                "hasUnsavedEditors": False,
                "hasOpenEditors": False,
                "pathStates": [
                    {
                        "path": "notes/test.md",
                        "observedHash": "sha256:" + "b" * 64,
                        "unsavedEditor": False,
                        "openEditor": False,
                    }
                ],
            }
        if method == "client/tool/lookup":
            return {"invocationId": "inv_1", "found": True, "result": _wire_result()}
        if method == "client/tool/cancel":
            return {"invocationId": "inv_1", "accepted": True, "alreadyTerminal": False}
        raise AssertionError(method)


class Channels:
    def __init__(self, channel: Channel) -> None:
        self.current = channel

    def channel(self, workspace_id: str) -> Channel:
        assert workspace_id == "ws_test"
        return self.current

    @asynccontextmanager
    async def connection_lease(self, workspace_id: str) -> AsyncIterator[Channel]:
        yield self.channel(workspace_id)


def _invocation(clock: ManualClock, *, workspace_id: str = "ws_test") -> ClientToolInvocation:
    arguments = {
        "operations": [
            {
                "op": "create",
                "path": "notes/test.md",
                "content": "hello",
                "expectedHash": "absent",
            }
        ]
    }
    call = ToolCall(
        tool_call_id="call_1",
        run_id="run_1",
        workspace_id=workspace_id,
        name="vault.transaction",
        version="1",
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="idem_1",
        deadline=None,
        lineage=AgentLineage.root("run_1"),
        definition_fingerprint="sha256:" + "c" * 64,
        result_sensitivity=ResultSensitivity.WORKSPACE,
    )
    return ClientToolInvocation("inv_1", call, clock.utcnow() + timedelta(seconds=30))


@pytest.mark.asyncio
async def test_connection_lease_pins_exact_reverse_channel_for_preview() -> None:
    clock = ManualClock()
    original = Channel()
    replacement = Channel()
    channels = Channels(original)
    bridge = NamedPipeClientToolPort(workspace_id="ws_test", channels=channels, clock=clock)

    async with bridge.hold_connection() as leased:
        channels.current = replacement
        preview = await leased.preview(_invocation(clock), ManualCancellationToken())
        observation = await leased.observe_commit(
            _invocation(clock),
            ("notes/test.md",),
            ManualCancellationToken(),
        )

    assert preview.tool_call_id == "call_1"
    assert observation.path_states[0].observed_hash == "sha256:" + "b" * 64
    assert original.calls == ["client/tool/preview", "client/tool/commit-observe"]
    assert replacement.calls == []


@pytest.mark.asyncio
async def test_bridge_round_trips_complete_tool_result() -> None:
    clock = ManualClock()
    channel = Channel()
    bridge = NamedPipeClientToolPort(workspace_id="ws_test", channels=Channels(channel), clock=clock)
    result = await bridge.invoke(_invocation(clock), ManualCancellationToken())
    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.data == {"afterHash": "sha256:" + "b" * 64}
    assert result.side_effects[0].state.value == "committed"
    assert result.before_state == {"revision": 1} and result.after_state == {"revision": 2}


@pytest.mark.asyncio
async def test_public_vault_transaction_maps_to_private_obsidian_contract() -> None:
    clock = ManualClock()
    channel = Channel()
    bridge = NamedPipeClientToolPort(workspace_id="ws_test", channels=Channels(channel), clock=clock)
    public_arguments = {
        "operations": [
            {
                "op": "create",
                "path": "notes/test.md",
                "content": "hello",
                "expectedHash": "absent",
            }
        ]
    }
    invocation = _invocation(clock)
    call = invocation.call
    routed_call = type(call)(
        tool_call_id=call.tool_call_id,
        run_id=call.run_id,
        workspace_id=call.workspace_id,
        name=call.name,
        version=call.version,
        arguments=public_arguments,
        args_hash=canonical_json_sha256(public_arguments),
        idempotency_key=call.idempotency_key,
        deadline=call.deadline,
        lineage=call.lineage,
        definition_fingerprint=call.definition_fingerprint,
    )
    await bridge.invoke(
        ClientToolInvocation(invocation.invocation_id, routed_call, invocation.deadline),
        ManualCancellationToken(),
    )
    params = channel.params[0]
    assert params.name == "obsidian.vault.transaction"
    assert set(params.arguments) == {"transactionId", "operations"}
    assert params.arguments["operations"][0]["path"] == "notes/test.md"
    assert params.arguments["transactionId"].startswith("tx_")
    assert params.args_hash == canonical_json_sha256(params.arguments)


@pytest.mark.asyncio
async def test_preview_uses_same_private_mapping_and_returns_live_state() -> None:
    clock = ManualClock()
    channel = Channel()
    bridge = NamedPipeClientToolPort(workspace_id="ws_test", channels=Channels(channel), clock=clock)
    preview = await bridge.preview(_invocation(clock), ManualCancellationToken())
    params = channel.params[0]
    assert channel.calls == ["client/tool/preview"]
    assert params.name == "obsidian.vault.transaction"
    assert set(params.arguments) == {"transactionId", "operations"}
    assert preview.paths == ("notes/test.md",) and preview.has_unsaved_editors and preview.has_open_editors
    assert preview.path_states[0].path == "notes/test.md"


@pytest.mark.asyncio
async def test_ack_loss_recovers_via_durable_lookup_on_current_reconnected_channel() -> None:
    clock = ManualClock()
    channel = Channel(lose_ack=True)
    bridge = NamedPipeClientToolPort(workspace_id="ws_test", channels=Channels(channel), clock=clock)
    result = await bridge.invoke(_invocation(clock), ManualCancellationToken())
    assert result.status is ToolResultStatus.SUCCEEDED
    assert channel.calls == ["client/tool/invoke", "client/tool/lookup"]


@pytest.mark.asyncio
async def test_lookup_rejects_response_for_another_invocation() -> None:
    clock = ManualClock()
    channel = Channel()

    async def mismatched_lookup(method: str, params: Any, *, timeout_seconds: float | None = None) -> object:
        del params, timeout_seconds
        assert method == "client/tool/lookup"
        return {"invocationId": "inv_other", "found": False, "result": None}

    channel.request = mismatched_lookup  # type: ignore[method-assign]
    bridge = NamedPipeClientToolPort(workspace_id="ws_test", channels=Channels(channel), clock=clock)

    with pytest.raises(ValueError, match="requested invocation"):
        await bridge.lookup_result("inv_1", run_id="run_1")


@pytest.mark.asyncio
async def test_missing_durable_result_after_ack_loss_is_explicit_unknown_ack() -> None:
    clock = ManualClock()
    channel = Channel(lose_ack=True)

    async def no_lookup(method: str, params: Any, *, timeout_seconds: float | None = None) -> object:
        if method == "client/tool/invoke":
            raise TimeoutError("ACK lost")
        return {"invocationId": "inv_1", "found": False, "result": None}

    channel.request = no_lookup  # type: ignore[method-assign]
    bridge = NamedPipeClientToolPort(workspace_id="ws_test", channels=Channels(channel), clock=clock)
    with pytest.raises(InvocationAcknowledgementLost):
        await bridge.invoke(_invocation(clock), ManualCancellationToken())


@pytest.mark.asyncio
async def test_bridge_rejects_cross_vault_calls_before_reverse_request() -> None:
    clock = ManualClock()
    channel = Channel()
    bridge = NamedPipeClientToolPort(workspace_id="ws_test", channels=Channels(channel), clock=clock)
    with pytest.raises(ValueError, match="different Workspace"):
        await bridge.invoke(_invocation(clock, workspace_id="ws_other"), ManualCancellationToken())
    assert channel.calls == []


@pytest.mark.asyncio
async def test_cancellation_propagates_to_pending_reverse_request() -> None:
    clock = ManualClock()
    channel = Channel(block=True)
    bridge = NamedPipeClientToolPort(workspace_id="ws_test", channels=Channels(channel), clock=clock)
    cancellation = ManualCancellationToken()
    task = asyncio.create_task(bridge.invoke(_invocation(clock), cancellation))
    await asyncio.sleep(0)
    cancellation.cancel()
    with pytest.raises(OperationCancelled):
        await task
    assert channel.cancelled.is_set()
