from __future__ import annotations

import pytest
from pydantic import ValidationError

from offeragent_harness.protocol.common import RunConfigSnapshot
from offeragent_harness.protocol.events import TurnStartedPayload


def test_new_run_config_has_no_provider_choice() -> None:
    snapshot = RunConfigSnapshot.model_validate_json('{"model":"gpt-selected"}')

    assert snapshot.to_wire()["model"] == "gpt-selected"
    assert "provider" not in snapshot.to_wire()


def test_live_turn_event_contract_rejects_retired_provider_identity() -> None:
    with pytest.raises(ValidationError):
        TurnStartedPayload.model_validate_json(
            """
            {
              "input": [{"type": "text", "text": "legacy event"}],
              "runConfig": {"provider": "codex", "model": "legacy-model"},
              "attempt": 1
            }
            """
        )
