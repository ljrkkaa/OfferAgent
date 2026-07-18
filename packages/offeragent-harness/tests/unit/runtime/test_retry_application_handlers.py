from __future__ import annotations

from types import SimpleNamespace

import pytest

from offeragent_harness.ports import ApplicationCommandContext
from offeragent_harness.protocol.messages import TurnRetryParams, validate_command_params
from offeragent_harness.runtime.application_handlers import conversation_control_handlers
from offeragent_harness.runtime.harness_service import RetryTurnCommand
from offeragent_harness.testing import ManualCancellationToken

CODEX_SUBSCRIPTION_PROVIDER = "codex-subscription-experimental"


class _Config:
    def __init__(self, *, provider: str = CODEX_SUBSCRIPTION_PROVIDER, model: str = "gpt-current") -> None:
        self.provider = provider
        self.model = model

    async def snapshot(self, **keys: str) -> object:
        del keys
        return SimpleNamespace(
            config=SimpleNamespace(
                model=SimpleNamespace(provider=SimpleNamespace(value=self.provider), model=self.model),
                policy=SimpleNamespace(allow_bypass=False, read_only=False, workspace_trusted=True),
            ),
            fingerprint="sha256:" + "a" * 64,
        )


class _Harness:
    def __init__(self, source_provider: str = CODEX_SUBSCRIPTION_PROVIDER, source_model: str = "gpt-current") -> None:
        self.source_provider = source_provider
        self.source_model = source_model
        self.command: RetryTurnCommand | None = None

    async def get_run(self, run_id: str) -> object:
        assert run_id == "run_source"
        return SimpleNamespace(
            config_snapshot={"provider": self.source_provider, "model": self.source_model},
        )

    async def retry_turn(self, command: RetryTurnCommand) -> object:
        self.command = command
        return SimpleNamespace(
            session_id=command.session_id,
            turn_id=command.turn_id,
            run_id="run_retry",
            accepted=True,
            duplicate=False,
        )


class _TransportPolicy:
    async def resolve_run_route(self, context: object, permission_mode: object) -> object:
        del context
        return SimpleNamespace(permission_mode=permission_mode)


def _params(*, provider: str | None = None, model: str | None = None) -> TurnRetryParams:
    raw: dict[str, object] = {
        "sessionId": "ses_one",
        "turnId": "turn_retry",
        "sourceRunId": "run_source",
        "idempotencyKey": "retry-one",
    }
    if model is not None:
        raw["runConfig"] = {"provider": provider, "model": model}
    params = validate_command_params("turn/retry", raw)
    assert isinstance(params, TurnRetryParams)
    return params


def _handlers(harness: _Harness, config: _Config) -> object:
    return conversation_control_handlers(
        workspace_id="ws_one",
        profile_id="profile_one",
        managed_owner_id="managed",
        harness=harness,  # type: ignore[arg-type]
        controls=SimpleNamespace(),  # type: ignore[arg-type]
        config=config,  # type: ignore[arg-type]
        transport_policy=_TransportPolicy(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "params"),
    [
        (_Harness(source_model="gpt-retired"), _params()),
        (_Harness(), _params(provider="codex", model="gpt-current")),
        (_Harness(), _params(provider=CODEX_SUBSCRIPTION_PROVIDER, model="gpt-other")),
    ],
)
async def test_retry_rejects_source_or_explicit_model_drift_without_starting_a_run(
    harness: _Harness,
    params: TurnRetryParams,
) -> None:
    handlers = _handlers(harness, _Config())

    with pytest.raises(ValueError):
        await handlers["turn/retry"](  # type: ignore[index]
            params,
            ManualCancellationToken(),
            ApplicationCommandContext(transport="stdio"),
        )

    assert harness.command is None


@pytest.mark.asyncio
async def test_retry_preserves_the_exact_current_codex_model() -> None:
    harness = _Harness()
    handlers = _handlers(harness, _Config())

    await handlers["turn/retry"](  # type: ignore[index]
        _params(),
        ManualCancellationToken(),
        ApplicationCommandContext(transport="stdio"),
    )

    assert harness.command is not None
    run_config = harness.command.run_config
    assert run_config is not None
    assert run_config["provider"] == CODEX_SUBSCRIPTION_PROVIDER
    assert run_config["model"] == "gpt-current"
