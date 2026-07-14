"""Obsidian bridge reverse-tool boundary."""

from __future__ import annotations

import re
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from offeragent_harness.tools import ToolCall, ToolResult

from .cancellation import CancellationToken


@dataclass(frozen=True)
class ClientToolInvocation:
    invocation_id: str
    call: ToolCall
    deadline: datetime

    def __post_init__(self) -> None:
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise ValueError("client invocation deadline must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ClientToolPathState:
    path: str
    before_hash: str
    after_hash: str
    unsaved_editor: bool
    open_editor: bool

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("Client Tool path state requires a path")
        for value in (self.before_hash, self.after_hash):
            if value != "absent" and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
                raise ValueError("Client Tool path state requires canonical content hashes")
        if self.unsaved_editor and not self.open_editor:
            raise ValueError("an unsaved Client Tool editor must also be open")


@dataclass(frozen=True, slots=True)
class ClientToolPreview:
    invocation_id: str
    tool_call_id: str
    state_hash: str
    after_state_hash: str
    paths: tuple[str, ...]
    diff: bytes
    diff_sha256: str
    has_unsaved_editors: bool
    has_open_editors: bool
    path_states: tuple[ClientToolPathState, ...]

    def __post_init__(self) -> None:
        if not self.invocation_id or not self.tool_call_id or not self.paths or not self.diff:
            raise ValueError("Client Tool preview is incomplete")
        object.__setattr__(self, "paths", tuple(self.paths))
        object.__setattr__(self, "diff", bytes(self.diff))
        path_states = tuple(self.path_states)
        if tuple(item.path for item in path_states) != tuple(self.paths):
            raise ValueError("Client Tool preview path states must exactly match the ordered path list")
        if self.has_unsaved_editors != any(item.unsaved_editor for item in path_states):
            raise ValueError("Client Tool preview unsaved-editor aggregate is inconsistent")
        if self.has_open_editors != any(item.open_editor for item in path_states):
            raise ValueError("Client Tool preview open-editor aggregate is inconsistent")
        object.__setattr__(self, "path_states", path_states)


@dataclass(frozen=True, slots=True)
class ClientToolCommitPathState:
    path: str
    observed_hash: str
    unsaved_editor: bool
    open_editor: bool

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("Client Tool committed path state requires a path")
        if self.observed_hash != "absent" and re.fullmatch(r"sha256:[0-9a-f]{64}", self.observed_hash) is None:
            raise ValueError("Client Tool committed path state requires a canonical content hash")
        if self.unsaved_editor and not self.open_editor:
            raise ValueError("an unsaved Client Tool editor must also be open")


@dataclass(frozen=True, slots=True)
class ClientToolCommitObservation:
    invocation_id: str
    tool_call_id: str
    paths: tuple[str, ...]
    has_unsaved_editors: bool
    has_open_editors: bool
    path_states: tuple[ClientToolCommitPathState, ...]

    def __post_init__(self) -> None:
        if not self.invocation_id or not self.tool_call_id or not self.paths:
            raise ValueError("Client Tool commit observation is incomplete")
        object.__setattr__(self, "paths", tuple(self.paths))
        path_states = tuple(self.path_states)
        if tuple(item.path for item in path_states) != tuple(self.paths):
            raise ValueError("Client Tool commit observation states must exactly match its ordered paths")
        if self.has_unsaved_editors != any(item.unsaved_editor for item in path_states):
            raise ValueError("Client Tool commit observation unsaved-editor aggregate is inconsistent")
        if self.has_open_editors != any(item.open_editor for item in path_states):
            raise ValueError("Client Tool commit observation open-editor aggregate is inconsistent")
        object.__setattr__(self, "path_states", path_states)


@runtime_checkable
class ClientToolPreviewPort(Protocol):
    async def preview(
        self,
        invocation: ClientToolInvocation,
        cancellation: CancellationToken,
    ) -> ClientToolPreview: ...


@runtime_checkable
class ClientToolCommitObservationPort(ClientToolPreviewPort, Protocol):
    async def observe_commit(
        self,
        invocation: ClientToolInvocation,
        paths: tuple[str, ...],
        cancellation: CancellationToken,
    ) -> ClientToolCommitObservation: ...


@runtime_checkable
class ClientToolPreviewLeasePort(ClientToolCommitObservationPort, Protocol):
    """Pin one authenticated client authority across a local commit boundary."""

    def hold_connection(self) -> AbstractAsyncContextManager[ClientToolCommitObservationPort]: ...


@runtime_checkable
class ClientToolPort(Protocol):
    async def invoke(self, invocation: ClientToolInvocation, cancellation: CancellationToken) -> ToolResult: ...

    async def cancel(self, invocation_id: str, reason: str) -> None: ...

    async def lookup_result(self, invocation_id: str, *, run_id: str | None = None) -> ToolResult | None: ...


__all__ = [
    "ClientToolCommitObservation",
    "ClientToolCommitObservationPort",
    "ClientToolCommitPathState",
    "ClientToolInvocation",
    "ClientToolPathState",
    "ClientToolPort",
    "ClientToolPreview",
    "ClientToolPreviewLeasePort",
    "ClientToolPreviewPort",
]
