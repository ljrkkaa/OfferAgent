from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from offeragent_harness.config import HarnessConfig
from offeragent_harness.foundation import canonical_json_sha256
from offeragent_harness.models import (
    ModelEvent,
    ModelEventKind,
    ModelFinishReason,
    ModelPurpose,
    ModelRequest,
    ModelUsage,
)
from offeragent_harness.ports import ApplicationCommandContext, CancellationToken
from offeragent_harness.ports.worker_runtime import WorkerBootstrap
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.capabilities import CapabilityName, CapabilitySet
from offeragent_harness.protocol.errors import ProtocolViolation
from offeragent_harness.protocol.framing import LengthPrefixedJsonRpcDecoder, encode_frame
from offeragent_harness.protocol.jsonrpc import JsonRpcErrorResponse, JsonRpcRequest, JsonRpcSuccessResponse
from offeragent_harness.protocol.messages import (
    EventsReplayResult,
    InitializeResult,
    RuntimeStatusResult,
    SessionCreateResult,
    TurnGetResult,
    TurnStartParams,
)
from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.runtime.loopback_gateway import LoopbackAsset
from offeragent_harness.runtime.named_pipe import (
    ConnectionRole,
    DiscoveryMaterialStore,
    DuplexJsonRpcConnection,
    authenticate_client_stream,
)
from offeragent_harness.runtime.production_worker_composition import (
    ProductionWorkerApplication,
    ProductionWorkerCompositionRoot,
    ProductionWorkerOverrides,
)
from offeragent_harness.runtime.windows_named_pipe import (
    DpapiCurrentUserProtector,
    connect_windows_named_pipe,
)
from offeragent_harness.runtime.worker_entrypoint import WorkerEntrypoint
from offeragent_harness.sessions import RunStatus
from offeragent_harness.testing import ManualCancellationToken
from offeragent_harness.workspace import identify_workspace_root
from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config
from offeragent_harness.workspace.runtime_identity import workspace_database_identity


def _ripgrep_executable() -> Path:
    executable = shutil.which("rg.exe")
    if executable is None:
        pytest.fail("ripgrep is required for the production Worker integration fixture")
    return Path(executable).resolve(strict=True)


pytestmark = pytest.mark.skipif(os.name != "nt", reason="production transport conformance requires Windows")


class _BlockingFakeModel:
    """Keep the root Run durably active until both real transports observe it."""

    def __init__(self) -> None:
        self.planning_entered = asyncio.Event()
        self.release_planning = asyncio.Event()
        self.requests: list[ModelRequest] = []

    async def stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        cancellation.checkpoint()
        yield ModelEvent(request.request_id, 1, ModelEventKind.STARTED)
        if request.purpose is ModelPurpose.PLANNING:
            self.planning_entered.set()
            await self.release_planning.wait()
            cancellation.checkpoint()
            yield ModelEvent(
                request.request_id,
                2,
                ModelEventKind.STRUCTURED_OUTPUT,
                data={"requiresWriteOutcome": False, "calls": [], "stopReason": "传输一致性验证完成"},
            )
        elif request.purpose is ModelPurpose.COMPOSING:
            yield ModelEvent(request.request_id, 2, ModelEventKind.TEXT_DELTA, text="同一 Worker 事件流正常。")
        else:
            raise AssertionError(f"unexpected model purpose: {request.purpose}")
        yield ModelEvent(request.request_id, 3, ModelEventKind.USAGE, usage=ModelUsage(5, 4, 0, 0))
        yield ModelEvent(
            request.request_id,
            4,
            ModelEventKind.COMPLETED,
            finish_reason=ModelFinishReason.STOP,
        )


class _NoReverseRequests:
    def require_ready(self) -> None:
        return None

    async def dispatch(
        self,
        method: str,
        params: Mapping[str, Any] | WireModel,
        cancellation: CancellationToken,
        *,
        context: ApplicationCommandContext | None = None,
    ) -> object:
        del params, cancellation, context
        raise AssertionError(f"unexpected reverse request: {method}")


def _development_web_assets() -> tuple[LoopbackAsset, ...]:
    web = Path(__file__).resolve().parents[3] / "web"
    assets: list[LoopbackAsset] = []
    for route, path, media_type in (
        ("/", web / "index.html", "text/html; charset=utf-8"),
        ("/assets/app.js", web / "assets" / "app.js", "text/javascript; charset=utf-8"),
        ("/assets/app.css", web / "assets" / "app.css", "text/css; charset=utf-8"),
    ):
        content = path.read_bytes()
        assets.append(LoopbackAsset(route, media_type, content, f"sha256:{hashlib.sha256(content).hexdigest()}"))
    return tuple(assets)


