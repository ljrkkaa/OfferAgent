from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import pytest

from offeragent_harness.runtime.installation_ledger import (
    InstallationLedgerCoordinator,
    InstallationLedgerEntry,
    InstallationLedgerError,
    LedgerUninstallMode,
    LedgerUninstallScope,
    ManagedPluginRemover,
)
from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config

_PURGE_CONFIRMATION = "DELETE OFFERAGENT LOCAL DATA"


class _UninstallArgs(TypedDict):
    operation_id: str
    scope: LedgerUninstallScope
    mode: LedgerUninstallMode
    selected_installation_id: str | None
    confirmation: str | None


class _Lock:
    def __enter__(self) -> object:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _RuntimeReferences:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.invocations: list[tuple[str, bool, str | None]] = []
        self.effects: list[tuple[str, bool]] = []
        self.current_exists = True

    def __call__(
        self,
        owner_id: str,
        *,
        purge_data: bool,
        confirmation: str | None,
    ) -> tuple[str, ...]:
        self.invocations.append((owner_id, purge_data, confirmation))
        if purge_data:
            assert confirmation == _PURGE_CONFIRMATION
            others = self.active - {owner_id}
            if others:
                raise AssertionError("global purge ran while another owner remained")
            changed = owner_id in self.active or self.current_exists
            self.active.discard(owner_id)
            self.current_exists = False
        else:
            assert confirmation is None
            changed = owner_id in self.active
            self.active.discard(owner_id)
            if not self.active:
                self.current_exists = False
        if changed:
            self.effects.append((owner_id, purge_data))
            return ("2.0.0",)
        return ()


def _coordinator(
    tmp_path: Path,
    runtime: _RuntimeReferences,
    *,
    failure_injector: Callable[[str, str | None], None] | None = None,
    plugin_remover: ManagedPluginRemover | None = None,
) -> InstallationLedgerCoordinator:
    return InstallationLedgerCoordinator(
        ledger_path=tmp_path / "OfferAgent" / "installer" / "vault-installations.json",
        runtime_uninstall=runtime,
        lock_factory=_Lock,
        acl_protector=lambda path, directory: None,
        acl_verifier=lambda path, directory: True,
        failure_injector=failure_injector,
        plugin_remover=plugin_remover,
    )


def _vault(tmp_path: Path, name: str, *, workspace_uuid: uuid.UUID | None = None) -> tuple[Path, str | None]:
    root = tmp_path / name
    plugin = root / ".obsidian" / "plugins" / "offeragent-obsidian-plugin"
    (plugin / "runtime" / "windows-x64").mkdir(parents=True)
    (plugin / "main.js").write_text("plugin", encoding="utf-8")
    (plugin / "runtime" / "windows-x64" / "manifest.json").write_text("{}", encoding="utf-8")
    if workspace_uuid is None:
        return root, None
    config = ensure_portable_workspace_config(root, new_uuid=lambda: workspace_uuid)
    return root, f"workspace:{config.portable_workspace_id}"


def _register(
    coordinator: InstallationLedgerCoordinator,
    root: Path,
    *,
    version: str = "2.0.0",
) -> InstallationLedgerEntry:
    return coordinator.register_vault(root, plugin_version=version)


def test_pending_setup_registration_and_selected_uninstall_touch_only_exact_plugin(tmp_path: Path) -> None:
    runtime = _RuntimeReferences()
    coordinator = _coordinator(tmp_path, runtime)
    first, _ = _vault(tmp_path, "first")
    second, _ = _vault(tmp_path, "second")
    first_entry = _register(coordinator, first)
    second_entry = _register(coordinator, second)

    assert first_entry.owner_id is None
    assert coordinator.selection_path.read_text(encoding="ascii") == second_entry.installation_id + "\n"

    result = coordinator.uninstall(
        operation_id="1" * 64,
        scope=LedgerUninstallScope.SELECTED,
        mode=LedgerUninstallMode.PRESERVE_DATA,
        selected_installation_id=first_entry.installation_id,
        confirmation=None,
    )

    assert result.installation_ids == (first_entry.installation_id,)
    assert not (first / ".obsidian" / "plugins" / "offeragent-obsidian-plugin").exists()
    assert (second / ".obsidian" / "plugins" / "offeragent-obsidian-plugin" / "main.js").is_file()
    assert runtime.invocations == []
    assert coordinator.snapshot().entries == (second_entry,)
    assert coordinator.selection_path.read_text(encoding="ascii") == second_entry.installation_id + "\n"


