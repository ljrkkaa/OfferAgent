from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from offeragent_harness.runtime.release_manifest import (
    BootstrapRecord,
    ProtocolCompatibility,
    RuntimeArchive,
    RuntimeFileRecord,
    RuntimePlatform,
    RuntimeReleaseManifest,
    VerifiedInstalledManifest,
    VerifiedRuntimeBundle,
    canonical_manifest_bytes,
    parse_manifest,
    runtime_content_digest,
)
from offeragent_harness.runtime.release_privileges import (
    PRIVILEGE_APPROVAL_CONFIRMATION,
    RuntimePrivilegeApprovalReceipt,
    build_privilege_envelope_from_process_catalog,
    format_utc_timestamp,
)
from offeragent_harness.runtime.runtime_installer import (
    InstalledRuntime,
    RuntimeInstaller,
    RuntimeInstallError,
    RuntimeInstallPhase,
    RuntimePrivilegeApprovalRequired,
    RuntimePurgePhase,
    RuntimeSelfTestReport,
)

_PROCESS_CATALOG = (Path(__file__).resolve().parents[3] / "packaging" / "process-catalog.v1.json").read_bytes()


def _expanded_catalog() -> bytes:
    value = json.loads(_PROCESS_CATALOG)
    value["environmentProfiles"][0]["allowedNames"] = ["OFFERAGENT_SAFE"]
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


_EXPANDED_CATALOG = _expanded_catalog()


