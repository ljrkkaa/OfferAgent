"""Bootstrap adapter from the path-bearing registry to the opaque Host DTO."""

from __future__ import annotations

from pathlib import Path

from offeragent_harness.runtime.host_supervisor import SupervisedWorkspaceIdentity

from .identity import WorkspaceRegistry


class WorkspaceRegistrationBoundary:
    """Resolve a real root outside the Host and discard its path immediately."""

    def __init__(self, registry: WorkspaceRegistry) -> None:
        self._registry = registry

    def register(
        self,
        root: Path,
        *,
        database_identity: str,
        portable_workspace_id: str | None = None,
    ) -> SupervisedWorkspaceIdentity:
        record = self._registry.register(root, portable_workspace_id=portable_workspace_id)
        return SupervisedWorkspaceIdentity(
            workspace_instance_id=record.workspace_instance_id,
            canonical_root_identity=record.root_identity.identity_hash,
            database_identity=database_identity,
        )


__all__ = ["WorkspaceRegistrationBoundary"]
