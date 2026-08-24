from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

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
    ModelUsage,
    TraceContext,
)
from offeragent_harness.observability import (
    InstrumentedModelGateway,
    LocalJsonLogger,
    LocalRunCorrelationRegistry,
    MetricName,
    MetricSnapshot,
    MetricsRegistry,
    ProductionToolObservability,
)
from offeragent_harness.permissions import CapabilityScope, PermissionMode, PolicyContext, RiskClass
from offeragent_harness.ports import CancellationToken
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import ManualCancellationToken, ManualClock
from offeragent_harness.tools import (
    ApprovalEvidence,
    ExecutorLocation,
    PreflightMode,
    ResultSensitivity,
    SideEffectClass,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


class _ModelGateway:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.requests: list[ModelRequest] = []

    async def stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        cancellation.checkpoint()
        self.clock.advance(timedelta(milliseconds=50))
        yield ModelEvent(request.request_id, 1, ModelEventKind.STARTED)
        self.clock.advance(timedelta(milliseconds=100))
        yield ModelEvent(request.request_id, 2, ModelEventKind.TEXT_DELTA, text="OK")
        yield ModelEvent(
            request.request_id,
            3,
            ModelEventKind.USAGE,
            usage=ModelUsage(11, 3, 2, 1, Decimal("0.001234"), "USD"),
        )
        self.clock.advance(timedelta(milliseconds=50))
        yield ModelEvent(
            request.request_id,
            4,
            ModelEventKind.COMPLETED,
            finish_reason=ModelFinishReason.STOP,
        )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="model_request_1",
        model="gpt-test",
        purpose=ModelPurpose.RESPONDING,
        messages=(
            ModelMessage(
                ModelRole.USER,
                (ModelContentBlock.text("private interview answer that must never enter diagnostics"),),
            ),
        ),
        output_mode=ModelOutputMode.TEXT,
        output_schema=None,
        max_output_tokens=8,
        reasoning_effort="minimal",
        temperature=0.0,
        seed=0,
        trace_context=TraceContext("trace_model_1"),
        metadata={
            "workspaceId": "workspace_1",
            "sessionId": "session_1",
            "turnId": "turn_1",
            "runId": "run_1",
        },
    )


def _logger(tmp_path: Path) -> LocalJsonLogger:
    return LocalJsonLogger(
        tmp_path / "logs",
        workspace_instance_id="workspace_instance_1",
        allowed_root=tmp_path,
    )


def _snapshots(metrics: MetricsRegistry) -> dict[str, MetricSnapshot]:
    return {item.name: item for item in metrics.snapshots()}


@pytest.mark.asyncio
async def test_model_instrumentation_preserves_stream_and_records_latency_usage_and_safe_correlation(
    tmp_path: Path,
) -> None:
    clock = ManualClock(NOW)
    metrics = MetricsRegistry()
    logger = _logger(tmp_path)
    correlations = LocalRunCorrelationRegistry()
    correlations.register(
        workspace_id="workspace_1",
        session_id="session_1",
        turn_id="turn_1",
        run_id="run_1",
        parent_run_id=None,
        trace_id="trace_run_1",
    )
    inner = _ModelGateway(clock)
    gateway = InstrumentedModelGateway(
        inner,
        provider_id="openai",
        workspace_id="workspace_1",
        clock=clock,
        metrics=metrics,
        logger=logger,
        correlations=correlations,
    )

    events = [event async for event in gateway.stream(_request(), ManualCancellationToken())]

    assert [event.kind for event in events] == [
        ModelEventKind.STARTED,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]
    rows = _snapshots(metrics)
    assert rows[MetricName.MODEL_FIRST_TOKEN_MS].p50 == 150
    assert rows[MetricName.MODEL_LATENCY_MS].p50 == 200
    assert rows[MetricName.MODEL_INPUT_TOKENS].total == 11
    assert rows[MetricName.MODEL_OUTPUT_TOKENS].total == 3
    assert rows[MetricName.MODEL_COST_MICROS].total == 1234
    records = [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]
    assert [item["event"] for item in records] == ["model.started", "model.completed"]
    assert records[-1]["traceId"] == "trace_model_1"
    assert records[-1]["workspaceId"] == "workspace_1"
    assert records[-1]["sessionId"] == "session_1"
    assert records[-1]["turnId"] == "turn_1"
    assert records[-1]["runId"] == "run_1"
    persisted = logger.path.read_text(encoding="utf-8")
    assert "private interview answer" not in persisted
    assert "messages" not in persisted


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="workspace.read",
        version="1",
        description="read",
        result_sensitivity=ResultSensitivity.WORKSPACE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "secretText": {"type": "string"},
            },
            "required": ["path", "secretText"],
            "additionalProperties": False,
        },
        output_schema={},
        executor_location=ExecutorLocation.LOCAL,
        risk=RiskClass.READ,
        side_effect_class=SideEffectClass.READ,
        required_capabilities=frozenset({"workspace.read"}),
        concurrency_safe=True,
        idempotent=True,
        retryable=True,
        timeout_ms=1000,
        output_limit_bytes=4096,
        preflight_mode=PreflightMode.NONE,
        preflight_provider=None,
        approval_evidence=ApprovalEvidence.NONE,
    )


