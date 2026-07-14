"""Authenticated private Host↔Worker readiness and shutdown control plane."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from .host_supervisor import (
    ResumeValidation,
    SupervisedWorkspaceIdentity,
    WorkerReadiness,
    WorkerReadinessProbe,
    WorkerShutdownControl,
    WorkerShutdownReceipt,
)
from .named_pipe import (
    DiscoveryMaterial,
    DiscoveryMaterialStore,
    HandshakeReplayGuard,
    MaterialProtector,
    PipeByteStream,
    authenticate_client_stream,
    authenticate_server_stream,
)
from .windows_named_pipe import Win32NamedPipeListener, connect_windows_named_pipe

_MAXIMUM_PACKET = 64 * 1024


class WorkerControlError(RuntimeError):
    pass


class WorkerControlHandler(Protocol):
    def readiness(self) -> WorkerReadiness: ...

    async def revalidate(self) -> ResumeValidation: ...

    async def reject_new_runs(self) -> None: ...

    async def graceful_shutdown(self) -> WorkerShutdownReceipt: ...


class WorkerControlServer:
    def __init__(
        self,
        *,
        store: DiscoveryMaterialStore,
        handler: WorkerControlHandler,
        now: Callable[[], datetime] | None = None,
        shutdown_request_finalized: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._handler = handler
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._shutdown_request_finalized = shutdown_request_finalized
        self._material: DiscoveryMaterial | None = None
        self._listener: Win32NamedPipeListener | None = None
        self._replay = HandshakeReplayGuard()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._closed:
            raise WorkerControlError("Worker control server cannot restart after close")
        if self._listener is not None:
            raise WorkerControlError("Worker control server was already started")
        material = self._store.issue(now=self._now())
        self._material = material
        self._listener = Win32NamedPipeListener(material.pipe_name)

    async def serve_forever(self) -> None:
        listener = self._listener
        if listener is None:
            raise WorkerControlError("Worker control server is not started")
        try:
            while not self._closed:
                try:
                    stream = await listener.accept()
                except OSError:
                    if self._closed:
                        break
                    raise
                # ``close`` can win the race after ``accept`` has completed but
                # before this coroutine resumes.  Never turn that already
                # accepted stream into a post-close request capability.
                if self._closed:
                    await stream.close()
                    break
                task = asyncio.create_task(self._serve_one(stream), name="offeragent-worker-control")
                self._tasks.add(task)
                task.add_done_callback(self._request_finished)
        finally:
            await self.close()

    async def close(self) -> None:
        task = self._close_task
        if task is None:
            # Claim closure synchronously, before yielding to the accept loop.
            # The dedicated task makes all callers await one teardown result
            # without making a request handler await or cancel itself.
            self._closed = True
            owner = asyncio.current_task()
            task = asyncio.create_task(
                self._run_close(owner),
                name="offeragent-worker-control-close",
            )
            self._close_task = task
        await asyncio.shield(task)

    async def _run_close(self, owner: asyncio.Task[Any] | None) -> None:
        failures: list[BaseException] = []
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                await listener.close()
            except BaseException as error:
                failures.append(error)
        tasks = tuple(self._tasks)
        self._tasks.clear()
        current = asyncio.current_task()
        pending = tuple(task for task in tasks if task is not current and task is not owner)
        for task in pending:
            task.cancel()
        # A handler blocked in native Pipe I/O can report the expected Windows
        # operation-aborted error after listener revocation.  The listener and
        # discovery cleanup results, not handler completion, determine whether
        # the control capability was removed.
        await asyncio.gather(*pending, return_exceptions=True)
        try:
            # Revoke the capability even if listener/client cleanup surfaced a
            # failure.  A stopped control plane must never remain discoverable.
            self._store.remove()
        except BaseException as error:
            failures.append(error)
        if failures:
            raise WorkerControlError("Worker control shutdown was incomplete") from failures[0]

    def _request_finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _serve_one(self, stream: PipeByteStream) -> None:
        material = self._material
        if material is None:
            await stream.close()
            return
        shutdown_request_started = False
        try:
            await authenticate_server_stream(stream, material, now=self._now, replay_guard=self._replay)
            request = _parse_packet(await _read_packet(stream))
            operation = request.get("operation")
            if set(request) != {"operation", "pid", "schemaVersion"} or request.get("schemaVersion") != 1:
                raise WorkerControlError("Worker control request fields are invalid")
            if request.get("pid") != self._handler.readiness().pid:
                raise WorkerControlError("Worker control PID does not match")
            if operation == "readiness":
                response = _readiness_wire(self._handler.readiness())
            elif operation == "revalidate":
                response = _resume_wire(await self._handler.revalidate())
            elif operation == "reject_new_runs":
                await self._handler.reject_new_runs()
                response = {"accepted": True, "schemaVersion": 1, "type": "ack"}
            elif operation == "shutdown":
                shutdown_request_started = True
                response = _shutdown_wire(await self._handler.graceful_shutdown())
            else:
                raise WorkerControlError("Worker control operation is invalid")
            await _write_packet(stream, _canonical_json(response))
        finally:
            try:
                await stream.close()
            finally:
                if shutdown_request_started and self._shutdown_request_finalized is not None:
                    # Either the receipt was fully written or this request can
                    # no longer deliver it.  Both outcomes terminate a durable
                    # shutdown without relying on an ACK from the Host.
                    self._shutdown_request_finalized()


class WindowsWorkerControlClient(WorkerReadinessProbe, WorkerShutdownControl):
    def __init__(
        self,
        *,
        workspaces_root: Path,
        protector: MaterialProtector,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._workspaces_root = workspaces_root.resolve(strict=False)
        self._protector = protector
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def wait_ready(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        runtime_version: str,
        deadline: datetime,
    ) -> WorkerReadiness:
        last_error: BaseException | None = None
        while self._now() < deadline:
            try:
                raw = await self._request(expected, pid=pid, operation="readiness")
                readiness = _wire_readiness(raw)
                if readiness.runtime_version != runtime_version:
                    raise WorkerControlError("Worker control Runtime version differs")
                return readiness
            except (OSError, RuntimeError) as error:
                last_error = error
                await asyncio.sleep(0.05)
        raise TimeoutError("authenticated Worker readiness did not arrive") from last_error

    async def revalidate_after_resume(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        deadline: datetime,
    ) -> ResumeValidation:
        return _wire_resume(await self._request(expected, pid=pid, operation="revalidate", deadline=deadline))

    async def reject_new_runs(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        deadline: datetime,
    ) -> None:
        value = await self._request(expected, pid=pid, operation="reject_new_runs", deadline=deadline)
        if value != {"accepted": True, "schemaVersion": 1, "type": "ack"}:
            raise WorkerControlError("Worker reject-new-runs acknowledgement is invalid")

    async def request_graceful_shutdown(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        deadline: datetime,
    ) -> WorkerShutdownReceipt:
        return _wire_shutdown(await self._request(expected, pid=pid, operation="shutdown", deadline=deadline))

    async def _request(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        operation: str,
        deadline: datetime | None = None,
    ) -> Mapping[str, Any]:
        now = self._now()
        if deadline is not None and now >= deadline:
            raise TimeoutError("Worker control deadline expired")
        store = DiscoveryMaterialStore(
            self._workspaces_root / expected.workspace_instance_id / "control",
            protector=self._protector,
        )
        material = store.load(now=now)
        timeout = 10.0 if deadline is None else max(0.001, min(10.0, (deadline - now).total_seconds()))
        stream = await connect_windows_named_pipe(material.pipe_name, timeout_seconds=timeout)
        try:
            await authenticate_client_stream(stream, material, now=self._now, timeout_seconds=timeout)
            await _write_packet(
                stream,
                _canonical_json({"operation": operation, "pid": pid, "schemaVersion": 1}),
            )
            return _parse_packet(await _read_packet(stream))
        finally:
            await stream.close()


def _readiness_wire(value: WorkerReadiness) -> Mapping[str, Any]:
    return {
        "canonicalRootIdentity": value.canonical_root_identity,
        "databaseIdentity": value.database_identity,
        "pid": value.pid,
        "runtimeVersion": value.runtime_version,
        "schemaVersion": 1,
        "type": "readiness",
        "workspaceInstanceId": value.workspace_instance_id,
    }


def _wire_readiness(value: Mapping[str, Any]) -> WorkerReadiness:
    expected = {
        "canonicalRootIdentity",
        "databaseIdentity",
        "pid",
        "runtimeVersion",
        "schemaVersion",
        "type",
        "workspaceInstanceId",
    }
    if set(value) != expected or value["schemaVersion"] != 1 or value["type"] != "readiness":
        raise WorkerControlError("Worker readiness response fields are invalid")
    try:
        return WorkerReadiness(
            pid=_integer(value["pid"]),
            runtime_version=_text(value["runtimeVersion"]),
            workspace_instance_id=_text(value["workspaceInstanceId"]),
            canonical_root_identity=_text(value["canonicalRootIdentity"]),
            database_identity=_text(value["databaseIdentity"]),
        )
    except (TypeError, ValueError) as error:
        raise WorkerControlError("Worker readiness response values are invalid") from error


def _resume_wire(value: ResumeValidation) -> Mapping[str, Any]:
    return {
        "approvalsValid": value.approvals_valid,
        "deadlinesValid": value.deadlines_valid,
        "modelConnectionValid": value.model_connection_valid,
        "namedPipeClientValid": value.named_pipe_client_valid,
        "schemaVersion": 1,
        "type": "resume",
        "vaultHashValid": value.vault_hash_valid,
    }


def _wire_resume(value: Mapping[str, Any]) -> ResumeValidation:
    expected = {
        "approvalsValid",
        "deadlinesValid",
        "modelConnectionValid",
        "namedPipeClientValid",
        "schemaVersion",
        "type",
        "vaultHashValid",
    }
    if set(value) != expected or value["schemaVersion"] != 1 or value["type"] != "resume":
        raise WorkerControlError("Worker resume response is invalid")
    return ResumeValidation(
        deadlines_valid=_boolean(value["deadlinesValid"]),
        vault_hash_valid=_boolean(value["vaultHashValid"]),
        named_pipe_client_valid=_boolean(value["namedPipeClientValid"]),
        model_connection_valid=_boolean(value["modelConnectionValid"]),
        approvals_valid=_boolean(value["approvalsValid"]),
    )


def _shutdown_wire(value: WorkerShutdownReceipt) -> Mapping[str, Any]:
    return {
        "activeRunsCancelled": value.active_runs_cancelled,
        "interruptedStatePersisted": value.interrupted_state_persisted,
        "newRunsRejected": value.new_runs_rejected,
        "schemaVersion": 1,
        "type": "shutdown",
        "workerStateFlushed": value.worker_state_flushed,
    }


def _wire_shutdown(value: Mapping[str, Any]) -> WorkerShutdownReceipt:
    expected = {
        "activeRunsCancelled",
        "interruptedStatePersisted",
        "newRunsRejected",
        "schemaVersion",
        "type",
        "workerStateFlushed",
    }
    if set(value) != expected or value["schemaVersion"] != 1 or value["type"] != "shutdown":
        raise WorkerControlError("Worker shutdown response is invalid")
    return WorkerShutdownReceipt(
        new_runs_rejected=_boolean(value["newRunsRejected"]),
        active_runs_cancelled=_boolean(value["activeRunsCancelled"]),
        interrupted_state_persisted=_boolean(value["interruptedStatePersisted"]),
        worker_state_flushed=_boolean(value["workerStateFlushed"]),
    )


async def _read_packet(stream: PipeByteStream) -> bytes:
    header = await _read_exact(stream, 4)
    length = struct.unpack(">I", header)[0]
    if not 1 <= length <= _MAXIMUM_PACKET:
        raise WorkerControlError("Worker control packet exceeds limits")
    return await _read_exact(stream, length)


async def _write_packet(stream: PipeByteStream, payload: bytes) -> None:
    if not 1 <= len(payload) <= _MAXIMUM_PACKET:
        raise WorkerControlError("Worker control packet exceeds limits")
    await stream.write(struct.pack(">I", len(payload)) + payload)


async def _read_exact(stream: PipeByteStream, length: int) -> bytes:
    value = bytearray()
    while len(value) < length:
        chunk = await stream.read(length - len(value))
        if not chunk:
            raise WorkerControlError("Worker control packet is truncated")
        value.extend(chunk)
    return bytes(value)


def _parse_packet(payload: bytes) -> Mapping[str, Any]:
    try:
        raw = cast(object, json.loads(payload.decode("utf-8", errors="strict")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WorkerControlError("Worker control packet is malformed") from error
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise WorkerControlError("Worker control packet is not an object")
    if _canonical_json(raw) != payload:
        raise WorkerControlError("Worker control packet is not canonical")
    return raw


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError
    return value


__all__ = [
    "WindowsWorkerControlClient",
    "WorkerControlError",
    "WorkerControlHandler",
    "WorkerControlServer",
]
