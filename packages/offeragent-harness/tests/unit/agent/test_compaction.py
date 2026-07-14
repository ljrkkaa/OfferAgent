from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from offeragent_harness.agent.budgets import BudgetExceeded, BudgetLedger, RunBudget
from offeragent_harness.agent.compaction import (
    CompactionBatch,
    CompactionConfig,
    CompactionError,
    CompactionInvariantError,
    CompactionRecord,
    CompactionRecordKind,
    CompactionService,
    PreparedCompactionRecord,
)
from offeragent_harness.agent.context_manager import ContextVisibilityPolicy
from offeragent_harness.hooks import (
    HookDecision,
    HookEvent,
    HookExecutionContext,
    HookInvocation,
    HookOutcome,
)
from offeragent_harness.models import ModelEvent, ModelEventKind, ModelFinishReason, ModelRequest, ModelUsage, thaw_json
from offeragent_harness.ports import ArtifactMetadata, ArtifactStore, HookLifecyclePort, ModelGateway, Sensitivity
from offeragent_harness.testing import (
    ControlledBarrier,
    DeterministicIdGenerator,
    FakeRunCancelled,
    ManualCancellationToken,
    ManualClock,
    ModelScriptStep,
    ScriptedModelEvent,
    ScriptedModelGateway,
)

