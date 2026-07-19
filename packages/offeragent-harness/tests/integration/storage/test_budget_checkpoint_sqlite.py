from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.agent.budget_checkpoint import BudgetCheckpoint
from offeragent_harness.agent.budgets import BudgetDelta, BudgetLedger, RunBudget
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.storage import EntityCodecVersionError

STARTED = datetime(2026, 7, 13, 8, 30, tzinfo=timezone.utc)


def budget() -> RunBudget:
    return RunBudget(
        max_model_rounds=8,
        max_tool_calls=20,
        max_parallel_reads=4,
        max_wall_seconds=120.25,
        max_input_tokens=20_000,
        max_output_tokens=8_000,
        max_cost=Decimal("25.1250"),
        max_artifact_bytes=5_000_000,
        max_subagents=5,
    )


@pytest.mark.asyncio
async def test_budget_checkpoint_round_trips_after_sqlite_reopen_and_old_codec_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "budget-state.sqlite"
    ledger = BudgetLedger(budget(), started_at=STARTED)
    await ledger.consume(BudgetDelta(model_rounds=2, tool_calls=4, cost=Decimal("2.1250")))
    await ledger.reserve(BudgetDelta(model_rounds=1, output_tokens=1_000, cost=Decimal("0.5000"), subagents=1))
    checkpoint = await BudgetCheckpoint.capture(ledger, now=STARTED + timedelta(seconds=11.75))
    lineage = AgentLineage.root("run_budget")
    state = RunState(
        workspace_id="ws_budget",
        session_id="session_budget",
        turn_id="turn_budget",
        run_id=lineage.run_id,
        lineage=lineage,
        phase=RunPhase.PLANNING,
        revision=7,
        model_rounds=2,
        tool_calls=4,
        budget_checkpoint=checkpoint,
    )

    first = SqliteUnitOfWorkFactory(database_path)
    async with first.begin() as uow:
        await uow.entities.put("run_states", state.run_id, state, expected_revision=0)
        await uow.commit()

    reopened = SqliteUnitOfWorkFactory(database_path)
    recovered = await reopened.get_entity("run_states", state.run_id)
    assert recovered == state
    assert recovered.budget_checkpoint is not None
    assert recovered.budget_checkpoint.budget.max_cost == Decimal("25.1250")
    restored = recovered.budget_checkpoint.restore_ledger()
    assert (await restored.snapshot(now=STARTED + timedelta(seconds=15))).reserved == checkpoint.reserved

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT value_json FROM entities WHERE collection = 'run_states' AND entity_id = ?",
            (state.run_id,),
        ).fetchone()
        assert row is not None
        envelope = json.loads(row[0])
        assert envelope["schemaVersion"] == 6
        assert envelope["payload"]["budgetCheckpoint"]["limits"]["maxCost"] == "25.1250"
        assert envelope["payload"]["budgetCheckpoint"]["used"]["cost"] == "2.1250"
        envelope["schemaVersion"] = 3
        connection.execute(
            "UPDATE entities SET value_json = ? WHERE collection = 'run_states' AND entity_id = ?",
            (json.dumps(envelope), state.run_id),
        )

    with pytest.raises(EntityCodecVersionError, match=r"v6, found .* v3"):
        await SqliteUnitOfWorkFactory(database_path).get_entity("run_states", state.run_id)


@pytest.mark.asyncio
async def test_missing_checkpoint_is_explicit_null_after_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "missing-budget.sqlite"
    state = RunState("ws", "session", "turn", "run_missing", AgentLineage.root("run_missing"))
    first = SqliteUnitOfWorkFactory(database_path)
    async with first.begin() as uow:
        await uow.entities.put("run_states", state.run_id, state, expected_revision=0)
        await uow.commit()

    recovered = await SqliteUnitOfWorkFactory(database_path).get_entity("run_states", state.run_id)
    assert isinstance(recovered, RunState)
    assert recovered.budget_checkpoint is None
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT value_json FROM entities WHERE collection = 'run_states' AND entity_id = ?",
            (state.run_id,),
        ).fetchone()
    assert row is not None
    assert json.loads(row[0])["payload"]["budgetCheckpoint"] is None
