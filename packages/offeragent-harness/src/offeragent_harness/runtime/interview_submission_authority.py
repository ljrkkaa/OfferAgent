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
_CATALOG_BINDINGS_KEY = "catalog-candidate-bindings"
_CATALOG_BINDINGS_TOOL_CALL_ID = "interview-submission-catalog-bindings"
_EXACT_READ_RECEIPT_KEY_PREFIX = "catalog-exact-read"
_APPLY_CLAIM_KEY = "vault-apply-claim"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXPERIENCE_PATH = re.compile(r"^(?:experiences|interviews/experiences)/[^/]+[.]md$")
_QUESTION_PATH = re.compile(r"^(?:interview|interviews/questions)/[^/]+[.]md$")
_INDEX_PATHS = frozenset({"experiences/index.md", "interview/index.md"})
_MAX_EXACT_READ_RECEIPTS_PER_TARGET = 512


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


@dataclass(frozen=True, slots=True)
class _CatalogTargetBinding:
    kind: str
    path: str
    modified_version: str
    content_hash: str | None
    exact_source_match: bool


@dataclass(frozen=True, slots=True)
class _CatalogBindings:
    targets: tuple[_CatalogTargetBinding, ...]
    truncated: bool

    @classmethod
    def from_value(cls, value: Any) -> _CatalogBindings:
        if not isinstance(value, Mapping) or not isinstance(value.get("truncated"), bool):
            raise _guard_error(
                "interview_catalog_bindings_invalid",
                "Catalog candidate and index bindings are missing or invalid.",
            )
        targets: list[_CatalogTargetBinding] = []
        for field_name, kind in (("experienceCandidates", "experience"), ("questionCandidates", "question")):
            candidates = value.get(field_name)
            if not isinstance(candidates, Sequence) or isinstance(candidates, str):
                raise _guard_error(
                    "interview_catalog_bindings_invalid",
                    "Catalog candidate and index bindings are missing or invalid.",
                )
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    raise _guard_error(
                        "interview_catalog_bindings_invalid",
                        "Catalog candidate and index bindings are missing or invalid.",
                    )
                exact_source_match = candidate.get("exactSourceMatch", False)
                if kind == "question" and "exactSourceMatch" in candidate:
                    raise _guard_error(
                        "interview_catalog_bindings_invalid",
                        "Catalog candidate and index bindings are missing or invalid.",
                    )
                targets.append(
                    _catalog_target_binding(
                        kind=kind,
                        value=candidate,
                        exact_source_match=exact_source_match,
                    )
                )
        indexes = value.get("indexes")
        if not isinstance(indexes, Sequence) or isinstance(indexes, str) or len(indexes) != 2:
            raise _guard_error(
                "interview_catalog_bindings_invalid",
                "Catalog candidate and index bindings are missing or invalid.",
            )
        for index in indexes:
            if not isinstance(index, Mapping) or index.get("kind") not in {"experience", "question"}:
                raise _guard_error(
                    "interview_catalog_bindings_invalid",
                    "Catalog candidate and index bindings are missing or invalid.",
                )
            exists = index.get("exists")
            if not isinstance(exists, bool):
                raise _guard_error(
                    "interview_catalog_bindings_invalid",
                    "Catalog candidate and index bindings are missing or invalid.",
                )
            binding = _catalog_target_binding(
                kind="index",
                value=index,
                exact_source_match=False,
                content_hash_required=exists,
            )
            if (exists and binding.modified_version == "missing") or (
                not exists and (binding.modified_version != "missing" or binding.content_hash is not None)
            ):
                raise _guard_error(
                    "interview_catalog_bindings_invalid",
                    "Catalog candidate and index bindings are missing or invalid.",
                )
            targets.append(binding)
        folded_paths = [target.path.casefold() for target in targets]
        if len(folded_paths) != len(set(folded_paths)):
            raise _guard_error(
                "interview_catalog_bindings_invalid",
                "Catalog candidate and index paths must be unique.",
            )
        return cls(tuple(targets), value["truncated"])

    def target(self, path: str) -> _CatalogTargetBinding | None:
        folded = path.casefold()
        return next((target for target in self.targets if target.path.casefold() == folded), None)

    @property
    def has_exact_source_experience(self) -> bool:
        return any(target.kind == "experience" and target.exact_source_match for target in self.targets)


