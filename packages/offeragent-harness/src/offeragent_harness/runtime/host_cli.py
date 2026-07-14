"""Signed Host CLI and current-user singleton control forwarding.

The Vault root is accepted only from inherited fd 4.  Canonical discovery is
returned only on inherited fd 3, which is closed immediately after the single
response.  Explicit current-user shutdown uses a separate fixed fd 3 result
contract.  No Vault path is put in argv, stdout, environment, or log text.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import struct
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from offeragent_harness.workspace.identity import WorkspaceRegistry
from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config
from offeragent_harness.workspace.runtime_identity import workspace_database_identity

from .host_supervisor import HostSupervisor, SupervisedWorkspaceIdentity, WorkerAttachment
from .named_pipe import (
    DiscoveryMaterial,
    DiscoveryMaterialStore,
    HandshakeReplayGuard,
    MaterialProtector,
    PipeByteStream,
    authenticate_client_stream,
    authenticate_server_stream,
    parse_discovery_material,
    serialize_discovery_material,
)
from .process_lock import ProcessAlreadyRunning
from .windows_named_pipe import Win32NamedPipeListener, connect_windows_named_pipe

_MAXIMUM_VAULT_ROOT_BYTES = 32 * 1024
_MAXIMUM_CONTROL_BYTES = 64 * 1024
_MAXIMUM_DISCOVERY_BYTES = 64 * 1024
_MAXIMUM_STOP_RESULT_BYTES = 4 * 1024
_CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_SELF_TEST_NONCE = re.compile(r"^[0-9a-f]{16,64}$")


class HostCliError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class HostAttachEngine(Protocol):
    async def start(self) -> None: ...

    async def attach(self, vault_root: Path, *, client_id: str) -> DiscoveryMaterial: ...

    async def shutdown(self) -> None: ...


class HostControlTransport(Protocol):
    async def start(self) -> None: ...

    async def serve_forever(self) -> None: ...

    async def forward(self, vault_root: Path, *, client_id: str) -> bytes: ...

    async def stop_all(self) -> bytes: ...

    async def close(self) -> None: ...


class WorkspaceDiscoveryProvider(Protocol):
    def load(self, workspace_instance_id: str, *, now: datetime) -> DiscoveryMaterial: ...


class HostControlConnector(Protocol):
    def __call__(self, pipe_name: str, *, timeout_seconds: float) -> Awaitable[PipeByteStream]: ...


class HostStoppedProbe(Protocol):
    def __call__(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class HostAttachOutcome:
    discovery: bytes
    owns_host: bool


@dataclass(frozen=True, slots=True)
class HostStopOutcome:
    status: str

    def __post_init__(self) -> None:
        if self.status not in {"stopped", "already_stopped"}:
            raise ValueError("Host stop status is invalid")


class SupervisedHostAttachEngine:
    """Bridge canonical Vault registration to the one HostSupervisor registry."""

    def __init__(
        self,
        *,
        supervisor: HostSupervisor,
        registry: WorkspaceRegistry,
        discovery: WorkspaceDiscoveryProvider,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._registry = registry
        self._discovery = discovery
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._attachments: dict[tuple[str, str], WorkerAttachment] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self._supervisor.start()

    async def attach(self, vault_root: Path, *, client_id: str) -> DiscoveryMaterial:
        if _CLIENT_ID.fullmatch(client_id) is None:
            raise HostCliError("client_id_invalid", "Host attach client identity is invalid")
        portable = ensure_portable_workspace_config(vault_root)
        record = self._registry.register(vault_root, portable_workspace_id=portable.portable_workspace_id)
        identity = SupervisedWorkspaceIdentity(
            workspace_instance_id=record.workspace_instance_id,
            canonical_root_identity=record.root_identity.identity_hash,
            database_identity=workspace_database_identity(record.workspace_instance_id),
        )
        key = (record.workspace_instance_id, client_id)
        async with self._lock:
            if key not in self._attachments:
                self._attachments[key] = await self._supervisor.attach(identity, client_id=client_id)
        return self._discovery.load(record.workspace_instance_id, now=self._now())

    async def shutdown(self) -> None:
        async with self._lock:
            attachments = tuple(self._attachments.values())
            self._attachments.clear()
        await asyncio.gather(*(attachment.detach() for attachment in attachments), return_exceptions=True)
        await self._supervisor.shutdown()
        if self._supervisor.shutdown_errors:
            raise HostCliError("host_shutdown_incomplete", "Host process-tree shutdown was incomplete")


class DpapiWorkspaceDiscoveryProvider:
    """Load the discovery material issued by the ready Worker transport."""

    def __init__(self, workspaces_root: Path, *, protector: MaterialProtector) -> None:
        self._workspaces_root = workspaces_root.resolve(strict=False)
        self._protector = protector

    def load(self, workspace_instance_id: str, *, now: datetime) -> DiscoveryMaterial:
        if re.fullmatch(r"wsi_[0-9a-f-]{36}", workspace_instance_id) is None:
            raise HostCliError("workspace_identity_invalid", "workspace identity is invalid")
        return DiscoveryMaterialStore(
            self._workspaces_root / workspace_instance_id / "transport",
            protector=self._protector,
        ).load(now=now)


class HostControlBroker:
    """Authenticated current-SID attach and explicit-stop control broker."""

    def __init__(
        self,
        *,
        engine: HostAttachEngine,
        material_store: DiscoveryMaterialStore,
        now: Callable[[], datetime] | None = None,
        listener_factory: Callable[[str], Win32NamedPipeListener] = Win32NamedPipeListener,
        connector: HostControlConnector = connect_windows_named_pipe,
    ) -> None:
        self._engine = engine
        self._store = material_store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._listener_factory = listener_factory
        self._connector = connector
        self._material: DiscoveryMaterial | None = None
        self._listener: Win32NamedPipeListener | None = None
        self._replay = HandshakeReplayGuard()
        self._tasks: set[asyncio.Task[None]] = set()
        self._state = asyncio.Condition()
        self._inflight_attaches = 0
        self._stopping = False
        self._stop_task: asyncio.Task[None] | None = None
        self._exit_after_receipt = asyncio.Event()
        self._closed = False

    async def start(self) -> None:
        if self._listener is not None:
            return
        material = self._store.issue(now=self._now(), lifetime=timedelta(days=30))
        self._material = material
        self._listener = self._listener_factory(material.pipe_name)

    async def serve_forever(self) -> None:
        listener = self._listener
        if listener is None:
            raise HostCliError("control_not_started", "Host control broker is not started")
        try:
            while not self._closed:
                accept = asyncio.create_task(listener.accept())
                exit_requested = asyncio.create_task(self._exit_after_receipt.wait())
                try:
                    done, _ = await asyncio.wait(
                        {accept, exit_requested},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if exit_requested in done:
                        if not accept.done():
                            accept.cancel()
                        await asyncio.gather(accept, return_exceptions=True)
                        break
                    stream = await accept
                finally:
                    if not exit_requested.done():
                        exit_requested.cancel()
                    await asyncio.gather(exit_requested, return_exceptions=True)
                task = asyncio.create_task(self._serve_one(stream), name="offeragent-host-control-client")
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except asyncio.CancelledError:
            raise
        finally:
            await self.close()

    async def forward(self, vault_root: Path, *, client_id: str) -> bytes:
        deadline = asyncio.get_running_loop().time() + 10.0
        last_error: BaseException | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                material = self._store.load(now=self._now())
                stream = await self._connector(material.pipe_name, timeout_seconds=1.0)
                try:
                    await authenticate_client_stream(stream, material, now=self._now)
                    request = _canonical_control_request(vault_root, client_id)
                    await _write_packet(stream, request)
                    response = await _read_packet(stream, maximum=_MAXIMUM_DISCOVERY_BYTES)
                    _validate_discovery_payload(response)
                    return response
                finally:
                    await stream.close()
            except (OSError, TimeoutError, RuntimeError) as error:
                last_error = error
                await asyncio.sleep(0.05)
        raise HostCliError(
            "host_forward_unavailable", "current-user Host control channel is unavailable"
        ) from last_error

    async def stop_all(self) -> bytes:
        """Request one receipt-confirmed shutdown from the current Host owner."""

        deadline = asyncio.get_running_loop().time() + 60.0
        last_error: BaseException | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                material = self._store.load(now=self._now())
                stream = await self._connector(material.pipe_name, timeout_seconds=1.0)
                try:
                    await authenticate_client_stream(stream, material, now=self._now)
                    await _write_packet(stream, _canonical_stop_request())
                    remaining = max(0.001, deadline - asyncio.get_running_loop().time())
                    response = await asyncio.wait_for(
                        _read_packet(stream, maximum=_MAXIMUM_STOP_RESULT_BYTES),
                        timeout=remaining,
                    )
                    _parse_stop_response(response)
                    return response
                finally:
                    await stream.close()
            except (OSError, TimeoutError, RuntimeError, asyncio.TimeoutError) as error:
                last_error = error
                await asyncio.sleep(0.05)
        raise HostCliError("host_stop_unavailable", "current-user Host stop channel is unavailable") from last_error

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        listener = self._listener
        self._listener = None
        if listener is not None:
            await listener.close()
        tasks = tuple(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._store.remove()

    async def _serve_one(self, stream: PipeByteStream) -> None:
        material = self._material
        if material is None:
            await stream.close()
            return
        try:
            await authenticate_server_stream(
                stream,
                material,
                now=self._now,
                replay_guard=self._replay,
            )
            payload = await _read_packet(stream, maximum=_MAXIMUM_CONTROL_BYTES)
            request = _parse_control_request(payload)
            if len(request) == 3:
                _, vault_root, client_id = request
                await self._begin_attach()
                try:
                    discovery = await self._engine.attach(vault_root, client_id=client_id)
                finally:
                    await self._end_attach()
                await _write_packet(stream, serialize_discovery_material(discovery))
                return
            stop_task = await self._begin_stop()
            await stop_task
            await _write_packet(stream, _canonical_stop_response())
        finally:
            await stream.close()
        # The owner may leave its accept loop only after the authenticated
        # caller has received a complete success packet and this pipe end has
        # been closed.  Failed shutdown never reaches this line.
        self._exit_after_receipt.set()

    async def _begin_attach(self) -> None:
        async with self._state:
            if self._stopping or self._closed:
                raise HostCliError("host_stopping", "Host is no longer accepting attaches")
            self._inflight_attaches += 1

    async def _end_attach(self) -> None:
        async with self._state:
            self._inflight_attaches -= 1
            if self._inflight_attaches < 0:
                self._inflight_attaches = 0
                raise AssertionError("Host broker attach gate underflow")
            self._state.notify_all()

    async def _begin_stop(self) -> asyncio.Task[None]:
        async with self._state:
            self._stopping = True
            if self._stop_task is None:
                self._stop_task = asyncio.create_task(
                    self._stop_after_attaches(),
                    name="offeragent-host-stop-all",
                )
            return self._stop_task

    async def _stop_after_attaches(self) -> None:
        async with self._state:
            await self._state.wait_for(lambda: self._inflight_attaches == 0)
        await self._engine.shutdown()


class HostCliApplication:
    def __init__(
        self,
        *,
        engine: HostAttachEngine,
        control: HostControlTransport,
        host_stopped: HostStoppedProbe | None = None,
    ) -> None:
        self._engine = engine
        self._control = control
        self._host_stopped = host_stopped or (lambda: False)
        self._owns_host = False

    async def attach(self, vault_root: Path, *, client_id: str) -> HostAttachOutcome:
        try:
            await self._engine.start()
        except ProcessAlreadyRunning:
            forwarded = await self._control.forward(vault_root, client_id=client_id)
            return HostAttachOutcome(forwarded, False)
        self._owns_host = True
        try:
            await self._control.start()
            material = await self._engine.attach(vault_root, client_id=client_id)
            payload = serialize_discovery_material(material)
            _validate_discovery_payload(payload)
            return HostAttachOutcome(payload, True)
        except BaseException:
            await self.close()
            raise

    async def serve_forever(self) -> None:
        if not self._owns_host:
            return
        await self._control.serve_forever()

    async def stop_all(self) -> HostStopOutcome:
        if self._host_stopped():
            return HostStopOutcome("already_stopped")
        try:
            response = await self._control.stop_all()
        except HostCliError:
            if self._host_stopped():
                return HostStopOutcome("already_stopped")
            raise
        _parse_stop_response(response)
        return HostStopOutcome("stopped")

    async def self_test_control_start_stop(self) -> None:
        """Exercise the real Host singleton/control composition without a Vault."""

        if self._owns_host:
            raise HostCliError("self_test_state_invalid", "Host self-test is already active")
        await self._engine.start()
        self._owns_host = True
        try:
            await self._control.start()
        finally:
            await self.close()

    async def close(self) -> None:
        if not self._owns_host:
            return
        self._owns_host = False
        await self._control.close()
        await self._engine.shutdown()


async def run_attach_contract(
    application: HostCliApplication,
    *,
    discovery_fd: int,
    vault_root_fd: int,
    stay_resident: bool = True,
) -> None:
    if discovery_fd != 3 or vault_root_fd != 4 or discovery_fd == vault_root_fd:
        raise HostCliError("fd_contract_invalid", "Host attach requires the fixed fd3/fd4 contract")
    vault_root = _read_vault_root(vault_root_fd)
    await _run_resolved_attach(
        application,
        vault_root,
        discovery_fd=discovery_fd,
        stay_resident=stay_resident,
    )


async def run_self_test_attach_contract(
    application: HostCliApplication,
    *,
    nonce: str,
    discovery_fd: int,
    vault_root_fd: int,
    stay_resident: bool = True,
) -> None:
    """Attach only the nonce-bound synthetic Vault through the production engine."""

    if _SELF_TEST_NONCE.fullmatch(nonce) is None:
        raise HostCliError("self_test_nonce_invalid", "Host self-test nonce is invalid")
    if discovery_fd != 3 or vault_root_fd != 4 or discovery_fd == vault_root_fd:
        raise HostCliError("fd_contract_invalid", "Host self-test attach requires the fixed fd3/fd4 contract")
    vault_root = _read_vault_root(vault_root_fd)
    from .windows_process import self_test_runtime_sandbox_root

    try:
        expected = (self_test_runtime_sandbox_root(nonce) / "Vault").resolve(strict=True)
    except OSError as error:
        raise HostCliError("self_test_vault_missing", "Host self-test Vault is unavailable") from error
    if os.path.normcase(str(vault_root)) != os.path.normcase(str(expected)):
        raise HostCliError("self_test_vault_mismatch", "Host self-test attach is confined to its synthetic Vault")
    await _run_resolved_attach(
        application,
        vault_root,
        discovery_fd=discovery_fd,
        stay_resident=stay_resident,
    )


async def _run_resolved_attach(
    application: HostCliApplication,
    vault_root: Path,
    *,
    discovery_fd: int,
    stay_resident: bool,
) -> None:
    outcome = await application.attach(vault_root, client_id=f"obsidian-pid-{os.getppid()}")
    _write_discovery(discovery_fd, outcome.discovery)
    if outcome.owns_host and stay_resident:
        try:
            await application.serve_forever()
        finally:
            await application.close()


async def run_stop_contract(
    application: HostCliApplication,
    *,
    result_fd: int,
) -> None:
    if result_fd != 3:
        raise HostCliError("fd_contract_invalid", "Host stop requires the fixed fd3 contract")
    outcome = await application.stop_all()
    _write_stop_result(result_fd, _canonical_cli_stop_result(outcome.status))


def parse_attach_arguments(arguments: list[str]) -> tuple[int, int]:
    if arguments != ["attach", "--discovery-fd", "3", "--vault-root-fd", "4"]:
        raise HostCliError("arguments_invalid", "Host supports only the fixed attach fd contract")
    return 3, 4


def parse_stop_arguments(arguments: list[str]) -> int:
    if arguments != ["stop-all", "--result-fd", "3"]:
        raise HostCliError("arguments_invalid", "Host supports only the fixed stop fd contract")
    return 3


def parse_self_test_attach_arguments(arguments: list[str]) -> tuple[str, int, int]:
    if (
        len(arguments) != 7
        or arguments[0] != "self-test-attach"
        or arguments[1] != "--nonce"
        or _SELF_TEST_NONCE.fullmatch(arguments[2]) is None
        or arguments[3:] != ["--discovery-fd", "3", "--vault-root-fd", "4"]
    ):
        raise HostCliError("arguments_invalid", "Host self-test attach requires the fixed nonce/fd3/fd4 contract")
    return arguments[2], 3, 4


def parse_self_test_stop_arguments(arguments: list[str]) -> tuple[str, int]:
    if (
        len(arguments) != 5
        or arguments[0] != "self-test-stop-all"
        or arguments[1] != "--nonce"
        or _SELF_TEST_NONCE.fullmatch(arguments[2]) is None
        or arguments[3:] != ["--result-fd", "3"]
    ):
        raise HostCliError("arguments_invalid", "Host self-test stop requires the fixed nonce/fd3 contract")
    return arguments[2], 3


def _read_vault_root(descriptor: int) -> Path:
    payload = bytearray()
    try:
        while len(payload) <= _MAXIMUM_VAULT_ROOT_BYTES:
            chunk = os.read(descriptor, min(4096, _MAXIMUM_VAULT_ROOT_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.close(descriptor)
    try:
        if not payload or len(payload) > _MAXIMUM_VAULT_ROOT_BYTES:
            raise HostCliError("vault_root_size", "Vault root fd payload is outside limits")
        text = payload.decode("utf-8", errors="strict")
        if text.startswith("\ufeff") or "\x00" in text or "\r" in text or "\n" in text:
            raise HostCliError("vault_root_encoding", "Vault root fd payload is not canonical UTF-8")
        if text.startswith(("\\\\", "//")) or re.match(r"^[\\/]{2}[?.][\\/]", text):
            raise HostCliError("vault_root_remote", "UNC and device Vault roots are forbidden")
        path = Path(text)
        if not path.is_absolute():
            raise HostCliError("vault_root_relative", "Vault root must be absolute")
        canonical = path.resolve(strict=True)
        if not canonical.is_dir():
            raise HostCliError("vault_root_type", "Vault root is not a directory")
        return canonical
    except UnicodeError as error:
        raise HostCliError("vault_root_encoding", "Vault root fd payload is not UTF-8") from error
    except OSError as error:
        raise HostCliError("vault_root_unavailable", "Vault root is unavailable") from error
    finally:
        payload[:] = b"\0" * len(payload)


def _write_discovery(descriptor: int, payload: bytes) -> None:
    try:
        _validate_discovery_payload(payload)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise HostCliError("discovery_write_failed", "Host discovery fd made no progress")
            view = view[written:]
    finally:
        os.close(descriptor)


def _write_stop_result(descriptor: int, payload: bytes) -> None:
    try:
        _parse_cli_stop_result(payload)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise HostCliError("stop_result_write_failed", "Host stop result fd made no progress")
            view = view[written:]
    finally:
        os.close(descriptor)


def _validate_discovery_payload(payload: bytes) -> None:
    if not payload or len(payload) > _MAXIMUM_DISCOVERY_BYTES or not payload.endswith(b"\n"):
        raise HostCliError("discovery_invalid", "Host discovery payload is outside limits")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HostCliError("discovery_invalid", "Host discovery payload is malformed") from error
    expected = {"bootstrapNonce", "expiresAt", "issuedAt", "pipeName", "schemaVersion"}
    if not isinstance(value, dict) or set(value) != expected or value["schemaVersion"] != 1:
        raise HostCliError("discovery_invalid", "Host discovery fields are invalid")
    canonical = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    if canonical != payload:
        raise HostCliError("discovery_invalid", "Host discovery payload is not canonical")
    try:
        parse_discovery_material(payload, now=datetime.now(timezone.utc))
    except (RuntimeError, ValueError) as error:
        raise HostCliError("discovery_invalid", "Host discovery values are invalid") from error


def _canonical_control_request(vault_root: Path, client_id: str) -> bytes:
    if _CLIENT_ID.fullmatch(client_id) is None:
        raise HostCliError("client_id_invalid", "Host attach client identity is invalid")
    return (
        json.dumps(
            {"clientId": client_id, "schemaVersion": 1, "vaultRoot": str(vault_root)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _canonical_stop_request() -> bytes:
    return _canonical_json({"operation": "stop_all", "schemaVersion": 1})


def _canonical_stop_response() -> bytes:
    return _canonical_json({"schemaVersion": 1, "status": "stopped", "type": "stopResult"})


def _canonical_cli_stop_result(status: str) -> bytes:
    if status not in {"stopped", "already_stopped"}:
        raise HostCliError("stop_result_invalid", "Host stop result status is invalid")
    return _canonical_json({"schemaVersion": 1, "status": status, "type": "result"})


def _parse_control_request(payload: bytes) -> tuple[str, Path, str] | tuple[str]:
    try:
        raw = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HostCliError("control_request_invalid", "Host control request is malformed") from error
    if raw == {"operation": "stop_all", "schemaVersion": 1}:
        if _canonical_stop_request() != payload:
            raise HostCliError("control_request_invalid", "Host control request is not canonical")
        return ("stop_all",)
    expected = {"clientId", "schemaVersion", "vaultRoot"}
    if not isinstance(raw, dict) or set(raw) != expected or raw["schemaVersion"] != 1:
        raise HostCliError("control_request_invalid", "Host control request fields are invalid")
    client_id = raw["clientId"]
    vault_text = raw["vaultRoot"]
    if not isinstance(client_id, str) or not isinstance(vault_text, str) or _CLIENT_ID.fullmatch(client_id) is None:
        raise HostCliError("control_request_invalid", "Host control request values are invalid")
    if _canonical_control_request(Path(vault_text), client_id) != payload:
        raise HostCliError("control_request_invalid", "Host control request is not canonical")
    # It arrived through an authenticated current-SID pipe, but it still gets
    # the same local-root validation as direct fd4 input.
    encoded = vault_text.encode("utf-8")
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, encoded)
    finally:
        os.close(write_fd)
    return "attach", _read_vault_root(read_fd), client_id


def _parse_stop_response(payload: bytes) -> None:
    if not payload or len(payload) > _MAXIMUM_STOP_RESULT_BYTES:
        raise HostCliError("stop_response_invalid", "Host stop response is outside limits")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HostCliError("stop_response_invalid", "Host stop response is malformed") from error
    expected = {"schemaVersion": 1, "status": "stopped", "type": "stopResult"}
    if value != expected or payload != _canonical_stop_response():
        raise HostCliError("stop_response_invalid", "Host stop response is invalid")


def _parse_cli_stop_result(payload: bytes) -> HostStopOutcome:
    if not payload or len(payload) > _MAXIMUM_STOP_RESULT_BYTES:
        raise HostCliError("stop_result_invalid", "Host stop result is outside limits")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HostCliError("stop_result_invalid", "Host stop result is malformed") from error
    expected_keys = {"schemaVersion", "status", "type"}
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value["schemaVersion"] != 1
        or value["type"] != "result"
        or value["status"] not in {"stopped", "already_stopped"}
        or payload != _canonical_cli_stop_result(value["status"])
    ):
        raise HostCliError("stop_result_invalid", "Host stop result is invalid")
    return HostStopOutcome(value["status"])


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


async def _read_packet(stream: PipeByteStream, *, maximum: int) -> bytes:
    header = await _read_exact(stream, 4)
    size = struct.unpack(">I", header)[0]
    if size < 1 or size > maximum:
        raise HostCliError("control_size_invalid", "Host control packet is outside limits")
    return await _read_exact(stream, size)


async def _write_packet(stream: PipeByteStream, payload: bytes) -> None:
    if not payload or len(payload) > _MAXIMUM_CONTROL_BYTES:
        raise HostCliError("control_size_invalid", "Host control packet is outside limits")
    await stream.write(struct.pack(">I", len(payload)) + payload)


async def _read_exact(stream: PipeByteStream, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = await stream.read(size - len(chunks))
        if not chunk:
            raise HostCliError("control_truncated", "Host control packet ended early")
        chunks.extend(chunk)
    return bytes(chunks)


def main(arguments: list[str] | None = None, *, application: HostCliApplication | None = None) -> int:
    try:
        actual = list(sys.argv[1:] if arguments is None else arguments)
        if actual[:1] == ["attach"]:
            discovery_fd, vault_fd = parse_attach_arguments(actual)
            if application is None:
                from .production_host_composition import create_production_host_application

                application = create_production_host_application()
            asyncio.run(
                run_attach_contract(
                    application,
                    discovery_fd=discovery_fd,
                    vault_root_fd=vault_fd,
                )
            )
        elif actual[:1] == ["stop-all"]:
            result_fd = parse_stop_arguments(actual)
            if application is None:
                from .production_host_composition import create_production_host_application

                application = create_production_host_application()
            asyncio.run(run_stop_contract(application, result_fd=result_fd))
        elif actual[:1] == ["self-test-attach"]:
            nonce, discovery_fd, vault_fd = parse_self_test_attach_arguments(actual)
            if application is None:
                from .production_host_composition import create_production_host_application

                application = create_production_host_application(self_test_nonce=nonce)
            asyncio.run(
                run_self_test_attach_contract(
                    application,
                    nonce=nonce,
                    discovery_fd=discovery_fd,
                    vault_root_fd=vault_fd,
                )
            )
        elif actual[:1] == ["self-test-stop-all"]:
            nonce, result_fd = parse_self_test_stop_arguments(actual)
            if application is None:
                from .production_host_composition import create_production_host_application

                application = create_production_host_application(self_test_nonce=nonce)
            asyncio.run(run_stop_contract(application, result_fd=result_fd))
        else:
            raise HostCliError("arguments_invalid", "Host command is invalid")
    except BaseException:
        # Never echo exception text: it may contain a Vault path.  Packaged
        # diagnostics use structured, redacted Runtime logs instead.
        try:
            os.write(2, b"offeragent-host: operation failed\n")
        except OSError:
            pass
        return 2
    return 0


__all__ = [
    "DpapiWorkspaceDiscoveryProvider",
    "HostAttachEngine",
    "HostAttachOutcome",
    "HostCliApplication",
    "HostCliError",
    "HostControlBroker",
    "HostControlTransport",
    "HostStopOutcome",
    "HostStoppedProbe",
    "SupervisedHostAttachEngine",
    "WorkspaceDiscoveryProvider",
    "main",
    "parse_attach_arguments",
    "parse_self_test_attach_arguments",
    "parse_self_test_stop_arguments",
    "parse_stop_arguments",
    "run_attach_contract",
    "run_self_test_attach_contract",
    "run_stop_contract",
]
