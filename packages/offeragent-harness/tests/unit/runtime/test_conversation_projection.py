from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.runtime.conversation_projection import UowConversationProjectionService
from offeragent_harness.sessions import AgentLineage, Run, RunKind, RunStatus, Turn, TurnStatus
from offeragent_harness.testing import InMemoryUnitOfWorkFactory, ManualCancellationToken

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_uow_conversation_projection_builds_authoritative_turn_and_active_run() -> None:
    uow = InMemoryUnitOfWorkFactory()
    turn = Turn("turn_1", "ses_1", 1, TurnStatus.RUNNING, ({"type": "text", "text": "hello"},), NOW, NOW)
    run = Run(
        "run_1",
        "ses_1",
        "turn_1",
        "ws_1",
        AgentLineage.root("run_1"),
        RunKind.ROOT,
        RunStatus.PLANNING,
        1,
        4,
        {"model": "fake"},
        NOW,
        NOW,
        NOW,
    )
    state = RunState("ws_1", "ses_1", "turn_1", "run_1", AgentLineage.root("run_1"))
    state = state.transition(RunPhase.LOADING_CONTEXT)
    state = state.transition(RunPhase.SELECTING_MEMORY)
    state = state.transition(RunPhase.PLANNING)
    state = replace(state, assistant_text="answer")
    async with uow.begin() as work:
        await work.entities.put("turns", turn.turn_id, turn, expected_revision=0)
        await work.entities.put("runs", run.run_id, run, expected_revision=0)
        await work.entities.put("run_states", run.run_id, state, expected_revision=0)
        await work.commit()

    projection = UowConversationProjectionService(workspace_id="ws_1", unit_of_work=uow)
    result = await projection.turn("ses_1", "turn_1", ManualCancellationToken())

    assert result.selected_run_id == "run_1" and result.runs[0].last_sequence == 4
    assert result.assistant_content[0].text == "answer"  # type: ignore[union-attr]
    assert await projection.resolve_run_id("ses_1", "turn_1", None, ManualCancellationToken()) == "run_1"
    assert await projection.active_run_ids() == ("run_1",)
