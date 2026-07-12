from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from offeragent_harness.models import (
    ModelContentBlock,
    ModelEvent,
    ModelEventKind,
    ModelFinishReason,
    ModelMessage,
    ModelOutputMode,
    ModelPurpose,
    ModelRequest,
    ModelRole,
    TraceContext,
)
from offeragent_harness.ports import CancellationToken, InvocationJournalConflict, NewEvent, StoredEvent
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import (
    AcknowledgementLost,
    ControlledBarrier,
    DeterministicIdGenerator,
    FakeRunCancelled,
    ManualCancellationToken,
    ManualClock,
    ModelScriptStep,
    RecordingEventSink,
    ScriptedModelEvent,
    ScriptedModelGateway,
    ScriptedToolExecutor,
    ScriptMismatch,
    ToolScriptStep,
)
from offeragent_harness.tools import ToolCall, ToolResult, ToolResultStatus, canonical_json_sha256

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def request(request_id: str, text: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        model="fake-model",
        purpose=ModelPurpose.PLANNING,
        messages=(ModelMessage(ModelRole.USER, (ModelContentBlock.text(text),)),),
        output_mode=ModelOutputMode.TEXT,
        output_schema=None,
        max_output_tokens=100,
        reasoning_effort="high",
        temperature=0,
        seed=1,
        trace_context=TraceContext("trace_1"),
    )


def call(number: int, *, value: int | None = None, idempotency_key: str | None = None) -> ToolCall:
    arguments = {"value": number if value is None else value}
    return ToolCall(
        tool_call_id=f"call_{number}",
        run_id="run_1",
        workspace_id="ws_1",
        name="fake.tool",
        version="1",
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=idempotency_key or f"idem_{number}",
        deadline=None,
        lineage=AgentLineage.root("run_1"),
    )


def result(tool_call_id: str, value: int) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        data={"value": value},
        user_visible_summary=f"returned {value}",
        artifact_ids=(),
        source_refs=(),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state=None,
        error=None,
    )


@pytest.mark.asyncio
async def test_scripted_model_matches_the_entire_request_and_streams_each_event() -> None:
    expected = request("req_1", "exact text")
    events = (
        ModelEvent("req_1", 1, ModelEventKind.STARTED),
        ModelEvent("req_1", 2, ModelEventKind.TEXT_DELTA, text="a"),
        ModelEvent("req_1", 3, ModelEventKind.TEXT_DELTA, text="b"),
        ModelEvent("req_1", 4, ModelEventKind.COMPLETED, finish_reason=ModelFinishReason.STOP),
    )
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(expected, events),))
    token = ManualCancellationToken()

    assert isinstance(token, CancellationToken)
    assert [event async for event in gateway.stream(expected, token)] == list(events)
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_scripted_model_does_not_use_prompt_keyword_matching() -> None:
    expected = request("req_1", "write a note")
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(expected, ()),))
    with pytest.raises(ScriptMismatch, match="mismatch"):
        _ = [
            event
            async for event in gateway.stream(request("req_1", "write a different note"), ManualCancellationToken())
        ]


@pytest.mark.asyncio
async def test_model_stream_cancellation_stops_at_an_explicit_barrier() -> None:
    expected = request("req_1", "cancel me")
    barrier = ControlledBarrier("model-event-1")
    scripted = ScriptedModelEvent(ModelEvent("req_1", 1, ModelEventKind.TEXT_DELTA, text="never"), barrier)
    gateway = ScriptedModelGateway((ModelScriptStep(expected, (scripted,)),))
    token = ManualCancellationToken()
    stream = gateway.stream(expected, token)
    next_event: asyncio.Future[ModelEvent] = asyncio.ensure_future(anext(stream))
    await barrier.wait_for_arrivals(1)
    token.cancel()

    with pytest.raises(FakeRunCancelled):
        await next_event
    assert gateway.emitted_events == []


