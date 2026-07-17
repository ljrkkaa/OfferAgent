"""Adapter from the path-bearing registry to an opaque process identity."""

from __future__ import annotations

from pathlib import Path

from offeragent_harness.runtime.process_identity import SupervisedWorkspaceIdentity

from .identity import WorkspaceRegistry


class WorkspaceRegistrationBoundary:
    """Resolve a real root before crossing the process-isolation boundary."""

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
