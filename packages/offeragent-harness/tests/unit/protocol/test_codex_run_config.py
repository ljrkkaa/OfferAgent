from __future__ import annotations

from offeragent_harness.protocol.common import RunConfigSnapshot
from offeragent_harness.protocol.events import TurnStartedPayload


def test_new_run_config_defaults_to_the_internal_codex_subscription_provider() -> None:
    snapshot = RunConfigSnapshot.model_validate_json('{"model":"gpt-selected"}')

    assert snapshot.provider == "codex-subscription-experimental"


def test_historical_turn_started_payload_keeps_its_explicit_retired_provider() -> None:
    payload = TurnStartedPayload.model_validate_json(
        """
        {
          "input": [{"type": "text", "text": "legacy event"}],
          "runConfig": {"provider": "codex", "model": "legacy-model"},
          "attempt": 1
        }
        """
    )

    assert payload.run_config.provider == "codex"
    assert payload.run_config.model == "legacy-model"
