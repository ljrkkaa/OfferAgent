from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal

import pytest

from offeragent_harness.agent.composer import CompositionEvent
from offeragent_harness.agent.context_manager import (
    ContextBudget,
    ContextFragment,
    ContextInputs,
    ContextLayer,
    ContextManager,
    ContextProjection,
    ContextVisibilityPolicy,
)
from offeragent_harness.agent.model_composer import ComposerModelConfig, CompositionIncomplete, ModelComposer
from offeragent_harness.agent.model_planner import ModelProviderFailure, ModelStreamProtocolError
from offeragent_harness.agent.state import RunState
from offeragent_harness.models import (
    ModelError,
    ModelEvent,
    ModelEventKind,
    ModelFinishReason,
    ModelRequest,
    ModelUsage,
    thaw_json,
)
from offeragent_harness.ports import ModelGateway, Sensitivity
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import (
    ControlledBarrier,
    DeterministicIdGenerator,
    FakeRunCancelled,
    ManualCancellationToken,
    ModelScriptStep,
    ScriptedModelEvent,
    ScriptedModelGateway,
)

USAGE = ModelUsage(20, 8, 4, 2, Decimal("0.04"), "USD")


def _context() -> ContextManager:
    return ContextManager(
        system_rules=("Compose only from verified evidence.",),
        inputs=ContextInputs(
            user_input=(
                ContextFragment(
                    "user-1",
                    ContextLayer.USER_INPUT,
                    "summarize the result",
                    Sensitivity.PUBLIC,
                ),
            )
        ),
        visibility=ContextVisibilityPolicy.local_model(),
        budget=ContextBudget.generous_default(),
    )


def _state() -> RunState:
    return RunState("ws", "session", "turn", "run", AgentLineage.root("run"))


def _composer(gateway: ModelGateway, *, ids: DeterministicIdGenerator | None = None) -> ModelComposer:
    return ModelComposer(
        gateway=gateway,
        context_manager=_context(),
        config=ComposerModelConfig("scripted-model", 256, seed=23),
        ids=ids or DeterministicIdGenerator(),
    )


def _request(*, partial: bool = False) -> ModelRequest:
    return _composer(ScriptedModelGateway(())).create_request(_state(), partial=partial)


def test_memory_context_enrichment_reaches_composer_as_untrusted_data() -> None:
    composer = _composer(ScriptedModelGateway(()))
    before = composer.create_request(_state(), partial=False)
    memory = ContextFragment(
        "memory:session:mem_composer",
        ContextLayer.MEMORY,
        "composer local Memory evidence",
        Sensitivity.PRIVATE,
        source_refs=("memory:session:mem_composer",),
        content_hash="sha256:" + "d" * 64,
    )

    composer.add_memory_context((memory, memory))
    after = composer.create_request(_state(), partial=False)

    assert "composer local Memory evidence" not in repr(before.messages)
    assert repr(after.messages).count("composer local Memory evidence") == 1
    assert "不可信数据" in repr(after.messages)


def _normal_events(request: ModelRequest) -> tuple[ModelEvent, ...]:
    return (
        ModelEvent(request.request_id, 1, ModelEventKind.STARTED),
        ModelEvent(request.request_id, 2, ModelEventKind.TEXT_DELTA, text="第一段"),
        ModelEvent(request.request_id, 3, ModelEventKind.TEXT_DELTA, text=", second"),
        ModelEvent(request.request_id, 4, ModelEventKind.USAGE, usage=USAGE),
        ModelEvent(request.request_id, 5, ModelEventKind.COMPLETED, finish_reason=ModelFinishReason.STOP),
    )


async def _collect(stream: AsyncIterator[CompositionEvent]) -> list[CompositionEvent]:
    return [event async for event in stream]


def test_partial_request_is_explicit_and_never_claims_unverified_writes() -> None:
    request = _request(partial=True)

    assert thaw_json(request.metadata)["partial"] is True
    names = [message.name for message in request.messages]
    assert names[:3] == [
        "offeragent-system-rules",
        "offeragent-run-snapshot",
        "offeragent-partial-composition",
    ]
    assert "未执行、未审批或未知结果的写入" in repr(request.messages[2])


