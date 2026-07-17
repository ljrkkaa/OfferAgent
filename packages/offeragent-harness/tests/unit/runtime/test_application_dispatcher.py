from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

import pytest

from offeragent_harness.app import ApplicationNotReady
from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.ports import ApplicationCommandContext
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.capabilities import CapabilityName, CapabilitySet, ProtocolRange
from offeragent_harness.protocol.errors import ProtocolViolation
from offeragent_harness.protocol.messages import COMMAND_REGISTRY, RuntimeArch, RuntimePingResult, RuntimeStatusResult
from offeragent_harness.runtime.application_dispatcher import (
    CommandHandlerConfigurationError,
    RuntimeApplicationCommandDispatcher,
)
from offeragent_harness.runtime.application_domain_handlers import (
    DomainCommandIdentity,
    compose_domain_command_handlers,
)
from offeragent_harness.runtime.application_handlers import (
    ApplicationRuntimeIdentity,
    compose_application_command_handlers,
)
from offeragent_harness.runtime.config_service import ConfigRevisionConflict
from offeragent_harness.runtime.conversation_attachments import AttachmentError
from offeragent_harness.runtime.loopback_gateway import LoopbackWebGateway
from offeragent_harness.testing import ManualCancellationToken, ManualClock


class Ready:
    def __init__(self) -> None:
        self.ready = True

    def require_ready(self) -> object:
        if not self.ready:
            raise ApplicationNotReady("not ready with private startup detail")
        return self


async def _unused(raw: WireModel, cancellation: Any, context: Any) -> WireModel:
    raise AssertionError(f"unexpected handler: {type(raw).__name__}/{context.transport}")


def _identity() -> ApplicationRuntimeIdentity:
    capabilities = CapabilitySet.from_enabled(set(CapabilityName))
    return ApplicationRuntimeIdentity(
        runtime_version="1.2.3",
        core_version="1.2.3",
        protocol_version="1.0",
        supported_protocol_range=ProtocolRange(minimum="1.0", maximum="1.0"),
        schema_hash="sha256:" + "a" * 64,
        workspace_id="ws_test",
        workspace_instance_id="wsi_test",
        parent_pid=100,
        worker_pid=101,
        runtime_arch=RuntimeArch.WIN_X64,
        capabilities=capabilities,
        build_commit="abcdef0",
    )


def test_dispatcher_rejects_any_partial_or_extra_command_table() -> None:
    with pytest.raises(CommandHandlerConfigurationError, match="missing"):
        RuntimeApplicationCommandDispatcher(application=Ready(), handlers={})
    handlers = {method: _unused for method in COMMAND_REGISTRY}
    handlers["not/a/command"] = _unused
    with pytest.raises(CommandHandlerConfigurationError, match="extra"):
        RuntimeApplicationCommandDispatcher(application=Ready(), handlers=handlers)


@pytest.mark.asyncio
async def test_initialize_negotiates_identity_and_transport_on_same_dispatcher() -> None:
    identity = _identity()
    domains = {
        method: _unused for method in COMMAND_REGISTRY if method not in {"initialize", "runtime/ping", "runtime/status"}
    }
    handlers = compose_application_command_handlers(
        identity=identity,
        clock=ManualClock(),
        runtime_status=_unused,
        domain_handlers=domains,
    )
    dispatcher = RuntimeApplicationCommandDispatcher(application=Ready(), handlers=handlers)
    params = {
        "protocolVersion": "1.0",
        "clientVersion": "2.0.0",
        "workspaceId": "ws_test",
        "capabilities": identity.capabilities.to_wire(),
        "requiredCapabilities": ["eventReplay"],
        "schemaHash": identity.schema_hash,
    }
    direct = await dispatcher.dispatch(
        "initialize",
        params,
        ManualCancellationToken(),
        context=ApplicationCommandContext(transport="stdio"),
    )
    web = await dispatcher.dispatch(
        "initialize",
        params,
        ManualCancellationToken(),
        context=ApplicationCommandContext(transport="loopback-http"),
    )
    assert direct["transport"] == "stdio"  # type: ignore[index]
    assert web["transport"] == "loopback-http"  # type: ignore[index]
    for key in ("workerPid", "coreVersion", "schemaHash", "workspaceInstanceId"):
        assert direct[key] == web[key]  # type: ignore[index]
    assert dispatcher.methods == frozenset(COMMAND_REGISTRY)


@pytest.mark.asyncio
async def test_dispatcher_checks_readiness_before_command_validation() -> None:
    ready = Ready()
    ready.ready = False
    dispatcher = RuntimeApplicationCommandDispatcher(
        application=ready,
        handlers={method: _unused for method in COMMAND_REGISTRY},
    )
    with pytest.raises(ProtocolViolation) as rejected:
        await dispatcher.dispatch("unknown", {}, ManualCancellationToken())
    assert rejected.value.error.code is ErrorCode.RUNTIME_NOT_READY
    assert "private startup detail" not in rejected.value.error.model_dump_json()


