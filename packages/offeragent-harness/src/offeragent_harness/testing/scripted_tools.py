"""Exact scripted tool execution with a durable-in-memory invocation journal."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from offeragent_harness.ports import CancellationToken, InvocationJournalConflict
from offeragent_harness.tools import ToolCall, ToolResult

from .barrier import ControlledBarrier
from .errors import AcknowledgementLost, ScriptMismatch, ScriptNotExhausted


@dataclass(frozen=True)
class ToolScriptStep:
    expected_call: ToolCall
    result: ToolResult
    barrier: ControlledBarrier | None = None
    acknowledgement_losses: int = 0

    def __post_init__(self) -> None:
        if self.result.tool_call_id != self.expected_call.tool_call_id:
            raise ValueError("scripted result tool_call_id must match the expected call")
        if self.acknowledgement_losses < 0:
            raise ValueError("acknowledgement loss count cannot be negative")


@dataclass
class _CompletedInvocation:
    fingerprint: str
    result: ToolResult
    remaining_ack_losses: int


@dataclass
class _InFlightInvocation:
    fingerprint: str
    future: asyncio.Future[ToolResult]


class ScriptedToolExecutor:
    """A fake executor driven solely by exact ToolCall scripts.

    Calls may arrive concurrently and are matched by full dataclass equality.
    An idempotency replay is resolved before script matching, so an ACK-lost write
    returns its journaled result without consuming or executing another step.
    """

    def __init__(self, steps: Sequence[ToolScriptStep]) -> None:
        self._remaining = list(steps)
        self._journal: dict[tuple[str, str], _CompletedInvocation] = {}
        self._in_flight: dict[tuple[str, str], _InFlightInvocation] = {}
        self._lock = asyncio.Lock()
        self.calls: list[ToolCall] = []
        self.executed_calls: list[ToolCall] = []
        self.replayed_calls: list[ToolCall] = []

    async def _wait_for_inflight(
        self,
        future: asyncio.Future[ToolResult],
        cancellation: CancellationToken,
    ) -> ToolResult:
        cancellation.checkpoint()
        cancel_wait = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait((future, cancel_wait), return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait in done:
                cancellation.checkpoint()
            return await asyncio.shield(future)
        finally:
            if not cancel_wait.done():
                cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        key = (call.workspace_id, call.idempotency_key)
        fingerprint = call.idempotency_fingerprint
        self.calls.append(call)

        leader = False
        step: ToolScriptStep | None = None
        step_index = -1
        in_flight_future: asyncio.Future[ToolResult]
        async with self._lock:
            completed = self._journal.get(key)
            if completed is not None:
                if completed.fingerprint != fingerprint:
                    raise InvocationJournalConflict(f"idempotency key {key!r} was bound to different arguments")
                self.replayed_calls.append(call)
                if completed.remaining_ack_losses > 0:
                    completed.remaining_ack_losses -= 1
                    raise AcknowledgementLost(
                        f"journaled tool result replayed but ACK was lost for {call.idempotency_key}"
                    )
                return completed.result

            current = self._in_flight.get(key)
            if current is not None:
                if current.fingerprint != fingerprint:
                    raise InvocationJournalConflict(f"in-flight idempotency key {key!r} has different arguments")
                in_flight_future = current.future
            else:
                for index, candidate in enumerate(self._remaining):
                    if candidate.expected_call == call:
                        step_index = index
                        step = self._remaining.pop(index)
                        break
                if step is None:
                    expected = "\n".join(repr(item.expected_call) for item in self._remaining) or "<script exhausted>"
                    raise ScriptMismatch(f"unexpected tool call:\n{call!r}\nremaining exact calls:\n{expected}")
                in_flight_future = asyncio.get_running_loop().create_future()
                self._in_flight[key] = _InFlightInvocation(fingerprint, in_flight_future)
                leader = True

        if not leader:
            result = await self._wait_for_inflight(in_flight_future, cancellation)
            self.replayed_calls.append(call)
            return result

        assert step is not None
        try:
            if step.barrier is not None:
                await step.barrier.arrive_and_wait(cancellation)
            cancellation.checkpoint()
        except BaseException:
            async with self._lock:
                self._in_flight.pop(key, None)
                self._remaining.insert(step_index, step)
                if not in_flight_future.done():
                    in_flight_future.cancel()
            raise

        async with self._lock:
            self.executed_calls.append(call)
            completed = _CompletedInvocation(fingerprint, step.result, step.acknowledgement_losses)
            self._journal[key] = completed
            self._in_flight.pop(key, None)
            if not in_flight_future.done():
                in_flight_future.set_result(step.result)
            lose_ack = completed.remaining_ack_losses > 0
            if lose_ack:
                completed.remaining_ack_losses -= 1
        if lose_ack:
            raise AcknowledgementLost(f"tool result committed but ACK was lost for {call.idempotency_key}")
        return step.result

    def journal_result(self, workspace_id: str, idempotency_key: str) -> ToolResult | None:
        completed = self._journal.get((workspace_id, idempotency_key))
        return None if completed is None else completed.result

    def assert_exhausted(self) -> None:
        if self._remaining:
            raise ScriptNotExhausted(f"{len(self._remaining)} tool script step(s) remain")


__all__ = ["ScriptedToolExecutor", "ToolScriptStep"]