def test_removing_explicit_selection_never_retargets_a_dictionary_order_neighbor(tmp_path: Path) -> None:
    runtime = _RuntimeReferences()
    coordinator = _coordinator(tmp_path, runtime)
    first, _ = _vault(tmp_path, "first")
    second, _ = _vault(tmp_path, "second")
    first_entry = _register(coordinator, first)
    selected = _register(coordinator, second)
    assert coordinator.selection_path.read_text(encoding="ascii") == selected.installation_id + "\n"

    coordinator.uninstall(
        operation_id="a" * 64,
        scope=LedgerUninstallScope.SELECTED,
        mode=LedgerUninstallMode.PRESERVE_DATA,
        selected_installation_id=selected.installation_id,
        confirmation=None,
    )

    assert coordinator.snapshot().entries == (first_entry,)
    assert not coordinator.selection_path.exists()
    assert (first / ".obsidian" / "plugins" / "offeragent-obsidian-plugin" / "main.js").is_file()


def test_selected_then_all_preserve_release_only_registered_owners_and_shared_current(tmp_path: Path) -> None:
    runtime = _RuntimeReferences()
    coordinator = _coordinator(tmp_path, runtime)
    first, first_owner = _vault(tmp_path, "first", workspace_uuid=uuid.UUID(int=1))
    second, second_owner = _vault(tmp_path, "second", workspace_uuid=uuid.UUID(int=2))
    first_entry = _register(coordinator, first)
    second_entry = _register(coordinator, second)
    assert first_owner is not None and second_owner is not None
    runtime.active.update({first_owner, second_owner})

    coordinator.uninstall(
        operation_id="2" * 64,
        scope=LedgerUninstallScope.SELECTED,
        mode=LedgerUninstallMode.PRESERVE_DATA,
        selected_installation_id=first_entry.installation_id,
        confirmation=None,
    )
    assert runtime.active == {second_owner}
    assert runtime.current_exists
    assert (second / ".obsidian" / "plugins" / "offeragent-obsidian-plugin").is_dir()

    coordinator.uninstall(
        operation_id="3" * 64,
        scope=LedgerUninstallScope.ALL,
        mode=LedgerUninstallMode.PRESERVE_DATA,
        selected_installation_id=None,
        confirmation=None,
    )
    assert runtime.active == set()
    assert not runtime.current_exists
    assert not (second / ".obsidian" / "plugins" / "offeragent-obsidian-plugin").exists()
    assert not coordinator.selection_path.exists()
    assert coordinator.snapshot().entries == ()
    assert {call[0] for call in runtime.invocations} == {first_owner, second_owner}
    assert second_entry.owner_id == second_owner


def test_all_purge_uses_only_final_bound_owner_after_other_references_release(tmp_path: Path) -> None:
    runtime = _RuntimeReferences()
    coordinator = _coordinator(tmp_path, runtime)
    roots_and_owners = [
        _vault(tmp_path, "first", workspace_uuid=uuid.UUID(int=11)),
        _vault(tmp_path, "second", workspace_uuid=uuid.UUID(int=12)),
    ]
    entries = [_register(coordinator, root) for root, _ in roots_and_owners]
    runtime.active.update(owner for _, owner in roots_and_owners if owner is not None)

    result = coordinator.uninstall(
        operation_id="4" * 64,
        scope=LedgerUninstallScope.ALL,
        mode=LedgerUninstallMode.PURGE_DATA,
        selected_installation_id=None,
        confirmation=_PURGE_CONFIRMATION,
    )

    assert result.installation_ids == tuple(sorted(entry.installation_id for entry in entries))
    assert [purge for _, purge, _ in runtime.invocations] == [False, True]
    assert runtime.active == set()
    assert not runtime.current_exists
    assert all(
        not (root / ".obsidian" / "plugins" / "offeragent-obsidian-plugin").exists() for root, _ in roots_and_owners
    )


