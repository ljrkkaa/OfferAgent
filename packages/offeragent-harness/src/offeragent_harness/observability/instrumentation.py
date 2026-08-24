"""Content-free production instrumentation for model and Tool Kernel boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from threading import RLock

from offeragent_harness.models import ModelEvent, ModelEventKind, ModelRequest
from offeragent_harness.permissions import PolicyContext
from offeragent_harness.ports import CancellationToken, Clock, ModelGateway
from offeragent_harness.tools import ToolCall, ToolDefinition, ToolResult, ToolResultStatus

from .logger import LocalJsonLogger
from .metrics import MetricName, MetricsRegistry
from .models import DataClass, LogField, LogLevel, TraceCorrelation

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ERROR_STATUSES = frozenset(
    {
        ToolResultStatus.FAILED,
        ToolResultStatus.TIMED_OUT,
        ToolResultStatus.PARTIAL,
        ToolResultStatus.UNKNOWN_OUTCOME,
    }
)


@dataclass(frozen=True, slots=True)
class RunCorrelation:
    workspace_id: str
    session_id: str
    turn_id: str
    run_id: str
    parent_run_id: str | None
    trace_id: str


class LocalRunCorrelationRegistry:
    """Bounded local mapping used only to correlate content-free diagnostics."""

    def __init__(self, *, maximum_runs: int = 4096) -> None:
        if maximum_runs < 16:
            raise ValueError("run correlation registry requires at least 16 entries")
        self._maximum_runs = maximum_runs
        self._values: OrderedDict[str, RunCorrelation] = OrderedDict()
        self._lock = RLock()

    def register(
        self,
        *,
        workspace_id: str,
        session_id: str,
        turn_id: str,
        run_id: str,
        parent_run_id: str | None,
        trace_id: str | None = None,
    ) -> RunCorrelation:
        correlation = RunCorrelation(
            workspace_id=_required_id(workspace_id, "workspace_id"),
            session_id=_required_id(session_id, "session_id"),
            turn_id=_required_id(turn_id, "turn_id"),
            run_id=_required_id(run_id, "run_id"),
            parent_run_id=_optional_id(parent_run_id),
            trace_id=_normalized_trace(trace_id, workspace_id=workspace_id, run_id=run_id),
        )
        with self._lock:
            self._values[run_id] = correlation
            self._values.move_to_end(run_id)
            while len(self._values) > self._maximum_runs:
                self._values.popitem(last=False)
        return correlation

    def get(self, run_id: str) -> RunCorrelation | None:
        with self._lock:
            value = self._values.get(run_id)
            if value is not None:
                self._values.move_to_end(run_id)
            return value


class InstrumentedModelGateway:
    """Preserve ModelGateway streaming while recording bounded local metrics."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        provider_id: str,
        workspace_id: str,
        clock: Clock,
        metrics: MetricsRegistry,
        logger: LocalJsonLogger,
        correlations: LocalRunCorrelationRegistry,
    ) -> None:
        if not provider_id:
            raise ValueError("instrumented model gateway requires a provider identity")
        self._gateway = gateway
        self._provider_id = provider_id
        self._workspace_id = _required_id(workspace_id, "workspace_id")
        self._clock = clock
        self._metrics = metrics
        self._logger = logger
        self._correlations = correlations

    async def stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        started = self._clock.monotonic()
        first_output = False
        outcome = "abandoned"
        correlation = _model_correlation(request, self._workspace_id, self._correlations)
        await self._emit(
            LogLevel.INFO,
            "model.started",
            "Model request started.",
            correlation,
            request,
            outcome=None,
            latency_ms=None,
        )
        try:
            async for event in self._gateway.stream(request, cancellation):
                if not first_output and event.kind in {
                    ModelEventKind.TEXT_DELTA,
                    ModelEventKind.REASONING_SUMMARY,
                    ModelEventKind.STRUCTURED_OUTPUT,
                }:
                    first_output = True
                    self._metrics.observe(
                        MetricName.MODEL_FIRST_TOKEN_MS,
                        _elapsed_ms(started, self._clock.monotonic()),
                    )
                if event.kind is ModelEventKind.USAGE and event.usage is not None:
                    self._metrics.increment(MetricName.MODEL_INPUT_TOKENS, event.usage.input_tokens)
                    self._metrics.increment(MetricName.MODEL_OUTPUT_TOKENS, event.usage.output_tokens)
                    if event.usage.cost is not None and event.usage.currency == "USD":
                        self._metrics.increment(MetricName.MODEL_COST_MICROS, float(event.usage.cost * 1_000_000))
                if event.kind is ModelEventKind.COMPLETED:
                    outcome = "completed"
                elif event.kind is ModelEventKind.ERROR:
                    outcome = "failed"
                elif event.kind is ModelEventKind.CANCELLED:
                    outcome = "cancelled"
                yield event
        except BaseException as error:
            if outcome == "abandoned":
                outcome = (
                    "cancelled" if cancellation.cancelled or isinstance(error, asyncio.CancelledError) else "failed"
                )
            raise
        finally:
            latency_ms = _elapsed_ms(started, self._clock.monotonic())
            self._metrics.observe(MetricName.MODEL_LATENCY_MS, latency_ms)
            level = {
                "completed": LogLevel.INFO,
                "cancelled": LogLevel.WARNING,
                "failed": LogLevel.ERROR,
                "abandoned": LogLevel.WARNING,
            }[outcome]
            await self._emit(
                level,
                f"model.{outcome}",
                f"Model request {outcome}.",
                correlation,
                request,
                outcome=outcome,
                latency_ms=latency_ms,
            )

    async def _emit(
        self,
        level: LogLevel,
        event: str,
        message: str,
        correlation: TraceCorrelation,
        request: ModelRequest,
        *,
        outcome: str | None,
        latency_ms: int | None,
    ) -> None:
        fields: dict[str, LogField] = {
            "provider": LogField(self._provider_id, DataClass.IDENTIFIER),
            "model": LogField(request.model, DataClass.IDENTIFIER),
            "purpose": LogField(request.purpose.value, DataClass.PUBLIC),
        }
        if outcome is not None:
            fields["outcome"] = LogField(outcome, DataClass.PUBLIC)
        if latency_ms is not None:
            fields["latencyMs"] = LogField(latency_ms, DataClass.METRIC)
        try:
            await self._logger.emit(
                level,
                event,
                message,
                correlation,
                fields,
                occurred_at=self._clock.utcnow(),
            )
        except Exception:
            # Logging storage failure cannot change a provider response. The
            # diagnostics surface still exposes in-memory metric snapshots.
            return


