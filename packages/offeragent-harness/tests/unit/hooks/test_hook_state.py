from __future__ import annotations

import asyncio

import pytest

from offeragent_harness.hooks import (
    HookCommandSpec,
    HookDefinition,
    HookEvent,
    HookImplementation,
    HookLayer,
    HookScope,
)
from offeragent_harness.hooks.state import (
    EntityHookConfigurationStore,
    HookConfigurationService,
    HookLayerTrust,
    hook_definition_hash,
    hook_layer_hash,
)
from offeragent_harness.ports import EntityRevisionConflict
from offeragent_harness.testing import InMemoryUnitOfWorkFactory, ManualCancellationToken

FINGERPRINT = "sha256:" + "a" * 64


def _builtin(scope: HookScope, owner: str, hook_id: str = "builtin-hook") -> HookDefinition:
    return HookDefinition(
        hook_id,
        scope,
        owner,
        HookEvent.PRE_TOOL_USE,
        HookImplementation.BUILTIN,
        handler_id="builtin.allow",
    )


def _command(owner: str, *, argument: str = "--json") -> HookDefinition:
    return HookDefinition(
        "workspace-command",
        HookScope.WORKSPACE,
        owner,
        HookEvent.PRE_TOOL_USE,
        HookImplementation.COMMAND,
        command=HookCommandSpec(
            "signed-hook",
            (argument,),
            frozenset({"LANG"}),
            FINGERPRINT,
            "vault",
            ".offeragent/hooks",
            "minimal",
            1024 * 1024,
        ),
    )


def _service(unit_of_work: InMemoryUnitOfWorkFactory, workspace_id: str = "workspace-1") -> HookConfigurationService:
    return HookConfigurationService(
        workspace_id=workspace_id,
        managed_layer=HookLayer(HookScope.MANAGED, "system", 1),
        signed_builtin_handler_ids=frozenset({"builtin.allow"}),
        store=EntityHookConfigurationStore(unit_of_work),
    )


@pytest.mark.asyncio
async def test_user_and_session_layers_execute_only_after_content_bound_confirmation_and_restart() -> None:
    durable = InMemoryUnitOfWorkFactory()
    service = _service(durable)
    token = ManualCancellationToken()
    await service.initialize(token)

    user_layer = HookLayer(HookScope.USER, "principal-1", 1, (_builtin(HookScope.USER, "principal-1"),))
    record = await service.install_layer(
        user_layer,
        expected_revision=0,
        idempotency_key="install-user",
        cancellation=token,
    )
    assert record.trust is HookLayerTrust.CONFIRMATION_REQUIRED
    assert [
        item.scope
        for item in service.effective_layers(principal_id="principal-1", session_id="session-1", workspace_trusted=True)
    ] == [HookScope.MANAGED]

    with pytest.raises(ValueError, match="content hash"):
        await service.confirm_layer(
            HookScope.USER,
            "principal-1",
            "sha256:" + "0" * 64,
            expected_revision=record.revision,
            idempotency_key="wrong-confirmation",
            cancellation=token,
        )
    confirmed = await service.confirm_layer(
        HookScope.USER,
        "principal-1",
        hook_layer_hash(user_layer),
        expected_revision=record.revision,
        idempotency_key="confirm-user",
        cancellation=token,
    )
    assert confirmed.trust is HookLayerTrust.CONFIRMED

    restarted = _service(durable)
    await restarted.initialize(token)
    assert [
        item.scope
        for item in restarted.effective_layers(
            principal_id="principal-1", session_id="session-1", workspace_trusted=True
        )
    ] == [HookScope.MANAGED, HookScope.USER]

    session_layer = HookLayer(
        HookScope.SESSION,
        "session-1",
        1,
        (_builtin(HookScope.SESSION, "session-1", "session-hook"),),
    )
    pending = await restarted.install_layer(
        session_layer,
        expected_revision=0,
        idempotency_key="install-session",
        cancellation=token,
    )
    await restarted.confirm_layer(
        HookScope.SESSION,
        "session-1",
        hook_layer_hash(session_layer),
        expected_revision=pending.revision,
        idempotency_key="confirm-session",
        cancellation=token,
    )
    assert [
        item.scope
        for item in restarted.effective_layers(
            principal_id="principal-1", session_id="session-1", workspace_trusted=True
        )
    ] == [HookScope.MANAGED, HookScope.USER, HookScope.SESSION]


