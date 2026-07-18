from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from offeragent_harness.ports import ApplicationCommandContext
from offeragent_harness.protocol.messages import TurnStartParams, validate_command_params
from offeragent_harness.runtime.application_dispatcher import ApplicationCommandHandler
from offeragent_harness.runtime.application_domain_handlers import DomainCommandIdentity, _turn_handlers
from offeragent_harness.runtime.harness_service import StartTurnCommand
from offeragent_harness.testing import ManualCancellationToken


class _Config:
    def __init__(self, *, model: str = "gpt-selected") -> None:
        self._model = model

    async def snapshot(self, **keys: str) -> object:
        del keys
        return SimpleNamespace(
            config=SimpleNamespace(
                model=SimpleNamespace(model=self._model),
                policy=SimpleNamespace(allow_bypass=False, read_only=False, workspace_trusted=True),
            ),
            fingerprint="sha256:" + "a" * 64,
        )


class _TransportPolicy:
    async def resolve_run_route(self, context: object, permission_mode: object) -> object:
        del context
        return SimpleNamespace(permission_mode=permission_mode)


class _UnexpectedHarness:
    async def start_turn(self, command: object) -> object:
        del command
        raise AssertionError("turn must be rejected before Harness run creation")


class _CapturingHarness:
    command: StartTurnCommand | None = None

    async def start_turn(self, command: StartTurnCommand) -> object:
        self.command = command
        return SimpleNamespace(
            session_id=command.session_id,
            turn_id=command.turn_id,
            run_id="run_one",
            accepted=True,
            duplicate=False,
        )


class _Attachments:
    async def release_turn_claim(self, turn_id: str, cancellation: object) -> None:
        del turn_id, cancellation


def _params(*, model: str = "gpt-selected") -> TurnStartParams:
    run_config = {"model": model}
    value = validate_command_params(
        "turn/start",
        {
            "sessionId": "ses_one",
            "turnId": "turn_one",
            "idempotencyKey": "turn-one",
            "input": [{"type": "text", "text": "hello"}],
            "runConfig": run_config,
        },
    )
    assert isinstance(value, TurnStartParams)
    return value


def _handlers(*, config: _Config, harness: object | None = None) -> Mapping[str, ApplicationCommandHandler]:
    return _turn_handlers(
        identity=DomainCommandIdentity("ws_one", "profile_one", "managed", "actor_one"),
        harness=harness or _UnexpectedHarness(),  # type: ignore[arg-type]
        projections=SimpleNamespace(),
        config=config,  # type: ignore[arg-type]
        transport_policy=_TransportPolicy(),  # type: ignore[arg-type]
        attachments=_Attachments(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_turn_start_rejects_request_and_persisted_exact_model_mismatch_before_run_creation() -> None:
    handlers = _handlers(config=_Config(model="gpt-persisted"))

    with pytest.raises(ValueError, match="Run model differs from the effective persisted configuration"):
        await handlers["turn/start"](
            _params(model="gpt-requested"),
            ManualCancellationToken(),
            ApplicationCommandContext(transport="stdio"),
        )


def test_turn_start_protocol_rejects_retired_provider_choice() -> None:
    with pytest.raises(Exception, match="参数不符合协议 Schema"):
        validate_command_params(
            "turn/start",
            {
                "sessionId": "ses_one",
                "turnId": "turn_one",
                "idempotencyKey": "turn-one-provider",
                "input": [{"type": "text", "text": "hello"}],
                "runConfig": {"provider": "codex", "model": "gpt-selected"},
            },
        )


@pytest.mark.asyncio
async def test_turn_start_persists_the_catalog_selected_model_with_the_internal_provider() -> None:
    harness = _CapturingHarness()
    handlers = _handlers(config=_Config(), harness=harness)

    await handlers["turn/start"](
        _params(),
        ManualCancellationToken(),
        ApplicationCommandContext(transport="stdio"),
    )

    assert harness.command is not None
    assert "provider" not in harness.command.run_config
    assert harness.command.run_config["model"] == "gpt-selected"