class ProductionToolObservability:
    """ToolObservabilitySink backed by the shared local logger and registry."""

    def __init__(
        self,
        *,
        clock: Clock,
        metrics: MetricsRegistry,
        logger: LocalJsonLogger,
        correlations: LocalRunCorrelationRegistry,
    ) -> None:
        self._clock = clock
        self._metrics = metrics
        self._logger = logger
        self._correlations = correlations

    async def result_recorded(
        self,
        call: ToolCall,
        definition: ToolDefinition,
        result: ToolResult,
        context: PolicyContext,
        elapsed_ms: int,
    ) -> None:
        self._metrics.observe(MetricName.TOOL_LATENCY_MS, elapsed_ms)
        if result.status in _ERROR_STATUSES:
            self._metrics.increment(MetricName.TOOL_ERRORS)
        level = LogLevel.ERROR if result.status in _ERROR_STATUSES else LogLevel.INFO
        event = "tool.failed" if result.status in _ERROR_STATUSES else "tool.completed"
        error_code = None if result.error is None else result.error.code
        await self._safe_emit(
            level,
            event,
            "Tool invocation reached a terminal result.",
            _tool_correlation(call, context, self._correlations),
            {
                "toolName": LogField(definition.name, DataClass.IDENTIFIER),
                "toolVersion": LogField(definition.version, DataClass.IDENTIFIER),
                "executorLocation": LogField(definition.executor_location.value, DataClass.PUBLIC),
                "status": LogField(result.status.value, DataClass.PUBLIC),
                "errorCode": LogField(error_code, DataClass.IDENTIFIER),
                "latencyMs": LogField(elapsed_ms, DataClass.METRIC),
            },
        )

    async def approval_wait_recorded(
        self,
        call: ToolCall,
        context: PolicyContext,
        elapsed_ms: int,
    ) -> None:
        self._metrics.observe(MetricName.APPROVAL_WAIT_MS, elapsed_ms)
        await self._safe_emit(
            LogLevel.INFO,
            "approval.resolved",
            "Tool approval wait ended.",
            _tool_correlation(call, context, self._correlations),
            {"waitMs": LogField(elapsed_ms, DataClass.METRIC)},
        )

    async def _safe_emit(
        self,
        level: LogLevel,
        event: str,
        message: str,
        correlation: TraceCorrelation,
        fields: Mapping[str, LogField],
    ) -> None:
        try:
            await self._logger.emit(
                level,
                event,
                message,
                correlation,
                fields,
                occurred_at=self._clock.utcnow(),
            )
        except Exception:
            return


