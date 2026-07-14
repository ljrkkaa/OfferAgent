from __future__ import annotations

import asyncio
import os
import struct
from typing import Any, cast

import pytest

from offeragent_harness.runtime import worker_control as worker_control_module
from offeragent_harness.runtime.host_supervisor import WorkerReadiness, WorkerShutdownReceipt
from offeragent_harness.runtime.production_worker_composition import (
    ProductionWorkerApplication,
    ProductionWorkerError,
    _ProductionNamedPipeServer,
    _WorkerControl,
)
from offeragent_harness.runtime.worker_control import WorkerControlServer


class _BlockingControl:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def close(self) -> None:
        self.calls.append("control")
        self.entered.set()
        await self.release.wait()


class _FailingControl:
    async def close(self) -> None:
        raise OSError("injected control close failure")


class _Pipe:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def stop(self) -> None:
        self.calls.append("pipe")


class _Loopback:
    def __init__(self, calls: list[str], *, healthy: bool = True) -> None:
        self.calls = calls
        self._healthy = healthy

    @property
    def healthy(self) -> bool:
        return self._healthy

    async def stop(self) -> None:
        self.calls.append("loopback")


class _HealthPipe:
    def __init__(self, accept_task: asyncio.Task[None]) -> None:
        self.accept_task = accept_task

    @property
    def healthy(self) -> bool:
        return not self.accept_task.done()


class _Listener:
    def __init__(self, *, failure: bool = False) -> None:
        self.failure = failure

    async def close(self) -> None:
        if self.failure:
            raise OSError("injected listener close failure")


class _BlockingListener:
    def __init__(self, *, failure: bool = False) -> None:
        self.failure = failure
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def close(self) -> None:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.failure:
            raise OSError("injected blocking listener close failure")


class _ClosableStream:
    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def close(self) -> None:
        self.closed.set()


