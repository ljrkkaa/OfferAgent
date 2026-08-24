from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.hooks import (
    HookDefinition,
    HookEvent,
    HookExecutionContext,
    HookImplementation,
    HookInvocation,
    HookLayer,
    HookOutput,
    HookScope,
)
from offeragent_harness.ports import SupervisedProcessResult
from offeragent_harness.runtime.hook_service import HookHandlerRegistry, HookService, StaticHookLayerSource
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    ManualCancellationToken,
    ManualClock,
    RecordingEventSink,
)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


class Handler:
    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, definition: Any, invocation: Any, cancellation: Any) -> HookOutput:
        del definition, invocation
        cancellation.checkpoint()
        self.calls += 1
        return HookOutput(audit_tags=("sqlite",))


class NoProcess:
    async def execute(self, request: Any, cancellation: Any) -> SupervisedProcessResult:
        del request, cancellation
        raise AssertionError("builtin Hook must not enter ProcessSupervisor")


def _layer() -> HookLayer:
    return HookLayer(
        HookScope.USER,
        "profile-1",
        1,
        (
            HookDefinition(
                "sqlite-hook",
                HookScope.USER,
                "profile-1",
                HookEvent.TURN_START,
                HookImplementation.BUILTIN,
                handler_id="sqlite-hook",
            ),
        ),
    )


def _invocation() -> HookInvocation:
    return HookInvocation(
        "turn-start:run-1",
        "agent:run-1",
        HookEvent.TURN_START,
        HookExecutionContext("system", "profile-1", "workspace-1", "session-1", True),
        "run-1",
        {"phase": "created"},
    )


def _service(database: Path, handler: Handler, sink: RecordingEventSink) -> HookService:
    return HookService(
        layers=StaticHookLayerSource((_layer(),)),
        handlers=HookHandlerRegistry({"sqlite-hook": handler}),
        process_supervisor=NoProcess(),
        unit_of_work=SqliteUnitOfWorkFactory(database, busy_timeout_ms=2_000),
        event_sink=sink,
        clock=ManualClock(NOW),
        ids=DeterministicIdGenerator(),
    )


@pytest.mark.asyncio
async def test_hook_receipt_audit_and_event_survive_sqlite_reopen_without_rerun(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    first_handler = Handler()
    first_sink = RecordingEventSink()
    first = _service(database, first_handler, first_sink)

    initial = await first.invoke(_invocation(), ManualCancellationToken())

    second_handler = Handler()
    replay_sink = RecordingEventSink()
    reopened = _service(database, second_handler, replay_sink)
    replay = await reopened.invoke(_invocation(), ManualCancellationToken())
    factory = SqliteUnitOfWorkFactory(database, busy_timeout_ms=2_000)
    async with factory.begin() as uow:
        audits = await uow.entities.list("hook_audits")
        events = await uow.events.read("hooks:workspace-1:session-1")

    assert not initial.replayed and replay.replayed
    assert first_handler.calls == 1 and second_handler.calls == 0
    assert len(audits) == len(events) == 1
    assert events[0].event_type == "hook.evaluated"
    assert replay_sink.events == [events[0]]
