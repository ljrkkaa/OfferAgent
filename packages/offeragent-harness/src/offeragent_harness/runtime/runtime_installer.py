"""Transactional, current-user Windows Runtime install/update lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Protocol, cast

from .process_lock import ProcessLock, installer_mutex_name
from .release_manifest import (
    RuntimeBundleVerifier,
    SafeRuntimeZipExtractor,
    VerifiedRuntimeBundle,
    native_windows_architecture,
    parse_manifest,
)
from .release_privileges import (
    RuntimePrivilegeApprovalReceipt,
    RuntimePrivilegeAssessment,
    RuntimePrivilegeError,
    assess_privilege_change,
    validate_privilege_approval_receipt,
)
from .windows_security import WindowsProcessIdentityState, windows_process_identity_state

_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,63}$")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_PURGE_CONFIRMATION = "DELETE OFFERAGENT LOCAL DATA"
_PURGE_JOURNAL_NAME = "purge-journal.json"
_ROOT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")


class RuntimeInstallError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RuntimePrivilegeApprovalRequired(RuntimeInstallError):
    """Structured, hash-bound privilege expansion challenge for the UI."""

    def __init__(
        self,
        *,
        old_manifest_hash: str | None,
        new_manifest_hash: str,
        assessment: RuntimePrivilegeAssessment,
    ) -> None:
        self.old_manifest_hash = old_manifest_hash
        self.new_manifest_hash = new_manifest_hash
        self.old_privilege_fingerprint = assessment.old_fingerprint
        self.new_privilege_fingerprint = assessment.new_fingerprint
        self.diff_hash = assessment.diff_hash
        self.diff = assessment.diff
        super().__init__(
            "runtime_privilege_approval_required",
            "Runtime privilege expansion requires an exact, one-time user approval receipt",
        )


class RuntimeInstallPhase(str, Enum):
    LOCATING_EMBEDDED_BUNDLE = "locating_embedded_bundle"
    VERIFYING_MANIFEST_AND_SIGNATURE = "verifying_manifest_and_signature"
    EXTRACTING_TO_STAGING = "extracting_to_staging"
    VERIFYING_EACH_FILE = "verifying_each_file"
    RUNTIME_SELF_TEST = "runtime_self_test"
    QUIESCING_RUNTIME = "quiescing_runtime"
    BACKING_UP_STATE = "backing_up_state"
    MIGRATING_STATE = "migrating_state"
    ATOMIC_ACTIVATE = "atomic_activate"
    READY = "ready"
    ROLLING_BACK = "rolling_back"


class RuntimePurgePhase(str, Enum):
    """Last durably completed step of a destructive local-data purge."""

    PREPARED = "prepared"
    OWNER_RELEASED = "owner_released"
    RUNTIME_VERSIONS_AND_POINTER_REMOVED = "runtime_versions_and_pointer_removed"
    APPCONTAINER_CLEANED = "appcontainer_cleaned"
    WORKSPACES_REMOVED = "workspaces_removed"
    RUNTIME_REMOVED = "runtime_removed"
    LOCAL_RESIDUE_REMOVED = "local_residue_removed"
    COMPLETE = "complete"


_PURGE_PHASES = tuple(RuntimePurgePhase)
_PURGE_PHASE_INDEX = {phase: index for index, phase in enumerate(_PURGE_PHASES)}


@dataclass(frozen=True, slots=True)
class _RuntimePurgeJournal:
    operation_id: str
    owner_id: str
    runtime_root_hash: str
    workspaces_root_hash: str
    phase: RuntimePurgePhase
    removed_versions: tuple[str, ...]

    def __post_init__(self) -> None:
        if _OPERATION_ID.fullmatch(self.operation_id) is None:
            raise RuntimeInstallError("purge_journal_invalid", "purge operation id is invalid")
        if _OWNER.fullmatch(self.owner_id) is None:
            raise RuntimeInstallError("purge_journal_invalid", "purge owner is invalid")
        if (
            _ROOT_HASH.fullmatch(self.runtime_root_hash) is None
            or _ROOT_HASH.fullmatch(self.workspaces_root_hash) is None
        ):
            raise RuntimeInstallError("purge_journal_invalid", "purge root identity is invalid")
        if (
            len(self.removed_versions) > 1024
            or tuple(sorted(self.removed_versions)) != self.removed_versions
            or len(set(self.removed_versions)) != len(self.removed_versions)
            or any(_VERSION.fullmatch(version) is None for version in self.removed_versions)
        ):
            raise RuntimeInstallError("purge_journal_invalid", "purge removed versions are invalid")


@dataclass(frozen=True, slots=True)
class RuntimeSelfTestReport:
    healthy: bool
    checks: Mapping[str, bool]
    diagnostic_code: str | None = None

    def __post_init__(self) -> None:
        if not self.checks or self.healthy != all(self.checks.values()):
            raise ValueError("self-test healthy flag must equal all checks")
        if self.healthy and self.diagnostic_code is not None:
            raise ValueError("healthy self-test cannot have a diagnostic code")


class RuntimeSelfTestRunner(Protocol):
    def run(self, runtime_root: Path, *, timeout_seconds: float) -> RuntimeSelfTestReport: ...


class RuntimeQuiescer(Protocol):
    def quiesce_for_update(self, *, timeout_seconds: float) -> AbstractContextManager[None]: ...


class StateMigrationRunner(Protocol):
    def migrate(self, runtime_root: Path, state_databases: Sequence[Path], target_schema: int) -> None: ...


class RuntimePurge(Protocol):
    def purge_non_vault_data(self) -> None:
        """Idempotently remove registered non-Vault resources while workspace journals still exist."""
        ...


class RuntimeLock(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


@dataclass(frozen=True, slots=True)
class ConsumedPrivilegeApproval:
    receipt_id: str
    expires_at: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.receipt_id) is None:
            raise RuntimeInstallError("pointer_invalid", "privilege approval receipt id is invalid")
        try:
            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeInstallError("pointer_invalid", "privilege approval expiry is invalid") from error
        if not self.expires_at.endswith("Z") or expires.tzinfo is None or expires.utcoffset() is None:
            raise RuntimeInstallError("pointer_invalid", "privilege approval expiry must be UTC")


@dataclass(frozen=True, slots=True)
class RuntimePointer:
    current_version: str
    previous_version: str | None
    manifest_hash: str
    activated_at: datetime
    generation: int
    state_schema_version: int
    rollback_snapshot: str | None = None
    privilege_approval_journal: tuple[ConsumedPrivilegeApproval, ...] = ()

    def __post_init__(self) -> None:
        if not _VERSION.fullmatch(self.current_version):
            raise RuntimeInstallError("pointer_invalid", "current runtime version is invalid")
        if self.previous_version is not None and not _VERSION.fullmatch(self.previous_version):
            raise RuntimeInstallError("pointer_invalid", "previous runtime version is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.manifest_hash):
            raise RuntimeInstallError("pointer_invalid", "current manifest hash is invalid")
        if self.activated_at.tzinfo is None or self.activated_at.utcoffset() is None:
            raise RuntimeInstallError("pointer_invalid", "activation time must be timezone-aware")
        if self.generation < 1 or self.state_schema_version < 1:
            raise RuntimeInstallError("pointer_invalid", "runtime pointer generation/schema is invalid")
        if (
            self.rollback_snapshot is not None
            and re.fullmatch(r"\.rollback-snapshots/generation-[1-9][0-9]*-[0-9a-f]{32}", self.rollback_snapshot)
            is None
        ):
            raise RuntimeInstallError("pointer_invalid", "runtime rollback snapshot path is invalid")
        journal_ids = [item.receipt_id for item in self.privilege_approval_journal]
        if (
            len(journal_ids) > 128
            or len(set(journal_ids)) != len(journal_ids)
            or tuple(sorted(self.privilege_approval_journal, key=lambda item: item.receipt_id))
            != self.privilege_approval_journal
        ):
            raise RuntimeInstallError("pointer_invalid", "privilege approval journal is invalid")


@dataclass(frozen=True, slots=True)
class InstalledRuntime:
    root: Path
    version: str
    pointer: RuntimePointer
    installed: bool
    self_test: RuntimeSelfTestReport


class SubprocessRuntimeSelfTestRunner:
    """Execute the packaged self-test without invoking a shell or PowerShell."""

    _MAXIMUM_RESULT = 256 * 1024
    _REQUIRED_CHECKS = frozenset(
        {
            "fd3_fd4_attach_contract",
            "host_worker_job_cleanup",
            "host_worker_signed_attach",
            "host_worker_start",
            "job_object_exit",
            "loopback_random_bind",
            "named_pipe_handshake",
            "process_host_signed_runtime_info",
            "runtime_hashes_authenticode_web",
            "sqlite_wal_integrity",
            "vault_read_only",
        }
    )

    def run(self, runtime_root: Path, *, timeout_seconds: float) -> RuntimeSelfTestReport:
        executable = (runtime_root / "offeragent-self-test.exe").resolve(strict=True)
        try:
            completed = subprocess.run(
                [str(executable), "run", "--canonical-json"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=_minimal_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeInstallError("self_test_unavailable", "runtime self-test could not complete") from error
        if len(completed.stdout) > self._MAXIMUM_RESULT or len(completed.stderr) > self._MAXIMUM_RESULT:
            raise RuntimeInstallError("self_test_output_limit", "runtime self-test output exceeded limits")
        try:
            value = json.loads(completed.stdout.decode("utf-8", errors="strict"))
            if not isinstance(value, dict) or set(value) != {"checks", "diagnosticCode", "healthy"}:
                raise ValueError
            checks = value["checks"]
            if (
                not isinstance(checks, dict)
                or not checks
                or any(not isinstance(key, str) or not isinstance(result, bool) for key, result in checks.items())
                or not self._REQUIRED_CHECKS <= set(checks)
            ):
                raise ValueError
            diagnostic = value["diagnosticCode"]
            if not isinstance(value["healthy"], bool):
                raise ValueError
            if diagnostic is not None and not isinstance(diagnostic, str):
                raise ValueError
            report = RuntimeSelfTestReport(value["healthy"], dict(checks), diagnostic)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise RuntimeInstallError("self_test_malformed", "runtime self-test returned malformed output") from error
        if completed.returncode != 0 or not report.healthy:
            raise RuntimeInstallError(
                report.diagnostic_code or "self_test_failed",
                "runtime self-test rejected the candidate release",
            )
        return report


class RuntimeInstaller:
    """Install and atomically activate one signed Runtime for the current user."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        workspaces_root: Path,
        verifier: RuntimeBundleVerifier,
        extractor: SafeRuntimeZipExtractor,
        self_test: RuntimeSelfTestRunner,
        migration: StateMigrationRunner,
        quiescer: RuntimeQuiescer,
        purge: RuntimePurge | None = None,
        lock_factory: Callable[[], RuntimeLock] | None = None,
        clock: Callable[[], datetime] | None = None,
        purge_failure_injector: Callable[[RuntimePurgePhase], None] | None = None,
        legacy_owner_is_active: Callable[[int], bool | None] | None = None,
    ) -> None:
        self._runtime_root = runtime_root.resolve(strict=False)
        self._workspaces_root = workspaces_root.resolve(strict=False)
        if (
            self._runtime_root == self._workspaces_root
            or _is_within(self._runtime_root, self._workspaces_root)
            or _is_within(self._workspaces_root, self._runtime_root)
        ):
            raise ValueError("runtime and workspace state roots must be isolated")
        if (
            self._runtime_root.parent != self._workspaces_root.parent
            or self._runtime_root.name != "runtime"
            or self._workspaces_root.name != "workspaces"
        ):
            raise ValueError("runtime and workspace roots must use the fixed OfferAgent local layout")
        self._local_state_root = self._runtime_root.parent
        self._verifier = verifier
        self._extractor = extractor
        self._self_test = self_test
        self._migration = migration
        self._quiescer = quiescer
        self._purge = purge
        self._lock_factory = lock_factory or (lambda: ProcessLock(installer_mutex_name()))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._purge_failure_injector = purge_failure_injector
        self._pointer_store = _PointerStore(self._runtime_root / "current.json")
        self._failures = _FailureCircuit(self._runtime_root / "update-failures.json", self._clock)
        self._references = _ReferenceStore(
            self._runtime_root / "runtime-references.json",
            legacy_owner_is_active=legacy_owner_is_active or _legacy_owner_is_active,
        )
        self._purge_journal = _PurgeJournalStore(
            self._local_state_root / _PURGE_JOURNAL_NAME,
            runtime_root_hash=_root_identity_hash(self._runtime_root),
            workspaces_root_hash=_root_identity_hash(self._workspaces_root),
        )

    def ensure_ready(
        self,
        bundle_root: Path,
        *,
        plugin_version: str,
        protocol_version: str,
        schema_hash: str,
        owner_id: str,
        legacy_owner_id: str | None = None,
        expected_architecture: str | None = None,
        windows_build: int | None = None,
        progress: Callable[[RuntimeInstallPhase], None] | None = None,
        privilege_approval: RuntimePrivilegeApprovalReceipt | None = None,
    ) -> InstalledRuntime:
        if not _OWNER.fullmatch(owner_id):
            raise ValueError("runtime reference owner is invalid")
        if legacy_owner_id is not None:
            legacy_match = re.fullmatch(r"obsidian-([1-9][0-9]{0,9})", legacy_owner_id)
            if legacy_match is None or int(legacy_match.group(1)) > 0xFFFFFFFF or legacy_owner_id == owner_id:
                raise ValueError("legacy runtime reference owner is invalid")
        notify = progress or (lambda _: None)
        architecture = expected_architecture if expected_architecture is not None else native_windows_architecture()
        notify(RuntimeInstallPhase.LOCATING_EMBEDDED_BUNDLE)
        notify(RuntimeInstallPhase.VERIFYING_MANIFEST_AND_SIGNATURE)
        bundle = self._verifier.verify_bundle(
            bundle_root,
            expected_architecture=architecture,
            windows_build=windows_build if windows_build is not None else _windows_build(),
            plugin_version=plugin_version,
            protocol_version=protocol_version,
            schema_hash=schema_hash,
        )
        version = bundle.manifest.runtime_version
        new_manifest_hash = f"sha256:{hashlib.sha256(bundle.manifest_bytes).hexdigest()}"
        new_privileges = bundle.manifest.privilege_envelope
        if new_privileges is None:
            raise RuntimeInstallError(
                "release_privilege_envelope_missing",
                "candidate Runtime has no signed privilege envelope",
            )
        with self._lock_factory():
            self._assert_purge_not_in_progress()
            self._runtime_root.mkdir(parents=True, exist_ok=True)
            _reject_managed_root(self._runtime_root)
            self._cleanup_abandoned_staging()
            current = self._pointer_store.load()
            candidate = self._runtime_root / version
            current_release = None
            if current is not None:
                current_root = self._runtime_root / current.current_version
                if not current_root.is_dir():
                    raise RuntimeInstallError("current_runtime_missing", "current Runtime directory is missing")
                current_release = self._verifier.verify_installed_manifest(
                    current_root,
                    expected_manifest_hash=current.manifest_hash,
                    expected_architecture=architecture,
                )
                if current_release.manifest.state_schema_version != current.state_schema_version:
                    raise RuntimeInstallError(
                        "pointer_release_mismatch",
                        "current pointer state schema differs from its signed Runtime",
                    )
            if current is not None and current.current_version == version and candidate.is_dir():
                if (
                    current.manifest_hash != new_manifest_hash
                    or current.state_schema_version != bundle.manifest.state_schema_version
                ):
                    raise RuntimeInstallError("pointer_release_mismatch", "current pointer differs from signed Runtime")
                if privilege_approval is not None:
                    raise RuntimeInstallError(
                        "privilege_approval_unexpected",
                        "unchanged Runtime cannot consume a privilege approval receipt",
                    )
                self._verifier.verify_installed_tree(candidate, bundle)
                report = self._self_test.run(candidate, timeout_seconds=60)
                self._references.replace_owner(version, owner_id, legacy_owner_id=legacy_owner_id)
                notify(RuntimeInstallPhase.READY)
                return InstalledRuntime(candidate, version, current, False, report)
            assessment = assess_privilege_change(
                None if current_release is None else current_release.manifest.privilege_envelope,
                new_privileges,
            )
            approval_to_consume: RuntimePrivilegeApprovalReceipt | None = None
            if assessment.automatic:
                if privilege_approval is not None:
                    raise RuntimeInstallError(
                        "privilege_approval_unexpected",
                        "same-or-narrower Runtime update cannot consume a privilege approval receipt",
                    )
            else:
                if privilege_approval is None:
                    raise RuntimePrivilegeApprovalRequired(
                        old_manifest_hash=None if current is None else current.manifest_hash,
                        new_manifest_hash=new_manifest_hash,
                        assessment=assessment,
                    )
                try:
                    validate_privilege_approval_receipt(
                        privilege_approval,
                        assessment=assessment,
                        old_manifest_hash=None if current is None else current.manifest_hash,
                        new_manifest_hash=new_manifest_hash,
                        now=self._clock(),
                        consumed_receipt_ids=(
                            ()
                            if current is None
                            else tuple(item.receipt_id for item in current.privilege_approval_journal)
                        ),
                    )
                except RuntimePrivilegeError as error:
                    raise RuntimeInstallError(error.code, str(error)) from error
                approval_to_consume = privilege_approval
            self._failures.assert_allowed(version)
            try:
                return self._install_locked(
                    bundle,
                    owner_id=owner_id,
                    legacy_owner_id=legacy_owner_id,
                    previous=current,
                    notify=notify,
                    privilege_approval=approval_to_consume,
                )
            except BaseException as error:
                self._failures.record(version, _safe_error_code(error))
                raise

    def rollback(self, *, progress: Callable[[RuntimeInstallPhase], None] | None = None) -> RuntimePointer:
        notify = progress or (lambda _: None)
        with self._lock_factory():
            self._assert_purge_not_in_progress()
            current = self._pointer_store.load()
            if current is None or current.previous_version is None:
                raise RuntimeInstallError("rollback_unavailable", "no previous runtime is available")
            previous_root = self._runtime_root / current.previous_version
            if not previous_root.is_dir():
                raise RuntimeInstallError("rollback_missing", "previous runtime directory is missing")
            notify(RuntimeInstallPhase.ROLLING_BACK)
            with self._quiescer.quiesce_for_update(timeout_seconds=45):
                current_backup = _SqliteBackupTransaction.create(
                    workspaces_root=self._workspaces_root,
                    backup_root=self._runtime_root / ".migration-backups" / uuid.uuid4().hex,
                )
                previous_snapshot = (
                    None
                    if current.rollback_snapshot is None
                    else _SqliteBackupTransaction.open_snapshot(
                        workspaces_root=self._workspaces_root,
                        backup_root=self._runtime_root / Path(current.rollback_snapshot),
                    )
                )
                try:
                    if previous_snapshot is not None:
                        previous_snapshot.restore()
                    rollback_snapshot = current_backup.preserve(
                        self._runtime_root
                        / ".rollback-snapshots"
                        / f"generation-{current.generation + 1}-{uuid.uuid4().hex}"
                    )
                    previous_manifest = parse_manifest((previous_root / "runtime-manifest.json").read_bytes())
                    pointer = RuntimePointer(
                        current_version=current.previous_version,
                        previous_version=current.current_version,
                        manifest_hash=_installed_manifest_hash(previous_root),
                        activated_at=self._clock(),
                        generation=current.generation + 1,
                        state_schema_version=previous_manifest.state_schema_version,
                        rollback_snapshot=rollback_snapshot,
                        privilege_approval_journal=current.privilege_approval_journal,
                    )
                    self._pointer_store.save(pointer)
                except BaseException:
                    current_backup.rollback()
                    raise
                else:
                    current_backup.retain()
                    if previous_snapshot is not None:
                        previous_snapshot.commit()
            return pointer

    def uninstall(
        self,
        *,
        owner_id: str,
        purge_data: bool = False,
        confirmation: str | None = None,
    ) -> tuple[str, ...]:
        if not _OWNER.fullmatch(owner_id):
            raise ValueError("runtime reference owner is invalid")
        if purge_data and confirmation != _PURGE_CONFIRMATION:
            raise RuntimeInstallError("purge_confirmation_required", "full data purge requires exact confirmation")
        with self._lock_factory():
            journal = self._purge_journal.load()
            if journal is not None:
                if not purge_data:
                    raise RuntimeInstallError(
                        "purge_in_progress",
                        "a full local-data purge must be recovered before preserve-data uninstall",
                    )
                if journal.owner_id != owner_id:
                    raise RuntimeInstallError(
                        "purge_owner_mismatch",
                        "the in-progress purge belongs to another Runtime owner",
                    )
            with self._quiescer.quiesce_for_update(timeout_seconds=45):
                if purge_data:
                    if journal is None:
                        referenced = self._references.referenced_versions(excluding_owner=owner_id)
                        if referenced:
                            raise RuntimeInstallError(
                                "runtime_still_referenced",
                                "other Vaults still reference Runtime",
                            )
                        removed_versions = tuple(sorted(child.name for child in self._managed_version_directories()))
                        _validate_runtime_root_for_purge(
                            self._runtime_root,
                            allowed_versions=set(removed_versions),
                        )
                        journal = _RuntimePurgeJournal(
                            operation_id=uuid.uuid4().hex,
                            owner_id=owner_id,
                            runtime_root_hash=self._purge_journal.runtime_root_hash,
                            workspaces_root_hash=self._purge_journal.workspaces_root_hash,
                            phase=RuntimePurgePhase.PREPARED,
                            removed_versions=removed_versions,
                        )
                        self._purge_journal.create(journal)
                        self._inject_purge_failure(RuntimePurgePhase.PREPARED)
                    return self._resume_purge_locked(journal)

                removed: list[str] = []
                referenced = self._references.referenced_versions(excluding_owner=owner_id)
                current = self._pointer_store.load()
                keep = set(referenced)
                if referenced:
                    if current is None:
                        raise RuntimeInstallError(
                            "referenced_runtime_pointer_missing",
                            "remaining Vault references require a valid global Runtime pointer",
                        )
                    # The global current Runtime is shared state, not ownership
                    # of the Vault being uninstalled.  Keeping it prevents an
                    # older remaining owner from forcing an unsafe downgrade or
                    # leaving the Host with no attach target.
                    keep.add(current.current_version)
                    architecture = native_windows_architecture()
                    for version in sorted(keep):
                        runtime = self._runtime_root / version
                        if not runtime.is_dir():
                            raise RuntimeInstallError(
                                "referenced_runtime_missing",
                                "a remaining Vault references a missing Runtime version",
                            )
                        expected_hash = (
                            current.manifest_hash
                            if version == current.current_version
                            else _installed_manifest_hash(runtime)
                        )
                        verified = self._verifier.verify_installed_manifest(
                            runtime,
                            expected_manifest_hash=expected_hash,
                            expected_architecture=architecture,
                        )
                        if (
                            version == current.current_version
                            and verified.manifest.state_schema_version != current.state_schema_version
                        ):
                            raise RuntimeInstallError(
                                "pointer_release_mismatch",
                                "current pointer state schema differs from its signed Runtime",
                            )

                self._references.release_owner(owner_id)
                if referenced:
                    assert current is not None
                    previous_is_retained = current.previous_version in keep
                    if not previous_is_retained and (
                        current.previous_version is not None or current.rollback_snapshot is not None
                    ):
                        # Commit the non-dangling pointer before deleting any
                        # unreferenced version.  A crash can leave harmless extra
                        # directories, never a pointer to a deleted Runtime.
                        current = replace(
                            current,
                            previous_version=None,
                            rollback_snapshot=None,
                            activated_at=self._clock(),
                            generation=current.generation + 1,
                        )
                        self._pointer_store.save(current)
                elif current is not None:
                    # With no remaining owners, remove the pointer first so an
                    # interrupted uninstall cannot advertise a deleted tree.
                    self._pointer_store.delete()

                for child in self._managed_version_directories():
                    if child.name in keep:
                        continue
                    _remove_managed_tree(self._runtime_root, child)
                    removed.append(child.name)
                return tuple(sorted(removed))

    def _assert_purge_not_in_progress(self) -> None:
        if self._purge_journal.load() is not None:
            raise RuntimeInstallError(
                "purge_in_progress",
                "Runtime installation or rollback is blocked until local-data purge recovery completes",
            )

    def _resume_purge_locked(self, journal: _RuntimePurgeJournal) -> tuple[str, ...]:
        if _PURGE_PHASE_INDEX[journal.phase] < _PURGE_PHASE_INDEX[RuntimePurgePhase.OWNER_RELEASED]:
            self._references.release_owner(journal.owner_id)
            self._inject_purge_failure(RuntimePurgePhase.OWNER_RELEASED)
            journal = self._purge_journal.advance(journal, RuntimePurgePhase.OWNER_RELEASED)

        if (
            _PURGE_PHASE_INDEX[journal.phase]
            < _PURGE_PHASE_INDEX[RuntimePurgePhase.RUNTIME_VERSIONS_AND_POINTER_REMOVED]
        ):
            unexpected_versions = {child.name for child in self._managed_version_directories()} - set(
                journal.removed_versions
            )
            if unexpected_versions:
                raise RuntimeInstallError(
                    "purge_scope_changed",
                    "managed Runtime versions changed after purge preparation",
                )
            for version in journal.removed_versions:
                _remove_managed_tree(self._runtime_root, self._runtime_root / version, missing_ok=True)
            self._pointer_store.delete()
            self._inject_purge_failure(RuntimePurgePhase.RUNTIME_VERSIONS_AND_POINTER_REMOVED)
            journal = self._purge_journal.advance(
                journal,
                RuntimePurgePhase.RUNTIME_VERSIONS_AND_POINTER_REMOVED,
            )

        if _PURGE_PHASE_INDEX[journal.phase] < _PURGE_PHASE_INDEX[RuntimePurgePhase.APPCONTAINER_CLEANED]:
            if self._purge is not None:
                self._purge.purge_non_vault_data()
            self._inject_purge_failure(RuntimePurgePhase.APPCONTAINER_CLEANED)
            journal = self._purge_journal.advance(journal, RuntimePurgePhase.APPCONTAINER_CLEANED)

        if _PURGE_PHASE_INDEX[journal.phase] < _PURGE_PHASE_INDEX[RuntimePurgePhase.WORKSPACES_REMOVED]:
            _remove_managed_tree(self._local_state_root, self._workspaces_root, missing_ok=True)
            self._inject_purge_failure(RuntimePurgePhase.WORKSPACES_REMOVED)
            journal = self._purge_journal.advance(journal, RuntimePurgePhase.WORKSPACES_REMOVED)

        if _PURGE_PHASE_INDEX[journal.phase] < _PURGE_PHASE_INDEX[RuntimePurgePhase.RUNTIME_REMOVED]:
            _validate_runtime_root_for_purge(self._runtime_root)
            _remove_managed_tree(self._local_state_root, self._runtime_root, missing_ok=True)
            self._inject_purge_failure(RuntimePurgePhase.RUNTIME_REMOVED)
            journal = self._purge_journal.advance(journal, RuntimePurgePhase.RUNTIME_REMOVED)

        if _PURGE_PHASE_INDEX[journal.phase] < _PURGE_PHASE_INDEX[RuntimePurgePhase.LOCAL_RESIDUE_REMOVED]:
            _remove_fixed_local_residue(self._local_state_root)
            self._inject_purge_failure(RuntimePurgePhase.LOCAL_RESIDUE_REMOVED)
            journal = self._purge_journal.advance(journal, RuntimePurgePhase.LOCAL_RESIDUE_REMOVED)

        if _PURGE_PHASE_INDEX[journal.phase] < _PURGE_PHASE_INDEX[RuntimePurgePhase.COMPLETE]:
            journal = self._purge_journal.advance(journal, RuntimePurgePhase.COMPLETE)
            self._inject_purge_failure(RuntimePurgePhase.COMPLETE)
        self._purge_journal.delete(journal)
        return journal.removed_versions

    def _inject_purge_failure(self, phase: RuntimePurgePhase) -> None:
        if self._purge_failure_injector is not None:
            self._purge_failure_injector(phase)

    def _install_locked(
        self,
        bundle: VerifiedRuntimeBundle,
        *,
        owner_id: str,
        legacy_owner_id: str | None,
        previous: RuntimePointer | None,
        notify: Callable[[RuntimeInstallPhase], None],
        privilege_approval: RuntimePrivilegeApprovalReceipt | None,
    ) -> InstalledRuntime:
        version = bundle.manifest.runtime_version
        candidate = self._runtime_root / version
        stage = self._runtime_root / f".staging-{version}-{uuid.uuid4().hex}"
        installed = False
        report: RuntimeSelfTestReport
        try:
            if candidate.exists():
                self._verifier.verify_installed_tree(candidate, bundle)
                report = self._self_test.run(candidate, timeout_seconds=60)
            else:
                notify(RuntimeInstallPhase.EXTRACTING_TO_STAGING)
                self._extractor.extract(bundle, stage)
                notify(RuntimeInstallPhase.VERIFYING_EACH_FILE)
                self._verifier.verify_installed_tree(stage, bundle)
                notify(RuntimeInstallPhase.RUNTIME_SELF_TEST)
                report = self._self_test.run(stage, timeout_seconds=60)
            notify(RuntimeInstallPhase.QUIESCING_RUNTIME)
            with self._quiescer.quiesce_for_update(timeout_seconds=45):
                notify(RuntimeInstallPhase.BACKING_UP_STATE)
                backups = _SqliteBackupTransaction.create(
                    workspaces_root=self._workspaces_root,
                    backup_root=self._runtime_root / ".migration-backups" / uuid.uuid4().hex,
                )
                old_pointer = previous
                try:
                    notify(RuntimeInstallPhase.MIGRATING_STATE)
                    self._migration.migrate(
                        stage if stage.exists() else candidate,
                        backups.state_databases,
                        bundle.manifest.state_schema_version,
                    )
                    if stage.exists():
                        notify(RuntimeInstallPhase.ATOMIC_ACTIVATE)
                        os.replace(stage, candidate)
                        installed = True
                    generation = (old_pointer.generation + 1) if old_pointer is not None else 1
                    rollback_snapshot = backups.preserve(
                        self._runtime_root / ".rollback-snapshots" / f"generation-{generation}-{uuid.uuid4().hex}"
                    )
                    activated_at = self._clock()
                    pointer = RuntimePointer(
                        current_version=version,
                        previous_version=old_pointer.current_version if old_pointer is not None else None,
                        manifest_hash=f"sha256:{hashlib.sha256(bundle.manifest_bytes).hexdigest()}",
                        activated_at=activated_at,
                        generation=generation,
                        state_schema_version=bundle.manifest.state_schema_version,
                        rollback_snapshot=rollback_snapshot,
                        privilege_approval_journal=_updated_privilege_approval_journal(
                            previous,
                            privilege_approval,
                            now=activated_at,
                        ),
                    )
                    self._pointer_store.save(pointer)
                    post_report = self._self_test.run(candidate, timeout_seconds=60)
                except BaseException:
                    notify(RuntimeInstallPhase.ROLLING_BACK)
                    backups.rollback()
                    if old_pointer is None:
                        self._pointer_store.delete()
                    else:
                        self._pointer_store.save(old_pointer)
                    raise
                else:
                    backups.retain()
                    report = post_report
            self._references.replace_owner(version, owner_id, legacy_owner_id=legacy_owner_id)
            self._failures.clear(version)
            self._prune_unreferenced_versions(keep={version, *([previous.current_version] if previous else [])})
            notify(RuntimeInstallPhase.READY)
            return InstalledRuntime(candidate, version, pointer, installed, report)
        finally:
            if stage.exists():
                _remove_managed_tree(self._runtime_root, stage)

    def _cleanup_abandoned_staging(self) -> None:
        for child in self._runtime_root.iterdir():
            if child.name.startswith(".staging-"):
                _remove_managed_tree(self._runtime_root, child)

    def _managed_version_directories(self) -> tuple[Path, ...]:
        if not self._runtime_root.exists():
            return ()
        return tuple(
            child
            for child in self._runtime_root.iterdir()
            if child.is_dir() and _VERSION.fullmatch(child.name) is not None
        )

    def _prune_unreferenced_versions(self, *, keep: set[str]) -> None:
        referenced = self._references.referenced_versions() | keep
        for child in self._managed_version_directories():
            if child.name not in referenced:
                _remove_managed_tree(self._runtime_root, child)