async def _http_post_json(
    *,
    port: int,
    path: str,
    value: Mapping[str, Any],
    origin: str,
    cookie: str | None = None,
    csrf_token: str | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Use a real TCP socket so the test cannot accidentally call the gateway directly."""

    body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    host = f"127.0.0.1:{port}"
    headers = {
        "Host": host,
        "Origin": origin,
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Connection": "close",
    }
    if cookie is not None:
        headers["Cookie"] = cookie
    if csrf_token is not None:
        headers["X-CSRF-Token"] = csrf_token
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        (
            f"POST {path} HTTP/1.1\r\n" + "".join(f"{name}: {item}\r\n" for name, item in headers.items()) + "\r\n"
        ).encode("iso-8859-1")
        + body
    )
    await writer.drain()
    try:
        status_line = (await reader.readline()).decode("ascii").rstrip("\r\n")
        protocol, raw_status, _reason = status_line.split(" ", maxsplit=2)
        assert protocol == "HTTP/1.1"
        response_headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line == b"\r\n":
                break
            name, separator, item = line.decode("iso-8859-1").rstrip("\r\n").partition(":")
            assert separator
            response_headers[name.casefold()] = item.strip()
        content_length = int(response_headers["content-length"])
        payload = await reader.readexactly(content_length)
        decoded = json.loads(payload.decode("utf-8"))
        assert isinstance(decoded, dict)
        return int(raw_status), response_headers, cast(dict[str, Any], decoded)
    finally:
        writer.close()
        await writer.wait_closed()


class _LoopbackClient:
    def __init__(self, application: ProductionWorkerApplication) -> None:
        self._application = application
        self._cookie: str | None = None
        self._csrf_token: str | None = None

    async def authenticate(self) -> dict[str, Any]:
        launch = self._application.gateway.issue_launch()
        token = launch.url.rsplit("#", maxsplit=1)[1]
        status, headers, payload = await _http_post_json(
            port=self._application.gateway.port,
            path="/auth/exchange",
            value={"token": token},
            origin=self._application.gateway.origin,
        )
        assert status == 200
        cookie = headers["set-cookie"].split(";", maxsplit=1)[0]
        result = cast(dict[str, Any], payload["result"] if "result" in payload else payload)
        self._cookie = cookie
        self._csrf_token = cast(str, result["csrfToken"])
        return result

    async def command(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        status, payload = await self.command_response(method, params)
        assert status == 200, payload
        return cast(dict[str, Any], payload["result"])

    async def command_response(self, method: str, params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        assert self._cookie is not None and self._csrf_token is not None
        status, _headers, payload = await _http_post_json(
            port=self._application.gateway.port,
            path="/api/command",
            value={"method": method, "params": dict(params)},
            origin=self._application.gateway.origin,
            cookie=self._cookie,
            csrf_token=self._csrf_token,
        )
        return status, payload


async def _connect_pipe(
    application: ProductionWorkerApplication,
    workspace_id: str,
) -> tuple[DuplexJsonRpcConnection, InitializeResult]:
    material = DiscoveryMaterialStore(
        application.state_directory / "transport",
        protector=DpapiCurrentUserProtector(),
    ).load(now=datetime.now(timezone.utc))
    stream = await connect_windows_named_pipe(material.pipe_name)
    await authenticate_client_stream(stream, material, now=lambda: datetime.now(timezone.utc))
    connection = DuplexJsonRpcConnection(
        stream,
        role=ConnectionRole.CLIENT,
        dispatcher=_NoReverseRequests(),
        connection_id="pipe-client-transport-conformance",
    )
    await connection.start()
    initialized = await connection.request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "clientVersion": "2.0.0",
            "workspaceId": workspace_id,
            "capabilities": CapabilitySet.from_enabled(set(CapabilityName)).to_wire(),
            "requiredCapabilities": ["eventReplay", "multiSession", "loopbackWeb"],
            "schemaHash": schema_hash(),
        },
    )
    assert isinstance(initialized, InitializeResult)
    await connection.wait_ready()
    return connection, initialized


async def _raw_pipe_invalid_params(
    application: ProductionWorkerApplication,
    workspace_id: str,
) -> dict[str, Any]:
    """Exercise the Worker-side validator without the typed client preflight.

    The public ``DuplexJsonRpcConnection.request`` intentionally validates
    command parameters before writing them.  A second authenticated stream is
    therefore required to prove that malformed parameters receive the same
    ErrorEnvelope from the real Pipe server and the Loopback adapter.
    """

    material = DiscoveryMaterialStore(
        application.state_directory / "transport",
        protector=DpapiCurrentUserProtector(),
    ).load(now=datetime.now(timezone.utc))
    stream = await connect_windows_named_pipe(material.pipe_name)
    await authenticate_client_stream(stream, material, now=lambda: datetime.now(timezone.utc))
    decoder = LengthPrefixedJsonRpcDecoder()

    async def round_trip(request: JsonRpcRequest) -> JsonRpcSuccessResponse | JsonRpcErrorResponse:
        await stream.write(encode_frame(request))
        while True:
            chunk = await asyncio.wait_for(stream.read(64 * 1024), timeout=10)
            assert chunk
            for message in decoder.feed(chunk):
                if isinstance(message, (JsonRpcSuccessResponse, JsonRpcErrorResponse)) and message.id == request.id:
                    return message

    try:
        initialized = await round_trip(
            JsonRpcRequest(
                jsonrpc="2.0",
                id="raw_initialize",
                method="initialize",
                params={
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientVersion": "2.0.0",
                    "workspaceId": workspace_id,
                    "capabilities": CapabilitySet.from_enabled(set(CapabilityName)).to_wire(),
                    "requiredCapabilities": ["eventReplay", "multiSession", "loopbackWeb"],
                    "schemaHash": schema_hash(),
                },
            )
        )
        assert isinstance(initialized, JsonRpcSuccessResponse)
        rejected = await round_trip(JsonRpcRequest(jsonrpc="2.0", id="raw_invalid", method="session/get", params={}))
        assert isinstance(rejected, JsonRpcErrorResponse)
        return rejected.error.data.to_wire()
    finally:
        stream.cancel_pending_io()
        await stream.close()


@pytest.fixture
async def native_production_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[ProductionWorkerApplication, _BlockingFakeModel, str]]:
    monkeypatch.setattr(
        "offeragent_harness.runtime.loopback_gateway.load_packaged_web_assets",
        _development_web_assets,
    )
    vault = tmp_path / "临时 Vault"
    vault.mkdir()
    portable = ensure_portable_workspace_config(vault)
    state = tmp_path / "runtime-state"
    workspace_instance_id = "wsi_2f98c5dd-cc41-49cc-82db-1084571f40ae"
    root_identity = identify_workspace_root(vault).identity_hash
    database_identity = workspace_database_identity(workspace_instance_id)
    fake_model = _BlockingFakeModel()
    root = ProductionWorkerCompositionRoot(
        canonical_root_identity=root_identity,
        database_identity=database_identity,
        runtime_version="1.2.3",
        build_commit="abcdef0",
        overrides=ProductionWorkerOverrides(
            model_gateway_factory=lambda _settings: fake_model,
            start_native_transports=True,
            runtime_config=HarnessConfig.model_validate({"ui": {"loopback_web_enabled": True}}),
            ripgrep_path=_ripgrep_executable(),
        ),
    )
    entrypoint = WorkerEntrypoint(root)
    application = await entrypoint.start(WorkerBootstrap(workspace_instance_id, vault, state))
    try:
        assert isinstance(application, ProductionWorkerApplication)
        yield application, fake_model, portable.portable_workspace_id
    finally:
        fake_model.release_planning.set()
        await entrypoint.shutdown()


@pytest.mark.asyncio
async def test_real_pipe_and_loopback_share_identity_session_active_run_and_replay(
    native_production_application: tuple[ProductionWorkerApplication, _BlockingFakeModel, str],
) -> None:
    application, fake_model, workspace_id = native_production_application
    pipe, pipe_initialize = await _connect_pipe(application, workspace_id)
    loopback = _LoopbackClient(application)
    try:
        auth = await loopback.authenticate()
        web_initialize = await loopback.command(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientVersion": "2.0.0",
                "workspaceId": workspace_id,
                "capabilities": CapabilitySet.from_enabled(set(CapabilityName)).to_wire(),
                "requiredCapabilities": ["eventReplay", "multiSession", "loopbackWeb"],
                "schemaHash": schema_hash(),
            },
        )
        pipe_status = await pipe.request("runtime/status", {})
        web_status = await loopback.command("runtime/status", {})
        direct_status = await application.dispatcher.dispatch(
            "runtime/status",
            {},
            ManualCancellationToken(),
            context=ApplicationCommandContext(
                transport="windows-named-pipe",
                client_id="direct-service-conformance",
                peer="same-worker",
            ),
        )
        assert isinstance(pipe_status, RuntimeStatusResult)

        assert auth["workerPid"] == pipe_initialize.worker_pid == web_initialize["workerPid"] == os.getpid()
        assert pipe_initialize.worker_pid == application.worker_pid == application.loopback_worker_pid
        assert pipe_initialize.workspace_instance_id == web_initialize["workspaceInstanceId"]
        assert pipe_initialize.workspace_instance_id == application.workspace_instance_id
        assert pipe_initialize.schema_hash == web_initialize["schemaHash"] == schema_hash()
        assert pipe_initialize.transport.value == "windows-named-pipe"
        assert web_initialize["transport"] == "loopback-http"
        assert pipe_status.worker_pid == web_status["workerPid"] == application.worker_pid
        assert pipe_status.workspace_instance_id == web_status["workspaceInstanceId"]
        assert pipe_status.database_identity == web_status["databaseIdentity"] == application.database_identity
        assert pipe_status.schema_hash == web_status["schemaHash"] == schema_hash()
        assert pipe_status.to_wire() == web_status == direct_status
        assert application.database_path.is_file()
        assert application.harness is application.harness_application.require_ready()

        pipe_invalid = await _raw_pipe_invalid_params(application, workspace_id)
        web_invalid_status, web_invalid = await loopback.command_response("session/get", {})
        with pytest.raises(ProtocolViolation) as direct_invalid:
            await application.dispatcher.dispatch("session/get", {}, ManualCancellationToken())
        assert web_invalid_status == 400
        assert pipe_invalid == web_invalid["error"] == direct_invalid.value.error.to_wire()

        created = await pipe.request(
            "session/create",
            {"title": "真实双传输一致性", "clientRequestId": "req_transport_session"},
        )
        assert isinstance(created, SessionCreateResult)
        session_id = created.session.session_id
        observed_session = await loopback.command(
            "session/get",
            {"sessionId": session_id, "includeTurns": True},
        )
        direct_session = await application.dispatcher.dispatch(
            "session/get",
            {"sessionId": session_id, "includeTurns": True},
            ManualCancellationToken(),
        )
        assert observed_session["session"]["summary"]["sessionId"] == session_id
        assert observed_session["session"]["summary"]["workspaceId"] == workspace_id
        assert observed_session == direct_session

        started = await loopback.command(
            "turn/start",
            {
                "sessionId": session_id,
                "turnId": "turn_transport_identity",
                "idempotencyKey": "transport-identity-turn",
                "writeIntent": {"kind": "none"},
                "input": [{"type": "text", "text": "验证两种真实传输共用活动 Run"}],
                "runConfig": {"provider": "codex", "model": "fake", "permissionMode": "normal"},
            },
        )
        run_id = cast(str, started["runId"])
        await asyncio.wait_for(fake_model.planning_entered.wait(), timeout=10)

        active_status = await pipe.request("runtime/status", {})
        web_active_status = await loopback.command("runtime/status", {})
        active_turn = await pipe.request(
            "turn/get",
            {"sessionId": session_id, "turnId": "turn_transport_identity"},
        )
        web_active_turn = await loopback.command(
            "turn/get",
            {"sessionId": session_id, "turnId": "turn_transport_identity"},
        )
        direct_active_status = await application.dispatcher.dispatch(
            "runtime/status",
            {},
            ManualCancellationToken(),
        )
        direct_active_turn = await application.dispatcher.dispatch(
            "turn/get",
            {"sessionId": session_id, "turnId": "turn_transport_identity"},
            ManualCancellationToken(),
        )
        assert isinstance(active_status, RuntimeStatusResult)
        assert isinstance(active_turn, TurnGetResult)
        assert active_status.active_run_ids == web_active_status["activeRunIds"] == [run_id]
        assert active_status.to_wire() == web_active_status == direct_active_status
        assert active_turn.to_wire() == web_active_turn == direct_active_turn
        assert active_turn.turn.selected_run_id == run_id
        assert [item.run_id for item in active_turn.turn.runs] == [run_id]
        assert active_turn.turn.runs[0].status.value not in {"completed", "cancelled", "failed", "interrupted"}

        fake_model.release_planning.set()
        live_types: list[str] = []
        for _ in range(100):
            event = await pipe.next_event(timeout_seconds=10)
            if event.run_id != run_id:
                continue
            live_types.append(event.type.value)
            if event.type.value == "turn.completed":
                break
        assert live_types[-1] == "turn.completed"

        pipe_replay = await pipe.request(
            "events/replay",
            {"runId": run_id, "afterSequence": 0, "limit": 1000},
        )
        web_replay = await loopback.command(
            "events/replay",
            {"runId": run_id, "afterSequence": 0, "limit": 1000},
        )
        direct_replay = await application.dispatcher.dispatch(
            "events/replay",
            {"runId": run_id, "afterSequence": 0, "limit": 1000},
            ManualCancellationToken(),
        )
        assert isinstance(pipe_replay, EventsReplayResult)
        pipe_events = [item.to_wire() for item in pipe_replay.events]
        assert pipe_events == web_replay["events"]
        assert pipe_replay.to_wire() == web_replay == direct_replay
        assert pipe_replay.last_sequence == web_replay["lastSequence"]
        assert pipe_replay.has_more is False is web_replay["hasMore"]
        assert pipe_events[-1]["type"] == "turn.completed"
        assert [item["sequence"] for item in pipe_events] == list(range(1, len(pipe_events) + 1))
        assert {item["runId"] for item in pipe_events} == {run_id}

        midpoint = cast(int, pipe_events[len(pipe_events) // 2]["sequence"])
        web_suffix = await loopback.command(
            "events/replay",
            {"runId": run_id, "afterSequence": midpoint, "limit": 1000},
        )
        assert web_suffix["events"] == [item for item in pipe_events if item["sequence"] > midpoint]

        completed = await pipe.request(
            "turn/get",
            {"sessionId": session_id, "turnId": "turn_transport_identity"},
        )
        web_completed = await loopback.command(
            "turn/get",
            {"sessionId": session_id, "turnId": "turn_transport_identity"},
        )
        direct_completed = await application.dispatcher.dispatch(
            "turn/get",
            {"sessionId": session_id, "turnId": "turn_transport_identity"},
            ManualCancellationToken(),
        )
        assert isinstance(completed, TurnGetResult)
        assert completed.to_wire() == web_completed == direct_completed
        assert completed.turn.runs[0].status.value == RunStatus.COMPLETED.value
        terminal_status = await pipe.request("runtime/status", {})
        web_terminal_status = await loopback.command("runtime/status", {})
        assert isinstance(terminal_status, RuntimeStatusResult)
        assert terminal_status.active_run_ids == web_terminal_status["activeRunIds"] == []

        intent_binding = {
            "kind": "vault_write_required",
            "targetPaths": ["notes/required.md"],
        }
        guarded_params = {
            "sessionId": session_id,
            "turnId": "turn_transport_write_required",
            "idempotencyKey": "transport-write-required",
            "writeIntent": {
                **intent_binding,
                "intentHash": canonical_json_sha256(intent_binding),
            },
            "input": [{"type": "text", "text": "必须写入目标文件, 但恶意 Planner 声称无需写入"}],
            "runConfig": {"provider": "codex", "model": "fake", "permissionMode": "normal"},
        }
        guarded = await loopback.command(
            "turn/start",
            guarded_params,
        )
        guarded_run_id = cast(str, guarded["runId"])
        guarded_status: str | None = None
        for _ in range(500):
            guarded_turn = await loopback.command(
                "turn/get",
                {"sessionId": session_id, "turnId": "turn_transport_write_required"},
            )
            guarded_status = cast(str, guarded_turn["turn"]["runs"][0]["status"])
            if guarded_status in {"completed", "cancelled", "failed", "interrupted"}:
                break
            await asyncio.sleep(0.01)
        assert guarded_status == RunStatus.FAILED.value
        guarded_state = await application.harness.get_run_state(guarded_run_id)
        assert guarded_state.write_obligation.intent is not None
        assert guarded_state.write_obligation.intent.request_hash == canonical_json_sha256(
            TurnStartParams.model_validate_json(json.dumps(guarded_params)).to_wire()
        )
        assert guarded_state.write_obligation.intent.intent_hash == canonical_json_sha256(intent_binding)
        assert guarded_state.write_obligation.intent.target_paths == ("notes/required.md",)
        guarded_replay = await loopback.command(
            "events/replay",
            {"runId": guarded_run_id, "afterSequence": 0, "limit": 1000},
        )
        guarded_types = [item["type"] for item in guarded_replay["events"]]
        assert "turn.completed" not in guarded_types
        assert guarded_types[-1] == "turn.failed"
        assert {request.purpose for request in fake_model.requests} == {
            ModelPurpose.PLANNING,
            ModelPurpose.COMPOSING,
        }
    finally:
        fake_model.release_planning.set()
        await pipe.close()
