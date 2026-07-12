from __future__ import annotations

import pytest

from offeragent_harness.runtime.lifecycle import (
    InvalidLifecycleTransition,
    LifecycleMachine,
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
