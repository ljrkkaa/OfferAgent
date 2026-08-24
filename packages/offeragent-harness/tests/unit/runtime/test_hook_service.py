from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from offeragent_harness.hooks import (
    HookCommandSpec,
    HookDecision,
    HookDefinition,
    HookEvent,
    HookExecutionContext,
    HookImplementation,
    HookInvocation,
    HookLayer,
    HookOutput,
    HookScope,
    HookToolInput,
)
from offeragent_harness.ports import SupervisedProcessRequest, SupervisedProcessResult
from offeragent_harness.runtime.hook_service import HookHandlerRegistry, HookService, StaticHookLayerSource
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWork,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
    RecordingEventSink,
)
from offeragent_harness.testing.errors import FakeRunCancelled
from offeragent_harness.tools import canonical_json_sha256


@dataclass
class Handler:
    output: HookOutput
    calls: int = 0

    async def invoke(self, definition: Any, invocation: Any, cancellation: Any) -> HookOutput:
        del definition, invocation
        cancellation.checkpoint()
        self.calls += 1
        return self.output


class WaitingHandler:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def invoke(self, definition: Any, invocation: Any, cancellation: Any) -> HookOutput:
        del definition, invocation, cancellation
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class RecursiveHandler:
    def __init__(self) -> None:
        self.service: HookService | None = None
        self.token: ManualCancellationToken | None = None

    async def invoke(self, definition: Any, invocation: HookInvocation, cancellation: Any) -> HookOutput:
        del definition, cancellation
        assert self.service is not None and self.token is not None
        nested = HookInvocation(
            "nested-invocation",
            invocation.chain_id,
            invocation.event,
            invocation.context,
            invocation.run_id,
            invocation.facts,
            invocation.tool,
        )
        outcome = await self.service.invoke(nested, self.token)
        return HookOutput(outcome.decision)


class ParallelHandler:
    def __init__(self) -> None:
        self.entered = 0
        self.release = asyncio.Event()

    async def invoke(self, definition: Any, invocation: Any, cancellation: Any) -> HookOutput:
        del definition, invocation
        cancellation.checkpoint()
        self.entered += 1
        if self.entered == 2:
            self.release.set()
        await self.release.wait()
        return HookOutput()


class ProcessFake:
    def __init__(self, result: SupervisedProcessResult) -> None:
        self.result = result
        self.requests: list[SupervisedProcessRequest] = []

    async def execute(self, request: SupervisedProcessRequest, cancellation: Any) -> SupervisedProcessResult:
        cancellation.checkpoint()
        self.requests.append(request)
        return self.result


class CommitAckLossUnitOfWork:
    def __init__(self, inner: InMemoryUnitOfWork, owner: CommitAckLossFactory) -> None:
        self._inner = inner
        self._owner = owner

    @property
    def entities(self) -> Any:
        return self._inner.entities

    @property
    def events(self) -> Any:
        return self._inner.events

    @property
    def journal(self) -> Any:
        return self._inner.journal

    async def __aenter__(self) -> CommitAckLossUnitOfWork:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self._inner.__aexit__(exc_type, exc, traceback)

    async def commit(self) -> None:
        await self._inner.commit()
        self._owner.commit_calls += 1
        if self._owner.commit_calls in self._owner.fail_calls:
            raise OSError("commit ACK lost")

    async def rollback(self) -> None:
        await self._inner.rollback()


class CommitAckLossFactory:
    def __init__(self, inner: InMemoryUnitOfWorkFactory, fail_calls: frozenset[int]) -> None:
        self.inner = inner
        self.fail_calls = fail_calls
        self.commit_calls = 0

    def begin(self) -> CommitAckLossUnitOfWork:
        return CommitAckLossUnitOfWork(self.inner.begin(), self)


def _tool() -> HookToolInput:
    arguments = {"path": "notes/secret-name.md", "content": "DO-NOT-PERSIST"}
    return HookToolInput(
        "call-1",
        "vault.transaction",
        "1",
        canonical_json_sha256({"definition": 1}),
        arguments,
        canonical_json_sha256(arguments),
        "idem-1",
    )