@pytest.mark.asyncio
async def test_text_is_yielded_before_next_provider_event_is_requested() -> None:
    request = _request()
    barrier = ControlledBarrier("second-delta")
    events = _normal_events(request)
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep(
                request,
                tuple(ScriptedModelEvent(event, barrier if event.sequence == 3 else None) for event in events),
            ),
        )
    )
    stream = _composer(gateway).stream(_state(), partial=False, cancellation=ManualCancellationToken())

    first = await anext(stream)
    assert first == CompositionEvent(text_delta="第一段")
    assert [event.sequence for event in gateway.emitted_events] == [1, 2]

    second_task: asyncio.Future[CompositionEvent] = asyncio.ensure_future(anext(stream))
    await barrier.wait_for_arrivals(1)
    assert [event.sequence for event in gateway.emitted_events] == [1, 2]
    barrier.release()
    second = await second_task
    assert second == CompositionEvent(text_delta=", second")

    remainder = await _collect(stream)
    assert remainder == [CompositionEvent(usage=USAGE)]
    assert [event.sequence for event in gateway.emitted_events] == [1, 2, 3, 4, 5]
    gateway.assert_exhausted()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events, message",
    (
        (
            lambda request: (
                ModelEvent(request.request_id, 1, ModelEventKind.STARTED),
                ModelEvent(request.request_id, 3, ModelEventKind.TEXT_DELTA, text="gap"),
            ),
            "expected sequence 2",
        ),
        (
            lambda request: (
                ModelEvent(request.request_id, 1, ModelEventKind.STARTED),
                ModelEvent(request.request_id, 2, ModelEventKind.TEXT_DELTA, text="text"),
                ModelEvent(request.request_id, 3, ModelEventKind.USAGE, usage=USAGE),
            ),
            "before completed",
        ),
        (
            lambda request: (
                ModelEvent(request.request_id, 1, ModelEventKind.STARTED),
                ModelEvent(request.request_id, 2, ModelEventKind.STRUCTURED_OUTPUT, data={"not": "text"}),
            ),
            "structured output is invalid",
        ),
        (
            lambda request: (
                ModelEvent(request.request_id, 1, ModelEventKind.STARTED),
                ModelEvent(request.request_id, 2, ModelEventKind.TEXT_DELTA, text="text"),
                ModelEvent(request.request_id, 3, ModelEventKind.USAGE, usage=USAGE),
                ModelEvent(request.request_id, 4, ModelEventKind.COMPLETED, finish_reason=ModelFinishReason.STOP),
                ModelEvent(request.request_id, 5, ModelEventKind.TEXT_DELTA, text="too late"),
            ),
            "after completed",
        ),
    ),
)
async def test_sequence_completion_and_output_mode_are_strict(
    events: object,
    message: str,
) -> None:
    request = _request()
    event_factory = events
    assert callable(event_factory)
    scripted = event_factory(request)
    assert isinstance(scripted, Sequence)
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(request, scripted),))

    with pytest.raises(ModelStreamProtocolError, match=message):
        await _collect(_composer(gateway).stream(_state(), partial=False, cancellation=ManualCancellationToken()))

    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_usage_must_be_monotonic_and_is_emitted_exactly_once() -> None:
    request = _request()
    previous = ModelUsage(20, 8, 4, 2, Decimal("0.04"), "USD")
    regressed = ModelUsage(19, 8, 4, 2, Decimal("0.04"), "USD")
    events = (
        ModelEvent(request.request_id, 1, ModelEventKind.STARTED),
        ModelEvent(request.request_id, 2, ModelEventKind.TEXT_DELTA, text="text"),
        ModelEvent(request.request_id, 3, ModelEventKind.USAGE, usage=previous),
        ModelEvent(request.request_id, 4, ModelEventKind.USAGE, usage=regressed),
    )
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(request, events),))
    with pytest.raises(ModelStreamProtocolError, match="monotonic"):
        await _collect(_composer(gateway).stream(_state(), partial=False, cancellation=ManualCancellationToken()))

    success_request = _request()
    success_gateway = ScriptedModelGateway(
        (ModelScriptStep.from_events(success_request, _normal_events(success_request)),)
    )
    result = await _collect(
        _composer(success_gateway).stream(_state(), partial=False, cancellation=ManualCancellationToken())
    )
    assert [event.text_delta for event in result if event.text_delta is not None] == ["第一段", ", second"]
    assert [event.usage for event in result if event.usage is not None] == [USAGE]


@pytest.mark.asyncio
async def test_non_stop_finish_emits_usage_then_raises_with_partial_text() -> None:
    request = _request()
    events = list(_normal_events(request))
    events[-1] = ModelEvent(
        request.request_id,
        5,
        ModelEventKind.COMPLETED,
        finish_reason=ModelFinishReason.LENGTH,
    )
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(request, events),))
    stream = _composer(gateway).stream(_state(), partial=False, cancellation=ManualCancellationToken())

    assert (await anext(stream)).text_delta == "第一段"
    assert (await anext(stream)).text_delta == ", second"
    assert (await anext(stream)).usage == USAGE
    with pytest.raises(CompositionIncomplete) as caught:
        await anext(stream)

    assert caught.value.finish_reason is ModelFinishReason.LENGTH
    assert caught.value.partial_text == "第一段, second"


