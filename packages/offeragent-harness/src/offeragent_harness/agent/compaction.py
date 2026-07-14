"""Lossless-boundary context compaction with Artifact and invariant preservation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Any, cast

from jsonschema import Draft202012Validator

from offeragent_harness.hooks import HookDecision, HookEvent, HookExecutionContext, HookInvocation
from offeragent_harness.models import (
    ModelContentBlock,
    ModelMessage,
    ModelOutputMode,
    ModelPurpose,
    ModelRequest,
    ModelRole,
    ModelUsage,
    TraceContext,
)
from offeragent_harness.models.json_types import FrozenJsonObject, thaw_json
from offeragent_harness.ports import (
    ArtifactMetadata,
    ArtifactState,
    ArtifactStore,
    CancellationToken,
    Clock,
    HookLifecyclePort,
    IdGenerator,
    ModelGateway,
    Sensitivity,
)
from offeragent_harness.tools import canonical_json_bytes, canonical_json_sha256

from .budgets import BudgetDelta, BudgetLedger
from .context_manager import ContextVisibilityPolicy
from .model_planner import (
    ModelInvalidOutput,
    ModelProviderFailure,
    ModelStreamProtocolError,
    StructuredModelResponse,
    collect_structured_response,
)

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class CompactionError(RuntimeError):
    pass


class CompactionInvariantError(CompactionError):
    pass


class CompactionRecordKind(str, Enum):
    USER_CONSTRAINT = "user_constraint"
    DECISION = "decision"
    MESSAGE = "message"
    TOOL_RESULT = "tool_result"
    WRITE_STATE = "write_state"
    APPROVAL = "approval"
    SOURCE = "source"


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    record_id: str
    sequence: int
    kind: CompactionRecordKind
    summary: str
    body: str | None
    sensitivity: Sensitivity
    source_refs: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    resource_id: str | None = None
    expected_hash: str | None = None
    after_hash: str | None = None
    write_status: str | None = None
    approval_id: str | None = None
    approval_status: str | None = None

    def __post_init__(self) -> None:
        if not self.record_id or self.sequence < 1 or not self.summary:
            raise ValueError("compaction records require identity, positive sequence, and summary")
        if len(self.source_refs) != len(set(self.source_refs)) or any(not value for value in self.source_refs):
            raise ValueError("source_refs must be unique non-empty values")
        if len(self.artifact_ids) != len(set(self.artifact_ids)) or any(not value for value in self.artifact_ids):
            raise ValueError("artifact_ids must be unique non-empty values")
        for value in (self.expected_hash, self.after_hash):
            if value is not None and not _SHA256.fullmatch(value):
                raise ValueError("file state hashes must be canonical sha256 digests")
        write_fields = (self.resource_id, self.write_status)
        if self.kind is CompactionRecordKind.WRITE_STATE:
            if any(value is None for value in write_fields):
                raise ValueError("write_state records require resource_id and write_status")
        elif any(value is not None for value in (*write_fields, self.expected_hash, self.after_hash)):
            raise ValueError("write state fields are only valid on write_state records")
        approval_fields = (self.approval_id, self.approval_status)
        if self.kind is CompactionRecordKind.APPROVAL:
            if any(value is None for value in approval_fields):
                raise ValueError("approval records require approval_id and approval_status")
        elif any(value is not None for value in approval_fields):
            raise ValueError("approval fields are only valid on approval records")
        if self.kind is CompactionRecordKind.SOURCE and not self.source_refs:
            raise ValueError("source records require source_refs")

    @property
    def fingerprint(self) -> str:
        return canonical_json_sha256(self.semantic_data())

    def semantic_data(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "summary": self.summary,
            "body": self.body,
            "sensitivity": self.sensitivity.value,
            "sourceRefs": list(self.source_refs),
            "artifactIds": list(self.artifact_ids),
            "resourceId": self.resource_id,
            "expectedHash": self.expected_hash,
            "afterHash": self.after_hash,
            "writeStatus": self.write_status,
            "approvalId": self.approval_id,
            "approvalStatus": self.approval_status,
        }


@dataclass(frozen=True, slots=True)
class CompactionBatch:
    workspace_id: str
    run_id: str
    replaced_sequence_start: int
    replaced_sequence_end: int
    records: tuple[CompactionRecord, ...]

    def __post_init__(self) -> None:
        if not self.workspace_id or not self.run_id or not self.records:
            raise ValueError("compaction batch identity and records must not be empty")
        if self.replaced_sequence_start < 1 or self.replaced_sequence_end < self.replaced_sequence_start:
            raise ValueError("invalid compaction sequence range")
        record_ids = [record.record_id for record in self.records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("compaction record IDs must be unique")
        sequences = [record.sequence for record in self.records]
        if sequences != sorted(sequences):
            raise ValueError("compaction records must be ordered by sequence")
        if any(
            sequence < self.replaced_sequence_start or sequence > self.replaced_sequence_end for sequence in sequences
        ):
            raise ValueError("record sequence is outside the replaced range")


@dataclass(frozen=True, slots=True)
class PreparedCompactionRecord:
    record_ids: tuple[str, ...]
    first_sequence: int
    last_sequence: int
    kind: CompactionRecordKind
    summary: str | None
    inline_body: str | None
    sensitivity: Sensitivity
    source_refs: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    resource_id: str | None
    expected_hash: str | None
    after_hash: str | None
    write_status: str | None
    approval_id: str | None
    approval_status: str | None
    protected: bool


@dataclass(frozen=True, slots=True)
class SummaryTextItem:
    record_ids: tuple[str, ...]
    text: str


@dataclass(frozen=True, slots=True)
class SummaryWriteState:
    record_ids: tuple[str, ...]
    resource_id: str
    status: str
    expected_hash: str | None
    after_hash: str | None


@dataclass(frozen=True, slots=True)
class SummaryApproval:
    record_ids: tuple[str, ...]
    approval_id: str
    status: str


@dataclass(frozen=True, slots=True)
class StructuredCompactionSummary:
    summary: str
    user_constraints: tuple[SummaryTextItem, ...]
    decisions: tuple[SummaryTextItem, ...]
    write_states: tuple[SummaryWriteState, ...]
    approvals: tuple[SummaryApproval, ...]
    sources: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    preserved_record_ids: tuple[str, ...]
    protected_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompactionResult:
    summary: StructuredCompactionSummary
    summary_artifact: ArtifactMetadata
    prepared_records: tuple[PreparedCompactionRecord, ...]
    original_record_ids: tuple[str, ...]
    replaced_sequence_start: int
    replaced_sequence_end: int
    usage: ModelUsage


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    model: str
    max_output_tokens: int
    max_inline_summary_bytes: int
    max_inline_body_bytes: int
    trigger_max_records: int
    trigger_max_bytes: int
    reasoning_effort: str | None = None
    temperature: float | None = 0
    seed: int | None = None

    def __post_init__(self) -> None:
        if (
            not self.model
            or self.max_output_tokens < 1
            or self.max_inline_summary_bytes < 1
            or self.max_inline_body_bytes < 1
            or self.trigger_max_records < 1
            or self.trigger_max_bytes < 1
        ):
            raise ValueError("compaction model and positive output/inline limits are required")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("compaction temperature must be between 0 and 2")


_TEXT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["recordIds", "text"],
    "properties": {
        "recordIds": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string"}},
        "text": {"type": "string", "minLength": 1},
    },
}

COMPACTION_SUMMARY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "userConstraints",
        "decisions",
        "writeStates",
        "approvals",
        "sources",
        "unresolvedQuestions",
        "artifactIds",
        "preservedRecordIds",
        "protectedRecordIds",
    ],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "userConstraints": {"type": "array", "items": _TEXT_ITEM_SCHEMA},
        "decisions": {"type": "array", "items": _TEXT_ITEM_SCHEMA},
        "writeStates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["recordIds", "resourceId", "status", "expectedHash", "afterHash"],
                "properties": {
                    "recordIds": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"type": "string"},
                    },
                    "resourceId": {"type": "string", "minLength": 1},
                    "status": {"type": "string", "minLength": 1},
                    "expectedHash": {"oneOf": [{"type": "null"}, {"type": "string", "pattern": _SHA256.pattern}]},
                    "afterHash": {"oneOf": [{"type": "null"}, {"type": "string", "pattern": _SHA256.pattern}]},
                },
            },
        },
        "approvals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["recordIds", "approvalId", "status"],
                "properties": {
                    "recordIds": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"type": "string"},
                    },
                    "approvalId": {"type": "string", "minLength": 1},
                    "status": {"type": "string", "minLength": 1},
                },
            },
        },
        "sources": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
        "unresolvedQuestions": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "artifactIds": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
        "preservedRecordIds": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "protectedRecordIds": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
    },
}
Draft202012Validator.check_schema(COMPACTION_SUMMARY_SCHEMA)


class CompactionService:
    def __init__(
        self,
        *,
        gateway: ModelGateway,
        artifact_store: ArtifactStore,
        clock: Clock,
        ids: IdGenerator,
        budget: BudgetLedger,
        visibility: ContextVisibilityPolicy,
        config: CompactionConfig,
        hooks: HookLifecyclePort | None = None,
        hook_context: HookExecutionContext | None = None,
    ) -> None:
        if hooks is not None and hook_context is None:
            raise ValueError("hook_context is required when compaction Hooks are configured")
        self._gateway = gateway
        self._artifact_store = artifact_store
        self._clock = clock
        self._ids = ids
        self._budget = budget
        self._visibility = visibility
        self._config = config
        self._hooks = hooks
        self._hook_context = hook_context
        self._validator = Draft202012Validator(COMPACTION_SUMMARY_SCHEMA)

    def needs_compaction(self, batch: CompactionBatch) -> bool:
        return (
            len(batch.records) > self._config.trigger_max_records
            or self.estimated_batch_bytes(batch) > self._config.trigger_max_bytes
        )

    @staticmethod
    def estimated_batch_bytes(batch: CompactionBatch) -> int:
        return len(
            canonical_json_bytes(
                {
                    "workspaceId": batch.workspace_id,
                    "runId": batch.run_id,
                    "sequenceStart": batch.replaced_sequence_start,
                    "sequenceEnd": batch.replaced_sequence_end,
                    "records": [
                        {
                            "recordId": record.record_id,
                            "sequence": record.sequence,
                            **record.semantic_data(),
                        }
                        for record in batch.records
                    ],
                }
            )
        )

    async def compact_if_needed(
        self,
        batch: CompactionBatch,
        cancellation: CancellationToken,
    ) -> CompactionResult | None:
        cancellation.checkpoint()
        if not self.needs_compaction(batch):
            return None
        return await self.compact(batch, cancellation)

    async def compact(self, batch: CompactionBatch, cancellation: CancellationToken) -> CompactionResult:
        cancellation.checkpoint()
        await self._before_compact(batch, cancellation)
        prepared = await self._artifactize_then_deduplicate(batch, cancellation)
        await self._budget.consume(BudgetDelta(model_rounds=1))
        request = self.create_request(batch, prepared)
        response = await self._invoke_and_charge(request, cancellation)
        raw = cast(dict[str, Any], thaw_json(response.output))
        violations = tuple(
            f"{_json_path(error.absolute_path)}: {error.message}"
            for error in sorted(
                self._validator.iter_errors(raw),
                key=lambda item: tuple(str(part) for part in item.path),
            )
        )
        if violations:
            raise CompactionInvariantError("compaction summary schema violations: " + "; ".join(violations))
        summary = _parse_summary(raw)
        self._verify_preservation(batch, prepared, summary)
        summary_artifact = await self._store_summary_artifact(batch, prepared, response.output, cancellation)
        return CompactionResult(
            summary=summary,
            summary_artifact=summary_artifact,
            prepared_records=prepared,
            original_record_ids=tuple(record.record_id for record in batch.records),
            replaced_sequence_start=batch.replaced_sequence_start,
            replaced_sequence_end=batch.replaced_sequence_end,
            usage=response.usage,
        )

    async def _before_compact(self, batch: CompactionBatch, cancellation: CancellationToken) -> None:
        if self._hooks is None:
            return
        context = self._hook_context
        assert context is not None
        if context.workspace_id != batch.workspace_id:
            raise CompactionError("compaction Hook context belongs to another workspace")
        outcome = await self._hooks.invoke(
            HookInvocation(
                invocation_id=(f"compact:{batch.run_id}:{batch.replaced_sequence_start}:{batch.replaced_sequence_end}"),
                chain_id=f"agent:{batch.run_id}",
                event=HookEvent.BEFORE_COMPACT,
                context=context,
                run_id=batch.run_id,
                facts={
                    "recordCount": len(batch.records),
                    "estimatedBytes": self.estimated_batch_bytes(batch),
                    "sequenceStart": batch.replaced_sequence_start,
                    "sequenceEnd": batch.replaced_sequence_end,
                },
            ),
            cancellation,
        )
        if outcome.decision is not HookDecision.CONTINUE:
            raise CompactionError(f"BeforeCompact Hook returned {outcome.decision.value}")

    def create_request(
        self,
        batch: CompactionBatch,
        prepared: Sequence[PreparedCompactionRecord],
    ) -> ModelRequest:
        records = [self._model_record(record) for record in prepared]
        return ModelRequest(
            request_id=self._ids.new_id("model-request"),
            model=self._config.model,
            purpose=ModelPurpose.COMPACTION,
            messages=(
                ModelMessage(
                    ModelRole.SYSTEM,
                    (
                        ModelContentBlock.text(
                            "把给定记录压缩成严格结构化摘要。必须逐字保留用户约束、决策、写状态、"
                            "expected/after hash、审批状态、source refs、Artifact refs 和全部 record IDs。"
                            "受保护记录只能保留其 ID 与 Artifact 引用, 不得猜测被隐藏正文。"
                        ),
                    ),
                    name="offeragent-compaction-rules",
                ),
                ModelMessage(
                    ModelRole.USER,
                    (
                        ModelContentBlock(
                            kind="compaction_records",
                            data={
                                "workspaceId": batch.workspace_id,
                                "runId": batch.run_id,
                                "sequenceStart": batch.replaced_sequence_start,
                                "sequenceEnd": batch.replaced_sequence_end,
                                "records": records,
                            },
                        ),
                    ),
                    name="offeragent-compaction-input",
                ),
            ),
            output_mode=ModelOutputMode.JSON,
            output_schema=COMPACTION_SUMMARY_SCHEMA,
            max_output_tokens=self._config.max_output_tokens,
            reasoning_effort=self._config.reasoning_effort,
            temperature=self._config.temperature,
            seed=self._config.seed,
            trace_context=TraceContext(self._ids.new_id("trace")),
            metadata={
                "workspaceId": batch.workspace_id,
                "runId": batch.run_id,
                "recordCount": len(batch.records),
                "deduplicatedRecordCount": len(prepared),
            },
        )

    async def _invoke_and_charge(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> StructuredModelResponse:
        try:
            response = await collect_structured_response(self._gateway, request, cancellation)
        except (ModelStreamProtocolError, ModelProviderFailure, ModelInvalidOutput) as error:
            if error.usage is not None:
                await self._charge_usage(error.usage)
            raise
        await self._charge_usage(response.usage)
        return response

    async def _charge_usage(self, usage: ModelUsage) -> None:
        await self._budget.consume(
            BudgetDelta(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost=usage.cost or Decimal("0"),
            )
        )

    async def _artifactize_then_deduplicate(
        self,
        batch: CompactionBatch,
        cancellation: CancellationToken,
    ) -> tuple[PreparedCompactionRecord, ...]:
        artifact_cache: dict[tuple[str, Sensitivity], ArtifactMetadata] = {}
        individually_prepared: list[PreparedCompactionRecord] = []
        for record in batch.records:
            cancellation.checkpoint()
            visible = self._visibility.allows(record.sensitivity)
            summary_too_large = len(record.summary.encode("utf-8")) > self._config.max_inline_summary_bytes
            body_bytes = record.body.encode("utf-8") if record.body is not None else b""
            must_artifactize = not visible or summary_too_large or len(body_bytes) > self._config.max_inline_body_bytes
            protected = not visible or summary_too_large
            artifact_ids = list(record.artifact_ids)
            if must_artifactize:
                content = canonical_json_bytes(
                    {
                        "recordId": record.record_id,
                        "sequence": record.sequence,
                        **record.semantic_data(),
                    }
                )
                digest = canonical_json_sha256(
                    {
                        "record": record.semantic_data(),
                        "sensitivity": record.sensitivity.value,
                    }
                )
                cache_key = (digest, record.sensitivity)
                metadata = artifact_cache.get(cache_key)
                if metadata is None:
                    metadata = await self._put_artifact(
                        workspace_id=batch.workspace_id,
                        run_id=batch.run_id,
                        content=content,
                        mime_type="application/json",
                        sensitivity=record.sensitivity,
                        attributes={"kind": "compaction_source", "recordFingerprint": record.fingerprint},
                        idempotency_key=f"compaction-source:{batch.run_id}:{digest}",
                        cancellation=cancellation,
                    )
                    artifact_cache[cache_key] = metadata
                if metadata.artifact_id not in artifact_ids:
                    artifact_ids.append(metadata.artifact_id)
            individually_prepared.append(
                PreparedCompactionRecord(
                    record_ids=(record.record_id,),
                    first_sequence=record.sequence,
                    last_sequence=record.sequence,
                    kind=record.kind,
                    summary=record.summary if not protected else None,
                    inline_body=record.body if visible and not must_artifactize else None,
                    sensitivity=record.sensitivity,
                    source_refs=record.source_refs,
                    artifact_ids=tuple(artifact_ids),
                    resource_id=record.resource_id,
                    expected_hash=record.expected_hash,
                    after_hash=record.after_hash,
                    write_status=record.write_status,
                    approval_id=record.approval_id,
                    approval_status=record.approval_status,
                    protected=protected,
                )
            )

        by_fingerprint: dict[str, PreparedCompactionRecord] = {}
        order: list[str] = []
        for prepared in individually_prepared:
            fingerprint = canonical_json_sha256(_prepared_semantics(prepared))
            existing = by_fingerprint.get(fingerprint)
            if existing is None:
                by_fingerprint[fingerprint] = prepared
                order.append(fingerprint)
                continue
            by_fingerprint[fingerprint] = replace(
                existing,
                record_ids=(*existing.record_ids, *prepared.record_ids),
                last_sequence=prepared.last_sequence,
                artifact_ids=tuple(dict.fromkeys((*existing.artifact_ids, *prepared.artifact_ids))),
            )
        return tuple(by_fingerprint[fingerprint] for fingerprint in order)

    async def _put_artifact(
        self,
        *,
        workspace_id: str,
        run_id: str,
        content: bytes,
        mime_type: str,
        sensitivity: Sensitivity,
        attributes: Mapping[str, Any],
        idempotency_key: str,
        cancellation: CancellationToken,
    ) -> ArtifactMetadata:
        cancellation.checkpoint()
        reservation = await self._budget.reserve(BudgetDelta(artifact_bytes=len(content)))
        metadata = ArtifactMetadata(
            artifact_id=self._ids.new_id("artifact"),
            workspace_id=workspace_id,
            owner_run_id=run_id,
            mime_type=mime_type,
            byte_length=len(content),
            sha256=f"sha256:{hashlib.sha256(content).hexdigest()}",
            sensitivity=sensitivity,
            state=ArtifactState.COMPLETE,
            created_at=self._clock.utcnow(),
            attributes=attributes,
        )
        try:
            stored = await self._artifact_store.put(metadata, content, idempotency_key=idempotency_key)
        except BaseException:
            await reservation.release()
            raise
        await reservation.consume(BudgetDelta(artifact_bytes=len(content)))
        cancellation.checkpoint()
        return stored

    async def _store_summary_artifact(
        self,
        batch: CompactionBatch,
        prepared: Sequence[PreparedCompactionRecord],
        raw_summary: FrozenJsonObject,
        cancellation: CancellationToken,
    ) -> ArtifactMetadata:
        content = canonical_json_bytes(raw_summary)
        visible_sensitivities = [record.sensitivity for record in prepared if not record.protected]
        sensitivity = _maximum_sensitivity(visible_sensitivities)
        return await self._put_artifact(
            workspace_id=batch.workspace_id,
            run_id=batch.run_id,
            content=content,
            mime_type="application/json",
            sensitivity=sensitivity,
            attributes={
                "kind": "context_compaction_summary",
                "sequenceStart": batch.replaced_sequence_start,
                "sequenceEnd": batch.replaced_sequence_end,
                "originalEventsRetained": True,
            },
            idempotency_key=(
                f"compaction-summary:{batch.run_id}:{batch.replaced_sequence_start}:{batch.replaced_sequence_end}:"
                f"{canonical_json_sha256(raw_summary)}"
            ),
            cancellation=cancellation,
        )

    @staticmethod
    def _model_record(record: PreparedCompactionRecord) -> dict[str, Any]:
        return {
            "recordIds": list(record.record_ids),
            "firstSequence": record.first_sequence,
            "lastSequence": record.last_sequence,
            "kind": record.kind.value,
            "summary": record.summary,
            "body": record.inline_body,
            "sensitivity": record.sensitivity.value,
            "protected": record.protected,
            "sourceRefs": list(record.source_refs),
            "artifactIds": list(record.artifact_ids),
            "resourceId": record.resource_id,
            "expectedHash": record.expected_hash,
            "afterHash": record.after_hash,
            "writeStatus": record.write_status,
            "approvalId": record.approval_id,
            "approvalStatus": record.approval_status,
        }

    @staticmethod
    def _verify_preservation(
        batch: CompactionBatch,
        prepared: Sequence[PreparedCompactionRecord],
        summary: StructuredCompactionSummary,
    ) -> None:
        original_ids = {record.record_id for record in batch.records}
        if set(summary.preserved_record_ids) != original_ids:
            raise CompactionInvariantError("preservedRecordIds must exactly cover every original record")
        protected_ids = {record_id for record in prepared if record.protected for record_id in record.record_ids}
        if set(summary.protected_record_ids) != protected_ids:
            raise CompactionInvariantError("protectedRecordIds do not match sensitivity filtering")
        expected_artifacts = {artifact for record in prepared for artifact in record.artifact_ids}
        if not expected_artifacts.issubset(summary.artifact_ids):
            raise CompactionInvariantError("compaction summary omitted an Artifact reference")
        expected_sources = {source for record in prepared for source in record.source_refs}
        if not expected_sources.issubset(summary.sources):
            raise CompactionInvariantError("compaction summary omitted a source reference")

        _verify_text_items(prepared, CompactionRecordKind.USER_CONSTRAINT, summary.user_constraints)
        _verify_text_items(prepared, CompactionRecordKind.DECISION, summary.decisions)

        expected_writes = {
            record.record_ids: (
                record.resource_id,
                record.write_status,
                record.expected_hash,
                record.after_hash,
            )
            for record in prepared
            if record.kind is CompactionRecordKind.WRITE_STATE
        }
        actual_writes = {
            item.record_ids: (item.resource_id, item.status, item.expected_hash, item.after_hash)
            for item in summary.write_states
        }
        if actual_writes != expected_writes:
            raise CompactionInvariantError("write state, hashes, or record lineage changed during compaction")

        expected_approvals = {
            record.record_ids: (record.approval_id, record.approval_status)
            for record in prepared
            if record.kind is CompactionRecordKind.APPROVAL
        }
        actual_approvals = {item.record_ids: (item.approval_id, item.status) for item in summary.approvals}
        if actual_approvals != expected_approvals:
            raise CompactionInvariantError("approval state or record lineage changed during compaction")


def _parse_summary(raw: Mapping[str, Any]) -> StructuredCompactionSummary:
    return StructuredCompactionSummary(
        summary=cast(str, raw["summary"]),
        user_constraints=tuple(_parse_text_item(item) for item in cast(list[dict[str, Any]], raw["userConstraints"])),
        decisions=tuple(_parse_text_item(item) for item in cast(list[dict[str, Any]], raw["decisions"])),
        write_states=tuple(
            SummaryWriteState(
                tuple(cast(list[str], item["recordIds"])),
                cast(str, item["resourceId"]),
                cast(str, item["status"]),
                cast(str | None, item["expectedHash"]),
                cast(str | None, item["afterHash"]),
            )
            for item in cast(list[dict[str, Any]], raw["writeStates"])
        ),
        approvals=tuple(
            SummaryApproval(
                tuple(cast(list[str], item["recordIds"])),
                cast(str, item["approvalId"]),
                cast(str, item["status"]),
            )
            for item in cast(list[dict[str, Any]], raw["approvals"])
        ),
        sources=tuple(cast(list[str], raw["sources"])),
        unresolved_questions=tuple(cast(list[str], raw["unresolvedQuestions"])),
        artifact_ids=tuple(cast(list[str], raw["artifactIds"])),
        preserved_record_ids=tuple(cast(list[str], raw["preservedRecordIds"])),
        protected_record_ids=tuple(cast(list[str], raw["protectedRecordIds"])),
    )


def _parse_text_item(raw: Mapping[str, Any]) -> SummaryTextItem:
    return SummaryTextItem(tuple(cast(list[str], raw["recordIds"])), cast(str, raw["text"]))


def _verify_text_items(
    prepared: Sequence[PreparedCompactionRecord],
    kind: CompactionRecordKind,
    actual: Sequence[SummaryTextItem],
) -> None:
    expected = {
        record.record_ids: record.summary for record in prepared if record.kind is kind and not record.protected
    }
    observed = {item.record_ids: item.text for item in actual}
    if observed != expected:
        raise CompactionInvariantError(f"{kind.value} text or record lineage changed during compaction")


def _prepared_semantics(record: PreparedCompactionRecord) -> dict[str, Any]:
    return {
        "kind": record.kind.value,
        "summary": record.summary,
        "inlineBody": record.inline_body,
        "sensitivity": record.sensitivity.value,
        "sourceRefs": list(record.source_refs),
        "artifactIds": list(record.artifact_ids),
        "resourceId": record.resource_id,
        "expectedHash": record.expected_hash,
        "afterHash": record.after_hash,
        "writeStatus": record.write_status,
        "approvalId": record.approval_id,
        "approvalStatus": record.approval_status,
        "protected": record.protected,
    }


def _maximum_sensitivity(values: Sequence[Sensitivity]) -> Sensitivity:
    rank = {
        Sensitivity.PUBLIC: 0,
        Sensitivity.WORKSPACE: 1,
        Sensitivity.PRIVATE: 2,
        Sensitivity.SECRET: 3,
    }
    return max(values, key=rank.__getitem__) if values else Sensitivity.PUBLIC


def _json_path(path: Sequence[object]) -> str:
    parts = [str(part) for part in path]
    return "$" if not parts else "$." + ".".join(parts)


__all__ = [
    "COMPACTION_SUMMARY_SCHEMA",
    "CompactionBatch",
    "CompactionConfig",
    "CompactionError",
    "CompactionInvariantError",
    "CompactionRecord",
    "CompactionRecordKind",
    "CompactionResult",
    "CompactionService",
    "PreparedCompactionRecord",
    "StructuredCompactionSummary",
    "SummaryApproval",
    "SummaryTextItem",
    "SummaryWriteState",
]