@dataclass(frozen=True, slots=True)
class _ExactReadReceipt:
    path: str
    line_start: int
    line_end: int
    modified_version: str
    content_hash: str
    truncated: bool

    @classmethod
    def from_result(cls, result: ToolResult, binding: _CatalogTargetBinding) -> _ExactReadReceipt:
        data = result.data
        content_hash = binding.content_hash
        if not isinstance(data, Mapping):
            raise _guard_error(
                "interview_submission_exact_read_invalid",
                "Exact Vault read returned an invalid result.",
            )
        line_start = data.get("lineStart")
        line_end = data.get("lineEnd")
        truncated = data.get("truncated")
        if (
            data.get("path") != binding.path
            or data.get("modifiedVersion") != binding.modified_version
            or content_hash is None
            or data.get("contentHash") != content_hash
            or not isinstance(line_start, int)
            or isinstance(line_start, bool)
            or not isinstance(line_end, int)
            or isinstance(line_end, bool)
            or line_start < 1
            or line_end < line_start
            or not isinstance(truncated, bool)
        ):
            raise _guard_error(
                "interview_submission_exact_read_invalid",
                "Exact Vault read did not match its Catalog path, version, hash, or line range.",
            )
        return cls(
            path=binding.path,
            line_start=line_start,
            line_end=line_end,
            modified_version=binding.modified_version,
            content_hash=content_hash,
            truncated=truncated,
        )

    def to_data(self) -> Mapping[str, JsonValue]:
        return {
            "path": self.path,
            "lineStart": self.line_start,
            "lineEnd": self.line_end,
            "modifiedVersion": self.modified_version,
            "contentHash": self.content_hash,
            "truncated": self.truncated,
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

    async def record_catalog_bindings(self, result: ToolResult) -> None:
        if result.status is not ToolResultStatus.SUCCEEDED:
            return
        data = result.data
        _CatalogBindings.from_value(data)
        assert isinstance(data, Mapping)
        request_hash = canonical_json_sha256(data)
        stable_result = ToolResult(
            tool_call_id=_CATALOG_BINDINGS_TOOL_CALL_ID,
            status=ToolResultStatus.SUCCEEDED,
            data=data,
            user_visible_summary="Recorded the root Run Interview Catalog candidate and index bindings.",
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
                _CATALOG_BINDINGS_KEY,
                request_hash,
                self._clock.utcnow(),
            )
            if isinstance(started, InvocationRecord) and started.state is JournalState.COMPLETED:
                self._validated_journal_record(
                    started,
                    idempotency_key=_CATALOG_BINDINGS_KEY,
                    request_hash=request_hash,
                    state=JournalState.COMPLETED,
                    expected_result=stable_result,
                )
                return
            self._validated_journal_record(
                started,
                idempotency_key=_CATALOG_BINDINGS_KEY,
                request_hash=request_hash,
                state=JournalState.STARTED,
            )
            completed = await self._journal.complete(
                self._scope,
                _CATALOG_BINDINGS_KEY,
                request_hash,
                stable_result,
                self._clock.utcnow(),
            )
            self._validated_journal_record(
                completed,
                idempotency_key=_CATALOG_BINDINGS_KEY,
                request_hash=request_hash,
                state=JournalState.COMPLETED,
                expected_result=stable_result,
            )
        except InvocationJournalConflict as error:
            raise _guard_error(
                "interview_catalog_bindings_conflict",
                "This root Run is already bound to a different Interview Catalog result.",
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

    async def prepare_exact_read(self, call: ToolCall) -> _CatalogTargetBinding | None:
        bindings = await self._load_catalog_bindings(required=False)
        if bindings is None:
            return None
        path = call.arguments.get("path")
        if not isinstance(path, str):
            return None
        binding = bindings.target(path)
        if binding is None or binding.content_hash is None:
            return None
        if (
            path != binding.path
            or call.arguments.get("expectedModifiedVersion") != binding.modified_version
            or call.arguments.get("expectedContentHash") != binding.content_hash
        ):
            raise _guard_error(
                "interview_submission_exact_read_required",
                "Catalog candidates and existing indexes must be read with their exact path, version, and hash.",
            )
        return binding

    async def record_exact_read(
        self,
        call: ToolCall,
        result: ToolResult,
        binding: _CatalogTargetBinding,
    ) -> None:
        if result.status is not ToolResultStatus.SUCCEEDED:
            return
        receipt = _ExactReadReceipt.from_result(result, binding)
        requested_start = call.arguments.get("lineStart", 1)
        requested_end = call.arguments.get("lineEnd")
        if receipt.line_start != requested_start or (
            isinstance(requested_end, int) and receipt.line_end > requested_end
        ):
            raise _guard_error(
                "interview_submission_exact_read_invalid",
                "Exact Vault read returned a line range outside the requested range.",
            )
        key = _exact_read_receipt_key(binding.path, receipt.line_start)
        request_hash = canonical_json_sha256(receipt.to_data())
        stable_result = ToolResult(
            tool_call_id=_exact_read_receipt_tool_call_id(key),
            status=ToolResultStatus.SUCCEEDED,
            data=receipt.to_data(),
            user_visible_summary="Recorded one exact Interview Catalog Vault read receipt.",
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
                key,
                request_hash,
                self._clock.utcnow(),
            )
            if isinstance(started, InvocationRecord) and started.state is JournalState.COMPLETED:
                self._validated_journal_record(
                    started,
                    idempotency_key=key,
                    request_hash=request_hash,
                    state=JournalState.COMPLETED,
                    expected_result=stable_result,
                )
                return
            self._validated_journal_record(
                started,
                idempotency_key=key,
                request_hash=request_hash,
                state=JournalState.STARTED,
            )
            completed = await self._journal.complete(
                self._scope,
                key,
                request_hash,
                stable_result,
                self._clock.utcnow(),
            )
            self._validated_journal_record(
                completed,
                idempotency_key=key,
                request_hash=request_hash,
                state=JournalState.COMPLETED,
                expected_result=stable_result,
            )
        except InvocationJournalConflict as error:
            raise _guard_error(
                "interview_submission_exact_read_conflict",
                "This root Run already recorded a different exact read for this Catalog range.",
            ) from error
        except ToolDispatchError:
            raise
        except Exception as error:
            raise _guard_error(
                "interview_submission_authority_unavailable",
                "Interview Submission authority journal is unavailable.",
            ) from error

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
        bindings = await self._load_catalog_bindings()
        assert bindings is not None
        if bindings.truncated:
            raise _guard_error(
                "interview_catalog_bindings_truncated",
                "A truncated Interview Catalog result cannot authorize a Vault Change Batch.",
            )
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
        review_items = submission.get("reviewItems")
        if not isinstance(review_items, Sequence) or isinstance(review_items, str):
            raise _guard_error(
                "interview_submission_review_invalid",
                "Interview Submission review items are missing or invalid.",
            )
        creates_experience = any(
            isinstance(item, Mapping)
            and item.get("kind") == "experience"
            and item.get("identity") == "new"
            and item.get("mutation") == "create"
            for item in review_items
        )
        if creates_experience and bindings.has_exact_source_experience:
            raise _guard_error(
                "interview_submission_exact_source_exists",
                "A new Interview Experience cannot be created when Catalog has an exact source match.",
            )
        await self._validate_review_plan(call, review_items, bindings)

    async def _validate_review_plan(
        self,
        call: ToolCall,
        review_items: Sequence[object],
        bindings: _CatalogBindings,
    ) -> None:
        operations = call.arguments.get("operations")
        source_bindings = call.arguments.get("sourceBindings")
        if (
            not isinstance(operations, Sequence)
            or isinstance(operations, str)
            or not isinstance(source_bindings, Sequence)
            or isinstance(source_bindings, str)
            or not (1 <= len(review_items) <= 40)
        ):
            raise _guard_error(
                "interview_submission_review_invalid",
                "Interview Submission review plan is missing or invalid.",
            )
        operation_by_path = _unique_path_mapping(operations, "interview_submission_review_invalid")
        review_by_path = _unique_path_mapping(review_items, "interview_submission_review_invalid")
        source_by_path = _unique_path_mapping(source_bindings, "interview_submission_source_binding_invalid")
        mutation_paths: set[str] = set()
        existing_paths: set[str] = set()
        new_entity_kinds: set[str] = set()
        for folded_path, raw_item in review_by_path.items():
            item = _review_item(raw_item)
            _validate_review_kind_path(item["kind"], item["path"])
            operation = operation_by_path.get(folded_path)
            identity = item["identity"]
            mutation = item["mutation"]
            if mutation == "none":
                if identity != "existing" or operation is not None:
                    raise _guard_error(
                        "interview_submission_review_invalid",
                        "A no-op review item must be an existing entity without a Vault operation.",
                    )
            else:
                if operation is None:
                    raise _guard_error(
                        "interview_submission_review_invalid",
                        "Every Interview mutation review item must match one Vault operation.",
                    )
                mutation_paths.add(folded_path)
                operation_kind = operation.get("op")
                if (mutation == "create") != (operation_kind == "create"):
                    raise _guard_error(
                        "interview_submission_review_invalid",
                        "Review mutation disposition does not match its Vault operation.",
                    )
                if (identity == "new") != (mutation == "create"):
                    raise _guard_error(
                        "interview_submission_review_invalid",
                        "New Interview entities must be creates; existing entities must not be creates.",
                    )
            if identity == "existing":
                existing_paths.add(folded_path)
                target = bindings.target(item["path"])
                source = source_by_path.get(folded_path)
                if (
                    target is None
                    or target.kind != item["kind"]
                    or target.content_hash is None
                    or source is None
                    or source.get("path") != target.path
                    or source.get("expectedModifiedVersion") != target.modified_version
                    or source.get("expectedContentHash") != target.content_hash
                ):
                    raise _guard_error(
                        "interview_submission_source_binding_invalid",
                        "Existing review items must match one exact Catalog source binding.",
                    )
                if operation is not None and (
                    operation.get("path") != target.path
                    or operation.get("expectedModifiedVersion") != target.modified_version
                    or operation.get("expectedContentHash") != target.content_hash
                ):
                    raise _guard_error(
                        "interview_submission_source_binding_invalid",
                        "Existing Vault operations must retain their exact Catalog version and hash.",
                    )
                await self._require_complete_exact_read(target)
            else:
                if item["kind"] in {"experience", "question"}:
                    new_entity_kinds.add(item["kind"])
                target = bindings.target(item["path"])
                if item["kind"] == "index":
                    if target is None or target.kind != "index" or target.content_hash is not None:
                        raise _guard_error(
                            "interview_submission_source_binding_invalid",
                            "A new index must match the Catalog's missing-index binding.",
                        )
                elif target is not None:
                    raise _guard_error(
                        "interview_submission_source_binding_invalid",
                        "A new Interview entity path must not already be a Catalog candidate.",
                    )
        if mutation_paths != set(operation_by_path):
            raise _guard_error(
                "interview_submission_review_invalid",
                "Every Interview Vault operation must have exactly one matching review item.",
            )
        if existing_paths != set(source_by_path):
            raise _guard_error(
                "interview_submission_source_binding_invalid",
                "Interview source bindings must exactly match existing review items.",
            )
        required_index_paths = {
            index_path.casefold()
            for entity_kind, index_path in (
                ("experience", "experiences/index.md"),
                ("question", "interview/index.md"),
            )
            if entity_kind in new_entity_kinds
        }
        mutated_index_paths = {
            folded_path
            for folded_path, item in review_by_path.items()
            if item.get("kind") == "index" and item.get("mutation") != "none"
        }
        if mutated_index_paths != required_index_paths:
            raise _guard_error(
                "interview_submission_review_invalid",
                "Primary Interview indexes must change exactly when their entity kind has a new item.",
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

    async def _load_catalog_bindings(self, *, required: bool = True) -> _CatalogBindings | None:
        try:
            record = await self._journal.get(self._scope, _CATALOG_BINDINGS_KEY)
        except Exception as error:
            raise _guard_error(
                "interview_submission_authority_unavailable",
                "Interview Submission authority journal is unavailable.",
            ) from error
        if record is None:
            if not required:
                return None
            raise _guard_error(
                "interview_catalog_bindings_missing",
                "A successful Interview Catalog candidate binding is required before apply.",
            )
        record = self._validated_journal_record(
            record,
            idempotency_key=_CATALOG_BINDINGS_KEY,
            request_hash=None,
            state=JournalState.COMPLETED,
            error_code="interview_catalog_bindings_invalid",
            error_message="The durable Interview Catalog candidate binding is corrupt.",
        )
        assert record.result is not None
        bindings = _CatalogBindings.from_value(record.result.data)
        if (
            record.result.status is not ToolResultStatus.SUCCEEDED
            or record.result.tool_call_id != _CATALOG_BINDINGS_TOOL_CALL_ID
            or record.result.data is None
            or record.request_hash != canonical_json_sha256(record.result.data)
        ):
            raise _guard_error(
                "interview_catalog_bindings_invalid",
                "The durable Interview Catalog candidate binding is corrupt.",
            )
        return bindings

    async def _require_complete_exact_read(self, binding: _CatalogTargetBinding) -> None:
        if binding.content_hash is None:
            raise _guard_error(
                "interview_submission_exact_read_missing",
                "An existing Interview review target must have a Catalog content binding.",
            )
        line_start = 1
        for _ in range(_MAX_EXACT_READ_RECEIPTS_PER_TARGET):
            key = _exact_read_receipt_key(binding.path, line_start)
            try:
                record = await self._journal.get(self._scope, key)
            except Exception as error:
                raise _guard_error(
                    "interview_submission_authority_unavailable",
                    "Interview Submission authority journal is unavailable.",
                ) from error
            if record is None:
                raise _guard_error(
                    "interview_submission_exact_read_missing",
                    "Every existing Interview review target requires a complete exact Vault read.",
                )
            record = self._validated_journal_record(
                record,
                idempotency_key=key,
                request_hash=None,
                state=JournalState.COMPLETED,
                error_code="interview_submission_exact_read_invalid",
                error_message="A durable exact Vault read receipt is corrupt.",
            )
            assert record.result is not None
            receipt = _ExactReadReceipt.from_result(record.result, binding)
            if (
                record.result.status is not ToolResultStatus.SUCCEEDED
                or record.result.tool_call_id != _exact_read_receipt_tool_call_id(key)
                or record.request_hash != canonical_json_sha256(receipt.to_data())
                or receipt.line_start != line_start
            ):
                raise _guard_error(
                    "interview_submission_exact_read_invalid",
                    "A durable exact Vault read receipt is corrupt.",
                )
            if not receipt.truncated:
                return
            line_start = receipt.line_end + 1
        raise _guard_error(
            "interview_submission_exact_read_invalid",
            "Exact Vault read receipts exceed the bounded range chain.",
        )


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

        definitions = {definition.name: definition for definition in plugin_tool_definitions()}
        self._catalog_definition = definitions["interview_catalog.search"]
        self._vault_read_definition = definitions["vault.read"]
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
            await self._store.record_catalog_bindings(result)
            return result
        if call.name == "vault.read":
            binding = await self._store.prepare_exact_read(call)
            result = await self._delegate.execute(call, cancellation)
            if result.tool_call_id != call.tool_call_id:
                raise _guard_error(
                    "interview_submission_exact_read_invalid",
                    "Vault read returned a result bound to another ToolCall.",
                )
            if binding is not None and result.status is ToolResultStatus.SUCCEEDED:
                try:
                    if call.definition_fingerprint != self._vault_read_definition.fingerprint:
                        raise ValueError("Vault read definition fingerprint differs from the fixed plugin contract")
                    self._validator.validate_output(self._vault_read_definition, result.data)
                except (ToolValidationError, TypeError, ValueError) as error:
                    raise _guard_error(
                        "interview_submission_exact_read_invalid",
                        "Vault read succeeded with an output that does not match its fixed schema.",
                    ) from error
                await self._store.record_exact_read(call, result, binding)
            return result
        if call.name == "vault.changes.apply" and call.arguments.get("changeKind") == "interview_submission":
            await self._store.verify_apply_claim(call)
        return await self._delegate.execute(call, cancellation)


def _string_tuple(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, str) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _catalog_target_binding(
    *,
    kind: str,
    value: Mapping[str, Any],
    exact_source_match: object,
    content_hash_required: bool = True,
) -> _CatalogTargetBinding:
    path = value.get("path")
    modified_version = value.get("modifiedVersion")
    content_hash = value.get("contentHash")
    if (
        not isinstance(path, str)
        or not path
        or not isinstance(modified_version, str)
        or not modified_version
        or not isinstance(exact_source_match, bool)
        or (content_hash_required and (not isinstance(content_hash, str) or _SHA256.fullmatch(content_hash) is None))
        or (
            not content_hash_required
            and content_hash is not None
            and (not isinstance(content_hash, str) or _SHA256.fullmatch(content_hash) is None)
        )
    ):
        raise _guard_error(
            "interview_catalog_bindings_invalid",
            "Catalog candidate and index bindings are missing or invalid.",
        )
    return _CatalogTargetBinding(
        kind=kind,
        path=path,
        modified_version=modified_version,
        content_hash=content_hash,
        exact_source_match=exact_source_match,
    )


def _unique_path_mapping(values: Sequence[object], error_code: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            raise _guard_error(error_code, "Interview Submission path bindings are invalid.")
        path = value.get("path")
        if not isinstance(path, str) or not path:
            raise _guard_error(error_code, "Interview Submission path bindings are invalid.")
        folded = path.casefold()
        if folded in result:
            raise _guard_error(error_code, "Interview Submission paths must be unique ignoring case.")
        result[folded] = value
    return result


def _review_item(value: Mapping[str, Any]) -> dict[str, str]:
    expected_fields = {"kind", "path", "identity", "mutation"}
    if set(value) != expected_fields:
        raise _guard_error(
            "interview_submission_review_invalid",
            "Interview Submission review items have invalid fields.",
        )
    kind = value.get("kind")
    path = value.get("path")
    identity = value.get("identity")
    mutation = value.get("mutation")
    if (
        kind not in {"experience", "question", "index"}
        or not isinstance(path, str)
        or identity not in {"new", "existing"}
        or mutation not in {"create", "modify", "none"}
    ):
        raise _guard_error(
            "interview_submission_review_invalid",
            "Interview Submission review items are invalid.",
        )
    return {"kind": kind, "path": path, "identity": identity, "mutation": mutation}


def _validate_review_kind_path(kind: str, path: str) -> None:
    valid = (
        (kind == "index" and path in _INDEX_PATHS)
        or (kind == "experience" and path not in _INDEX_PATHS and _EXPERIENCE_PATH.fullmatch(path) is not None)
        or (kind == "question" and path not in _INDEX_PATHS and _QUESTION_PATH.fullmatch(path) is not None)
    )
    if not valid:
        raise _guard_error(
            "interview_submission_review_invalid",
            "Interview Submission review kind does not match its Vault path.",
        )


def _exact_read_receipt_key(path: str, line_start: int) -> str:
    digest = canonical_json_sha256({"path": path.casefold(), "lineStart": line_start})
    return f"{_EXACT_READ_RECEIPT_KEY_PREFIX}:{digest.removeprefix('sha256:')}"


def _exact_read_receipt_tool_call_id(key: str) -> str:
    return f"interview-submission-{key}"


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
