"""Bounded local metrics with exact names and percentile snapshots."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum
from threading import RLock


class MetricName(str, Enum):
    MODEL_LATENCY_MS = "model.latency_ms"
    MODEL_FIRST_TOKEN_MS = "model.first_token_ms"
    TOOL_LATENCY_MS = "tool.latency_ms"
    MODEL_INPUT_TOKENS = "model.input_tokens"
    MODEL_OUTPUT_TOKENS = "model.output_tokens"
    MODEL_COST_MICROS = "model.cost_micros"
    TOOL_ERRORS = "tool.errors"
    APPROVAL_WAIT_MS = "approval.wait_ms"
    CANCELLATION_LATENCY_MS = "cancellation.latency_ms"
    SUBAGENT_DEPTH = "subagent.depth"
    ACTIVE_RUNS = "run.active"
    EVENT_QUEUE_LENGTH = "event.queue_length"


_HISTOGRAMS = frozenset(
    {
        MetricName.MODEL_LATENCY_MS,
        MetricName.MODEL_FIRST_TOKEN_MS,
        MetricName.TOOL_LATENCY_MS,
        MetricName.APPROVAL_WAIT_MS,
        MetricName.CANCELLATION_LATENCY_MS,
    }
)
_COUNTERS = frozenset(
    {
        MetricName.MODEL_INPUT_TOKENS,
        MetricName.MODEL_OUTPUT_TOKENS,
        MetricName.MODEL_COST_MICROS,
        MetricName.TOOL_ERRORS,
    }
)
_GAUGES = frozenset(
    {
        MetricName.SUBAGENT_DEPTH,
        MetricName.ACTIVE_RUNS,
        MetricName.EVENT_QUEUE_LENGTH,
    }
)


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    name: MetricName
    kind: str
    count: int
    total: float
    current: float | None
    p50: float | None
    p95: float | None
    p99: float | None


class MetricsRegistry:
    def __init__(self, *, reservoir_size: int = 4096) -> None:
        if reservoir_size < 16:
            raise ValueError("metrics reservoir_size must be >= 16")
        self._reservoir_size = reservoir_size
        self._samples: dict[MetricName, deque[float]] = {name: deque(maxlen=reservoir_size) for name in _HISTOGRAMS}
        self._counters = {name: 0.0 for name in _COUNTERS}
        self._gauges = {name: 0.0 for name in _GAUGES}
        self._lock = RLock()

    def observe(self, name: MetricName, value: float) -> None:
        numeric = _value(value)
        if name not in _HISTOGRAMS:
            raise ValueError(f"{name.value} is not a histogram")
        with self._lock:
            self._samples[name].append(numeric)

    def increment(self, name: MetricName, value: float = 1) -> None:
        numeric = _value(value)
        if name not in _COUNTERS or numeric < 0:
            raise ValueError(f"{name.value} is not a non-negative counter")
        with self._lock:
            self._counters[name] += numeric

    def set_gauge(self, name: MetricName, value: float) -> None:
        numeric = _value(value)
        if name not in _GAUGES or numeric < 0:
            raise ValueError(f"{name.value} is not a non-negative gauge")
        with self._lock:
            self._gauges[name] = numeric

    def snapshots(self) -> tuple[MetricSnapshot, ...]:
        with self._lock:
            values: list[MetricSnapshot] = []
            for name in sorted(_HISTOGRAMS, key=lambda item: item.value):
                samples = sorted(self._samples[name])
                values.append(
                    MetricSnapshot(
                        name,
                        "histogram",
                        len(samples),
                        sum(samples),
                        samples[-1] if samples else None,
                        _percentile(samples, 0.50),
                        _percentile(samples, 0.95),
                        _percentile(samples, 0.99),
                    )
                )
            for name in sorted(_COUNTERS, key=lambda item: item.value):
                total = self._counters[name]
                values.append(MetricSnapshot(name, "counter", 0, total, total, None, None, None))
            for name in sorted(_GAUGES, key=lambda item: item.value):
                current = self._gauges[name]
                values.append(MetricSnapshot(name, "gauge", 0, current, current, None, None, None))
            return tuple(values)


def _value(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError("metric value must be a finite number")
    return float(value)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    index = max(0, math.ceil(quantile * len(values)) - 1)
    return values[index]


__all__ = ["MetricName", "MetricSnapshot", "MetricsRegistry"]