def _invocation(
    event: HookEvent = HookEvent.PRE_TOOL_USE,
    *,
    invocation_id: str = "hook-invocation-1",
    chain_id: str = "chain-1",
    trusted: bool = True,
) -> HookInvocation:
    return HookInvocation(
        invocation_id,
        chain_id,
        event,
        HookExecutionContext("system", "user-1", "workspace-1", "session-1", trusted),
        "run-1",
        {"purpose": "test", "private": "DO-NOT-PERSIST"},
        _tool() if event in {HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE, HookEvent.APPROVAL_REQUIRED} else None,
    )


def _definition(
    hook_id: str,
    scope: HookScope,
    owner: str,
    *,
    event: HookEvent = HookEvent.PRE_TOOL_USE,
    handler_id: str | None = None,
    command: HookCommandSpec | None = None,
    timeout_ms: int = 5_000,
    output_limit_bytes: int = 64 * 1024,
) -> HookDefinition:
    implementation = HookImplementation.COMMAND if command is not None else HookImplementation.BUILTIN
    return HookDefinition(
        hook_id,
        scope,
        owner,
        event,
        implementation,
        timeout_ms=timeout_ms,
        output_limit_bytes=output_limit_bytes,
        handler_id=handler_id or (None if command else hook_id),
        command=command,
    )


def _service(
    layers: tuple[HookLayer, ...],
    handlers: Mapping[str, Any],
    *,
    clock: ManualClock | None = None,
    sink: RecordingEventSink | None = None,
    process: ProcessFake | None = None,
    uow: InMemoryUnitOfWorkFactory | None = None,
) -> tuple[HookService, InMemoryUnitOfWorkFactory, RecordingEventSink, ProcessFake]:
    factory = uow or InMemoryUnitOfWorkFactory()
    event_sink = sink or RecordingEventSink()
    process_fake = process or ProcessFake(SupervisedProcessResult(0, b'{"decision":"continue"}', b""))
    return (
        HookService(
            layers=StaticHookLayerSource(layers),
            handlers=HookHandlerRegistry(handlers),
            process_supervisor=process_fake,
            unit_of_work=factory,
            event_sink=event_sink,
            clock=clock or ManualClock(),
            ids=DeterministicIdGenerator(),
            environment={"LANG": "zh_CN.UTF-8", "PATH": "must-not-leak", "TOKEN": "must-not-leak"},
        ),
        factory,
        event_sink,
        process_fake,
    )


@pytest.mark.asyncio
async def test_any_deny_wins_while_mutations_compose_in_layer_order() -> None:
    managed = Handler(HookOutput(argument_patch={"path": "managed.md"}, audit_tags=("managed",)))
    user = Handler(HookOutput(HookDecision.ASK, argument_patch={"extra": 1}))
    workspace = Handler(HookOutput(HookDecision.DENY, audit_tags=("blocked",)))
    layers = (
        HookLayer(HookScope.MANAGED, "system", 1, (_definition("managed", HookScope.MANAGED, "system"),)),
        HookLayer(HookScope.USER, "user-1", 1, (_definition("user", HookScope.USER, "user-1"),)),
        HookLayer(
            HookScope.WORKSPACE,
            "workspace-1",
            1,
            (_definition("workspace", HookScope.WORKSPACE, "workspace-1"),),
        ),
    )
    service, _, _, _ = _service(layers, {"managed": managed, "user": user, "workspace": workspace})

    outcome = await service.invoke(_invocation(), ManualCancellationToken())

    assert outcome.decision is HookDecision.DENY
    assert outcome.mutated_arguments is not None
    assert outcome.mutated_arguments["path"] == "managed.md"
    assert outcome.mutated_arguments["extra"] == 1
    assert outcome.audit_tags == ("blocked", "managed")


@pytest.mark.asyncio
async def test_same_invocation_is_concurrency_safe_and_ack_loss_does_not_rerun_hook() -> None:
    handler = Handler(HookOutput())
    layer = HookLayer(HookScope.USER, "user-1", 1, (_definition("once", HookScope.USER, "user-1"),))
    sink = RecordingEventSink(acknowledgement_loss_calls=frozenset({1}))
    service, _, _, _ = _service((layer,), {"once": handler}, sink=sink)
    invocation = _invocation(event=HookEvent.PRE_TOOL_USE)

    first, second = await asyncio.gather(
        service.invoke(invocation, ManualCancellationToken()),
        service.invoke(invocation, ManualCancellationToken()),
    )

    assert handler.calls == 1
    assert not first.replayed
    assert second.replayed
    assert service.delivery_failures == ["AcknowledgementLost"]


