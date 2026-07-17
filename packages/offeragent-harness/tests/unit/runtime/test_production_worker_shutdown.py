from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest

from offeragent_harness.runtime.process_identity import WorkerShutdownReceipt
from offeragent_harness.runtime.production_worker_composition import (
    ProductionWorkerApplication,
    ProductionWorkerError,
)


class _CommitHarness:
    def __init__(
        self,
        commit: Callable[[float], Awaitable[WorkerShutdownReceipt]],
    ) -> None:
        self._commit = commit
        self._shutdown_committed = False
        self._shutdown_commit_task: asyncio.Task[WorkerShutdownReceipt] | None = None

    async def _commit_shutdown_once(self, *, grace_seconds: float) -> WorkerShutdownReceipt:
        return await self._commit(grace_seconds)

    def _shutdown_commit_finished(self, task: asyncio.Task[WorkerShutdownReceipt]) -> None:
        if not task.cancelled():
            task.exception()


async def _commit(
    harness: _CommitHarness,
    *,
    grace_seconds: float,
) -> WorkerShutdownReceipt:
    application = cast(Any, harness)
    return await ProductionWorkerApplication.commit_shutdown(application, grace_seconds=grace_seconds)


def _terminal_application() -> ProductionWorkerApplication:
    application = object.__new__(ProductionWorkerApplication)
    raw = cast(Any, application)
    raw._ready = False
    raw._stopped = False
    raw._shutdown_task = None
    raw._shutdown_committed = False
    raw._shutdown_commit_task = None
    raw._shutdown_delivery_started = False
    raw._shutdown_delivery_finalized = asyncio.Event()
    raw._shutdown_delivery_task = None
    raw._fatal_error = None
    raw._background_tasks = set()
    raw._transport_shutdown_task = None
    raw._shutdown_event = asyncio.Event()
    raw.reject_new_runs = False
    return application


