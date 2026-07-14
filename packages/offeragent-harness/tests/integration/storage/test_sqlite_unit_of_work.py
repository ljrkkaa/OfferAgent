from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.ports import EntityRevisionConflict, JournalState, NewEvent, TerminalEventConflict
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
async def test_entity_event_and_journal_commit_in_one_real_sqlite_transaction(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    factory = SqliteUnitOfWorkFactory(database_path)
    expected_result = success_result()

    async with factory.begin() as unit_of_work:
        revision = await unit_of_work.entities.put(
            "test_entities", "run_1", {"status": "completed"}, expected_revision=0
        )
        await unit_of_work.events.append("run_1", 0, (started_event(), terminal_event()))
        await unit_of_work.journal.start("ws_1", "idem_1", "request-hash", NOW)
        await unit_of_work.journal.complete("ws_1", "idem_1", "request-hash", expected_result, NOW)
        await unit_of_work.commit()

    reopened = SqliteUnitOfWorkFactory(database_path)
    assert revision == 1
    assert await reopened.get_entity("test_entities", "run_1") == {"status": "completed"}
    assert [item.event_type for item in await reopened.event_store.read("run_1")] == [
        "turn.started",
        "turn.completed",
    ]
    record = await reopened.get_journal("ws_1", "idem_1")
    assert record is not None
    assert record.state is JournalState.COMPLETED
    assert record.result == expected_result


@pytest.mark.asyncio
async def test_exception_or_missing_commit_rolls_back_every_participant(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "state.sqlite")

    with pytest.raises(RuntimeError, match="fault injection"):
        async with factory.begin() as unit_of_work:
            await unit_of_work.entities.put("test_entities", "run_1", {"status": "completed"}, expected_revision=0)
            await unit_of_work.events.append("run_1", 0, (started_event(),))
            await unit_of_work.journal.start("ws_1", "idem_1", "request-hash", NOW)
            raise RuntimeError("fault injection")

    assert await factory.get_entity("test_entities", "run_1") is None
    assert await factory.event_store.read("run_1") == ()
    assert await factory.get_journal("ws_1", "idem_1") is None

    async with factory.begin() as unit_of_work:
        await unit_of_work.entities.put("test_entities", "run_2", {"status": "created"}, expected_revision=0)
        # Deliberately omit commit.
    assert await factory.get_entity("test_entities", "run_2") is None


@pytest.mark.asyncio
async def test_late_event_failure_rolls_back_earlier_entity_and_journal_changes(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "state.sqlite")
    async with factory.begin() as unit_of_work:
        await unit_of_work.events.append("run_1", 0, (terminal_event(),))
        await unit_of_work.commit()

    with pytest.raises(TerminalEventConflict):
        async with factory.begin() as unit_of_work:
            await unit_of_work.entities.put("test_entities", "run_1", {"status": "completed"}, expected_revision=0)
            await unit_of_work.journal.start("ws_1", "idem_1", "request-hash", NOW)
            await unit_of_work.events.append("run_1", 1, (started_event(),))
            await unit_of_work.commit()

    assert await factory.get_entity("test_entities", "run_1") is None
    assert await factory.get_journal("ws_1", "idem_1") is None
    assert len(await factory.event_store.read("run_1")) == 1


@pytest.mark.asyncio
async def test_contending_sqlite_writer_does_not_block_the_asyncio_event_loop(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    first_factory = SqliteUnitOfWorkFactory(database_path, busy_timeout_ms=2_000)
    second_factory = SqliteUnitOfWorkFactory(database_path, busy_timeout_ms=2_000)
    contender_started = asyncio.Event()

    async def contend() -> None:
        contender_started.set()
        async with second_factory.begin() as unit_of_work:
            await unit_of_work.entities.put("test_entities", "run_1", {"owner": "second"}, expected_revision=0)
            await unit_of_work.commit()

    async with first_factory.begin() as first:
        await first.entities.put("test_entities", "run_1", {"owner": "first"}, expected_revision=0)
        contender = asyncio.create_task(contend())
        await contender_started.wait()

        # This callback runs while the second connection is blocked in BEGIN IMMEDIATE.
        loop_progressed = asyncio.Event()
        asyncio.get_running_loop().call_soon(loop_progressed.set)
        await asyncio.wait_for(loop_progressed.wait(), timeout=1)
        assert not contender.done()
        await first.commit()

    with pytest.raises(EntityRevisionConflict):
        await contender
    assert await first_factory.get_entity("test_entities", "run_1") == {"owner": "first"}


@pytest.mark.asyncio
async def test_wal_reader_observes_committed_snapshot_while_writer_is_open(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    writer_factory = SqliteUnitOfWorkFactory(database_path)
    reader_factory = SqliteUnitOfWorkFactory(database_path)
    await writer_factory.entity_store.put("test_entities", "run_1", {"revision": 1}, expected_revision=0)

    async with writer_factory.begin() as writer:
        await writer.entities.put("test_entities", "run_1", {"revision": 2}, expected_revision=1)
        assert await asyncio.wait_for(reader_factory.get_entity("test_entities", "run_1"), timeout=1) == {"revision": 1}
        await writer.commit()

    assert await reader_factory.get_entity("test_entities", "run_1") == {"revision": 2}


@pytest.mark.asyncio
async def test_cancelled_waiting_uow_cleans_up_connection_before_propagating_cancel(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    first_factory = SqliteUnitOfWorkFactory(database_path, busy_timeout_ms=2_000)
    second_factory = SqliteUnitOfWorkFactory(database_path, busy_timeout_ms=2_000)
    contender_started = asyncio.Event()

    async def blocked_writer() -> None:
        contender_started.set()
        async with second_factory.begin() as unit_of_work:
            await unit_of_work.entities.put("test_entities", "run_2", {"owner": "cancelled"}, expected_revision=0)
            await unit_of_work.commit()

    async with first_factory.begin() as first:
        await first.entities.put("test_entities", "run_1", {"owner": "first"}, expected_revision=0)
        contender = asyncio.create_task(blocked_writer())
        await contender_started.wait()
        loop_progressed = asyncio.Event()
        asyncio.get_running_loop().call_soon(loop_progressed.set)
        await loop_progressed.wait()
        assert not contender.done()
        contender.cancel()
        await first.commit()

    with pytest.raises(asyncio.CancelledError):
        await contender

    # A leaked BEGIN IMMEDIATE/connection would prevent this third writer.
    async with first_factory.begin() as third:
        await third.entities.put("test_entities", "run_3", {"owner": "third"}, expected_revision=0)
        await third.commit()
    assert await first_factory.get_entity("test_entities", "run_2") is None
    assert await first_factory.get_entity("test_entities", "run_3") == {"owner": "third"}
