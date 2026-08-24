"""Ports that keep Subagent scheduling inside the one local Harness runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from offeragent_harness.permissions import CapabilityScope, PermissionMode
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.subagents.models import (
    AgentBudget,
    ContextSnapshot,
    EffectiveToolScope,
    SubagentResult,
    SubagentRunRecord,
)
from offeragent_harness.tools import ToolDefinition

from .artifacts import ArtifactMetadata
from .cancellation import CancellationReasonLike, CancellationToken
from .events import NewEvent


@dataclass(frozen=True, slots=True)
class ParentRunAuthority:
    workspace_id: str
    session_id: str
    turn_id: str
    lineage: AgentLineage
    permission_mode: PermissionMode
    effective_scope: CapabilityScope
    tool_definitions: tuple[ToolDefinition, ...]
    registry_snapshot_hash: str
    remaining_budget: AgentBudget
    deadline_at: datetime
    context: Mapping[str, Any]
    run_config: Mapping[str, Any]
    can_spawn_children: bool
    active: bool

    def __post_init__(self) -> None:
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise ValueError("parent authority deadline must be timezone-aware")
        if not self.registry_snapshot_hash.startswith("sha256:"):
            raise ValueError("parent authority requires a Registry snapshot hash")


@dataclass(frozen=True, slots=True)
class ChildRunExecution:
    record: SubagentRunRecord
    context: ContextSnapshot
    tool_scope: EffectiveToolScope
    run_config: Mapping[str, Any]
    run_entity_revision: int
    state_entity_revision: int
    event_sequence: int
    trace_id: str
    control_messages: tuple[ChildRunControlMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class ChildRunControlMessage:
    message_id: str
    mode: str
    message: str
    artifact_ids: tuple[str, ...]
    apply_after_sequence: int


@dataclass(frozen=True, slots=True)
class StoredSubagentResultArtifact:
    metadata: ArtifactMetadata


@runtime_checkable
class ParentRunAuthorityProvider(Protocol):
    async def authority_for(self, run_id: str) -> ParentRunAuthority: ...


@runtime_checkable
class SubagentRunExecutor(Protocol):
    """Must invoke the Worker's existing HarnessService/Agent Loop."""

    async def execute(
        self,
        execution: ChildRunExecution,
        cancellation: CancellationToken,
    ) -> SubagentResult: ...

    async def deliver_message(self, run_id: str, message: ChildRunControlMessage) -> bool: ...


@runtime_checkable
class ChildCancellationSource(CancellationToken, Protocol):
    async def cancel(self, reason: CancellationReasonLike) -> bool: ...

    async def close(self) -> None: ...


@runtime_checkable
class ChildCancellationFactory(Protocol):
    def create(self, *, root_run_id: str, parent_run_id: str, child_run_id: str) -> ChildCancellationSource: ...


@runtime_checkable
class RootCancellationRegistry(ChildCancellationFactory, Protocol):
    def register_root(self, run_id: str, source: CancellationToken) -> None: ...

    def unregister_root(self, run_id: str) -> None: ...


@runtime_checkable
class SubagentResultArtifactWriter(Protocol):
    async def store(
        self,
        record: SubagentRunRecord,
        result: SubagentResult,
        cancellation: CancellationToken,
    ) -> StoredSubagentResultArtifact: ...


@runtime_checkable
class SubagentOwnershipCleaner(Protocol):
    async def cleanup(self, run_id: str) -> None: ...


@runtime_checkable
class SubagentTreeController(Protocol):
    async def cancel_descendants(self, parent_run_id: str, reason: str) -> tuple[str, ...]: ...

    async def parent_finished(self, parent_run_id: str, *, turn_finished: bool, reason: str) -> tuple[str, ...]: ...


@runtime_checkable
class SubagentEventFactory(Protocol):
    """Runtime adapter that binds domain child events to the wire protocol."""

    def make(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        trace_id: str,
        workspace_id: str,
        session_id: str,
        turn_id: str,
        run_id: str,
        root_run_id: str,
        parent_run_id: str,
        state_revision: int,
        sequence: int,
        occurred_at: datetime,
        terminal: bool,
    ) -> NewEvent: ...


__all__ = [
    "ChildCancellationFactory",
    "ChildCancellationSource",
    "ChildRunControlMessage",
    "ChildRunExecution",
    "ParentRunAuthority",
    "ParentRunAuthorityProvider",
    "RootCancellationRegistry",
    "StoredSubagentResultArtifact",
    "SubagentEventFactory",
    "SubagentOwnershipCleaner",
    "SubagentResultArtifactWriter",
    "SubagentRunExecutor",
    "SubagentTreeController",
]
