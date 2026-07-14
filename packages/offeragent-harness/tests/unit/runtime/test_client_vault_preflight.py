from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.agent import BudgetLedger, RunBudget
from offeragent_harness.ports import (
    ClientToolCommitObservation,
    ClientToolCommitPathState,
    ClientToolInvocation,
    ClientToolPathState,
    ClientToolPreview,
)
from offeragent_harness.protocol.schemas import build_examples
from offeragent_harness.runtime.client_tool_bridge import NamedPipeClientToolPort
from offeragent_harness.runtime.client_vault_preflight import ClientBoundVaultTransaction
from offeragent_harness.runtime.named_pipe import ConnectionRole, DuplexJsonRpcConnection
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import ManualCancellationToken, ManualClock
from offeragent_harness.tools import PreflightConflict, ToolCall, ToolResultStatus, canonical_json_sha256
from offeragent_harness.vault import VaultTransactionCoordinator, content_hash, vault_transaction_definition

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Client:
    def __init__(self, *, before_hash: str, after_hash: str) -> None:
        self.before_hash = before_hash
        self.after_hash = after_hash
        self.open_editor = False
        self.unsaved_editor = False
        self.calls = 0
        self.fail_at: int | None = None
        self.connection_available = True
        self.disconnect_on_commit_observation = False
        self.open_on_commit_observation = False
        self.unsaved_on_commit_observation = False
        self.commit_observed_hash: str | None = None
        self.active_leases = 0
        self.released_leases = 0
        self.commit_observations = 0

    @asynccontextmanager
    async def hold_connection(self) -> AsyncIterator[Client]:
        if not self.connection_available:
            raise ConnectionError("plugin disconnected before commit lease")
        self.active_leases += 1
        try:
            yield self
        finally:
            self.active_leases -= 1
            self.released_leases += 1

    async def preview(self, invocation: ClientToolInvocation, cancellation: Any) -> ClientToolPreview:
        cancellation.checkpoint()
        self.calls += 1
        if self.fail_at == self.calls:
            raise ConnectionError("plugin disconnected")
        diff = b"--- a/note.md\n+++ b/note.md\n+live edit\n"
        return ClientToolPreview(
            invocation_id=invocation.invocation_id,
            tool_call_id=invocation.call.tool_call_id,
            state_hash="sha256:" + "a" * 64,
            after_state_hash="sha256:" + "b" * 64,
            paths=("note.md",),
            diff=diff,
            diff_sha256=f"sha256:{hashlib.sha256(diff).hexdigest()}",
            has_unsaved_editors=self.unsaved_editor,
            has_open_editors=self.open_editor,
            path_states=(
                ClientToolPathState(
                    "note.md",
                    self.before_hash,
                    self.after_hash,
                    self.unsaved_editor,
                    self.open_editor,
                ),
            ),
        )

    async def observe_commit(
        self,
        invocation: ClientToolInvocation,
        paths: tuple[str, ...],
        cancellation: Any,
    ) -> ClientToolCommitObservation:
        cancellation.checkpoint()
        self.commit_observations += 1
        if self.disconnect_on_commit_observation:
            raise ConnectionError("plugin disconnected after final preview")
        open_editor = self.open_on_commit_observation or self.unsaved_on_commit_observation
        return ClientToolCommitObservation(
            invocation_id=invocation.invocation_id,
            tool_call_id=invocation.call.tool_call_id,
            paths=paths,
            has_unsaved_editors=self.unsaved_on_commit_observation,
            has_open_editors=open_editor,
            path_states=(
                ClientToolCommitPathState(
                    "note.md",
                    self.commit_observed_hash or self.after_hash,
                    self.unsaved_on_commit_observation,
                    open_editor,
                ),
            ),
        )