def test_pending_only_global_purge_fails_closed_without_fake_runtime_owner(tmp_path: Path) -> None:
    runtime = _RuntimeReferences()
    coordinator = _coordinator(tmp_path, runtime)
    root, _ = _vault(tmp_path, "pending")
    _register(coordinator, root)

    with pytest.raises(InstallationLedgerError) as validation:
        coordinator.validate_uninstall_request(
            scope=LedgerUninstallScope.ALL,
            mode=LedgerUninstallMode.PURGE_DATA,
            selected_installation_id=None,
            confirmation=_PURGE_CONFIRMATION,
        )
    assert validation.value.code == "pending_only_purge_forbidden"

    with pytest.raises(InstallationLedgerError) as caught:
        coordinator.uninstall(
            operation_id="b" * 64,
            scope=LedgerUninstallScope.ALL,
            mode=LedgerUninstallMode.PURGE_DATA,
            selected_installation_id=None,
            confirmation=_PURGE_CONFIRMATION,
        )

    assert caught.value.code == "pending_only_purge_forbidden"
    assert runtime.invocations == []
    assert (root / ".obsidian" / "plugins" / "offeragent-obsidian-plugin" / "main.js").is_file()
    assert not (tmp_path / "OfferAgent" / "installer" / "uninstall-journal.json").exists()


def test_first_bootstrap_binds_pending_owner_and_repeated_version_updates_are_idempotent(tmp_path: Path) -> None:
    runtime = _RuntimeReferences()
    coordinator = _coordinator(tmp_path, runtime)
    root, _ = _vault(tmp_path, "vault")
    pending = _register(coordinator, root, version="2.0.0")
    config = ensure_portable_workspace_config(root, new_uuid=lambda: uuid.UUID(int=31))
    owner = f"workspace:{config.portable_workspace_id}"

    bound = coordinator.bind_runtime_owner(root, owner_id=owner, plugin_version="2.0.1")
    repeated = coordinator.bind_runtime_owner(root, owner_id=owner, plugin_version="2.0.1")

    assert bound.installation_id == pending.installation_id == repeated.installation_id
    assert repeated.owner_id == owner
    assert repeated.plugin_versions == ("2.0.0", "2.0.1")
    assert len(coordinator.snapshot().entries) == 1


def test_moved_vault_rebinds_same_owner_but_deleted_vault_is_never_searched_for(tmp_path: Path) -> None:
    runtime = _RuntimeReferences()
    coordinator = _coordinator(tmp_path, runtime)
    original, owner = _vault(tmp_path, "original", workspace_uuid=uuid.UUID(int=41))
    entry = _register(coordinator, original)
    assert owner is not None
    runtime.active.add(owner)

    moved = tmp_path / "moved"
    original.rename(moved)
    rebound = coordinator.bind_runtime_owner(moved, owner_id=owner, plugin_version="2.1.0")
    assert rebound.installation_id == entry.installation_id
    assert rebound.root_identity.canonical_path != entry.root_identity.canonical_path

    coordinator.uninstall(
        operation_id="5" * 64,
        scope=LedgerUninstallScope.SELECTED,
        mode=LedgerUninstallMode.PRESERVE_DATA,
        selected_installation_id=rebound.installation_id,
        confirmation=None,
    )
    assert not (moved / ".obsidian" / "plugins" / "offeragent-obsidian-plugin").exists()
    assert owner not in runtime.active

    deleted, deleted_owner = _vault(tmp_path, "deleted", workspace_uuid=uuid.UUID(int=42))
    deleted_entry = _register(coordinator, deleted)
    assert deleted_owner is not None
    runtime.active.add(deleted_owner)
    renamed_outside_record = tmp_path / "user-moved-without-rebind"
    deleted.rename(renamed_outside_record)
    coordinator.uninstall(
        operation_id="6" * 64,
        scope=LedgerUninstallScope.SELECTED,
        mode=LedgerUninstallMode.PRESERVE_DATA,
        selected_installation_id=deleted_entry.installation_id,
        confirmation=None,
    )
    assert (renamed_outside_record / ".obsidian" / "plugins" / "offeragent-obsidian-plugin" / "main.js").is_file()
    assert deleted_owner not in runtime.active


