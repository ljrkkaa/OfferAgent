from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.permissions import (
    CapabilityScope,
    PermissionMode,
    PolicyContext,
    PolicyDecision,
    PolicyDisposition,
)
from offeragent_harness.runtime.interview_submission_authority import (
    InterviewSubmissionAuthorityPolicy,
    InterviewSubmissionRunAuthority,
    InterviewSubmissionToolExecutor,
)
from offeragent_harness.runtime.plugin_tools import plugin_tool_definitions
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import ManualCancellationToken, ManualClock
from offeragent_harness.tools import (
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
    ToolValidator,
)
from offeragent_harness.tools.dispatcher import ToolDispatchError

NOW = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)
FIRST_HASH = "sha256:" + "a" * 64
SECOND_HASH = "sha256:" + "b" * 64
SOURCE_FINGERPRINT = "sha256:0cd65e3ab3208489c6bfaa9d2ce2cb125ae8fbef34c9a965c1efe3f7d2302c37"
CANONICAL_URL = "https://example.com/interview/42"


class _AllowPolicy:
    async def evaluate(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: PolicyContext,
    ) -> PolicyDecision:
        del call, context
        return PolicyDecision(
            PolicyDisposition.ALLOW,
            definition.risk,
            "test_allow",
            "Allowed by the test policy.",
        )


class _PolicyAudit:
    def __init__(self) -> None:
        self.records: list[object] = []

    async def record(self, record: object) -> None:
        self.records.append(record)


def _definition(name: str) -> ToolDefinition:
    return next(item for item in plugin_tool_definitions() if item.name == name)


def _call(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    *,
    call_id: str,
    lineage: AgentLineage | None = None,
) -> ToolCall:
    validated = ToolValidator().validate_arguments(definition, arguments)
    active_lineage = lineage or AgentLineage.root("run_root")
    return ToolCall(
        tool_call_id=call_id,
        run_id=active_lineage.run_id,
        workspace_id="ws_vault",
        name=definition.name,
        version=definition.version,
        arguments=validated.arguments,
        args_hash=validated.args_hash,
        idempotency_key=f"idem-{call_id}",
        deadline=NOW.replace(hour=7),
        lineage=active_lineage,
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


def _context(definition: ToolDefinition, *, run_id: str = "run_root") -> PolicyContext:
    return PolicyContext(
        workspace_id="ws_vault",
        session_id="session_interview",
        principal_id="profile_local",
        run_id=run_id,
        permission_mode=PermissionMode.TRUSTED_WORKSPACE,
        effective_scope=CapabilityScope(
            allowed_tools=frozenset({definition.name}),
            denied_tools=frozenset(),
            allowed_risks=frozenset({definition.risk}),
            root_capabilities=definition.required_capabilities,
            allow_network=True,
            allow_secret_handles=False,
        ),
        workspace_trusted=True,
        now=NOW,
    )


def _policy(tmp_path: Path) -> InterviewSubmissionAuthorityPolicy:
    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    return InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=InterviewSubmissionRunAuthority(
            captured_on=date(2026, 7, 18),
            ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
        ),
        journal=journal,
        clock=ManualClock(NOW),
        downstream=_AllowPolicy(),
        audit_sink=_PolicyAudit(),
    )


class _PluginBoundary:
    def __init__(self, *, catalog_truncated: bool = False) -> None:
        self.calls: list[ToolCall] = []
        self.catalog_truncated = catalog_truncated

    async def execute(self, call: ToolCall, cancellation: object) -> ToolResult:
        del cancellation
        self.calls.append(call)
        if call.name == "interview_catalog.search":
            return ToolResult(
                tool_call_id=call.tool_call_id,
                status=ToolResultStatus.SUCCEEDED,
                data={
                    "normalizedSource": {
                        "canonicalUrls": [CANONICAL_URL],
                        "sourceFingerprint": SOURCE_FINGERPRINT,
                        "orderedImageContentHashes": [FIRST_HASH, SECOND_HASH],
                    },
                    "experienceCandidates": [],
                    "questionCandidates": [],
                    "indexes": [
                        {
                            "kind": "experience",
                            "path": "experiences/index.md",
                            "exists": False,
                            "modifiedVersion": "missing",
                        },
                        {
                            "kind": "question",
                            "path": "interview/index.md",
                            "exists": False,
                            "modifiedVersion": "missing",
                        },
                    ],
                    "truncated": self.catalog_truncated,
                },
                user_visible_summary="Catalog source normalized.",
                artifact_ids=(),
                source_refs=(),
                side_effects=(),
                retryable=False,
                before_state=None,
                after_state=None,
                error=None,
            )
        return ToolResult(
            tool_call_id=call.tool_call_id,
            status=ToolResultStatus.FAILED,
            data=None,
            user_visible_summary="User rejected the Interview Submission preview.",
            artifact_ids=(),
            source_refs=(),
            side_effects=(),
            retryable=False,
            before_state=None,
            after_state=None,
            error=ToolError(
                code="preview_rejected",
                message="User rejected the preview.",
                retryable=False,
                cancelled=False,
            ),
        )