class TrashClient:
    def __init__(self, before_hash: str) -> None:
        self.before_hash = before_hash
        self.calls = 0
        self.trash_path: str | None = None

    @asynccontextmanager
    async def hold_connection(self) -> AsyncIterator[TrashClient]:
        yield self

    async def preview(self, invocation: ClientToolInvocation, cancellation: Any) -> ClientToolPreview:
        cancellation.checkpoint()
        self.calls += 1
        call = invocation.call
        source = "note.md"
        identity = hashlib.sha256(
            f"{call.run_id}:{call.tool_call_id}:{call.args_hash}:0:{source}".encode()
        ).hexdigest()[:20]
        self.trash_path = f".trash/offeragent/{identity}-note.md"
        diff = (f"--- /dev/null\n+++ b/{self.trash_path}\n+before\n--- a/{source}\n+++ /dev/null\n-before\n").encode()
        return ClientToolPreview(
            invocation_id=invocation.invocation_id,
            tool_call_id=call.tool_call_id,
            state_hash="sha256:" + "c" * 64,
            after_state_hash="sha256:" + "d" * 64,
            paths=(self.trash_path, source),
            diff=diff,
            diff_sha256=f"sha256:{hashlib.sha256(diff).hexdigest()}",
            has_unsaved_editors=False,
            has_open_editors=False,
            path_states=(
                ClientToolPathState(self.trash_path, "absent", self.before_hash, False, False),
                ClientToolPathState(source, self.before_hash, "absent", False, False),
            ),
        )

    async def observe_commit(
        self,
        invocation: ClientToolInvocation,
        paths: tuple[str, ...],
        cancellation: Any,
    ) -> ClientToolCommitObservation:
        cancellation.checkpoint()
        assert self.trash_path is not None
        return ClientToolCommitObservation(
            invocation_id=invocation.invocation_id,
            tool_call_id=invocation.call.tool_call_id,
            paths=paths,
            has_unsaved_editors=False,
            has_open_editors=False,
            path_states=(
                ClientToolCommitPathState(self.trash_path, self.before_hash, False, False),
                ClientToolCommitPathState("note.md", "absent", False, False),
            ),
        )


class _MemoryPipeStream:
    def __init__(self) -> None:
        self.peer: _MemoryPipeStream | None = None
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False

    async def read(self, max_bytes: int) -> bytes:
        del max_bytes
        value = await self.incoming.get()
        return b"" if value is None else value

    async def write(self, data: bytes) -> None:
        if self.closed or self.peer is None or self.peer.closed:
            raise OSError("memory Pipe is disconnected")
        await self.peer.incoming.put(bytes(data))

    def cancel_pending_io(self) -> None:
        self.incoming.put_nowait(None)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.incoming.put_nowait(None)
        if self.peer is not None:
            self.peer.incoming.put_nowait(None)


def _memory_pipe_pair() -> tuple[_MemoryPipeStream, _MemoryPipeStream]:
    first = _MemoryPipeStream()
    second = _MemoryPipeStream()
    first.peer = second
    second.peer = first
    return first, second


class _WorkerPipeDispatcher:
    def require_ready(self) -> None:
        return

    async def dispatch(
        self,
        method: str,
        params: Mapping[str, Any],
        cancellation: Any,
        *,
        context: Any = None,
    ) -> object:
        del params, cancellation, context
        if method == "initialize":
            return build_examples()["initialize.response.json"]["result"]
        raise AssertionError(method)


class _DisconnectingPluginDispatcher:
    def __init__(
        self,
        stream: _MemoryPipeStream,
        before_hash: str,
        after_hash: str,
        cas_barrier_started: threading.Event,
        disconnected: threading.Event,
    ) -> None:
        self.stream = stream
        self.before_hash = before_hash
        self.after_hash = after_hash
        self.cas_barrier_started = cas_barrier_started
        self.disconnected = disconnected
        self.previews = 0
        self.commit_observe_started = asyncio.Event()
        self.disconnect_task: asyncio.Task[None] | None = None

    def require_ready(self) -> None:
        return

    async def dispatch(
        self,
        method: str,
        params: Mapping[str, Any],
        cancellation: Any,
        *,
        context: Any = None,
    ) -> object:
        del cancellation, context
        if method == "client/tool/preview":
            self.previews += 1
            if self.previews == 3:
                self.disconnect_task = asyncio.create_task(self._disconnect_after_cas_barrier())
            diff = "--- a/note.md\n+++ b/note.md\n+live edit\n"
            return {
                "invocationId": params["invocationId"],
                "toolCallId": params["toolCallId"],
                "stateHash": "sha256:" + "a" * 64,
                "afterStateHash": "sha256:" + "b" * 64,
                "paths": ["note.md"],
                "diff": diff,
                "diffSha256": f"sha256:{hashlib.sha256(diff.encode()).hexdigest()}",
                "hasUnsavedEditors": False,
                "hasOpenEditors": False,
                "pathStates": [
                    {
                        "path": "note.md",
                        "beforeHash": self.before_hash,
                        "afterHash": self.after_hash,
                        "unsavedEditor": False,
                        "openEditor": False,
                    }
                ],
            }
        if method == "client/tool/commit-observe":
            self.commit_observe_started.set()
            raise AssertionError("post-commit observation must not cross a disconnected Pipe")
        raise AssertionError(method)

    async def _disconnect_after_cas_barrier(self) -> None:
        await asyncio.to_thread(self.cas_barrier_started.wait)
        await self.stream.close()
        self.disconnected.set()