def _hash(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _manifest(
    version: str,
    *,
    schema: int = 1,
    process_catalog: bytes = _PROCESS_CATALOG,
) -> RuntimeReleaseManifest:
    records = tuple(
        RuntimeFileRecord(
            path,
            len(process_catalog) if path == "process-catalog.v1.json" else 1,
            _hash(process_catalog) if path == "process-catalog.v1.json" else _hash(path.encode()),
            kind,
            executable,
        )
        for path, kind, executable in (
            ("LICENSES/a", "license", False),
            ("offeragent-host.exe", "executable", True),
            ("offeragent-process-host.exe", "executable", True),
            ("offeragent-self-test.exe", "executable", True),
            ("offeragent-worker.exe", "executable", True),
            ("process-catalog.v1.json", "asset", False),
            ("provenance/a", "provenance", False),
            ("sbom/a", "sbom", False),
            ("web/index.html", "web", False),
        )
    )
    return RuntimeReleaseManifest(
        version,
        version,
        "2.0.0",
        "2.9.9",
        "release",
        "a" * 40,
        datetime(2026, 7, 13, tzinfo=timezone.utc),
        RuntimePlatform("windows", "x64", 19_045),
        ProtocolCompatibility("1.0", "1.0", "sha256:" + "b" * 64),
        schema,
        "1",
        RuntimeArchive("runtime.zip", runtime_content_digest(records), 64 * 1024),
        BootstrapRecord("bootstrap.exe", 1, _hash(b"b"), True),
        records,
        ("offline",),
        build_privilege_envelope_from_process_catalog(process_catalog),
        2,
    )


def _bundle(
    root: Path,
    version: str,
    *,
    schema: int = 1,
    process_catalog: bytes = _PROCESS_CATALOG,
) -> VerifiedRuntimeBundle:
    manifest = _manifest(version, schema=schema, process_catalog=process_catalog)
    manifest_bytes = canonical_manifest_bytes(manifest)
    return VerifiedRuntimeBundle(
        root,
        root / "runtime.zip",
        root / "runtime-manifest.json",
        root / "runtime-manifest.sig",
        root / "bootstrap.exe",
        manifest_bytes,
        b"signature\n",
        manifest,
    )


class _Verifier:
    def verify_bundle(self, root: Path, **_: object) -> VerifiedRuntimeBundle:
        version, schema = root.name.split("-")
        return _bundle(root, version, schema=int(schema))

    def verify_installed_tree(self, root: Path, bundle: VerifiedRuntimeBundle) -> None:
        if (root / "runtime-manifest.json").read_bytes() != bundle.manifest_bytes:
            raise AssertionError("wrong installed manifest")

    def verify_installed_manifest(
        self,
        root: Path,
        *,
        expected_manifest_hash: str,
        expected_architecture: str,
    ) -> VerifiedInstalledManifest:
        payload = (root / "runtime-manifest.json").read_bytes()
        manifest = parse_manifest(payload)
        assert _hash(payload) == expected_manifest_hash
        assert expected_architecture == "x64"
        return VerifiedInstalledManifest(root, payload, b"signature\n", expected_manifest_hash, manifest)


class _ArchitectureCapturingVerifier(_Verifier):
    def __init__(self) -> None:
        self.expected_architecture: object = None

    def verify_bundle(self, root: Path, **values: object) -> VerifiedRuntimeBundle:
        self.expected_architecture = values.get("expected_architecture")
        return super().verify_bundle(root, **values)


class _ExpansionVerifier(_Verifier):
    def verify_bundle(self, root: Path, **_: object) -> VerifiedRuntimeBundle:
        version, schema = root.name.split("-")
        catalog = _EXPANDED_CATALOG if version == "2.0.0" else _PROCESS_CATALOG
        return _bundle(root, version, schema=int(schema), process_catalog=catalog)


class _Extractor:
    def extract(self, bundle: VerifiedRuntimeBundle, destination: Path) -> None:
        destination.mkdir()
        (destination / "runtime-manifest.json").write_bytes(bundle.manifest_bytes)
        (destination / "runtime-manifest.sig").write_bytes(bundle.signature_bytes)
        (destination / "offeragent-self-test.exe").write_bytes(b"self-test")


class _SelfTest:
    def __init__(self) -> None:
        self.fail_version: str | None = None

    def run(self, root: Path, *, timeout_seconds: float) -> RuntimeSelfTestReport:
        assert timeout_seconds == 60
        if self.fail_version == root.name:
            raise RuntimeInstallError("post_activate_failed", "injected self-test failure")
        return RuntimeSelfTestReport(True, {"all": True})


class _Migration:
    def __init__(self) -> None:
        self.fail = False

    def migrate(self, runtime_root: Path, state_databases: Sequence[Path], target_schema: int) -> None:
        assert runtime_root.exists()
        for database in state_databases:
            with sqlite3.connect(database) as connection:
                connection.execute(f"PRAGMA user_version={target_schema}")
                connection.execute("INSERT INTO test(value) VALUES (?)", (f"schema-{target_schema}",))
        if self.fail:
            raise RuntimeInstallError("migration_failed", "injected migration failure")


class _Quiescer:
    def __init__(self) -> None:
        self.entries = 0

    @contextlib.contextmanager
    def quiesce_for_update(self, *, timeout_seconds: float) -> Iterator[None]:
        assert timeout_seconds == 45
        self.entries += 1
        yield


class _Lock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _Purge:
    def __init__(self, workspaces_root: Path) -> None:
        self._workspaces_root = workspaces_root
        self.called = False
        self.calls = 0

    def purge_non_vault_data(self) -> None:
        assert not self._workspaces_root.exists() or self._workspaces_root.is_dir()
        self.called = True
        self.calls += 1


def _installer(
    tmp_path: Path,
    *,
    self_test: _SelfTest | None = None,
    migration: _Migration | None = None,
    purge: _Purge | None = None,
    verifier: _Verifier | None = None,
    purge_failure_injector: Callable[[RuntimePurgePhase], None] | None = None,
    legacy_owner_is_active: Callable[[int], bool | None] | None = None,
) -> RuntimeInstaller:
    return RuntimeInstaller(
        runtime_root=tmp_path / "runtime",
        workspaces_root=tmp_path / "workspaces",
        verifier=verifier or _Verifier(),  # type: ignore[arg-type]
        extractor=_Extractor(),  # type: ignore[arg-type]
        self_test=self_test or _SelfTest(),
        migration=migration or _Migration(),
        quiescer=_Quiescer(),
        purge=purge,
        lock_factory=_Lock,
        purge_failure_injector=purge_failure_injector,
        legacy_owner_is_active=legacy_owner_is_active,
    )


def _create_state(tmp_path: Path) -> Path:
    database = tmp_path / "workspaces" / "workspace-a" / "state.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE test(value TEXT NOT NULL)")
        connection.execute("INSERT INTO test(value) VALUES ('original')")
    return database


