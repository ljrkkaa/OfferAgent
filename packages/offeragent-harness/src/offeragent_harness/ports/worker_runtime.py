"""Worker-only composition boundary.

The Windows Host supervises a process.  It must never import the concrete
``HarnessApplication`` (or any of the stores, tools, models, or Vault ports
behind it).  The Worker executable crosses this small port exactly once to
obtain the one application composition root used by every local transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class WorkerBootstrap:
    """Worker-local bootstrap material that is never retained by the Host."""

    workspace_instance_id: str
    canonical_root: Path
    state_directory: Path
    diagnostic_stdio: bool = False


@runtime_checkable
class WorkerApplication(Protocol):
    @property
    def ready(self) -> bool: ...

    async def start(self) -> object: ...

    async def shutdown(self, *, grace_seconds: float = 10.0) -> None: ...


@runtime_checkable
class WorkerCompositionRoot(Protocol):
    """Build the unique application owned by one Worker process."""

    def build(self, bootstrap: WorkerBootstrap) -> WorkerApplication: ...


__all__ = ["WorkerApplication", "WorkerBootstrap", "WorkerCompositionRoot"]