@pytest.mark.parametrize("failure_phase,expected_invocations", [("entry_effects_applied", 2), ("entry_journaled", 1)])
def test_interrupted_uninstall_recovers_and_ack_replay_never_repeats_effect(
    tmp_path: Path,
    failure_phase: str,
    expected_invocations: int,
) -> None:
    runtime = _RuntimeReferences()
    fired = False

    def inject(phase: str, installation_id: str | None) -> None:
        nonlocal fired
        if phase == failure_phase and installation_id is not None and not fired:
            fired = True
            raise RuntimeError("simulated interruption")

    coordinator = _coordinator(tmp_path, runtime, failure_injector=inject)
    root, owner = _vault(tmp_path, "vault", workspace_uuid=uuid.UUID(int=51))
    entry = _register(coordinator, root)
    assert owner is not None
    runtime.active.add(owner)
    request: _UninstallArgs = dict(
        operation_id="7" * 64,
        scope=LedgerUninstallScope.SELECTED,
        mode=LedgerUninstallMode.PRESERVE_DATA,
        selected_installation_id=entry.installation_id,
        confirmation=None,
    )

    with pytest.raises(RuntimeError, match="interruption"):
        coordinator.uninstall(**request)
    assert (tmp_path / "OfferAgent" / "installer" / "uninstall-journal.json").is_file()

    recovered = coordinator.uninstall(**request)
    replayed = coordinator.uninstall(**request)
    assert not recovered.replayed
    assert replayed.replayed
    assert len(runtime.invocations) == expected_invocations
    assert runtime.effects == [(owner, False)]
    assert not (tmp_path / "OfferAgent" / "installer" / "uninstall-journal.json").exists()


def test_receipt_saved_before_ack_replay_cleans_completed_outer_journal(tmp_path: Path) -> None:
    runtime = _RuntimeReferences()
    fired = False

    def inject(phase: str, installation_id: str | None) -> None:
        nonlocal fired
        del installation_id
        if phase == "completed" and not fired:
            fired = True
            raise RuntimeError("ACK lost after durable receipt")

    coordinator = _coordinator(tmp_path, runtime, failure_injector=inject)
    root, owner = _vault(tmp_path, "vault", workspace_uuid=uuid.UUID(int=52))
    entry = _register(coordinator, root)
    assert owner is not None
    runtime.active.add(owner)
    request: _UninstallArgs = dict(
        operation_id="c" * 64,
        scope=LedgerUninstallScope.SELECTED,
        mode=LedgerUninstallMode.PRESERVE_DATA,
        selected_installation_id=entry.installation_id,
        confirmation=None,
    )

    with pytest.raises(RuntimeError, match="ACK lost"):
        coordinator.uninstall(**request)
    journal = tmp_path / "OfferAgent" / "installer" / "uninstall-journal.json"
    assert journal.is_file()

    replay = coordinator.uninstall(**request)

    assert replay.replayed
    assert runtime.effects == [(owner, False)]
    assert len(runtime.invocations) == 1
    assert not journal.exists()