NOW = datetime(2031, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
USAGE = ModelUsage(80, 30, 10, 5, Decimal("0.12"), "USD")


class MemoryArtifactStore:
    def __init__(self) -> None:
        self.metadata_by_id: dict[str, ArtifactMetadata] = {}
        self.content_by_id: dict[str, bytes] = {}
        self.idempotency: dict[str, str] = {}

    async def put(
        self,
        metadata: ArtifactMetadata,
        content: bytes,
        *,
        idempotency_key: str,
    ) -> ArtifactMetadata:
        assert metadata.byte_length == len(content)
        assert metadata.sha256 == f"sha256:{hashlib.sha256(content).hexdigest()}"
        prior_id = self.idempotency.get(idempotency_key)
        if prior_id is not None:
            if self.content_by_id[prior_id] != content:
                raise AssertionError("idempotency key reused for different artifact content")
            return self.metadata_by_id[prior_id]
        self.idempotency[idempotency_key] = metadata.artifact_id
        self.metadata_by_id[metadata.artifact_id] = metadata
        self.content_by_id[metadata.artifact_id] = content
        return metadata

    async def metadata(self, artifact_id: str) -> ArtifactMetadata | None:
        return self.metadata_by_id.get(artifact_id)

    async def read(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> AsyncIterator[bytes]:
        content = self.content_by_id[artifact_id]
        end = None if limit is None else offset + limit
        yield content[offset:end]


def _budget(*, artifact_bytes: int = 1_000_000, rounds: int = 4) -> BudgetLedger:
    return BudgetLedger(
        RunBudget(
            max_model_rounds=rounds,
            max_tool_calls=10,
            max_parallel_reads=4,
            max_wall_seconds=60,
            max_input_tokens=10_000,
            max_output_tokens=10_000,
            max_cost=Decimal("100"),
            max_artifact_bytes=artifact_bytes,
            max_subagents=2,
            max_subagent_depth=1,
        ),
        started_at=NOW,
    )


def _config(
    *,
    trigger_max_records: int = 3,
    trigger_max_bytes: int = 1_000,
) -> CompactionConfig:
    return CompactionConfig(
        model="scripted-model",
        max_output_tokens=1_024,
        max_inline_summary_bytes=64,
        max_inline_body_bytes=64,
        trigger_max_records=trigger_max_records,
        trigger_max_bytes=trigger_max_bytes,
        seed=31,
    )


def _service(
    gateway: ModelGateway,
    store: ArtifactStore,
    *,
    config: CompactionConfig | None = None,
    budget: BudgetLedger | None = None,
    hooks: HookLifecyclePort | None = None,
) -> CompactionService:
    return CompactionService(
        gateway=gateway,
        artifact_store=store,
        clock=ManualClock(NOW),
        ids=DeterministicIdGenerator(),
        budget=budget or _budget(),
        visibility=ContextVisibilityPolicy.local_model(),
        config=config or _config(),
        hooks=hooks,
        hook_context=(None if hooks is None else HookExecutionContext("system", "principal", "ws", "session-1", True)),
    )


class DenyCompactionHook:
    def __init__(self) -> None:
        self.invocations: list[HookInvocation] = []

    async def invoke(self, invocation: HookInvocation, cancellation: Any) -> HookOutcome:
        cancellation.checkpoint()
        self.invocations.append(invocation)
        return HookOutcome(HookDecision.DENY, (), (), ("deny-compact",), ())


def _batch() -> CompactionBatch:
    expected = "sha256:" + "1" * 64
    after = "sha256:" + "2" * 64
    duplicate_body = "PUBLIC-SECRET-keyword-" + "x" * 240
    records = (
        CompactionRecord(
            "constraint-1",
            1,
            CompactionRecordKind.USER_CONSTRAINT,
            "只能修改本轮测试目录",
            None,
            Sensitivity.PUBLIC,
        ),
        CompactionRecord(
            "decision-1",
            2,
            CompactionRecordKind.DECISION,
            "选择本地 SQLite",
            None,
            Sensitivity.PRIVATE,
        ),
        CompactionRecord(
            "tool-1",
            3,
            CompactionRecordKind.TOOL_RESULT,
            "public tool result",
            duplicate_body,
            Sensitivity.WORKSPACE,
            source_refs=("source:tool",),
        ),
        CompactionRecord(
            "tool-2",
            4,
            CompactionRecordKind.TOOL_RESULT,
            "public tool result",
            duplicate_body,
            Sensitivity.WORKSPACE,
            source_refs=("source:tool",),
        ),
        CompactionRecord(
            "write-1",
            5,
            CompactionRecordKind.WRITE_STATE,
            "write committed",
            None,
            Sensitivity.WORKSPACE,
            resource_id="vault://notes/test.md",
            expected_hash=expected,
            after_hash=after,
            write_status="succeeded",
        ),
        CompactionRecord(
            "approval-1",
            6,
            CompactionRecordKind.APPROVAL,
            "user approved",
            None,
            Sensitivity.WORKSPACE,
            approval_id="approval-id-1",
            approval_status="approved",
        ),
        CompactionRecord(
            "source-1",
            7,
            CompactionRecordKind.SOURCE,
            "source retained",
            None,
            Sensitivity.PUBLIC,
            source_refs=("vault:notes/test.md",),
            artifact_ids=("artifact-existing",),
        ),
        CompactionRecord(
            "secret-1",
            8,
            CompactionRecordKind.MESSAGE,
            "hello",
            "TOP-SECRET-TOKEN",
            Sensitivity.SECRET,
        ),
        CompactionRecord(
            "oversized-1",
            9,
            CompactionRecordKind.MESSAGE,
            "oversized-summary-" + "z" * 120,
            None,
            Sensitivity.PUBLIC,
        ),
    )
    return CompactionBatch("ws", "run", 1, 9, records)


def _summary(batch: CompactionBatch, prepared: Sequence[PreparedCompactionRecord]) -> dict[str, Any]:
    return {
        "summary": "structured summary",
        "userConstraints": [
            {"recordIds": list(record.record_ids), "text": record.summary}
            for record in prepared
            if record.kind is CompactionRecordKind.USER_CONSTRAINT and not record.protected
        ],
        "decisions": [
            {"recordIds": list(record.record_ids), "text": record.summary}
            for record in prepared
            if record.kind is CompactionRecordKind.DECISION and not record.protected
        ],
        "writeStates": [
            {
                "recordIds": list(record.record_ids),
                "resourceId": record.resource_id,
                "status": record.write_status,
                "expectedHash": record.expected_hash,
                "afterHash": record.after_hash,
            }
            for record in prepared
            if record.kind is CompactionRecordKind.WRITE_STATE
        ],
        "approvals": [
            {
                "recordIds": list(record.record_ids),
                "approvalId": record.approval_id,
                "status": record.approval_status,
            }
            for record in prepared
            if record.kind is CompactionRecordKind.APPROVAL
        ],
        "sources": list(dict.fromkeys(source for record in prepared for source in record.source_refs)),
        "unresolvedQuestions": [],
        "artifactIds": list(dict.fromkeys(artifact for record in prepared for artifact in record.artifact_ids)),
        "preservedRecordIds": [record.record_id for record in batch.records],
        "protectedRecordIds": [record_id for record in prepared if record.protected for record_id in record.record_ids],
    }


def _events(request: ModelRequest, summary: Mapping[str, Any]) -> tuple[ModelEvent, ...]:
    return (
        ModelEvent(request.request_id, 1, ModelEventKind.STARTED),
        ModelEvent(request.request_id, 2, ModelEventKind.STRUCTURED_OUTPUT, data=summary),
        ModelEvent(request.request_id, 3, ModelEventKind.USAGE, usage=USAGE),
        ModelEvent(request.request_id, 4, ModelEventKind.COMPLETED, finish_reason=ModelFinishReason.STOP),
    )


async def _expected_request_and_summary(
    batch: CompactionBatch,
    *,
    config: CompactionConfig | None = None,
) -> tuple[ModelRequest, dict[str, Any], tuple[PreparedCompactionRecord, ...]]:
    service = _service(ScriptedModelGateway(()), MemoryArtifactStore(), config=config)
    prepared = await service._artifactize_then_deduplicate(batch, ManualCancellationToken())
    request = service.create_request(batch, prepared)
    return request, _summary(batch, prepared), prepared


@pytest.mark.asyncio
async def test_threshold_trigger_runs_lossless_compaction_and_artifactizes_before_model() -> None:
    batch = _batch()
    expected_request, summary, _ = await _expected_request_and_summary(batch)
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(expected_request, _events(expected_request, summary)),))
    store = MemoryArtifactStore()
    ledger = _budget()
    service = _service(gateway, store, budget=ledger)

    original_records = batch.records
    result = await service.compact_if_needed(batch, ManualCancellationToken())

    assert result is not None
    assert batch.records == original_records
    assert result.original_record_ids == tuple(record.record_id for record in batch.records)
    assert (result.replaced_sequence_start, result.replaced_sequence_end) == (1, 9)
    duplicate = next(record for record in result.prepared_records if record.record_ids == ("tool-1", "tool-2"))
    assert duplicate.first_sequence == 3
    assert duplicate.last_sequence == 4
    assert duplicate.inline_body is None
    assert len(duplicate.artifact_ids) == 1
    protected_ids = {
        record_id for record in result.prepared_records if record.protected for record_id in record.record_ids
    }
    assert protected_ids == {"secret-1", "oversized-1"}

    request_text = repr(gateway.requests[0])
    assert "PUBLIC-SECRET-keyword" not in request_text
    assert "TOP-SECRET-TOKEN" not in request_text
    assert "oversized-summary-" not in request_text
    assert "public tool result" in request_text
    assert "hello" not in request_text
    request_records = thaw_json(gateway.requests[0].messages[1].content[0].data)["records"]
    assert len(request_records) == 8

    assert len(store.metadata_by_id) == 4
    assert result.summary_artifact.attributes["originalEventsRetained"] is True
    assert result.summary.write_states[0].expected_hash == "sha256:" + "1" * 64
    assert result.summary.write_states[0].after_hash == "sha256:" + "2" * 64
    assert result.summary.approvals[0].status == "approved"
    assert set(result.summary.sources) == {"source:tool", "vault:notes/test.md"}
    assert "artifact-existing" in result.summary.artifact_ids
    snapshot = await ledger.snapshot(now=NOW)
    assert snapshot.used.model_rounds == 1
    assert snapshot.used.input_tokens == USAGE.input_tokens
    assert snapshot.used.output_tokens == USAGE.output_tokens
    assert snapshot.used.cost == USAGE.cost
    assert snapshot.used.artifact_bytes == sum(metadata.byte_length for metadata in store.metadata_by_id.values())
    assert snapshot.reserved.artifact_bytes == 0
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_threshold_boundary_is_deterministic_and_does_nothing_below_or_at_limit() -> None:
    record = CompactionRecord("message-1", 1, CompactionRecordKind.MESSAGE, "short", None, Sensitivity.PUBLIC)
    batch = CompactionBatch("ws", "run", 1, 1, (record,))
    probe = _service(ScriptedModelGateway(()), MemoryArtifactStore(), config=_config(trigger_max_records=10))
    exact_bytes = probe.estimated_batch_bytes(batch)
    exact_config = _config(trigger_max_records=1, trigger_max_bytes=exact_bytes)
    gateway = ScriptedModelGateway(())
    store = MemoryArtifactStore()
    service = _service(gateway, store, config=exact_config)

    assert not service.needs_compaction(batch)
    assert await service.compact_if_needed(batch, ManualCancellationToken()) is None
    assert gateway.requests == []
    assert store.metadata_by_id == {}

    byte_trigger = _service(
        ScriptedModelGateway(()),
        MemoryArtifactStore(),
        config=_config(trigger_max_records=1, trigger_max_bytes=exact_bytes - 1),
    )
    assert byte_trigger.needs_compaction(batch)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("constraint", "hash", "approval", "source", "artifact", "records"))
