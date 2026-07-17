"""Process identities and Job Object contracts shared by the local Runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_WORKSPACE_ID_RE = re.compile(r"wsi_[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


@dataclass(frozen=True, slots=True)
class SupervisedWorkspaceIdentity:
    """Opaque workspace identity safe to pass to process-isolation backends."""

    workspace_instance_id: str
    canonical_root_identity: str
    database_identity: str

    def __post_init__(self) -> None:
        if not _WORKSPACE_ID_RE.fullmatch(self.workspace_instance_id):
            raise ValueError("workspace_instance_id must be a canonical wsi_<uuid>")
        if not _SHA256_RE.fullmatch(self.canonical_root_identity):
            raise ValueError("canonical_root_identity must be a lowercase sha256 digest")
        if not _SHA256_RE.fullmatch(self.database_identity):
            raise ValueError("database_identity must be a lowercase sha256 digest")

    @property
    def registry_key(self) -> tuple[str, str]:
        return self.workspace_instance_id, self.canonical_root_identity


class ManagedWorkerProcess(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def native_process_handle(self) -> int: ...

    def resume(self) -> None: ...

    def poll(self) -> int | None: ...

    async def wait(self) -> int: ...

    def terminate(self, exit_code: int) -> None: ...

    def close(self) -> None: ...


class WorkerJob(Protocol):
    @property
    def native_job_handle(self) -> int: ...

    def assign(self, process: ManagedWorkerProcess) -> None: ...

    def assign_process_handle(self, process_handle: int) -> None: ...

    def terminate_tree(self, exit_code: int) -> None: ...

    def contains_process_handle(self, process_handle: int) -> bool: ...

    async def wait_empty(self, timeout_seconds: float) -> bool: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerShutdownReceipt:
    new_runs_rejected: bool
    active_runs_cancelled: bool
    interrupted_state_persisted: bool
    worker_state_flushed: bool

    @property
    def safely_committed(self) -> bool:
        return all(
            (
                self.new_runs_rejected,
                self.active_runs_cancelled,
                self.interrupted_state_persisted,
                self.worker_state_flushed,
            )
        )


__all__ = [
    "ManagedWorkerProcess",
    "SupervisedWorkspaceIdentity",
    "WorkerJob",
    "WorkerShutdownReceipt",
]
