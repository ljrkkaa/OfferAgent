from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import (
    SqliteEntityStore,
    SqliteInvocationJournal,
)
from offeragent_harness.ports import EntityRevisionConflict, InvocationJournalConflict, JournalState
from offeragent_harness.tools import (
    SideEffect,
    SideEffectKind,
    SideEffectState,
    ToolResult,
    ToolResultStatus,
)

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def result(after_hash: str = "sha256:" + "a" * 64) -> ToolResult:
    return ToolResult(
        tool_call_id="call_1",
        status=ToolResultStatus.SUCCEEDED,
        data={"afterHash": after_hash, "nested": [1, {"ok": True}]},
        user_visible_summary="write committed",
        artifact_ids=("artifact_1",),
        source_refs=("source_1",),
        side_effects=(
            SideEffect(
                kind=SideEffectKind.FILE_WRITE,
                state=SideEffectState.COMMITTED,
                resource_id="vault:notes/example.md",
                before_state={"hash": "sha256:" + "b" * 64},
                after_state={"hash": after_hash},
                metadata={"operation": "append"},
            ),
        ),
        retryable=False,
        before_state={"hash": "sha256:" + "b" * 64},
        after_state={"hash": after_hash},
        error=None,
    )


@pytest.mark.asyncio
async def test_entity_revision_cas_is_durable_and_concurrent(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    first = SqliteEntityStore(database_path)
    second = SqliteEntityStore(database_path)

    outcomes = await asyncio.gather(
        first.put("test_entities", "run_1", {"owner": "first"}, expected_revision=0),
        second.put("test_entities", "run_1", {"owner": "second"}, expected_revision=0),
        return_exceptions=True,
    )
    assert sum(outcome == 1 for outcome in outcomes) == 1
    assert sum(isinstance(outcome, EntityRevisionConflict) for outcome in outcomes) == 1
    assert await first.get_revision("test_entities", "run_1") == 1

    persisted = await SqliteEntityStore(database_path).get("test_entities", "run_1")
    assert persisted in ({"owner": "first"}, {"owner": "second"})
    revision = await first.put("test_entities", "run_1", {"status": "completed"}, expected_revision=1)
    assert revision == 2
    with pytest.raises(EntityRevisionConflict):
        await first.delete("test_entities", "run_1", expected_revision=1)
    await first.delete("test_entities", "run_1", expected_revision=2)
    assert await first.get("test_entities", "run_1") is None


@pytest.mark.asyncio
async def test_entity_store_rejects_non_json_without_mutating_revision(tmp_path: Path) -> None:
    store = SqliteEntityStore(tmp_path / "state.sqlite")
    with pytest.raises(TypeError, match="plain JSON only"):
        await store.put("test_entities", "run_1", {"bad": object()}, expected_revision=0)
    assert await store.get_revision("test_entities", "run_1") == 0


@pytest.mark.asyncio
async def test_explicit_database_paths_isolate_identical_entity_keys(tmp_path: Path) -> None:
    first = SqliteEntityStore(tmp_path / "workspace-a" / "state.sqlite")
    second = SqliteEntityStore(tmp_path / "workspace-b" / "state.sqlite")
    await first.put("test_entities", "run_1", {"workspace": "a"}, expected_revision=0)
    await second.put("test_entities", "run_1", {"workspace": "b"}, expected_revision=0)

    assert await first.get("test_entities", "run_1") == {"workspace": "a"}
    assert await second.get("test_entities", "run_1") == {"workspace": "b"}


@pytest.mark.asyncio
async def test_started_journal_survives_restart_and_can_be_marked_unknown(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    journal = SqliteInvocationJournal(database_path)
    started = await journal.start("ws_1", "idem_1", "request-hash", NOW)
    assert started.state is JournalState.STARTED

    reopened = SqliteInvocationJournal(database_path)
    recovered = await reopened.get("ws_1", "idem_1")
    assert recovered == started
    unknown = await reopened.mark_unknown("ws_1", "idem_1", "request-hash", NOW)
    assert unknown.state is JournalState.UNKNOWN
    assert unknown.result is None
    assert await reopened.mark_unknown("ws_1", "idem_1", "request-hash", NOW + timedelta(minutes=1)) == unknown


@pytest.mark.asyncio
async def test_completed_journal_round_trips_and_ack_loss_never_rebinds_or_reexecutes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite"
    expected_result = result()
    journal = SqliteInvocationJournal(database_path)
    await journal.start("ws_1", "idem_1", "request-hash", NOW)
    completed = await journal.complete("ws_1", "idem_1", "request-hash", expected_result, NOW)

    reopened = SqliteInvocationJournal(database_path)
    assert await reopened.get("ws_1", "idem_1") == completed
    assert (await reopened.start("ws_1", "idem_1", "request-hash", NOW)).state is JournalState.COMPLETED
    assert await reopened.complete("ws_1", "idem_1", "request-hash", expected_result, NOW) == completed
    assert await reopened.mark_unknown("ws_1", "idem_1", "request-hash", NOW) == completed

    with pytest.raises(InvocationJournalConflict, match="different arguments"):
        await reopened.start("ws_1", "idem_1", "different-hash", NOW)
    with pytest.raises(InvocationJournalConflict, match="different result"):
        await reopened.complete("ws_1", "idem_1", "request-hash", result("sha256:" + "c" * 64), NOW)


@pytest.mark.asyncio
async def test_unknown_record_can_be_reconciled_to_a_verified_completed_result(tmp_path: Path) -> None:
    journal = SqliteInvocationJournal(tmp_path / "state.sqlite")
    await journal.start("ws_1", "idem_1", "request-hash", NOW)
    await journal.mark_unknown("ws_1", "idem_1", "request-hash", NOW)
    reconciled = await journal.complete("ws_1", "idem_1", "request-hash", result(), NOW)
    assert reconciled.state is JournalState.COMPLETED
    assert reconciled.result == result()