@pytest.mark.asyncio
async def test_tool_ack_loss_replays_journaled_result_without_second_execution() -> None:
    expected = call(1)
    expected_result = result("call_1", 10)
    executor = ScriptedToolExecutor((ToolScriptStep(expected, expected_result, acknowledgement_losses=1),))
    token = ManualCancellationToken()

    with pytest.raises(AcknowledgementLost):
        await executor.execute(expected, token)
    assert executor.journal_result("ws_1", "idem_1") == expected_result
    assert await executor.execute(expected, token) == expected_result
    assert executor.executed_calls == [expected]
    assert executor.replayed_calls == [expected]
    executor.assert_exhausted()


@pytest.mark.asyncio
async def test_repeated_ack_loss_never_reexecutes_the_tool() -> None:
    expected = call(1)
    expected_result = result("call_1", 10)
    executor = ScriptedToolExecutor((ToolScriptStep(expected, expected_result, acknowledgement_losses=2),))
    with pytest.raises(AcknowledgementLost):
        await executor.execute(expected, ManualCancellationToken())
    with pytest.raises(AcknowledgementLost):
        await executor.execute(expected, ManualCancellationToken())
    assert await executor.execute(expected, ManualCancellationToken()) == expected_result
    assert executor.executed_calls == [expected]


@pytest.mark.asyncio
async def test_idempotency_key_cannot_be_rebound_to_new_arguments() -> None:
    first = call(1, idempotency_key="same")
    executor = ScriptedToolExecutor((ToolScriptStep(first, result("call_1", 1)),))
    await executor.execute(first, ManualCancellationToken())
    changed = call(1, value=999, idempotency_key="same")
    with pytest.raises(InvocationJournalConflict):
        await executor.execute(changed, ManualCancellationToken())


@pytest.mark.asyncio
async def test_tool_concurrency_is_proven_with_a_barrier_not_timing_or_keywords() -> None:
    shared = ControlledBarrier("two-tools-entered")
    first = call(1)
    second = call(2)
    executor = ScriptedToolExecutor(
        (
            ToolScriptStep(first, result("call_1", 1), barrier=shared),
            ToolScriptStep(second, result("call_2", 2), barrier=shared),
        )
    )
    token = ManualCancellationToken()
    first_task = asyncio.create_task(executor.execute(first, token))
    second_task = asyncio.create_task(executor.execute(second, token))
    await shared.wait_for_arrivals(2)
    assert not first_task.done() and not second_task.done()
    shared.release()
    first_result, second_result = await asyncio.gather(first_task, second_task)
    assert first_result == result("call_1", 1)
    assert second_result == result("call_2", 2)
    executor.assert_exhausted()


@pytest.mark.asyncio
async def test_manual_clock_and_ids_have_no_wall_clock_or_random_dependency() -> None:
    clock = ManualClock(NOW)
    sleeper = asyncio.create_task(clock.sleep_until(NOW + timedelta(seconds=5)))
    await asyncio.sleep(0)
    assert not sleeper.done()
    clock.advance(timedelta(seconds=5))
    await sleeper
    assert clock.utcnow() == NOW + timedelta(seconds=5)
    assert clock.monotonic() == 5

    ids = DeterministicIdGenerator(start=7, width=3)
    assert [ids.new_id("run"), ids.new_id("event"), ids.new_id("run")] == ["run_007", "event_007", "run_008"]


@pytest.mark.asyncio
async def test_recording_sink_deduplicates_retry_after_delivery_ack_loss() -> None:
    new = NewEvent("evt_1", "phase.changed", {}, NOW, False, "key_1")
    stored = StoredEvent.from_new("run_1", 1, new)
    sink = RecordingEventSink(acknowledgement_loss_calls=frozenset({1}))
    with pytest.raises(AcknowledgementLost):
        await sink.publish((stored,))
    await sink.publish((stored,))
    assert sink.events == [stored]
    assert len(sink.attempts) == 2
