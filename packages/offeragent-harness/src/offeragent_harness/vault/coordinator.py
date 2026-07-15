"""Hash-bound local Vault transaction preflight and executor.

The coordinator is both a generic preflight provider and the local executor for
one definition.  The Tool Kernel selects it through definition metadata; the
Kernel itself never branches on a tool name.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Generic, Protocol, TypeVar, cast

from jsonschema import Draft202012Validator

from offeragent_harness.models import thaw_json
from offeragent_harness.ports.artifacts import (
    ArtifactMetadata,
    ArtifactState,
    ArtifactStore,
    Sensitivity,
)
from offeragent_harness.ports.cancellation import CancellationToken, OperationCancelled
from offeragent_harness.ports.storage import InvocationJournal, JournalState
from offeragent_harness.ports.system import Clock
from offeragent_harness.tools.artifacts import ArtifactByteBudget
from offeragent_harness.tools.canonical import canonical_json_sha256
from offeragent_harness.tools.definitions import ApprovalEvidence, ToolCall, ToolDefinition
from offeragent_harness.tools.preflight import PreflightConflict, PreflightEvidence
from offeragent_harness.tools.recovery_contract import invocation_journal_scope, invocation_request_fingerprint
from offeragent_harness.tools.results import (
    SideEffect,
    SideEffectKind,
    SideEffectState,
    ToolError,
    ToolResult,
    ToolResultStatus,
)
from offeragent_harness.workspace.path_policy import PathPolicyError, WorkspacePathPolicy

from .atomic_cas import (
    AtomicVaultCas,
    AtomicVaultCasConflict,
    AtomicVaultCasUncertain,
    AtomicVaultChange,
    VaultCasBarrier,
)
from .durable_manifest import (
    DurableManifestError,
    DurableManifestState,
    DurableVaultManifestStore,
    DurableVaultTransactionManifest,
    durable_manifest_id,
    manifest_identity_token,
    parse_manifest_identity,
)
from .schema import (
    ABSENT_HASH,
    INTERNAL_VAULT_TRANSACTION_SCHEMA,
    VAULT_TRANSACTION_PREFLIGHT_PROVIDER,
    vault_transaction_definition,
)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_READ_BLOCK_BYTES = 64 * 1024
T = TypeVar("T")


class VaultTransactionError(RuntimeError):
    pass


class VaultFaultInjector(Protocol):
    def before_apply(self, index: int, relative_path: str) -> None: ...

    def before_rollback(self, index: int, relative_path: str) -> None: ...

    def before_cleanup(self, index: int, relative_path: str) -> None: ...


class _NoFaults:
    def before_apply(self, index: int, relative_path: str) -> None:
        del index, relative_path

    def before_rollback(self, index: int, relative_path: str) -> None:
        del index, relative_path

    def before_cleanup(self, index: int, relative_path: str) -> None:
        del index, relative_path


@dataclass(frozen=True)
class _Snapshot:
    relative_path: str
    exists: bool
    content: bytes | None
    content_hash: str
    identity: tuple[int, int] | None


@dataclass(frozen=True)
class _PreparedTransaction:
    key: str
    token: str
    state_hash: str
    root_identity: tuple[int, int]
    parent_identities: Mapping[str, tuple[int, int] | None]
    snapshots: Mapping[str, _Snapshot]
    final_contents: Mapping[str, bytes | None]
    operations: tuple[Mapping[str, object], ...]
    diff: bytes
    changed_paths: tuple[str, ...]
    journal_scope: str
    request_hash: str


@dataclass(frozen=True)
class _AppliedChange:
    relative_path: str
    target: Path
    snapshot: _Snapshot
    final_content: bytes | None
    cas: AtomicVaultChange


@dataclass(frozen=True, slots=True)
class _OwnedIoCompletion(Generic[T]):
    """A thread completion whose value remains owned across Task cancellation."""

    value: T | None
    error: BaseException | None
    cancellation: asyncio.CancelledError | None


class _ApplyOutcomeUncertain(RuntimeError):
    def __init__(
        self,
        change: _AppliedChange,
        created_directories: tuple[Path, ...],
        cause: BaseException,
    ) -> None:
        self.change = change
        self.created_directories = created_directories
        self.cause = cause
        super().__init__(f"atomic apply outcome is unconfirmed: {type(cause).__name__}: {cause}")


class _DurableApplyOutcomeUncertain(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VaultTransactionRecoveryReport:
    committed: int
    rolled_back: int
    manual_review_paths: tuple[str, ...]


def content_hash(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


class VaultTransactionCoordinator:
    provider_id = VAULT_TRANSACTION_PREFLIGHT_PROVIDER

    def __init__(
        self,
        *,
        workspace_id: str,
        vault_root: Path,
        artifacts: ArtifactStore,
        artifact_budget: ArtifactByteBudget,
        clock: Clock,
        max_file_bytes: int = 10 * 1024 * 1024,
        max_batch_bytes: int = 20 * 1024 * 1024,
        allowed_suffixes: frozenset[str] = frozenset({".md", ".txt"}),
        faults: VaultFaultInjector | None = None,
        cas_barrier: VaultCasBarrier | None = None,
        manifest_directory: Path | None = None,
        manifest_state_root: Path | None = None,
        journal: InvocationJournal | None = None,
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id cannot be empty")
        if max_file_bytes < 1 or max_batch_bytes < max_file_bytes:
            raise ValueError("invalid Vault transaction byte limits")
        if (manifest_directory is None) != (journal is None):
            raise ValueError("durable manifest storage and Invocation Journal must be configured together")
        if manifest_directory is None and manifest_state_root is not None:
            raise ValueError("manifest_state_root requires durable manifest storage")
        self._workspace_id = workspace_id
        self._paths = WorkspacePathPolicy(vault_root)
        self._root = self._paths.root().path
        self._root_identity = self._current_root_identity()
        self._artifacts = artifacts
        self._artifact_budget = artifact_budget
        self._clock = clock
        self._max_file_bytes = max_file_bytes
        self._max_batch_bytes = max_batch_bytes
        self._allowed_suffixes = frozenset(item.casefold() for item in allowed_suffixes)
        self._faults = faults or _NoFaults()
        self._cas = AtomicVaultCas(
            root=self._root,
            max_file_bytes=max_file_bytes,
            barrier=cas_barrier,
        )
        self._cas_barrier = cas_barrier
        self._journal = journal
        self._manifests = (
            None
            if manifest_directory is None
            else DurableVaultManifestStore(
                manifest_directory,
                vault_root=self._root,
                workspace_id=workspace_id,
                trusted_state_root=manifest_state_root,
            )
        )
        self._plans: dict[str, _PreparedTransaction] = {}
        self._plan_lock = asyncio.Lock()
        self._validator = Draft202012Validator(INTERNAL_VAULT_TRANSACTION_SCHEMA)

    async def prepare(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        cancellation: CancellationToken,
    ) -> PreflightEvidence:
        self._validate_definition_and_call(definition, call)
        cancellation.checkpoint()
        plan = await _run_io(
            self._build_plan,
            call,
            invocation_journal_scope(call, definition),
            invocation_request_fingerprint(call),
        )
        cancellation.checkpoint()
        if self._manifests is not None:
            self._require_public_durable_plan(plan)
            try:
                await _run_io(self._manifests.assert_paths_available, plan.changed_paths)
            except DurableManifestError as error:
                raise PreflightConflict(str(error), details={"reason": "vault_path_recovery_blocked"}) from error
        diff_artifact = await self._store_artifact(
            call,
            plan.diff,
            mime_type="text/x-diff",
            purpose="vault-transaction-diff",
            sensitivity=Sensitivity.WORKSPACE,
        )
        async with self._plan_lock:
            self._plans[plan.key] = plan
        return PreflightEvidence(
            provider_id=self.provider_id,
            state_hash=plan.state_hash,
            artifact_ids=(diff_artifact.artifact_id,),
            lock_keys=plan.changed_paths,
            token=plan.token,
            facts={
                "paths": list(plan.changed_paths),
                "operationCount": len(plan.operations),
                "diffSha256": diff_artifact.sha256,
                "beforeHashes": {
                    path: plan.snapshots[path].content_hash for path in sorted(plan.changed_paths, key=str.casefold)
                },
                "afterHashes": {
                    path: (
                        ABSENT_HASH
                        if plan.final_contents[path] is None
                        else content_hash(plan.final_contents[path] or b"")
                    )
                    for path in sorted(plan.changed_paths, key=str.casefold)
                },
            },
        )

    async def revalidate(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        evidence: PreflightEvidence,
        cancellation: CancellationToken,
    ) -> None:
        self._validate_definition_and_call(definition, call)
        plan = await self._get_plan(call, evidence)
        cancellation.checkpoint()
        observed = await _run_io(self._observe_plan_state, plan)
        cancellation.checkpoint()
        observed_hash = canonical_json_sha256(observed)
        if observed_hash != plan.state_hash:
            raise PreflightConflict(
                "Vault state changed after approval preflight",
                details={"expected": plan.state_hash, "observed": observed_hash},
            )

    async def complete(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        evidence: PreflightEvidence,
        result: ToolResult,
    ) -> None:
        self._validate_definition_and_call(definition, call)
        key = self._plan_key(call)
        async with self._plan_lock:
            current = self._plans.get(key)
        try:
            if current is not None and current.token == evidence.token:
                await self._retire_completed_manifest(current, result)
        finally:
            async with self._plan_lock:
                current = self._plans.get(key)
                if current is not None and current.token == evidence.token:
                    self._plans.pop(key, None)

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        return await self._execute(call, cancellation)

    async def _execute(
        self,
        call: ToolCall,
        cancellation: CancellationToken,
    ) -> ToolResult:
        key = self._plan_key(call)
        async with self._plan_lock:
            plan = self._plans.get(key)
        if plan is None:
            return self._failure(call, "preflight_plan_missing", "Vault transaction has no approved preflight plan")
        applied: list[_AppliedChange] = []
        created_directories: set[Path] = set()
        created_directory_identities: dict[Path, tuple[int, int]] = {}
        manifest: DurableVaultTransactionManifest | None = None
        durable_commit_recorded = False
        try:
            cancellation.checkpoint()
            if self._manifests is not None:
                manifest, manifest_cancellation = await self._begin_durable_manifest(plan, call)
                self._signal("manifest_prepared", manifest.path)
                if manifest_cancellation is not None:
                    # PREPARED is not a commit point.  Hand ownership of the
                    # durable record to this frame before honoring cancellation
                    # so the normal rollback path can retire it immediately.
                    raise manifest_cancellation
            cancellation.checkpoint()
            for index, relative_path in enumerate(plan.changed_paths):
                cancellation.checkpoint()
                self._faults.before_apply(index, relative_path)
                cancellation.checkpoint()
                snapshot = plan.snapshots[relative_path]
                final_content = plan.final_contents[relative_path]
                completion = await _run_io_owned(
                    self._apply_change,
                    plan,
                    relative_path,
                    snapshot,
                    final_content,
                    created_directory_identities,
                    manifest,
                )
                if completion.value is not None:
                    # The worker thread can publish the file just before its
                    # awaiting Task is cancelled.  Transfer the returned CAS
                    # lease before re-raising cancellation; otherwise its
                    # Windows handles and rollback authority would be lost.
                    change, directories = completion.value
                    applied.append(change)
                    created_directories.update(directories)
                    created_directory_identities.update(directories)
                if completion.error is not None:
                    if completion.cancellation is not None and not isinstance(
                        completion.error,
                        (_ApplyOutcomeUncertain, _DurableApplyOutcomeUncertain),
                    ):
                        raise completion.cancellation from completion.error
                    raise completion.error
                if completion.cancellation is not None:
                    raise completion.cancellation
                if completion.value is None:
                    raise RuntimeError("Vault apply thread returned no owned change")
                cancellation.checkpoint()
            for change in applied:
                cancellation.checkpoint()
                await _run_io(change.cas.verify_committed)
                cancellation.checkpoint()
            if manifest is not None:
                await self._transition_durable_manifest(
                    manifest.manifest_id,
                    DurableManifestState.COMMITTED,
                )
                durable_commit_recorded = True
                self._signal("manifest_committed", manifest.path)
        except _ApplyOutcomeUncertain as error:
            created_directories.update(error.created_directories)
            rollback_errors = await self._rollback(applied, created_directories)
            rollback_errors.append(f"{error.change.relative_path}: {error}")
            return await self._uncertain_result(
                call,
                plan,
                (*applied, error.change),
                rollback_errors,
                error.cause,
            )
        except _DurableApplyOutcomeUncertain as error:
            return await self._uncertain_result(call, plan, applied, (str(error),), error)
        except BaseException as error:
            if durable_commit_recorded:
                cleanup_errors = self._cleanup_applied(applied, created_directories)
                if cleanup_errors:
                    return await self._committed_cleanup_partial(call, plan, cleanup_errors)
                return self._succeeded(call, plan)
            rollback_errors = await self._rollback(applied, created_directories)
            if rollback_errors:
                return await self._uncertain_result(call, plan, applied, rollback_errors, error)
            if manifest is not None:
                try:
                    if self._manifests is None:
                        raise DurableManifestError("durable Vault transaction storage is unavailable")
                    current_manifest = await _run_io(self._manifests.get, manifest.manifest_id)
                    if current_manifest is None or not await _run_io(
                        self._manifest_terminal_state_is_clean,
                        current_manifest,
                        DurableManifestState.ROLLED_BACK,
                    ):
                        return await self._uncertain_result(
                            call,
                            plan,
                            applied,
                            ("durable rollback filesystem state is not clean",),
                            error,
                        )
                    await self._transition_durable_manifest(
                        manifest.manifest_id,
                        DurableManifestState.ROLLED_BACK,
                    )
                    self._signal("manifest_rolled_back", manifest.path)
                except BaseException as manifest_error:
                    return await self._uncertain_result(
                        call,
                        plan,
                        applied,
                        (f"manifest rollback state: {type(manifest_error).__name__}: {manifest_error}",),
                        error,
                    )
            if isinstance(error, OperationCancelled | asyncio.CancelledError):
                return self._cancelled(call, plan, applied)
            if isinstance(error, PreflightConflict):
                return self._rolled_back_conflict(call, plan, applied, error)
            if isinstance(error, AtomicVaultCasConflict):
                return self._rolled_back_conflict(call, plan, applied, self._cas_conflict(error))
            return self._rolled_back_failure(call, plan, applied, error)

        cleanup_errors = self._cleanup_applied(applied, created_directories)
        if cleanup_errors:
            return await self._committed_cleanup_partial(call, plan, cleanup_errors)
        return self._succeeded(call, plan)

    def _succeeded(self, call: ToolCall, plan: _PreparedTransaction) -> ToolResult:
        return ToolResult(
            tool_call_id=call.tool_call_id,
            status=ToolResultStatus.SUCCEEDED,
            data={
                "paths": list(plan.changed_paths),
                "stateHash": plan.state_hash,
                "recoveryArtifactIds": [],
            },
            user_visible_summary=f"Vault transaction committed {len(plan.changed_paths)} path(s)",
            artifact_ids=(),
            source_refs=(),
            side_effects=self._effects(plan, SideEffectState.COMMITTED),
            retryable=False,
            before_state={"stateHash": plan.state_hash},
            after_state={
                path: ABSENT_HASH if content is None else content_hash(content)
                for path, content in plan.final_contents.items()
            },
            error=None,
        )

    async def recover_after_restart(self) -> VaultTransactionRecoveryReport:
        """Reconcile every durable Vault manifest before transport.

        Filesystem recovery is completed first and the Invocation Journal is
        finalized second.  The manifest is deleted only after both facts are
        durable, so termination at any recovery barrier is safely rerunnable.
        """

        if self._manifests is None:
            return VaultTransactionRecoveryReport(0, 0, ())
        manifests = await _run_io(self._manifests.list)
        committed = 0
        rolled_back = 0
        manual: list[str] = []
        for manifest in manifests:
            if manifest.state is DurableManifestState.MANUAL_REVIEW:
                manual.append(manifest.path)
                continue
            try:
                outcome = await _run_io(self._recover_manifest_filesystem, manifest)
            except Exception as error:
                reason = f"{type(error).__name__}: {error}"[:1024]
                try:
                    await self._mark_manifest_manual(manifest.manifest_id, reason)
                except Exception:
                    pass
                manual.append(manifest.path)
                continue
            try:
                await self._complete_recovery_journal(manifest, committed=outcome == "committed")
            except DurableManifestError as error:
                reason = f"{type(error).__name__}: {error}"[:1024]
                try:
                    await self._mark_manifest_manual(manifest.manifest_id, reason)
                except Exception:
                    pass
                manual.append(manifest.path)
                continue
            # Storage failures are not reclassified as ambiguous filesystem
            # outcomes.  Propagate them so startup retries the still-durable,
            # terminal manifest on the next launch.
            await _run_io(self._manifests.delete, manifest.manifest_id)
            self._signal("recovery_completed", manifest.path)
            if outcome == "committed":
                committed += 1
            else:
                rolled_back += 1
        return VaultTransactionRecoveryReport(committed, rolled_back, tuple(sorted(set(manual), key=str.casefold)))

    @staticmethod
    def _require_public_durable_plan(plan: _PreparedTransaction) -> None:
        if len(plan.operations) != 1 or len(plan.changed_paths) != 1:
            raise VaultTransactionError("production vault.transaction requires exactly one physical path")
        operation = plan.operations[0].get("op")
        if operation not in {"create", "append", "replace", "patch"}:
            raise VaultTransactionError("production vault.transaction operation is not model-facing")
        if plan.final_contents[plan.changed_paths[0]] is None:
            raise VaultTransactionError("production vault.transaction cannot delete a path")

    async def _begin_durable_manifest(
        self,
        plan: _PreparedTransaction,
        call: ToolCall,
    ) -> tuple[DurableVaultTransactionManifest, asyncio.CancelledError | None]:
        if self._manifests is None or self._journal is None:
            raise DurableManifestError("durable Vault transaction storage is unavailable")
        record = await self._journal.get(plan.journal_scope, call.idempotency_key)
        if record is None or record.request_hash != plan.request_hash or record.state is not JournalState.STARTED:
            raise DurableManifestError("Invocation Journal does not contain the exact started Vault transaction")
        manifest = self._new_durable_manifest(plan, call)
        created = await _run_io_owned(self._manifests.create, manifest)
        cancellation = created.cancellation
        if created.error is not None:
            # A storage error can be a lost acknowledgement.  Reconcile it,
            # but retain any Task cancellation instead of silently converting
            # that cancellation into permission to continue the transaction.
            observed = await _run_io_owned(self._manifests.get, manifest.manifest_id)
            cancellation = cancellation or observed.cancellation
            if observed.error is not None:
                if cancellation is not None:
                    raise cancellation from observed.error
                raise observed.error
            if observed.value != manifest:
                if cancellation is not None:
                    raise cancellation from created.error
                raise created.error
        return manifest, cancellation

    def _new_durable_manifest(
        self,
        plan: _PreparedTransaction,
        call: ToolCall,
    ) -> DurableVaultTransactionManifest:
        self._require_public_durable_plan(plan)
        path = plan.changed_paths[0]
        snapshot = plan.snapshots[path]
        final_content = plan.final_contents[path]
        assert final_content is not None
        operation = plan.operations[0]["op"]
        assert isinstance(operation, str)
        target = PurePosixPath(path)
        suffix = plan.token.removeprefix("sha256:")[:20]
        parent = target.parent
        approved_parents = {
            item: None if identity is None else manifest_identity_token(identity)
            for item, identity in plan.parent_identities.items()
        }
        return DurableVaultTransactionManifest(
            manifest_id=durable_manifest_id(self._workspace_id, plan.token),
            workspace_id=self._workspace_id,
            root_identity=manifest_identity_token(plan.root_identity),
            journal_scope=plan.journal_scope,
            idempotency_key=call.idempotency_key,
            request_hash=plan.request_hash,
            tool_call_id=call.tool_call_id,
            run_id=call.run_id,
            root_run_id=call.lineage.root_run_id,
            definition_fingerprint=call.definition_fingerprint,
            plan_token=plan.token,
            state_hash=plan.state_hash,
            operation=operation,
            path=path,
            before_hash=snapshot.content_hash,
            after_hash=content_hash(final_content),
            original_identity=(None if snapshot.identity is None else manifest_identity_token(snapshot.identity)),
            approved_parents=approved_parents,
            active_parents=approved_parents,
            backup_path=(
                None if not snapshot.exists else (parent / f".offeragent-tx-{suffix}-{target.name}.bak").as_posix()
            ),
            temporary_path=(parent / f".offeragent-tx-{suffix}-{target.name}.tmp").as_posix(),
            rollback_path=(parent / f".offeragent-rollback-{suffix}-{target.name}").as_posix(),
            temporary_identity=None,
            state=DurableManifestState.PREPARED,
        )

    async def _transition_durable_manifest(
        self,
        manifest_id: str,
        state: DurableManifestState,
    ) -> DurableVaultTransactionManifest:
        if self._manifests is None:
            raise DurableManifestError("durable Vault transaction storage is unavailable")
        store = self._manifests

        def transition() -> DurableVaultTransactionManifest:
            current = store.get(manifest_id)
            if current is None:
                raise DurableManifestError("durable Vault transaction manifest disappeared")
            candidate = current.transition(state)
            try:
                store.save(candidate)
            except BaseException:
                observed = store.get(manifest_id)
                if observed != candidate:
                    raise
            return candidate

        completion = await _run_io_owned(transition)
        if completion.error is None:
            if completion.value is None:
                raise DurableManifestError("durable manifest transition returned no state")
            # COMMITTED is the irreversible decision point; ROLLED_BACK is the
            # corresponding completed cleanup point.  Once either exact state
            # is durable, a concurrent Task cancellation cannot reverse it.
            return completion.value

        # A storage exception can be a lost acknowledgement.  Re-read the
        # canonical record synchronously after the shielded thread has exited.
        # If the requested boundary is present, that durable fact wins over
        # both the storage error and a concurrent Task cancellation.
        observed = store.get(manifest_id)
        if observed is not None and observed.state is state:
            return observed
        if completion.cancellation is not None:
            raise completion.cancellation from completion.error
        raise completion.error

    async def _mark_manifest_manual(self, manifest_id: str, reason: str) -> None:
        if self._manifests is None:
            return
        store = self._manifests

        def mark() -> None:
            current = store.get(manifest_id)
            if current is None or current.state is DurableManifestState.MANUAL_REVIEW:
                return
            store.save(
                current.transition(
                    DurableManifestState.MANUAL_REVIEW,
                    manual_reason=reason or "manual recovery review required",
                )
            )

        await _run_io(mark)

    async def _retire_completed_manifest(self, plan: _PreparedTransaction, result: ToolResult) -> None:
        if self._manifests is None or self._journal is None:
            return
        manifest_id = durable_manifest_id(self._workspace_id, plan.token)
        manifest = await _run_io(self._manifests.get, manifest_id)
        if manifest is None:
            return
        record = await self._journal.get(plan.journal_scope, manifest.idempotency_key)
        if record is None or record.request_hash != plan.request_hash:
            return
        if record.state is JournalState.STARTED and manifest.state in {
            DurableManifestState.COMMITTED,
            DurableManifestState.ROLLED_BACK,
        }:
            clean = await _run_io(self._manifest_terminal_state_is_clean, manifest, manifest.state)
            if not clean:
                return
            recovered = self._recovery_result(
                manifest,
                committed=manifest.state is DurableManifestState.COMMITTED,
            )
            await self._journal.complete(
                manifest.journal_scope,
                manifest.idempotency_key,
                manifest.request_hash,
                recovered,
                self._clock.utcnow(),
            )
            await _run_io(self._manifests.delete, manifest_id)
            return
        if record.state is not JournalState.COMPLETED or record.result != result:
            return
        expected_state = (
            DurableManifestState.COMMITTED
            if result.status in {ToolResultStatus.SUCCEEDED, ToolResultStatus.PARTIAL}
            else DurableManifestState.ROLLED_BACK
        )
        if manifest.state is not expected_state:
            return
        clean = await _run_io(self._manifest_terminal_state_is_clean, manifest, expected_state)
        if clean:
            await _run_io(self._manifests.delete, manifest_id)

    def _recover_manifest_filesystem(self, manifest: DurableVaultTransactionManifest) -> str:
        if self._manifests is None:
            raise DurableManifestError("durable Vault transaction storage is unavailable")
        if manifest.root_identity != manifest_identity_token(self._current_root_identity()):
            raise DurableManifestError("Vault root identity differs from durable transaction manifest")
        if manifest.definition_fingerprint != vault_transaction_definition().fingerprint:
            raise DurableManifestError("durable transaction definition fingerprint is not production-current")
        if self._normalize_path(manifest.path, internal=False) != manifest.path:
            raise DurableManifestError("durable transaction target path is no longer canonical")
        if manifest.state is DurableManifestState.COMMITTED:
            parent_paths = self._recovery_parent_paths(manifest, active=True)
            self._recover_committed_files(manifest, parent_paths)
            return "committed"
        if manifest.state is DurableManifestState.ROLLED_BACK:
            self._recovery_parent_paths(manifest, active=False)
            if not self._manifest_terminal_state_is_clean(manifest, DurableManifestState.ROLLED_BACK):
                raise DurableManifestError("rolled-back durable transaction is not clean")
            return "rolled_back"
        if manifest.state is DurableManifestState.PREPARED:
            self._recovery_parent_paths(manifest, active=False)
            if not self._manifest_terminal_state_is_clean(manifest, DurableManifestState.ROLLED_BACK):
                raise DurableManifestError("prepared transaction contains unbound filesystem mutations")
            self._manifests.save(manifest.transition(DurableManifestState.ROLLED_BACK))
            return "rolled_back"
        if manifest.state is not DurableManifestState.STAGED:
            raise DurableManifestError("durable transaction requires manual review")
        parent_paths = self._recovery_parent_paths(manifest, active=True)
        self._recover_staged_files(manifest, parent_paths)
        current = self._manifests.get(manifest.manifest_id)
        if current is None:
            raise DurableManifestError("durable transaction manifest disappeared during recovery")
        self._manifests.save(current.transition(DurableManifestState.ROLLED_BACK))
        self._signal("recovery_rolled_back", manifest.path)
        return "rolled_back"

    def _recover_committed_files(
        self,
        manifest: DurableVaultTransactionManifest,
        parent_paths: tuple[tuple[Path, tuple[int, int]], ...],
    ) -> None:
        target, backup, temporary, rollback = self._manifest_file_states(manifest)
        if not self._matches_manifest_file(target, manifest.after_hash, manifest.temporary_identity):
            raise DurableManifestError("committed Vault target no longer matches its durable identity/hash")
        if temporary.exists or rollback.exists:
            raise DurableManifestError("committed transaction has an unexpected temporary or rollback file")
        if backup.exists:
            if not self._matches_manifest_file(backup, manifest.before_hash, manifest.original_identity):
                raise DurableManifestError("committed transaction backup identity/hash drifted")
            assert backup.identity is not None
            self._cas.recover_delete(
                path=self._internal_manifest_path(manifest.backup_path or ""),
                expected_root_identity=parse_manifest_identity(manifest.root_identity),
                parent_paths=parent_paths,
                expected_hash=manifest.before_hash,
                expected_identity=backup.identity,
            )

    def _recover_staged_files(
        self,
        manifest: DurableVaultTransactionManifest,
        parent_paths: tuple[tuple[Path, tuple[int, int]], ...],
    ) -> None:
        root_identity = parse_manifest_identity(manifest.root_identity)
        target_path = self._internal_manifest_path(manifest.path)
        backup_path = None if manifest.backup_path is None else self._internal_manifest_path(manifest.backup_path)
        temporary_path = self._internal_manifest_path(manifest.temporary_path)
        rollback_path = self._internal_manifest_path(manifest.rollback_path)
        target, backup, temporary, rollback = self._manifest_file_states(manifest)
        before_matches = self._matches_manifest_file(target, manifest.before_hash, manifest.original_identity)
        after_matches = self._matches_manifest_file(target, manifest.after_hash, manifest.temporary_identity)
        backup_matches = self._matches_manifest_file(backup, manifest.before_hash, manifest.original_identity)
        temporary_matches = self._matches_manifest_file(temporary, manifest.after_hash, manifest.temporary_identity)
        rollback_matches = self._matches_manifest_file(rollback, manifest.after_hash, manifest.temporary_identity)
        if target.exists and not (before_matches or after_matches):
            raise DurableManifestError("Vault target identity/hash drifted during transaction recovery")
        if backup.exists and not backup_matches:
            raise DurableManifestError("Vault backup identity/hash drifted during transaction recovery")
        if temporary.exists and not temporary_matches:
            raise DurableManifestError("Vault temporary identity/hash drifted during transaction recovery")
        if rollback.exists and not rollback_matches:
            raise DurableManifestError("Vault rollback identity/hash drifted during transaction recovery")
        if temporary.exists and rollback.exists:
            raise DurableManifestError("transaction has two final-content identities during recovery")

        if after_matches:
            if backup_path is not None and not backup_matches:
                raise DurableManifestError("published transaction lost its verified original backup")
            assert target.identity is not None
            self._signal("recovery_before_rollback_claim", manifest.path)
            self._cas.recover_rename_noreplace(
                source=target_path,
                destination=rollback_path,
                expected_root_identity=root_identity,
                parent_paths=parent_paths,
                expected_hash=manifest.after_hash,
                expected_identity=target.identity,
            )
            self._signal("recovery_final_claimed", manifest.path)
            target = self._snapshot(manifest.path)
            rollback = self._snapshot(manifest.rollback_path)
            before_matches = False
            rollback_matches = self._matches_manifest_file(rollback, manifest.after_hash, manifest.temporary_identity)

        if not target.exists and backup_path is not None:
            backup = self._snapshot(manifest.backup_path or "")
            if not self._matches_manifest_file(backup, manifest.before_hash, manifest.original_identity):
                raise DurableManifestError("missing Vault target has no verified original backup")
            assert backup.identity is not None
            self._cas.recover_rename_noreplace(
                source=backup_path,
                destination=target_path,
                expected_root_identity=root_identity,
                parent_paths=parent_paths,
                expected_hash=manifest.before_hash,
                expected_identity=backup.identity,
            )
            self._signal("recovery_original_restored", manifest.path)
            target = self._snapshot(manifest.path)
            before_matches = self._matches_manifest_file(target, manifest.before_hash, manifest.original_identity)
        if manifest.before_hash == ABSENT_HASH and target.exists:
            raise DurableManifestError("create rollback refused to preserve an unexpected Vault target")
        if manifest.before_hash != ABSENT_HASH and not before_matches:
            raise DurableManifestError("transaction recovery did not restore the original Vault target")

        for path in (temporary_path, rollback_path):
            current = self._snapshot(path.relative_to(self._root).as_posix())
            if not current.exists:
                continue
            if not self._matches_manifest_file(current, manifest.after_hash, manifest.temporary_identity):
                raise DurableManifestError("recovery cleanup file identity/hash drifted")
            assert current.identity is not None
            self._cas.recover_delete(
                path=path,
                expected_root_identity=root_identity,
                parent_paths=parent_paths,
                expected_hash=manifest.after_hash,
                expected_identity=current.identity,
            )
        if backup_path is not None and self._snapshot(manifest.backup_path or "").exists:
            raise DurableManifestError("recovery left an original backup after restore")
        if not self._manifest_terminal_state_is_clean(manifest, DurableManifestState.ROLLED_BACK):
            raise DurableManifestError("durable transaction rollback did not reach a clean state")

    def _manifest_file_states(
        self,
        manifest: DurableVaultTransactionManifest,
    ) -> tuple[_Snapshot, _Snapshot, _Snapshot, _Snapshot]:
        target = self._snapshot(manifest.path)
        backup = (
            _Snapshot("", False, None, ABSENT_HASH, None)
            if manifest.backup_path is None
            else self._snapshot(manifest.backup_path)
        )
        return target, backup, self._snapshot(manifest.temporary_path), self._snapshot(manifest.rollback_path)

    @staticmethod
    def _matches_manifest_file(snapshot: _Snapshot, expected_hash: str, expected_identity: str | None) -> bool:
        if expected_hash == ABSENT_HASH:
            return not snapshot.exists and expected_identity is None
        return (
            snapshot.exists
            and snapshot.content_hash == expected_hash
            and snapshot.identity is not None
            and expected_identity is not None
            and manifest_identity_token(snapshot.identity) == expected_identity
        )

    def _manifest_terminal_state_is_clean(
        self,
        manifest: DurableVaultTransactionManifest,
        state: DurableManifestState,
    ) -> bool:
        try:
            target, backup, temporary, rollback = self._manifest_file_states(manifest)
        except Exception:
            return False
        if backup.exists or temporary.exists or rollback.exists:
            return False
        if state is DurableManifestState.COMMITTED:
            return self._matches_manifest_file(target, manifest.after_hash, manifest.temporary_identity)
        return self._matches_manifest_file(target, manifest.before_hash, manifest.original_identity)

    def _recovery_parent_paths(
        self,
        manifest: DurableVaultTransactionManifest,
        *,
        active: bool,
    ) -> tuple[tuple[Path, tuple[int, int]], ...]:
        identities = manifest.active_parents if active else manifest.approved_parents
        result: list[tuple[Path, tuple[int, int]]] = []
        current = self._root
        relative_parts: list[str] = []
        for part in PurePosixPath(manifest.path).parts[:-1]:
            current /= part
            relative_parts.append(part)
            parent = PurePosixPath(*relative_parts).as_posix()
            expected_token = identities.get(parent)
            try:
                info = os.stat(current, follow_symlinks=False)
            except FileNotFoundError:
                if expected_token is None:
                    continue
                raise DurableManifestError(f"manifest-bound Vault parent disappeared: {parent}") from None
            self._validate_directory_stat(info, parent)
            if expected_token is None:
                raise DurableManifestError(f"unbound Vault parent appeared during recovery: {parent}")
            expected = parse_manifest_identity(expected_token)
            if self._identity(info) != expected:
                raise DurableManifestError(f"manifest-bound Vault parent identity changed: {parent}")
            result.append((current, expected))
        return tuple(result)

    async def _complete_recovery_journal(
        self,
        manifest: DurableVaultTransactionManifest,
        *,
        committed: bool,
    ) -> None:
        if self._journal is None:
            raise DurableManifestError("Invocation Journal is unavailable during Vault recovery")
        record = await self._journal.get(manifest.journal_scope, manifest.idempotency_key)
        if record is None or record.request_hash != manifest.request_hash:
            raise DurableManifestError("durable transaction journal binding is missing or changed")
        if record.state is JournalState.UNKNOWN:
            raise DurableManifestError("durable transaction journal is already marked unknown")
        if record.state is JournalState.COMPLETED:
            result = record.result
            committed_status = result is not None and result.status in {
                ToolResultStatus.SUCCEEDED,
                ToolResultStatus.PARTIAL,
            }
            if committed_status is not committed:
                raise DurableManifestError("completed Invocation Journal contradicts durable Vault state")
            return
        result = self._recovery_result(manifest, committed=committed)
        await self._journal.complete(
            manifest.journal_scope,
            manifest.idempotency_key,
            manifest.request_hash,
            result,
            self._clock.utcnow(),
        )

    def _recovery_result(
        self,
        manifest: DurableVaultTransactionManifest,
        *,
        committed: bool,
    ) -> ToolResult:
        state = SideEffectState.COMMITTED if committed else SideEffectState.ROLLED_BACK
        after_hash = manifest.after_hash if committed else manifest.before_hash
        error = None
        status = ToolResultStatus.SUCCEEDED
        summary = "Vault transaction commit recovered after Worker restart"
        if not committed:
            status = ToolResultStatus.FAILED
            summary = "Interrupted Vault transaction was rolled back during Worker startup"
            error = ToolError("vault_transaction_interrupted_rolled_back", summary, False, False)
        return ToolResult(
            manifest.tool_call_id,
            status,
            {"paths": [manifest.path], "stateHash": manifest.state_hash, "recoveryArtifactIds": []},
            summary,
            (),
            (),
            (
                SideEffect(
                    SideEffectKind.FILE_WRITE,
                    state,
                    f"vault:{self._workspace_id}:{manifest.path}",
                    {"hash": manifest.before_hash},
                    {"hash": after_hash},
                    {"op": manifest.operation, "recoveredAfterRestart": True},
                ),
            ),
            False,
            {"stateHash": manifest.state_hash},
            ({manifest.path: manifest.after_hash} if committed else {"stateHash": manifest.state_hash}),
            error,
        )

    def _validate_definition_and_call(self, definition: ToolDefinition, call: ToolCall) -> None:
        if definition.preflight_provider != self.provider_id:
            raise VaultTransactionError("ToolDefinition is not bound to this preflight provider")
        if definition.approval_evidence is not ApprovalEvidence.DIFF:
            raise VaultTransactionError("Vault transactions require diff approval evidence")
        if call.workspace_id != self._workspace_id:
            raise VaultTransactionError("ToolCall belongs to a different workspace")

    async def _get_plan(self, call: ToolCall, evidence: PreflightEvidence) -> _PreparedTransaction:
        if evidence.provider_id != self.provider_id:
            raise PreflightConflict("preflight evidence provider changed")
        async with self._plan_lock:
            plan = self._plans.get(self._plan_key(call))
        if plan is None or plan.token != evidence.token or plan.state_hash != evidence.state_hash:
            raise PreflightConflict("prepared Vault transaction is missing or changed")
        return plan

    def _build_plan(
        self,
        call: ToolCall,
        journal_scope: str,
        request_hash: str,
    ) -> _PreparedTransaction:
        arguments = thaw_json(call.arguments)
        issues = sorted(
            self._validator.iter_errors(arguments), key=lambda error: tuple(str(item) for item in error.path)
        )
        if issues:
            raise VaultTransactionError(f"invalid vault.transaction arguments: {issues[0].message}")
        raw_operations = arguments["operations"]
        assert isinstance(raw_operations, list)
        operations = tuple(dict(operation) for operation in raw_operations)
        snapshots: dict[str, _Snapshot] = {}
        states: dict[str, bytes | None] = {}
        canonical_paths: dict[str, str] = {}

        def load(raw_path: object, *, internal: bool = False) -> tuple[str, bytes | None]:
            if not isinstance(raw_path, str):
                raise VaultTransactionError("Vault path must be a string")
            path = self._normalize_path(raw_path, internal=internal)
            folded = path.casefold()
            existing_case = canonical_paths.get(folded)
            if existing_case is not None and existing_case != path:
                raise VaultTransactionError(f"case-insensitive Vault path collision: {existing_case!r} / {path!r}")
            canonical_paths[folded] = path
            if path not in states:
                snapshot = self._snapshot(path)
                snapshots[path] = snapshot
                states[path] = snapshot.content
            return path, states[path]

        for index, operation in enumerate(operations):
            op = operation["op"]
            path, current = load(operation["path"])
            self._require_expected(path, current, operation["expectedHash"])
            if op == "create":
                assert isinstance(operation["content"], str)
                states[path] = operation["content"].encode("utf-8")
            elif op == "append":
                assert current is not None and isinstance(operation["content"], str)
                states[path] = current + operation["content"].encode("utf-8")
            elif op == "replace":
                assert current is not None
                text = self._decode(current, path)
                find = operation["find"]
                replacement = operation["replace"]
                assert isinstance(find, str) and isinstance(replacement, str)
                matches = text.count(find)
                if matches != 1:
                    raise VaultTransactionError(f"replace requires exactly one match in {path}; found {matches}")
                states[path] = text.replace(find, replacement, 1).encode("utf-8")
            elif op == "patch":
                assert current is not None
                edits = operation["edits"]
                assert isinstance(edits, list)
                states[path] = self._apply_line_edits(path, current, edits)
            elif op == "rename":
                assert current is not None
                destination, destination_content = load(operation["destination"])
                if destination == path:
                    raise VaultTransactionError("rename source and destination must differ")
                self._require_expected(destination, destination_content, operation["expectedDestinationHash"])
                states[destination] = current
                states[path] = None
            elif op == "trash":
                assert current is not None
                trash_path = self._trash_path(call, index, path)
                trash_path, trash_content = load(trash_path, internal=True)
                self._require_expected(trash_path, trash_content, ABSENT_HASH)
                states[trash_path] = current
                states[path] = None
            else:
                raise VaultTransactionError(f"unsupported Vault operation {op!r}")
            current_value = states[path]
            if current_value is not None and len(current_value) > self._max_file_bytes:
                raise VaultTransactionError(f"Vault target exceeds {self._max_file_bytes} bytes: {path}")

        changed_paths = tuple(
            sorted(
                (path for path, final in states.items() if final != snapshots[path].content),
                key=str.casefold,
            )
        )
        if not changed_paths:
            raise VaultTransactionError("Vault transaction has no effective changes")
        total_bytes = sum(len(content) for content in states.values() if content is not None)
        if total_bytes > self._max_batch_bytes:
            raise VaultTransactionError(f"Vault transaction exceeds {self._max_batch_bytes} bytes")
        final_contents = {path: states[path] for path in changed_paths}
        selected_snapshots = {path: snapshots[path] for path in changed_paths}
        root_identity = self._current_root_identity()
        parent_identities = self._capture_parent_identities(changed_paths)
        state_hash = canonical_json_sha256(self._state_facts(root_identity, parent_identities, selected_snapshots))
        diff = self._build_diff(selected_snapshots, final_contents)
        token = canonical_json_sha256(
            {
                "workspaceId": call.workspace_id,
                "runId": call.run_id,
                "toolCallId": call.tool_call_id,
                "argsHash": call.args_hash,
                "stateHash": state_hash,
                "diffHash": content_hash(diff),
            }
        )
        return _PreparedTransaction(
            key=self._plan_key(call),
            token=token,
            state_hash=state_hash,
            root_identity=root_identity,
            parent_identities=parent_identities,
            snapshots=selected_snapshots,
            final_contents=final_contents,
            operations=operations,
            diff=diff,
            changed_paths=changed_paths,
            journal_scope=journal_scope,
            request_hash=request_hash,
        )

    def _normalize_path(self, raw_path: str, *, internal: bool) -> str:
        try:
            resolved = self._paths.resolve(raw_path, for_write=True)
        except PathPolicyError as error:
            raise VaultTransactionError(str(error)) from error
        path = resolved.relative_path
        parts = PurePosixPath(path).parts
        if not internal and any(part.startswith(".") for part in parts):
            raise VaultTransactionError(f"hidden Vault paths are not writable: {path}")
        if not internal and PurePosixPath(path).suffix.casefold() not in self._allowed_suffixes:
            raise VaultTransactionError(f"unsupported Vault file extension: {path}")
        return path

    def _snapshot(self, relative_path: str) -> _Snapshot:
        resolved = self._paths.resolve(relative_path, for_write=True)
        if not resolved.exists:
            return _Snapshot(relative_path, False, None, ABSENT_HASH, None)
        return self._snapshot_path(resolved.path, relative_path)

    def _snapshot_path(
        self,
        path: Path,
        logical_path: str,
    ) -> _Snapshot:
        try:
            before = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise VaultTransactionError(f"cannot inspect Vault target {logical_path}: {error}") from error
        self._validate_stat(before, logical_path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            self._validate_stat(opened, logical_path)
            if self._identity(before) != self._identity(opened):
                raise VaultTransactionError(f"Vault target changed before open: {logical_path}")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, _READ_BLOCK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > self._max_file_bytes:
                    raise VaultTransactionError(f"Vault target exceeds {self._max_file_bytes} bytes: {logical_path}")
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if self._version(opened) != self._version(after) or self._identity(after) != self._identity(current):
            raise VaultTransactionError(f"Vault target changed while reading: {logical_path}")
        self._validate_stat(current, logical_path)
        content = b"".join(chunks)
        self._decode(content, logical_path)
        return _Snapshot(logical_path, True, content, content_hash(content), self._identity(current))

    @staticmethod
    def _validate_stat(
        info: os.stat_result,
        relative_path: str,
    ) -> None:
        attributes = int(getattr(info, "st_file_attributes", 0))
        if stat.S_ISLNK(info.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise VaultTransactionError(f"reparse point is forbidden: {relative_path}")
        if not stat.S_ISREG(info.st_mode):
            raise VaultTransactionError(f"Vault target is not a regular file: {relative_path}")
        if info.st_nlink > 1:
            raise VaultTransactionError(f"hard-linked Vault files are forbidden: {relative_path}")

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int]:
        return info.st_dev, info.st_ino

    @staticmethod
    def _version(info: os.stat_result) -> tuple[int, int, int, int, int]:
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    @staticmethod
    def _decode(content: bytes, relative_path: str) -> str:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise VaultTransactionError(f"Vault target is not UTF-8 text: {relative_path}") from error

    @staticmethod
    def _require_expected(path: str, content: bytes | None, expected: object) -> None:
        actual = ABSENT_HASH if content is None else content_hash(content)
        if expected != actual:
            raise PreflightConflict(
                f"expectedHash conflict for {path}",
                details={"path": path, "expected": expected, "actual": actual},
            )

    def _apply_line_edits(self, path: str, content: bytes, edits: Sequence[object]) -> bytes:
        lines = self._decode(content, path).splitlines(keepends=True)
        parsed: list[tuple[int, int, str]] = []
        for raw in edits:
            if not isinstance(raw, Mapping):
                raise VaultTransactionError("patch edit must be an object")
            start = raw.get("startLine")
            end = raw.get("endLine")
            replacement = raw.get("replacement")
            if not isinstance(start, int) or not isinstance(end, int) or not isinstance(replacement, str):
                raise VaultTransactionError("patch edit fields have invalid types")
            if end < start or start < 1 or end > len(lines):
                raise VaultTransactionError(f"patch line range is invalid for {path}: {start}-{end}")
            parsed.append((start, end, replacement))
        parsed.sort()
        for previous, current in pairwise(parsed):
            if current[0] <= previous[1]:
                raise VaultTransactionError(f"patch edits overlap in {path}")
        for start, end, replacement in reversed(parsed):
            lines[start - 1 : end] = replacement.splitlines(keepends=True)
        return "".join(lines).encode("utf-8")

    @staticmethod
    def _trash_path(call: ToolCall, index: int, source: str) -> str:
        basename = PurePosixPath(source).name
        identity = hashlib.sha256(
            f"{call.run_id}:{call.tool_call_id}:{call.args_hash}:{index}:{source}".encode()
        ).hexdigest()[:20]
        return f".trash/offeragent/{identity}-{basename}"

    @staticmethod
    def _build_diff(
        snapshots: Mapping[str, _Snapshot],
        final_contents: Mapping[str, bytes | None],
    ) -> bytes:
        parts: list[str] = []
        for path in sorted(final_contents, key=str.casefold):
            before = snapshots[path].content or b""
            after = final_contents[path] or b""
            parts.extend(
                difflib.unified_diff(
                    before.decode("utf-8").splitlines(keepends=True),
                    after.decode("utf-8").splitlines(keepends=True),
                    fromfile=f"a/{path}" if snapshots[path].exists else "/dev/null",
                    tofile=f"b/{path}" if final_contents[path] is not None else "/dev/null",
                )
            )
        return "".join(parts).encode("utf-8")

    def _observe_plan_state(self, plan: _PreparedTransaction) -> Mapping[str, object]:
        return self._state_facts(
            self._current_root_identity(),
            self._capture_parent_identities(plan.changed_paths),
            {path: self._snapshot(path) for path in plan.snapshots},
        )

    @staticmethod
    def _state_facts(
        root_identity: tuple[int, int],
        parent_identities: Mapping[str, tuple[int, int] | None],
        snapshots: Mapping[str, _Snapshot],
    ) -> Mapping[str, object]:
        return {
            "rootIdentity": VaultTransactionCoordinator._identity_token(root_identity),
            "parents": {
                path: None if identity is None else VaultTransactionCoordinator._identity_token(identity)
                for path, identity in sorted(parent_identities.items(), key=lambda item: item[0].casefold())
            },
            "files": {
                path: snapshot.content_hash
                for path, snapshot in sorted(snapshots.items(), key=lambda item: item[0].casefold())
            },
        }

    def _current_root_identity(self) -> tuple[int, int]:
        try:
            info = os.stat(self._root, follow_symlinks=False)
        except OSError as error:
            raise VaultTransactionError(f"Vault root is unavailable: {error}") from error
        self._validate_directory_stat(info, "")
        identity = self._identity(info)
        expected = getattr(self, "_root_identity", identity)
        if identity != expected:
            raise PreflightConflict(
                "Vault root identity changed",
                details={
                    "expected": self._identity_token(expected),
                    "observed": self._identity_token(identity),
                },
            )
        return identity

    @staticmethod
    def _identity_token(identity: tuple[int, int]) -> str:
        return f"{identity[0]:x}:{identity[1]:x}"

    def _capture_parent_identities(
        self,
        paths: Sequence[str],
    ) -> dict[str, tuple[int, int] | None]:
        identities: dict[str, tuple[int, int] | None] = {}
        for relative_path in paths:
            parts = PurePosixPath(relative_path).parts[:-1]
            current = self._root
            relative_parts: list[str] = []
            ancestor_missing = False
            for part in parts:
                current /= part
                relative_parts.append(part)
                parent = PurePosixPath(*relative_parts).as_posix()
                if ancestor_missing:
                    identities.setdefault(parent, None)
                    continue
                try:
                    info = os.stat(current, follow_symlinks=False)
                except FileNotFoundError:
                    identities.setdefault(parent, None)
                    ancestor_missing = True
                    continue
                except OSError as error:
                    raise VaultTransactionError(f"cannot inspect Vault parent {parent}: {error}") from error
                self._validate_directory_stat(info, parent)
                identity = self._identity(info)
                existing = identities.setdefault(parent, identity)
                if existing != identity:
                    raise PreflightConflict(f"Vault parent identity changed while reading: {parent}")
        return identities

    @staticmethod
    def _validate_directory_stat(info: os.stat_result, relative_path: str) -> None:
        attributes = int(getattr(info, "st_file_attributes", 0))
        if stat.S_ISLNK(info.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise VaultTransactionError(f"reparse point is forbidden: {relative_path or '<vault-root>'}")
        if not stat.S_ISDIR(info.st_mode):
            raise VaultTransactionError(f"Vault parent is not a directory: {relative_path or '<vault-root>'}")

    def _apply_change(
        self,
        plan: _PreparedTransaction,
        relative_path: str,
        snapshot: _Snapshot,
        final_content: bytes | None,
        transaction_created_identities: Mapping[Path, tuple[int, int]],
        manifest: DurableVaultTransactionManifest | None,
    ) -> tuple[_AppliedChange, Mapping[Path, tuple[int, int]]]:
        current = self._snapshot(relative_path)
        if current.content_hash != snapshot.content_hash:
            raise PreflightConflict(
                f"Vault target changed immediately before apply: {relative_path}",
                details={"expected": snapshot.content_hash, "actual": current.content_hash},
            )
        target = self._root.joinpath(*PurePosixPath(relative_path).parts)
        self._validate_apply_ancestry(
            plan,
            relative_path,
            created_identities=transaction_created_identities,
            allow_missing=True,
        )
        created = self._ensure_parent_directories(target.parent)
        created_dirs = tuple(path for path, _identity in created)
        created_identities = {**transaction_created_identities, **dict(created)}
        self._validate_apply_ancestry(
            plan,
            relative_path,
            created_identities=created_identities,
            allow_missing=False,
        )
        parent_paths = self._cas_parent_paths(plan, relative_path, created_identities)
        after_hash = ABSENT_HASH if final_content is None else content_hash(final_content)
        change: AtomicVaultChange | None = None
        temporary: Path | None = None
        temporary_identity: tuple[int, int] | None = None
        applied: _AppliedChange | None = None
        suffix = plan.token.removeprefix("sha256:")[:20]
        backup = target.parent / f".offeragent-tx-{suffix}-{target.name}.bak" if snapshot.exists else None
        rollback_transient = target.parent / f".offeragent-rollback-{suffix}-{target.name}"
        try:
            if manifest is not None:
                if self._manifests is None or final_content is None or manifest.path != relative_path:
                    raise DurableManifestError("durable transaction plan is not a single public file write")
                temporary = self._internal_manifest_path(manifest.temporary_path)
                manifest_backup = (
                    None if manifest.backup_path is None else self._internal_manifest_path(manifest.backup_path)
                )
                manifest_rollback = self._internal_manifest_path(manifest.rollback_path)
                if (
                    temporary.parent != target.parent
                    or manifest_backup != backup
                    or manifest_rollback != rollback_transient
                ):
                    raise DurableManifestError("durable transaction internal paths changed after preflight")
                for internal in (temporary, backup, rollback_transient):
                    if internal is not None:
                        self._require_absent_internal(internal)
                self._write_temporary_at(temporary, final_content)
                staged_temporary = self._snapshot_path(temporary, manifest.temporary_path)
                if staged_temporary.identity is None:
                    raise DurableManifestError("durable temporary file has no filesystem identity")
                temporary_identity = staged_temporary.identity
                active_parents = {
                    path: manifest_identity_token(identity)
                    for path, identity in self._capture_parent_identities((relative_path,)).items()
                    if identity is not None
                }
                if frozenset(active_parents) != frozenset(manifest.approved_parents):
                    raise DurableManifestError("durable transaction parent identity set changed")
                staged = manifest.transition(
                    DurableManifestState.STAGED,
                    active_parents=active_parents,
                    temporary_identity=manifest_identity_token(temporary_identity),
                )
                self._manifests.save(staged)
                self._signal("manifest_staged", relative_path)
            change = self._cas.begin(
                relative_path=relative_path,
                target=target,
                expected_root_identity=plan.root_identity,
                parent_paths=parent_paths,
                expected_exists=snapshot.exists,
                before_hash=snapshot.content_hash,
                after_hash=after_hash,
            )
            applied = _AppliedChange(relative_path, target, snapshot, final_content, change)
            if final_content is not None and manifest is None:
                temporary = self._write_temporary(target.parent, final_content)
            change.apply(
                temporary=temporary,
                backup=backup,
                rollback_transient=rollback_transient,
            )
            return applied, dict(created)
        except BaseException as error:
            translated = self._translate_cas_error(error)
            if change is None:
                if temporary is not None and temporary_identity is not None:
                    try:
                        self._cas.recover_delete(
                            path=temporary,
                            expected_root_identity=plan.root_identity,
                            parent_paths=parent_paths,
                            expected_hash=after_hash,
                            expected_identity=temporary_identity,
                        )
                    except FileNotFoundError:
                        pass
                    except BaseException as cleanup_error:
                        raise _DurableApplyOutcomeUncertain(
                            f"temporary cleanup was not confirmed: {type(cleanup_error).__name__}: {cleanup_error}"
                        ) from error
                self._remove_empty_directories(set(created_dirs))
                if translated is error:
                    raise
                raise translated from error
            if change.mutated:
                assert applied is not None
                try:
                    change.rollback()
                except BaseException as rollback_error:
                    try:
                        change.close_preserving()
                    except BaseException as close_error:
                        rollback_error = AtomicVaultCasUncertain(
                            f"{rollback_error}; CAS handle close also failed: {close_error}"
                        )
                    cause = AtomicVaultCasUncertain(
                        f"{translated}; current-path rollback was not confirmed: {rollback_error}"
                    )
                    raise _ApplyOutcomeUncertain(applied, created_dirs, cause) from error
            else:
                try:
                    change.discard_unmutated()
                except BaseException as discard_error:
                    cause = AtomicVaultCasUncertain(
                        f"{translated}; temporary cleanup was not confirmed: {discard_error}"
                    )
                    assert applied is not None
                    raise _ApplyOutcomeUncertain(applied, created_dirs, cause) from error
            if temporary is not None:
                try:
                    temporary.lstat()
                except FileNotFoundError:
                    pass
                else:
                    try:
                        if temporary_identity is None:
                            self._cas.discard_temporary(temporary, after_hash)
                        else:
                            self._cas.recover_delete(
                                path=temporary,
                                expected_root_identity=plan.root_identity,
                                parent_paths=parent_paths,
                                expected_hash=after_hash,
                                expected_identity=temporary_identity,
                            )
                    except BaseException as discard_error:
                        assert applied is not None
                        cause = AtomicVaultCasUncertain(
                            f"{translated}; detached temporary cleanup was not confirmed: {discard_error}"
                        )
                        raise _ApplyOutcomeUncertain(applied, created_dirs, cause) from error
            self._remove_empty_directories(set(created_dirs))
            if translated is error:
                raise
            raise translated from error

    def _cas_parent_paths(
        self,
        plan: _PreparedTransaction,
        relative_path: str,
        created_identities: Mapping[Path, tuple[int, int]],
    ) -> tuple[tuple[Path, tuple[int, int]], ...]:
        result: list[tuple[Path, tuple[int, int]]] = []
        current = self._root
        relative_parts: list[str] = []
        for part in PurePosixPath(relative_path).parts[:-1]:
            current /= part
            relative_parts.append(part)
            parent = PurePosixPath(*relative_parts).as_posix()
            expected = plan.parent_identities[parent]
            if expected is None:
                expected = created_identities.get(current)
            if expected is None:
                raise PreflightConflict(f"Vault parent has no approved identity: {parent}")
            result.append((current, expected))
        return tuple(result)

    @staticmethod
    def _translate_cas_error(error: BaseException) -> BaseException:
        if isinstance(error, AtomicVaultCasConflict):
            return VaultTransactionCoordinator._cas_conflict(error)
        return error

    @staticmethod
    def _cas_conflict(error: AtomicVaultCasConflict) -> PreflightConflict:
        return PreflightConflict(
            str(error),
            details={"reason": "vault_atomic_cas_conflict", "cause": type(error).__name__},
        )

    def _validate_apply_ancestry(
        self,
        plan: _PreparedTransaction,
        relative_path: str,
        *,
        created_identities: Mapping[Path, tuple[int, int]],
        allow_missing: bool,
    ) -> None:
        root_identity = self._current_root_identity()
        if root_identity != plan.root_identity:
            raise PreflightConflict("Vault root changed after transaction preflight")
        current = self._root
        relative_parts: list[str] = []
        for part in PurePosixPath(relative_path).parts[:-1]:
            current /= part
            relative_parts.append(part)
            parent = PurePosixPath(*relative_parts).as_posix()
            expected = plan.parent_identities[parent]
            try:
                info = os.stat(current, follow_symlinks=False)
            except FileNotFoundError:
                if expected is None and allow_missing:
                    continue
                raise PreflightConflict(f"Vault parent disappeared before apply: {parent}") from None
            except OSError as error:
                raise PreflightConflict(f"cannot revalidate Vault parent before apply: {parent}") from error
            self._validate_directory_stat(info, parent)
            observed = self._identity(info)
            if expected is not None:
                if observed != expected:
                    raise PreflightConflict(f"Vault parent identity changed before apply: {parent}")
                continue
            created_identity = created_identities.get(current)
            if created_identity is None or observed != created_identity:
                raise PreflightConflict(f"Vault parent appeared or changed after preflight: {parent}")

    def _ensure_parent_directories(self, parent: Path) -> tuple[tuple[Path, tuple[int, int]], ...]:
        missing: list[Path] = []
        current = parent
        while current != self._root:
            try:
                info = os.stat(current, follow_symlinks=False)
            except FileNotFoundError:
                missing.append(current)
                current = current.parent
                continue
            self._validate_directory_stat(info, current.relative_to(self._root).as_posix())
            break
        if current == self._root:
            self._current_root_identity()
        created: list[tuple[Path, tuple[int, int]]] = []
        for directory in reversed(missing):
            directory.mkdir()
            info = os.stat(directory, follow_symlinks=False)
            self._validate_directory_stat(info, directory.relative_to(self._root).as_posix())
            created.append((directory, self._identity(info)))
        return tuple(created)

    @staticmethod
    def _write_temporary(parent: Path, content: bytes) -> Path:
        descriptor, raw_path = tempfile.mkstemp(prefix=".offeragent-tx-", suffix=".tmp", dir=parent)
        path = Path(raw_path)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _write_temporary_at(path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # Do not unlink by name after the creating handle closed: a
            # same-user path swap could make that name external.  The durable
            # PREPARED manifest preserves the path for identity-safe startup
            # classification instead.
            raise

    def _internal_manifest_path(self, relative_path: str) -> Path:
        normalized = self._normalize_path(relative_path, internal=True)
        if normalized != relative_path:
            raise DurableManifestError("durable manifest path is not canonical for this Vault")
        return self._root.joinpath(*PurePosixPath(normalized).parts)

    @staticmethod
    def _require_absent_internal(path: Path) -> None:
        try:
            path.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise DurableManifestError(f"cannot prove internal transaction path absent: {path.name}") from error
        raise DurableManifestError(f"internal transaction path is already occupied: {path.name}")

    def _signal(self, stage: str, relative_path: str) -> None:
        if self._cas_barrier is not None:
            self._cas_barrier(stage, relative_path)

    async def _rollback(
        self,
        applied: Sequence[_AppliedChange],
        created_directories: set[Path],
    ) -> list[str]:
        errors: list[str] = []
        for index, change in enumerate(reversed(applied)):
            try:
                self._faults.before_rollback(index, change.relative_path)
                await _run_io(self._rollback_change, change)
            except BaseException as error:
                close_error: BaseException | None = None
                try:
                    change.cas.close_preserving()
                except BaseException as candidate:
                    close_error = candidate
                message = f"{change.relative_path}: {type(error).__name__}: {error}"
                if close_error is not None:
                    message += f"; close: {type(close_error).__name__}: {close_error}"
                errors.append(message)
        if not errors:
            self._remove_empty_directories(created_directories)
        return errors

    def _rollback_change(self, change: _AppliedChange) -> None:
        change.cas.rollback()

    def _cleanup_applied(
        self,
        applied: Sequence[_AppliedChange],
        created_directories: set[Path],
    ) -> list[str]:
        errors: list[str] = []
        for index, change in enumerate(applied):
            try:
                self._faults.before_cleanup(index, change.relative_path)
                change.cas.cleanup_committed()
            except BaseException as error:
                close_error: BaseException | None = None
                try:
                    change.cas.close_preserving()
                except BaseException as candidate:
                    close_error = candidate
                message = f"{change.relative_path}: {type(error).__name__}: {error}"
                if close_error is not None:
                    message += f"; close: {type(close_error).__name__}: {close_error}"
                errors.append(message)
        self._remove_empty_directories(created_directories)
        return errors

    def _remove_empty_directories(self, directories: set[Path]) -> None:
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                continue

    async def _store_artifact(
        self,
        call: ToolCall,
        content: bytes,
        *,
        mime_type: str,
        purpose: str,
        sensitivity: Sensitivity,
    ) -> ArtifactMetadata:
        digest = content_hash(content)
        identity = hashlib.sha256(
            f"{call.workspace_id}:{call.run_id}:{call.tool_call_id}:{call.args_hash}:{purpose}:{digest}".encode()
        ).hexdigest()[:32]
        metadata = ArtifactMetadata(
            artifact_id=f"art_{identity}",
            workspace_id=call.workspace_id,
            owner_run_id=call.run_id,
            mime_type=mime_type,
            byte_length=len(content),
            sha256=digest,
            sensitivity=sensitivity,
            state=ArtifactState.COMPLETE,
            created_at=self._clock.utcnow(),
            attributes={"purpose": purpose, "toolCallId": call.tool_call_id, "argsHash": call.args_hash},
        )
        reservation = await self._artifact_budget.reserve_artifact_bytes(len(content))
        try:
            stored = await self._artifacts.put(
                metadata,
                content,
                idempotency_key=f"{purpose}:{call.idempotency_key}:{digest}",
            )
            await reservation.commit()
        except BaseException:
            await reservation.release()
            raise
        if stored.sha256 != digest or stored.workspace_id != call.workspace_id:
            raise VaultTransactionError("Artifact Store returned mismatched preflight evidence")
        return stored

    async def _uncertain_result(
        self,
        call: ToolCall,
        plan: _PreparedTransaction,
        applied: Sequence[_AppliedChange],
        rollback_errors: Sequence[str],
        cause: BaseException,
    ) -> ToolResult:
        bundle = {
            "stateHash": plan.state_hash,
            "cause": f"{type(cause).__name__}: {cause}",
            "rollbackErrors": list(rollback_errors),
            "files": {
                path: {
                    "original": (
                        None if snapshot.content is None else base64.b64encode(snapshot.content).decode("ascii")
                    ),
                    "originalHash": snapshot.content_hash,
                    "plannedHash": (
                        ABSENT_HASH
                        if plan.final_contents[path] is None
                        else content_hash(plan.final_contents[path] or b"")
                    ),
                }
                for path, snapshot in plan.snapshots.items()
            },
        }
        content = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        recovery_ids: tuple[str, ...] = ()
        try:
            artifact = await self._store_artifact(
                call,
                content,
                mime_type="application/vnd.offeragent.vault-recovery+json",
                purpose="vault-transaction-recovery",
                sensitivity=Sensitivity.PRIVATE,
            )
            recovery_ids = (artifact.artifact_id,)
        except Exception:
            pass
        effects = tuple(
            SideEffect(
                SideEffectKind.FILE_WRITE,
                SideEffectState.UNKNOWN,
                f"vault:{self._workspace_id}:{change.relative_path}",
                {"hash": change.snapshot.content_hash},
                None,
                {"rollbackErrors": list(rollback_errors)},
            )
            for change in applied
        ) or (
            SideEffect(
                SideEffectKind.FILE_WRITE,
                SideEffectState.UNKNOWN,
                f"vault:{self._workspace_id}",
                None,
                None,
                {"rollbackErrors": list(rollback_errors)},
            ),
        )
        message = "Vault transaction outcome requires manual recovery review"
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.UNKNOWN_OUTCOME,
            {
                "paths": list(plan.changed_paths),
                "stateHash": plan.state_hash,
                "recoveryArtifactIds": list(recovery_ids),
            },
            message,
            recovery_ids,
            (),
            effects,
            False,
            {"stateHash": plan.state_hash},
            None,
            ToolError("vault_rollback_unconfirmed", message, False, False, {"errors": list(rollback_errors)}),
        )

    async def _committed_cleanup_partial(
        self,
        call: ToolCall,
        plan: _PreparedTransaction,
        cleanup_errors: Sequence[str],
    ) -> ToolResult:
        bundle = {
            "stateHash": plan.state_hash,
            "cleanupErrors": list(cleanup_errors),
            "paths": list(plan.changed_paths),
            "committedHashes": {
                path: ABSENT_HASH if content is None else content_hash(content)
                for path, content in plan.final_contents.items()
            },
        }
        content = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        recovery_ids: tuple[str, ...] = ()
        try:
            artifact = await self._store_artifact(
                call,
                content,
                mime_type="application/vnd.offeragent.vault-cleanup-recovery+json",
                purpose="vault-transaction-cleanup-recovery",
                sensitivity=Sensitivity.PRIVATE,
            )
            recovery_ids = (artifact.artifact_id,)
        except Exception:
            pass
        message = "Vault transaction committed, but internal backup cleanup is incomplete"
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.PARTIAL,
            {
                "paths": list(plan.changed_paths),
                "stateHash": plan.state_hash,
                "recoveryArtifactIds": list(recovery_ids),
            },
            message,
            recovery_ids,
            (),
            self._effects(plan, SideEffectState.COMMITTED),
            False,
            {"stateHash": plan.state_hash},
            {
                path: ABSENT_HASH if content is None else content_hash(content)
                for path, content in plan.final_contents.items()
            },
            ToolError(
                "vault_cleanup_incomplete",
                message,
                False,
                False,
                {"errors": list(cleanup_errors)},
            ),
        )

    def _cancelled(
        self,
        call: ToolCall,
        plan: _PreparedTransaction,
        applied: Sequence[_AppliedChange],
    ) -> ToolResult:
        message = "Vault transaction was cancelled and rolled back"
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.CANCELLED,
            {"paths": list(plan.changed_paths), "stateHash": plan.state_hash, "recoveryArtifactIds": []},
            message,
            (),
            (),
            self._effects(plan, SideEffectState.ROLLED_BACK, applied),
            False,
            {"stateHash": plan.state_hash},
            {"stateHash": plan.state_hash},
            ToolError("vault_transaction_cancelled", message, False, True),
        )

    def _rolled_back_failure(
        self,
        call: ToolCall,
        plan: _PreparedTransaction,
        applied: Sequence[_AppliedChange],
        cause: BaseException,
    ) -> ToolResult:
        message = f"Vault transaction failed and rolled back: {type(cause).__name__}: {cause}"
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.FAILED,
            {"paths": list(plan.changed_paths), "stateHash": plan.state_hash, "recoveryArtifactIds": []},
            message,
            (),
            (),
            self._effects(plan, SideEffectState.ROLLED_BACK, applied),
            False,
            {"stateHash": plan.state_hash},
            {"stateHash": plan.state_hash},
            ToolError("vault_transaction_rolled_back", message, False, False),
        )

    def _rolled_back_conflict(
        self,
        call: ToolCall,
        plan: _PreparedTransaction,
        applied: Sequence[_AppliedChange],
        cause: PreflightConflict,
    ) -> ToolResult:
        message = f"Vault transaction conflicted and rolled back: {cause}"
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.CONFLICTED,
            {"paths": list(plan.changed_paths), "stateHash": plan.state_hash, "recoveryArtifactIds": []},
            message,
            (),
            (),
            self._effects(plan, SideEffectState.ROLLED_BACK, applied),
            False,
            {"stateHash": plan.state_hash},
            {"stateHash": plan.state_hash},
            ToolError("vault_transaction_conflict", message, False, False, cause.details),
        )

    def _effects(
        self,
        plan: _PreparedTransaction,
        state: SideEffectState,
        applied: Sequence[_AppliedChange] | None = None,
    ) -> tuple[SideEffect, ...]:
        limited = set(plan.changed_paths if applied is None else (item.relative_path for item in applied))
        effects: list[SideEffect] = []
        for operation in plan.operations:
            source = operation["path"]
            assert isinstance(source, str)
            normalized_source = self._normalize_path(source, internal=False)
            if normalized_source not in limited:
                continue
            op = operation["op"]
            kind = (
                SideEffectKind.FILE_RENAME
                if op == "rename"
                else SideEffectKind.FILE_TRASH
                if op == "trash"
                else SideEffectKind.FILE_WRITE
            )
            effects.append(
                SideEffect(
                    kind,
                    state,
                    f"vault:{self._workspace_id}:{normalized_source}",
                    {"hash": plan.snapshots[normalized_source].content_hash},
                    {
                        "hash": (
                            ABSENT_HASH
                            if plan.final_contents[normalized_source] is None
                            else content_hash(plan.final_contents[normalized_source] or b"")
                        )
                    },
                    {"op": op},
                )
            )
        return tuple(effects)

    @staticmethod
    def _failure(call: ToolCall, code: str, message: str) -> ToolResult:
        return ToolResult(
            call.tool_call_id,
            ToolResultStatus.FAILED,
            None,
            message,
            (),
            (),
            (),
            False,
            None,
            None,
            ToolError(code, message, False, False),
        )

    @staticmethod
    def _plan_key(call: ToolCall) -> str:
        return canonical_json_sha256(
            {
                "workspaceId": call.workspace_id,
                "runId": call.run_id,
                "toolCallId": call.tool_call_id,
                "argsHash": call.args_hash,
                "definitionFingerprint": call.definition_fingerprint,
            }
        )


async def _run_io(function: Callable[..., T], *args: object) -> T:
    completion = await _run_io_owned(function, *args)
    if completion.cancellation is not None:
        if completion.error is not None:
            raise completion.cancellation from completion.error
        raise completion.cancellation
    if completion.error is not None:
        raise completion.error
    return cast(T, completion.value)


async def _run_io_owned(function: Callable[..., T], *args: object) -> _OwnedIoCompletion[T]:
    """Wait for one shielded thread and preserve its value across cancellation.

    A Windows CAS operation can return the only Python owner of open file and
    directory handles.  Cancelling the awaiting Task must not discard that
    value: callers such as ``_execute`` first register the returned lease, then
    re-raise ``cancellation`` and roll it back deterministically.
    """

    def invoke() -> _OwnedIoCompletion[T]:
        try:
            return _OwnedIoCompletion(function(*args), None, None)
        except BaseException as error:
            return _OwnedIoCompletion(None, error, None)

    task = asyncio.create_task(asyncio.to_thread(invoke))
    interruption: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if interruption is None:
                interruption = error
            continue
    completion = task.result()
    return _OwnedIoCompletion(completion.value, completion.error, interruption)


__all__ = [
    "VaultFaultInjector",
    "VaultTransactionCoordinator",
    "VaultTransactionError",
    "VaultTransactionRecoveryReport",
    "content_hash",
]