class ProductionRunObservability:
    """Synchronous TurnManager observer for active, depth and cancel latency metrics."""

    def __init__(self, *, clock: Clock, metrics: MetricsRegistry) -> None:
        self._clock = clock
        self._metrics = metrics
        self._cancel_started: dict[str, float] = {}
        self._maximum_depth = 0
        self._lock = RLock()

    def active_runs_changed(self, count: int) -> None:
        self._metrics.set_gauge(MetricName.ACTIVE_RUNS, count)

    def cancellation_requested(self, run_id: str) -> None:
        with self._lock:
            self._cancel_started.setdefault(run_id, self._clock.monotonic())

    def run_finished(self, run_id: str) -> None:
        with self._lock:
            started = self._cancel_started.pop(run_id, None)
        if started is not None:
            self._metrics.observe(
                MetricName.CANCELLATION_LATENCY_MS,
                _elapsed_ms(started, self._clock.monotonic()),
            )

    def run_depth_registered(self, depth: int) -> None:
        if depth < 0:
            raise ValueError("Run depth cannot be negative")
        with self._lock:
            self._maximum_depth = max(self._maximum_depth, depth)
            maximum = self._maximum_depth
        self._metrics.set_gauge(MetricName.SUBAGENT_DEPTH, maximum)


def _model_correlation(
    request: ModelRequest,
    workspace_id: str,
    correlations: LocalRunCorrelationRegistry,
) -> TraceCorrelation:
    run_id = _metadata_id(request.metadata, "runId")
    registered = None if run_id is None else correlations.get(run_id)
    return TraceCorrelation(
        trace_id=_normalized_trace(request.trace_context.trace_id, workspace_id=workspace_id, run_id=run_id),
        workspace_id=workspace_id,
        session_id=(registered.session_id if registered is not None else _metadata_id(request.metadata, "sessionId")),
        turn_id=(registered.turn_id if registered is not None else _metadata_id(request.metadata, "turnId")),
        run_id=run_id,
        parent_run_id=None if registered is None else registered.parent_run_id,
        tool_call_id=_metadata_id(request.metadata, "toolCallId") if run_id is not None else None,
    )


def _tool_correlation(
    call: ToolCall,
    context: PolicyContext,
    correlations: LocalRunCorrelationRegistry,
) -> TraceCorrelation:
    registered = correlations.get(call.run_id)
    return TraceCorrelation(
        trace_id=(
            registered.trace_id
            if registered is not None
            else _normalized_trace(None, workspace_id=call.workspace_id, run_id=call.run_id)
        ),
        workspace_id=call.workspace_id,
        session_id=registered.session_id if registered is not None else context.session_id,
        turn_id=None if registered is None else registered.turn_id,
        run_id=call.run_id,
        parent_run_id=call.lineage.parent_run_id,
        tool_call_id=call.tool_call_id,
    )


def _metadata_id(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) and _ID.fullmatch(value) is not None else None


def _required_id(value: str, name: str) -> str:
    if _ID.fullmatch(value) is None:
        raise ValueError(f"invalid local observability {name}")
    return value


def _optional_id(value: str | None) -> str | None:
    if value is None:
        return None
    return _required_id(value, "optional identifier")


def _normalized_trace(value: str | None, *, workspace_id: str, run_id: str | None) -> str:
    if value is not None and _ID.fullmatch(value) is not None:
        return value
    digest = hashlib.sha256(f"offeragent-trace-v1\0{workspace_id}\0{run_id or 'runtime'}".encode()).hexdigest()
    return f"trace_{digest[:32]}"


def _elapsed_ms(started: float, finished: float) -> int:
    return max(0, round((finished - started) * 1_000))


__all__ = [
    "InstrumentedModelGateway",
    "LocalRunCorrelationRegistry",
    "ProductionRunObservability",
    "ProductionToolObservability",
    "RunCorrelation",
]
