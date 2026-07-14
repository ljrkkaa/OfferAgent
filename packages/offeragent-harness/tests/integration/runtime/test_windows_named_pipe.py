from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from offeragent_harness.ports import CancellationToken
from offeragent_harness.protocol.messages import ClientToolInvokeResult, InitializeResult, RuntimePingResult
from offeragent_harness.protocol.schemas import build_examples
from offeragent_harness.runtime.named_pipe import (
    ConnectionRole,
    DiscoveryMaterial,
    DiscoveryMaterialStore,
    DuplexJsonRpcConnection,
    HandshakeRejected,
    HandshakeReplayGuard,
    authenticate_client_stream,
    authenticate_server_stream,
)
from offeragent_harness.runtime.windows_named_pipe import (
    PIPE_REJECT_REMOTE_CLIENTS,
    DpapiCurrentUserProtector,
    Win32NamedPipeListener,
    Win32NamedPipeStream,
    connect_windows_named_pipe,
    current_session_id,
    current_user_sid,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Win32 Named Pipe E2E requires Windows")

_PIPE_CHILD_SCRIPT = r"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from offeragent_harness.protocol.schemas import build_examples
from offeragent_harness.runtime.named_pipe import (
    ConnectionRole,
    DiscoveryMaterialStore,
    DuplexJsonRpcConnection,
    authenticate_client_stream,
)
from offeragent_harness.runtime.windows_named_pipe import DpapiCurrentUserProtector, connect_windows_named_pipe


class ClientDispatcher:
    def require_ready(self):
        return None

    async def dispatch(self, method, params, cancellation, *, context=None):
        del context
        if method == "client/tool/invoke":
            return build_examples()["client-tool-invoke.response.json"]["result"]
        raise AssertionError("unexpected reverse method")


async def main():
    runtime_directory = Path(sys.argv[1])
    store = DiscoveryMaterialStore(runtime_directory, protector=DpapiCurrentUserProtector())
    material = store.load(now=datetime.now(timezone.utc))
    stream = await connect_windows_named_pipe(material.pipe_name)
    await authenticate_client_stream(stream, material, now=lambda: datetime.now(timezone.utc))
    connection = DuplexJsonRpcConnection(stream, role=ConnectionRole.CLIENT, dispatcher=ClientDispatcher())
    await connection.start()
    examples = build_examples()
    initialized = await connection.request("initialize", examples["initialize.request.json"]["params"])
    if initialized.worker_pid <= 0:
        raise AssertionError("invalid worker PID")
    pong = await connection.request("runtime/ping", {"nonce": "req_child_process"})
    if pong.nonce != "req_child_process":
        raise AssertionError("ping mismatch")
    await connection.close()
    print(f"child-ok:{os.getpid()}")


asyncio.run(main())
"""


class RuntimeDispatcher:
    def __init__(self) -> None:
        examples = build_examples()
        self.initialize_result = cast(dict[str, Any], examples["initialize.response.json"]["result"])
        self.client_tool_result = cast(dict[str, Any], examples["client-tool-invoke.response.json"]["result"])
        self.calls: list[str] = []

    def require_ready(self) -> None:
        return None

    async def dispatch(
        self,
        method: str,
        params: Mapping[str, Any],
        cancellation: CancellationToken,
        *,
        context: object | None = None,
    ) -> object:
        del context
        cancellation.checkpoint()
        self.calls.append(method)
        if method == "initialize":
            return self.initialize_result
        if method == "runtime/ping":
            return {
                "nonce": params["nonce"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "workerPid": os.getpid(),
            }
        if method == "client/tool/invoke":
            return self.client_tool_result
        raise AssertionError(f"unexpected method: {method}")


class FixedNonce:
    def __init__(self, value: int) -> None:
        self.value = value.to_bytes(32, "big")

    def __call__(self, size: int) -> bytes:
        assert size == 32
        return self.value


def issue_material(runtime_directory: Path) -> tuple[DiscoveryMaterialStore, DiscoveryMaterial]:
    store = DiscoveryMaterialStore(runtime_directory, protector=DpapiCurrentUserProtector())
    return store, store.issue(now=datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_real_pipe_dpapi_dacl_flags_identity_handshake_and_full_duplex(tmp_path: Path) -> None:
    runtime_directory = tmp_path / "LocalAppData" / "OfferAgent" / "workspaces" / "wsi_test"
    store, raw_material = issue_material(runtime_directory)
    material = store.load(now=datetime.now(timezone.utc))
    assert material == raw_material
    protected = store.path.read_bytes()
    assert material.pipe_name.encode() not in protected
    assert material.bootstrap_nonce not in protected
    assert store.path.parent == runtime_directory

    listener = Win32NamedPipeListener(material.pipe_name)
    accept = asyncio.create_task(listener.accept())
    client_stream = await connect_windows_named_pipe(material.pipe_name)
    server_stream = await accept
    snapshot = server_stream.security_snapshot()
    assert snapshot.allowed_sids == (current_user_sid(),)
    assert ";;;SY)" not in snapshot.dacl_sddl
    assert snapshot.dacl_sddl.count("(") == 1
    assert snapshot.creation_pipe_mode & PIPE_REJECT_REMOTE_CLIENTS
    assert snapshot.queried_pipe_flags & 0x00000004 == 0  # PIPE_TYPE_BYTE, not message mode
    assert snapshot.handle_inheritable is False
    assert client_stream.security_snapshot().handle_inheritable is False
    assert snapshot.peer.process_id == os.getpid()
    assert snapshot.peer.session_id == current_session_id()
    assert snapshot.peer.user_sid == current_user_sid()

    fixed_now = material.issued_at + timedelta(seconds=1)

    def now() -> datetime:
        return fixed_now

    await asyncio.gather(
        authenticate_server_stream(
            server_stream,
            material,
            now=now,
            replay_guard=HandshakeReplayGuard(),
        ),
        authenticate_client_stream(client_stream, material, now=now),
    )

    server_dispatcher = RuntimeDispatcher()
    client_dispatcher = RuntimeDispatcher()
    server = DuplexJsonRpcConnection(
        server_stream,
        role=ConnectionRole.SERVER,
        dispatcher=server_dispatcher,
    )
    client = DuplexJsonRpcConnection(
        client_stream,
        role=ConnectionRole.CLIENT,
        dispatcher=client_dispatcher,
    )
    await asyncio.gather(server.start(), client.start())
    examples = build_examples()
    try:
        initialized = await client.request(
            "initialize",
            cast(dict[str, Any], examples["initialize.request.json"]["params"]),
        )
        assert isinstance(initialized, InitializeResult)
        pong, reverse = await asyncio.gather(
            client.request("runtime/ping", {"nonce": "req_real_pipe"}),
            server.request(
                "client/tool/invoke",
                cast(dict[str, Any], examples["client-tool-invoke.request.json"]["params"]),
            ),
        )
        assert isinstance(pong, RuntimePingResult)
        assert pong.nonce == "req_real_pipe"
        assert isinstance(reverse, ClientToolInvokeResult)
        assert reverse.status.value == "succeeded"
        assert server_dispatcher.calls == ["initialize", "runtime/ping"]
        assert client_dispatcher.calls == ["client/tool/invoke"]
    finally:
        await asyncio.gather(client.close(), server.close())
        await listener.close()


@pytest.mark.asyncio
async def test_real_pipe_cancel_io_ex_settles_read_thread_and_stream_remains_usable(tmp_path: Path) -> None:
    _, material = issue_material(tmp_path / "runtime")
    listener = Win32NamedPipeListener(material.pipe_name)
    accept = asyncio.create_task(listener.accept())
    client = await connect_windows_named_pipe(material.pipe_name)
    server = await accept
    pending_read = asyncio.create_task(server.read(16))
    await asyncio.sleep(0.02)
    pending_read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_read

    await client.write(b"still-usable")
    assert await server.read(16) == b"still-usable"
    await asyncio.gather(client.close(), server.close())
    await listener.close()


@pytest.mark.asyncio
async def test_cancelled_connect_closes_any_handle_that_completes_late(tmp_path: Path) -> None:
    _, material = issue_material(tmp_path / "runtime")
    connect = asyncio.create_task(connect_windows_named_pipe(material.pipe_name, timeout_seconds=2))
    await asyncio.sleep(0.05)
    connect.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connect

    listener = Win32NamedPipeListener(material.pipe_name)
    accepted = await asyncio.wait_for(listener.accept(), timeout=2)
    assert await asyncio.wait_for(accepted.read(1), timeout=2) == b""
    await accepted.close()
    await listener.close()


@pytest.mark.asyncio
async def test_real_pipe_rejects_replayed_challenge_response(tmp_path: Path) -> None:
    _, material = issue_material(tmp_path / "runtime")
    listener = Win32NamedPipeListener(material.pipe_name)
    guard = HandshakeReplayGuard()

    async def connect_pair() -> tuple[Win32NamedPipeStream, Win32NamedPipeStream]:
        accepted = asyncio.create_task(listener.accept())
        client = await connect_windows_named_pipe(material.pipe_name)
        return client, await accepted

    fixed_now = material.issued_at + timedelta(seconds=1)

    def now() -> datetime:
        return fixed_now

    first_client, first_server = await connect_pair()
    await asyncio.gather(
        authenticate_server_stream(
            first_server,
            material,
            now=now,
            replay_guard=guard,
            nonce_source=FixedNonce(7),
        ),
        authenticate_client_stream(
            first_client,
            material,
            now=now,
            nonce_source=FixedNonce(9),
        ),
    )
    await asyncio.gather(first_client.close(), first_server.close())

    replay_client, replay_server = await connect_pair()
    replay = await asyncio.gather(
        authenticate_server_stream(
            replay_server,
            material,
            now=now,
            replay_guard=guard,
            nonce_source=FixedNonce(7),
        ),
        authenticate_client_stream(
            replay_client,
            material,
            now=now,
            nonce_source=FixedNonce(9),
        ),
        return_exceptions=True,
    )
    assert any(isinstance(result, HandshakeRejected) for result in replay)
    await asyncio.gather(replay_client.close(), replay_server.close())
    await listener.close()


@pytest.mark.asyncio
async def test_real_two_process_pipe_uses_discovery_path_only_and_validates_child_identity(tmp_path: Path) -> None:
    runtime_directory = tmp_path / "runtime"
    _, material = issue_material(runtime_directory)
    listener = Win32NamedPipeListener(material.pipe_name)
    accept = asyncio.create_task(listener.accept())
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _PIPE_CHILD_SCRIPT,
        str(runtime_directory),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    server_stream = await asyncio.wait_for(accept, timeout=10)
    assert server_stream.peer.user_sid == current_user_sid()
    assert server_stream.peer.session_id == current_session_id()

    await authenticate_server_stream(
        server_stream,
        material,
        now=lambda: datetime.now(timezone.utc),
        replay_guard=HandshakeReplayGuard(),
    )
    dispatcher = RuntimeDispatcher()
    connection = DuplexJsonRpcConnection(
        server_stream,
        role=ConnectionRole.SERVER,
        dispatcher=dispatcher,
    )
    await connection.start()
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    assert process.returncode == 0, f"stdout={stdout.decode()!r}, stderr={stderr.decode()!r}"
    child_output = stdout.decode().strip()
    assert child_output.startswith("child-ok:")
    assert server_stream.peer.process_id == int(child_output.partition(":")[2])
    assert dispatcher.calls == ["initialize", "runtime/ping"]
    await asyncio.wait_for(connection.wait_closed(), timeout=2)
    await listener.close()