class _PurgeJournalStore:
    _EXPECTED_FIELDS: ClassVar[set[str]] = {
        "operationId",
        "ownerId",
        "phase",
        "journalHash",
        "removedVersions",
        "runtimeRootHash",
        "schemaVersion",
        "workspacesRootHash",
    }

    def __init__(self, path: Path, *, runtime_root_hash: str, workspaces_root_hash: str) -> None:
        self._path = path
        self.runtime_root_hash = runtime_root_hash
        self.workspaces_root_hash = workspaces_root_hash

    def load(self) -> _RuntimePurgeJournal | None:
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeInstallError("purge_journal_reparse", "purge journal cannot be a reparse point")
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeInstallError("purge_journal_invalid", "purge journal must be a regular file")
        try:
            payload = self._path.read_bytes()
        except OSError as error:
            raise RuntimeInstallError("purge_journal_unavailable", "purge journal could not be read") from error
        if not payload or len(payload) > 64 * 1024:
            raise RuntimeInstallError("purge_journal_invalid", "purge journal exceeds its strict bounds")
        try:
            raw = cast(object, json.loads(payload.decode("utf-8", errors="strict")))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeInstallError("purge_journal_invalid", "purge journal is malformed") from error
        if _canonical_json(raw) != payload:
            raise RuntimeInstallError("purge_journal_noncanonical", "purge journal is not canonical JSON")
        if not isinstance(raw, dict) or set(raw) != self._EXPECTED_FIELDS:
            raise RuntimeInstallError("purge_journal_invalid", "purge journal fields are invalid")
        if (
            not isinstance(raw["schemaVersion"], int)
            or isinstance(raw["schemaVersion"], bool)
            or raw["schemaVersion"] != 1
            or not isinstance(raw["operationId"], str)
            or not isinstance(raw["ownerId"], str)
            or not isinstance(raw["runtimeRootHash"], str)
            or not isinstance(raw["workspacesRootHash"], str)
            or not isinstance(raw["phase"], str)
            or not isinstance(raw["journalHash"], str)
            or _ROOT_HASH.fullmatch(raw["journalHash"]) is None
            or not isinstance(raw["removedVersions"], list)
            or any(not isinstance(version, str) for version in raw["removedVersions"])
        ):
            raise RuntimeInstallError("purge_journal_invalid", "purge journal values are invalid")
        try:
            phase = RuntimePurgePhase(raw["phase"])
        except ValueError as error:
            raise RuntimeInstallError("purge_journal_invalid", "purge journal phase is invalid") from error
        journal = _RuntimePurgeJournal(
            operation_id=raw["operationId"],
            owner_id=raw["ownerId"],
            runtime_root_hash=raw["runtimeRootHash"],
            workspaces_root_hash=raw["workspacesRootHash"],
            phase=phase,
            removed_versions=tuple(raw["removedVersions"]),
        )
        if (
            journal.runtime_root_hash != self.runtime_root_hash
            or journal.workspaces_root_hash != self.workspaces_root_hash
        ):
            raise RuntimeInstallError(
                "purge_journal_root_mismatch",
                "purge journal does not match the configured Runtime and workspace roots",
            )
        if _purge_journal_payload(journal) != payload:
            raise RuntimeInstallError("purge_journal_tampered", "purge journal integrity check failed")
        return journal

    def create(self, journal: _RuntimePurgeJournal) -> None:
        if self.load() is not None:
            raise RuntimeInstallError("purge_in_progress", "a local-data purge is already in progress")
        if (
            journal.runtime_root_hash != self.runtime_root_hash
            or journal.workspaces_root_hash != self.workspaces_root_hash
            or journal.phase is not RuntimePurgePhase.PREPARED
        ):
            raise RuntimeInstallError("purge_journal_invalid", "new purge journal identity is invalid")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _reject_managed_root(self._path.parent)
        _atomic_write(self._path, _purge_journal_payload(journal))

    def advance(
        self,
        current: _RuntimePurgeJournal,
        phase: RuntimePurgePhase,
    ) -> _RuntimePurgeJournal:
        if _PURGE_PHASE_INDEX[phase] != _PURGE_PHASE_INDEX[current.phase] + 1:
            raise RuntimeInstallError("purge_phase_invalid", "purge journal phase must advance monotonically")
        actual = self.load()
        if actual != current:
            raise RuntimeInstallError("purge_journal_changed", "purge journal changed during recovery")
        updated = replace(current, phase=phase)
        _atomic_write(self._path, _purge_journal_payload(updated))
        return updated

    def delete(self, current: _RuntimePurgeJournal) -> None:
        if current.phase is not RuntimePurgePhase.COMPLETE:
            raise RuntimeInstallError("purge_incomplete", "incomplete purge journal cannot be deleted")
        actual = self.load()
        if actual != current:
            raise RuntimeInstallError("purge_journal_changed", "purge journal changed before completion")
        try:
            self._path.unlink()
        except FileNotFoundError as error:
            raise RuntimeInstallError("purge_journal_changed", "purge journal disappeared before completion") from error