class _WrongTypeJournal:
    def __init__(self, delegate: object, *, operation: str, key: str) -> None:
        self._delegate = delegate
        self._operation = operation
        self._key = key

    @staticmethod
    def _wrong_type(record: object) -> object:
        return SimpleNamespace(**vars(record))

    async def get(self, scope: str, idempotency_key: str) -> object | None:
        record: object | None = await self._delegate.get(scope, idempotency_key)  # type: ignore[attr-defined]
        if self._operation == "get" and idempotency_key == self._key and record is not None:
            return self._wrong_type(record)
        return record

    async def start(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        started_at: datetime,
    ) -> object:
        record = await self._delegate.start(  # type: ignore[attr-defined]
            scope,
            idempotency_key,
            request_hash,
            started_at,
        )
        if self._operation == "start" and idempotency_key == self._key:
            return self._wrong_type(record)
        return record

    async def complete(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        result: ToolResult,
        completed_at: datetime,
    ) -> object:
        record = await self._delegate.complete(  # type: ignore[attr-defined]
            scope,
            idempotency_key,
            request_hash,
            result,
            completed_at,
        )
        if self._operation == "complete" and idempotency_key == self._key:
            return self._wrong_type(record)
        return record


def _interview_batch(batch_id: str) -> dict[str, Any]:
    return {
        "batchId": batch_id,
        "task": "Ingest one Interview Submission",
        "changeKind": "interview_submission",
        "sourceBindings": [],
        "interviewSubmission": {
            "sourceKind": "mixed",
            "capturedOn": "2026-07-18",
            "canonicalUrls": [CANONICAL_URL],
            "orderedImageContentHashes": [FIRST_HASH, SECOND_HASH],
            "sourceFingerprint": SOURCE_FINGERPRINT,
            "reviewItems": [
                {
                    "kind": "experience",
                    "path": "experiences/example.md",
                    "identity": "new",
                    "mutation": "create",
                },
                {
                    "kind": "index",
                    "path": "experiences/index.md",
                    "identity": "new",
                    "mutation": "create",
                },
            ],
        },
        "operations": [
            {
                "op": "create",
                "path": "experiences/example.md",
                "content": "# Interview Experience\n",
                "expectedContentHash": "absent",
                "expectedModifiedVersion": "missing",
            },
            {
                "op": "create",
                "path": "experiences/index.md",
                "content": "# Interview Experiences\n\n- [[example]]\n",
                "expectedContentHash": "absent",
                "expectedModifiedVersion": "missing",
            },
        ],
    }


@pytest.mark.asyncio
async def test_catalog_cannot_drop_or_reorder_store_authoritative_images(tmp_path: Path) -> None:
    definition = _definition("interview_catalog.search")
    policy = _policy(tmp_path)

    dropped = await policy.evaluate(
        definition,
        _call(
            definition,
            {"orderedImageContentHashes": [FIRST_HASH]},
            call_id="catalog-dropped",
        ),
        _context(definition),
    )
    reordered = await policy.evaluate(
        definition,
        _call(
            definition,
            {"orderedImageContentHashes": [SECOND_HASH, FIRST_HASH]},
            call_id="catalog-reordered",
        ),
        _context(definition),
    )

    assert dropped.disposition is PolicyDisposition.DENY
    assert dropped.reason_code == "interview_catalog_attachment_authority_mismatch"
    assert reordered.disposition is PolicyDisposition.DENY
    assert reordered.reason_code == "interview_catalog_attachment_authority_mismatch"


