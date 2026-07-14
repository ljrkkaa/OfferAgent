from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from offeragent_harness.runtime.lifecycle import (
    HostLifecycle,
    InvalidLifecycleTransition,
    LifecycleMachine,
    LifecycleWaitTimeout,
    WorkerLifecycle,
)


@pytest.mark.asyncio
async def test_lifecycle_is_revision_guarded() -> None:
    machine = LifecycleMachine()
    started = await machine.transition(WorkerLifecycle.STARTING, expected_revision=0)
    assert started.revision == 1
    with pytest.raises(InvalidLifecycleTransition, match="revision changed"):
        await machine.transition(WorkerLifecycle.READY, expected_revision=0)


@pytest.mark.asyncio
async def test_lifecycle_rejects_invalid_transition() -> None:
    machine = LifecycleMachine()
    with pytest.raises(InvalidLifecycleTransition, match="cold -> busy"):
        await machine.transition(WorkerLifecycle.BUSY, expected_revision=0)


@pytest.mark.asyncio
async def test_host_and_worker_use_disjoint_transition_graphs_and_injected_clock() -> None:
    timestamp = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    host = LifecycleMachine.host(now=lambda: timestamp)

    assert host.snapshot.state is HostLifecycle.ABSENT
    started = await host.transition(HostLifecycle.STARTING, expected_revision=0, reason="user attach")
    assert started.changed_at == timestamp
    assert started.reason == "user attach"
    with pytest.raises(InvalidLifecycleTransition, match="starting -> restarting"):
        await host.transition(HostLifecycle.RESTARTING, expected_revision=1)
    with pytest.raises(InvalidLifecycleTransition, match="starting -> busy"):
        await host.transition(WorkerLifecycle.BUSY, expected_revision=1)


@pytest.mark.asyncio
async def test_waiter_observes_only_a_new_matching_revision() -> None:
    machine = LifecycleMachine.worker()
    waiter = asyncio.create_task(
        machine.wait_for(
            frozenset({WorkerLifecycle.READY}),
            after_revision=0,
            timeout_seconds=1,
        )
    )
    await asyncio.sleep(0)
    await machine.transition(WorkerLifecycle.STARTING, expected_revision=0)
    assert waiter.done() is False
    ready = await machine.transition(WorkerLifecycle.READY, expected_revision=1)

    assert await waiter == ready


@pytest.mark.asyncio
async def test_waiter_timeout_is_typed_and_does_not_mutate_state() -> None:
    machine = LifecycleMachine.worker()
    with pytest.raises(LifecycleWaitTimeout, match="deadline"):
        await machine.wait_for(frozenset({WorkerLifecycle.READY}), timeout_seconds=0.01)
    assert machine.snapshot.revision == 0


def test_lifecycle_rejects_naive_clock_and_cross_enum_graph() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        LifecycleMachine.worker(now=lambda: datetime(2026, 7, 12))
    with pytest.raises(ValueError, match="transition graph"):
        LifecycleMachine(WorkerLifecycle.COLD, transitions={WorkerLifecycle.COLD: frozenset()})


@pytest.mark.asyncio
async def test_lifecycle_rejects_unbounded_reason() -> None:
    machine = LifecycleMachine.worker()
    with pytest.raises(ValueError, match="reason"):
        await machine.transition(WorkerLifecycle.STARTING, expected_revision=0, reason="x" * 2_049)
    assert machine.snapshot.revision == 0


@pytest.mark.asyncio
async def test_lifecycle_rejects_a_backwards_audit_clock() -> None:
    initial = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    values = iter((initial, initial - timedelta(seconds=1)))
    machine = LifecycleMachine.worker(now=lambda: next(values))
    with pytest.raises(ValueError, match="backwards"):
        await machine.transition(WorkerLifecycle.STARTING, expected_revision=0)
    assert machine.snapshot.changed_at == initial
    assert machine.snapshot.revision == 0
