"""Worker executable boundary without a second Agent runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum

from offeragent_harness.ports.worker_runtime import (
    WorkerApplication,
    WorkerBootstrap,
    WorkerCompositionRoot,
)


class WorkerTransportMode(str, Enum):
    STDIO = "stdio"


class WorkerEntrypointError(RuntimeError):
    pass


@dataclass(slots=True)
class WorkerEntrypoint:
    """Construct and start one application, once, inside a Worker process."""

    composition_root: WorkerCompositionRoot
    _application: WorkerApplication | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def start(
        self,
        bootstrap: WorkerBootstrap,
        *,
        transport: WorkerTransportMode = WorkerTransportMode.STDIO,
    ) -> WorkerApplication:
        if transport is not WorkerTransportMode.STDIO:
            raise WorkerEntrypointError("unsupported Worker transport")
        async with self._lock:
            if self._application is not None:
                raise WorkerEntrypointError("Worker application composition root was already consumed")
            application = self.composition_root.build(bootstrap)
            self._application = application
            try:
                await application.start()
            except BaseException:
                await application.shutdown()
                raise
            if not application.ready:
                await application.shutdown()
                raise WorkerEntrypointError("composition root returned before the application was ready")
            return application

    async def shutdown(self, *, grace_seconds: float = 10.0) -> None:
        async with self._lock:
            application = self._application
            self._application = None
        if application is not None:
            await application.shutdown(grace_seconds=grace_seconds)


__all__ = ["WorkerEntrypoint", "WorkerEntrypointError", "WorkerTransportMode"]