class _FailPurgeAt:
    def __init__(self, phase: RuntimePurgePhase) -> None:
        self.phase = phase
        self.triggered = False

    def __call__(self, phase: RuntimePurgePhase) -> None:
        if phase is self.phase and not self.triggered:
            self.triggered = True
            raise RuntimeInstallError("injected_purge_crash", f"injected crash at {phase.value}")


def _create_local_purge_state(tmp_path: Path) -> None:
    (tmp_path / "workspaces").mkdir(exist_ok=True)
    (tmp_path / "workspace-registry.json").write_text("registry", encoding="utf-8")
    (tmp_path / "workspace-registry.json.lock").write_text("lock", encoding="utf-8")
    (tmp_path / f".workspace-registry.json.{'a' * 32}.tmp").write_text("temporary", encoding="utf-8")
    for name in ("host", "logs", "config", "secrets", "skills"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "owned-state").write_text(name, encoding="utf-8")


def _purge(installer: RuntimeInstaller, *, owner: str = "vault-a") -> tuple[str, ...]:
    return installer.uninstall(
        owner_id=owner,
        purge_data=True,
        confirmation="DELETE OFFERAGENT LOCAL DATA",
    )


def test_runtime_installer_uses_verified_native_architecture_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from offeragent_harness.runtime import runtime_installer

    verifier = _ArchitectureCapturingVerifier()
    installer = _installer(tmp_path, verifier=verifier)
    monkeypatch.setattr(runtime_installer, "native_windows_architecture", lambda: "arm64")
    bundle = tmp_path / "1.0.0-1"
    bundle.mkdir()

    with pytest.raises(RuntimePrivilegeApprovalRequired):
        installer.ensure_ready(
            bundle,
            plugin_version="2.0.0",
            protocol_version="1.0",
            schema_hash="sha256:" + "b" * 64,
            owner_id="vault-a",
            windows_build=22_631,
        )

    assert verifier.expected_architecture == "arm64"


def _ensure(
    installer: RuntimeInstaller,
    root: Path,
    *,
    owner: str = "vault-a",
    progress: Callable[[RuntimeInstallPhase], None] | None = None,
) -> InstalledRuntime:
    try:
        return _invoke(installer, root, owner=owner, progress=progress)
    except RuntimePrivilegeApprovalRequired as challenge:
        receipt = _receipt_for_challenge(challenge, receipt_id="a" * 64)
        return _invoke(installer, root, owner=owner, progress=progress, privilege_approval=receipt)


def _invoke(
    installer: RuntimeInstaller,
    root: Path,
    *,
    owner: str = "vault-a",
    progress: Callable[[RuntimeInstallPhase], None] | None = None,
    privilege_approval: RuntimePrivilegeApprovalReceipt | None = None,
) -> InstalledRuntime:
    return installer.ensure_ready(
        root,
        plugin_version="2.0.0",
        protocol_version="1.0",
        schema_hash="sha256:" + "b" * 64,
        owner_id=owner,
        expected_architecture="x64",
        windows_build=22_631,
        progress=progress,
        privilege_approval=privilege_approval,
    )