@pytest.mark.asyncio
async def test_uow_commit_ack_loss_recovers_claim_and_completion_without_rerun() -> None:
    handler = Handler(HookOutput())
    layer = HookLayer(HookScope.USER, "user-1", 1, (_definition("once", HookScope.USER, "user-1"),))
    inner = InMemoryUnitOfWorkFactory()
    service, _, sink, _ = _service(
        (layer,),
        {"once": handler},
        uow=CommitAckLossFactory(inner, frozenset({1, 2})),  # type: ignore[arg-type]
    )

    outcome = await service.invoke(_invocation(), ManualCancellationToken())

    assert outcome.decision is HookDecision.CONTINUE
    assert handler.calls == 1
    assert len(sink.events) == 1
    assert len(await inner.list_entities("hook_audits")) == 1


@pytest.mark.asyncio
async def test_unknown_session_start_outcome_fails_closed_after_restart() -> None:
    invocation = _invocation(event=HookEvent.SESSION_START)
    inner = InMemoryUnitOfWorkFactory()
    async with inner.begin() as uow:
        await uow.entities.put(
            "hook_invocation_receipts",
            invocation.invocation_id,
            {
                "schemaVersion": 1,
                "state": "started",
                "requestHash": invocation.request_hash,
                "event": HookEvent.SESSION_START.value,
                "startedAt": ManualClock().utcnow().isoformat(),
            },
            expected_revision=0,
        )
        await uow.commit()
    service, _, _, _ = _service((), {}, uow=inner)

    outcome = await service.invoke(invocation, ManualCancellationToken())

    assert outcome.decision is HookDecision.DENY
    assert outcome.warning_codes == ("hook_previous_outcome_unknown",)


@pytest.mark.asyncio
async def test_recursion_in_same_chain_is_blocked_and_pre_tool_fails_closed() -> None:
    handler = RecursiveHandler()
    layer = HookLayer(HookScope.USER, "user-1", 1, (_definition("recursive", HookScope.USER, "user-1"),))
    service, _, _, _ = _service((layer,), {"recursive": handler})
    handler.service = service
    handler.token = ManualCancellationToken()

    outcome = await service.invoke(_invocation(), ManualCancellationToken())

    assert outcome.decision is HookDecision.DENY


@pytest.mark.asyncio
async def test_distinct_hook_chains_run_concurrently_without_false_recursion() -> None:
    handler = ParallelHandler()
    layer = HookLayer(HookScope.USER, "user-1", 1, (_definition("parallel", HookScope.USER, "user-1"),))
    service, _, _, _ = _service((layer,), {"parallel": handler})

    outcomes = await asyncio.gather(
        service.invoke(_invocation(invocation_id="hook-a", chain_id="chain-a"), ManualCancellationToken()),
        service.invoke(_invocation(invocation_id="hook-b", chain_id="chain-b"), ManualCancellationToken()),
    )

    assert handler.entered == 2
    assert all(outcome.decision is HookDecision.CONTINUE for outcome in outcomes)


@pytest.mark.asyncio
async def test_independent_timeout_cancels_handler_and_pre_tool_fails_closed() -> None:
    clock = ManualClock()
    handler = WaitingHandler()
    layer = HookLayer(
        HookScope.USER,
        "user-1",
        1,
        (_definition("slow", HookScope.USER, "user-1", timeout_ms=10),),
    )
    service, _, _, _ = _service((layer,), {"slow": handler}, clock=clock)
    task = asyncio.create_task(service.invoke(_invocation(), ManualCancellationToken()))
    await handler.started.wait()

    clock.advance(__import__("datetime").timedelta(milliseconds=11))
    outcome = await task

    assert outcome.decision is HookDecision.DENY
    assert "hook_timeout" in outcome.warning_codes
    assert handler.cancelled


