"""Signed installer/bootstrap helper using the shared Runtime verification chain."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from offeragent_harness.storage.migrations import LATEST_SCHEMA_VERSION
from offeragent_harness.storage.sqlite import SqliteDatabase, SqliteStorageError

from .installation_ledger import (
    InstallationLedgerCoordinator,
    LedgerUninstallMode,
    LedgerUninstallResult,
    LedgerUninstallScope,
)
from .process_lock import ProcessAlreadyRunning, ProcessLock, host_mutex_name, installation_ledger_mutex_name
from .release_manifest import (
    ReleaseKeyring,
    ReleaseVerificationError,
    RuntimeBundleVerifier,
    SafeRuntimeZipExtractor,
    native_windows_architecture,
    parse_manifest,
)
from .release_privileges import RuntimePrivilegeError, parse_privilege_approval_receipt
from .runtime_installer import (
    RuntimeInstaller,
    RuntimeInstallError,
    RuntimeInstallPhase,
    RuntimeQuiescer,
    StateMigrationRunner,
    SubprocessRuntimeSelfTestRunner,
)
from .windows_appcontainer import WindowsAppContainerRuntimePurge
from .windows_authenticode import WindowsAuthenticodeVerifier

_MAXIMUM_REQUEST_BYTES = 64 * 1024
_MAXIMUM_RESULT_BYTES = 256 * 1024
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_INSTALLATION_ID = re.compile(r"^install_[0-9a-f]{32}$")
_OPERATION_ID = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,63}$")
_PROTOCOL = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_PURGE_CONFIRMATION = "DELETE OFFERAGENT LOCAL DATA"


class BootstrapCliError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class HostStoppedQuiescer(RuntimeQuiescer):
    """Prove no current-user Host can mutate state throughout activation."""

    @contextmanager
    def quiesce_for_update(self, *, timeout_seconds: float) -> Iterator[None]:
        if timeout_seconds <= 0:
            raise ValueError("quiesce timeout must be positive")
        lock = ProcessLock(host_mutex_name())
        try:
            lock.acquire(timeout_ms=0)
        except BaseException as error:
            raise RuntimeInstallError(
                "runtime_in_use",
                "Runtime update requires the current Host to stop cleanly before activation",
            ) from error
        try:
            yield
        finally:
            lock.release()


class LocalStateSchemaMigration(StateMigrationRunner):
    """Run the package's checksum-verified SQLite migration chain."""

    def migrate(self, runtime_root: Path, state_databases: Sequence[Path], target_schema: int) -> None:
        if not runtime_root.is_dir() or target_schema != LATEST_SCHEMA_VERSION:
            raise RuntimeInstallError("migration_candidate_invalid", "migration candidate is invalid")
        for database in state_databases:
            try:
                storage = SqliteDatabase(database)
                asyncio.run(storage.initialize())
                diagnostics = asyncio.run(storage.diagnostics())
                if diagnostics.schema_version != target_schema or diagnostics.journal_mode != "wal":
                    raise RuntimeInstallError("state_integrity_failed", "state database migration is incomplete")
            except (SqliteStorageError, OSError) as error:
                raise RuntimeInstallError("state_migration_failed", "local state migration failed") from error