def _call(definition: ToolDefinition) -> ToolCall:
    arguments = {"path": "private/answer.md", "secretText": "never-log-this"}
    return ToolCall(
        tool_call_id="tool_call_1",
        run_id="run_1",
        workspace_id="workspace_1",
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="tool-idempotency-1",
        deadline=None,
        lineage=AgentLineage.root("run_1"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


@pytest.mark.asyncio
async def test_tool_instrumentation_records_error_and_approval_metrics_without_arguments(
    tmp_path: Path,
) -> None:
    clock = ManualClock(NOW)
    metrics = MetricsRegistry()
    logger = _logger(tmp_path)
    correlations = LocalRunCorrelationRegistry()
    correlations.register(
        workspace_id="workspace_1",
        session_id="session_1",
        turn_id="turn_1",
        run_id="run_1",
        parent_run_id=None,
        trace_id="trace_run_1",
    )
    telemetry = ProductionToolObservability(
        clock=clock,
        metrics=metrics,
        logger=logger,
        correlations=correlations,
    )
    definition = _tool()
    call = _call(definition)
    context = PolicyContext(
        workspace_id="workspace_1",
        session_id="session_1",
        principal_id="profile_1",
        run_id="run_1",
        permission_mode=PermissionMode.NORMAL,
        effective_scope=CapabilityScope(
            frozenset({definition.name}),
            frozenset(),
            frozenset({RiskClass.READ}),
            definition.required_capabilities,
            False,
            False,
        ),
        workspace_trusted=True,
        now=NOW,
    )
    result = ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.FAILED,
        data=None,
        user_visible_summary="private failure details",
        artifact_ids=(),
        source_refs=(),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state=None,
        error=ToolError("read_failed", "private error body", False, False),
    )

    await telemetry.approval_wait_recorded(call, context, 1250)
    await telemetry.result_recorded(call, definition, result, context, 2500)

    rows = _snapshots(metrics)
    assert rows[MetricName.APPROVAL_WAIT_MS].p50 == 1250
    assert rows[MetricName.TOOL_LATENCY_MS].p50 == 2500
    assert rows[MetricName.TOOL_ERRORS].total == 1
    records = [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]
    assert [item["event"] for item in records] == ["approval.resolved", "tool.failed"]
    assert records[-1]["traceId"] == "trace_run_1"
    assert records[-1]["toolCallId"] == "tool_call_1"
    assert records[-1]["fields"]["errorCode"] == "read_failed"
    persisted = logger.path.read_text(encoding="utf-8")
    for forbidden in ("private/answer.md", "never-log-this", "private failure", "private error body"):
        assert forbidden not in persisted


def test_run_correlation_registry_is_bounded_and_updates_existing_trace() -> None:
    registry = LocalRunCorrelationRegistry(maximum_runs=16)
    for index in range(17):
        registry.register(
            workspace_id="workspace_1",
            session_id=f"session_{index}",
            turn_id=f"turn_{index}",
            run_id=f"run_{index}",
            parent_run_id=None,
        )
    assert registry.get("run_0") is None
    updated = registry.register(
        workspace_id="workspace_1",
        session_id="session_16",
        turn_id="turn_16",
        run_id="run_16",
        parent_run_id=None,
        trace_id="trace_actual_16",
    )
    assert updated.trace_id == "trace_actual_16"
    assert registry.get("run_16") == updated