def _receipt_for_challenge(
    challenge: RuntimePrivilegeApprovalRequired,
    *,
    receipt_id: str,
) -> RuntimePrivilegeApprovalReceipt:
    now = datetime.now(timezone.utc)
    return RuntimePrivilegeApprovalReceipt(
        receipt_id=receipt_id,
        issued_at=format_utc_timestamp(now),
        expires_at=format_utc_timestamp(now + timedelta(minutes=5)),
        confirmation=PRIVILEGE_APPROVAL_CONFIRMATION,
        old_manifest_hash=challenge.old_manifest_hash,
        new_manifest_hash=challenge.new_manifest_hash,
        old_privilege_fingerprint=challenge.old_privilege_fingerprint,
        new_privilege_fingerprint=challenge.new_privilege_fingerprint,
        diff_hash=challenge.diff_hash,
    )


def test_install_uses_staging_sqlite_backup_and_atomic_pointer(tmp_path: Path) -> None:
    database = _create_state(tmp_path)
    installer = _installer(tmp_path)
    phases: list[RuntimeInstallPhase] = []

    result = _ensure(installer, tmp_path / "1.0.0-2", progress=phases.append)

    assert result.root == tmp_path / "runtime" / "1.0.0"
    assert result.pointer.current_version == "1.0.0"
    assert RuntimeInstallPhase.BACKING_UP_STATE in phases
    assert RuntimeInstallPhase.ATOMIC_ACTIVATE in phases
    assert not list((tmp_path / "runtime").glob(".staging-*"))
    pointer = json.loads((tmp_path / "runtime" / "current.json").read_text("utf-8"))
    assert pointer["currentVersion"] == "1.0.0"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)


def test_post_activation_failure_restores_pointer_and_sqlite_snapshot(tmp_path: Path) -> None:
    database = _create_state(tmp_path)
    self_test = _SelfTest()
    installer = _installer(tmp_path, self_test=self_test)
    _ensure(installer, tmp_path / "1.0.0-1")
    self_test.fail_version = "2.0.0"

    with pytest.raises(RuntimeInstallError, match="self-test"):
        _ensure(installer, tmp_path / "2.0.0-2")

    pointer = json.loads((tmp_path / "runtime" / "current.json").read_text("utf-8"))
    assert pointer["currentVersion"] == "1.0.0"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("SELECT value FROM test ORDER BY rowid").fetchall() == [
            ("original",),
            ("schema-1",),
        ]


def test_three_failed_versions_open_persistent_update_circuit(tmp_path: Path) -> None:
    _create_state(tmp_path)
    migration = _Migration()
    migration.fail = True
    installer = _installer(tmp_path, migration=migration)

    for _ in range(3):
        with pytest.raises(RuntimeInstallError, match="migration"):
            _ensure(installer, tmp_path / "2.0.0-2")

    with pytest.raises(RuntimeInstallError, match="temporarily blocked") as captured:
        _ensure(installer, tmp_path / "2.0.0-2")
    assert captured.value.code == "update_circuit_open"


def test_manual_rollback_switches_runtime_and_restores_matching_sqlite_generation(tmp_path: Path) -> None:
    database = _create_state(tmp_path)
    installer = _installer(tmp_path)
    _ensure(installer, tmp_path / "1.0.0-1")
    _ensure(installer, tmp_path / "2.0.0-2")

    pointer = installer.rollback()

    assert pointer.current_version == "1.0.0"
    assert pointer.previous_version == "2.0.0"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("SELECT value FROM test ORDER BY rowid").fetchall() == [
            ("original",),
            ("schema-1",),
        ]

    forward = installer.rollback()
    assert forward.current_version == "2.0.0"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)