class BootstrapApplication:
    def __init__(
        self,
        *,
        installer_factory: Callable[[Mapping[str, Any]], RuntimeInstaller],
        authenticode: WindowsAuthenticodeVerifier,
        ledger_factory: Callable[[], InstallationLedgerCoordinator] | None = None,
    ) -> None:
        self._installer_factory = installer_factory
        self._authenticode = authenticode
        self._ledger_factory = ledger_factory

    def verify_self(self) -> None:
        if not self._authenticode.verify(Path(sys.executable).resolve(strict=True)):
            raise BootstrapCliError("bootstrap_authenticode_invalid", "bootstrap Authenticode failed self-check")

    def install(self, request: Mapping[str, Any], emit: Callable[[Mapping[str, Any]], None]) -> Mapping[str, Any]:
        bundle_root = _local_absolute_directory(_required_text(request["bundleRoot"], "bundleRoot"))
        expected_manifest_hash = _required_hash(request["manifestHash"], "manifestHash")
        manifest_bytes = _read_bounded(bundle_root / "runtime-manifest.json", 8 * 1024 * 1024)
        actual_manifest_hash = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
        if actual_manifest_hash != expected_manifest_hash:
            raise BootstrapCliError("manifest_hash_mismatch", "bootstrap request manifest identity differs")
        self.verify_self()
        vault_root = Path(_required_local_path_text(request["vaultRoot"], "vaultRoot"))
        owner_id = _required_owner(request["ownerId"])
        plugin_version = _required_version(request["pluginVersion"], "pluginVersion")
        installer = self._installer_factory(request)
        approval_raw = request["privilegeApproval"]
        try:
            privilege_approval = None if approval_raw is None else parse_privilege_approval_receipt(approval_raw)
        except RuntimePrivilegeError as error:
            raise BootstrapCliError(error.code, str(error)) from error

        def progress(phase: RuntimeInstallPhase) -> None:
            if phase in {
                RuntimeInstallPhase.EXTRACTING_TO_STAGING,
                RuntimeInstallPhase.VERIFYING_EACH_FILE,
                RuntimeInstallPhase.ATOMIC_ACTIVATE,
                RuntimeInstallPhase.RUNTIME_SELF_TEST,
            }:
                emit({"phase": phase.value, "type": "progress"})

        result = installer.ensure_ready(
            bundle_root,
            plugin_version=plugin_version,
            protocol_version=_required_protocol(request["protocolVersion"], "protocolVersion"),
            schema_hash=_required_hash(request["schemaHash"], "schemaHash"),
            owner_id=owner_id,
            legacy_owner_id=_optional_legacy_owner(request["legacyOwnerId"]),
            expected_architecture=_native_windows_architecture(),
            windows_build=_windows_build(),
            progress=progress,
            privilege_approval=privilege_approval,
        )
        if self._ledger_factory is not None:
            self._ledger_factory().bind_runtime_owner(
                vault_root,
                owner_id=owner_id,
                plugin_version=plugin_version,
            )
        manifest = result.pointer
        host = result.root / "offeragent-host.exe"
        runtime_manifest = parse_manifest(manifest_bytes)
        if runtime_manifest.privilege_envelope is None:
            raise BootstrapCliError("privilege_envelope_missing", "installed Runtime privilege envelope is missing")
        return {
            "bootstrapAuthenticode": True,
            "hostExecutable": str(host.resolve(strict=True)),
            "manifestHash": manifest.manifest_hash,
            "protocolMaximum": runtime_manifest.protocol.maximum,
            "protocolMinimum": runtime_manifest.protocol.minimum,
            "privilegeFingerprint": runtime_manifest.privilege_envelope.fingerprint,
            "runtimeVersion": result.version,
            "schemaHash": runtime_manifest.protocol.schema_hash,
            "status": "ready",
            "type": "result",
        }

    def prepare_ledger_registration(self, request_id: str) -> None:
        self.verify_self()
        self._ledger().prepare_registration_request(request_id)

    def consume_ledger_registration(self, request_id: str) -> None:
        self.verify_self()
        self._ledger().consume_registration_request(request_id)

    def uninstall_from_ledger(
        self,
        *,
        operation_id: str,
        scope: LedgerUninstallScope,
        mode: LedgerUninstallMode,
        selected_installation_id: str | None,
        confirmation: str | None,
    ) -> LedgerUninstallResult:
        self.verify_self()
        return self._ledger().uninstall(
            operation_id=operation_id,
            scope=scope,
            mode=mode,
            selected_installation_id=selected_installation_id,
            confirmation=confirmation,
        )

    def validate_ledger_uninstall(
        self,
        *,
        scope: LedgerUninstallScope,
        mode: LedgerUninstallMode,
        selected_installation_id: str | None,
        confirmation: str | None,
    ) -> None:
        self.verify_self()
        self._ledger().validate_uninstall_request(
            scope=scope,
            mode=mode,
            selected_installation_id=selected_installation_id,
            confirmation=confirmation,
        )

    def _ledger(self) -> InstallationLedgerCoordinator:
        if self._ledger_factory is None:
            raise BootstrapCliError("installation_ledger_unavailable", "installer ledger is unavailable")
        return self._ledger_factory()

    def uninstall(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.verify_self()
        owner_id = _required_owner(request["ownerId"])
        purge_data = request["purgeData"]
        confirmation = request["confirmation"]
        if not isinstance(purge_data, bool):
            raise BootstrapCliError("request_invalid", "bootstrap purgeData is invalid")
        if confirmation is not None and not isinstance(confirmation, str):
            raise BootstrapCliError("request_invalid", "bootstrap confirmation is invalid")
        if purge_data:
            if confirmation != _PURGE_CONFIRMATION:
                raise BootstrapCliError("purge_confirmation_required", "full purge requires exact confirmation")
            mode = "purge_data"
        else:
            if confirmation is not None:
                raise BootstrapCliError("request_invalid", "preserve-data uninstall forbids confirmation")
            mode = "preserve_data"
        removed = self._installer_factory(request).uninstall(
            owner_id=owner_id,
            purge_data=purge_data,
            confirmation=confirmation,
        )
        if len(removed) > 1024 or any(_VERSION.fullmatch(version) is None for version in removed):
            raise BootstrapCliError("uninstall_result_invalid", "uninstall result is invalid")
        return {
            "mode": mode,
            "removedVersions": list(removed),
            "schemaVersion": 1,
            "status": "uninstalled",
            "type": "result",
        }


def create_production_application() -> BootstrapApplication:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not local_app_data:
        raise BootstrapCliError("windows_required", "bootstrap requires current-user Windows local state")
    keys = _embedded_release_keys()
    authenticode = WindowsAuthenticodeVerifier()
    verifier = RuntimeBundleVerifier(
        keyring=ReleaseKeyring(keys),
        authenticode=authenticode,
        require_authenticode=True,
    )

    def factory(request: Mapping[str, Any]) -> RuntimeInstaller:
        del request
        local_root = Path(local_app_data) / "OfferAgent"
        return RuntimeInstaller(
            runtime_root=local_root / "runtime",
            workspaces_root=local_root / "workspaces",
            verifier=verifier,
            extractor=SafeRuntimeZipExtractor(),
            self_test=SubprocessRuntimeSelfTestRunner(),
            migration=LocalStateSchemaMigration(),
            quiescer=HostStoppedQuiescer(),
            purge=WindowsAppContainerRuntimePurge(local_root / "workspaces"),
        )

    local_root = Path(local_app_data) / "OfferAgent"

    def runtime_uninstall(
        owner_id: str,
        *,
        purge_data: bool,
        confirmation: str | None,
    ) -> tuple[str, ...]:
        return factory({}).uninstall(
            owner_id=owner_id,
            purge_data=purge_data,
            confirmation=confirmation,
        )

    def ledger_factory() -> InstallationLedgerCoordinator:
        return InstallationLedgerCoordinator(
            ledger_path=local_root / "installer" / "vault-installations.json",
            runtime_uninstall=runtime_uninstall,
            lock_factory=lambda: ProcessLock(installation_ledger_mutex_name()),
        )

    return BootstrapApplication(
        installer_factory=factory,
        authenticode=authenticode,
        ledger_factory=ledger_factory,
    )


async def _stop_current_user_host(local_root: Path) -> str:
    """Use the authenticated broker without starting a replacement Host."""

    from .host_cli import HostCliApplication, HostControlBroker
    from .named_pipe import DiscoveryMaterial, DiscoveryMaterialStore
    from .windows_named_pipe import DpapiCurrentUserProtector

    class _UnusedEngine:
        async def start(self) -> None:
            raise AssertionError("stop-only client cannot start a Host")

        async def attach(self, vault_root: Path, *, client_id: str) -> DiscoveryMaterial:
            del vault_root, client_id
            raise AssertionError("stop-only client cannot attach a Vault")

        async def shutdown(self) -> None:
            raise AssertionError("stop-only client does not own the Host")

    def host_stopped() -> bool:
        lock = ProcessLock(host_mutex_name())
        try:
            lock.acquire(timeout_ms=0)
        except ProcessAlreadyRunning:
            return False
        else:
            lock.release()
            return True

    protector = DpapiCurrentUserProtector()
    engine = _UnusedEngine()
    control = HostControlBroker(
        engine=engine,
        material_store=DiscoveryMaterialStore(local_root / "host" / "control", protector=protector),
    )
    outcome = await HostCliApplication(
        engine=engine,
        control=control,
        host_stopped=host_stopped,
    ).stop_all()
    return outcome.status


def run_inno_ledger_uninstall_contract(
    application: BootstrapApplication,
    *,
    operation_id: str,
    scope: LedgerUninstallScope,
    mode: LedgerUninstallMode,
    selected_installation_id: str | None,
    confirmation: str | None,
) -> LedgerUninstallResult:
    """Stop the Host and execute an explicit, ID-only ledger uninstall."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not local_app_data:
        raise BootstrapCliError("windows_required", "bootstrap requires current-user Windows local state")
    asyncio.run(_stop_current_user_host((Path(local_app_data) / "OfferAgent").resolve(strict=False)))
    return application.uninstall_from_ledger(
        operation_id=operation_id,
        scope=scope,
        mode=mode,
        selected_installation_id=selected_installation_id,
        confirmation=confirmation,
    )


def run_install_contract(
    application: BootstrapApplication,
    *,
    request_fd: int,
    result_fd: int,
) -> None:
    if request_fd != 3 or result_fd != 4 or request_fd == result_fd:
        raise BootstrapCliError("fd_contract_invalid", "bootstrap requires fixed fd3/fd4")
    try:
        request = _read_request(request_fd)
        emitted = 0

        def emit(value: Mapping[str, Any]) -> None:
            nonlocal emitted
            payload = _canonical_json(value)
            emitted += len(payload)
            if emitted > _MAXIMUM_RESULT_BYTES:
                raise BootstrapCliError("result_limit", "bootstrap result stream exceeded its limit")
            _write_all(result_fd, payload)

        emit(application.install(request, emit))
    finally:
        os.close(result_fd)


def run_uninstall_contract(
    application: BootstrapApplication,
    *,
    request_fd: int,
    result_fd: int,
) -> None:
    if request_fd != 3 or result_fd != 4 or request_fd == result_fd:
        raise BootstrapCliError("fd_contract_invalid", "bootstrap requires fixed fd3/fd4")
    try:
        request = _read_uninstall_request(request_fd)
        payload = _canonical_json(application.uninstall(request))
        if len(payload) > _MAXIMUM_RESULT_BYTES:
            raise BootstrapCliError("result_limit", "bootstrap result stream exceeded its limit")
        _write_all(result_fd, payload)
    finally:
        os.close(result_fd)


def _read_request(descriptor: int) -> Mapping[str, Any]:
    payload = bytearray()
    try:
        while len(payload) <= _MAXIMUM_REQUEST_BYTES:
            chunk = os.read(descriptor, min(4096, _MAXIMUM_REQUEST_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.close(descriptor)
    try:
        if not payload or len(payload) > _MAXIMUM_REQUEST_BYTES:
            raise BootstrapCliError("request_size", "bootstrap request is outside limits")
        raw = cast(object, json.loads(payload.decode("utf-8", errors="strict")))
        value = _mapping(raw)
        expected = {
            "bundleRoot",
            "manifestHash",
            "legacyOwnerId",
            "ownerId",
            "pluginVersion",
            "privilegeApproval",
            "protocolVersion",
            "schemaHash",
            "schemaVersion",
            "vaultRoot",
        }
        if set(value) != expected or value["schemaVersion"] != 3 or _canonical_json(value) != payload:
            raise BootstrapCliError("request_invalid", "bootstrap request is not canonical")
        _required_text(value["bundleRoot"], "bundleRoot")
        _required_hash(value["manifestHash"], "manifestHash")
        _optional_legacy_owner(value["legacyOwnerId"])
        _required_owner(value["ownerId"])
        _required_version(value["pluginVersion"], "pluginVersion")
        _required_protocol(value["protocolVersion"], "protocolVersion")
        _required_hash(value["schemaHash"], "schemaHash")
        _required_local_path_text(value["vaultRoot"], "vaultRoot")
        approval = value["privilegeApproval"]
        if approval is not None:
            try:
                parse_privilege_approval_receipt(approval)
            except RuntimePrivilegeError as error:
                raise BootstrapCliError(error.code, str(error)) from error
        return value
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BootstrapCliError("request_invalid", "bootstrap request is malformed") from error
    finally:
        payload[:] = b"\0" * len(payload)


def _read_uninstall_request(descriptor: int) -> Mapping[str, Any]:
    payload = bytearray()
    try:
        while len(payload) <= _MAXIMUM_REQUEST_BYTES:
            chunk = os.read(descriptor, min(4096, _MAXIMUM_REQUEST_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.close(descriptor)
    try:
        if not payload or len(payload) > _MAXIMUM_REQUEST_BYTES:
            raise BootstrapCliError("request_size", "bootstrap request is outside limits")
        raw = cast(object, json.loads(payload.decode("utf-8", errors="strict")))
        value = _mapping(raw)
        expected = {"confirmation", "ownerId", "purgeData", "schemaVersion"}
        if set(value) != expected or value["schemaVersion"] != 1 or _canonical_json(value) != payload:
            raise BootstrapCliError("request_invalid", "bootstrap uninstall request is not canonical")
        _required_owner(value["ownerId"])
        if not isinstance(value["purgeData"], bool):
            raise BootstrapCliError("request_invalid", "bootstrap purgeData is invalid")
        confirmation = value["confirmation"]
        if value["purgeData"]:
            if confirmation != _PURGE_CONFIRMATION:
                raise BootstrapCliError("purge_confirmation_required", "full purge requires exact confirmation")
        elif confirmation is not None:
            raise BootstrapCliError("request_invalid", "preserve-data uninstall forbids confirmation")
        return value
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BootstrapCliError("request_invalid", "bootstrap uninstall request is malformed") from error
    finally:
        payload[:] = b"\0" * len(payload)


def _embedded_release_keys() -> Mapping[str, bytes]:
    """Load trust roots frozen into the signed bootstrap executable."""

    try:
        module = importlib.import_module("offeragent_harness._release_keys")
        encoded = module.PUBLIC_KEYS_BASE64URL
    except (ImportError, AttributeError) as error:
        raise BootstrapCliError("release_keys_missing", "signed bootstrap has no embedded release keys") from error
    if not isinstance(encoded, dict) or not encoded:
        raise BootstrapCliError("release_keys_invalid", "embedded release keyring is invalid")
    result: dict[str, bytes] = {}
    for key_id, value in encoded.items():
        if not isinstance(key_id, str) or not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            raise BootstrapCliError("release_keys_invalid", "embedded release keyring is invalid")
        padding = "=" * ((4 - len(value) % 4) % 4)
        try:
            raw = base64.urlsafe_b64decode(value + padding)
        except ValueError as error:
            raise BootstrapCliError("release_keys_invalid", "embedded release key is malformed") from error
        if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
            raise BootstrapCliError("release_keys_invalid", "embedded release key is non-canonical")
        result[key_id] = raw
    return result


def parse_arguments(arguments: Sequence[str]) -> tuple[str, int, int]:
    actual = list(arguments)
    if actual == ["install", "--request-fd", "3", "--result-fd", "4"]:
        return "install", 3, 4
    if actual == ["uninstall", "--request-fd", "3", "--result-fd", "4"]:
        return "uninstall", 3, 4
    raise BootstrapCliError("arguments_invalid", "bootstrap supports only fixed install/uninstall fd contracts")


def parse_inno_ledger_arguments(
    arguments: Sequence[str],
) -> tuple[
    str,
    str | None,
    str | None,
    LedgerUninstallScope | None,
    LedgerUninstallMode | None,
    str | None,
    str | None,
]:
    actual = list(arguments)
    if len(actual) == 4 and actual[:3] == ["inno-ledger", "prepare-registration", "--request-id"]:
        return "prepare", _required_operation_id(actual[3], "requestId"), None, None, None, None, None
    if len(actual) == 4 and actual[:3] == ["inno-ledger", "register", "--request-id"]:
        return "register", _required_operation_id(actual[3], "requestId"), None, None, None, None, None
    if (
        len(actual) == 8
        and actual[:2] == ["inno-ledger", "validate-uninstall"]
        and actual[2:5] == ["--scope", "selected", "--installation-id"]
        and actual[6:] == ["--mode", "preserve-data"]
    ):
        return (
            "validate",
            None,
            None,
            LedgerUninstallScope.SELECTED,
            LedgerUninstallMode.PRESERVE_DATA,
            _required_installation_id(actual[5]),
            None,
        )
    if (
        len(actual) == 8
        and actual[:2] == ["inno-ledger", "validate-uninstall"]
        and actual[2:6] == ["--scope", "all", "--mode", "purge-data"]
        and actual[6:] == ["--confirmation", _PURGE_CONFIRMATION]
    ):
        return (
            "validate",
            None,
            None,
            LedgerUninstallScope.ALL,
            LedgerUninstallMode.PURGE_DATA,
            None,
            _PURGE_CONFIRMATION,
        )
    prefix = ["inno-ledger", "uninstall", "--operation-id"]
    if len(actual) == 10 and actual[:3] == prefix and actual[4:7] == ["--scope", "selected", "--installation-id"]:
        if actual[8:] != ["--mode", "preserve-data"]:
            raise BootstrapCliError("arguments_invalid", "selected uninstall arguments are invalid")
        return (
            "uninstall",
            None,
            _required_operation_id(actual[3], "operationId"),
            LedgerUninstallScope.SELECTED,
            LedgerUninstallMode.PRESERVE_DATA,
            _required_installation_id(actual[7]),
            None,
        )
    if len(actual) == 8 and actual[:3] == prefix and actual[4:] == ["--scope", "all", "--mode", "preserve-data"]:
        return (
            "uninstall",
            None,
            _required_operation_id(actual[3], "operationId"),
            LedgerUninstallScope.ALL,
            LedgerUninstallMode.PRESERVE_DATA,
            None,
            None,
        )
    if (
        len(actual) == 10
        and actual[:3] == prefix
        and actual[4:8] == ["--scope", "all", "--mode", "purge-data"]
        and actual[8:] == ["--confirmation", _PURGE_CONFIRMATION]
    ):
        return (
            "uninstall",
            None,
            _required_operation_id(actual[3], "operationId"),
            LedgerUninstallScope.ALL,
            LedgerUninstallMode.PURGE_DATA,
            None,
            _PURGE_CONFIRMATION,
        )
    raise BootstrapCliError("arguments_invalid", "bootstrap Inno ledger arguments are invalid")


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        actual = list(sys.argv[1:] if arguments is None else arguments)
        application = create_production_application()
        if actual[:1] == ["inno-ledger"]:
            action, request_id, operation_id, scope, mode, installation_id, confirmation = parse_inno_ledger_arguments(
                actual
            )
            if action == "prepare":
                assert request_id is not None
                application.prepare_ledger_registration(request_id)
            elif action == "register":
                assert request_id is not None
                application.consume_ledger_registration(request_id)
            elif action == "validate":
                assert scope is not None and mode is not None
                application.validate_ledger_uninstall(
                    scope=scope,
                    mode=mode,
                    selected_installation_id=installation_id,
                    confirmation=confirmation,
                )
            else:
                assert operation_id is not None and scope is not None and mode is not None
                run_inno_ledger_uninstall_contract(
                    application,
                    operation_id=operation_id,
                    scope=scope,
                    mode=mode,
                    selected_installation_id=installation_id,
                    confirmation=confirmation,
                )
        else:
            operation, request_fd, result_fd = parse_arguments(actual)
            if operation == "install":
                run_install_contract(application, request_fd=request_fd, result_fd=result_fd)
            else:
                run_uninstall_contract(application, request_fd=request_fd, result_fd=result_fd)
    except BaseException:
        try:
            os.write(2, b"offeragent-bootstrap: operation failed\n")
        except OSError:
            pass
        return 2
    return 0


def _local_absolute_directory(value: str) -> Path:
    if "\x00" in value or "\r" in value or "\n" in value or value.startswith(("\\\\", "//")):
        raise BootstrapCliError("bundle_root_invalid", "bundle root must be a local path")
    path = Path(value)
    if not path.is_absolute():
        raise BootstrapCliError("bundle_root_invalid", "bundle root must be absolute")
    try:
        canonical = path.resolve(strict=True)
    except OSError as error:
        raise BootstrapCliError("bundle_root_unavailable", "bundle root is unavailable") from error
    if not canonical.is_dir():
        raise BootstrapCliError("bundle_root_invalid", "bundle root is not a directory")
    return canonical


def _read_bounded(path: Path, maximum: int) -> bytes:
    try:
        with path.open("rb", buffering=0) as stream:
            payload = stream.read(maximum + 1)
    except OSError as error:
        raise BootstrapCliError("release_file_unavailable", "release file is unavailable") from error
    if not payload or len(payload) > maximum:
        raise BootstrapCliError("release_file_size", "release file exceeds limits")
    return payload


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written < 1:
            raise BootstrapCliError("result_write_failed", "bootstrap result fd made no progress")
        view = view[written:]


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise BootstrapCliError("request_invalid", "bootstrap request must be an object")
    return value


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise BootstrapCliError("request_invalid", f"bootstrap {label} is invalid")
    return value


def _required_hash(value: object, label: str) -> str:
    text = _required_text(value, label)
    if _SHA256.fullmatch(text) is None:
        raise BootstrapCliError("request_invalid", f"bootstrap {label} is invalid")
    return text


def _required_owner(value: object) -> str:
    text = _required_text(value, "ownerId")
    if _OWNER.fullmatch(text) is None:
        raise BootstrapCliError("request_invalid", "bootstrap ownerId is invalid")
    return text


def _optional_legacy_owner(value: object) -> str | None:
    if value is None:
        return None
    text = _required_text(value, "legacyOwnerId")
    if re.fullmatch(r"obsidian-[1-9][0-9]{0,19}", text) is None:
        raise BootstrapCliError("request_invalid", "bootstrap legacyOwnerId is invalid")
    return text


def _required_version(value: object, label: str) -> str:
    text = _required_text(value, label)
    if _VERSION.fullmatch(text) is None:
        raise BootstrapCliError("request_invalid", f"bootstrap {label} is invalid")
    return text


def _required_protocol(value: object, label: str) -> str:
    text = _required_text(value, label)
    if _PROTOCOL.fullmatch(text) is None:
        raise BootstrapCliError("request_invalid", f"bootstrap {label} is invalid")
    return text


def _required_local_path_text(value: object, label: str) -> str:
    text = _required_text(value, label)
    if (
        "\x00" in text
        or "\r" in text
        or "\n" in text
        or text.startswith(("\\\\", "//"))
        or not Path(text).is_absolute()
    ):
        raise BootstrapCliError("request_invalid", f"bootstrap {label} must be an absolute local path")
    return text


def _required_operation_id(value: object, label: str) -> str:
    text = _required_text(value, label)
    if _OPERATION_ID.fullmatch(text) is None:
        raise BootstrapCliError("arguments_invalid", f"bootstrap {label} is invalid")
    return text


def _required_installation_id(value: object) -> str:
    text = _required_text(value, "installationId")
    if _INSTALLATION_ID.fullmatch(text) is None:
        raise BootstrapCliError("arguments_invalid", "bootstrap installationId is invalid")
    return text


def _windows_build() -> int:
    if os.name != "nt":
        raise BootstrapCliError("platform_unsupported", "bootstrap requires native Windows x64 or arm64")
    getter = getattr(sys, "getwindowsversion", None)
    if getter is None:
        raise BootstrapCliError("platform_unsupported", "Windows build identity is unavailable")
    return int(getter().build)


def _native_windows_architecture() -> str:
    try:
        return native_windows_architecture()
    except ReleaseVerificationError as error:
        raise BootstrapCliError(error.code, str(error)) from error


__all__ = [
    "BootstrapApplication",
    "BootstrapCliError",
    "HostStoppedQuiescer",
    "LocalStateSchemaMigration",
    "create_production_application",
    "main",
    "parse_arguments",
    "parse_inno_ledger_arguments",
    "run_inno_ledger_uninstall_contract",
    "run_install_contract",
    "run_uninstall_contract",
]