async def _wait_for_terminal_tasks(application: ProductionWorkerApplication) -> None:
    raw = cast(Any, application)
    tasks = tuple(raw._background_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_commit_shutdown_is_one_task_and_every_waiter_receives_the_same_receipt() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[float] = []
    receipt = WorkerShutdownReceipt(True, True, True, True)

    async def execute(grace_seconds: float) -> WorkerShutdownReceipt:
        calls.append(grace_seconds)
        started.set()
        await release.wait()
        return receipt

    harness = _CommitHarness(execute)
    first = asyncio.create_task(_commit(harness, grace_seconds=1.25))
    await started.wait()
    followers = tuple(asyncio.create_task(_commit(harness, grace_seconds=value)) for value in (2.0, 3.0, 4.0))
    await asyncio.sleep(0)

    assert calls == [1.25]
    assert harness._shutdown_committed
    release.set()
    results = await asyncio.gather(first, *followers)

    assert all(result is receipt for result in results)
    assert await _commit(harness, grace_seconds=99.0) is receipt
    assert calls == [1.25]


@pytest.mark.asyncio
async def test_commit_shutdown_failure_is_shared_and_never_degrades_to_none() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    failure = RuntimeError("deterministic shutdown failure")

    async def execute(_grace_seconds: float) -> WorkerShutdownReceipt:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        raise failure

    harness = _CommitHarness(execute)
    first = asyncio.create_task(_commit(harness, grace_seconds=1.0))
    await started.wait()
    followers = tuple(asyncio.create_task(_commit(harness, grace_seconds=2.0)) for _ in range(3))
    release.set()
    results = await asyncio.gather(first, *followers, return_exceptions=True)

    assert calls == 1
    assert all(result is failure for result in results)
    with pytest.raises(RuntimeError) as captured:
        await _commit(harness, grace_seconds=3.0)
    assert captured.value is failure


@pytest.mark.asyncio
async def test_shutdown_delivery_arming_atomically_owns_commit_before_request_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _terminal_application()
    raw = cast(Any, application)
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    receipt = WorkerShutdownReceipt(True, True, True, True)
    teardown_calls = 0

    async def commit_once(
        _application: ProductionWorkerApplication,
        *,
        grace_seconds: float,
    ) -> WorkerShutdownReceipt:
        assert grace_seconds == 1.5
        commit_started.set()
        await release_commit.wait()
        return receipt

    async def teardown(_application: ProductionWorkerApplication) -> None:
        nonlocal teardown_calls
        teardown_calls += 1
        raw._stopped = True
        raw._shutdown_event.set()

    monkeypatch.setattr(ProductionWorkerApplication, "_commit_shutdown_once", commit_once)
    monkeypatch.setattr(ProductionWorkerApplication, "_run_transport_shutdown", teardown)

    async def transport_request() -> None:
        application.begin_shutdown_delivery()
        current = asyncio.current_task()
        assert current is not None
        # Deliver cancellation at the first suspension after delivery is
        # armed.  commit_shutdown must synchronously publish its shared Task
        # before that cancellation can escape this request waiter.
        asyncio.get_running_loop().call_soon(current.cancel)
        try:
            await application.commit_shutdown(grace_seconds=1.5)
        finally:
            application.finalize_shutdown_delivery()

    request = asyncio.create_task(transport_request())
    with pytest.raises(asyncio.CancelledError):
        await request

    commit_task = raw._shutdown_commit_task
    assert commit_task is not None
    assert commit_task.cancelled() is False
    await asyncio.wait_for(commit_started.wait(), timeout=1.0)
    assert commit_task.done() is False
    assert teardown_calls == 0

    release_commit.set()
    assert await commit_task is receipt
    await asyncio.wait_for(application.wait_stopped(), timeout=1.0)
    await _wait_for_terminal_tasks(application)

    assert teardown_calls == 1
    assert raw._fatal_error is None
    assert application.reject_new_runs


@pytest.mark.asyncio
async def test_shutdown_delivery_survives_lost_commit_waiter_and_tears_down_only_after_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _terminal_application()
    raw = cast(Any, application)
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    receipt = WorkerShutdownReceipt(True, True, True, True)
    teardown_calls = 0

    async def commit_once(
        _application: ProductionWorkerApplication,
        *,
        grace_seconds: float,
    ) -> WorkerShutdownReceipt:
        assert grace_seconds == 1.25
        commit_started.set()
        await release_commit.wait()
        return receipt

    async def teardown(_application: ProductionWorkerApplication) -> None:
        nonlocal teardown_calls
        teardown_calls += 1
        raw._stopped = True
        raw._shutdown_event.set()

    monkeypatch.setattr(ProductionWorkerApplication, "_commit_shutdown_once", commit_once)
    monkeypatch.setattr(ProductionWorkerApplication, "_run_transport_shutdown", teardown)

    application.begin_shutdown_delivery()
    waiter = asyncio.create_task(application.commit_shutdown(grace_seconds=1.25))
    await commit_started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release_commit.set()
    commit_task = raw._shutdown_commit_task
    assert commit_task is not None
    assert await commit_task is receipt
    assert teardown_calls == 0

    application.finalize_shutdown_delivery()
    await asyncio.wait_for(application.wait_stopped(), timeout=1.0)
    await _wait_for_terminal_tasks(application)

    assert teardown_calls == 1
    assert raw._fatal_error is None
    assert application.reject_new_runs


@pytest.mark.asyncio
async def test_shutdown_delivery_commit_failure_still_tears_down_and_fails_wait_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _terminal_application()
    raw = cast(Any, application)
    failure = RuntimeError("durable commit failed")
    teardown_calls = 0
    log_events: list[str] = []

    async def commit_once(
        _application: ProductionWorkerApplication,
        *,
        grace_seconds: float,
    ) -> WorkerShutdownReceipt:
        assert grace_seconds == 2.0
        raise failure

    async def teardown(_application: ProductionWorkerApplication) -> None:
        nonlocal teardown_calls
        teardown_calls += 1
        raw._stopped = True
        raw._shutdown_event.set()

    async def emit_runtime_log(
        _application: ProductionWorkerApplication,
        _level: object,
        event: str,
        _message: str,
        **_fields: object,
    ) -> None:
        log_events.append(event)

    monkeypatch.setattr(ProductionWorkerApplication, "_commit_shutdown_once", commit_once)
    monkeypatch.setattr(ProductionWorkerApplication, "_run_transport_shutdown", teardown)
    monkeypatch.setattr(ProductionWorkerApplication, "_emit_runtime_log", emit_runtime_log)

    application.begin_shutdown_delivery()
    with pytest.raises(RuntimeError) as captured:
        await application.commit_shutdown(grace_seconds=2.0)
    assert captured.value is failure
    assert teardown_calls == 0

    application.finalize_shutdown_delivery()
    with pytest.raises(ProductionWorkerError, match="commit failed after delivery began"):
        await asyncio.wait_for(application.wait_stopped(), timeout=1.0)
    await _wait_for_terminal_tasks(application)

    assert teardown_calls == 1
    assert application.reject_new_runs
    assert "runtime.shutdown_delivery_commit_failed" in log_events


@pytest.mark.asyncio
async def test_shutdown_delivery_unsafe_receipt_still_tears_down_and_fails_wait_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _terminal_application()
    raw = cast(Any, application)
    receipt = WorkerShutdownReceipt(True, False, True, True)
    teardown_calls = 0

    async def commit_once(
        _application: ProductionWorkerApplication,
        *,
        grace_seconds: float,
    ) -> WorkerShutdownReceipt:
        assert grace_seconds == 3.0
        return receipt

    async def teardown(_application: ProductionWorkerApplication) -> None:
        nonlocal teardown_calls
        teardown_calls += 1
        raw._stopped = True
        raw._shutdown_event.set()

    monkeypatch.setattr(ProductionWorkerApplication, "_commit_shutdown_once", commit_once)
    monkeypatch.setattr(ProductionWorkerApplication, "_run_transport_shutdown", teardown)

    application.begin_shutdown_delivery()
    assert await application.commit_shutdown(grace_seconds=3.0) is receipt
    assert teardown_calls == 0

    application.finalize_shutdown_delivery()
    with pytest.raises(ProductionWorkerError, match="did not prove durable interrupted state"):
        await asyncio.wait_for(application.wait_stopped(), timeout=1.0)
    await _wait_for_terminal_tasks(application)

    assert teardown_calls == 1
    assert application.reject_new_runs