@pytest.mark.asyncio
async def test_catalog_receipt_binds_one_apply_slot_across_executor_reconstruction(tmp_path: Path) -> None:
    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )
    clock = ManualClock(NOW)
    audit = _PolicyAudit()
    plugin = _PluginBoundary()
    policy = InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=clock,
        downstream=_AllowPolicy(),
        audit_sink=audit,
    )
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=clock,
        delegate=plugin,
    )
    catalog_definition = _definition("interview_catalog.search")
    catalog_call = _call(
        catalog_definition,
        {
            "sourceUrls": ["https://example.com/interview/42?utm_source=feed"],
            "orderedImageContentHashes": [FIRST_HASH, SECOND_HASH],
        },
        call_id="catalog-valid",
    )

    assert (
        await policy.evaluate(catalog_definition, catalog_call, _context(catalog_definition))
    ).disposition is PolicyDisposition.ALLOW
    assert (await executor.execute(catalog_call, ManualCancellationToken())).status is ToolResultStatus.SUCCEEDED

    # Reconstruct both Run-bound adapters: receipt and claim authority must survive process-local state.
    policy = InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=clock,
        downstream=_AllowPolicy(),
        audit_sink=audit,
    )
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=clock,
        delegate=plugin,
    )
    apply_definition = _definition("vault.changes.apply")
    first = _call(
        apply_definition,
        _interview_batch("interview_submission_first"),
        call_id="apply-first",
    )

    assert (await policy.evaluate(apply_definition, first, _context(apply_definition))).disposition is (
        PolicyDisposition.ALLOW
    )
    assert (await executor.execute(first, ManualCancellationToken())).status is ToolResultStatus.FAILED
    assert (await policy.evaluate(apply_definition, first, _context(apply_definition))).disposition is (
        PolicyDisposition.ALLOW
    )
    assert (await executor.execute(first, ManualCancellationToken())).status is ToolResultStatus.FAILED

    second = _call(
        apply_definition,
        _interview_batch("interview_submission_second"),
        call_id="apply-second",
    )
    denied = await policy.evaluate(apply_definition, second, _context(apply_definition))

    assert denied.disposition is PolicyDisposition.DENY
    assert denied.reason_code == "interview_submission_batch_already_claimed"
    assert [call.tool_call_id for call in plugin.calls] == ["catalog-valid", "apply-first", "apply-first"]


@pytest.mark.asyncio
async def test_truncated_catalog_cannot_authorize_an_interview_write(tmp_path: Path) -> None:
    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )
    clock = ManualClock(NOW)
    policy = InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=clock,
        downstream=_AllowPolicy(),
        audit_sink=_PolicyAudit(),
    )
    plugin = _PluginBoundary(catalog_truncated=True)
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=clock,
        delegate=plugin,
    )
    catalog_definition = _definition("interview_catalog.search")
    catalog_call = _call(
        catalog_definition,
        {
            "sourceUrls": [CANONICAL_URL],
            "orderedImageContentHashes": [FIRST_HASH, SECOND_HASH],
        },
        call_id="catalog-truncated",
    )
    assert (
        await policy.evaluate(catalog_definition, catalog_call, _context(catalog_definition))
    ).disposition is PolicyDisposition.ALLOW
    assert (await executor.execute(catalog_call, ManualCancellationToken())).status is ToolResultStatus.SUCCEEDED
    apply_definition = _definition("vault.changes.apply")
    apply_call = _call(
        apply_definition,
        _interview_batch("interview_submission_truncated"),
        call_id="apply-truncated",
    )

    denied = await policy.evaluate(apply_definition, apply_call, _context(apply_definition))

    assert denied.disposition is PolicyDisposition.DENY
    assert denied.reason_code == "interview_catalog_bindings_truncated"
    assert [call.tool_call_id for call in plugin.calls] == ["catalog-truncated"]