class _PostCloseAcceptListener:
    def __init__(self, stream: _ClosableStream) -> None:
        self.stream = stream
        self.accept_entered = asyncio.Event()
        self.accept_release = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_calls = 0

    async def accept(self) -> _ClosableStream:
        self.accept_entered.set()
        await self.accept_release.wait()
        return self.stream

    async def close(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        self.accept_release.set()
        await self.close_release.wait()


class _CloseAbortsAcceptListener:
    def __init__(self) -> None:
        self.accept_entered = asyncio.Event()
        self.aborted = asyncio.Event()
        self.close_calls = 0

    async def accept(self) -> object:
        self.accept_entered.set()
        await self.aborted.wait()
        raise OSError("injected expected accept abort")

    async def close(self) -> None:
        self.close_calls += 1
        self.aborted.set()


class _Store:
    def __init__(self) -> None:
        self.removes = 0

    def remove(self) -> None:
        self.removes += 1


class _BlockingResponseStream:
    def __init__(self, request: bytes) -> None:
        self._request = bytearray(request)
        self.write_entered = asyncio.Event()
        self.release_write = asyncio.Event()
        self.response = b""
        self.write_cancelled = False
        self.closed = False

    async def read(self, maximum_bytes: int) -> bytes:
        value = bytes(self._request[:maximum_bytes])
        del self._request[:maximum_bytes]
        return value

    async def write(self, data: bytes) -> None:
        self.write_entered.set()
        try:
            await self.release_write.wait()
        except asyncio.CancelledError:
            self.write_cancelled = True
            raise
        self.response += data

    async def close(self) -> None:
        self.closed = True


class _FailingResponseStream(_BlockingResponseStream):
    def __init__(self, request: bytes, *, failure: str) -> None:
        super().__init__(request)
        self.failure = failure
        self.release_write.set()

    async def write(self, data: bytes) -> None:
        if self.failure == "write":
            self.write_entered.set()
            raise OSError("injected shutdown receipt write failure")
        await super().write(data)

    async def close(self) -> None:
        await super().close()
        if self.failure == "close":
            raise OSError("injected shutdown stream close failure")


def _application(*, control: object, pipe: object, loopback: object) -> ProductionWorkerApplication:
    application = object.__new__(ProductionWorkerApplication)
    raw = cast(Any, application)
    raw._control = control
    raw._control_task = None
    raw._pipe = pipe
    raw.loopback = loopback
    raw._stopped = False
    raw._transport_shutdown_task = None
    raw._fatal_error = None
    raw._shutdown_delivery_started = False
    raw._shutdown_delivery_finalized = asyncio.Event()
    raw._shutdown_delivery_task = None
    raw._shutdown_event = asyncio.Event()
    raw._background_tasks = set()
    return application


async def _wait_for_release(release: asyncio.Event) -> None:
    await release.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_component", ["named_pipe", "control", "loopback"])
async def test_native_transport_task_completion_closes_worker_readiness_gate(failed_component: str) -> None:
    watcher_release = asyncio.Event()
    pipe_release = asyncio.Event()
    control_release = asyncio.Event()
    watcher_task = asyncio.create_task(_wait_for_release(watcher_release))
    pipe_task = asyncio.create_task(_wait_for_release(pipe_release))
    control_task = asyncio.create_task(_wait_for_release(control_release))
    application = object.__new__(ProductionWorkerApplication)
    raw = cast(Any, application)
    raw.native_transports = True
    raw._ready = True
    raw._stopped = False
    raw._fatal_error = None
    raw._watcher_task = watcher_task
    raw._pipe = _HealthPipe(pipe_task)
    raw._control = object()
    raw._control_task = control_task
    loopback = _Loopback([])
    raw.loopback = loopback

    try:
        assert application.ready
        if failed_component == "named_pipe":
            pipe_release.set()
            await pipe_task
        elif failed_component == "control":
            control_release.set()
            await control_task
        else:
            loopback._healthy = False

        assert not application.ready
    finally:
        for task in (watcher_task, pipe_task, control_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(watcher_task, pipe_task, control_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_transport_listener_failure_runs_fatal_cleanup_and_is_fully_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    events: list[tuple[str, dict[str, object]]] = []
    application = _application(control=None, pipe=None, loopback=_Loopback(calls))
    raw = cast(Any, application)
    raw._shutdown_committed = False
    raw.reject_new_runs = False
    listener_failure = RuntimeError("injected listener failure")
    commit_failure = RuntimeError("injected commit failure")

    async def emit(
        self: ProductionWorkerApplication,
        level: object,
        event: str,
        message: str,
        **metrics: object,
    ) -> None:
        del self, level, message
        events.append((event, dict(metrics)))

    async def commit_shutdown(
        self: ProductionWorkerApplication,
        *,
        grace_seconds: float = 10.0,
    ) -> WorkerShutdownReceipt:
        del self, grace_seconds
        calls.append("commit")
        raise commit_failure

    async def finish_transport(self: ProductionWorkerApplication) -> None:
        del self
        calls.append("transport")

    async def fail_listener() -> None:
        raise listener_failure

    monkeypatch.setattr(ProductionWorkerApplication, "_emit_runtime_log", emit)
    monkeypatch.setattr(ProductionWorkerApplication, "commit_shutdown", commit_shutdown)
    monkeypatch.setattr(ProductionWorkerApplication, "_finish_transport_shutdown", finish_transport)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    exception_contexts: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: exception_contexts.append(dict(context)))
    listener_task = asyncio.create_task(fail_listener(), name="injected-control-listener")
    listener_task.add_done_callback(lambda task: application._transport_listener_finished("control", task))
    waiter = asyncio.create_task(application.wait_stopped())

    try:
        with pytest.raises(ProductionWorkerError, match="control component terminated unexpectedly") as captured:
            await asyncio.wait_for(waiter, timeout=1)
        await asyncio.sleep(0)

        assert raw.reject_new_runs
        assert calls == ["commit", "transport"]
        assert captured.value is raw._fatal_error
        assert raw._shutdown_event.is_set()
        assert raw._background_tasks == set()
        assert getattr(listener_task, "_log_traceback", False) is False
        assert (
            "runtime.required_component_failed",
            {"component": "control", "errorType": "RuntimeError"},
        ) in events
        assert any(event == "runtime.fatal_shutdown_commit_failed" for event, _metrics in events)
        assert not [
            context for context in exception_contexts if context.get("message") == "Task exception was never retrieved"
        ]
    finally:
        await asyncio.gather(listener_task, return_exceptions=True)
        background = tuple(raw._background_tasks)
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_concurrent_transport_shutdown_waits_for_one_complete_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    control = _BlockingControl(calls)
    application = _application(control=control, pipe=_Pipe(calls), loopback=_Loopback(calls))

    async def emit(self: ProductionWorkerApplication, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        calls.append("log")

    monkeypatch.setattr(ProductionWorkerApplication, "_emit_runtime_log", emit)

    first = asyncio.create_task(application._finish_transport_shutdown())
    await asyncio.wait_for(control.entered.wait(), timeout=1)
    second = asyncio.create_task(application._finish_transport_shutdown())
    await asyncio.sleep(0)

    assert application._stopped is False
    assert second.done() is False
    assert application._shutdown_event.is_set() is False
    assert calls == ["pipe", "loopback", "control"]

    control.release.set()
    await asyncio.gather(first, second)

    assert calls == ["pipe", "loopback", "control", "log"]
    assert application._stopped is True
    assert application._shutdown_event.is_set() is True

    # A later shutdown observes the completed single-flight task and cannot
    # execute teardown a second time.
    await application._finish_transport_shutdown()
    assert calls == ["pipe", "loopback", "control", "log"]


@pytest.mark.asyncio
async def test_transport_shutdown_attempts_all_capability_revocation_before_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    application = _application(control=_FailingControl(), pipe=_Pipe(calls), loopback=_Loopback(calls))

    async def emit(self: ProductionWorkerApplication, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        calls.append("error-log")

    monkeypatch.setattr(ProductionWorkerApplication, "_emit_runtime_log", emit)

    with pytest.raises(ProductionWorkerError, match="transport shutdown was incomplete"):
        await application._finish_transport_shutdown()

    assert calls == ["pipe", "loopback", "error-log"]
    assert application._stopped is False
    # Failure is terminal (and observable via wait_stopped) even though the
    # success-only _stopped flag must remain false.
    assert application._shutdown_event.is_set() is True


@pytest.mark.asyncio
async def test_scheduled_transport_failure_wakes_wait_stopped_and_propagates_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The process-level waiter must reach a terminal failure, never wait forever."""

    calls: list[str] = []
    application = _application(control=_FailingControl(), pipe=_Pipe(calls), loopback=_Loopback(calls))

    async def emit(self: ProductionWorkerApplication, *args: object, **kwargs: object) -> None:
        del self, args, kwargs

    monkeypatch.setattr(ProductionWorkerApplication, "_emit_runtime_log", emit)

    application.schedule_transport_shutdown()
    scheduled = application._transport_shutdown_task
    assert scheduled is not None
    try:
        with pytest.raises(ProductionWorkerError, match="transport shutdown was incomplete"):
            await asyncio.wait_for(application.wait_stopped(), timeout=0.25)
    finally:
        # Keep the intentionally failing scheduler task observed even when the
        # assertion above demonstrates the current wait-stopped deadlock.
        await asyncio.gather(scheduled, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_response_is_flushed_before_transport_cancels_control_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timer is not a response boundary: teardown must await the actual write."""

    calls: list[str] = []
    application = _application(control=None, pipe=_Pipe(calls), loopback=_Loopback(calls))
    receipt = WorkerShutdownReceipt(True, True, True, True)
    readiness = WorkerReadiness(
        pid=os.getpid(),
        runtime_version="1.0.0",
        workspace_instance_id="workspace-1",
        canonical_root_identity="sha256:" + "1" * 64,
        database_identity="sha256:" + "2" * 64,
    )

    async def authenticate(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def commit_shutdown(self: ProductionWorkerApplication, *, grace_seconds: float = 10.0) -> object:
        del self, grace_seconds
        return receipt

    async def emit(self: ProductionWorkerApplication, *args: object, **kwargs: object) -> None:
        del self, args, kwargs

    def readiness_result(self: _WorkerControl) -> WorkerReadiness:
        del self
        return readiness

    monkeypatch.setattr(worker_control_module, "authenticate_server_stream", authenticate)
    monkeypatch.setattr(ProductionWorkerApplication, "commit_shutdown", commit_shutdown)
    monkeypatch.setattr(ProductionWorkerApplication, "begin_shutdown_delivery", lambda self: None)
    monkeypatch.setattr(ProductionWorkerApplication, "_emit_runtime_log", emit)
    monkeypatch.setattr(_WorkerControl, "readiness", readiness_result)

    handler = _WorkerControl(application)

    def shutdown_request_finalized() -> None:
        application.schedule_transport_shutdown()

    server = WorkerControlServer(
        store=cast(Any, _Store()),
        handler=handler,
        shutdown_request_finalized=shutdown_request_finalized,
    )
    raw_server = cast(Any, server)
    raw_server._material = object()
    cast(Any, application)._control = server

    payload = worker_control_module._canonical_json({"operation": "shutdown", "pid": os.getpid(), "schemaVersion": 1})
    stream = _BlockingResponseStream(struct.pack(">I", len(payload)) + payload)
    serve_task = asyncio.create_task(server._serve_one(cast(Any, stream)))
    raw_server._tasks.add(serve_task)

    try:
        await asyncio.wait_for(stream.write_entered.wait(), timeout=1)
        # Several loop turns (and any elapsed wall-clock delay) cannot trigger
        # teardown while the write itself is still blocked.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert serve_task.done() is False
        assert stream.write_cancelled is False

        stream.release_write.set()
        await asyncio.wait_for(serve_task, timeout=1)
        await asyncio.wait_for(application.wait_stopped(), timeout=1)
        assert stream.response
    finally:
        stream.release_write.set()
        await asyncio.gather(serve_task, return_exceptions=True)
        background = tuple(application._background_tasks)
        if background:
            await asyncio.gather(*background, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["write", "close"])
async def test_worker_control_ack_loss_still_finalizes_shutdown_request(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    readiness = WorkerReadiness(
        pid=os.getpid(),
        runtime_version="1.0.0",
        workspace_instance_id="workspace-ack-loss",
        canonical_root_identity="sha256:" + "3" * 64,
        database_identity="sha256:" + "4" * 64,
    )
    receipt = WorkerShutdownReceipt(True, True, True, True)

    class ShutdownHandler:
        def readiness(self) -> WorkerReadiness:
            return readiness

        async def graceful_shutdown(self) -> WorkerShutdownReceipt:
            return receipt

    async def authenticate(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(worker_control_module, "authenticate_server_stream", authenticate)
    finalized: list[str] = []
    server = WorkerControlServer(
        store=cast(Any, _Store()),
        handler=cast(Any, ShutdownHandler()),
        shutdown_request_finalized=lambda: finalized.append("shutdown"),
    )
    cast(Any, server)._material = object()
    payload = worker_control_module._canonical_json({"operation": "shutdown", "pid": os.getpid(), "schemaVersion": 1})
    stream = _FailingResponseStream(
        struct.pack(">I", len(payload)) + payload,
        failure=failure,
    )

    with pytest.raises(OSError, match=f"shutdown .* {failure} failure"):
        await server._serve_one(cast(Any, stream))

    assert finalized == ["shutdown"]
    assert stream.closed is True


@pytest.mark.asyncio
async def test_scheduled_transport_failure_is_logged_without_unretrieved_task_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    events: list[str] = []
    application = _application(control=_FailingControl(), pipe=_Pipe(calls), loopback=_Loopback(calls))

    async def emit(
        self: ProductionWorkerApplication,
        level: object,
        event: str,
        message: str,
        **metrics: object,
    ) -> None:
        del self, level, message, metrics
        events.append(event)

    monkeypatch.setattr(ProductionWorkerApplication, "_emit_runtime_log", emit)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    exception_contexts: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: exception_contexts.append(dict(context)))

    application.schedule_transport_shutdown()
    scheduled = application._transport_shutdown_task
    assert scheduled is not None
    completed = asyncio.Event()
    scheduled.add_done_callback(lambda _task: completed.set())
    try:
        await asyncio.wait_for(completed.wait(), timeout=1)
        await asyncio.sleep(0)
        assert "runtime.transport_shutdown_failed" in events

        # ``Future._log_traceback`` is the CPython flag consumed by
        # Task.exception()/result().  Leaving it true is exactly what later
        # emits "Task exception was never retrieved" when the Task is released.
        assert getattr(scheduled, "_log_traceback", False) is False
        assert not [
            context for context in exception_contexts if context.get("message") == "Task exception was never retrieved"
        ]
    finally:
        if scheduled.done() and not scheduled.cancelled():
            scheduled.exception()
        loop.set_exception_handler(previous_handler)
        transport = application._transport_shutdown_task
        if transport is not None and transport.done() and not transport.cancelled():
            transport.exception()


@pytest.mark.asyncio
async def test_named_pipe_stop_revokes_discovery_after_listener_failure() -> None:
    server = object.__new__(_ProductionNamedPipeServer)
    raw = cast(Any, server)
    store = _Store()
    raw._listener = _Listener(failure=True)
    raw._accept_task = None
    raw._connections = set()
    raw._client_tasks = set()
    raw._store = store

    with pytest.raises(ProductionWorkerError, match="Named Pipe shutdown was incomplete"):
        await server.stop()

    assert store.removes == 1


@pytest.mark.asyncio
async def test_worker_control_close_does_not_cancel_its_current_handler() -> None:
    store = _Store()
    server = WorkerControlServer(store=cast(Any, store), handler=cast(Any, object()))
    raw = cast(Any, server)
    raw._listener = _Listener()
    current = asyncio.current_task()
    assert current is not None
    raw._tasks = {current}

    await server.close()

    # ``asyncio.current_task`` is typed against the oldest supported Task
    # surface by mypy, while the bundled Python runtime provides cancelling().
    assert cast(Any, current).cancelling() == 0
    assert store.removes == 1


@pytest.mark.asyncio
async def test_worker_control_concurrent_close_waits_for_one_complete_teardown() -> None:
    store = _Store()
    listener = _BlockingListener()
    server = WorkerControlServer(store=cast(Any, store), handler=cast(Any, object()))
    raw = cast(Any, server)
    raw._listener = listener

    first = asyncio.create_task(server.close())
    await asyncio.wait_for(listener.entered.wait(), timeout=1)
    second = asyncio.create_task(server.close())
    await asyncio.sleep(0)

    assert first.done() is False
    assert second.done() is False
    assert listener.calls == 1
    assert store.removes == 0

    listener.release.set()
    await asyncio.gather(first, second)
    await server.close()

    assert listener.calls == 1
    assert store.removes == 1


@pytest.mark.asyncio
async def test_worker_control_close_failure_is_shared_and_not_retried() -> None:
    store = _Store()
    listener = _BlockingListener(failure=True)
    server = WorkerControlServer(store=cast(Any, store), handler=cast(Any, object()))
    raw = cast(Any, server)
    raw._listener = listener

    first = asyncio.create_task(server.close())
    await asyncio.wait_for(listener.entered.wait(), timeout=1)
    second = asyncio.create_task(server.close())
    listener.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert len(results) == 2
    assert isinstance(results[0], RuntimeError)
    assert results[1] is results[0]
    with pytest.raises(RuntimeError) as repeated:
        await server.close()
    assert repeated.value is results[0]
    assert listener.calls == 1
    assert store.removes == 1


@pytest.mark.asyncio
async def test_worker_control_closes_accept_that_completes_after_close_claim() -> None:
    store = _Store()
    stream = _ClosableStream()
    listener = _PostCloseAcceptListener(stream)
    server = WorkerControlServer(store=cast(Any, store), handler=cast(Any, object()))
    raw = cast(Any, server)
    raw._listener = listener

    serving = asyncio.create_task(server.serve_forever())
    await asyncio.wait_for(listener.accept_entered.wait(), timeout=1)
    closing = asyncio.create_task(server.close())
    await asyncio.wait_for(listener.close_entered.wait(), timeout=1)
    await asyncio.wait_for(stream.closed.wait(), timeout=1)

    assert raw._tasks == set()
    assert serving.done() is False
    assert closing.done() is False

    listener.close_release.set()
    await asyncio.gather(serving, closing)

    assert listener.close_calls == 1
    assert store.removes == 1
    assert raw._tasks == set()


@pytest.mark.asyncio
async def test_worker_control_treats_close_induced_accept_error_as_completion() -> None:
    store = _Store()
    listener = _CloseAbortsAcceptListener()
    server = WorkerControlServer(store=cast(Any, store), handler=cast(Any, object()))
    raw = cast(Any, server)
    raw._listener = listener

    serving = asyncio.create_task(server.serve_forever())
    await asyncio.wait_for(listener.accept_entered.wait(), timeout=1)
    closing = asyncio.create_task(server.close())
    await asyncio.gather(serving, closing)

    assert listener.close_calls == 1
    assert store.removes == 1
    assert raw._tasks == set()