async def test_model_cannot_drop_or_change_preserved_compaction_invariants(mutation: str) -> None:
    batch = _batch()
    request, valid_summary, _ = await _expected_request_and_summary(batch)
    invalid = deepcopy(valid_summary)
    if mutation == "constraint":
        invalid["userConstraints"][0]["text"] = "changed"
    elif mutation == "hash":
        invalid["writeStates"][0]["expectedHash"] = "sha256:" + "9" * 64
    elif mutation == "approval":
        invalid["approvals"][0]["status"] = "denied"
    elif mutation == "source":
        invalid["sources"] = []
    elif mutation == "artifact":
        invalid["artifactIds"] = []
    else:
        invalid["preservedRecordIds"] = invalid["preservedRecordIds"][:-1]
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(request, _events(request, invalid)),))

    with pytest.raises(CompactionInvariantError):
        await _service(gateway, MemoryArtifactStore()).compact(batch, ManualCancellationToken())

    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_malformed_summary_fails_schema_validation_before_artifact_commit() -> None:
    batch = _batch()
    request, summary, _ = await _expected_request_and_summary(batch)
    del summary["writeStates"]
    gateway = ScriptedModelGateway((ModelScriptStep.from_events(request, _events(request, summary)),))
    store = MemoryArtifactStore()

    with pytest.raises(CompactionInvariantError, match="schema violations"):
        await _service(gateway, store).compact(batch, ManualCancellationToken())

    assert all(metadata.attributes["kind"] == "compaction_source" for metadata in store.metadata_by_id.values())
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_artifact_budget_fails_before_any_model_request() -> None:
    batch = _batch()
    gateway = ScriptedModelGateway(())
    store = MemoryArtifactStore()

    with pytest.raises(BudgetExceeded, match="artifact_bytes"):
        await _service(gateway, store, budget=_budget(artifact_bytes=1)).compact(
            batch,
            ManualCancellationToken(),
        )

    assert gateway.requests == []
    assert store.metadata_by_id == {}


