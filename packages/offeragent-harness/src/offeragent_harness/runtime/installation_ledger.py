"""SID-protected multi-Vault installation ledger and uninstall coordinator.

Inno Setup owns one global AppId, so its uninstall log cannot represent plugin
copies installed into several Vaults.  This ledger records each exact plugin
installation independently.  A record may remain pending until Obsidian first
creates a portable workspace identity; only then is it bound to a Runtime
owner.  Uninstall never re-reads a Vault identity to guess an owner.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

from offeragent_harness.workspace.identity import CanonicalRootIdentity, identify_workspace_root
from offeragent_harness.workspace.portable_config import PortableWorkspaceConfigError, read_portable_workspace_config

from .windows_secure_tree import WindowsSecureTreeError, identify_windows_file, remove_windows_tree
from .windows_security import current_windows_identity, protect_current_user_path

_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_INSTALLATION_ID = re.compile(r"^install_[0-9a-f]{32}$")
_OPERATION_ID = re.compile(r"^[0-9a-f]{64}$")
_OWNER = re.compile(r"^workspace:ws_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_REQUEST_ID = _OPERATION_ID
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
_PURGE_CONFIRMATION = "DELETE OFFERAGENT LOCAL DATA"
_MAX_LEDGER_BYTES = 1024 * 1024
_MAX_JOURNAL_BYTES = 512 * 1024
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_ENTRIES = 1024
_MAX_RECEIPTS = 64
_PLUGIN_DIRECTORY = Path(".obsidian") / "plugins" / "offeragent-obsidian-plugin"


class InstallationLedgerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class LedgerRegistrationSource(str, Enum):
    SETUP = "setup"


class LedgerUninstallScope(str, Enum):
    SELECTED = "selected"
    ALL = "all"


class LedgerUninstallMode(str, Enum):
    PRESERVE_DATA = "preserve_data"
    PURGE_DATA = "purge_data"


class RuntimeOwnerUninstaller(Protocol):
    def __call__(
        self,
        owner_id: str,
        *,
        purge_data: bool,
        confirmation: str | None,
    ) -> tuple[str, ...]: ...


class LedgerLock(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


@dataclass(frozen=True, slots=True)
class PluginDirectoryIdentity:
    volume_id: str
    filesystem_id: str
    identity_hash: str

    def __post_init__(self) -> None:
        if not self.volume_id or not self.filesystem_id or _HASH.fullmatch(self.identity_hash) is None:
            raise InstallationLedgerError("plugin_identity_invalid", "plugin directory identity is invalid")
        if self.identity_hash != _plugin_identity_hash(self.volume_id, self.filesystem_id):
            raise InstallationLedgerError("plugin_identity_invalid", "plugin directory identity hash differs")


@dataclass(frozen=True, slots=True)
class InstallationLedgerEntry:
    installation_id: str
    owner_id: str | None
    root_identity: CanonicalRootIdentity
    plugin_identity: PluginDirectoryIdentity
    plugin_versions: tuple[str, ...]
    registered_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if _INSTALLATION_ID.fullmatch(self.installation_id) is None:
            raise InstallationLedgerError("ledger_entry_invalid", "installation identity is invalid")
        if self.owner_id is not None and _OWNER.fullmatch(self.owner_id) is None:
            raise InstallationLedgerError("ledger_entry_invalid", "Runtime owner identity is invalid")
        _validate_root_identity(self.root_identity)
        if (
            not self.plugin_versions
            or len(self.plugin_versions) > 64
            or self.plugin_versions != tuple(sorted(set(self.plugin_versions)))
            or any(_VERSION.fullmatch(version) is None for version in self.plugin_versions)
        ):
            raise InstallationLedgerError("ledger_entry_invalid", "plugin version history is invalid")
        _parse_timestamp(self.registered_at)
        _parse_timestamp(self.updated_at)


@dataclass(frozen=True, slots=True)
class UninstallTarget:
    entry: InstallationLedgerEntry


@dataclass(frozen=True, slots=True)
class InstallationUninstallReceipt:
    operation_id: str
    scope: LedgerUninstallScope
    mode: LedgerUninstallMode
    installation_ids: tuple[str, ...]
    removed_versions: tuple[str, ...]
    completed_at: str

    def __post_init__(self) -> None:
        _validate_operation_values(self.operation_id, self.installation_ids, self.removed_versions)
        _parse_timestamp(self.completed_at)


@dataclass(frozen=True, slots=True)
class InstallationLedgerState:
    generation: int
    entries: tuple[InstallationLedgerEntry, ...]
    receipts: tuple[InstallationUninstallReceipt, ...] = ()

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise InstallationLedgerError("ledger_invalid", "ledger generation is invalid")
        installation_ids = tuple(item.installation_id for item in self.entries)
        root_ids = tuple(item.root_identity.identity_hash for item in self.entries)
        root_paths = tuple(item.root_identity.canonical_path for item in self.entries)
        owners = tuple(item.owner_id for item in self.entries if item.owner_id is not None)
        if (
            len(self.entries) > _MAX_ENTRIES
            or installation_ids != tuple(sorted(installation_ids))
            or len(set(installation_ids)) != len(installation_ids)
            or len(set(root_ids)) != len(root_ids)
            or len(set(root_paths)) != len(root_paths)
            or len(set(owners)) != len(owners)
        ):
            raise InstallationLedgerError("ledger_invalid", "ledger registrations are ambiguous")
        operation_ids = tuple(item.operation_id for item in self.receipts)
        if len(self.receipts) > _MAX_RECEIPTS or len(set(operation_ids)) != len(operation_ids):
            raise InstallationLedgerError("ledger_invalid", "ledger receipts are invalid")


@dataclass(frozen=True, slots=True)
class LedgerUninstallResult:
    operation_id: str
    installation_ids: tuple[str, ...]
    removed_versions: tuple[str, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class _UninstallJournal:
    operation_id: str
    ledger_path_hash: str
    scope: LedgerUninstallScope
    mode: LedgerUninstallMode
    targets: tuple[UninstallTarget, ...]
    processed_installation_ids: tuple[str, ...]
    removed_versions: tuple[str, ...]

    def __post_init__(self) -> None:
        ids = tuple(item.entry.installation_id for item in self.targets)
        _validate_operation_values(self.operation_id, ids, self.removed_versions)
        if _HASH.fullmatch(self.ledger_path_hash) is None:
            raise InstallationLedgerError("uninstall_journal_invalid", "journal path binding is invalid")
        if (
            len(set(self.processed_installation_ids)) != len(self.processed_installation_ids)
            or any(item not in ids for item in self.processed_installation_ids)
            or self.processed_installation_ids
            != tuple(item for item in ids if item in set(self.processed_installation_ids))
        ):
            raise InstallationLedgerError("uninstall_journal_invalid", "journal progress is invalid")


class ManagedPluginRemover:
    """Delete only the recorded fixed plugin directory after identity proof."""

    def __init__(
        self,
        *,
        race_barrier: Callable[[Path, tuple[str, ...]], None] | None = None,
    ) -> None:
        self._race_barrier = race_barrier

    def remove(self, entry: InstallationLedgerEntry) -> None:
        root = Path(entry.root_identity.canonical_path)
        try:
            root_info = root.lstat()
        except FileNotFoundError:
            # A moved/deleted Vault cannot be searched for safely.  Runtime
            # owner release still uses the durable ledger binding.
            return
        except OSError as error:
            raise InstallationLedgerError("vault_root_ambiguous", "recorded Vault root cannot be proven") from error
        if not _safe_directory(root, root_info):
            raise InstallationLedgerError("vault_root_replaced", "recorded Vault root is no longer a safe directory")
        observed_root = identify_workspace_root(root)
        if observed_root != entry.root_identity:
            raise InstallationLedgerError("vault_root_replaced", "recorded Vault root identity changed")

        plugin = root / _PLUGIN_DIRECTORY
        tombstone = plugin.with_name(f".offeragent-uninstall-{entry.installation_id}.pending")
        plugin_exists = _exists_without_ambiguity(plugin)
        tombstone_exists = _exists_without_ambiguity(tombstone)
        if plugin_exists and tombstone_exists:
            raise InstallationLedgerError("plugin_uninstall_ambiguous", "plugin and uninstall tombstone both exist")
        target = tombstone if tombstone_exists else plugin
        if not plugin_exists and not tombstone_exists:
            return
        _assert_plugin_identity(target, entry.plugin_identity)
        if target == plugin:
            try:
                os.rename(plugin, tombstone)
            except OSError as error:
                raise InstallationLedgerError(
                    "plugin_quarantine_failed", "plugin directory could not be quarantined"
                ) from error
            target = tombstone
            _assert_plugin_identity(target, entry.plugin_identity)
        _remove_verified_tree(
            target,
            entry.plugin_identity,
            race_barrier=self._race_barrier,
        )


class InstallationLedgerCoordinator:
    """Register installations and recover exact multi-owner uninstall steps."""

    def __init__(
        self,
        *,
        ledger_path: Path,
        runtime_uninstall: RuntimeOwnerUninstaller,
        lock_factory: Callable[[], LedgerLock],
        plugin_remover: ManagedPluginRemover | None = None,
        clock: Callable[[], datetime] | None = None,
        new_uuid: Callable[[], uuid.UUID] | None = None,
        acl_protector: Callable[[Path, bool], None] | None = None,
        acl_verifier: Callable[[Path, bool], bool] | None = None,
        failure_injector: Callable[[str, str | None], None] | None = None,
    ) -> None:
        self._ledger_path = ledger_path.resolve(strict=False)
        if self._ledger_path.name != "vault-installations.json" or self._ledger_path.parent.name != "installer":
            raise ValueError("installation ledger must use the fixed OfferAgent installer layout")
        self._request_root = self._ledger_path.parent / "requests"
        self._selection_path = self._ledger_path.parent / "selected-installation.txt"
        self._journal_path = self._ledger_path.parent / "uninstall-journal.json"
        self._runtime_uninstall = runtime_uninstall
        self._lock_factory = lock_factory
        self._plugin_remover = plugin_remover or ManagedPluginRemover()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._new_uuid = new_uuid or uuid.uuid4
        self._acl_protector = acl_protector or _default_acl_protector
        self._acl_verifier = acl_verifier or _default_acl_verifier
        self._failure_injector = failure_injector
        self._ledger_path_hash = _path_identity_hash(self._ledger_path)

    @property
    def selection_path(self) -> Path:
        return self._selection_path

    def prepare_registration_request(self, request_id: str) -> Path:
        """Create an empty, fixed-name, SID-only request file for Inno."""

        _require_request_id(request_id)
        with self._lock_factory():
            self._prepare_directory(self._request_root)
            target = self._request_path(request_id)
            if target.exists():
                raise InstallationLedgerError("registration_request_exists", "registration request already exists")
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            os.close(descriptor)
            self._protect_and_verify(target, directory=False)
            return target

    def consume_registration_request(self, request_id: str) -> InstallationLedgerEntry:
        """Validate, consume, and always erase one Inno registration request."""

        _require_request_id(request_id)
        target = self._request_path(request_id)
        try:
            value = self._read_request(target)
            if _text(value["source"]) != LedgerRegistrationSource.SETUP.value:
                raise InstallationLedgerError("request_source_invalid", "installer registration source is invalid")
            return self.register_vault(
                Path(_text(value["vaultRoot"])),
                plugin_version=_text(value["pluginVersion"]),
            )
        finally:
            try:
                target.unlink()
            except FileNotFoundError:
                pass

    def register_vault(
        self,
        vault_root: Path,
        *,
        plugin_version: str,
    ) -> InstallationLedgerEntry:
        """Record Setup output; missing workspace identity becomes pending."""

        if _VERSION.fullmatch(plugin_version) is None:
            raise InstallationLedgerError("plugin_version_invalid", "installer plugin version is invalid")
        root = _verified_vault_root(vault_root)
        plugin_identity = _identify_plugin_directory(root / _PLUGIN_DIRECTORY)
        owner_id = _read_optional_owner(root)
        return self._upsert_registration(
            root,
            plugin_identity=plugin_identity,
            plugin_version=plugin_version,
            owner_id=owner_id,
        )

    def bind_runtime_owner(
        self,
        vault_root: Path,
        *,
        owner_id: str,
        plugin_version: str,
    ) -> InstallationLedgerEntry:
        """Atomically bind a pending install during the first real bootstrap."""

        if _OWNER.fullmatch(owner_id) is None or _VERSION.fullmatch(plugin_version) is None:
            raise InstallationLedgerError("owner_binding_invalid", "Runtime owner binding is invalid")
        root = _verified_vault_root(vault_root)
        try:
            config = read_portable_workspace_config(root)
        except (FileNotFoundError, PortableWorkspaceConfigError) as error:
            raise InstallationLedgerError(
                "owner_binding_unproven", "Runtime owner cannot be proven from the Vault"
            ) from error
        if owner_id != f"workspace:{config.portable_workspace_id}":
            raise InstallationLedgerError("owner_binding_mismatch", "Runtime owner differs from the Vault identity")
        return self._upsert_registration(
            root,
            plugin_identity=_identify_plugin_directory(root / _PLUGIN_DIRECTORY),
            plugin_version=plugin_version,
            owner_id=owner_id,
        )

    def uninstall(
        self,
        *,
        operation_id: str,
        scope: LedgerUninstallScope,
        mode: LedgerUninstallMode,
        selected_installation_id: str | None,
        confirmation: str | None,
    ) -> LedgerUninstallResult:
        _require_operation_request(operation_id, scope, mode, selected_installation_id, confirmation)
        with self._lock_factory():
            state = self._load_ledger(required=True)
            assert state is not None
            receipt = next((item for item in state.receipts if item.operation_id == operation_id), None)
            if receipt is not None:
                self._validate_receipt(receipt, scope, mode, selected_installation_id)
                completed_journal = self._load_journal()
                if completed_journal is not None:
                    self._validate_journal(
                        completed_journal,
                        operation_id,
                        scope,
                        mode,
                        selected_installation_id,
                    )
                    target_ids = tuple(target.entry.installation_id for target in completed_journal.targets)
                    if (
                        completed_journal.processed_installation_ids != target_ids
                        or completed_journal.removed_versions != receipt.removed_versions
                        or receipt.installation_ids != target_ids
                    ):
                        raise InstallationLedgerError(
                            "completed_journal_mismatch",
                            "completed uninstall journal differs from its durable receipt",
                        )
                    self._delete_journal(completed_journal)
                return LedgerUninstallResult(operation_id, receipt.installation_ids, receipt.removed_versions, True)

            journal = self._load_journal()
            if journal is None:
                entries = {item.installation_id: item for item in state.entries}
                targets: tuple[UninstallTarget, ...]
                if scope is LedgerUninstallScope.SELECTED:
                    assert selected_installation_id is not None
                    selected = entries.get(selected_installation_id)
                    if selected is None:
                        raise InstallationLedgerError(
                            "selected_installation_unknown",
                            "explicitly selected plugin installation is not registered",
                        )
                    targets = (UninstallTarget(selected),)
                else:
                    targets = tuple(UninstallTarget(item) for item in state.entries)
                    if mode is LedgerUninstallMode.PURGE_DATA and not targets:
                        raise InstallationLedgerError(
                            "global_purge_unproven",
                            "global purge requires at least one installer-owned registration",
                        )
                    if mode is LedgerUninstallMode.PURGE_DATA and not any(
                        target.entry.owner_id is not None for target in targets
                    ):
                        raise InstallationLedgerError(
                            "pending_only_purge_forbidden",
                            "pending-only installations have no proven Runtime owner; use preserve-data uninstall",
                        )
                journal = _UninstallJournal(
                    operation_id=operation_id,
                    ledger_path_hash=self._ledger_path_hash,
                    scope=scope,
                    mode=mode,
                    targets=targets,
                    processed_installation_ids=(),
                    removed_versions=(),
                )
                self._save_journal(journal, create=True)
                self._inject("prepared", None)
            else:
                self._validate_journal(journal, operation_id, scope, mode, selected_installation_id)

            if (
                mode is LedgerUninstallMode.PURGE_DATA
                and journal.targets
                and not any(target.entry.owner_id is not None for target in journal.targets)
            ):
                raise InstallationLedgerError(
                    "pending_only_purge_forbidden",
                    "pending-only installations have no proven Runtime owner; use preserve-data uninstall",
                )

            state = self._reconcile_processed(state, journal)
            self._sync_selection(state)
            bound_targets = tuple(target for target in journal.targets if target.entry.owner_id is not None)
            purge_owner = bound_targets[-1].entry.owner_id if bound_targets else None

            processed = set(journal.processed_installation_ids)
            for target in journal.targets:
                entry = target.entry
                if entry.installation_id in processed:
                    continue
                current = {item.installation_id: item for item in state.entries}.get(entry.installation_id)
                if current != entry:
                    raise InstallationLedgerError(
                        "uninstall_target_changed",
                        "registered installation changed after uninstall preparation",
                    )
                # Quarantine and delete the exact recorded plugin before
                # releasing its Runtime owner.  If the Vault/plugin directory
                # was replaced, fail closed without weakening shared Runtime
                # ownership.  A crash after deletion is recoverable because a
                # missing exact path is an idempotent success on retry.
                self._plugin_remover.remove(entry)
                if entry.owner_id is not None:
                    global_purge = mode is LedgerUninstallMode.PURGE_DATA and entry.owner_id == purge_owner
                    removed = self._runtime_uninstall(
                        entry.owner_id,
                        purge_data=global_purge,
                        confirmation=_PURGE_CONFIRMATION if global_purge else None,
                    )
                else:
                    removed = ()
                if any(_VERSION.fullmatch(version) is None for version in removed):
                    raise InstallationLedgerError(
                        "runtime_uninstall_result_invalid", "Runtime uninstall result is invalid"
                    )
                self._inject("entry_effects_applied", entry.installation_id)
                journal = replace(
                    journal,
                    processed_installation_ids=(*journal.processed_installation_ids, entry.installation_id),
                    removed_versions=tuple(sorted({*journal.removed_versions, *removed})),
                )
                self._save_journal(journal, create=False)
                self._inject("entry_journaled", entry.installation_id)
                state = self._remove_entry(state, entry.installation_id)
                self._inject("entry_removed", entry.installation_id)
                processed.add(entry.installation_id)

            receipt = InstallationUninstallReceipt(
                operation_id=operation_id,
                scope=scope,
                mode=mode,
                installation_ids=tuple(item.entry.installation_id for item in journal.targets),
                removed_versions=journal.removed_versions,
                completed_at=_timestamp(self._clock()),
            )
            state = replace(
                state,
                generation=state.generation + 1,
                receipts=(*state.receipts, receipt)[-_MAX_RECEIPTS:],
            )
            self._save_ledger(state)
            self._inject("completed", None)
            self._delete_journal(journal)
            return LedgerUninstallResult(operation_id, receipt.installation_ids, receipt.removed_versions, False)

    def validate_uninstall_request(
        self,
        *,
        scope: LedgerUninstallScope,
        mode: LedgerUninstallMode,
        selected_installation_id: str | None,
        confirmation: str | None,
    ) -> None:
        """Fail closed before Inno persists a new operation choice."""

        _require_operation_request(
            "0" * 64,
            scope,
            mode,
            selected_installation_id,
            confirmation,
        )
        with self._lock_factory():
            if self._load_journal() is not None:
                raise InstallationLedgerError(
                    "uninstall_in_progress",
                    "an existing uninstall must recover before a new choice",
                )
            state = self._load_ledger(required=True)
            assert state is not None
            if scope is LedgerUninstallScope.SELECTED:
                assert selected_installation_id is not None
                if not any(entry.installation_id == selected_installation_id for entry in state.entries):
                    raise InstallationLedgerError(
                        "selected_installation_unknown",
                        "explicitly selected plugin installation is not registered",
                    )
            elif mode is LedgerUninstallMode.PURGE_DATA:
                if not state.entries:
                    raise InstallationLedgerError(
                        "global_purge_unproven",
                        "global purge requires at least one installer-owned registration",
                    )
                if not any(entry.owner_id is not None for entry in state.entries):
                    raise InstallationLedgerError(
                        "pending_only_purge_forbidden",
                        "pending-only installations have no proven Runtime owner; use preserve-data uninstall",
                    )

    def snapshot(self) -> InstallationLedgerState:
        with self._lock_factory():
            state = self._load_ledger(required=True)
            assert state is not None
            return state

    def _upsert_registration(
        self,
        root: Path,
        *,
        plugin_identity: PluginDirectoryIdentity,
        plugin_version: str,
        owner_id: str | None,
    ) -> InstallationLedgerEntry:
        root_identity = identify_workspace_root(root)
        timestamp = _timestamp(self._clock())
        with self._lock_factory():
            if self._load_journal() is not None:
                raise InstallationLedgerError(
                    "uninstall_in_progress",
                    "installation registration must wait for exact uninstall recovery",
                )
            state = self._load_ledger(required=False)
            entries = [] if state is None else list(state.entries)
            exact = next(
                (item for item in entries if item.root_identity.identity_hash == root_identity.identity_hash),
                None,
            )
            same_path = next(
                (item for item in entries if item.root_identity.canonical_path == root_identity.canonical_path),
                None,
            )
            if same_path is not None and same_path is not exact:
                raise InstallationLedgerError(
                    "root_identity_replaced",
                    "the recorded Vault path now identifies a different filesystem object",
                )
            same_owner = (
                None if owner_id is None else next((item for item in entries if item.owner_id == owner_id), None)
            )
            if exact is not None and owner_id is not None and exact.owner_id not in {None, owner_id}:
                raise InstallationLedgerError("root_owner_conflict", "Vault root is bound to another Runtime owner")
            if same_owner is not None and same_owner is not exact:
                if not _recorded_root_is_absent(same_owner.root_identity.canonical_path):
                    raise InstallationLedgerError(
                        "portable_owner_conflict",
                        "Runtime owner is concurrently installed at another existing Vault root",
                    )
                entries.remove(same_owner)
                exact = same_owner
            previous = exact
            if previous is not None and previous in entries:
                entries.remove(previous)
            installation_id = previous.installation_id if previous is not None else f"install_{self._new_uuid().hex}"
            bound_owner = owner_id if owner_id is not None else (None if previous is None else previous.owner_id)
            versions = tuple(sorted({plugin_version, *(previous.plugin_versions if previous is not None else ())}))
            entry = InstallationLedgerEntry(
                installation_id=installation_id,
                owner_id=bound_owner,
                root_identity=root_identity,
                plugin_identity=plugin_identity,
                plugin_versions=versions,
                registered_at=timestamp if previous is None else previous.registered_at,
                updated_at=timestamp,
            )
            entries.append(entry)
            entries.sort(key=lambda item: item.installation_id)
            updated = InstallationLedgerState(
                generation=1 if state is None else state.generation + 1,
                entries=tuple(entries),
                receipts=() if state is None else state.receipts,
            )
            self._save_ledger(updated)
            self._write_selection(entry.installation_id)
            return entry

    def _reconcile_processed(
        self,
        state: InstallationLedgerState,
        journal: _UninstallJournal,
    ) -> InstallationLedgerState:
        for installation_id in journal.processed_installation_ids:
            if installation_id in {item.installation_id for item in state.entries}:
                state = self._remove_entry(state, installation_id)
        return state

    def _remove_entry(self, state: InstallationLedgerState, installation_id: str) -> InstallationLedgerState:
        entries = tuple(item for item in state.entries if item.installation_id != installation_id)
        if len(entries) == len(state.entries):
            raise InstallationLedgerError("uninstall_target_missing", "uninstall target could not be removed")
        updated = replace(state, generation=state.generation + 1, entries=entries)
        self._save_ledger(updated)
        self._sync_selection(updated)
        return updated

    def _validate_receipt(
        self,
        receipt: InstallationUninstallReceipt,
        scope: LedgerUninstallScope,
        mode: LedgerUninstallMode,
        selected: str | None,
    ) -> None:
        expected = (cast(str, selected),) if scope is LedgerUninstallScope.SELECTED else receipt.installation_ids
        if receipt.scope is not scope or receipt.mode is not mode or receipt.installation_ids != expected:
            raise InstallationLedgerError("operation_id_reused", "operation identity belongs to another uninstall")

    def _validate_journal(
        self,
        journal: _UninstallJournal,
        operation_id: str,
        scope: LedgerUninstallScope,
        mode: LedgerUninstallMode,
        selected: str | None,
    ) -> None:
        target_selected = (
            None if journal.scope is LedgerUninstallScope.ALL else journal.targets[0].entry.installation_id
        )
        if (
            journal.operation_id != operation_id
            or journal.scope is not scope
            or journal.mode is not mode
            or target_selected != selected
        ):
            raise InstallationLedgerError("uninstall_in_progress", "another exact uninstall must recover first")

    def _load_ledger(self, *, required: bool) -> InstallationLedgerState | None:
        raw = self._load_document(
            self._ledger_path,
            maximum=_MAX_LEDGER_BYTES,
            hash_field="ledgerHash",
            missing_code="ledger_missing" if required else None,
        )
        if raw is None:
            return None
        if (
            set(raw) != {"entries", "generation", "ledgerHash", "receipts", "schemaVersion"}
            or raw["schemaVersion"] != 2
        ):
            raise InstallationLedgerError("ledger_invalid", "installation ledger fields are invalid")
        if not isinstance(raw["generation"], int) or isinstance(raw["generation"], bool):
            raise InstallationLedgerError("ledger_invalid", "installation ledger generation is invalid")
        if not isinstance(raw["entries"], list) or not isinstance(raw["receipts"], list):
            raise InstallationLedgerError("ledger_invalid", "installation ledger collections are invalid")
        try:
            return InstallationLedgerState(
                generation=raw["generation"],
                entries=tuple(_parse_entry(item) for item in raw["entries"]),
                receipts=tuple(_parse_receipt(item) for item in raw["receipts"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise InstallationLedgerError("ledger_invalid", "installation ledger is malformed") from error

    def _save_ledger(self, state: InstallationLedgerState) -> None:
        self._save_document(
            self._ledger_path,
            {
                "entries": [_entry_json(item) for item in state.entries],
                "generation": state.generation,
                "receipts": [_receipt_json(item) for item in state.receipts],
                "schemaVersion": 2,
            },
            "ledgerHash",
        )

    def _load_journal(self) -> _UninstallJournal | None:
        raw = self._load_document(
            self._journal_path,
            maximum=_MAX_JOURNAL_BYTES,
            hash_field="journalHash",
            missing_code=None,
        )
        if raw is None:
            return None
        expected = {
            "journalHash",
            "ledgerPathHash",
            "mode",
            "operationId",
            "processedInstallationIds",
            "removedVersions",
            "schemaVersion",
            "scope",
            "targets",
        }
        if set(raw) != expected or raw["schemaVersion"] != 1 or not isinstance(raw["targets"], list):
            raise InstallationLedgerError("uninstall_journal_invalid", "uninstall journal fields are invalid")
        try:
            journal = _UninstallJournal(
                operation_id=_text(raw["operationId"]),
                ledger_path_hash=_text(raw["ledgerPathHash"]),
                scope=LedgerUninstallScope(_text(raw["scope"])),
                mode=LedgerUninstallMode(_text(raw["mode"])),
                targets=tuple(UninstallTarget(_parse_entry(item)) for item in raw["targets"]),
                processed_installation_ids=_text_tuple(raw["processedInstallationIds"]),
                removed_versions=_text_tuple(raw["removedVersions"]),
            )
        except (TypeError, ValueError) as error:
            raise InstallationLedgerError("uninstall_journal_invalid", "uninstall journal is malformed") from error
        if journal.ledger_path_hash != self._ledger_path_hash:
            raise InstallationLedgerError(
                "uninstall_journal_path_mismatch",
                "uninstall journal belongs to another installer ledger path",
            )
        return journal

    def _save_journal(self, journal: _UninstallJournal, *, create: bool) -> None:
        existing = self._load_journal()
        if create and existing is not None:
            raise InstallationLedgerError("uninstall_in_progress", "an uninstall journal already exists")
        if not create and existing is None:
            raise InstallationLedgerError("uninstall_journal_missing", "uninstall journal disappeared")
        self._save_document(
            self._journal_path,
            {
                "ledgerPathHash": journal.ledger_path_hash,
                "mode": journal.mode.value,
                "operationId": journal.operation_id,
                "processedInstallationIds": list(journal.processed_installation_ids),
                "removedVersions": list(journal.removed_versions),
                "schemaVersion": 1,
                "scope": journal.scope.value,
                "targets": [_entry_json(item.entry) for item in journal.targets],
            },
            "journalHash",
        )

    def _delete_journal(self, journal: _UninstallJournal) -> None:
        if self._load_journal() != journal:
            raise InstallationLedgerError("uninstall_journal_changed", "uninstall journal changed before completion")
        self._journal_path.unlink()

    def _read_request(self, path: Path) -> dict[str, Any]:
        try:
            info = path.lstat()
        except OSError as error:
            raise InstallationLedgerError(
                "registration_request_missing", "registration request is unavailable"
            ) from error
        if not _safe_regular_file(path, info):
            raise InstallationLedgerError("registration_request_invalid", "registration request is not a safe file")
        self._protect_and_verify(path, directory=False)
        payload = path.read_bytes()
        if not payload or len(payload) > _MAX_REQUEST_BYTES:
            raise InstallationLedgerError("registration_request_size", "registration request exceeds its bound")
        try:
            raw = cast(object, json.loads(payload.decode("utf-8", errors="strict")))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise InstallationLedgerError(
                "registration_request_invalid", "registration request is malformed"
            ) from error
        if (
            not isinstance(raw, dict)
            or set(raw) != {"pluginVersion", "schemaVersion", "source", "vaultRoot"}
            or raw["schemaVersion"] != 1
            or _canonical_json(raw) != payload
        ):
            raise InstallationLedgerError("registration_request_invalid", "registration request is non-canonical")
        return cast(dict[str, Any], raw)

    def _load_document(
        self,
        path: Path,
        *,
        maximum: int,
        hash_field: str,
        missing_code: str | None,
    ) -> dict[str, Any] | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing_code is not None:
                raise InstallationLedgerError(
                    missing_code,
                    "installer-owned registration ledger is missing",
                ) from None
            return None
        if not _safe_regular_file(path, info):
            raise InstallationLedgerError("ledger_file_invalid", "installer metadata is not a safe regular file")
        self._protect_and_verify(path, directory=False)
        payload = path.read_bytes()
        if not payload or len(payload) > maximum:
            raise InstallationLedgerError("ledger_size_invalid", "installer metadata exceeds its bound")
        try:
            raw = cast(object, json.loads(payload.decode("utf-8", errors="strict")))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise InstallationLedgerError("ledger_malformed", "installer metadata is malformed") from error
        if not isinstance(raw, dict) or _canonical_json(raw) != payload:
            raise InstallationLedgerError("ledger_noncanonical", "installer metadata is non-canonical")
        recorded = raw.get(hash_field)
        if not isinstance(recorded, str) or _HASH.fullmatch(recorded) is None:
            raise InstallationLedgerError("ledger_integrity_invalid", "installer metadata has no valid integrity hash")
        base = dict(raw)
        base.pop(hash_field)
        expected = "sha256:" + hashlib.sha256(_canonical_json(base)).hexdigest()
        if recorded != expected:
            raise InstallationLedgerError("ledger_integrity_invalid", "installer metadata integrity check failed")
        return cast(dict[str, Any], raw)

    def _save_document(self, path: Path, base: Mapping[str, object], hash_field: str) -> None:
        value = dict(base)
        value[hash_field] = "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()
        self._atomic_write(path, _canonical_json(value))

    def _write_selection(self, installation_id: str) -> None:
        if _INSTALLATION_ID.fullmatch(installation_id) is None:
            raise InstallationLedgerError("selected_installation_invalid", "selected installation is invalid")
        self._atomic_write(self._selection_path, (installation_id + "\n").encode("ascii"))

    def _sync_selection(self, state: InstallationLedgerState) -> None:
        try:
            info = self._selection_path.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise InstallationLedgerError(
                "selected_installation_ambiguous",
                "selected installation pointer cannot be inspected",
            ) from error
        if not _safe_regular_file(self._selection_path, info):
            raise InstallationLedgerError(
                "selected_installation_invalid",
                "selected installation pointer is not a safe file",
            )
        self._protect_and_verify(self._selection_path, directory=False)
        try:
            payload = self._selection_path.read_bytes()
            selected = payload.decode("ascii", errors="strict").removesuffix("\n")
        except (OSError, UnicodeError) as error:
            raise InstallationLedgerError(
                "selected_installation_invalid",
                "selected installation pointer is malformed",
            ) from error
        if payload != (selected + "\n").encode("ascii") or _INSTALLATION_ID.fullmatch(selected) is None:
            raise InstallationLedgerError(
                "selected_installation_invalid",
                "selected installation pointer is malformed",
            )
        if any(entry.installation_id == selected for entry in state.entries):
            return
        self._selection_path.unlink()

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        self._prepare_directory(path.parent)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb", buffering=0, closefd=False) as stream:
                    stream.write(payload)
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._protect_and_verify(temporary, directory=False)
            os.replace(temporary, path)
            self._protect_and_verify(path, directory=False)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _prepare_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        _require_safe_directory(path)
        self._protect_and_verify(path, directory=True)

    def _protect_and_verify(self, path: Path, *, directory: bool) -> None:
        try:
            self._acl_protector(path, directory)
            if not self._acl_verifier(path, directory):
                raise InstallationLedgerError("ledger_acl_invalid", "installer metadata ACL is not current-user-only")
        except InstallationLedgerError:
            raise
        except OSError as error:
            raise InstallationLedgerError("ledger_acl_failed", "current-user-only ACL could not be enforced") from error

    def _request_path(self, request_id: str) -> Path:
        return self._request_root / f"{request_id}.json"

    def _inject(self, phase: str, installation_id: str | None) -> None:
        if self._failure_injector is not None:
            self._failure_injector(phase, installation_id)


def _read_optional_owner(root: Path) -> str | None:
    try:
        config = read_portable_workspace_config(root)
    except FileNotFoundError:
        return None
    except PortableWorkspaceConfigError as error:
        raise InstallationLedgerError("workspace_identity_invalid", "Vault workspace identity is invalid") from error
    return f"workspace:{config.portable_workspace_id}"


def _verified_vault_root(value: Path) -> Path:
    text = str(value)
    if not value.is_absolute() or "\x00" in text or "\r" in text or "\n" in text or text.startswith(("\\\\", "//")):
        raise InstallationLedgerError("vault_root_invalid", "Vault root must be an absolute local path")
    try:
        direct = value.lstat()
        canonical = value.resolve(strict=True)
    except OSError as error:
        raise InstallationLedgerError("vault_root_unavailable", "Vault root cannot be inspected") from error
    if not _safe_directory(value, direct):
        raise InstallationLedgerError("vault_root_reparse", "Vault root cannot be a reparse point")
    _require_safe_directory(canonical)
    for relative in (Path(".obsidian"), Path(".obsidian") / "plugins"):
        target = canonical / relative
        try:
            info = target.lstat()
        except OSError as error:
            raise InstallationLedgerError("plugin_parent_missing", "plugin parent directory is unavailable") from error
        if not _safe_directory(target, info):
            raise InstallationLedgerError("plugin_parent_invalid", "plugin parent directory is unsafe")
    return canonical


def _identify_plugin_directory(path: Path) -> PluginDirectoryIdentity:
    if os.name == "nt":
        try:
            observed = identify_windows_file(path)
        except WindowsSecureTreeError as error:
            raise InstallationLedgerError(error.code, str(error)) from error
        if not observed.is_directory or observed.is_reparse_point:
            raise InstallationLedgerError("plugin_directory_invalid", "installed plugin directory is unsafe")
        return PluginDirectoryIdentity(
            observed.volume_id,
            observed.filesystem_id,
            _plugin_identity_hash(observed.volume_id, observed.filesystem_id),
        )
    try:
        info = path.lstat()
    except OSError as error:
        raise InstallationLedgerError(
            "plugin_directory_missing", "installed plugin directory is unavailable"
        ) from error
    if not _safe_directory(path, info):
        raise InstallationLedgerError("plugin_directory_invalid", "installed plugin directory is unsafe")
    volume_id = f"{info.st_dev:x}"
    filesystem_id = f"{info.st_ino:x}"
    return PluginDirectoryIdentity(volume_id, filesystem_id, _plugin_identity_hash(volume_id, filesystem_id))


def _assert_plugin_identity(path: Path, expected: PluginDirectoryIdentity) -> None:
    observed = _identify_plugin_directory(path)
    if observed != expected:
        raise InstallationLedgerError("plugin_directory_replaced", "installed plugin directory identity changed")


def _remove_verified_tree(
    root: Path,
    expected: PluginDirectoryIdentity,
    *,
    race_barrier: Callable[[Path, tuple[str, ...]], None] | None,
) -> None:
    if os.name == "nt":
        try:
            remove_windows_tree(
                root,
                expected_volume_id=expected.volume_id,
                expected_filesystem_id=expected.filesystem_id,
                race_barrier=race_barrier,
            )
        except WindowsSecureTreeError as error:
            raise InstallationLedgerError(error.code, str(error)) from error
        return
    _assert_plugin_identity(root, expected)
    for directory, directories, files in os.walk(root, topdown=False, followlinks=False):
        _assert_plugin_identity(root, expected)
        current = Path(directory)
        _require_safe_directory(current)
        for name in files:
            child = current / name
            info = child.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or child.is_symlink()
                or bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)
            ):
                raise InstallationLedgerError("plugin_tree_invalid", "plugin tree file is unsafe")
            child.unlink()
        for name in directories:
            child = current / name
            _require_safe_directory(child)
            child.rmdir()
    _assert_plugin_identity(root, expected)
    root.rmdir()


def _exists_without_ambiguity(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise InstallationLedgerError("plugin_path_ambiguous", "plugin path availability is ambiguous") from error
    return True


def _recorded_root_is_absent(path: str) -> bool:
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return True
    except OSError as error:
        raise InstallationLedgerError(
            "recorded_root_ambiguous", "recorded Vault root availability is ambiguous"
        ) from error
    return False


def _parse_entry(value: object) -> InstallationLedgerEntry:
    if not isinstance(value, dict) or set(value) != {
        "installationId",
        "ownerId",
        "pluginIdentity",
        "pluginVersions",
        "registeredAt",
        "rootIdentity",
        "updatedAt",
    }:
        raise TypeError("entry fields")
    root = value["rootIdentity"]
    plugin = value["pluginIdentity"]
    if not isinstance(root, dict) or set(root) != {"canonicalPath", "filesystemId", "identityHash", "volumeId"}:
        raise TypeError("root identity")
    if not isinstance(plugin, dict) or set(plugin) != {"filesystemId", "identityHash", "volumeId"}:
        raise TypeError("plugin identity")
    owner = value["ownerId"]
    if owner is not None and not isinstance(owner, str):
        raise TypeError("owner")
    return InstallationLedgerEntry(
        installation_id=_text(value["installationId"]),
        owner_id=owner,
        root_identity=CanonicalRootIdentity(
            canonical_path=_text(root["canonicalPath"]),
            volume_id=_text(root["volumeId"]),
            filesystem_id=_text(root["filesystemId"]),
            identity_hash=_text(root["identityHash"]),
        ),
        plugin_identity=PluginDirectoryIdentity(
            volume_id=_text(plugin["volumeId"]),
            filesystem_id=_text(plugin["filesystemId"]),
            identity_hash=_text(plugin["identityHash"]),
        ),
        plugin_versions=_text_tuple(value["pluginVersions"]),
        registered_at=_text(value["registeredAt"]),
        updated_at=_text(value["updatedAt"]),
    )


def _entry_json(entry: InstallationLedgerEntry) -> dict[str, object]:
    return {
        "installationId": entry.installation_id,
        "ownerId": entry.owner_id,
        "pluginIdentity": {
            "filesystemId": entry.plugin_identity.filesystem_id,
            "identityHash": entry.plugin_identity.identity_hash,
            "volumeId": entry.plugin_identity.volume_id,
        },
        "pluginVersions": list(entry.plugin_versions),
        "registeredAt": entry.registered_at,
        "rootIdentity": {
            "canonicalPath": entry.root_identity.canonical_path,
            "filesystemId": entry.root_identity.filesystem_id,
            "identityHash": entry.root_identity.identity_hash,
            "volumeId": entry.root_identity.volume_id,
        },
        "updatedAt": entry.updated_at,
    }


def _parse_receipt(value: object) -> InstallationUninstallReceipt:
    if not isinstance(value, dict) or set(value) != {
        "completedAt",
        "installationIds",
        "mode",
        "operationId",
        "removedVersions",
        "scope",
    }:
        raise TypeError("receipt fields")
    return InstallationUninstallReceipt(
        operation_id=_text(value["operationId"]),
        scope=LedgerUninstallScope(_text(value["scope"])),
        mode=LedgerUninstallMode(_text(value["mode"])),
        installation_ids=_text_tuple(value["installationIds"]),
        removed_versions=_text_tuple(value["removedVersions"]),
        completed_at=_text(value["completedAt"]),
    )


def _receipt_json(receipt: InstallationUninstallReceipt) -> dict[str, object]:
    return {
        "completedAt": receipt.completed_at,
        "installationIds": list(receipt.installation_ids),
        "mode": receipt.mode.value,
        "operationId": receipt.operation_id,
        "removedVersions": list(receipt.removed_versions),
        "scope": receipt.scope.value,
    }


def _validate_root_identity(identity: CanonicalRootIdentity) -> None:
    if (
        not identity.canonical_path
        or len(identity.canonical_path) > 32_767
        or "\x00" in identity.canonical_path
        or "\r" in identity.canonical_path
        or "\n" in identity.canonical_path
        or not Path(identity.canonical_path).is_absolute()
        or not identity.volume_id
        or not identity.filesystem_id
        or _HASH.fullmatch(identity.identity_hash) is None
    ):
        raise InstallationLedgerError("root_identity_invalid", "canonical root identity is invalid")
    expected = (
        "sha256:"
        + hashlib.sha256(
            f"v1\0{identity.canonical_path}\0{identity.volume_id}\0{identity.filesystem_id}".encode()
        ).hexdigest()
    )
    if expected != identity.identity_hash:
        raise InstallationLedgerError("root_identity_invalid", "canonical root identity hash differs")


def _validate_operation_values(operation_id: str, installation_ids: tuple[str, ...], versions: tuple[str, ...]) -> None:
    if _OPERATION_ID.fullmatch(operation_id) is None:
        raise InstallationLedgerError("operation_id_invalid", "operation identity is invalid")
    if (
        len(installation_ids) > _MAX_ENTRIES
        or installation_ids != tuple(sorted(set(installation_ids)))
        or any(_INSTALLATION_ID.fullmatch(item) is None for item in installation_ids)
        or len(versions) > 1024
        or versions != tuple(sorted(set(versions)))
        or any(_VERSION.fullmatch(item) is None for item in versions)
    ):
        raise InstallationLedgerError("operation_values_invalid", "operation values are invalid")


def _require_operation_request(
    operation_id: str,
    scope: LedgerUninstallScope,
    mode: LedgerUninstallMode,
    selected: str | None,
    confirmation: str | None,
) -> None:
    if _OPERATION_ID.fullmatch(operation_id) is None:
        raise InstallationLedgerError("operation_id_invalid", "operation identity is invalid")
    if scope is LedgerUninstallScope.SELECTED:
        if selected is None or _INSTALLATION_ID.fullmatch(selected) is None:
            raise InstallationLedgerError("selected_installation_required", "selected uninstall needs an exact ID")
        if mode is not LedgerUninstallMode.PRESERVE_DATA:
            raise InstallationLedgerError("selected_purge_forbidden", "selected uninstall always preserves local data")
    elif selected is not None:
        raise InstallationLedgerError("selected_installation_unexpected", "global uninstall forbids selected ID")
    if mode is LedgerUninstallMode.PURGE_DATA:
        if confirmation != _PURGE_CONFIRMATION:
            raise InstallationLedgerError("purge_confirmation_required", "global purge requires exact confirmation")
    elif confirmation is not None:
        raise InstallationLedgerError("confirmation_unexpected", "preserve-data uninstall forbids confirmation")


def _require_request_id(value: str) -> None:
    if _REQUEST_ID.fullmatch(value) is None:
        raise InstallationLedgerError("request_id_invalid", "registration request identity is invalid")


def _plugin_identity_hash(volume_id: str, filesystem_id: str) -> str:
    payload = f"offeragent-plugin-directory-v1\0{volume_id}\0{filesystem_id}".encode("ascii")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _path_identity_hash(path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(str(path)))
    return "sha256:" + hashlib.sha256(f"offeragent-installer-ledger-v1\0{normalized}".encode()).hexdigest()


def _safe_directory(path: Path, info: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and not path.is_symlink()
        and not bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)
    )


def _safe_regular_file(path: Path, info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and not path.is_symlink()
        and not bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)
    )


def _require_safe_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise InstallationLedgerError("directory_unavailable", "managed directory cannot be inspected") from error
    if not _safe_directory(path, info):
        raise InstallationLedgerError("directory_reparse", "managed directory is not a safe real directory")


def _default_acl_protector(path: Path, directory: bool) -> None:
    if os.name == "nt":
        protect_current_user_path(path, directory=directory)
    else:
        path.chmod(0o700 if directory else 0o600)


def _default_acl_verifier(path: Path, directory: bool) -> bool:
    if os.name != "nt":
        return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    from .windows_appcontainer import filesystem_dacl_sddl

    sid = current_windows_identity().sid
    sddl = filesystem_dacl_sddl(path)
    aces = re.findall(r"\([^)]*\)", sddl)
    expected_flags = "OICI" if directory else ""
    return (
        sddl.startswith("D:P")
        and len(aces) == 1
        and aces[0].startswith(f"(A;{expected_flags};")
        and aces[0].endswith(f";;;{sid})")
    )


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected text")
    return value


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError("expected text array")
    return tuple(value)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InstallationLedgerError("clock_invalid", "ledger clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise InstallationLedgerError("timestamp_invalid", "ledger timestamp must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InstallationLedgerError("timestamp_invalid", "ledger timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InstallationLedgerError("timestamp_invalid", "ledger timestamp is not timezone-aware")
    return parsed


__all__ = [
    "InstallationLedgerCoordinator",
    "InstallationLedgerEntry",
    "InstallationLedgerError",
    "InstallationLedgerState",
    "LedgerRegistrationSource",
    "LedgerUninstallMode",
    "LedgerUninstallResult",
    "LedgerUninstallScope",
    "ManagedPluginRemover",
    "PluginDirectoryIdentity",
]