@pytest.mark.parametrize(
    ("failure", "expected", "expected_details"),
    [
        (lambda: PermissionError("private permission rule"), ErrorCode.POLICY_DENIED, {}),
        (lambda: FileNotFoundError("C:/private/notebook.md"), ErrorCode.RESOURCE_NOT_FOUND, {}),
        (
            lambda: ConfigRevisionConflict(7, 9),
            ErrorCode.RESOURCE_CONFLICT,
            {"reason": "resource_state_conflict"},
        ),
        (
            lambda: AttachmentError("capacity_exceeded", "delete old images or start a new Conversation"),
            ErrorCode.RESOURCE_CONFLICT,
            {"reason": "capacity_exceeded"},
        ),
        (lambda: asyncio.CancelledError("private cancellation reason"), ErrorCode.REQUEST_CANCELLED, {}),
        (lambda: RuntimeError("private internal failure"), ErrorCode.INTERNAL_ERROR, {}),
    ],
)
@pytest.mark.asyncio
async def test_dispatcher_sanitizes_application_failures(
    failure: Callable[[], BaseException],
    expected: ErrorCode,
    expected_details: dict[str, str],
) -> None:
    async def fail(_raw: WireModel, _cancellation: Any, _context: Any) -> WireModel:
        raise failure()

    dispatcher = RuntimeApplicationCommandDispatcher(
        application=Ready(),
        handlers={method: fail for method in COMMAND_REGISTRY},
    )
    with pytest.raises(ProtocolViolation) as rejected:
        await dispatcher.dispatch("runtime/status", {}, ManualCancellationToken())
    envelope = rejected.value.error
    assert envelope.code is expected
    assert envelope.cancelled is (expected is ErrorCode.REQUEST_CANCELLED)
    assert envelope.details == expected_details
    serialized = envelope.model_dump_json()
    assert all(secret not in serialized for secret in ("private", "C:/", "7", "9"))


@pytest.mark.asyncio
async def test_ping_result_is_validated_after_real_identity_handler() -> None:
    identity = _identity()
    handlers = compose_application_command_handlers(
        identity=identity,
        clock=ManualClock(),
        runtime_status=_unused,
        domain_handlers={
            method: _unused
            for method in COMMAND_REGISTRY
            if method not in {"initialize", "runtime/ping", "runtime/status"}
        },
    )
    dispatcher = RuntimeApplicationCommandDispatcher(application=Ready(), handlers=handlers)
    raw = await dispatcher.dispatch("runtime/ping", {"nonce": "req_ping"}, ManualCancellationToken())
    result = RuntimePingResult.model_validate(raw)
    assert result.worker_pid == 101 and result.nonce == "req_ping"


def test_status_provider_type_is_part_of_wire_contract() -> None:
    assert RuntimeStatusResult.model_fields["database_identity"].is_required()


def test_domain_factory_covers_every_non_identity_command_exactly_once() -> None:
    class Harness:
        approvals = object()

    shared = object()

    def gateway_provider() -> LoopbackWebGateway | None:
        return cast(LoopbackWebGateway, shared)

    handlers = compose_domain_command_handlers(
        identity=DomainCommandIdentity("ws_test", "profile_1", "managed", "actor_1"),
        clock=shared,  # type: ignore[arg-type]
        harness=Harness(),  # type: ignore[arg-type]
        config=shared,  # type: ignore[arg-type]
        config_activation=shared,  # type: ignore[arg-type]
        models=shared,  # type: ignore[arg-type]
        projections=shared,  # type: ignore[arg-type]
        artifacts=shared,  # type: ignore[arg-type]
        attachments=shared,  # type: ignore[arg-type]
        secrets=shared,  # type: ignore[arg-type]
        controls=shared,  # type: ignore[arg-type]
        subagents=shared,  # type: ignore[arg-type]
        subagent_authorities=shared,  # type: ignore[arg-type]
        subagent_artifacts=shared,  # type: ignore[arg-type]
        diagnostics=shared,  # type: ignore[arg-type]
        diagnostics_owner_runs=shared,  # type: ignore[arg-type]
        gateway_provider=gateway_provider,
        transport_policy=shared,  # type: ignore[arg-type]
        extension_management_handlers={
            method: _unused
            for method in COMMAND_REGISTRY
            if method.startswith(("skills/", "shell/", "hooks/", "process/"))
        },
        plugin_tool_handlers={"plugin-tools/complete": _unused},
    )
    assert set(handlers) == set(COMMAND_REGISTRY) - {"initialize", "runtime/ping", "runtime/status"}
