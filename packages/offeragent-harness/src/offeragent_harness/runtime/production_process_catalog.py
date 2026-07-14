"""Release-manifest-bound production process, environment and Shell profiles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import ProcessStdinMode
from offeragent_harness.shell import ShellCommandProfile
from offeragent_harness.tools import SideEffectClass

from .process_supervisor import (
    ExecutableTrust,
    ProcessEnvironmentProfile,
    ProcessExecutableProfile,
    ProcessFilesystemAccess,
    ProcessFilesystemCapability,
)
from .release_manifest import ReleaseKeyring, ReleaseVerificationError, RuntimeFileRecord
from .release_trust import (
    InstalledReleaseManifestTrust,
    InstalledReleaseProcessManifestTrust,
    load_embedded_release_keys,
)

if TYPE_CHECKING:
    from .development_runtime_manifest import InstalledDevelopmentRuntimeTrust

PROCESS_CATALOG_PATH = "process-catalog.v1.json"

_MAX_CATALOG_BYTES = 512 * 1024
_MAX_ENVIRONMENT_PROFILES = 32
_MAX_EXECUTABLE_PROFILES = 64
_MAX_SHELL_PROFILES = 64
_MAX_ARGUMENTS = 128
_MAX_CAPABILITIES = 64
_MAX_ENVIRONMENT_NAMES = 128
_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SHELL_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class ProductionProcessCatalogError(RuntimeError):
    """The activated release does not contain a safe canonical process catalog."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProductionProcessCatalog:
    runtime_root: Path
    catalog_hash: str
    executable_profiles: tuple[ProcessExecutableProfile, ...]
    environment_profiles: tuple[ProcessEnvironmentProfile, ...]
    signed_shell_profiles: tuple[ShellCommandProfile, ...]
    manifest_trust: InstalledReleaseProcessManifestTrust | None

    def __post_init__(self) -> None:
        if not self.executable_profiles or not self.environment_profiles or not self.signed_shell_profiles:
            raise ValueError("production process catalog cannot omit its base profiles")
        if any(profile.allow_network for profile in self.executable_profiles):
            raise ValueError("production local process profiles cannot authorize network access")
        if any(profile.allow_network for profile in self.signed_shell_profiles):
            raise ValueError("production Shell profiles cannot authorize network access")


@dataclass(frozen=True, slots=True)
class _ExecutableSpec:
    executable_id: str
    relative_path: str
    fixed_arguments: tuple[str, ...]
    minimum_variable_arguments: int
    maximum_variable_arguments: int
    variable_argument_pattern: str
    environment_profile_ids: frozenset[str]
    allowed_stdin_modes: frozenset[ProcessStdinMode]
    allowed_cwd_root_ids: frozenset[str]
    appcontainer_filesystem: tuple[ProcessFilesystemCapability, ...]


@dataclass(frozen=True, slots=True)
class _ShellSpec:
    profile_id: str
    description: str
    executable_id: str
    fixed_arguments: tuple[str, ...]
    minimum_variable_arguments: int
    maximum_variable_arguments: int
    variable_argument_pattern: str
    cwd_root_id: str
    environment_profile_id: str
    environment_allowlist: frozenset[str]
    environment: Mapping[str, str]
    timeout_ms: int
    inline_output_limit_bytes: int
    artifact_output_limit_bytes: int
    risk: RiskClass
    side_effect_class: SideEffectClass
    concurrency_safe: bool
    idempotent: bool
    retryable: bool
    version: str