def test_registration_request_is_fixed_acl_guarded_canonical_and_consumed(tmp_path: Path) -> None:
    runtime = _RuntimeReferences()
    coordinator = _coordinator(tmp_path, runtime)
    root, _ = _vault(tmp_path, "vault")
    request_id = "8" * 64
    request = coordinator.prepare_registration_request(request_id)
    payload = {
        "pluginVersion": "2.0.0",
        "schemaVersion": 1,
        "source": "setup",
        "vaultRoot": str(root),
    }
    request.write_bytes((json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())

    entry = coordinator.consume_registration_request(request_id)

    assert entry.owner_id is None
    assert not request.exists()
    assert entry.root_identity.canonical_path == os.path.normcase(os.path.normpath(str(root.resolve())))


def test_ledger_integrity_tamper_fails_closed_without_rewriting_registration(tmp_path: Path) -> None:
    runtime = _RuntimeReferences()
    coordinator = _coordinator(tmp_path, runtime)
    root, _ = _vault(tmp_path, "vault")
    entry = _register(coordinator, root)
    ledger = tmp_path / "OfferAgent" / "installer" / "vault-installations.json"
    document = json.loads(ledger.read_text(encoding="utf-8"))
    document["entries"][0]["pluginVersions"] = ["9.9.9"]
    ledger.write_bytes(
        (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )

    with pytest.raises(InstallationLedgerError) as caught:
        coordinator.snapshot()

    assert caught.value.code == "ledger_integrity_invalid"
    assert (root / ".obsidian" / "plugins" / "offeragent-obsidian-plugin" / "main.js").is_file()
    assert entry.installation_id in ledger.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="junction race is a Windows product-boundary test")
def test_scan_then_junction_replacement_never_deletes_external_tree_and_keeps_journal(tmp_path: Path) -> None:
    runtime = _RuntimeReferences()
    fired = False
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "must-survive.txt"
    protected.write_text("KEEP", encoding="utf-8")

    def replace_child_with_junction(path: Path, names: tuple[str, ...]) -> None:
        nonlocal fired
        if fired or "runtime" not in names:
            return
        fired = True
        child = path / "runtime"
        for descendant in sorted(child.rglob("*"), reverse=True):
            if descendant.is_file():
                descendant.unlink()
            else:
                descendant.rmdir()
        child.rmdir()
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(child), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("current Windows filesystem cannot create a junction")

    remover = ManagedPluginRemover(race_barrier=replace_child_with_junction)
    coordinator = _coordinator(tmp_path, runtime, plugin_remover=remover)
    root, owner = _vault(tmp_path, "vault", workspace_uuid=uuid.UUID(int=61))
    entry = _register(coordinator, root)
    assert owner is not None
    runtime.active.add(owner)

    with pytest.raises(InstallationLedgerError) as caught:
        coordinator.uninstall(
            operation_id="9" * 64,
            scope=LedgerUninstallScope.SELECTED,
            mode=LedgerUninstallMode.PRESERVE_DATA,
            selected_installation_id=entry.installation_id,
            confirmation=None,
        )

    assert caught.value.code == "plugin_tree_reparse"
    assert protected.read_text(encoding="utf-8") == "KEEP"
    assert runtime.invocations == []
    assert (tmp_path / "OfferAgent" / "installer" / "uninstall-journal.json").is_file()
    assert coordinator.snapshot().entries == (entry,)

    tombstone = root / ".obsidian" / "plugins" / f".offeragent-uninstall-{entry.installation_id}.pending"
    os.rmdir(tombstone / "runtime")


def test_inno_all_scope_cleans_only_fixed_known_metadata_after_coordinator_ack() -> None:
    repository = Path(__file__).resolve().parents[5]
    source = (repository / "packages" / "offeragent-harness" / "packaging" / "OfferAgent.iss").read_text(
        encoding="utf-8"
    )
    uninstall_section = source.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]

    assert "vault-installations.json" in uninstall_section
    assert "selected-installation.txt" in uninstall_section
    assert "uninstall-journal.json" in uninstall_section
    assert "Type: dirifempty" in uninstall_section
    assert "filesandordirs" not in uninstall_section.lower()
    assert "*" not in uninstall_section
    assert "Result := False" in source
    assert "RunBootstrap(Parameters)" in source
    assert "CoCreateGuid" in source
    assert "Random(" not in source
    assert "GetTickCount" not in source
    assert "GetDateTimeString" not in source
    assert "请完整输入上方 installationId" in source
    assert "最近一次由 Setup 选择或插件 bootstrap 明确绑定" in source
    initialize = source.index("function InitializeUninstall")
    assert source.index("RunBootstrap(Parameters)", initialize) < source.index("Result := True", initialize)
    assert "[Registry]" not in source