@pytest.mark.asyncio
async def test_exact_catalog_source_match_rejects_a_new_experience_before_tool_started(tmp_path: Path) -> None:
    existing_hash = "sha256:" + "c" * 64

    class _ExactSourcePlugin(_PluginBoundary):
        async def execute(self, call: ToolCall, cancellation: object) -> ToolResult:
            result = await super().execute(call, cancellation)
            assert isinstance(result.data, Mapping)
            return ToolResult(
                tool_call_id=result.tool_call_id,
                status=ToolResultStatus.SUCCEEDED,
                data={
                    **dict(result.data),
                    "experienceCandidates": [
                        {
                            "path": "experiences/existing.md",
                            "experienceId": "experience_existing",
                            "sourceKind": "mixed",
                            "sourceUrl": CANONICAL_URL,
                            "sourceFingerprint": SOURCE_FINGERPRINT,
                            "exactSourceMatch": True,
                            "contentHash": existing_hash,
                            "modifiedVersion": "mtime:1:size:240",
                        }
                    ],
                },
                user_visible_summary=result.user_visible_summary,
                artifact_ids=(),
                source_refs=(),
                side_effects=(),
                retryable=False,
                before_state=None,
                after_state=None,
                error=None,
            )

    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )
    plugin = _ExactSourcePlugin()
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=ManualClock(NOW),
        delegate=plugin,
    )
    catalog_definition = _definition("interview_catalog.search")
    catalog = _call(
        catalog_definition,
        {
            "sourceUrls": [CANONICAL_URL],
            "orderedImageContentHashes": [FIRST_HASH, SECOND_HASH],
        },
        call_id="catalog-exact-source",
    )
    await executor.execute(catalog, ManualCancellationToken())

    apply_definition = _definition("vault.changes.apply")
    apply = _call(
        apply_definition,
        _interview_batch("new-despite-exact-source"),
        call_id="apply-new-despite-exact-source",
    )
    policy = InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=ManualClock(NOW),
        downstream=_AllowPolicy(),
        audit_sink=_PolicyAudit(),
    )

    decision = await policy.evaluate(apply_definition, apply, _context(apply_definition))

    assert decision.disposition is PolicyDisposition.DENY
    assert decision.reason_code == "interview_submission_exact_source_exists"
    assert [item.tool_call_id for item in plugin.calls] == ["catalog-exact-source"]


@pytest.mark.asyncio
async def test_existing_merge_requires_a_durable_exact_full_read_receipt(tmp_path: Path) -> None:
    existing_path = "experiences/existing.md"
    existing_content = "# Existing Interview Experience\n"
    existing_hash = "sha256:d949c1953334247890d30bb6ccb3e1b3835455d719bb2bbad73cafd59015cd74"
    existing_version = "mtime:1:size:32"

    class _ExistingCandidatePlugin(_PluginBoundary):
        async def execute(self, call: ToolCall, cancellation: object) -> ToolResult:
            result = await super().execute(call, cancellation)
            if call.name == "interview_catalog.search":
                assert isinstance(result.data, Mapping)
                return ToolResult(
                    tool_call_id=call.tool_call_id,
                    status=ToolResultStatus.SUCCEEDED,
                    data={
                        **dict(result.data),
                        "experienceCandidates": [
                            {
                                "path": existing_path,
                                "experienceId": "experience_existing",
                                "sourceKind": "public_url",
                                "sourceUrl": "https://example.com/interview/prior",
                                "exactSourceMatch": False,
                                "contentHash": existing_hash,
                                "modifiedVersion": existing_version,
                            }
                        ],
                    },
                    user_visible_summary=result.user_visible_summary,
                    artifact_ids=(),
                    source_refs=(),
                    side_effects=(),
                    retryable=False,
                    before_state=None,
                    after_state=None,
                    error=None,
                )
            if call.name == "vault.read":
                return ToolResult(
                    tool_call_id=call.tool_call_id,
                    status=ToolResultStatus.SUCCEEDED,
                    data={
                        "path": existing_path,
                        "lineStart": 1,
                        "lineEnd": 1,
                        "modifiedVersion": existing_version,
                        "contentHash": existing_hash,
                        "content": existing_content,
                        "truncated": False,
                    },
                    user_visible_summary="Read the exact existing Experience.",
                    artifact_ids=(),
                    source_refs=(),
                    side_effects=(),
                    retryable=False,
                    before_state=None,
                    after_state=None,
                    error=None,
                )
            return result

    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )
    plugin = _ExistingCandidatePlugin()
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=ManualClock(NOW),
        delegate=plugin,
    )
    policy = InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=ManualClock(NOW),
        downstream=_AllowPolicy(),
        audit_sink=_PolicyAudit(),
    )
    catalog_definition = _definition("interview_catalog.search")
    await executor.execute(
        _call(
            catalog_definition,
            {
                "sourceUrls": [CANONICAL_URL],
                "orderedImageContentHashes": [FIRST_HASH, SECOND_HASH],
            },
            call_id="catalog-existing-candidate",
        ),
        ManualCancellationToken(),
    )
    apply_arguments = _interview_batch("merge-existing")
    assert isinstance(apply_arguments["interviewSubmission"], dict)
    apply_arguments["interviewSubmission"]["reviewItems"] = [
        {
            "kind": "experience",
            "path": existing_path,
            "identity": "existing",
            "mutation": "modify",
        }
    ]
    apply_arguments["sourceBindings"] = [
        {
            "path": existing_path,
            "expectedModifiedVersion": existing_version,
            "expectedContentHash": existing_hash,
        }
    ]
    apply_arguments["operations"] = [
        {
            "op": "append",
            "path": existing_path,
            "content": "\n## Additional context\n",
            "expectedContentHash": existing_hash,
            "expectedModifiedVersion": existing_version,
        }
    ]
    apply_definition = _definition("vault.changes.apply")
    apply = _call(apply_definition, apply_arguments, call_id="apply-merge-existing")

    missing_receipt = await policy.evaluate(apply_definition, apply, _context(apply_definition))

    assert missing_receipt.disposition is PolicyDisposition.DENY
    assert missing_receipt.reason_code == "interview_submission_exact_read_missing"

    read_definition = _definition("vault.read")
    read = _call(
        read_definition,
        {
            "path": existing_path,
            "expectedModifiedVersion": existing_version,
            "expectedContentHash": existing_hash,
        },
        call_id="read-existing-candidate",
    )
    assert (await executor.execute(read, ManualCancellationToken())).status is ToolResultStatus.SUCCEEDED

    allowed = await policy.evaluate(apply_definition, apply, _context(apply_definition))

    assert allowed.disposition is PolicyDisposition.ALLOW


