from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

from offeragent_harness._release_keys import PUBLIC_KEYS_BASE64URL
from offeragent_harness.runtime.bootstrap_cli import (
    BootstrapApplication,
    BootstrapCliError,
    LocalStateSchemaMigration,
    parse_arguments,
    parse_inno_ledger_arguments,
)
from offeragent_harness.runtime.installation_ledger import LedgerUninstallMode, LedgerUninstallScope
from offeragent_harness.runtime.runtime_installer import RuntimeInstallError
from offeragent_harness.storage.migrations import LATEST_REQUIRED_TABLES, LATEST_SCHEMA_VERSION


def test_bootstrap_cli_accepts_only_fixed_private_fd_contract() -> None:
    assert parse_arguments(["install", "--request-fd", "3", "--result-fd", "4"]) == ("install", 3, 4)
    assert parse_arguments(["uninstall", "--request-fd", "3", "--result-fd", "4"]) == ("uninstall", 3, 4)
    with pytest.raises(BootstrapCliError, match="fixed"):
        parse_arguments(["install", "--bundle-root", "C:\\payload"])


def test_inno_ledger_contract_accepts_only_fixed_ids_and_explicit_scope() -> None:
    operation = "a" * 64
    installation = "install_" + "b" * 32
    assert parse_inno_ledger_arguments(
        [
            "inno-ledger",
            "uninstall",
            "--operation-id",
            operation,
            "--scope",
            "selected",
            "--installation-id",
            installation,
            "--mode",
            "preserve-data",
        ]
    ) == (
        "uninstall",
        None,
        operation,
        LedgerUninstallScope.SELECTED,
        LedgerUninstallMode.PRESERVE_DATA,
        installation,
        None,
    )
    assert parse_inno_ledger_arguments(
        [
            "inno-ledger",
            "validate-uninstall",
            "--scope",
            "selected",
            "--installation-id",
            installation,
            "--mode",
            "preserve-data",
        ]
    ) == (
        "validate",
        None,
        None,
        LedgerUninstallScope.SELECTED,
        LedgerUninstallMode.PRESERVE_DATA,
        installation,
        None,
    )
    assert parse_inno_ledger_arguments(
        [
            "inno-ledger",
            "uninstall",
            "--operation-id",
            operation,
            "--scope",
            "all",
            "--mode",
            "purge-data",
            "--confirmation",
            "DELETE OFFERAGENT LOCAL DATA",
        ]
    ) == (
        "uninstall",
        None,
        operation,
        LedgerUninstallScope.ALL,
        LedgerUninstallMode.PURGE_DATA,
        None,
        "DELETE OFFERAGENT LOCAL DATA",
    )
    with pytest.raises(BootstrapCliError):
        parse_inno_ledger_arguments(
            [
                "inno-ledger",
                "uninstall",
                "--operation-id",
                operation,
                "--scope",
                "selected",
                "--installation-id",
                "C:\\vault",
                "--mode",
                "preserve-data",
            ]
        )


class _Authenticode:
    def verify(self, executable: Path) -> bool:
        assert executable.is_file()
        return True


class _Uninstaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, str | None]] = []

    def uninstall(self, *, owner_id: str, purge_data: bool, confirmation: str | None) -> tuple[str, ...]:
        self.calls.append((owner_id, purge_data, confirmation))
        return ("2.0.0",)


def _uninstall_application(uninstaller: _Uninstaller) -> BootstrapApplication:
    return BootstrapApplication(
        installer_factory=lambda request: uninstaller,  # type: ignore[arg-type,return-value]
        authenticode=_Authenticode(),  # type: ignore[arg-type]
    )


def test_bootstrap_uninstall_defaults_to_preserve_data_and_returns_no_paths() -> None:
    uninstaller = _Uninstaller()
    result = _uninstall_application(uninstaller).uninstall(
        {"confirmation": None, "ownerId": "workspace:ws_1234", "purgeData": False, "schemaVersion": 1}
    )

    assert uninstaller.calls == [("workspace:ws_1234", False, None)]
    assert result == {
        "mode": "preserve_data",
        "removedVersions": ["2.0.0"],
        "schemaVersion": 1,
        "status": "uninstalled",
        "type": "result",
    }
    assert "\\" not in json.dumps(result)


