from __future__ import annotations

from datetime import datetime, timezone

import pytest

from offeragent_harness.ports import JournalState, NewEvent, TerminalEventConflict
from offeragent_harness.testing import InMemoryUnitOfWorkFactory
from offeragent_harness.tools import ToolResult, ToolResultStatus

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def started_event() -> NewEvent:
    return NewEvent("evt_1", "turn.started", {"runId": "run_1"}, NOW, False, "evt-key-1")


def terminal_event(event_id: str = "evt_2", key: str = "evt-key-2") -> NewEvent:
    return NewEvent(event_id, "turn.completed", {"runId": "run_1"}, NOW, True, key)


def success_result() -> ToolResult:
    return ToolResult(
        tool_call_id="call_1",
        status=ToolResultStatus.SUCCEEDED,
        data={"afterHash": "sha256:" + "a" * 64},
        user_visible_summary="write committed",
        artifact_ids=(),
        source_refs=(),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state={"hash": "sha256:" + "a" * 64},
        error=None,
    )


@pytest.mark.asyncio
async def test_entity_event_and_journal_commit_together() -> None:
    factory = InMemoryUnitOfWorkFactory()
    result = success_result()
    async with factory.begin() as uow:
        revision = await uow.entities.put("runs", "run_1", {"status": "completed"}, expected_revision=0)
        await uow.events.append("run_1", 0, (started_event(), terminal_event()))
        await uow.journal.start("ws_1", "idem_1", "request-hash", NOW)
        await uow.journal.complete("ws_1", "idem_1", "request-hash", result, NOW)
        await uow.commit()

    assert revision == 1
    assert await factory.get_entity("runs", "run_1") == {"status": "completed"}
    assert [item.event_type for item in await factory.event_store.read("run_1")] == [
        "turn.started",
        "turn.completed",
    ]
    record = await factory.get_journal("ws_1", "idem_1")
    assert record is not None
    assert record.state is JournalState.COMPLETED
    assert record.result == result


@pytest.mark.asyncio
async def test_exception_before_commit_rolls_back_every_participant() -> None:
    factory = InMemoryUnitOfWorkFactory()
    with pytest.raises(RuntimeError, match="fault injection"):
        async with factory.begin() as uow:
            await uow.entities.put("runs", "run_1", {"status": "completed"}, expected_revision=0)
            await uow.events.append("run_1", 0, (started_event(),))
            await uow.journal.start("ws_1", "idem_1", "request-hash", NOW)
            raise RuntimeError("fault injection")

    assert await factory.get_entity("runs", "run_1") is None
    assert await factory.event_store.read("run_1") == ()
    assert await factory.get_journal("ws_1", "idem_1") is None


@pytest.mark.asyncio
async def test_late_event_failure_does_not_leave_earlier_entity_or_journal_changes() -> None:
    factory = InMemoryUnitOfWorkFactory()
    async with factory.begin() as uow:
        await uow.events.append("run_1", 0, (terminal_event(),))
        await uow.commit()

    with pytest.raises(TerminalEventConflict):
        async with factory.begin() as uow:
            await uow.entities.put("runs", "run_1", {"status": "completed"}, expected_revision=0)
            await uow.journal.start("ws_1", "idem_1", "request-hash", NOW)
            await uow.events.append("run_1", 1, (started_event(),))
            await uow.commit()

    assert await factory.get_entity("runs", "run_1") is None
    assert await factory.get_journal("ws_1", "idem_1") is None
    assert len(await factory.event_store.read("run_1")) == 1
