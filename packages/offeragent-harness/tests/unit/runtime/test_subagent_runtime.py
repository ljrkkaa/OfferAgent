from __future__ import annotations

import pytest

from offeragent_harness.runtime.cancellation import CancellationCode, CancellationReason, CancellationScope
from offeragent_harness.runtime.subagent_runtime import HarnessChildCancellationFactory


@pytest.mark.asyncio
async def test_child_cancellation_is_hierarchical_and_child_failure_is_isolated() -> None:
    factory = HarnessChildCancellationFactory()
    root = CancellationScope(name="root")
    factory.register_root("run_root", root)
    first = factory.create(root_run_id="run_root", parent_run_id="run_root", child_run_id="run_first")
    second = factory.create(root_run_id="run_root", parent_run_id="run_root", child_run_id="run_second")
    grandchild = factory.create(
        root_run_id="run_root",
        parent_run_id="run_first",
        child_run_id="run_grandchild",
    )

    await first.cancel(CancellationReason.now(CancellationCode.USER, "stop one branch"))
    assert first.cancelled and grandchild.cancelled
    assert not root.cancelled and not second.cancelled

    await root.cancel(CancellationReason.now(CancellationCode.USER, "stop tree"))
    assert second.cancelled
    factory.unregister_root("run_root")
    await grandchild.close()
    await first.close()
    await second.close()
    await root.close()


@pytest.mark.asyncio
async def test_retired_session_root_remains_registered_until_last_child_closes() -> None:
    factory = HarnessChildCancellationFactory()
    root = CancellationScope(name="root")
    factory.register_root("run_root", root)
    child = factory.create(root_run_id="run_root", parent_run_id="run_root", child_run_id="run_child")
    factory.unregister_root("run_root")
    grandchild = factory.create(
        root_run_id="run_root",
        parent_run_id="run_child",
        child_run_id="run_grandchild",
    )
    await grandchild.close()
    await child.close()
    with pytest.raises(RuntimeError, match="not registered"):
        factory.create(root_run_id="run_root", parent_run_id="run_root", child_run_id="run_late")
    await root.close()