def test_bootstrap_uninstall_purge_requires_exact_second_confirmation() -> None:
    uninstaller = _Uninstaller()
    application = _uninstall_application(uninstaller)

    with pytest.raises(BootstrapCliError, match="confirmation"):
        application.uninstall(
            {"confirmation": "yes", "ownerId": "workspace:ws_1234", "purgeData": True, "schemaVersion": 1}
        )
    assert uninstaller.calls == []


def test_uninstall_request_requires_canonical_closed_schema() -> None:
    from offeragent_harness.runtime import bootstrap_cli

    value = {"confirmation": None, "ownerId": "workspace:ws_1234", "purgeData": False, "schemaVersion": 1}
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    os.close(write_fd)
    assert bootstrap_cli._read_uninstall_request(read_fd) == value

    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload.removesuffix(b"\n"))
    os.close(write_fd)
    with pytest.raises(BootstrapCliError, match="canonical"):
        bootstrap_cli._read_uninstall_request(read_fd)


def test_install_request_requires_canonical_hash_bound_privilege_receipt() -> None:
    from offeragent_harness.runtime import bootstrap_cli

    receipt = {
        "confirmation": "我确认授予 OfferAgent Runtime 上述本地权限",
        "diffHash": "sha256:" + "d" * 64,
        "expiresAt": "2026-07-13T12:05:00.000Z",
        "issuedAt": "2026-07-13T12:00:00.000Z",
        "newManifestHash": "sha256:" + "b" * 64,
        "newPrivilegeFingerprint": "sha256:" + "c" * 64,
        "oldManifestHash": None,
        "oldPrivilegeFingerprint": None,
        "receiptId": "1" * 64,
        "schemaVersion": 1,
    }
    value = {
        "bundleRoot": "C:\\signed-payload",
        "legacyOwnerId": None,
        "manifestHash": "sha256:" + "b" * 64,
        "ownerId": "workspace:ws_1234",
        "pluginVersion": "2.0.0",
        "privilegeApproval": receipt,
        "protocolVersion": "1.0",
        "schemaHash": "sha256:" + "a" * 64,
        "schemaVersion": 3,
        "vaultRoot": "C:\\Vault",
    }
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    os.close(write_fd)
    assert bootstrap_cli._read_request(read_fd) == value

    value["privilegeApproval"] = {**receipt, "newManifestHash": "sha256:" + "e" * 64, "extra": True}
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    os.close(write_fd)
    with pytest.raises(BootstrapCliError):
        bootstrap_cli._read_request(read_fd)


def test_bootstrap_runs_the_real_checksum_verified_sqlite_migrations(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = tmp_path / "workspace" / "state.sqlite"
    database.parent.mkdir()

    LocalStateSchemaMigration().migrate(runtime, (database,), LATEST_SCHEMA_VERSION)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (LATEST_SCHEMA_VERSION,)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert LATEST_REQUIRED_TABLES <= tables


def test_bootstrap_refuses_manifest_state_schema_not_equal_to_packaged_generation(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    with pytest.raises(RuntimeInstallError, match="candidate"):
        LocalStateSchemaMigration().migrate(runtime, (), LATEST_SCHEMA_VERSION + 1)


def test_bootstrap_uses_the_verified_native_windows_architecture(monkeypatch: pytest.MonkeyPatch) -> None:
    from offeragent_harness.runtime import bootstrap_cli

    monkeypatch.setattr(bootstrap_cli, "native_windows_architecture", lambda: "arm64")

    assert bootstrap_cli._native_windows_architecture() == "arm64"


def test_plugin_and_bootstrap_embed_the_same_public_release_keyring() -> None:
    repo = Path(__file__).resolve().parents[5]
    source = (repo / "src" / "interface" / "obsidian" / "src" / "runtime" / "generated_release_keyring.ts").read_text(
        encoding="utf-8"
    )
    values = dict(re.findall(r'"([a-z][a-z0-9_.-]+)": "([A-Za-z0-9_-]{43})"', source))
    assert values == PUBLIC_KEYS_BASE64URL