@pytest.mark.asyncio
async def test_new_experience_without_its_primary_index_mutation_is_rejected(tmp_path: Path) -> None:
    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )
    plugin = _PluginBoundary()
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=ManualClock(NOW),
        delegate=plugin,
    )
    catalog_definition = _definition("interview_catalog.search")
    await executor.execute(
        _call(
            catalog_definition,
            {
                "sourceUrls": [CANONICAL_URL],
                "orderedImageContentHashes": [FIRST_HASH, SECOND_HASH],
            },
            call_id="catalog-before-orphan-create",
        ),
        ManualCancellationToken(),
    )
    arguments = _interview_batch("orphan-experience")
    submission = arguments["interviewSubmission"]
    assert isinstance(submission, dict)
    submission["reviewItems"] = [submission["reviewItems"][0]]
    arguments["operations"] = [arguments["operations"][0]]
    apply_definition = _definition("vault.changes.apply")
    policy = InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=ManualClock(NOW),
        downstream=_AllowPolicy(),
        audit_sink=_PolicyAudit(),
    )

    decision = await policy.evaluate(
        apply_definition,
        _call(apply_definition, arguments, call_id="apply-orphan-experience"),
        _context(apply_definition),
    )

    assert decision.disposition is PolicyDisposition.DENY
    assert decision.reason_code == "interview_submission_review_invalid"


def test_run_authority_durable_snapshot_rejects_attachment_or_fingerprint_drift() -> None:
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )

    snapshot = authority.durable_snapshot()

    assert snapshot == {
        "schemaVersion": 1,
        "capturedOn": "2026-07-18",
        "orderedImageContentHashes": [FIRST_HASH, SECOND_HASH],
        "sourceFingerprint": SOURCE_FINGERPRINT,
    }
    assert InterviewSubmissionRunAuthority.from_durable_snapshot(snapshot) == authority
    with pytest.raises(ValueError, match="fingerprint"):
        InterviewSubmissionRunAuthority.from_durable_snapshot({**snapshot, "sourceFingerprint": "sha256:" + "f" * 64})
    with pytest.raises(ValueError, match="fields"):
        InterviewSubmissionRunAuthority.from_durable_snapshot({**snapshot, "unexpected": True})


