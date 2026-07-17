from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from offeragent_harness.ports.worker_runtime import WorkerApplication, WorkerBootstrap
from offeragent_harness.runtime.worker_entrypoint import (
    WorkerEntrypoint,
    WorkerEntrypointError,
    WorkerTransportMode,
)


@dataclass
class FakeApplication:
    ready: bool = False
    starts: int = 0
    shutdowns: int = 0

    async def start(self) -> object:
        self.starts += 1
        self.ready = True
        return object()

    async def shutdown(self, *, grace_seconds: float = 10.0) -> None:
        assert grace_seconds > 0
        self.shutdowns += 1
        self.ready = False


@dataclass
class FakeCompositionRoot:
    application: FakeApplication
    builds: int = 0

    def build(self, bootstrap: WorkerBootstrap) -> WorkerApplication:
        assert bootstrap.workspace_instance_id.startswith("wsi_")
        self.builds += 1
        return self.application


def bootstrap() -> WorkerBootstrap:
    return WorkerBootstrap(
        "wsi_12345678-1234-4234-8234-123456789abc",
        Path(r"C:\Vault"),
        Path(r"C:\Users\user\AppData\Local\OfferAgent\workspaces\id"),
    )


@pytest.mark.asyncio
async def test_worker_consumes_exactly_one_composition_root_for_direct_stdio() -> None:
    application = FakeApplication()
    root = FakeCompositionRoot(application)
    entrypoint = WorkerEntrypoint(root)

    assert await entrypoint.start(bootstrap()) is application
    assert application.ready
    assert root.builds == 1
    with pytest.raises(WorkerEntrypointError, match="already consumed"):
        await entrypoint.start(bootstrap())

    await entrypoint.shutdown()
    assert application.shutdowns == 1


@pytest.mark.asyncio
async def test_worker_rejects_every_transport_other_than_direct_stdio() -> None:
    entrypoint = WorkerEntrypoint(FakeCompositionRoot(FakeApplication()))
    with pytest.raises(WorkerEntrypointError, match="unsupported Worker transport"):
        await entrypoint.start(bootstrap(), transport=cast(WorkerTransportMode, "loopback"))