def load_production_process_catalog(
    runtime_root: Path,
    *,
    manifest_trust: InstalledReleaseManifestTrust | None = None,
) -> ProductionProcessCatalog:
    """Load only profiles authorized by the activated signed Runtime manifest."""

    try:
        root = Path(runtime_root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Runtime root is not a directory")
        release = manifest_trust or InstalledReleaseManifestTrust(
            root,
            keyring=ReleaseKeyring(load_embedded_release_keys()),
        )
        if release.version_directory.resolve(strict=True) != root:
            raise ValueError("release manifest belongs to another Runtime root")
        catalog_record = release.manifest.by_path.get(PROCESS_CATALOG_PATH)
        if catalog_record is None or catalog_record.kind != "asset":
            raise ValueError("release manifest has no process catalog asset")
        payload = _read_manifest_asset(root, catalog_record, maximum=_MAX_CATALOG_BYTES)
        environments, executable_specs, shell_specs = _validated_catalog_specs(payload)
        environment_by_id = {item.profile_id: item for item in environments}
        process_manifest_trust = InstalledReleaseProcessManifestTrust(
            release,
            executable_bindings={item.executable_id: item.relative_path for item in executable_specs},
        )
        executables = tuple(
            _build_executable_profile(root, spec, process_manifest_trust, environment_by_id)
            for spec in executable_specs
        )
        executable_by_id = {item.executable_id: item for item in executables}
        shells = tuple(_build_shell_profile(spec, executable_by_id, environment_by_id) for spec in shell_specs)
        return ProductionProcessCatalog(
            runtime_root=root,
            catalog_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            executable_profiles=executables,
            environment_profiles=environments,
            signed_shell_profiles=shells,
            manifest_trust=process_manifest_trust,
        )
    except ProductionProcessCatalogError:
        raise
    except (KeyError, OSError, TypeError, ValueError, ReleaseVerificationError) as error:
        raise ProductionProcessCatalogError(
            "process_catalog_invalid",
            "activated signed process catalog is invalid",
        ) from error


def load_development_process_catalog(
    runtime_root: Path,
    *,
    manifest_trust: InstalledDevelopmentRuntimeTrust,
) -> ProductionProcessCatalog:
    """Load the same bounded catalog with explicit fixed-hash development trust.

    This function is never selected by the production Worker entry point.  The
    development manifest has already pinned the complete tree; each executable
    profile additionally captures and rechecks its SHA-256/opened-file identity
    immediately before process creation.
    """

    try:
        root = Path(runtime_root).resolve(strict=True)
        if manifest_trust.version_directory.resolve(strict=True) != root:
            raise ValueError("development manifest belongs to another Runtime root")
        catalog_record = manifest_trust.manifest.by_path.get(PROCESS_CATALOG_PATH)
        if catalog_record is None or catalog_record.kind != "asset":
            raise ValueError("development manifest has no process catalog asset")
        payload = _read_manifest_asset(root, catalog_record, maximum=_MAX_CATALOG_BYTES)
        environments, executable_specs, shell_specs = _validated_catalog_specs(payload)
        environment_by_id = {item.profile_id: item for item in environments}
        executables = tuple(
            _build_fixed_hash_executable_profile(root, spec, manifest_trust, environment_by_id)
            for spec in executable_specs
        )
        executable_by_id = {item.executable_id: item for item in executables}
        shells = tuple(_build_shell_profile(spec, executable_by_id, environment_by_id) for spec in shell_specs)
        return ProductionProcessCatalog(
            runtime_root=root,
            catalog_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            executable_profiles=executables,
            environment_profiles=environments,
            signed_shell_profiles=shells,
            manifest_trust=None,
        )
    except ProductionProcessCatalogError:
        raise
    except (KeyError, OSError, TypeError, ValueError, ReleaseVerificationError) as error:
        raise ProductionProcessCatalogError(
            "development_process_catalog_invalid",
            "hash-pinned development process catalog is invalid",
        ) from error


def validate_process_catalog_payload(payload: bytes) -> str:
    """Validate one release catalog before it is copied or trusted.

    This helper intentionally has no filesystem or manifest authority.  Release
    build and independent audit code use it to reject malformed, non-canonical,
    duplicate-key, capability-escalating catalogs before a Worker can start.
    The returned digest is suitable for deterministic build/audit evidence.
    """

    _validated_catalog_specs(bytes(payload))
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _validated_catalog_specs(
    payload: bytes,
) -> tuple[
    tuple[ProcessEnvironmentProfile, ...],
    tuple[_ExecutableSpec, ...],
    tuple[_ShellSpec, ...],
]:
    raw = _parse_canonical_document(payload)
    environments = _environment_profiles(raw["environmentProfiles"])
    executable_specs = _executable_specs(raw["executableProfiles"])
    shell_specs = _shell_specs(raw["signedShellProfiles"])
    environment_by_id = {item.profile_id: item for item in environments}
    executable_by_id = {item.executable_id: item for item in executable_specs}
    for executable in executable_specs:
        if not executable.environment_profile_ids <= environment_by_id.keys():
            raise ProductionProcessCatalogError(
                "process_catalog_environment",
                "executable profile references an unknown environment profile",
            )
    for shell in shell_specs:
        _validate_shell_spec_binding(shell, executable_by_id, environment_by_id)
    return environments, executable_specs, shell_specs


def _parse_canonical_document(payload: bytes) -> Mapping[str, Any]:
    if not payload or len(payload) > _MAX_CATALOG_BYTES:
        raise ProductionProcessCatalogError("process_catalog_size", "process catalog size is invalid")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProductionProcessCatalogError("process_catalog_malformed", "process catalog is malformed") from error
    document = _mapping(value, "process catalog")
    _exact_keys(
        document,
        {"environmentProfiles", "executableProfiles", "schemaVersion", "signedShellProfiles"},
        "process catalog",
    )
    if _integer(document["schemaVersion"], "schemaVersion", minimum=1, maximum=1) != 1:
        raise ProductionProcessCatalogError("process_catalog_schema", "process catalog schema is unsupported")
    canonical = (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if canonical != payload:
        raise ProductionProcessCatalogError(
            "process_catalog_noncanonical",
            "process catalog is not canonical JSON",
        )
    return document


def _environment_profiles(value: object) -> tuple[ProcessEnvironmentProfile, ...]:
    values = _bounded_sequence(value, "environmentProfiles", maximum=_MAX_ENVIRONMENT_PROFILES, minimum=1)
    profiles: list[ProcessEnvironmentProfile] = []
    for item in values:
        raw = _mapping(item, "environment profile")
        _exact_keys(raw, {"allowedNames", "allowedSecretNames", "profileId"}, "environment profile")
        profile_id = _profile_id(raw["profileId"], "environment profileId")
        allowed = _environment_names(raw["allowedNames"], "allowedNames")
        secret = _environment_names(raw["allowedSecretNames"], "allowedSecretNames")
        profiles.append(ProcessEnvironmentProfile(profile_id, frozenset(allowed), frozenset(secret)))
    _require_sorted_unique((item.profile_id for item in profiles), "environmentProfiles")
    return tuple(profiles)


def _executable_specs(value: object) -> tuple[_ExecutableSpec, ...]:
    values = _bounded_sequence(value, "executableProfiles", maximum=_MAX_EXECUTABLE_PROFILES, minimum=1)
    specs: list[_ExecutableSpec] = []
    expected = {
        "allowedCwdRootIds",
        "allowedStdinModes",
        "allowNetwork",
        "allowShellMetacharacters",
        "appContainerFilesystem",
        "environmentProfileIds",
        "executableId",
        "fixedArguments",
        "maximumVariableArguments",
        "minimumVariableArguments",
        "relativePath",
        "trust",
        "variableArgumentPattern",
    }
    for item in values:
        raw = _mapping(item, "executable profile")
        _exact_keys(raw, expected, "executable profile")
        if _text(raw["trust"], "trust", maximum=32) != ExecutableTrust.SIGNED_RELEASE.value:
            raise ProductionProcessCatalogError(
                "process_catalog_trust",
                "production executable profiles must use signed_release trust",
            )
        if _boolean(raw["allowNetwork"], "allowNetwork"):
            raise ProductionProcessCatalogError(
                "process_catalog_network",
                "production local process profiles cannot authorize network access",
            )
        if _boolean(raw["allowShellMetacharacters"], "allowShellMetacharacters"):
            raise ProductionProcessCatalogError(
                "process_catalog_shell_syntax",
                "production process catalog cannot authorize shell metacharacters",
            )
        minimum = _integer(raw["minimumVariableArguments"], "minimumVariableArguments", minimum=0, maximum=128)
        maximum = _integer(raw["maximumVariableArguments"], "maximumVariableArguments", minimum=0, maximum=128)
        if minimum > maximum:
            raise ProductionProcessCatalogError("process_catalog_arguments", "process argv bounds are inverted")
        capabilities = _filesystem_capabilities(raw["appContainerFilesystem"])
        environment_ids = _profile_ids(raw["environmentProfileIds"], "environmentProfileIds")
        cwd_ids = _profile_ids(raw["allowedCwdRootIds"], "allowedCwdRootIds")
        stdin_values = _text_list(
            raw["allowedStdinModes"],
            "allowedStdinModes",
            maximum=3,
            item_maximum=32,
            minimum=1,
        )
        _require_sorted_unique(stdin_values, "allowedStdinModes")
        try:
            stdin_modes = frozenset(ProcessStdinMode(item) for item in stdin_values)
        except ValueError as error:
            raise ProductionProcessCatalogError(
                "process_catalog_stdin",
                "process catalog stdin mode is unsupported",
            ) from error
        specs.append(
            _ExecutableSpec(
                executable_id=_profile_id(raw["executableId"], "executableId"),
                relative_path=_runtime_relative_path(raw["relativePath"]),
                fixed_arguments=tuple(_arguments(raw["fixedArguments"], "fixedArguments")),
                minimum_variable_arguments=minimum,
                maximum_variable_arguments=maximum,
                variable_argument_pattern=_pattern(raw["variableArgumentPattern"], "variableArgumentPattern"),
                environment_profile_ids=frozenset(environment_ids),
                allowed_stdin_modes=stdin_modes,
                allowed_cwd_root_ids=frozenset(cwd_ids),
                appcontainer_filesystem=capabilities,
            )
        )
    _require_sorted_unique((item.executable_id for item in specs), "executableProfiles")
    return tuple(specs)


def _shell_specs(value: object) -> tuple[_ShellSpec, ...]:
    values = _bounded_sequence(value, "signedShellProfiles", maximum=_MAX_SHELL_PROFILES, minimum=1)
    specs: list[_ShellSpec] = []
    expected = {
        "allowNetwork",
        "artifactOutputLimitBytes",
        "concurrencySafe",
        "cwdRootId",
        "description",
        "environment",
        "environmentAllowlist",
        "environmentProfileId",
        "executableId",
        "fixedArguments",
        "idempotent",
        "inlineOutputLimitBytes",
        "maximumVariableArguments",
        "minimumVariableArguments",
        "profileId",
        "retryable",
        "risk",
        "sideEffectClass",
        "timeoutMs",
        "variableArgumentPattern",
        "version",
    }
    for item in values:
        raw = _mapping(item, "signed Shell profile")
        _exact_keys(raw, expected, "signed Shell profile")
        if _boolean(raw["allowNetwork"], "allowNetwork"):
            raise ProductionProcessCatalogError(
                "process_catalog_network",
                "production Shell profiles cannot authorize network access",
            )
        minimum = _integer(raw["minimumVariableArguments"], "minimumVariableArguments", minimum=0, maximum=128)
        maximum = _integer(raw["maximumVariableArguments"], "maximumVariableArguments", minimum=0, maximum=128)
        if minimum > maximum:
            raise ProductionProcessCatalogError("process_catalog_arguments", "Shell argv bounds are inverted")
        environment_allowlist = _environment_names(raw["environmentAllowlist"], "environmentAllowlist")
        environment = _text_mapping(raw["environment"], "environment", maximum=_MAX_ENVIRONMENT_NAMES)
        try:
            risk = RiskClass(_text(raw["risk"], "risk", maximum=32))
            side_effect = SideEffectClass(_text(raw["sideEffectClass"], "sideEffectClass", maximum=32))
        except ValueError as error:
            raise ProductionProcessCatalogError(
                "process_catalog_risk",
                "signed Shell profile risk is invalid",
            ) from error
        version = _text(raw["version"], "version", maximum=64)
        if _VERSION.fullmatch(version) is None:
            raise ProductionProcessCatalogError("process_catalog_version", "Shell profile version is invalid")
        specs.append(
            _ShellSpec(
                profile_id=_shell_profile_id(raw["profileId"]),
                description=_text(raw["description"], "description", maximum=512, minimum=1),
                executable_id=_profile_id(raw["executableId"], "executableId"),
                fixed_arguments=tuple(_arguments(raw["fixedArguments"], "fixedArguments")),
                minimum_variable_arguments=minimum,
                maximum_variable_arguments=maximum,
                variable_argument_pattern=_pattern(raw["variableArgumentPattern"], "variableArgumentPattern"),
                cwd_root_id=_profile_id(raw["cwdRootId"], "cwdRootId"),
                environment_profile_id=_profile_id(raw["environmentProfileId"], "environmentProfileId"),
                environment_allowlist=frozenset(environment_allowlist),
                environment=MappingProxyType(environment),
                timeout_ms=_integer(raw["timeoutMs"], "timeoutMs", minimum=1, maximum=3_600_000),
                inline_output_limit_bytes=_integer(
                    raw["inlineOutputLimitBytes"],
                    "inlineOutputLimitBytes",
                    minimum=1,
                    maximum=64 * 1024 * 1024,
                ),
                artifact_output_limit_bytes=_integer(
                    raw["artifactOutputLimitBytes"],
                    "artifactOutputLimitBytes",
                    minimum=1,
                    maximum=64 * 1024 * 1024,
                ),
                risk=risk,
                side_effect_class=side_effect,
                concurrency_safe=_boolean(raw["concurrencySafe"], "concurrencySafe"),
                idempotent=_boolean(raw["idempotent"], "idempotent"),
                retryable=_boolean(raw["retryable"], "retryable"),
                version=version,
            )
        )
    _require_sorted_unique((item.profile_id for item in specs), "signedShellProfiles")
    return tuple(specs)


def _build_executable_profile(
    root: Path,
    spec: _ExecutableSpec,
    trust: InstalledReleaseProcessManifestTrust,
    environments: Mapping[str, ProcessEnvironmentProfile],
) -> ProcessExecutableProfile:
    if not spec.environment_profile_ids <= environments.keys():
        raise ProductionProcessCatalogError(
            "process_catalog_environment",
            "executable profile references an unknown environment profile",
        )
    record = trust.record_for(spec.executable_id)
    executable = _manifest_path(root, record)
    profile = ProcessExecutableProfile(
        executable_id=spec.executable_id,
        executable=executable,
        fixed_root=root,
        trust=ExecutableTrust.SIGNED_RELEASE,
        file_sha256=record.sha256,
        fixed_arguments=spec.fixed_arguments,
        minimum_variable_arguments=spec.minimum_variable_arguments,
        maximum_variable_arguments=spec.maximum_variable_arguments,
        variable_argument_pattern=spec.variable_argument_pattern,
        allow_shell_metacharacters=False,
        environment_profiles=spec.environment_profile_ids,
        allowed_stdin_modes=spec.allowed_stdin_modes,
        allowed_cwd_roots=spec.allowed_cwd_root_ids,
        allow_network=False,
        appcontainer_filesystem=spec.appcontainer_filesystem,
    )
    if profile.captured_content_sha256 != record.sha256 or not trust.authorizes(profile):
        raise ProductionProcessCatalogError(
            "process_catalog_executable_drift",
            "process executable differs from its signed release identity",
        )
    return profile


def _build_fixed_hash_executable_profile(
    root: Path,
    spec: _ExecutableSpec,
    trust: InstalledDevelopmentRuntimeTrust,
    environments: Mapping[str, ProcessEnvironmentProfile],
) -> ProcessExecutableProfile:
    if not spec.environment_profile_ids <= environments.keys():
        raise ProductionProcessCatalogError(
            "process_catalog_environment",
            "executable profile references an unknown environment profile",
        )
    record = trust.manifest.by_path.get(spec.relative_path)
    if record is None or record.kind != "executable" or not trust.verify_file(root / spec.relative_path):
        raise ProductionProcessCatalogError(
            "development_process_executable_drift",
            "development process executable differs from its pinned manifest identity",
        )
    executable = _manifest_path(root, record)
    profile = ProcessExecutableProfile(
        executable_id=spec.executable_id,
        executable=executable,
        fixed_root=root,
        trust=ExecutableTrust.FIXED_HASH,
        file_sha256=record.sha256,
        fixed_arguments=spec.fixed_arguments,
        minimum_variable_arguments=spec.minimum_variable_arguments,
        maximum_variable_arguments=spec.maximum_variable_arguments,
        variable_argument_pattern=spec.variable_argument_pattern,
        allow_shell_metacharacters=False,
        environment_profiles=spec.environment_profile_ids,
        allowed_stdin_modes=spec.allowed_stdin_modes,
        allowed_cwd_roots=spec.allowed_cwd_root_ids,
        allow_network=False,
        appcontainer_filesystem=spec.appcontainer_filesystem,
    )
    if profile.captured_content_sha256 != record.sha256:
        raise ProductionProcessCatalogError(
            "development_process_executable_drift",
            "development process executable changed while its identity was captured",
        )
    return profile


def _build_shell_profile(
    spec: _ShellSpec,
    executables: Mapping[str, ProcessExecutableProfile],
    environments: Mapping[str, ProcessEnvironmentProfile],
) -> ShellCommandProfile:
    executable = executables[spec.executable_id]
    prefix = executable.fixed_arguments
    # The manifest-backed profiles have the same capabilities as the already
    # validated pure catalog specs.  Keep the executable prefix local for the
    # construction below and assert the invariant defensively.
    assert spec.fixed_arguments[: len(prefix)] == prefix
    return ShellCommandProfile(
        profile_id=spec.profile_id,
        description=spec.description,
        executable_id=spec.executable_id,
        executable_profile_fingerprint=executable.fingerprint,
        fixed_arguments=spec.fixed_arguments,
        risk=spec.risk,
        side_effect_class=spec.side_effect_class,
        cwd_root_id=spec.cwd_root_id,
        environment_profile_id=spec.environment_profile_id,
        environment_allowlist=spec.environment_allowlist,
        environment=spec.environment,
        minimum_variable_arguments=spec.minimum_variable_arguments,
        maximum_variable_arguments=spec.maximum_variable_arguments,
        variable_argument_pattern=spec.variable_argument_pattern,
        timeout_ms=spec.timeout_ms,
        inline_output_limit_bytes=spec.inline_output_limit_bytes,
        artifact_output_limit_bytes=spec.artifact_output_limit_bytes,
        allow_network=False,
        concurrency_safe=spec.concurrency_safe,
        idempotent=spec.idempotent,
        retryable=spec.retryable,
        version=spec.version,
    )


def _validate_shell_spec_binding(
    spec: _ShellSpec,
    executables: Mapping[str, _ExecutableSpec],
    environments: Mapping[str, ProcessEnvironmentProfile],
) -> None:
    executable = executables.get(spec.executable_id)
    environment_profile = environments.get(spec.environment_profile_id)
    if executable is None or environment_profile is None:
        raise ProductionProcessCatalogError(
            "process_catalog_shell_binding",
            "signed Shell profile references an unknown process profile",
        )
    if spec.cwd_root_id not in executable.allowed_cwd_root_ids:
        raise ProductionProcessCatalogError(
            "process_catalog_shell_binding",
            "signed Shell profile cwd exceeds its executable profile",
        )
    if spec.environment_profile_id not in executable.environment_profile_ids:
        raise ProductionProcessCatalogError(
            "process_catalog_shell_binding",
            "signed Shell environment exceeds its executable profile",
        )
    if not spec.environment_allowlist <= environment_profile.allowed_names:
        raise ProductionProcessCatalogError(
            "process_catalog_shell_binding",
            "signed Shell environment allowlist exceeds its environment profile",
        )
    prefix = executable.fixed_arguments
    if spec.fixed_arguments[: len(prefix)] != prefix:
        raise ProductionProcessCatalogError(
            "process_catalog_shell_binding",
            "signed Shell argv does not retain its executable prefix",
        )
    fixed_variable_count = len(spec.fixed_arguments) - len(prefix)
    if (
        fixed_variable_count + spec.minimum_variable_arguments < executable.minimum_variable_arguments
        or fixed_variable_count + spec.maximum_variable_arguments > executable.maximum_variable_arguments
        or spec.variable_argument_pattern != executable.variable_argument_pattern
    ):
        raise ProductionProcessCatalogError(
            "process_catalog_shell_binding",
            "signed Shell argv exceeds its executable profile",
        )


def _filesystem_capabilities(value: object) -> tuple[ProcessFilesystemCapability, ...]:
    values = _bounded_sequence(value, "appContainerFilesystem", maximum=_MAX_CAPABILITIES, minimum=1)
    capabilities: list[ProcessFilesystemCapability] = []
    for item in values:
        raw = _mapping(item, "AppContainer filesystem capability")
        _exact_keys(raw, {"access", "relativePath", "rootId"}, "AppContainer filesystem capability")
        try:
            access = ProcessFilesystemAccess(_text(raw["access"], "access", maximum=32))
        except ValueError as error:
            raise ProductionProcessCatalogError(
                "process_catalog_filesystem",
                "AppContainer filesystem access is invalid",
            ) from error
        capabilities.append(
            ProcessFilesystemCapability(
                _profile_id(raw["rootId"], "rootId"),
                _runtime_relative_path(raw["relativePath"]),
                access,
            )
        )
    keys = tuple((item.root_id, item.relative_path, item.access.value) for item in capabilities)
    _require_sorted_unique(keys, "appContainerFilesystem")
    return tuple(capabilities)


def _read_manifest_asset(root: Path, record: RuntimeFileRecord, *, maximum: int) -> bytes:
    if record.byte_length < 1 or record.byte_length > maximum:
        raise ProductionProcessCatalogError("process_catalog_size", "process catalog manifest size is invalid")
    path = _manifest_path(root, record)
    try:
        with path.open("rb", buffering=0) as stream:
            before = os.fstat(stream.fileno())
            payload = stream.read(record.byte_length + 1)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise ProductionProcessCatalogError(
            "process_catalog_unavailable",
            "process catalog asset is unavailable",
        ) from error
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or stat.S_IFMT(before.st_mode) != stat.S_IFREG
        or getattr(before, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        or before.st_nlink != 1
        or len(payload) != record.byte_length
        or f"sha256:{hashlib.sha256(payload).hexdigest()}" != record.sha256
    ):
        raise ProductionProcessCatalogError(
            "process_catalog_identity",
            "process catalog asset differs from its signed manifest record",
        )
    if _manifest_path(root, record) != path:
        raise ProductionProcessCatalogError(
            "process_catalog_path_changed",
            "process catalog path changed during verification",
        )
    return payload


def _manifest_path(root: Path, record: RuntimeFileRecord) -> Path:
    relative = _runtime_relative_path(record.path)
    current = root
    for part in relative.split("/"):
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise ProductionProcessCatalogError(
                "process_catalog_unavailable",
                "signed process catalog path is unavailable",
            ) from error
        if current.is_symlink() or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ProductionProcessCatalogError(
                "process_catalog_reparse",
                "signed process catalog path contains a reparse point",
            )
    try:
        path = current.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as error:
        raise ProductionProcessCatalogError(
            "process_catalog_path_escape",
            "signed process catalog path escaped the activated Runtime",
        ) from error
    return path


def _object_without_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProductionProcessCatalogError(
                "process_catalog_duplicate_key",
                "process catalog contains a duplicate JSON key",
            )
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ProductionProcessCatalogError(
        "process_catalog_number",
        f"process catalog contains unsupported numeric constant {value}",
    )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ProductionProcessCatalogError("process_catalog_type", f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ProductionProcessCatalogError("process_catalog_fields", f"{label} fields are not exact")


def _bounded_sequence(
    value: object,
    label: str,
    *,
    maximum: int,
    minimum: int = 0,
) -> Sequence[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ProductionProcessCatalogError("process_catalog_type", f"{label} must be a bounded array")
    return value


def _text(value: object, label: str, *, maximum: int, minimum: int = 0) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum or "\x00" in value:
        raise ProductionProcessCatalogError("process_catalog_type", f"{label} must be bounded text")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ProductionProcessCatalogError("process_catalog_type", f"{label} must be boolean")
    return value


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProductionProcessCatalogError("process_catalog_type", f"{label} must be a bounded integer")
    return value


def _profile_id(value: object, label: str) -> str:
    text = _text(value, label, maximum=128, minimum=1)
    if _PROFILE_ID.fullmatch(text) is None:
        raise ProductionProcessCatalogError("process_catalog_identity", f"{label} is invalid")
    return text


def _shell_profile_id(value: object) -> str:
    text = _text(value, "profileId", maximum=64, minimum=1)
    if _SHELL_PROFILE_ID.fullmatch(text) is None:
        raise ProductionProcessCatalogError("process_catalog_identity", "Shell profileId is invalid")
    return text


def _profile_ids(value: object, label: str) -> tuple[str, ...]:
    values = tuple(
        _profile_id(item, label) for item in _bounded_sequence(value, label, maximum=_MAX_CAPABILITIES, minimum=1)
    )
    _require_sorted_unique(values, label)
    return values


def _environment_names(value: object, label: str) -> tuple[str, ...]:
    values = _text_list(
        value,
        label,
        maximum=_MAX_ENVIRONMENT_NAMES,
        item_maximum=128,
    )
    if any(_ENVIRONMENT_NAME.fullmatch(item) is None for item in values):
        raise ProductionProcessCatalogError("process_catalog_environment", f"{label} contains an invalid name")
    _require_sorted_unique(tuple(item.upper() for item in values), label)
    return values


def _arguments(value: object, label: str) -> tuple[str, ...]:
    return _text_list(value, label, maximum=_MAX_ARGUMENTS, item_maximum=4096)


def _text_list(
    value: object,
    label: str,
    *,
    maximum: int,
    item_maximum: int,
    minimum: int = 0,
) -> tuple[str, ...]:
    values = _bounded_sequence(value, label, maximum=maximum, minimum=minimum)
    return tuple(_text(item, label, maximum=item_maximum) for item in values)


def _text_mapping(value: object, label: str, *, maximum: int) -> dict[str, str]:
    raw = _mapping(value, label)
    if len(raw) > maximum:
        raise ProductionProcessCatalogError("process_catalog_type", f"{label} is too large")
    result: dict[str, str] = {}
    for key, item in raw.items():
        if _ENVIRONMENT_NAME.fullmatch(key) is None:
            raise ProductionProcessCatalogError("process_catalog_environment", f"{label} key is invalid")
        result[key] = _text(item, label, maximum=32 * 1024)
    return result


def _pattern(value: object, label: str) -> str:
    pattern = _text(value, label, maximum=4096, minimum=1)
    try:
        re.compile(pattern)
    except re.error as error:
        raise ProductionProcessCatalogError("process_catalog_pattern", f"{label} is invalid") from error
    return pattern


def _runtime_relative_path(value: object) -> str:
    text = _text(value, "relativePath", maximum=1024, minimum=1)
    if (
        "\\" in text
        or text.startswith("/")
        or re.match(r"^[A-Za-z]:", text)
        or any(part in {"", ".", ".."} for part in text.split("/"))
    ):
        raise ProductionProcessCatalogError(
            "process_catalog_path",
            "process catalog path must be canonical Runtime/root-relative text",
        )
    return text


def _require_sorted_unique(values: Sequence[Any] | Any, label: str) -> None:
    ordered = tuple(values)
    if ordered != tuple(sorted(ordered)) or len(ordered) != len(set(ordered)):
        raise ProductionProcessCatalogError(
            "process_catalog_order",
            f"{label} must be sorted and unique",
        )


__all__ = [
    "PROCESS_CATALOG_PATH",
    "ProductionProcessCatalog",
    "ProductionProcessCatalogError",
    "load_development_process_catalog",
    "load_production_process_catalog",
    "validate_process_catalog_payload",
]