@pytest.mark.asyncio
async def test_child_lineage_shares_the_root_catalog_receipt_and_single_apply_claim(tmp_path: Path) -> None:
    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )
    clock = ManualClock(NOW)
    audit = _PolicyAudit()
    plugin = _PluginBoundary()
    child_lineage = AgentLineage.root("run_root").child("run_child", "researcher")

    child_policy = InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=clock,
        downstream=_AllowPolicy(),
        audit_sink=audit,
    )
    child_executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=clock,
        delegate=plugin,
    )
    catalog_definition = _definition("interview_catalog.search")
    child_catalog = _call(
        catalog_definition,
        {
            "sourceUrls": [CANONICAL_URL],
            "orderedImageContentHashes": [FIRST_HASH, SECOND_HASH],
        },
        call_id="child-catalog",
        lineage=child_lineage,
    )
    assert (
        await child_policy.evaluate(
            catalog_definition,
            child_catalog,
            _context(catalog_definition, run_id="run_child"),
        )
    ).disposition is PolicyDisposition.ALLOW
    await child_executor.execute(child_catalog, ManualCancellationToken())

    root_policy = InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=clock,
        downstream=_AllowPolicy(),
        audit_sink=audit,
    )
    apply_definition = _definition("vault.changes.apply")
    root_apply = _call(
        apply_definition,
        _interview_batch("root_batch"),
        call_id="root-apply",
    )
    assert (await root_policy.evaluate(apply_definition, root_apply, _context(apply_definition))).disposition is (
        PolicyDisposition.ALLOW
    )

    child_apply = _call(
        apply_definition,
        _interview_batch("child_batch"),
        call_id="child-apply",
        lineage=child_lineage,
    )
    denied = await child_policy.evaluate(
        apply_definition,
        child_apply,
        _context(apply_definition, run_id="run_child"),
    )

    assert denied.disposition is PolicyDisposition.DENY
    assert denied.reason_code == "interview_submission_batch_already_claimed"


@pytest.mark.asyncio
async def test_apply_requires_every_exact_catalog_and_attachment_authority_field(tmp_path: Path) -> None:
    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )
    clock = ManualClock(NOW)
    plugin = _PluginBoundary()
    policy = InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=clock,
        downstream=_AllowPolicy(),
        audit_sink=_PolicyAudit(),
    )
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=clock,
        delegate=plugin,
    )
    catalog_definition = _definition("interview_catalog.search")
    catalog = _call(
        catalog_definition,
        {
            "sourceUrls": [CANONICAL_URL],
            "orderedImageContentHashes": [FIRST_HASH, SECOND_HASH],
        },
        call_id="catalog-exact-fields",
    )
    await executor.execute(catalog, ManualCancellationToken())

    apply_definition = _definition("vault.changes.apply")
    mismatches: tuple[tuple[str, object], ...] = (
        ("capturedOn", "2026-07-19"),
        ("canonicalUrls", ["https://example.com/interview/other"]),
        ("orderedImageContentHashes", [SECOND_HASH, FIRST_HASH]),
        ("sourceFingerprint", "sha256:" + "f" * 64),
    )
    for index, (field, value) in enumerate(mismatches):
        arguments = _interview_batch(f"mismatch_{index}")
        arguments["interviewSubmission"] = {
            **arguments["interviewSubmission"],
            field: value,
        }
        decision = await policy.evaluate(
            apply_definition,
            _call(apply_definition, arguments, call_id=f"apply-mismatch-{index}"),
            _context(apply_definition),
        )
        assert decision.disposition is PolicyDisposition.DENY
        assert decision.reason_code == "interview_submission_authority_mismatch"


@pytest.mark.asyncio
async def test_catalog_result_cannot_forge_the_store_attachment_receipt(tmp_path: Path) -> None:
    class _ForgingPlugin(_PluginBoundary):
        async def execute(self, call: ToolCall, cancellation: object) -> ToolResult:
            result = await super().execute(call, cancellation)
            assert isinstance(result.data, Mapping)
            return ToolResult(
                tool_call_id=result.tool_call_id,
                status=ToolResultStatus.SUCCEEDED,
                data={
                    **dict(result.data),
                    "normalizedSource": {
                        "canonicalUrls": [CANONICAL_URL],
                        "sourceFingerprint": SOURCE_FINGERPRINT,
                        "orderedImageContentHashes": [SECOND_HASH, FIRST_HASH],
                    },
                },
                user_visible_summary=result.user_visible_summary,
                artifact_ids=(),
                source_refs=(),
                side_effects=(),
                retryable=False,
                before_state=None,
                after_state=None,
                error=None,
            )

    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=ManualClock(NOW),
        delegate=_ForgingPlugin(),
    )
    definition = _definition("interview_catalog.search")
    call = _call(
        definition,
        {"orderedImageContentHashes": [FIRST_HASH, SECOND_HASH]},
        call_id="catalog-forged-result",
    )

    with pytest.raises(ToolDispatchError) as caught:
        await executor.execute(call, ManualCancellationToken())

    assert caught.value.code == "interview_catalog_receipt_authority_mismatch"
    assert caught.value.side_effect_possible is False