@pytest.mark.asyncio
async def test_post_tool_failure_warns_and_continues() -> None:
    process = ProcessFake(SupervisedProcessResult(7, b"", b"private stderr"))
    definition = _definition(
        "observer",
        HookScope.WORKSPACE,
        "workspace-1",
        event=HookEvent.POST_TOOL_USE,
        command=HookCommandSpec("signed-observer"),
    )
    service, _, _, _ = _service(
        (HookLayer(HookScope.WORKSPACE, "workspace-1", 1, (definition,)),),
        {},
        process=process,
    )

    outcome = await service.invoke(_invocation(HookEvent.POST_TOOL_USE), ManualCancellationToken())

    assert outcome.decision is HookDecision.CONTINUE
    assert outcome.warning_codes == ("hook_process_failed",)


@pytest.mark.asyncio
async def test_untrusted_workspace_never_starts_command_hook() -> None:
    process = ProcessFake(SupervisedProcessResult(0, b'{"decision":"deny"}', b""))
    definition = _definition(
        "command",
        HookScope.WORKSPACE,
        "workspace-1",
        command=HookCommandSpec("signed-hook", allowed_environment=frozenset({"LANG", "TOKEN"})),
    )
    service, _, _, _ = _service(
        (HookLayer(HookScope.WORKSPACE, "workspace-1", 1, (definition,)),),
        {},
        process=process,
    )

    outcome = await service.invoke(_invocation(trusted=False), ManualCancellationToken())

    assert outcome.decision is HookDecision.CONTINUE
    assert outcome.warning_codes == ("hook_command_workspace_untrusted",)
    assert not process.requests


@pytest.mark.asyncio
async def test_command_hook_uses_supervisor_strict_environment_and_output_protocol() -> None:
    process = ProcessFake(
        SupervisedProcessResult(
            0,
            b'{"decision":"ask","auditTags":["review"],"argumentPatch":{"path":"safe.md"}}',
            b"",
        )
    )
    definition = _definition(
        "command",
        HookScope.USER,
        "user-1",
        command=HookCommandSpec("signed-hook", ("--json",), frozenset({"LANG"})),
    )
    service, _, _, _ = _service((HookLayer(HookScope.USER, "user-1", 1, (definition,)),), {}, process=process)

    outcome = await service.invoke(_invocation(), ManualCancellationToken())

    assert outcome.decision is HookDecision.ASK
    assert process.requests[0].environment == {"LANG": "zh_CN.UTF-8"}
    assert not process.requests[0].allow_network
    assert process.requests[0].owner_kind.value == "hook"
    assert process.requests[0].workspace_id == "workspace-1"
    assert process.requests[0].cwd == ""
    assert process.requests[0].stdin_mode.value == "fixed_payload"
    assert not process.requests[0].allow_artifact_spill
    assert json.loads(process.requests[0].stdin)["tool"]["arguments"]["content"] == "DO-NOT-PERSIST"


@pytest.mark.asyncio
async def test_output_bomb_fails_closed_and_audit_is_redacted() -> None:
    process = ProcessFake(SupervisedProcessResult(0, b"x" * 200, b"stderr-secret", output_truncated=True))
    definition = _definition(
        "bomb",
        HookScope.USER,
        "user-1",
        command=HookCommandSpec("signed-hook"),
        output_limit_bytes=100,
    )
    service, uow, sink, _ = _service(
        (HookLayer(HookScope.USER, "user-1", 1, (definition,)),),
        {},
        process=process,
    )

    outcome = await service.invoke(_invocation(), ManualCancellationToken())

    assert outcome.decision is HookDecision.DENY
    assert outcome.warning_codes == ("hook_output_limit_exceeded",)
    persisted = await uow.list_entities("hook_audits")
    serialized = json.dumps([item.value for item in persisted], ensure_ascii=False)
    serialized += json.dumps([dict(event.payload) for event in sink.events], ensure_ascii=False)
    assert "DO-NOT-PERSIST" not in serialized
    assert "secret-name" not in serialized
    assert "stderr-secret" not in serialized


@pytest.mark.asyncio
async def test_cancellation_propagates_and_cancels_running_handler() -> None:
    handler = WaitingHandler()
    layer = HookLayer(HookScope.USER, "user-1", 1, (_definition("wait", HookScope.USER, "user-1"),))
    service, _, _, _ = _service((layer,), {"wait": handler})
    token = ManualCancellationToken()
    task = asyncio.create_task(service.invoke(_invocation(), token))
    await handler.started.wait()

    token.cancel()
    with pytest.raises(FakeRunCancelled):
        await task

    assert handler.cancelled
