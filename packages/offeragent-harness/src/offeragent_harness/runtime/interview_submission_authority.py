"""Store-bound authority for one root Run's Interview Submission."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import count
from typing import Any

from offeragent_harness.models.json_types import JsonValue
from offeragent_harness.permissions import PolicyContext, PolicyDecision, PolicyDisposition
from offeragent_harness.permissions.audit import PolicyAuditRecord, PolicyAuditSink
from offeragent_harness.ports import (
    CancellationToken,
    Clock,
    InvocationJournal,
    InvocationRecord,
    PolicyEvaluator,
    ToolExecutor,
)
from offeragent_harness.ports.storage import InvocationJournalConflict, JournalState
from offeragent_harness.tools import (
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
    ToolValidationError,
    ToolValidator,
    canonical_json_sha256,
)
from offeragent_harness.tools.dispatcher import ToolDispatchError

from .ordered_image_source import create_ordered_image_source_manifest

_CATALOG_RECEIPT_KEY = "catalog-normalized-source"
_CATALOG_RECEIPT_TOOL_CALL_ID = "interview-submission-catalog-receipt"
_APPLY_CLAIM_KEY = "vault-apply-claim"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class InterviewSubmissionRunAuthority:
    """Immutable attachment facts materialized for one root Agent Run."""

    captured_on: date
    ordered_image_content_hashes: tuple[str, ...]
    source_fingerprint: str | None = field(init=False)

    def __post_init__(self) -> None:
        if type(self.captured_on) is not date:
            raise TypeError("Interview Submission captured_on must be a date")
        hashes = tuple(self.ordered_image_content_hashes)
        fingerprint: str | None = None
        if hashes:
            fingerprint = create_ordered_image_source_manifest(
                captured_on=self.captured_on,
                ordered_image_content_hashes=hashes,
            ).source_fingerprint
        object.__setattr__(self, "ordered_image_content_hashes", hashes)
        object.__setattr__(self, "source_fingerprint", fingerprint)

    def durable_snapshot(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "capturedOn": self.captured_on.isoformat(),
            "orderedImageContentHashes": list(self.ordered_image_content_hashes),
            "sourceFingerprint": self.source_fingerprint,
        }

    @classmethod
    def from_durable_snapshot(cls, snapshot: Mapping[str, Any]) -> InterviewSubmissionRunAuthority:
        expected_fields = {
            "schemaVersion",
            "capturedOn",
            "orderedImageContentHashes",
            "sourceFingerprint",
        }
        if set(snapshot) != expected_fields or snapshot.get("schemaVersion") != 1:
            raise ValueError("Interview Submission authority snapshot fields are invalid")
        captured_on = snapshot.get("capturedOn")
        hashes = _string_tuple(snapshot.get("orderedImageContentHashes"))
        if (
            not isinstance(captured_on, str)
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", captured_on) is None
            or hashes is None
        ):
            raise ValueError("Interview Submission authority snapshot fields are invalid")
        try:
            authority = cls(date.fromisoformat(captured_on), hashes)
        except (TypeError, ValueError) as error:
            raise ValueError("Interview Submission authority snapshot fields are invalid") from error
        if snapshot.get("sourceFingerprint") != authority.source_fingerprint:
            raise ValueError("Interview Submission authority snapshot fingerprint is invalid")
        return authority


@dataclass(frozen=True, slots=True)
class _NormalizedSourceReceipt:
    canonical_urls: tuple[str, ...]
    ordered_image_content_hashes: tuple[str, ...]
    source_fingerprint: str | None

    @classmethod
    def from_value(cls, value: Any) -> _NormalizedSourceReceipt:
        if not isinstance(value, Mapping):
            raise _guard_error(
                "interview_catalog_receipt_invalid",
                "Catalog normalizedSource is missing or invalid.",
            )
        canonical_urls = _string_tuple(value.get("canonicalUrls"))
        hashes = _string_tuple(value.get("orderedImageContentHashes"))
        fingerprint = value.get("sourceFingerprint")
        if (
            canonical_urls is None
            or hashes is None
            or any(not item for item in canonical_urls)
            or len(canonical_urls) != len(set(canonical_urls))
            or any(_SHA256.fullmatch(item) is None for item in hashes)
            or (
                fingerprint is not None and (not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None)
            )
        ):
            raise _guard_error(
                "interview_catalog_receipt_invalid",
                "Catalog normalizedSource is missing or invalid.",
            )
        return cls(canonical_urls, hashes, fingerprint)

    def to_data(self) -> Mapping[str, JsonValue]:
        return {
            "canonicalUrls": list(self.canonical_urls),
            "orderedImageContentHashes": list(self.ordered_image_content_hashes),
            "sourceFingerprint": self.source_fingerprint,
        }


class _InterviewSubmissionAuthorityStore:
    def __init__(
        self,
        *,
        workspace_id: str,
        root_run_id: str,
        authority: InterviewSubmissionRunAuthority,
        journal: InvocationJournal,
        clock: Clock,
    ) -> None:
        if not workspace_id or not root_run_id:
            raise ValueError("Interview Submission authority requires Workspace and root Run identities")
        self.workspace_id = workspace_id
        self.root_run_id = root_run_id
        self.authority = authority
        self._journal = journal
        self._clock = clock
        self._scope = f"{workspace_id}:interview-submission-authority:{root_run_id}"

    def validate_identity(self, call: ToolCall) -> None:
        if call.workspace_id != self.workspace_id or call.lineage.root_run_id != self.root_run_id:
            raise _guard_error(
                "interview_submission_authority_identity_mismatch",
                "Interview Submission authority does not match this ToolCall lineage.",
            )

    def validate_catalog_call(self, call: ToolCall) -> None:
        raw = call.arguments.get("orderedImageContentHashes", ())
        actual = _string_tuple(raw)
        if actual != self.authority.ordered_image_content_hashes:
            raise _guard_error(
                "interview_catalog_attachment_authority_mismatch",
                "Catalog image hashes must exactly match the Store-materialized Run attachments.",
            )

    def _validated_journal_record(
        self,
        record: object,
        *,
        idempotency_key: str,
        request_hash: str | None,
        state: JournalState,
        expected_result: ToolResult | None = None,
        error_code: str = "interview_submission_authority_unavailable",
        error_message: str = "Interview Submission authority journal returned a corrupt record.",
    ) -> InvocationRecord:
        valid_binding = (
            isinstance(record, InvocationRecord)
            and record.scope == self._scope
            and record.idempotency_key == idempotency_key
            and (request_hash is None or record.request_hash == request_hash)
            and record.state is state
        )
        valid_shape = False
        if valid_binding and isinstance(record, InvocationRecord):
            if state is JournalState.STARTED:
                valid_shape = record.completed_at is None and record.result is None
            elif state is JournalState.COMPLETED:
                valid_shape = (
                    record.completed_at is not None
                    and isinstance(record.result, ToolResult)
                    and record.result.status is not ToolResultStatus.UNKNOWN_OUTCOME
                    and (expected_result is None or record.result == expected_result)
                )
        if not valid_binding or not valid_shape:
            raise _guard_error(error_code, error_message)
        assert isinstance(record, InvocationRecord)
        return record

    async def record_catalog_receipt(self, result: ToolResult) -> None:
        if result.status is not ToolResultStatus.SUCCEEDED:
            return
        data = result.data
        normalized = data.get("normalizedSource") if isinstance(data, Mapping) else None
        receipt = _NormalizedSourceReceipt.from_value(normalized)
        if (
            receipt.ordered_image_content_hashes != self.authority.ordered_image_content_hashes
            or receipt.source_fingerprint != self.authority.source_fingerprint
        ):
            raise _guard_error(
                "interview_catalog_receipt_authority_mismatch",
                "Catalog normalizedSource does not match the Store-materialized Run attachments.",
            )
        request_hash = canonical_json_sha256(receipt.to_data())
        stable_result = ToolResult(
            tool_call_id=_CATALOG_RECEIPT_TOOL_CALL_ID,
            status=ToolResultStatus.SUCCEEDED,
            data=receipt.to_data(),
            user_visible_summary="Recorded the root Run Interview Submission source receipt.",
            artifact_ids=(),
            source_refs=(),
            side_effects=(),
            retryable=False,
            before_state=None,
            after_state=None,
            error=None,
        )
        try:
            started = await self._journal.start(
                self._scope,
                _CATALOG_RECEIPT_KEY,
                request_hash,
                self._clock.utcnow(),
            )
            if isinstance(started, InvocationRecord) and started.state is JournalState.COMPLETED:
                self._validated_journal_record(
                    started,
                    idempotency_key=_CATALOG_RECEIPT_KEY,
                    request_hash=request_hash,
                    state=JournalState.COMPLETED,
                    expected_result=stable_result,
                )
                return
            self._validated_journal_record(
                started,
                idempotency_key=_CATALOG_RECEIPT_KEY,
                request_hash=request_hash,
                state=JournalState.STARTED,
            )
            completed = await self._journal.complete(
                self._scope,
                _CATALOG_RECEIPT_KEY,
                request_hash,
                stable_result,
                self._clock.utcnow(),
            )
            self._validated_journal_record(
                completed,
                idempotency_key=_CATALOG_RECEIPT_KEY,
                request_hash=request_hash,
                state=JournalState.COMPLETED,
                expected_result=stable_result,
            )
        except InvocationJournalConflict as error:
            raise _guard_error(
                "interview_catalog_receipt_conflict",
                "This root Run is already bound to a different Catalog normalizedSource receipt.",
            ) from error
        except ToolDispatchError:
            raise
        except Exception as error:
            raise _guard_error(
                "interview_submission_authority_unavailable",
                "Interview Submission authority journal is unavailable.",
            ) from error

    async def claim_apply(self, call: ToolCall) -> None:
        await self._validate_apply_binding(call)
        request_hash = _exact_call_fingerprint(call)
        try:
            record = await self._journal.start(
                self._scope,
                _APPLY_CLAIM_KEY,
                request_hash,
                self._clock.utcnow(),
            )
        except InvocationJournalConflict as error:
            raise _guard_error(
                "interview_submission_batch_already_claimed",
                "This root Run already claimed a different Interview Submission apply call.",
            ) from error
        except Exception as error:
            raise _guard_error(
                "interview_submission_authority_unavailable",
                "Interview Submission authority journal is unavailable.",
            ) from error
        self._validated_journal_record(
            record,
            idempotency_key=_APPLY_CLAIM_KEY,
            request_hash=request_hash,
            state=JournalState.STARTED,
        )

    async def verify_apply_claim(self, call: ToolCall) -> None:
        await self._validate_apply_binding(call)
        try:
            record = await self._journal.get(self._scope, _APPLY_CLAIM_KEY)
        except Exception as error:
            raise _guard_error(
                "interview_submission_authority_unavailable",
                "Interview Submission authority journal is unavailable.",
            ) from error
        if record is None:
            raise _guard_error(
                "interview_submission_apply_unclaimed",
                "Interview Submission apply call was not claimed by the root Run authority gate.",
            )
        self._validated_journal_record(
            record,
            idempotency_key=_APPLY_CLAIM_KEY,
            request_hash=_exact_call_fingerprint(call),
            state=JournalState.STARTED,
        )

    async def _validate_apply_binding(self, call: ToolCall) -> None:
        receipt = await self._load_catalog_receipt()
        submission = call.arguments.get("interviewSubmission")
        if not isinstance(submission, Mapping):
            raise _guard_error(
                "interview_submission_authority_mismatch",
                "Interview Submission provenance is missing or invalid.",
            )
        if (
            submission.get("capturedOn") != self.authority.captured_on.isoformat()
            or _string_tuple(submission.get("canonicalUrls")) != receipt.canonical_urls
            or _string_tuple(submission.get("orderedImageContentHashes")) != self.authority.ordered_image_content_hashes
            or submission.get("sourceFingerprint") != self.authority.source_fingerprint
            or receipt.ordered_image_content_hashes != self.authority.ordered_image_content_hashes
            or receipt.source_fingerprint != self.authority.source_fingerprint
        ):
            raise _guard_error(
                "interview_submission_authority_mismatch",
                "Interview Submission provenance must exactly match the root Run Catalog receipt and attachments.",
            )

    async def _load_catalog_receipt(self) -> _NormalizedSourceReceipt:
        try:
            record = await self._journal.get(self._scope, _CATALOG_RECEIPT_KEY)
        except Exception as error:
            raise _guard_error(
                "interview_submission_authority_unavailable",
                "Interview Submission authority journal is unavailable.",
            ) from error
        if record is None:
            raise _guard_error(
                "interview_catalog_receipt_missing",
                "A successful Catalog normalizedSource receipt is required before Interview Submission apply.",
            )
        record = self._validated_journal_record(
            record,
            idempotency_key=_CATALOG_RECEIPT_KEY,
            request_hash=None,
            state=JournalState.COMPLETED,
            error_code="interview_catalog_receipt_invalid",
            error_message="The durable Catalog normalizedSource receipt binding is corrupt.",
        )
        assert record.result is not None
        receipt = _NormalizedSourceReceipt.from_value(record.result.data)
        if (
            record.result.status is not ToolResultStatus.SUCCEEDED
            or record.result.tool_call_id != _CATALOG_RECEIPT_TOOL_CALL_ID
            or record.request_hash != canonical_json_sha256(receipt.to_data())
        ):
            raise _guard_error(
                "interview_catalog_receipt_invalid",
                "The durable Catalog normalizedSource receipt binding is corrupt.",
            )
        return receipt


class InterviewSubmissionAuthorityPolicy:
    """Deny model-supplied provenance drift before plugin ``tool.started``."""

    def __init__(
        self,
        *,
        workspace_id: str,
        root_run_id: str,
        authority: InterviewSubmissionRunAuthority,
        journal: InvocationJournal,
        clock: Clock,
        downstream: PolicyEvaluator,
        audit_sink: PolicyAuditSink,
    ) -> None:
        self._store = _InterviewSubmissionAuthorityStore(
            workspace_id=workspace_id,
            root_run_id=root_run_id,
            authority=authority,
            journal=journal,
            clock=clock,
        )
        self._downstream = downstream
        self._audit = audit_sink
        self._audit_sequence = count(1)

    async def evaluate(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
    ) -> PolicyDecision:
        try:
            self._store.validate_identity(call)
            if context.workspace_id != self._store.workspace_id or context.run_id != call.run_id:
                raise _guard_error(
                    "interview_submission_authority_identity_mismatch",
                    "Interview Submission authority does not match this PolicyContext.",
                )
            if call.name == "interview_catalog.search":
                self._store.validate_catalog_call(call)
            elif call.name == "vault.changes.apply" and call.arguments.get("changeKind") == "interview_submission":
                await self._store.claim_apply(call)
        except ToolDispatchError as error:
            return await self._deny(definition, call, context, error)
        return await self._downstream.evaluate(definition, call, context)

    async def _deny(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
        error: ToolDispatchError,
    ) -> PolicyDecision:
        decision = PolicyDecision(
            PolicyDisposition.DENY,
            definition.risk,
            error.code,
            str(error),
            {"interviewSubmissionAuthority": True},
        )
        await self._audit.record(
            PolicyAuditRecord(
                audit_id=(
                    f"audit:{call.tool_call_id}:{context.now.isoformat()}:deny:{error.code}:"
                    f"interview-submission:{next(self._audit_sequence):08d}"
                ),
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                run_id=context.run_id,
                root_run_id=call.lineage.root_run_id,
                tool_call_id=call.tool_call_id,
                tool_name=definition.name,
                tool_version=definition.version,
                args_hash=call.args_hash,
                disposition=decision.disposition,
                risk=definition.risk,
                reason_code=decision.reason_code,
                matched_rule_ids=(),
                evaluated_at=context.now,
                facts=decision.audit_facts,
            )
        )
        return decision


class InterviewSubmissionToolExecutor:
    """Persist Catalog receipts and defend the plugin boundary with the same root authority."""

    def __init__(
        self,
        *,
        workspace_id: str,
        root_run_id: str,
        authority: InterviewSubmissionRunAuthority,
        journal: InvocationJournal,
        clock: Clock,
        delegate: ToolExecutor,
    ) -> None:
        self._store = _InterviewSubmissionAuthorityStore(
            workspace_id=workspace_id,
            root_run_id=root_run_id,
            authority=authority,
            journal=journal,
            clock=clock,
        )
        self._delegate = delegate
        from .plugin_tools import plugin_tool_definitions

        self._catalog_definition = next(
            definition for definition in plugin_tool_definitions() if definition.name == "interview_catalog.search"
        )
        self._validator = ToolValidator()

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        cancellation.checkpoint()
        self._store.validate_identity(call)
        if call.name == "interview_catalog.search":
            self._store.validate_catalog_call(call)
            result = await self._delegate.execute(call, cancellation)
            if result.tool_call_id != call.tool_call_id:
                raise _guard_error(
                    "interview_catalog_output_invalid",
                    "Catalog returned a result bound to another ToolCall.",
                )
            if result.status is ToolResultStatus.SUCCEEDED:
                try:
                    if call.definition_fingerprint != self._catalog_definition.fingerprint:
                        raise ValueError("Catalog definition fingerprint differs from the fixed plugin contract")
                    self._validator.validate_output(self._catalog_definition, result.data)
                except (ToolValidationError, TypeError, ValueError) as error:
                    raise _guard_error(
                        "interview_catalog_output_invalid",
                        "Catalog succeeded with an output that does not match its fixed schema.",
                    ) from error
            await self._store.record_catalog_receipt(result)
            return result
        if call.name == "vault.changes.apply" and call.arguments.get("changeKind") == "interview_submission":
            await self._store.verify_apply_claim(call)
        return await self._delegate.execute(call, cancellation)


def _string_tuple(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, str) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _exact_call_fingerprint(call: ToolCall) -> str:
    return canonical_json_sha256(
        {
            "workspaceId": call.workspace_id,
            "rootRunId": call.lineage.root_run_id,
            "runId": call.run_id,
            "toolCallId": call.tool_call_id,
            "name": call.name,
            "version": call.version,
            "definitionFingerprint": call.definition_fingerprint,
            "argsHash": call.args_hash,
            "idempotencyKey": call.idempotency_key,
            "resultSensitivity": call.result_sensitivity.value,
        }
    )


def _guard_error(code: str, message: str) -> ToolDispatchError:
    return ToolDispatchError(
        code,
        message,
        retryable=False,
        side_effect_possible=False,
    )


__all__ = [
    "InterviewSubmissionAuthorityPolicy",
    "InterviewSubmissionRunAuthority",
    "InterviewSubmissionToolExecutor",
]