@pytest.mark.asyncio
async def test_catalog_receipt_rejects_a_success_result_that_fails_the_catalog_output_schema(tmp_path: Path) -> None:
    class _MalformedPlugin(_PluginBoundary):
        async def execute(self, call: ToolCall, cancellation: object) -> ToolResult:
            result = await super().execute(call, cancellation)
            assert isinstance(result.data, Mapping)
            return ToolResult(
                tool_call_id=result.tool_call_id,
                status=ToolResultStatus.SUCCEEDED,
                data={"normalizedSource": result.data["normalizedSource"]},
                user_visible_summary=result.user_visible_summary,
                artifact_ids=(),
                source_refs=(),
                side_effects=(),
                retryable=False,
                before_state=None,
                after_state=None,
                error=None,
            )

    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=ManualClock(NOW),
        delegate=_MalformedPlugin(),
    )
    definition = _definition("interview_catalog.search")
    call = _call(
        definition,
        {"orderedImageContentHashes": [FIRST_HASH, SECOND_HASH]},
        call_id="catalog-malformed-output",
    )

    with pytest.raises(ToolDispatchError) as caught:
        await executor.execute(call, ManualCancellationToken())

    assert caught.value.code == "interview_catalog_output_invalid"
    assert caught.value.side_effect_possible is False


@pytest.mark.asyncio
async def test_apply_rejects_a_catalog_receipt_with_a_corrupt_journal_binding(tmp_path: Path) -> None:
    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    corrupt_hash = "sha256:" + "f" * 64
    corrupt_result = ToolResult(
        tool_call_id="not-the-fixed-receipt",
        status=ToolResultStatus.SUCCEEDED,
        data={
            "canonicalUrls": [CANONICAL_URL],
            "orderedImageContentHashes": [FIRST_HASH, SECOND_HASH],
            "sourceFingerprint": SOURCE_FINGERPRINT,
        },
        user_visible_summary="Injected corrupt receipt.",
        artifact_ids=(),
        source_refs=(),
        side_effects=(),
        retryable=False,
        before_state=None,
        after_state=None,
        error=None,
    )
    scope = "ws_vault:interview-submission-authority:run_root"
    await journal.start(scope, "catalog-normalized-source", corrupt_hash, NOW)
    await journal.complete(scope, "catalog-normalized-source", corrupt_hash, corrupt_result, NOW)
    apply_definition = _definition("vault.changes.apply")
    policy = InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=InterviewSubmissionRunAuthority(
            captured_on=date(2026, 7, 18),
            ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
        ),
        journal=journal,
        clock=ManualClock(NOW),
        downstream=_AllowPolicy(),
        audit_sink=_PolicyAudit(),
    )

    decision = await policy.evaluate(
        apply_definition,
        _call(apply_definition, _interview_batch("corrupt_receipt"), call_id="apply-corrupt-receipt"),
        _context(apply_definition),
    )

    assert decision.disposition is PolicyDisposition.DENY
    assert decision.reason_code == "interview_catalog_receipt_invalid"


@pytest.mark.asyncio
async def test_catalog_result_identity_is_checked_before_receipt_is_persisted(tmp_path: Path) -> None:
    class _WrongIdentityPlugin(_PluginBoundary):
        async def execute(self, call: ToolCall, cancellation: object) -> ToolResult:
            result = await super().execute(call, cancellation)
            return ToolResult(
                tool_call_id="another-tool-call",
                status=result.status,
                data=result.data,
                user_visible_summary=result.user_visible_summary,
                artifact_ids=result.artifact_ids,
                source_refs=result.source_refs,
                side_effects=result.side_effects,
                retryable=result.retryable,
                before_state=result.before_state,
                after_state=result.after_state,
                error=result.error,
            )

    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=InterviewSubmissionRunAuthority(
            captured_on=date(2026, 7, 18),
            ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
        ),
        journal=journal,
        clock=ManualClock(NOW),
        delegate=_WrongIdentityPlugin(),
    )
    definition = _definition("interview_catalog.search")
    call = _call(
        definition,
        {"orderedImageContentHashes": [FIRST_HASH, SECOND_HASH]},
        call_id="catalog-wrong-result-identity",
    )

    with pytest.raises(ToolDispatchError) as caught:
        await executor.execute(call, ManualCancellationToken())

    assert caught.value.code == "interview_catalog_output_invalid"
    assert (
        await journal.get(
            "ws_vault:interview-submission-authority:run_root",
            "catalog-normalized-source",
        )
        is None
    )


