"""Worker-only composition boundary for the direct stdio process."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class WorkerBootstrap:
    """Worker-local bootstrap material derived from the selected Vault."""

    workspace_instance_id: str
    canonical_root: Path
    state_directory: Path
    plugin_journal_directory: Path | None = None
    plugin_recovery_token: str | None = None


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
