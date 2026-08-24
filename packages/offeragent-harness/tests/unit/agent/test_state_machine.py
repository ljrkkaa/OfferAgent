from dataclasses import replace

import pytest

from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.sessions import AgentLineage


def state() -> RunState:
    return RunState(
        workspace_id="ws_1",
        session_id="ses_1",
        turn_id="turn_1",
        run_id="run_1",
        lineage=AgentLineage.root("run_1"),
    )


@pytest.mark.parametrize(
    "phase",
    (
        RunPhase.VALIDATING_CALLS,
        RunPhase.CHECKING_POLICY,
        RunPhase.AWAITING_APPROVAL,
    ),
)
def test_validation_policy_and_approval_infrastructure_failures_have_terminal_path(
    phase: RunPhase,
) -> None:
    current = replace(state(), phase=phase)

    assert current.transition(RunPhase.FAILED).phase is RunPhase.FAILED
    assert current.transition(RunPhase.INTERRUPTED).phase is RunPhase.INTERRUPTED