def test_same_or_narrower_privilege_update_is_silent_but_expansion_is_structured(tmp_path: Path) -> None:
    verifier = _ExpansionVerifier()
    installer = _installer(tmp_path, verifier=verifier)
    _ensure(installer, tmp_path / "2.0.0-1")

    narrowed = _invoke(installer, tmp_path / "1.0.0-1")
    assert narrowed.version == "1.0.0"

    with pytest.raises(RuntimePrivilegeApprovalRequired) as captured:
        _invoke(installer, tmp_path / "2.0.0-1")
    challenge = captured.value
    assert challenge.old_manifest_hash is not None
    assert challenge.old_privilege_fingerprint != challenge.new_privilege_fingerprint
    assert challenge.diff_hash.startswith("sha256:")
    assert set(challenge.diff) == {"new", "old"}


def test_receipt_commit_is_atomic_with_pointer_and_replay_is_rejected(tmp_path: Path) -> None:
    migration = _Migration()
    installer = _installer(tmp_path, migration=migration, verifier=_ExpansionVerifier())
    _ensure(installer, tmp_path / "1.0.0-1")

    with pytest.raises(RuntimePrivilegeApprovalRequired) as captured:
        _invoke(installer, tmp_path / "2.0.0-2")
    receipt = _receipt_for_challenge(captured.value, receipt_id="b" * 64)

    migration.fail = True
    with pytest.raises(RuntimeInstallError, match="migration"):
        _invoke(installer, tmp_path / "2.0.0-2", privilege_approval=receipt)
    after_failure = json.loads((tmp_path / "runtime" / "current.json").read_bytes())
    assert [item["receiptId"] for item in after_failure["privilegeApprovalJournal"]] == ["a" * 64]

    migration.fail = False
    installed = _invoke(installer, tmp_path / "2.0.0-2", privilege_approval=receipt)
    assert [item.receipt_id for item in installed.pointer.privilege_approval_journal] == ["a" * 64, "b" * 64]

    installer.rollback()
    with pytest.raises(RuntimeInstallError) as replayed:
        _invoke(installer, tmp_path / "2.0.0-2", privilege_approval=receipt)
    assert replayed.value.code == "privilege_receipt_replayed"


def test_runtime_reference_count_prevents_one_vault_from_removing_shared_version(tmp_path: Path) -> None:
    installer = _installer(tmp_path)
    bundle = tmp_path / "1.0.0-1"
    _ensure(installer, bundle, owner="vault-a")
    _ensure(installer, bundle, owner="vault-b")

    assert installer.uninstall(owner_id="vault-a") == ()
    assert (tmp_path / "runtime" / "1.0.0").is_dir()
    assert installer.uninstall(owner_id="vault-b") == ("1.0.0",)
    assert not (tmp_path / "runtime" / "current.json").exists()


def test_preserve_uninstall_keeps_global_current_when_remaining_vault_references_previous_version(
    tmp_path: Path,
) -> None:
    installer = _installer(tmp_path)
    _ensure(installer, tmp_path / "1.0.0-1", owner="workspace:ws_b")
    _ensure(installer, tmp_path / "2.0.0-1", owner="workspace:ws_a")

    assert installer.uninstall(owner_id="workspace:ws_a") == ()

    pointer = json.loads((tmp_path / "runtime" / "current.json").read_text(encoding="utf-8"))
    references = json.loads((tmp_path / "runtime" / "runtime-references.json").read_text(encoding="utf-8"))
    assert pointer["currentVersion"] == "2.0.0"
    assert pointer["previousVersion"] == "1.0.0"
    assert references == {
        "references": {"1.0.0": ["workspace:ws_b"]},
        "schemaVersion": 2,
    }
    assert (tmp_path / "runtime" / "1.0.0").is_dir()
    assert (tmp_path / "runtime" / "2.0.0").is_dir()

    assert installer.uninstall(owner_id="workspace:ws_b") == ("1.0.0", "2.0.0")
    assert not (tmp_path / "runtime" / "current.json").exists()