@pytest.mark.asyncio
async def test_provider_error_emits_observed_usage_before_typed_failure() -> None:
    request = _request()
    provider_error = ModelError("content_filter", "blocked", False, False)
    events = (
        ModelEvent(request.request_id, 1, ModelEventKind.STARTED),
        ModelEvent(request.request_id, 2, ModelEventKind.TEXT_DELTA, text="partial"),
        ModelEvent(request.request_id, 3, ModelEventKind.USAGE, usage=USAGE),
        ModelEvent(request.request_id, 4, ModelEventKind.ERROR, error=provider_error),
    )
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(request, events),))
    stream = _composer(gateway).stream(_state(), partial=False, cancellation=ManualCancellationToken())

    assert (await anext(stream)).text_delta == "partial"
    assert (await anext(stream)).usage == USAGE
    with pytest.raises(ModelProviderFailure) as caught:
        await anext(stream)
    assert caught.value.error is provider_error


@pytest.mark.asyncio
async def test_context_overflow_retries_once_before_any_text_and_preserves_each_usage() -> None:
    builder = _composer(ScriptedModelGateway(()))
    first = builder.create_request(_state(), partial=False)
    retry = builder.create_request(
        _state(),
        partial=False,
        projection=ContextProjection.OVERFLOW_REFERENCES,
        retry_of_request_id=first.request_id,
    )
    overflow = ModelError("context_overflow", "provider context is full", False, False)
    first_events = (
        ModelEvent(first.request_id, 1, ModelEventKind.STARTED),
        ModelEvent(first.request_id, 2, ModelEventKind.USAGE, usage=USAGE),
        ModelEvent(first.request_id, 3, ModelEventKind.ERROR, error=overflow),
    )
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep.from_events(first, first_events),
            ModelScriptStep.from_events(retry, _normal_events(retry)),
        )
    )

    events = await _collect(_composer(gateway).stream(_state(), partial=False, cancellation=ManualCancellationToken()))

    assert events[0] == CompositionEvent(usage=USAGE)
    assert events[1].retry is not None
    assert events[1].retry.request_id == retry.request_id
    assert events[1].retry.retry_of_request_id == first.request_id
    assert events[1].retry.projection == "overflow_references"
    assert events[1].retry.projection_hash is not None
    assert [event.text_delta for event in events if event.text_delta is not None] == ["第一段", ", second"]
    assert [event.usage for event in events if event.usage is not None] == [USAGE, USAGE]
    metadata = thaw_json(gateway.requests[1].metadata)
    assert metadata["contextOverflowRetry"] is True
    assert metadata["retryOfRequestId"] == first.request_id
    assert metadata["projection"] == "overflow_references"
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_context_overflow_after_text_delta_is_not_retried() -> None:
    request = _request()
    overflow = ModelError("context_overflow", "provider context is full", False, False)
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep.from_events(
                request,
                (
                    ModelEvent(request.request_id, 1, ModelEventKind.STARTED),
                    ModelEvent(request.request_id, 2, ModelEventKind.TEXT_DELTA, text="visible"),
                    ModelEvent(request.request_id, 3, ModelEventKind.USAGE, usage=USAGE),
                    ModelEvent(request.request_id, 4, ModelEventKind.ERROR, error=overflow),
                ),
            ),
        )
    )
    stream = _composer(gateway).stream(_state(), partial=False, cancellation=ManualCancellationToken())

    assert await anext(stream) == CompositionEvent(text_delta="visible")
    assert await anext(stream) == CompositionEvent(usage=USAGE)
    with pytest.raises(ModelProviderFailure) as caught:
        await anext(stream)
    assert caught.value.error.code == "context_overflow"
    assert len(gateway.requests) == 1
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_cancellation_at_provider_barrier_stops_stream_immediately() -> None:
    request = _request()
    barrier = ControlledBarrier("composer-delta")
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep(
                request,
                (
                    ScriptedModelEvent(ModelEvent(request.request_id, 1, ModelEventKind.STARTED)),
                    ScriptedModelEvent(
                        ModelEvent(request.request_id, 2, ModelEventKind.TEXT_DELTA, text="never emitted"),
                        barrier,
                    ),
                ),
            ),
        )
    )
    cancellation = ManualCancellationToken()
    stream = _composer(gateway).stream(_state(), partial=False, cancellation=cancellation)
    task: asyncio.Future[CompositionEvent] = asyncio.ensure_future(anext(stream))
    await barrier.wait_for_arrivals(1)

    cancellation.cancel()
    with pytest.raises(FakeRunCancelled):
        await task

    assert [event.kind for event in gateway.emitted_events] == [ModelEventKind.STARTED]
    gateway.assert_exhausted()