@pytest.mark.asyncio
async def test_workspace_layer_needs_workspace_trust_and_each_command_definition_confirmation() -> None:
    durable = InMemoryUnitOfWorkFactory()
    service = _service(durable)
    token = ManualCancellationToken()
    await service.initialize(token)
    layer = HookLayer(
        HookScope.WORKSPACE,
        "workspace-1",
        1,
        (_builtin(HookScope.WORKSPACE, "workspace-1"), _command("workspace-1")),
    )
    installed = await service.install_layer(
        layer,
        expected_revision=0,
        idempotency_key="install-workspace",
        cancellation=token,
    )
    assert all(
        item.scope is not HookScope.WORKSPACE
        for item in service.effective_layers(
            principal_id="principal-1", session_id="session-1", workspace_trusted=False
        )
    )
    trusted = service.effective_layers(principal_id="principal-1", session_id="session-1", workspace_trusted=True)
    assert [item.hook_id for item in trusted[-1].hooks] == ["builtin-hook"]

    command = layer.hooks[1]
    confirmed = await service.confirm_workspace_command(
        "workspace-1",
        command.hook_id,
        hook_definition_hash(command),
        expected_revision=installed.revision,
        idempotency_key="confirm-command",
        cancellation=token,
    )
    assert [
        item.hook_id
        for item in service.effective_layers(
            principal_id="principal-1", session_id="session-1", workspace_trusted=True
        )[-1].hooks
    ] == ["builtin-hook", "workspace-command"]

    changed = HookLayer(
        HookScope.WORKSPACE,
        "workspace-1",
        2,
        (_builtin(HookScope.WORKSPACE, "workspace-1"), _command("workspace-1", argument="--changed")),
    )
    updated = await service.install_layer(
        changed,
        expected_revision=confirmed.revision,
        idempotency_key="change-command",
        cancellation=token,
    )
    assert updated.command_confirmations == {}
    assert [
        item.hook_id
        for item in service.effective_layers(
            principal_id="principal-1", session_id="session-1", workspace_trusted=True
        )[-1].hooks
    ] == ["builtin-hook"]


@pytest.mark.asyncio
async def test_configuration_cas_has_one_winner_and_idempotent_replay() -> None:
    durable = InMemoryUnitOfWorkFactory()
    first = _service(durable)
    second = _service(durable)
    token = ManualCancellationToken()
    await asyncio.gather(first.initialize(token), second.initialize(token))
    layer = HookLayer(HookScope.USER, "principal-1", 1, (_builtin(HookScope.USER, "principal-1"),))

    results = await asyncio.gather(
        first.install_layer(
            layer,
            expected_revision=0,
            idempotency_key="writer-first",
            cancellation=token,
        ),
        second.install_layer(
            layer,
            expected_revision=0,
            idempotency_key="writer-second",
            cancellation=token,
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    assert sum(isinstance(item, EntityRevisionConflict) for item in results) == 1

    winner = next(item for item in results if not isinstance(item, BaseException))
    assert not isinstance(winner, BaseException)
    owner = first if winner.idempotency_key == "writer-first" else second
    replay = await owner.install_layer(
        layer,
        expected_revision=0,
        idempotency_key=winner.idempotency_key,
        cancellation=token,
    )
    assert replay == winner


@pytest.mark.asyncio
async def test_production_validation_rejects_dynamic_code_shell_switches_and_secret_environment() -> None:
    durable = InMemoryUnitOfWorkFactory()
    service = _service(durable)
    token = ManualCancellationToken()
    await service.initialize(token)

    unknown = HookLayer(
        HookScope.USER,
        "principal-1",
        1,
        (
            HookDefinition(
                "dynamic",
                HookScope.USER,
                "principal-1",
                HookEvent.TURN_START,
                HookImplementation.BUILTIN,
                handler_id="vault.module:function",
            ),
        ),
    )
    with pytest.raises(ValueError, match="signed builtin"):
        await service.install_layer(
            unknown,
            expected_revision=0,
            idempotency_key="dynamic-import",
            cancellation=token,
        )

    with pytest.raises(ValueError, match="structural"):
        HookCommandSpec("powershell", ("-EncodedCommand", "AAAA"), executable_profile_fingerprint=FINGERPRINT)

    secret_command = HookDefinition(
        "secret-env",
        HookScope.USER,
        "principal-1",
        HookEvent.TURN_START,
        HookImplementation.COMMAND,
        command=HookCommandSpec(
            "signed-hook",
            allowed_environment=frozenset({"API_TOKEN"}),
            executable_profile_fingerprint=FINGERPRINT,
        ),
    )
    with pytest.raises(ValueError, match="environment allowlist"):
        await service.install_layer(
            HookLayer(HookScope.USER, "principal-1", 1, (secret_command,)),
            expected_revision=0,
            idempotency_key="secret-env",
            cancellation=token,
        )