def test_full_purge_requires_exact_second_confirmation(tmp_path: Path) -> None:
    purge = _Purge(tmp_path / "workspaces")
    installer = _installer(tmp_path, purge=purge)
    _ensure(installer, tmp_path / "1.0.0-1")
    (tmp_path / "workspaces").mkdir()

    with pytest.raises(RuntimeInstallError, match="confirmation"):
        installer.uninstall(owner_id="vault-a", purge_data=True, confirmation="yes")

    installer.uninstall(
        owner_id="vault-a",
        purge_data=True,
        confirmation="DELETE OFFERAGENT LOCAL DATA",
    )
    assert purge.called
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "workspaces").exists()


def test_full_purge_with_other_vault_reference_is_non_mutating(tmp_path: Path) -> None:
    installer = _installer(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    reference_payload = {
        "references": {"1.0.0": ["workspace:ws_a", "workspace:ws_b"]},
        "schemaVersion": 1,
    }
    reference_file = runtime / "runtime-references.json"
    reference_file.write_bytes(
        (json.dumps(reference_payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )

    with pytest.raises(RuntimeInstallError, match="other Vaults") as captured:
        installer.uninstall(
            owner_id="workspace:ws_a",
            purge_data=True,
            confirmation="DELETE OFFERAGENT LOCAL DATA",
        )

    assert captured.value.code == "runtime_still_referenced"
    assert json.loads(reference_file.read_text(encoding="utf-8")) == reference_payload
    assert not (tmp_path / "purge-journal.json").exists()


def test_stable_owner_atomically_replaces_current_live_legacy_owner_only(tmp_path: Path) -> None:
    installer = _installer(tmp_path, legacy_owner_is_active=lambda _pid: True)
    references = installer._references
    references.replace_owner("1.0.0", "obsidian-4321")
    references.replace_owner("1.0.0", "obsidian-9876")

    references.replace_owner(
        "1.0.0",
        "workspace:ws_1234",
        legacy_owner_id="obsidian-4321",
    )

    payload = json.loads((tmp_path / "runtime" / "runtime-references.json").read_text(encoding="utf-8"))
    assert payload["references"] == {"1.0.0": ["obsidian-9876", "workspace:ws_1234"]}
    assert payload["schemaVersion"] == 2


def test_reference_migration_prunes_all_proven_dead_legacy_pids_but_retains_unknown_and_live(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    reference_file = runtime / "runtime-references.json"
    reference_file.write_bytes(
        (
            json.dumps(
                {
                    "references": {
                        "1.0.0": ["obsidian-101", "obsidian-202", "workspace:ws_a"],
                        "2.0.0": ["obsidian-303"],
                    },
                    "schemaVersion": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )
    states: dict[int, bool | None] = {101: False, 202: True, 303: None}
    installer = _installer(tmp_path, legacy_owner_is_active=states.__getitem__)

    installer._references.replace_owner("2.0.0", "workspace:ws_b")

    payload = json.loads(reference_file.read_text(encoding="utf-8"))
    assert payload == {
        "references": {
            "1.0.0": ["obsidian-202", "workspace:ws_a"],
            "2.0.0": ["obsidian-303", "workspace:ws_b"],
        },
        "schemaVersion": 2,
    }


def test_purge_journal_is_canonical_path_free_and_preserves_unknown_local_siblings(tmp_path: Path) -> None:
    purge = _Purge(tmp_path / "workspaces")
    failure = _FailPurgeAt(RuntimePurgePhase.PREPARED)
    installer = _installer(tmp_path, purge=purge, purge_failure_injector=failure)
    _ensure(installer, tmp_path / "1.0.0-1")
    _create_local_purge_state(tmp_path)
    protected = [tmp_path / name for name in ("setup-payload", "plugin", "vault", "unknown-user-data")]
    for directory in protected:
        directory.mkdir()
        (directory / "keep").write_text("keep", encoding="utf-8")

    with pytest.raises(RuntimeInstallError) as captured:
        _purge(installer)
    assert captured.value.code == "injected_purge_crash"

    journal_path = tmp_path / "purge-journal.json"
    payload = journal_path.read_bytes()
    value = json.loads(payload)
    assert payload == (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    assert set(value) == {
        "journalHash",
        "operationId",
        "ownerId",
        "phase",
        "removedVersions",
        "runtimeRootHash",
        "schemaVersion",
        "workspacesRootHash",
    }
    assert value["ownerId"] == "vault-a"
    assert value["phase"] == "prepared"
    assert value["removedVersions"] == ["1.0.0"]
    assert len(value["operationId"]) == 32
    assert value["runtimeRootHash"].startswith("sha256:")
    assert value["workspacesRootHash"].startswith("sha256:")
    assert str(tmp_path) not in payload.decode("utf-8")
    assert tmp_path.as_posix() not in payload.decode("utf-8")
    assert "DELETE OFFERAGENT LOCAL DATA" not in payload.decode("utf-8")

    recovered = _installer(tmp_path, purge=purge)
    assert _purge(recovered) == ("1.0.0",)
    assert not journal_path.exists()
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "workspaces").exists()
    assert not (tmp_path / "workspace-registry.json").exists()
    assert not (tmp_path / "workspace-registry.json.lock").exists()
    assert not (tmp_path / f".workspace-registry.json.{'a' * 32}.tmp").exists()
    assert all(not (tmp_path / name).exists() for name in ("host", "logs", "config", "secrets", "skills"))
    assert all((directory / "keep").read_text(encoding="utf-8") == "keep" for directory in protected)


@pytest.mark.parametrize("phase", list(RuntimePurgePhase), ids=lambda phase: phase.value)
def test_purge_recovers_after_every_crash_checkpoint(tmp_path: Path, phase: RuntimePurgePhase) -> None:
    purge = _Purge(tmp_path / "workspaces")
    failure = _FailPurgeAt(phase)
    installer = _installer(tmp_path, purge=purge, purge_failure_injector=failure)
    _ensure(installer, tmp_path / "1.0.0-1")
    _create_local_purge_state(tmp_path)

    with pytest.raises(RuntimeInstallError) as captured:
        _purge(installer)
    assert captured.value.code == "injected_purge_crash"
    assert failure.triggered
    assert (tmp_path / "purge-journal.json").is_file()

    recovered = _installer(tmp_path, purge=purge)
    assert _purge(recovered) == ("1.0.0",)
    assert not (tmp_path / "purge-journal.json").exists()
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "workspaces").exists()
    assert purge.calls >= 1


def test_complete_journal_recovers_ack_loss_without_repeating_destructive_steps(tmp_path: Path) -> None:
    purge = _Purge(tmp_path / "workspaces")
    failure = _FailPurgeAt(RuntimePurgePhase.COMPLETE)
    installer = _installer(tmp_path, purge=purge, purge_failure_injector=failure)
    _ensure(installer, tmp_path / "1.0.0-1")
    _create_local_purge_state(tmp_path)

    with pytest.raises(RuntimeInstallError):
        _purge(installer)
    journal_path = tmp_path / "purge-journal.json"
    journal = json.loads(journal_path.read_bytes())
    assert journal["phase"] == "complete"
    operation_id = journal["operationId"]
    purge_calls = purge.calls

    recovered = _installer(tmp_path, purge=purge)
    assert _purge(recovered) == ("1.0.0",)
    assert purge.calls == purge_calls
    assert not journal_path.exists()

    assert _purge(_installer(tmp_path, purge=purge)) == ()
    assert not journal_path.exists()
    assert operation_id


def test_in_progress_purge_rejects_preserve_other_owner_install_and_rollback(tmp_path: Path) -> None:
    purge = _Purge(tmp_path / "workspaces")
    failure = _FailPurgeAt(RuntimePurgePhase.PREPARED)
    installer = _installer(tmp_path, purge=purge, purge_failure_injector=failure)
    bundle = tmp_path / "1.0.0-1"
    _ensure(installer, bundle)
    _create_local_purge_state(tmp_path)
    with pytest.raises(RuntimeInstallError):
        _purge(installer)
    journal_path = tmp_path / "purge-journal.json"
    journal_before = journal_path.read_bytes()
    references_before = (tmp_path / "runtime" / "runtime-references.json").read_bytes()

    resumed = _installer(tmp_path, purge=purge)
    with pytest.raises(RuntimeInstallError) as preserve:
        resumed.uninstall(owner_id="vault-a")
    assert preserve.value.code == "purge_in_progress"
    with pytest.raises(RuntimeInstallError) as other_owner:
        _purge(resumed, owner="vault-b")
    assert other_owner.value.code == "purge_owner_mismatch"
    with pytest.raises(RuntimeInstallError) as install:
        _ensure(resumed, bundle)
    assert install.value.code == "purge_in_progress"
    with pytest.raises(RuntimeInstallError) as rollback:
        resumed.rollback()
    assert rollback.value.code == "purge_in_progress"
    assert journal_path.read_bytes() == journal_before
    assert (tmp_path / "runtime" / "runtime-references.json").read_bytes() == references_before
    assert (tmp_path / "runtime" / "1.0.0").is_dir()
    assert (tmp_path / "workspaces").is_dir()


@pytest.mark.parametrize(
    ("tamper", "expected_code"),
    [
        ("noncanonical", "purge_journal_noncanonical"),
        ("phase", "purge_journal_tampered"),
        ("root_hash", "purge_journal_root_mismatch"),
    ],
)
def test_tampered_or_root_mismatched_purge_journal_fails_closed(
    tmp_path: Path,
    tamper: str,
    expected_code: str,
) -> None:
    purge = _Purge(tmp_path / "workspaces")
    failure = _FailPurgeAt(RuntimePurgePhase.PREPARED)
    installer = _installer(tmp_path, purge=purge, purge_failure_injector=failure)
    _ensure(installer, tmp_path / "1.0.0-1")
    _create_local_purge_state(tmp_path)
    with pytest.raises(RuntimeInstallError):
        _purge(installer)
    journal_path = tmp_path / "purge-journal.json"
    value = json.loads(journal_path.read_bytes())
    if tamper == "noncanonical":
        journal_path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    elif tamper == "phase":
        value["phase"] = "owner_released"
        journal_path.write_bytes((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
    else:
        value["runtimeRootHash"] = "sha256:" + "0" * 64
        journal_path.write_bytes((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())

    references_before = (tmp_path / "runtime" / "runtime-references.json").read_bytes()
    with pytest.raises(RuntimeInstallError) as captured:
        _purge(_installer(tmp_path, purge=purge))
    assert captured.value.code == expected_code
    assert (tmp_path / "runtime" / "runtime-references.json").read_bytes() == references_before
    assert (tmp_path / "runtime" / "1.0.0").is_dir()
    assert (tmp_path / "workspaces").is_dir()


def test_unknown_runtime_entry_blocks_purge_before_any_mutation(tmp_path: Path) -> None:
    installer = _installer(tmp_path)
    _ensure(installer, tmp_path / "1.0.0-1")
    _create_local_purge_state(tmp_path)
    protected = tmp_path / "runtime" / "setup-payload"
    protected.mkdir()
    (protected / "payload").write_text("keep", encoding="utf-8")
    references_before = (tmp_path / "runtime" / "runtime-references.json").read_bytes()

    with pytest.raises(RuntimeInstallError) as captured:
        _purge(installer)

    assert captured.value.code == "purge_unknown_runtime_entry"
    assert not (tmp_path / "purge-journal.json").exists()
    assert (tmp_path / "runtime" / "runtime-references.json").read_bytes() == references_before
    assert (tmp_path / "runtime" / "1.0.0").is_dir()
    assert (protected / "payload").read_text(encoding="utf-8") == "keep"
    assert (tmp_path / "workspaces").is_dir()