@pytest.mark.asyncio
async def test_cancellation_during_model_stream_keeps_sources_but_never_commits_summary() -> None:
    batch = _batch()
    request, summary, _ = await _expected_request_and_summary(batch)
    barrier = ControlledBarrier("compaction-output")
    events = _events(request, summary)
    gateway = ScriptedModelGateway(
        (
            ModelScriptStep(
                request,
                tuple(
                    ScriptedModelEvent(event, barrier if event.kind is ModelEventKind.STRUCTURED_OUTPUT else None)
                    for event in events
                ),
            ),
        )
    )
    store = MemoryArtifactStore()
    cancellation = ManualCancellationToken()
    task = asyncio.create_task(_service(gateway, store).compact(batch, cancellation))
    await barrier.wait_for_arrivals(1)

    cancellation.cancel()
    with pytest.raises(FakeRunCancelled):
        await task

    assert len(store.metadata_by_id) == 3
    assert all(metadata.attributes["kind"] == "compaction_source" for metadata in store.metadata_by_id.values())
    assert [event.kind for event in gateway.emitted_events] == [ModelEventKind.STARTED]
    gateway.assert_exhausted()


@pytest.mark.asyncio
async def test_deduplication_is_exact_and_never_uses_summary_keywords() -> None:
    records = (
        CompactionRecord(
            "a",
            1,
            CompactionRecordKind.TOOL_RESULT,
            "duplicate SECRET keyword",
            "same body",
            Sensitivity.PUBLIC,
        ),
        CompactionRecord(
            "b",
            2,
            CompactionRecordKind.TOOL_RESULT,
            "duplicate SECRET keyword",
            "same body",
            Sensitivity.PUBLIC,
        ),
        CompactionRecord(
            "c",
            3,
            CompactionRecordKind.TOOL_RESULT,
            "duplicate SECRET keyword",
            "same body!",
            Sensitivity.PUBLIC,
        ),
    )
    batch = CompactionBatch("ws", "run", 1, 3, records)
    service = _service(ScriptedModelGateway(()), MemoryArtifactStore())

    prepared = await service._artifactize_then_deduplicate(batch, ManualCancellationToken())

    assert [record.record_ids for record in prepared] == [("a", "b"), ("c",)]
    assert all(record.summary == "duplicate SECRET keyword" for record in prepared)


@pytest.mark.asyncio
async def test_before_compact_hook_can_block_before_artifact_or_model_work() -> None:
    gateway = ScriptedModelGateway(())
    store = MemoryArtifactStore()
    hooks = DenyCompactionHook()

    with pytest.raises(CompactionError, match="BeforeCompact"):
        await _service(gateway, store, hooks=hooks).compact(_batch(), ManualCancellationToken())

    assert [item.event for item in hooks.invocations] == [HookEvent.BEFORE_COMPACT]
    assert gateway.requests == []
    assert store.metadata_by_id == {}