class _FixedConnectionProvider:
    def __init__(self, connection: DuplexJsonRpcConnection) -> None:
        self.connection = connection

    def channel(self, workspace_id: str) -> DuplexJsonRpcConnection:
        assert workspace_id == "ws_1"
        return self.connection

    @asynccontextmanager
    async def connection_lease(self, workspace_id: str) -> AsyncIterator[DuplexJsonRpcConnection]:
        yield self.channel(workspace_id)


def _budget() -> BudgetLedger:
    return BudgetLedger(
        RunBudget(8, 8, 2, 60, 100_000, 100_000, Decimal("10"), 1_000_000, 4, 1),
        started_at=NOW,
    )


def _call(before_hash: str) -> ToolCall:
    definition = vault_transaction_definition()
    arguments = {
        "operations": [
            {
                "op": "append",
                "path": "note.md",
                "content": "+after",
                "expectedHash": before_hash,
            }
        ]
    }
    return ToolCall(
        tool_call_id="call_1",
        run_id="run_1",
        workspace_id="ws_1",
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="idem_1",
        deadline=None,
        lineage=AgentLineage.root("run_1"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


def _trash_call(before_hash: str) -> ToolCall:
    definition = vault_transaction_definition()
    arguments = {
        "operations": [
            {
                "op": "trash",
                "path": "note.md",
                "expectedHash": before_hash,
            }
        ]
    }
    return ToolCall(
        tool_call_id="call_trash",
        run_id="run_trash",
        workspace_id="ws_1",
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="idem_trash",
        deadline=None,
        lineage=AgentLineage.root("run_trash"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


def _fixture(tmp_path: Path) -> tuple[ClientBoundVaultTransaction, Client, ToolCall, Path]:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_text("before", encoding="utf-8")
    before_hash = content_hash(b"before")
    after_hash = content_hash(b"before+after")
    client = Client(before_hash=before_hash, after_hash=after_hash)
    artifacts = LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_1")
    transaction = VaultTransactionCoordinator(
        workspace_id="ws_1",
        vault_root=vault,
        artifacts=artifacts,
        artifact_budget=_budget(),
        clock=ManualClock(NOW),
    )
    return (
        ClientBoundVaultTransaction(
            workspace_id="ws_1",
            client=client,
            transaction=transaction,
            clock=ManualClock(NOW),
        ),
        client,
        _call(before_hash),
        target,
    )


@pytest.mark.asyncio
async def test_client_bound_transaction_uses_live_proof_but_only_worker_cas_mutates(tmp_path: Path) -> None:
    provider, client, call, target = _fixture(tmp_path)
    definition = vault_transaction_definition()
    token = ManualCancellationToken()

    evidence = await provider.prepare(definition, call, token)
    assert evidence.facts["executionAuthority"] == "worker-local-client-bound"
    assert evidence.facts["beforeHashes"] == {"note.md": content_hash(b"before")}
    assert evidence.facts["afterHashes"] == {"note.md": content_hash(b"before+after")}
    assert target.read_text(encoding="utf-8") == "before"

    await provider.revalidate(definition, call, evidence, token)
    result = await provider.execute(call, token)
    await provider.complete(definition, call, evidence, result)

    assert result.status is ToolResultStatus.SUCCEEDED
    assert target.read_text(encoding="utf-8") == "before+after"
    assert client.calls == 3
    assert client.commit_observations == 1


@pytest.mark.asyncio
async def test_client_bound_trash_proves_and_executes_both_physical_paths(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_text("before", encoding="utf-8")
    before_hash = content_hash(b"before")
    client = TrashClient(before_hash)
    call = _trash_call(before_hash)
    transaction = VaultTransactionCoordinator(
        workspace_id="ws_1",
        vault_root=vault,
        artifacts=LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_1"),
        artifact_budget=_budget(),
        clock=ManualClock(NOW),
    )
    provider = ClientBoundVaultTransaction(
        workspace_id="ws_1",
        client=client,
        transaction=transaction,
        clock=ManualClock(NOW),
    )
    definition = vault_transaction_definition()
    token = ManualCancellationToken()

    evidence = await provider.prepare(definition, call, token)
    assert client.trash_path is not None
    assert evidence.facts["beforeHashes"] == {
        client.trash_path: "absent",
        "note.md": before_hash,
    }
    assert evidence.facts["afterHashes"] == {
        client.trash_path: before_hash,
        "note.md": "absent",
    }

    await provider.revalidate(definition, call, evidence, token)
    result = await provider.execute(call, token)
    await provider.complete(definition, call, evidence, result)

    assert result.status is ToolResultStatus.SUCCEEDED
    assert not target.exists()
    assert (vault / client.trash_path).read_bytes() == b"before"
    assert client.calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(("open_editor", "unsaved_editor"), [(True, False), (True, True)])
async def test_open_or_unsaved_touched_editor_fails_closed_before_any_disk_plan(
    tmp_path: Path,
    open_editor: bool,
    unsaved_editor: bool,
) -> None:
    provider, client, call, target = _fixture(tmp_path)
    client.open_editor = open_editor
    client.unsaved_editor = unsaved_editor

    with pytest.raises(PreflightConflict, match="save and close") as captured:
        await provider.prepare(vault_transaction_definition(), call, ManualCancellationToken())

    assert captured.value.details["reason"] == "obsidian_editor_must_be_closed"
    assert target.read_text(encoding="utf-8") == "before"


@pytest.mark.asyncio
async def test_client_hash_plan_mismatch_discards_worker_plan_without_mutation(tmp_path: Path) -> None:
    provider, client, call, target = _fixture(tmp_path)
    client.after_hash = "sha256:" + "f" * 64

    with pytest.raises(PreflightConflict, match="differs from the Worker"):
        await provider.prepare(vault_transaction_definition(), call, ManualCancellationToken())

    result = await provider.execute(call, ManualCancellationToken())
    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None and result.error.code == "client_bound_plan_missing"
    assert target.read_text(encoding="utf-8") == "before"


@pytest.mark.asyncio
async def test_changed_live_proof_after_approval_is_conflict(tmp_path: Path) -> None:
    provider, client, call, target = _fixture(tmp_path)
    definition = vault_transaction_definition()
    evidence = await provider.prepare(definition, call, ManualCancellationToken())
    client.before_hash = "sha256:" + "d" * 64

    with pytest.raises(PreflightConflict, match="differs from the Worker"):
        await provider.revalidate(definition, call, evidence, ManualCancellationToken())

    await provider.complete(
        definition,
        call,
        evidence,
        ClientBoundVaultTransaction._result(call, ToolResultStatus.CONFLICTED, "changed", "changed"),
    )
    assert target.read_text(encoding="utf-8") == "before"


@pytest.mark.asyncio
async def test_disconnect_at_final_barrier_returns_failed_without_side_effect(tmp_path: Path) -> None:
    provider, client, call, target = _fixture(tmp_path)
    definition = vault_transaction_definition()
    evidence = await provider.prepare(definition, call, ManualCancellationToken())
    await provider.revalidate(definition, call, evidence, ManualCancellationToken())
    client.fail_at = 3

    result = await provider.execute(call, ManualCancellationToken())
    await provider.complete(definition, call, evidence, result)

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None and result.error.code == "client_live_state_unavailable"
    assert target.read_text(encoding="utf-8") == "before"
    assert client.active_leases == 0 and client.released_leases == 1


@pytest.mark.asyncio
async def test_disconnect_after_final_preview_rolls_back_before_commit_cleanup(tmp_path: Path) -> None:
    provider, client, call, target = _fixture(tmp_path)
    definition = vault_transaction_definition()
    token = ManualCancellationToken()
    evidence = await provider.prepare(definition, call, token)
    await provider.revalidate(definition, call, evidence, token)
    client.disconnect_on_commit_observation = True

    result = await provider.execute(call, token)
    await provider.complete(definition, call, evidence, result)

    assert result.status is ToolResultStatus.FAILED
    assert target.read_text(encoding="utf-8") == "before"
    assert client.calls == 3 and client.commit_observations == 1
    assert client.active_leases == 0 and client.released_leases == 1


@pytest.mark.asyncio
async def test_real_duplex_close_after_final_preview_rolls_back_tentative_cas(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_text("before", encoding="utf-8")
    before_hash = content_hash(b"before")
    after_hash = content_hash(b"before+after")
    call = _call(before_hash)
    cas_barrier_started = threading.Event()
    disconnected = threading.Event()

    def cas_barrier(stage: str, relative_path: str) -> None:
        if stage != "before_publish":
            return
        assert relative_path == "note.md"
        cas_barrier_started.set()
        assert disconnected.wait(timeout=5)

    transaction = VaultTransactionCoordinator(
        workspace_id="ws_1",
        vault_root=vault,
        artifacts=LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_1"),
        artifact_budget=_budget(),
        clock=ManualClock(NOW),
        cas_barrier=cas_barrier,
    )
    plugin_stream, worker_stream = _memory_pipe_pair()
    plugin_dispatcher = _DisconnectingPluginDispatcher(
        plugin_stream,
        before_hash,
        after_hash,
        cas_barrier_started,
        disconnected,
    )
    plugin_connection = DuplexJsonRpcConnection(
        plugin_stream,
        role=ConnectionRole.CLIENT,
        dispatcher=plugin_dispatcher,
        connection_id="pipe-client-commit-barrier",
    )
    worker_connection = DuplexJsonRpcConnection(
        worker_stream,
        role=ConnectionRole.SERVER,
        dispatcher=_WorkerPipeDispatcher(),
        connection_id="pipe-worker-commit-barrier",
    )
    await asyncio.gather(plugin_connection.start(), worker_connection.start())
    initialize = build_examples()["initialize.request.json"]["params"]
    assert isinstance(initialize, Mapping)
    await plugin_connection.request("initialize", initialize, timeout_seconds=2)
    provider = ClientBoundVaultTransaction(
        workspace_id="ws_1",
        client=NamedPipeClientToolPort(
            workspace_id="ws_1",
            channels=_FixedConnectionProvider(worker_connection),
            clock=ManualClock(NOW),
        ),
        transaction=transaction,
        clock=ManualClock(NOW),
    )
    definition = vault_transaction_definition()
    token = ManualCancellationToken()
    try:
        evidence = await provider.prepare(definition, call, token)
        await provider.revalidate(definition, call, evidence, token)
        result = await provider.execute(call, token)
        await provider.complete(definition, call, evidence, result)

        assert plugin_dispatcher.previews == 3
        assert cas_barrier_started.is_set() and disconnected.is_set()
        assert not plugin_dispatcher.commit_observe_started.is_set()
        assert result.status is ToolResultStatus.FAILED
        assert target.read_text(encoding="utf-8") == "before"
        assert all(effect.state.value == "rolled_back" for effect in result.side_effects)
        assert not list(vault.rglob(".offeragent-*"))
        replay = await provider.execute(call, ManualCancellationToken())
        assert replay.error is not None and replay.error.code == "client_bound_plan_missing"
        assert target.read_text(encoding="utf-8") == "before"
    finally:
        if plugin_dispatcher.disconnect_task is not None:
            await asyncio.gather(plugin_dispatcher.disconnect_task, return_exceptions=True)
        await asyncio.gather(plugin_connection.close(), worker_connection.close(), return_exceptions=True)


@pytest.mark.asyncio
async def test_editor_reopened_after_final_preview_forces_post_cas_rollback(tmp_path: Path) -> None:
    provider, client, call, target = _fixture(tmp_path)
    definition = vault_transaction_definition()
    token = ManualCancellationToken()
    evidence = await provider.prepare(definition, call, token)
    await provider.revalidate(definition, call, evidence, token)
    client.open_on_commit_observation = True

    result = await provider.execute(call, token)
    await provider.complete(definition, call, evidence, result)

    assert result.status is ToolResultStatus.CONFLICTED
    assert target.read_text(encoding="utf-8") == "before"
    assert result.error is not None and "opened or became unsaved" in result.error.message
    assert client.calls == 3 and client.commit_observations == 1


@pytest.mark.asyncio
async def test_post_commit_hash_drift_forces_rollback(tmp_path: Path) -> None:
    provider, client, call, target = _fixture(tmp_path)
    definition = vault_transaction_definition()
    token = ManualCancellationToken()
    evidence = await provider.prepare(definition, call, token)
    await provider.revalidate(definition, call, evidence, token)
    client.commit_observed_hash = "sha256:" + "e" * 64

    result = await provider.execute(call, token)
    await provider.complete(definition, call, evidence, result)

    assert result.status is ToolResultStatus.CONFLICTED
    assert target.read_text(encoding="utf-8") == "before"
    assert result.error is not None and "after-state" in result.error.message


@pytest.mark.asyncio
async def test_disconnect_wins_commit_lease_and_complete_discards_recovery_state(tmp_path: Path) -> None:
    provider, client, call, target = _fixture(tmp_path)
    definition = vault_transaction_definition()
    token = ManualCancellationToken()
    evidence = await provider.prepare(definition, call, token)
    await provider.revalidate(definition, call, evidence, token)
    client.connection_available = False

    result = await provider.execute(call, token)
    await provider.complete(definition, call, evidence, result)

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None and result.error.code == "client_live_state_unavailable"
    assert target.read_text(encoding="utf-8") == "before"
    assert client.active_leases == 0 and client.released_leases == 0

    # Recovery/retry cannot resurrect either the client proof or the Worker plan.
    replay = await provider.execute(call, ManualCancellationToken())
    assert replay.status is ToolResultStatus.FAILED
    assert replay.error is not None and replay.error.code == "client_bound_plan_missing"
    assert target.read_text(encoding="utf-8") == "before"