@pytest.mark.asyncio
async def test_exact_catalog_receipt_replay_survives_the_kernel_completion_crash_window(tmp_path: Path) -> None:
    journal = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )
    plugin = _PluginBoundary()
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=journal,
        clock=ManualClock(NOW),
        delegate=plugin,
    )
    definition = _definition("interview_catalog.search")
    call = _call(
        definition,
        {"orderedImageContentHashes": [FIRST_HASH, SECOND_HASH]},
        call_id="catalog-exact-replay",
    )

    first = await executor.execute(call, ManualCancellationToken())
    replay = await executor.execute(call, ManualCancellationToken())

    assert first.status is ToolResultStatus.SUCCEEDED
    assert replay == first
    assert [item.tool_call_id for item in plugin.calls] == [call.tool_call_id, call.tool_call_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "complete"])
async def test_catalog_receipt_rejects_wrong_journal_record_type(tmp_path: Path, operation: str) -> None:
    base = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=InterviewSubmissionRunAuthority(
            captured_on=date(2026, 7, 18),
            ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
        ),
        journal=_WrongTypeJournal(base, operation=operation, key="catalog-normalized-source"),  # type: ignore[arg-type]
        clock=ManualClock(NOW),
        delegate=_PluginBoundary(),
    )
    definition = _definition("interview_catalog.search")
    call = _call(
        definition,
        {"orderedImageContentHashes": [FIRST_HASH, SECOND_HASH]},
        call_id=f"catalog-wrong-journal-{operation}",
    )

    with pytest.raises(ToolDispatchError) as caught:
        await executor.execute(call, ManualCancellationToken())

    assert caught.value.code == "interview_submission_authority_unavailable"


@pytest.mark.asyncio
async def test_apply_claim_rejects_wrong_journal_record_type(tmp_path: Path) -> None:
    base = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )
    catalog = _call(
        _definition("interview_catalog.search"),
        {"orderedImageContentHashes": [FIRST_HASH, SECOND_HASH]},
        call_id="catalog-before-wrong-claim",
    )
    await InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=base,
        clock=ManualClock(NOW),
        delegate=_PluginBoundary(),
    ).execute(catalog, ManualCancellationToken())
    definition = _definition("vault.changes.apply")
    policy = InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=_WrongTypeJournal(base, operation="start", key="vault-apply-claim"),  # type: ignore[arg-type]
        clock=ManualClock(NOW),
        downstream=_AllowPolicy(),
        audit_sink=_PolicyAudit(),
    )

    decision = await policy.evaluate(
        definition,
        _call(definition, _interview_batch("wrong_claim_type"), call_id="apply-wrong-claim-type"),
        _context(definition),
    )

    assert decision.disposition is PolicyDisposition.DENY
    assert decision.reason_code == "interview_submission_authority_unavailable"


@pytest.mark.asyncio
async def test_apply_verification_rejects_wrong_journal_record_type(tmp_path: Path) -> None:
    base = SqliteUnitOfWorkFactory(tmp_path / "runtime.sqlite").invocation_journal
    authority = InterviewSubmissionRunAuthority(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(FIRST_HASH, SECOND_HASH),
    )
    plugin = _PluginBoundary()
    catalog = _call(
        _definition("interview_catalog.search"),
        {"orderedImageContentHashes": [FIRST_HASH, SECOND_HASH]},
        call_id="catalog-before-wrong-verify",
    )
    await InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=base,
        clock=ManualClock(NOW),
        delegate=plugin,
    ).execute(catalog, ManualCancellationToken())
    definition = _definition("vault.changes.apply")
    call = _call(definition, _interview_batch("wrong_verify_type"), call_id="apply-wrong-verify-type")
    policy = InterviewSubmissionAuthorityPolicy(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=base,
        clock=ManualClock(NOW),
        downstream=_AllowPolicy(),
        audit_sink=_PolicyAudit(),
    )
    assert (await policy.evaluate(definition, call, _context(definition))).disposition is PolicyDisposition.ALLOW
    executor = InterviewSubmissionToolExecutor(
        workspace_id="ws_vault",
        root_run_id="run_root",
        authority=authority,
        journal=_WrongTypeJournal(base, operation="get", key="vault-apply-claim"),  # type: ignore[arg-type]
        clock=ManualClock(NOW),
        delegate=plugin,
    )

    with pytest.raises(ToolDispatchError) as caught:
        await executor.execute(call, ManualCancellationToken())

    assert caught.value.code == "interview_submission_authority_unavailable"
