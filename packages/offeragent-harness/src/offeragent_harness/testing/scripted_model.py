"""Exact, event-by-event model scripts with explicit cancellation gates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from offeragent_harness.models import ModelEvent, ModelRequest
from offeragent_harness.ports import CancellationToken

from .barrier import ControlledBarrier
from .errors import ScriptMismatch, ScriptNotExhausted


@dataclass(frozen=True)
class ScriptedModelEvent:
    event: ModelEvent
    barrier: ControlledBarrier | None = None


@dataclass(frozen=True)
class ModelScriptStep:
    expected_request: ModelRequest
    events: tuple[ScriptedModelEvent, ...]

    def __post_init__(self) -> None:
        previous = 0
        for scripted in self.events:
            event = scripted.event
            if event.request_id != self.expected_request.request_id:
                raise ValueError("scripted model event request_id does not match its request")
            if event.sequence <= previous:
                raise ValueError("scripted model event sequence must be strictly increasing")
            previous = event.sequence

    @classmethod
    def from_events(cls, expected_request: ModelRequest, events: Sequence[ModelEvent]) -> ModelScriptStep:
        return cls(expected_request, tuple(ScriptedModelEvent(event) for event in events))


class ScriptedModelGateway:
    """Consumes an ordered script and rejects any non-identical request."""

    def __init__(self, steps: Sequence[ModelScriptStep]) -> None:
        self._steps = tuple(steps)
        self._next_step = 0
        self._lock = asyncio.Lock()
        self.requests: list[ModelRequest] = []
        self.emitted_events: list[ModelEvent] = []

    async def _claim_step(self, request: ModelRequest) -> ModelScriptStep:
        async with self._lock:
            if self._next_step >= len(self._steps):
                raise ScriptMismatch(f"unexpected model request after script exhaustion: {request!r}")
            step = self._steps[self._next_step]
            if request != step.expected_request:
                raise ScriptMismatch(
                    f"model request #{self._next_step + 1} mismatch\n"
                    f"expected: {step.expected_request!r}\nactual:   {request!r}"
                )
            self._next_step += 1
            self.requests.append(request)
            return step

    async def stream(self, request: ModelRequest, cancellation: CancellationToken) -> AsyncIterator[ModelEvent]:
        step = await self._claim_step(request)
        for scripted in step.events:
            cancellation.checkpoint()
            if scripted.barrier is not None:
                await scripted.barrier.arrive_and_wait(cancellation)
            cancellation.checkpoint()
            self.emitted_events.append(scripted.event)
            yield scripted.event

    def assert_exhausted(self) -> None:
        if self._next_step != len(self._steps):
            raise ScriptNotExhausted(f"{len(self._steps) - self._next_step} model script step(s) remain")


__all__ = ["ModelScriptStep", "ScriptedModelEvent", "ScriptedModelGateway"]
