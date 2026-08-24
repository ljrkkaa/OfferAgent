"""Adapters binding Subagent ports to the existing Harness and CancellationScope."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from offeragent_harness.agent.state import RunControlMessage
from offeragent_harness.ports.cancellation import CancellationReasonLike, CancellationToken
from offeragent_harness.ports.events import NewEvent
from offeragent_harness.ports.subagents import (
    ChildCancellationSource,
    ChildRunControlMessage,
    ChildRunExecution,
    RootCancellationRegistry,
    SubagentEventFactory,
)
from offeragent_harness.protocol.events import make_domain_event_record
from offeragent_harness.subagents.models import SubagentResult

from .cancellation import CancellationCode, CancellationReason, CancellationScope
from .harness_service import HarnessService
from .turn_manager import RunControlInbox


class ProtocolSubagentEventFactory(SubagentEventFactory):
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
    ) -> NewEvent:
        domain = make_domain_event_record(
            event_type=event_type,
            payload=payload,
            trace_id=trace_id,
            workspace_id=workspace_id,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            root_run_id=root_run_id,
            parent_run_id=parent_run_id,
            state_revision=state_revision,
        )
        identity = f"{run_id}:{sequence}:{event_type}:{state_revision}".encode()
        return NewEvent(
            f"evt_{hashlib.sha256(identity).hexdigest()}",
            event_type,
            domain.to_wire(),
            occurred_at,
            terminal,
            f"{run_id}:{sequence}:{event_type}",
        )


class HarnessSubagentRunExecutor:
    def __init__(self, harness: HarnessService) -> None:
        self._harness = harness
        self._controls: dict[str, RunControlInbox] = {}

    async def execute(self, execution: ChildRunExecution, cancellation: CancellationToken) -> SubagentResult:
        if not isinstance(cancellation, _RegisteredCancellationSource):
            raise TypeError("Harness child execution requires the Worker CancellationScope adapter")
        controls = RunControlInbox()
        for message in execution.control_messages:
            await controls.offer(_control_message(message))
        self._controls[execution.record.run_id] = controls
        try:
            return await self._harness.execute_child_agent(execution, cancellation.scope, controls)
        finally:
            self._controls.pop(execution.record.run_id, None)

    async def deliver_message(self, run_id: str, message: ChildRunControlMessage) -> bool:
        controls = self._controls.get(run_id)
        return False if controls is None else await controls.offer(_control_message(message))


class _RegisteredCancellationSource:
    def __init__(
        self,
        factory: HarnessChildCancellationFactory,
        run_id: str,
        scope: CancellationScope,
    ) -> None:
        self._factory = factory
        self._run_id = run_id
        self.scope = scope

    @property
    def cancelled(self) -> bool:
        return self.scope.cancelled

    @property
    def reason(self) -> CancellationReason | None:
        return self.scope.reason

    async def wait(self) -> CancellationReason:
        return await self.scope.wait()

    def checkpoint(self) -> None:
        self.scope.checkpoint()

    async def cancel(self, reason: CancellationReasonLike) -> bool:
        code = _cancellation_code(reason.code.value)
        return await self.scope.cancel(CancellationReason(code, reason.message, reason.requested_at))

    async def close(self) -> None:
        self._factory._remove(self._run_id, self)
        await self.scope.close()


class HarnessChildCancellationFactory(RootCancellationRegistry):
    """Creates real hierarchical scopes; never starts another Runtime."""

    def __init__(self) -> None:
        self._sources: dict[str, CancellationToken] = {}
        self._root_by_run: dict[str, str] = {}
        self._retired_roots: set[str] = set()

    def register_root(self, run_id: str, source: CancellationToken) -> None:
        if run_id in self._sources:
            raise RuntimeError(f"cancellation root {run_id!r} is already registered")
        self._sources[run_id] = source
        self._root_by_run[run_id] = run_id

    def unregister_root(self, run_id: str) -> None:
        if any(root == run_id and child != run_id for child, root in self._root_by_run.items()):
            self._retired_roots.add(run_id)
            return
        self._sources.pop(run_id, None)
        self._root_by_run.pop(run_id, None)

    def create(self, *, root_run_id: str, parent_run_id: str, child_run_id: str) -> ChildCancellationSource:
        if child_run_id in self._sources:
            raise RuntimeError(f"child cancellation source {child_run_id!r} already exists")
        parent = self._sources.get(parent_run_id)
        if parent is None:
            raise RuntimeError(f"parent cancellation source {parent_run_id!r} is not registered")
        parent_scope = parent.scope if isinstance(parent, _RegisteredCancellationSource) else parent
        if not isinstance(parent_scope, CancellationScope):
            raise TypeError("registered root cancellation source must be the Worker CancellationScope")
        scope = parent_scope.child(f"subagent:{child_run_id}")
        source = _RegisteredCancellationSource(self, child_run_id, scope)
        self._sources[child_run_id] = source
        if root_run_id not in self._sources:
            # The parent may be a recovered child, but its root must remain
            # registered for tree ownership/audit.
            raise RuntimeError(f"root cancellation source {root_run_id!r} is not registered")
        self._root_by_run[child_run_id] = root_run_id
        return source

    async def cleanup(self, run_id: str) -> None:
        """Release any stale in-process child ownership before recovery requeues it.

        Windows Job ownership terminates process descendants when a Worker dies;
        this registry owns the remaining cancellation scope in same-process
        recovery and therefore must retire it before a replacement is created.
        """

        source = self._sources.get(run_id)
        if not isinstance(source, _RegisteredCancellationSource):
            return
        await source.cancel(
            CancellationReason.now(
                CancellationCode.PARENT,
                "Subagent ownership is being recovered",
            )
        )
        await source.close()

    def _remove(self, run_id: str, source: _RegisteredCancellationSource) -> None:
        if self._sources.get(run_id) is source:
            self._sources.pop(run_id, None)
        root = self._root_by_run.pop(run_id, None)
        if (
            root is not None
            and root in self._retired_roots
            and not any(owner == root and child != root for child, owner in self._root_by_run.items())
        ):
            self._sources.pop(root, None)
            self._root_by_run.pop(root, None)
            self._retired_roots.discard(root)


def _cancellation_code(value: str) -> CancellationCode:
    mapping = {
        "user": CancellationCode.USER,
        "parent": CancellationCode.PARENT,
        "deadline": CancellationCode.DEADLINE,
        "shutdown": CancellationCode.SHUTDOWN,
        "policy": CancellationCode.POLICY,
    }
    return mapping.get(value, CancellationCode.PARENT)


def _control_message(value: ChildRunControlMessage) -> RunControlMessage:
    artifact_note = "" if not value.artifact_ids else f"\nAuthorized Artifacts: {', '.join(value.artifact_ids)}"
    return RunControlMessage(
        value.message_id,
        (
            {
                "type": "text",
                "text": value.message + artifact_note,
                "format": "plain",
                "references": [],
            },
        ),
        value.mode,
        value.apply_after_sequence,
    )


__all__ = [
    "HarnessChildCancellationFactory",
    "HarnessSubagentRunExecutor",
    "ProtocolSubagentEventFactory",
]