def _purge_journal_payload(journal: _RuntimePurgeJournal) -> bytes:
    value: dict[str, object] = {
        "operationId": journal.operation_id,
        "ownerId": journal.owner_id,
        "phase": journal.phase.value,
        "removedVersions": list(journal.removed_versions),
        "runtimeRootHash": journal.runtime_root_hash,
        "schemaVersion": 1,
        "workspacesRootHash": journal.workspaces_root_hash,
    }
    value["journalHash"] = f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"
    return _canonical_json(value)


class _PointerStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> RuntimePointer | None:
        raw = _load_json(self._path)
        if raw is None:
            return None
        legacy_expected = {
            "activatedAt",
            "currentVersion",
            "generation",
            "manifestHash",
            "previousVersion",
            "rollbackSnapshot",
            "stateSchemaVersion",
        }
        expected = legacy_expected | {"privilegeApprovalJournal"}
        if not isinstance(raw, dict) or frozenset(raw) not in {frozenset(legacy_expected), frozenset(expected)}:
            raise RuntimeInstallError("pointer_invalid", "runtime pointer fields are invalid")
        legacy = set(raw) == legacy_expected
        try:
            activated_at = datetime.fromisoformat(str(raw["activatedAt"]).replace("Z", "+00:00"))
            if not isinstance(raw["generation"], int) or isinstance(raw["generation"], bool):
                raise ValueError
            if not isinstance(raw["stateSchemaVersion"], int) or isinstance(raw["stateSchemaVersion"], bool):
                raise ValueError
            previous = raw["previousVersion"]
            if previous is not None and not isinstance(previous, str):
                raise ValueError
            journal_raw = [] if legacy else raw["privilegeApprovalJournal"]
            if not isinstance(journal_raw, list):
                raise ValueError
            journal: list[ConsumedPrivilegeApproval] = []
            for value in journal_raw:
                if not isinstance(value, dict) or set(value) != {"expiresAt", "receiptId"}:
                    raise ValueError
                if not isinstance(value["expiresAt"], str) or not isinstance(value["receiptId"], str):
                    raise ValueError
                journal.append(ConsumedPrivilegeApproval(value["receiptId"], value["expiresAt"]))
            pointer = RuntimePointer(
                current_version=str(raw["currentVersion"]),
                previous_version=previous,
                manifest_hash=str(raw["manifestHash"]),
                activated_at=activated_at.astimezone(timezone.utc),
                generation=raw["generation"],
                state_schema_version=raw["stateSchemaVersion"],
                rollback_snapshot=(None if raw["rollbackSnapshot"] is None else str(raw["rollbackSnapshot"])),
                privilege_approval_journal=tuple(journal),
            )
        except (TypeError, ValueError) as error:
            raise RuntimeInstallError("pointer_invalid", "runtime pointer is malformed") from error
        expected_payload = _legacy_pointer_payload(pointer) if legacy else _pointer_payload(pointer)
        if expected_payload != _canonical_json(raw):
            raise RuntimeInstallError("pointer_noncanonical", "runtime pointer is not canonical JSON")
        return pointer

    def save(self, pointer: RuntimePointer) -> None:
        _atomic_write(self._path, _pointer_payload(pointer))

    def delete(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass


def _pointer_payload(pointer: RuntimePointer) -> bytes:
    return _canonical_json(
        {
            "activatedAt": pointer.activated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "currentVersion": pointer.current_version,
            "generation": pointer.generation,
            "manifestHash": pointer.manifest_hash,
            "previousVersion": pointer.previous_version,
            "privilegeApprovalJournal": [
                {"expiresAt": item.expires_at, "receiptId": item.receipt_id}
                for item in pointer.privilege_approval_journal
            ],
            "rollbackSnapshot": pointer.rollback_snapshot,
            "stateSchemaVersion": pointer.state_schema_version,
        }
    )


def _legacy_pointer_payload(pointer: RuntimePointer) -> bytes:
    if pointer.privilege_approval_journal:
        raise RuntimeInstallError("pointer_invalid", "legacy Runtime pointer cannot contain privilege approvals")
    return _canonical_json(
        {
            "activatedAt": pointer.activated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "currentVersion": pointer.current_version,
            "generation": pointer.generation,
            "manifestHash": pointer.manifest_hash,
            "previousVersion": pointer.previous_version,
            "rollbackSnapshot": pointer.rollback_snapshot,
            "stateSchemaVersion": pointer.state_schema_version,
        }
    )


def _updated_privilege_approval_journal(
    previous: RuntimePointer | None,
    receipt: RuntimePrivilegeApprovalReceipt | None,
    *,
    now: datetime,
) -> tuple[ConsumedPrivilegeApproval, ...]:
    current = [] if previous is None else list(previous.privilege_approval_journal)
    threshold = now.astimezone(timezone.utc)
    current = [
        item
        for item in current
        if datetime.fromisoformat(item.expires_at.replace("Z", "+00:00")).astimezone(timezone.utc) >= threshold
    ]
    if receipt is not None:
        current.append(ConsumedPrivilegeApproval(receipt.receipt_id, receipt.expires_at))
    current.sort(key=lambda item: item.receipt_id)
    if len(current) > 128:
        raise RuntimeInstallError(
            "privilege_approval_journal_full",
            "too many unexpired Runtime privilege approval receipts are retained",
        )
    return tuple(current)


class _FailureCircuit:
    _WINDOW = timedelta(minutes=30)
    _BLOCK = timedelta(hours=1)
    _THRESHOLD = 3

    def __init__(self, path: Path, clock: Callable[[], datetime]) -> None:
        self._path = path
        self._clock = clock

    def assert_allowed(self, version: str) -> None:
        state = self._load()
        record = state.get(version)
        if record is None:
            return
        blocked_until = record.get("blockedUntil")
        if isinstance(blocked_until, str):
            when = datetime.fromisoformat(blocked_until.replace("Z", "+00:00"))
            if self._clock() < when:
                raise RuntimeInstallError("update_circuit_open", "runtime update is temporarily blocked after failures")

    def record(self, version: str, code: str) -> None:
        now = self._clock().astimezone(timezone.utc)
        state = self._load()
        record = state.get(version, {"attempts": []})
        attempts_raw = record.get("attempts", [])
        attempts = [
            datetime.fromisoformat(value.replace("Z", "+00:00")) for value in attempts_raw if isinstance(value, str)
        ]
        attempts = [value for value in attempts if now - value <= self._WINDOW]
        attempts.append(now)
        updated: dict[str, Any] = {
            "attempts": [value.isoformat().replace("+00:00", "Z") for value in attempts],
            "lastError": code,
        }
        if len(attempts) >= self._THRESHOLD:
            updated["blockedUntil"] = (now + self._BLOCK).isoformat().replace("+00:00", "Z")
        state[version] = updated
        _atomic_write(self._path, _canonical_json({"schemaVersion": 1, "versions": state}))

    def clear(self, version: str) -> None:
        state = self._load()
        if state.pop(version, None) is not None:
            _atomic_write(self._path, _canonical_json({"schemaVersion": 1, "versions": state}))

    def _load(self) -> dict[str, dict[str, Any]]:
        raw = _load_json(self._path)
        if raw is None:
            return {}
        if not isinstance(raw, dict) or set(raw) != {"schemaVersion", "versions"} or raw["schemaVersion"] != 1:
            raise RuntimeInstallError("failure_state_invalid", "runtime update failure state is invalid")
        versions = raw["versions"]
        if not isinstance(versions, dict) or any(
            not isinstance(version, str) or not isinstance(record, dict) for version, record in versions.items()
        ):
            raise RuntimeInstallError("failure_state_invalid", "runtime failure records are invalid")
        return {version: dict(record) for version, record in versions.items()}


class _ReferenceStore:
    _LEGACY_OWNER = re.compile(r"^obsidian-([1-9][0-9]{0,9})$")

    def __init__(self, path: Path, *, legacy_owner_is_active: Callable[[int], bool | None]) -> None:
        self._path = path
        self._legacy_owner_is_active = legacy_owner_is_active

    def replace_owner(self, version: str, owner: str, *, legacy_owner_id: str | None = None) -> None:
        references = self._load()
        for owners in references.values():
            owners.discard(owner)
            if legacy_owner_id is not None:
                owners.discard(legacy_owner_id)
        owners = references.setdefault(version, set())
        owners.add(owner)
        references = self._prune_proven_inactive_legacy_owners(references)
        self._save(references)

    def release_owner(self, owner: str) -> None:
        references = self._load()
        changed = False
        for version in tuple(references):
            if owner in references[version]:
                references[version].remove(owner)
                changed = True
            if not references[version]:
                references.pop(version)
        if changed or self._path.exists():
            self._save(references)

    def referenced_versions(self, *, excluding_owner: str | None = None) -> set[str]:
        excluded = set() if excluding_owner is None else {excluding_owner}
        return {version for version, owners in self._load().items() if owners - excluded}

    def _load(self) -> dict[str, set[str]]:
        raw = _load_json(self._path)
        if raw is None:
            return {}
        if (
            not isinstance(raw, dict)
            or set(raw) != {"references", "schemaVersion"}
            or not isinstance(raw["schemaVersion"], int)
            or isinstance(raw["schemaVersion"], bool)
            or raw["schemaVersion"] not in {1, 2}
        ):
            raise RuntimeInstallError("reference_state_invalid", "runtime references are invalid")
        references = raw["references"]
        if not isinstance(references, dict):
            raise RuntimeInstallError("reference_state_invalid", "runtime references are malformed")
        result: dict[str, set[str]] = {}
        for version, owners in references.items():
            if (
                not isinstance(version, str)
                or _VERSION.fullmatch(version) is None
                or not isinstance(owners, list)
                or any(not isinstance(owner, str) or _OWNER.fullmatch(owner) is None for owner in owners)
            ):
                raise RuntimeInstallError("reference_state_invalid", "runtime reference entry is malformed")
            result[version] = set(owners)
        return self._prune_proven_inactive_legacy_owners(result)

    def _prune_proven_inactive_legacy_owners(
        self,
        references: Mapping[str, set[str]],
    ) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for version, owners in references.items():
            retained: set[str] = set()
            for owner in owners:
                match = self._LEGACY_OWNER.fullmatch(owner)
                if match is None:
                    retained.add(owner)
                    continue
                # ``None`` means inspection was unavailable/ambiguous and is
                # therefore retained.  Only a definitive false result may
                # retire a legacy PID owner.
                if self._legacy_owner_is_active(int(match.group(1))) is not False:
                    retained.add(owner)
            if retained:
                result[version] = retained
        return result

    def _save(self, references: Mapping[str, set[str]]) -> None:
        _atomic_write(
            self._path,
            _canonical_json(
                {
                    "references": {version: sorted(owners) for version, owners in sorted(references.items()) if owners},
                    "schemaVersion": 2,
                }
            ),
        )


def _legacy_owner_is_active(process_id: int) -> bool | None:
    """Return false only when Windows proves a legacy Obsidian PID is stale."""

    try:
        state = windows_process_identity_state(process_id)
    except (OSError, ValueError):
        return None
    if state is WindowsProcessIdentityState.CURRENT_USER:
        return True
    if state in {WindowsProcessIdentityState.NOT_FOUND, WindowsProcessIdentityState.OTHER_USER}:
        return False
    return None


class _SqliteBackupTransaction:
    def __init__(self, backup_root: Path, pairs: Sequence[tuple[Path, Path]]) -> None:
        self._backup_root = backup_root
        self._pairs = tuple(pairs)
        self._closed = False

    @property
    def state_databases(self) -> tuple[Path, ...]:
        return tuple(database for database, _ in self._pairs)

    @classmethod
    def create(cls, *, workspaces_root: Path, backup_root: Path) -> _SqliteBackupTransaction:
        pairs: list[tuple[Path, Path]] = []
        if not workspaces_root.exists():
            return cls(backup_root, pairs)
        backup_root.mkdir(parents=True)
        _reject_managed_root(backup_root)
        for workspace in sorted(workspaces_root.iterdir(), key=lambda item: item.name):
            database = workspace / "state.sqlite"
            if not database.is_file():
                continue
            if _is_reparse(workspace) or _is_reparse(database):
                raise RuntimeInstallError("state_reparse", "workspace state cannot be symlinked")
            backup = backup_root / workspace.name / "state.sqlite"
            backup.parent.mkdir()
            _sqlite_backup(database, backup)
            pairs.append((database, backup))
        return cls(backup_root, pairs)

    @classmethod
    def open_snapshot(
        cls,
        *,
        workspaces_root: Path,
        backup_root: Path,
    ) -> _SqliteBackupTransaction:
        if not backup_root.is_dir():
            raise RuntimeInstallError("rollback_snapshot_missing", "rollback state snapshot is missing")
        pairs: list[tuple[Path, Path]] = []
        for workspace in sorted(backup_root.iterdir(), key=lambda item: item.name):
            backup = workspace / "state.sqlite"
            if backup.is_file():
                pairs.append((workspaces_root / workspace.name / "state.sqlite", backup))
        return cls(backup_root, pairs)

    def restore(self) -> None:
        if self._closed:
            raise RuntimeInstallError("rollback_snapshot_closed", "rollback state snapshot is closed")
        for database, backup in self._pairs:
            _sqlite_backup(backup, database)

    def preserve(self, destination: Path) -> str | None:
        if self._closed:
            raise RuntimeInstallError("state_backup_closed", "state backup is closed")
        if not self._pairs:
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise RuntimeInstallError("rollback_snapshot_exists", "rollback snapshot target already exists")
        os.replace(self._backup_root, destination)
        self._backup_root = destination
        self._pairs = tuple(
            (database, destination / backup.relative_to(backup.parents[1])) for database, backup in self._pairs
        )
        return destination.relative_to(destination.parents[1]).as_posix()

    def rollback(self) -> None:
        if self._closed:
            return
        self.restore()
        self._closed = True
        _remove_managed_tree(self._backup_root.parent, self._backup_root, missing_ok=True)

    def commit(self) -> None:
        if self._closed:
            return
        self._closed = True
        _remove_managed_tree(self._backup_root.parent, self._backup_root, missing_ok=True)

    def retain(self) -> None:
        self._closed = True


def _sqlite_backup(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.as_posix()}?mode=ro"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(source_uri, uri=True, timeout=5)) as source_connection:
            with closing(sqlite3.connect(destination, timeout=5)) as destination_connection:
                source_connection.backup(destination_connection, pages=256, sleep=0.01)
                destination_connection.commit()
                result = destination_connection.execute("PRAGMA integrity_check").fetchone()
                if result != ("ok",):
                    raise RuntimeInstallError("state_backup_corrupt", "SQLite backup failed integrity check")
    except sqlite3.Error as error:
        raise RuntimeInstallError("state_backup_failed", "SQLite Backup API failed") from error


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb", buffering=0) as stream:
            stream.write(payload)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _load_json(path: Path) -> object | None:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None
    if not payload or len(payload) > 4 * 1024 * 1024:
        raise RuntimeInstallError("state_file_invalid", "runtime metadata exceeds bounds")
    try:
        value = cast(object, json.loads(payload.decode("utf-8", errors="strict")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeInstallError("state_file_invalid", "runtime metadata is malformed") from error
    if _canonical_json(value) != payload:
        raise RuntimeInstallError("state_file_noncanonical", "runtime metadata is not canonical JSON")
    return value


def _installed_manifest_hash(root: Path) -> str:
    try:
        payload = (root / "runtime-manifest.json").read_bytes()
    except OSError as error:
        raise RuntimeInstallError("rollback_manifest_missing", "previous runtime manifest is unavailable") from error
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _root_identity_hash(root: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(str(root.resolve(strict=False))))
    payload = ("offeragent-local-root-v1\0" + normalized).encode("utf-8", errors="strict")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _validate_runtime_root_for_purge(runtime_root: Path, *, allowed_versions: set[str] | None = None) -> None:
    try:
        info = runtime_root.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise RuntimeInstallError("purge_runtime_root_invalid", "Runtime root is invalid during purge")
    known_files = {"current.json", "runtime-references.json", "update-failures.json"}
    known_directories = {".migration-backups", ".rollback-snapshots"}
    staging = re.compile(r"^\.staging-[0-9][0-9A-Za-z.+_-]{0,63}-[0-9a-f]{32}$")
    atomic_temporary = re.compile(
        r"^\.(?:current\.json|runtime-references\.json|update-failures\.json)\.[0-9a-f]{32}\.tmp$"
    )
    version_allowlist = set() if allowed_versions is None else allowed_versions
    for child in runtime_root.iterdir():
        child_info = child.lstat()
        if (
            stat.S_ISLNK(child_info.st_mode)
            or getattr(child_info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise RuntimeInstallError("delete_reparse", "managed Runtime root contains a reparse point")
        if child.name in known_files:
            if not stat.S_ISREG(child_info.st_mode):
                raise RuntimeInstallError("purge_runtime_entry_invalid", "Runtime metadata entry has the wrong type")
            continue
        if child.name in version_allowlist:
            if not stat.S_ISDIR(child_info.st_mode):
                raise RuntimeInstallError("purge_runtime_entry_invalid", "Runtime version has the wrong type")
            continue
        if child.name in known_directories or staging.fullmatch(child.name) is not None:
            if not stat.S_ISDIR(child_info.st_mode):
                raise RuntimeInstallError(
                    "purge_runtime_entry_invalid",
                    "Runtime metadata directory has the wrong type",
                )
            continue
        if atomic_temporary.fullmatch(child.name) is not None and stat.S_ISREG(child_info.st_mode):
            continue
        raise RuntimeInstallError(
            "purge_unknown_runtime_entry",
            "Runtime root contains an entry outside the destructive purge allowlist",
        )


def _remove_fixed_local_residue(local_root: Path) -> None:
    try:
        info = local_root.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise RuntimeInstallError("purge_local_root_invalid", "OfferAgent local state root is invalid")

    registry_temporary = re.compile(r"^\.workspace-registry\.json\.[0-9a-f]{32}\.tmp$")
    registry_files = [local_root / "workspace-registry.json", local_root / "workspace-registry.json.lock"]
    registry_files.extend(
        child for child in local_root.iterdir() if registry_temporary.fullmatch(child.name) is not None
    )
    for registry in registry_files:
        try:
            registry_info = registry.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(registry_info.st_mode)
            or getattr(registry_info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            or not stat.S_ISREG(registry_info.st_mode)
        ):
            raise RuntimeInstallError("purge_residue_invalid", "workspace registry residue is invalid")
        registry.unlink()

    # These are the complete, product-owned user-state roots outside the
    # per-Workspace directory.  In particular, user-installed Skills and the
    # DPAPI-backed secret store must not survive an explicitly confirmed full
    # purge.  Setup payloads/plugins and unknown siblings remain owned by their
    # respective uninstallers (or the user) and are deliberately not inferred.
    for name in ("host", "logs", "config", "secrets", "skills"):
        target = local_root / name
        try:
            target_info = target.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(target_info.st_mode)
            or getattr(target_info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            or not stat.S_ISDIR(target_info.st_mode)
        ):
            raise RuntimeInstallError("purge_residue_invalid", "fixed local residue is invalid")
        _remove_managed_tree(local_root, target)


def _remove_managed_tree(parent: Path, target: Path, *, missing_ok: bool = False) -> None:
    try:
        target_info = target.lstat()
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    parent = parent.resolve(strict=True)
    unresolved_target = target
    if (
        stat.S_ISLNK(target_info.st_mode)
        or getattr(target_info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise RuntimeInstallError("delete_reparse", "managed deletion target is a reparse point")
    target = unresolved_target.resolve(strict=False)
    if not _is_within(parent, target) or target == parent:
        raise RuntimeInstallError("delete_scope_escape", "managed deletion escaped its root")
    if target.is_file():
        target.unlink()
        return
    for directory, directories, files in os.walk(target, topdown=False, followlinks=False):
        current = Path(directory)
        for name in files:
            child = current / name
            _reject_delete_reparse(child)
            child.unlink()
        for name in directories:
            child = current / name
            _reject_delete_reparse(child)
            child.rmdir()
    target.rmdir()


def _reject_delete_reparse(path: Path) -> None:
    info = path.lstat()
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeInstallError("delete_reparse", "managed tree contains a reparse point")


def _reject_managed_root(path: Path) -> None:
    info = path.lstat()
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeInstallError("managed_root_reparse", "managed Runtime directory cannot be a reparse point")


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_within(parent: Path, child: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(parent), os.path.normcase(child))) == os.path.normcase(parent)
    except ValueError:
        return False


def _safe_error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code):
        return code
    return "install_failed"


def _minimal_environment() -> dict[str, str]:
    allowed = {"LOCALAPPDATA", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def _windows_build() -> int:
    getter = getattr(sys, "getwindowsversion", None)
    if getter is None:
        raise RuntimeInstallError("windows_required", "Runtime installation is only supported on Windows")
    return int(getter().build)


__all__ = [
    "ConsumedPrivilegeApproval",
    "InstalledRuntime",
    "RuntimeInstallError",
    "RuntimeInstallPhase",
    "RuntimeInstaller",
    "RuntimePointer",
    "RuntimePrivilegeApprovalRequired",
    "RuntimePurge",
    "RuntimePurgePhase",
    "RuntimeQuiescer",
    "RuntimeSelfTestReport",
    "RuntimeSelfTestRunner",
    "StateMigrationRunner",
    "SubprocessRuntimeSelfTestRunner",
]
