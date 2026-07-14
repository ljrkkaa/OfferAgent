"""Canonical signed Runtime privilege envelopes and update approval bindings.

The envelope deliberately contains only manifest-relative identities and bounded
privilege specifications.  It never contains an installation path, environment
value, secret value, filesystem inode, or other machine-local identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_RECEIPT_ID = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")
_NETWORK_CATEGORIES = frozenset({"model", "signed_update"})
_ROOT_ACCESS = frozenset({"cwd", "read_only", "read_write"})
_FILESYSTEM_ACCESS = frozenset({"read_only", "read_write"})
_PROFILE_KINDS = frozenset({"hook", "shell"})
_RISKS = frozenset({"read", "network", "write", "execute", "destructive", "external_path", "secret_access"})
_SIDE_EFFECTS = frozenset({"none", "read", "network", "write", "execute", "destructive", "unknown"})
_CONFIRMATION = "我确认授予 OfferAgent Runtime 上述本地权限"
_MAX_ENVELOPE_BYTES = 32 * 1024
_MAX_PROFILES = 128


class RuntimePrivilegeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PrivilegeRootCapability:
    root_id: str
    relative_path: str
    access: str

    def __post_init__(self) -> None:
        _identifier(self.root_id, "root capability id")
        _relative_path(self.relative_path, allow_dot=True)
        if self.access not in _ROOT_ACCESS:
            raise RuntimePrivilegeError("privilege_root_access", "root capability access is invalid")
        if self.access == "cwd" and self.relative_path != ".":
            raise RuntimePrivilegeError("privilege_root_cwd", "cwd root capability must use the root itself")


@dataclass(frozen=True, slots=True)
class EnvironmentPrivilegeProfile:
    profile_id: str
    allowed_names: tuple[str, ...]
    allowed_secret_names: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.profile_id, "environment profile id")
        _sorted_unique(self.allowed_names, "plain environment names", pattern=_ENVIRONMENT_NAME)
        _sorted_unique(self.allowed_secret_names, "secret environment names", pattern=_ENVIRONMENT_NAME)
        if set(self.allowed_names) & set(self.allowed_secret_names):
            raise RuntimePrivilegeError("privilege_environment_overlap", "plain and secret environment names overlap")


@dataclass(frozen=True, slots=True)
class ExecutablePrivilegeProfile:
    executable_id: str
    relative_path: str
    trust: str
    fixed_arguments: tuple[str, ...]
    minimum_variable_arguments: int
    maximum_variable_arguments: int
    variable_argument_pattern: str
    allow_shell_metacharacters: bool
    allowed_cwd_root_ids: tuple[str, ...]
    environment_profile_ids: tuple[str, ...]
    allowed_stdin_modes: tuple[str, ...]
    app_container_filesystem: tuple[PrivilegeRootCapability, ...]
    privilege_fingerprint: str

    def __post_init__(self) -> None:
        _identifier(self.executable_id, "executable id")
        _relative_path(self.relative_path, allow_dot=False)
        if self.trust != "signed_release":
            raise RuntimePrivilegeError("privilege_executable_trust", "release executable trust must be signed_release")
        _arguments(self.fixed_arguments)
        _argument_bounds(self.minimum_variable_arguments, self.maximum_variable_arguments)
        _bounded_text(self.variable_argument_pattern, "variable argument pattern", 4096)
        if self.allow_shell_metacharacters:
            raise RuntimePrivilegeError(
                "privilege_shell_metacharacters", "release processes cannot allow shell metacharacters"
            )
        _sorted_unique(self.allowed_cwd_root_ids, "cwd root ids", pattern=_IDENTIFIER)
        _sorted_unique(self.environment_profile_ids, "environment profile ids", pattern=_IDENTIFIER)
        _sorted_unique(self.allowed_stdin_modes, "stdin modes", pattern=_IDENTIFIER)
        _canonical_records(
            self.app_container_filesystem,
            "AppContainer filesystem capabilities",
            key=lambda item: (item.root_id, item.relative_path, item.access),
        )
        if any(item.access not in _FILESYSTEM_ACCESS for item in self.app_container_filesystem):
            raise RuntimePrivilegeError("privilege_appcontainer_access", "AppContainer filesystem access is invalid")
        expected = _fingerprint(_executable_payload(self, include_fingerprint=False))
        if self.privilege_fingerprint != expected:
            raise RuntimePrivilegeError("privilege_item_fingerprint", "executable privilege fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class RuntimeProfilePrivilege:
    kind: str
    profile_id: str
    executable_id: str
    risk: str
    side_effect_class: str
    fixed_arguments: tuple[str, ...]
    minimum_variable_arguments: int
    maximum_variable_arguments: int
    variable_argument_pattern: str
    allowed_cwd_root_ids: tuple[str, ...]
    environment_profile_ids: tuple[str, ...]
    allowed_plain_environment_names: tuple[str, ...]
    allowed_secret_environment_names: tuple[str, ...]
    allow_network: bool
    privilege_fingerprint: str

    def __post_init__(self) -> None:
        if self.kind not in _PROFILE_KINDS:
            raise RuntimePrivilegeError("privilege_profile_kind", "Runtime privilege profile kind is invalid")
        _identifier(self.profile_id, "Runtime privilege profile id")
        _identifier(self.executable_id, "Runtime privilege executable id")
        if self.risk not in _RISKS or self.side_effect_class not in _SIDE_EFFECTS:
            raise RuntimePrivilegeError("privilege_profile_risk", "Runtime privilege risk summary is invalid")
        _arguments(self.fixed_arguments)
        _argument_bounds(self.minimum_variable_arguments, self.maximum_variable_arguments)
        _bounded_text(self.variable_argument_pattern, "variable argument pattern", 4096)
        _sorted_unique(self.allowed_cwd_root_ids, "profile cwd root ids", pattern=_IDENTIFIER)
        _sorted_unique(self.environment_profile_ids, "profile environment ids", pattern=_IDENTIFIER)
        _sorted_unique(
            self.allowed_plain_environment_names, "profile plain environment names", pattern=_ENVIRONMENT_NAME
        )
        _sorted_unique(
            self.allowed_secret_environment_names,
            "profile secret environment names",
            pattern=_ENVIRONMENT_NAME,
        )
        if set(self.allowed_plain_environment_names) & set(self.allowed_secret_environment_names):
            raise RuntimePrivilegeError("privilege_environment_overlap", "profile environment names overlap")
        if self.allow_network:
            raise RuntimePrivilegeError("privilege_local_network", "local Runtime process profiles cannot use network")
        expected = _fingerprint(_runtime_profile_payload(self, include_fingerprint=False))
        if self.privilege_fingerprint != expected:
            raise RuntimePrivilegeError(
                "privilege_item_fingerprint", "Runtime profile privilege fingerprint is invalid"
            )


@dataclass(frozen=True, slots=True)
class RuntimePrivilegeEnvelope:
    process_catalog_sha256: str
    allowed_network_categories: tuple[str, ...]
    local_process_network: bool
    root_capabilities: tuple[PrivilegeRootCapability, ...]
    environment_profiles: tuple[EnvironmentPrivilegeProfile, ...]
    executable_profiles: tuple[ExecutablePrivilegeProfile, ...]
    profile_privileges: tuple[RuntimeProfilePrivilege, ...]
    fingerprint: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or _SHA256.fullmatch(self.process_catalog_sha256) is None:
            raise RuntimePrivilegeError("privilege_envelope_identity", "Runtime privilege envelope identity is invalid")
        _sorted_unique(self.allowed_network_categories, "network categories", allowed=_NETWORK_CATEGORIES)
        if self.local_process_network:
            raise RuntimePrivilegeError("privilege_local_network", "local Runtime processes cannot use network")
        if (
            max(
                len(self.root_capabilities),
                len(self.environment_profiles),
                len(self.executable_profiles),
                len(self.profile_privileges),
            )
            > _MAX_PROFILES
        ):
            raise RuntimePrivilegeError("privilege_envelope_limit", "Runtime privilege envelope has too many profiles")
        _canonical_records(
            self.root_capabilities,
            "root capabilities",
            key=lambda item: (item.root_id, item.relative_path, item.access),
        )
        _canonical_records(self.environment_profiles, "environment profiles", key=lambda item: item.profile_id)
        _canonical_records(self.executable_profiles, "executable profiles", key=lambda item: item.executable_id)
        _canonical_records(
            self.profile_privileges,
            "Runtime profile privileges",
            key=lambda item: (item.kind, item.profile_id),
        )
        environment_ids = {item.profile_id for item in self.environment_profiles}
        executable_ids = {item.executable_id for item in self.executable_profiles}
        root_ids = {item.root_id for item in self.root_capabilities}
        for executable in self.executable_profiles:
            if not set(executable.environment_profile_ids) <= environment_ids:
                raise RuntimePrivilegeError(
                    "privilege_environment_reference", "executable environment profile is missing"
                )
            if not set(executable.allowed_cwd_root_ids) <= root_ids:
                raise RuntimePrivilegeError("privilege_root_reference", "executable cwd root is missing")
        for profile in self.profile_privileges:
            if profile.executable_id not in executable_ids:
                raise RuntimePrivilegeError("privilege_executable_reference", "Runtime profile executable is missing")
            if not set(profile.environment_profile_ids) <= environment_ids:
                raise RuntimePrivilegeError("privilege_environment_reference", "Runtime profile environment is missing")
            if not set(profile.allowed_cwd_root_ids) <= root_ids:
                raise RuntimePrivilegeError("privilege_root_reference", "Runtime profile cwd root is missing")
        expected = _fingerprint(privilege_envelope_payload(self, include_fingerprint=False))
        if self.fingerprint != expected:
            raise RuntimePrivilegeError(
                "privilege_envelope_fingerprint", "Runtime privilege envelope fingerprint is invalid"
            )
        if len(_canonical_json(privilege_envelope_payload(self))) > _MAX_ENVELOPE_BYTES:
            raise RuntimePrivilegeError(
                "privilege_envelope_limit", "Runtime privilege envelope exceeds its display limit"
            )


@dataclass(frozen=True, slots=True)
class RuntimePrivilegeAssessment:
    automatic: bool
    old_fingerprint: str | None
    new_fingerprint: str
    diff_hash: str
    diff: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RuntimePrivilegeApprovalReceipt:
    receipt_id: str
    issued_at: str
    expires_at: str
    confirmation: str
    old_manifest_hash: str | None
    new_manifest_hash: str
    old_privilege_fingerprint: str | None
    new_privilege_fingerprint: str
    diff_hash: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or _RECEIPT_ID.fullmatch(self.receipt_id) is None:
            raise RuntimePrivilegeError(
                "privilege_receipt_invalid", "Runtime privilege approval receipt identity is invalid"
            )
        if self.confirmation != _CONFIRMATION:
            raise RuntimePrivilegeError("privilege_receipt_confirmation", "Runtime privilege confirmation is invalid")
        hashes = (
            self.new_manifest_hash,
            self.new_privilege_fingerprint,
            self.diff_hash,
            *(value for value in (self.old_manifest_hash, self.old_privilege_fingerprint) if value is not None),
        )
        if any(_SHA256.fullmatch(value) is None for value in hashes):
            raise RuntimePrivilegeError("privilege_receipt_binding", "Runtime privilege receipt binding is invalid")
        if self.old_manifest_hash is None and self.old_privilege_fingerprint is not None:
            raise RuntimePrivilegeError("privilege_receipt_binding", "Runtime privilege old identity is incomplete")
        issued = _utc_timestamp(self.issued_at)
        expires = _utc_timestamp(self.expires_at)
        if not issued < expires or expires - issued > timedelta(minutes=10):
            raise RuntimePrivilegeError("privilege_receipt_expiry", "Runtime privilege receipt lifetime is invalid")


def build_privilege_envelope_from_process_catalog(payload: bytes) -> RuntimePrivilegeEnvelope:
    raw = _parse_canonical_catalog(payload)
    _exact_keys(raw, {"environmentProfiles", "executableProfiles", "schemaVersion", "signedShellProfiles"}, "catalog")
    if _integer(raw["schemaVersion"], "catalog schemaVersion") != 1:
        raise RuntimePrivilegeError("process_catalog_schema", "process catalog schema is unsupported")

    environments: list[EnvironmentPrivilegeProfile] = []
    for value in _sequence(raw["environmentProfiles"], "environmentProfiles"):
        item = _mapping(value, "environment profile")
        _exact_keys(item, {"allowedNames", "allowedSecretNames", "profileId"}, "environment profile")
        environments.append(
            EnvironmentPrivilegeProfile(
                _text(item["profileId"], "environment profileId"),
                tuple(
                    _text(value, "allowed environment name")
                    for value in _sequence(item["allowedNames"], "allowedNames")
                ),
                tuple(
                    _text(value, "allowed secret environment name")
                    for value in _sequence(item["allowedSecretNames"], "allowedSecretNames")
                ),
            )
        )
    environment_by_id = {item.profile_id: item for item in environments}

    executables: list[ExecutablePrivilegeProfile] = []
    executable_ids: set[str] = set()
    for value in _sequence(raw["executableProfiles"], "executableProfiles"):
        item = _mapping(value, "executable profile")
        _exact_keys(
            item,
            {
                "allowNetwork",
                "allowShellMetacharacters",
                "allowedCwdRootIds",
                "allowedStdinModes",
                "appContainerFilesystem",
                "environmentProfileIds",
                "executableId",
                "fixedArguments",
                "maximumVariableArguments",
                "minimumVariableArguments",
                "relativePath",
                "trust",
                "variableArgumentPattern",
            },
            "executable profile",
        )
        if _boolean(item["allowNetwork"], "allowNetwork"):
            raise RuntimePrivilegeError("privilege_local_network", "process catalog enables local process network")
        filesystem = tuple(
            _catalog_filesystem(value) for value in _sequence(item["appContainerFilesystem"], "filesystem")
        )
        executable = _create_executable_profile(
            executable_id=_text(item["executableId"], "executableId"),
            relative_path=_text(item["relativePath"], "relativePath"),
            trust=_text(item["trust"], "trust"),
            fixed_arguments=tuple(
                _text(value, "fixed argument") for value in _sequence(item["fixedArguments"], "fixedArguments")
            ),
            minimum_variable_arguments=_integer(item["minimumVariableArguments"], "minimumVariableArguments"),
            maximum_variable_arguments=_integer(item["maximumVariableArguments"], "maximumVariableArguments"),
            variable_argument_pattern=_text(item["variableArgumentPattern"], "variableArgumentPattern"),
            allow_shell_metacharacters=_boolean(item["allowShellMetacharacters"], "allowShellMetacharacters"),
            allowed_cwd_root_ids=tuple(
                _text(value, "cwd root id") for value in _sequence(item["allowedCwdRootIds"], "allowedCwdRootIds")
            ),
            environment_profile_ids=tuple(
                _text(value, "environment profile id")
                for value in _sequence(item["environmentProfileIds"], "environmentProfileIds")
            ),
            allowed_stdin_modes=tuple(
                _text(value, "stdin mode") for value in _sequence(item["allowedStdinModes"], "allowedStdinModes")
            ),
            app_container_filesystem=filesystem,
        )
        executables.append(executable)
        if executable.executable_id in executable_ids:
            raise RuntimePrivilegeError("process_catalog_duplicate", "process catalog executable id is duplicated")
        executable_ids.add(executable.executable_id)

    roots: set[PrivilegeRootCapability] = set()
    for executable in executables:
        roots.update(PrivilegeRootCapability(root_id, ".", "cwd") for root_id in executable.allowed_cwd_root_ids)
        roots.update(executable.app_container_filesystem)

    profiles: list[RuntimeProfilePrivilege] = []
    shell_executable_ids: set[str] = set()
    for value in _sequence(raw["signedShellProfiles"], "signedShellProfiles"):
        item = _mapping(value, "signed shell profile")
        _exact_keys(
            item,
            {
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
            },
            "signed shell profile",
        )
        executable_id = _text(item["executableId"], "shell executableId")
        shell_executable = next((profile for profile in executables if profile.executable_id == executable_id), None)
        if shell_executable is None:
            raise RuntimePrivilegeError("process_catalog_reference", "shell profile executable is missing")
        environment_id = _text(item["environmentProfileId"], "shell environmentProfileId")
        environment = environment_by_id.get(environment_id)
        if environment is None:
            raise RuntimePrivilegeError("process_catalog_reference", "shell environment profile is missing")
        constants = _mapping(item["environment"], "shell environment")
        plain_names = set(environment.allowed_names)
        plain_names.update(_environment_name(key) for key in constants)
        plain_names.update(
            _environment_name(_text(value, "shell environment allowlist"))
            for value in _sequence(item["environmentAllowlist"], "environmentAllowlist")
        )
        shell_fixed_arguments = tuple(
            _text(value, "shell fixed argument") for value in _sequence(item["fixedArguments"], "fixedArguments")
        )
        shell_minimum = _integer(item["minimumVariableArguments"], "minimumVariableArguments")
        shell_maximum = _integer(item["maximumVariableArguments"], "maximumVariableArguments")
        shell_pattern = _text(item["variableArgumentPattern"], "variableArgumentPattern")
        shell_cwd = _text(item["cwdRootId"], "shell cwdRootId")
        if (
            shell_fixed_arguments != shell_executable.fixed_arguments
            or shell_minimum < shell_executable.minimum_variable_arguments
            or shell_maximum > shell_executable.maximum_variable_arguments
            or shell_pattern != shell_executable.variable_argument_pattern
            or shell_cwd not in shell_executable.allowed_cwd_root_ids
            or environment_id not in shell_executable.environment_profile_ids
        ):
            raise RuntimePrivilegeError(
                "process_catalog_privilege_mismatch",
                "shell profile exceeds or differs from its executable privilege profile",
            )
        profile = _create_runtime_profile(
            kind="shell",
            profile_id=_text(item["profileId"], "shell profileId"),
            executable_id=executable_id,
            risk=_text(item["risk"], "shell risk"),
            side_effect_class=_text(item["sideEffectClass"], "shell sideEffectClass"),
            fixed_arguments=shell_fixed_arguments,
            minimum_variable_arguments=shell_minimum,
            maximum_variable_arguments=shell_maximum,
            variable_argument_pattern=shell_pattern,
            allowed_cwd_root_ids=(shell_cwd,),
            environment_profile_ids=(environment_id,),
            allowed_plain_environment_names=tuple(sorted(plain_names)),
            allowed_secret_environment_names=environment.allowed_secret_names,
            allow_network=_boolean(item["allowNetwork"], "shell allowNetwork"),
        )
        profiles.append(profile)
        shell_executable_ids.add(executable_id)

    for executable in executables:
        if executable.executable_id in shell_executable_ids:
            continue
        if executable.executable_id.startswith("hook-"):
            kind = "hook"
        else:
            raise RuntimePrivilegeError(
                "process_catalog_profile_kind", "process executable has no privilege profile kind"
            )
        plain: set[str] = set()
        secret: set[str] = set()
        for environment_id in executable.environment_profile_ids:
            environment = environment_by_id.get(environment_id)
            if environment is None:
                raise RuntimePrivilegeError("process_catalog_reference", "process environment profile is missing")
            plain.update(environment.allowed_names)
            secret.update(environment.allowed_secret_names)
        profile = _create_runtime_profile(
            kind=kind,
            profile_id=executable.executable_id,
            executable_id=executable.executable_id,
            risk="execute",
            side_effect_class="execute",
            fixed_arguments=executable.fixed_arguments,
            minimum_variable_arguments=executable.minimum_variable_arguments,
            maximum_variable_arguments=executable.maximum_variable_arguments,
            variable_argument_pattern=executable.variable_argument_pattern,
            allowed_cwd_root_ids=executable.allowed_cwd_root_ids,
            environment_profile_ids=executable.environment_profile_ids,
            allowed_plain_environment_names=tuple(sorted(plain)),
            allowed_secret_environment_names=tuple(sorted(secret)),
            allow_network=False,
        )
        profiles.append(profile)

    return _create_envelope(
        process_catalog_sha256=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        allowed_network_categories=tuple(sorted(_NETWORK_CATEGORIES)),
        local_process_network=False,
        root_capabilities=tuple(sorted(roots, key=lambda item: (item.root_id, item.relative_path, item.access))),
        environment_profiles=tuple(sorted(environments, key=lambda item: item.profile_id)),
        executable_profiles=tuple(sorted(executables, key=lambda item: item.executable_id)),
        profile_privileges=tuple(sorted(profiles, key=lambda item: (item.kind, item.profile_id))),
    )


def parse_privilege_envelope(value: object) -> RuntimePrivilegeEnvelope:
    raw = _mapping(value, "privilegeEnvelope")
    _exact_keys(
        raw,
        {
            "allowedNetworkCategories",
            "environmentProfiles",
            "executableProfiles",
            "fingerprint",
            "localProcessNetwork",
            "processCatalogSha256",
            "profilePrivileges",
            "rootCapabilities",
            "schemaVersion",
        },
        "privilegeEnvelope",
    )
    environments: list[EnvironmentPrivilegeProfile] = []
    for value in _sequence(raw["environmentProfiles"], "environmentProfiles"):
        item = _mapping(value, "environment privilege profile")
        _exact_keys(item, {"allowedNames", "allowedSecretNames", "profileId"}, "environment privilege profile")
        environments.append(
            EnvironmentPrivilegeProfile(
                _text(item["profileId"], "profileId"),
                tuple(_text(value, "allowed name") for value in _sequence(item["allowedNames"], "allowedNames")),
                tuple(
                    _text(value, "allowed secret name")
                    for value in _sequence(item["allowedSecretNames"], "allowedSecretNames")
                ),
            )
        )
    roots = tuple(
        _parse_root_capability(value, allow_cwd=True) for value in _sequence(raw["rootCapabilities"], "roots")
    )
    executables: list[ExecutablePrivilegeProfile] = []
    for value in _sequence(raw["executableProfiles"], "executableProfiles"):
        item = _mapping(value, "executable privilege profile")
        _exact_keys(
            item,
            {
                "allowShellMetacharacters",
                "allowedCwdRootIds",
                "allowedStdinModes",
                "appContainerFilesystem",
                "environmentProfileIds",
                "executableId",
                "fixedArguments",
                "maximumVariableArguments",
                "minimumVariableArguments",
                "privilegeFingerprint",
                "relativePath",
                "trust",
                "variableArgumentPattern",
            },
            "executable privilege profile",
        )
        executables.append(
            ExecutablePrivilegeProfile(
                executable_id=_text(item["executableId"], "executableId"),
                relative_path=_text(item["relativePath"], "relativePath"),
                trust=_text(item["trust"], "trust"),
                fixed_arguments=tuple(
                    _text(v, "fixed argument") for v in _sequence(item["fixedArguments"], "fixedArguments")
                ),
                minimum_variable_arguments=_integer(item["minimumVariableArguments"], "minimumVariableArguments"),
                maximum_variable_arguments=_integer(item["maximumVariableArguments"], "maximumVariableArguments"),
                variable_argument_pattern=_text(item["variableArgumentPattern"], "variableArgumentPattern"),
                allow_shell_metacharacters=_boolean(item["allowShellMetacharacters"], "allowShellMetacharacters"),
                allowed_cwd_root_ids=tuple(
                    _text(v, "cwd root id") for v in _sequence(item["allowedCwdRootIds"], "allowedCwdRootIds")
                ),
                environment_profile_ids=tuple(
                    _text(v, "environment profile id")
                    for v in _sequence(item["environmentProfileIds"], "environmentProfileIds")
                ),
                allowed_stdin_modes=tuple(
                    _text(v, "stdin mode") for v in _sequence(item["allowedStdinModes"], "allowedStdinModes")
                ),
                app_container_filesystem=tuple(
                    _parse_root_capability(v, allow_cwd=False)
                    for v in _sequence(item["appContainerFilesystem"], "appContainerFilesystem")
                ),
                privilege_fingerprint=_text(item["privilegeFingerprint"], "privilegeFingerprint"),
            )
        )
    profiles: list[RuntimeProfilePrivilege] = []
    for value in _sequence(raw["profilePrivileges"], "profilePrivileges"):
        item = _mapping(value, "Runtime profile privilege")
        _exact_keys(
            item,
            {
                "allowNetwork",
                "allowedCwdRootIds",
                "allowedPlainEnvironmentNames",
                "allowedSecretEnvironmentNames",
                "environmentProfileIds",
                "executableId",
                "fixedArguments",
                "kind",
                "maximumVariableArguments",
                "minimumVariableArguments",
                "privilegeFingerprint",
                "profileId",
                "risk",
                "sideEffectClass",
                "variableArgumentPattern",
            },
            "Runtime profile privilege",
        )
        profiles.append(
            RuntimeProfilePrivilege(
                kind=_text(item["kind"], "kind"),
                profile_id=_text(item["profileId"], "profileId"),
                executable_id=_text(item["executableId"], "executableId"),
                risk=_text(item["risk"], "risk"),
                side_effect_class=_text(item["sideEffectClass"], "sideEffectClass"),
                fixed_arguments=tuple(
                    _text(v, "fixed argument") for v in _sequence(item["fixedArguments"], "fixedArguments")
                ),
                minimum_variable_arguments=_integer(item["minimumVariableArguments"], "minimumVariableArguments"),
                maximum_variable_arguments=_integer(item["maximumVariableArguments"], "maximumVariableArguments"),
                variable_argument_pattern=_text(item["variableArgumentPattern"], "variableArgumentPattern"),
                allowed_cwd_root_ids=tuple(
                    _text(v, "cwd root id") for v in _sequence(item["allowedCwdRootIds"], "allowedCwdRootIds")
                ),
                environment_profile_ids=tuple(
                    _text(v, "environment profile id")
                    for v in _sequence(item["environmentProfileIds"], "environmentProfileIds")
                ),
                allowed_plain_environment_names=tuple(
                    _text(v, "plain environment name")
                    for v in _sequence(item["allowedPlainEnvironmentNames"], "allowedPlainEnvironmentNames")
                ),
                allowed_secret_environment_names=tuple(
                    _text(v, "secret environment name")
                    for v in _sequence(item["allowedSecretEnvironmentNames"], "allowedSecretEnvironmentNames")
                ),
                allow_network=_boolean(item["allowNetwork"], "allowNetwork"),
                privilege_fingerprint=_text(item["privilegeFingerprint"], "privilegeFingerprint"),
            )
        )
    return RuntimePrivilegeEnvelope(
        process_catalog_sha256=_text(raw["processCatalogSha256"], "processCatalogSha256"),
        allowed_network_categories=tuple(
            _text(value, "network category")
            for value in _sequence(raw["allowedNetworkCategories"], "allowedNetworkCategories")
        ),
        local_process_network=_boolean(raw["localProcessNetwork"], "localProcessNetwork"),
        root_capabilities=roots,
        environment_profiles=tuple(environments),
        executable_profiles=tuple(executables),
        profile_privileges=tuple(profiles),
        fingerprint=_text(raw["fingerprint"], "fingerprint"),
        schema_version=_integer(raw["schemaVersion"], "schemaVersion"),
    )


def privilege_envelope_payload(
    envelope: RuntimePrivilegeEnvelope,
    include_fingerprint: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "allowedNetworkCategories": list(envelope.allowed_network_categories),
        "environmentProfiles": [
            {
                "allowedNames": list(item.allowed_names),
                "allowedSecretNames": list(item.allowed_secret_names),
                "profileId": item.profile_id,
            }
            for item in envelope.environment_profiles
        ],
        "executableProfiles": [_executable_payload(item) for item in envelope.executable_profiles],
        "localProcessNetwork": envelope.local_process_network,
        "processCatalogSha256": envelope.process_catalog_sha256,
        "profilePrivileges": [_runtime_profile_payload(item) for item in envelope.profile_privileges],
        "rootCapabilities": [_root_payload(item) for item in envelope.root_capabilities],
        "schemaVersion": envelope.schema_version,
    }
    if include_fingerprint:
        payload["fingerprint"] = envelope.fingerprint
    return payload


def assess_privilege_change(
    old: RuntimePrivilegeEnvelope | None,
    new: RuntimePrivilegeEnvelope,
) -> RuntimePrivilegeAssessment:
    diff: dict[str, object] = {
        "new": privilege_envelope_payload(new, include_fingerprint=False),
        "old": None if old is None else privilege_envelope_payload(old, include_fingerprint=False),
    }
    return RuntimePrivilegeAssessment(
        automatic=old is not None and _is_same_or_narrower(old, new),
        old_fingerprint=None if old is None else old.fingerprint,
        new_fingerprint=new.fingerprint,
        diff_hash=_fingerprint(diff),
        diff=diff,
    )


def parse_privilege_approval_receipt(value: object) -> RuntimePrivilegeApprovalReceipt:
    raw = _mapping(value, "privilegeApproval")
    _exact_keys(
        raw,
        {
            "confirmation",
            "diffHash",
            "expiresAt",
            "issuedAt",
            "newManifestHash",
            "newPrivilegeFingerprint",
            "oldManifestHash",
            "oldPrivilegeFingerprint",
            "receiptId",
            "schemaVersion",
        },
        "privilegeApproval",
    )
    old_manifest = raw["oldManifestHash"]
    old_privilege = raw["oldPrivilegeFingerprint"]
    if old_manifest is not None and not isinstance(old_manifest, str):
        raise RuntimePrivilegeError("privilege_receipt_invalid", "old manifest hash is invalid")
    if old_privilege is not None and not isinstance(old_privilege, str):
        raise RuntimePrivilegeError("privilege_receipt_invalid", "old privilege fingerprint is invalid")
    return RuntimePrivilegeApprovalReceipt(
        receipt_id=_text(raw["receiptId"], "receiptId"),
        issued_at=_text(raw["issuedAt"], "issuedAt"),
        expires_at=_text(raw["expiresAt"], "expiresAt"),
        confirmation=_text(raw["confirmation"], "confirmation"),
        old_manifest_hash=old_manifest,
        new_manifest_hash=_text(raw["newManifestHash"], "newManifestHash"),
        old_privilege_fingerprint=old_privilege,
        new_privilege_fingerprint=_text(raw["newPrivilegeFingerprint"], "newPrivilegeFingerprint"),
        diff_hash=_text(raw["diffHash"], "diffHash"),
        schema_version=_integer(raw["schemaVersion"], "schemaVersion"),
    )


def privilege_approval_receipt_payload(receipt: RuntimePrivilegeApprovalReceipt) -> dict[str, object]:
    return {
        "confirmation": receipt.confirmation,
        "diffHash": receipt.diff_hash,
        "expiresAt": receipt.expires_at,
        "issuedAt": receipt.issued_at,
        "newManifestHash": receipt.new_manifest_hash,
        "newPrivilegeFingerprint": receipt.new_privilege_fingerprint,
        "oldManifestHash": receipt.old_manifest_hash,
        "oldPrivilegeFingerprint": receipt.old_privilege_fingerprint,
        "receiptId": receipt.receipt_id,
        "schemaVersion": receipt.schema_version,
    }


def validate_privilege_approval_receipt(
    receipt: RuntimePrivilegeApprovalReceipt,
    *,
    assessment: RuntimePrivilegeAssessment,
    old_manifest_hash: str | None,
    new_manifest_hash: str,
    now: datetime,
    consumed_receipt_ids: Sequence[str],
) -> None:
    expected = (
        old_manifest_hash,
        new_manifest_hash,
        assessment.old_fingerprint,
        assessment.new_fingerprint,
        assessment.diff_hash,
    )
    actual = (
        receipt.old_manifest_hash,
        receipt.new_manifest_hash,
        receipt.old_privilege_fingerprint,
        receipt.new_privilege_fingerprint,
        receipt.diff_hash,
    )
    if actual != expected:
        raise RuntimePrivilegeError("privilege_receipt_drift", "Runtime privilege receipt does not bind this upgrade")
    current = now.astimezone(timezone.utc)
    issued = _utc_timestamp(receipt.issued_at)
    expires = _utc_timestamp(receipt.expires_at)
    if issued > current + timedelta(seconds=30) or current > expires:
        raise RuntimePrivilegeError(
            "privilege_receipt_expired", "Runtime privilege receipt is outside its validity window"
        )
    if receipt.receipt_id in consumed_receipt_ids:
        raise RuntimePrivilegeError("privilege_receipt_replayed", "Runtime privilege receipt was already consumed")


def format_utc_timestamp(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    return utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_same_or_narrower(old: RuntimePrivilegeEnvelope, new: RuntimePrivilegeEnvelope) -> bool:
    if not set(new.allowed_network_categories) <= set(old.allowed_network_categories):
        return False
    if not _root_capabilities_narrower(old.root_capabilities, new.root_capabilities):
        return False
    old_environments = {item.profile_id: item for item in old.environment_profiles}
    new_environments = {item.profile_id: item for item in new.environment_profiles}
    if not set(new_environments) <= set(old_environments):
        return False
    for profile_id, candidate_environment in new_environments.items():
        current_environment = old_environments[profile_id]
        if not set(candidate_environment.allowed_names) <= set(current_environment.allowed_names):
            return False
        if not set(candidate_environment.allowed_secret_names) <= set(current_environment.allowed_secret_names):
            return False
    old_executables = {item.executable_id: item for item in old.executable_profiles}
    new_executables = {item.executable_id: item for item in new.executable_profiles}
    if not set(new_executables) <= set(old_executables):
        return False
    for executable_id, candidate_executable in new_executables.items():
        current_executable = old_executables[executable_id]
        if (
            candidate_executable.relative_path != current_executable.relative_path
            or candidate_executable.trust != current_executable.trust
            or candidate_executable.fixed_arguments != current_executable.fixed_arguments
            or candidate_executable.variable_argument_pattern != current_executable.variable_argument_pattern
            or candidate_executable.minimum_variable_arguments < current_executable.minimum_variable_arguments
            or candidate_executable.maximum_variable_arguments > current_executable.maximum_variable_arguments
            or not set(candidate_executable.allowed_cwd_root_ids) <= set(current_executable.allowed_cwd_root_ids)
            or not set(candidate_executable.environment_profile_ids) <= set(current_executable.environment_profile_ids)
            or not set(candidate_executable.allowed_stdin_modes) <= set(current_executable.allowed_stdin_modes)
            or not _root_capabilities_narrower(
                current_executable.app_container_filesystem,
                candidate_executable.app_container_filesystem,
            )
        ):
            return False
    old_profiles = {(item.kind, item.profile_id): item for item in old.profile_privileges}
    new_profiles = {(item.kind, item.profile_id): item for item in new.profile_privileges}
    if not set(new_profiles) <= set(old_profiles):
        return False
    for key, candidate_profile in new_profiles.items():
        current_profile = old_profiles[key]
        if (
            candidate_profile.executable_id != current_profile.executable_id
            or candidate_profile.risk != current_profile.risk
            or candidate_profile.side_effect_class != current_profile.side_effect_class
            or candidate_profile.fixed_arguments != current_profile.fixed_arguments
            or candidate_profile.variable_argument_pattern != current_profile.variable_argument_pattern
            or candidate_profile.minimum_variable_arguments < current_profile.minimum_variable_arguments
            or candidate_profile.maximum_variable_arguments > current_profile.maximum_variable_arguments
            or not set(candidate_profile.allowed_cwd_root_ids) <= set(current_profile.allowed_cwd_root_ids)
            or not set(candidate_profile.environment_profile_ids) <= set(current_profile.environment_profile_ids)
            or not set(candidate_profile.allowed_plain_environment_names)
            <= set(current_profile.allowed_plain_environment_names)
            or not set(candidate_profile.allowed_secret_environment_names)
            <= set(current_profile.allowed_secret_environment_names)
        ):
            return False
    return True


def _root_capabilities_narrower(
    old: Sequence[PrivilegeRootCapability],
    new: Sequence[PrivilegeRootCapability],
) -> bool:
    current = {(item.root_id, item.relative_path): item.access for item in old}
    rank = {"read_only": 0, "read_write": 1}
    for item in new:
        previous = current.get((item.root_id, item.relative_path))
        if previous is None:
            return False
        if item.access == "cwd" or previous == "cwd":
            if item.access != previous:
                return False
        elif rank[item.access] > rank[previous]:
            return False
    return True


def _executable_payload(item: ExecutablePrivilegeProfile, include_fingerprint: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "allowShellMetacharacters": item.allow_shell_metacharacters,
        "allowedCwdRootIds": list(item.allowed_cwd_root_ids),
        "allowedStdinModes": list(item.allowed_stdin_modes),
        "appContainerFilesystem": [_root_payload(value) for value in item.app_container_filesystem],
        "environmentProfileIds": list(item.environment_profile_ids),
        "executableId": item.executable_id,
        "fixedArguments": list(item.fixed_arguments),
        "maximumVariableArguments": item.maximum_variable_arguments,
        "minimumVariableArguments": item.minimum_variable_arguments,
        "relativePath": item.relative_path,
        "trust": item.trust,
        "variableArgumentPattern": item.variable_argument_pattern,
    }
    if include_fingerprint:
        payload["privilegeFingerprint"] = item.privilege_fingerprint
    return payload


def _runtime_profile_payload(item: RuntimeProfilePrivilege, include_fingerprint: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "allowNetwork": item.allow_network,
        "allowedCwdRootIds": list(item.allowed_cwd_root_ids),
        "allowedPlainEnvironmentNames": list(item.allowed_plain_environment_names),
        "allowedSecretEnvironmentNames": list(item.allowed_secret_environment_names),
        "environmentProfileIds": list(item.environment_profile_ids),
        "executableId": item.executable_id,
        "fixedArguments": list(item.fixed_arguments),
        "kind": item.kind,
        "maximumVariableArguments": item.maximum_variable_arguments,
        "minimumVariableArguments": item.minimum_variable_arguments,
        "profileId": item.profile_id,
        "risk": item.risk,
        "sideEffectClass": item.side_effect_class,
        "variableArgumentPattern": item.variable_argument_pattern,
    }
    if include_fingerprint:
        payload["privilegeFingerprint"] = item.privilege_fingerprint
    return payload


def _root_payload(item: PrivilegeRootCapability) -> dict[str, object]:
    return {"access": item.access, "relativePath": item.relative_path, "rootId": item.root_id}


def _create_executable_profile(
    *,
    executable_id: str,
    relative_path: str,
    trust: str,
    fixed_arguments: tuple[str, ...],
    minimum_variable_arguments: int,
    maximum_variable_arguments: int,
    variable_argument_pattern: str,
    allow_shell_metacharacters: bool,
    allowed_cwd_root_ids: tuple[str, ...],
    environment_profile_ids: tuple[str, ...],
    allowed_stdin_modes: tuple[str, ...],
    app_container_filesystem: tuple[PrivilegeRootCapability, ...],
) -> ExecutablePrivilegeProfile:
    payload = {
        "allowShellMetacharacters": allow_shell_metacharacters,
        "allowedCwdRootIds": list(allowed_cwd_root_ids),
        "allowedStdinModes": list(allowed_stdin_modes),
        "appContainerFilesystem": [_root_payload(value) for value in app_container_filesystem],
        "environmentProfileIds": list(environment_profile_ids),
        "executableId": executable_id,
        "fixedArguments": list(fixed_arguments),
        "maximumVariableArguments": maximum_variable_arguments,
        "minimumVariableArguments": minimum_variable_arguments,
        "relativePath": relative_path,
        "trust": trust,
        "variableArgumentPattern": variable_argument_pattern,
    }
    return ExecutablePrivilegeProfile(
        executable_id,
        relative_path,
        trust,
        fixed_arguments,
        minimum_variable_arguments,
        maximum_variable_arguments,
        variable_argument_pattern,
        allow_shell_metacharacters,
        allowed_cwd_root_ids,
        environment_profile_ids,
        allowed_stdin_modes,
        app_container_filesystem,
        _fingerprint(payload),
    )


def _create_runtime_profile(
    *,
    kind: str,
    profile_id: str,
    executable_id: str,
    risk: str,
    side_effect_class: str,
    fixed_arguments: tuple[str, ...],
    minimum_variable_arguments: int,
    maximum_variable_arguments: int,
    variable_argument_pattern: str,
    allowed_cwd_root_ids: tuple[str, ...],
    environment_profile_ids: tuple[str, ...],
    allowed_plain_environment_names: tuple[str, ...],
    allowed_secret_environment_names: tuple[str, ...],
    allow_network: bool,
) -> RuntimeProfilePrivilege:
    payload = {
        "allowNetwork": allow_network,
        "allowedCwdRootIds": list(allowed_cwd_root_ids),
        "allowedPlainEnvironmentNames": list(allowed_plain_environment_names),
        "allowedSecretEnvironmentNames": list(allowed_secret_environment_names),
        "environmentProfileIds": list(environment_profile_ids),
        "executableId": executable_id,
        "fixedArguments": list(fixed_arguments),
        "kind": kind,
        "maximumVariableArguments": maximum_variable_arguments,
        "minimumVariableArguments": minimum_variable_arguments,
        "profileId": profile_id,
        "risk": risk,
        "sideEffectClass": side_effect_class,
        "variableArgumentPattern": variable_argument_pattern,
    }
    return RuntimeProfilePrivilege(
        kind,
        profile_id,
        executable_id,
        risk,
        side_effect_class,
        fixed_arguments,
        minimum_variable_arguments,
        maximum_variable_arguments,
        variable_argument_pattern,
        allowed_cwd_root_ids,
        environment_profile_ids,
        allowed_plain_environment_names,
        allowed_secret_environment_names,
        allow_network,
        _fingerprint(payload),
    )


def _create_envelope(
    *,
    process_catalog_sha256: str,
    allowed_network_categories: tuple[str, ...],
    local_process_network: bool,
    root_capabilities: tuple[PrivilegeRootCapability, ...],
    environment_profiles: tuple[EnvironmentPrivilegeProfile, ...],
    executable_profiles: tuple[ExecutablePrivilegeProfile, ...],
    profile_privileges: tuple[RuntimeProfilePrivilege, ...],
) -> RuntimePrivilegeEnvelope:
    payload = {
        "allowedNetworkCategories": list(allowed_network_categories),
        "environmentProfiles": [
            {
                "allowedNames": list(item.allowed_names),
                "allowedSecretNames": list(item.allowed_secret_names),
                "profileId": item.profile_id,
            }
            for item in environment_profiles
        ],
        "executableProfiles": [_executable_payload(item) for item in executable_profiles],
        "localProcessNetwork": local_process_network,
        "processCatalogSha256": process_catalog_sha256,
        "profilePrivileges": [_runtime_profile_payload(item) for item in profile_privileges],
        "rootCapabilities": [_root_payload(item) for item in root_capabilities],
        "schemaVersion": 1,
    }
    return RuntimePrivilegeEnvelope(
        process_catalog_sha256,
        allowed_network_categories,
        local_process_network,
        root_capabilities,
        environment_profiles,
        executable_profiles,
        profile_privileges,
        _fingerprint(payload),
        1,
    )


def _replace_executable_fingerprint(
    item: ExecutablePrivilegeProfile,
    fingerprint: str,
) -> ExecutablePrivilegeProfile:
    return ExecutablePrivilegeProfile(
        item.executable_id,
        item.relative_path,
        item.trust,
        item.fixed_arguments,
        item.minimum_variable_arguments,
        item.maximum_variable_arguments,
        item.variable_argument_pattern,
        item.allow_shell_metacharacters,
        item.allowed_cwd_root_ids,
        item.environment_profile_ids,
        item.allowed_stdin_modes,
        item.app_container_filesystem,
        fingerprint,
    )


def _replace_runtime_profile_fingerprint(
    item: RuntimeProfilePrivilege,
    fingerprint: str,
) -> RuntimeProfilePrivilege:
    return RuntimeProfilePrivilege(
        item.kind,
        item.profile_id,
        item.executable_id,
        item.risk,
        item.side_effect_class,
        item.fixed_arguments,
        item.minimum_variable_arguments,
        item.maximum_variable_arguments,
        item.variable_argument_pattern,
        item.allowed_cwd_root_ids,
        item.environment_profile_ids,
        item.allowed_plain_environment_names,
        item.allowed_secret_environment_names,
        item.allow_network,
        fingerprint,
    )


def _replace_envelope_fingerprint(item: RuntimePrivilegeEnvelope, fingerprint: str) -> RuntimePrivilegeEnvelope:
    return RuntimePrivilegeEnvelope(
        item.process_catalog_sha256,
        item.allowed_network_categories,
        item.local_process_network,
        item.root_capabilities,
        item.environment_profiles,
        item.executable_profiles,
        item.profile_privileges,
        fingerprint,
        item.schema_version,
    )


def _catalog_filesystem(value: object) -> PrivilegeRootCapability:
    item = _mapping(value, "AppContainer filesystem capability")
    _exact_keys(item, {"access", "relativePath", "rootId"}, "AppContainer filesystem capability")
    return PrivilegeRootCapability(
        _text(item["rootId"], "rootId"),
        _text(item["relativePath"], "relativePath"),
        _text(item["access"], "access"),
    )


def _parse_root_capability(value: object, *, allow_cwd: bool) -> PrivilegeRootCapability:
    item = _mapping(value, "root capability")
    _exact_keys(item, {"access", "relativePath", "rootId"}, "root capability")
    result = PrivilegeRootCapability(
        _text(item["rootId"], "rootId"),
        _text(item["relativePath"], "relativePath"),
        _text(item["access"], "access"),
    )
    if not allow_cwd and result.access == "cwd":
        raise RuntimePrivilegeError("privilege_appcontainer_access", "AppContainer capability cannot be cwd")
    return result


def _parse_canonical_catalog(payload: bytes) -> Mapping[str, Any]:
    if not payload or len(payload) > _MAX_ENVELOPE_BYTES:
        raise RuntimePrivilegeError("process_catalog_size", "process catalog is outside release privilege limits")
    try:
        raw = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimePrivilegeError("process_catalog_malformed", "process catalog is malformed") from error
    mapping = _mapping(raw, "catalog")
    if _canonical_json(mapping) + b"\n" != payload:
        raise RuntimePrivilegeError("process_catalog_noncanonical", "process catalog must be canonical JSON")
    return mapping


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fingerprint(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RuntimePrivilegeError("privilege_type", f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise RuntimePrivilegeError("privilege_type", f"{label} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RuntimePrivilegeError("privilege_fields", f"{label} fields are not exact")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimePrivilegeError("privilege_type", f"{label} must be text")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimePrivilegeError("privilege_type", f"{label} must be boolean")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimePrivilegeError("privilege_type", f"{label} must be an integer")
    return value


def _identifier(value: str, label: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise RuntimePrivilegeError("privilege_identifier", f"{label} is invalid")
    return value


def _environment_name(value: str) -> str:
    if _ENVIRONMENT_NAME.fullmatch(value) is None:
        raise RuntimePrivilegeError("privilege_environment_name", "environment name is invalid")
    return value


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if not value or len(value) > maximum or "\x00" in value or "\r" in value or "\n" in value:
        raise RuntimePrivilegeError("privilege_text", f"{label} is invalid")
    return value


def _relative_path(value: str, *, allow_dot: bool) -> str:
    if value == "." and allow_dot:
        return value
    _bounded_text(value, "manifest-relative path", 512)
    if "\\" in value or value.startswith(("/", "//")):
        raise RuntimePrivilegeError("privilege_relative_path", "privilege path must be manifest-relative")
    parts = value.split("/")
    if any(not part or part in {".", ".."} or ":" in part for part in parts):
        raise RuntimePrivilegeError("privilege_relative_path", "privilege path is invalid")
    return value


def _arguments(values: Sequence[str]) -> None:
    if len(values) > 64:
        raise RuntimePrivilegeError("privilege_arguments", "fixed argument list is too large")
    for value in values:
        if not isinstance(value, str) or len(value) > 4096 or "\x00" in value or "\r" in value or "\n" in value:
            raise RuntimePrivilegeError("privilege_arguments", "fixed argument is invalid")


def _argument_bounds(minimum: int, maximum: int) -> None:
    if minimum < 0 or maximum < minimum or maximum > 64:
        raise RuntimePrivilegeError("privilege_argument_bounds", "variable argument bounds are invalid")


def _sorted_unique(
    values: Sequence[str],
    label: str,
    *,
    pattern: re.Pattern[str] | None = None,
    allowed: frozenset[str] | None = None,
) -> None:
    if tuple(sorted(set(values))) != tuple(values):
        raise RuntimePrivilegeError("privilege_order", f"{label} must be sorted and unique")
    if pattern is not None and any(pattern.fullmatch(value) is None for value in values):
        raise RuntimePrivilegeError("privilege_identifier", f"{label} contains an invalid value")
    if allowed is not None and not set(values) <= allowed:
        raise RuntimePrivilegeError("privilege_value", f"{label} contains an unsupported value")


def _canonical_records(values: Sequence[Any], label: str, *, key: Any) -> None:
    keys = [key(value) for value in values]
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise RuntimePrivilegeError("privilege_order", f"{label} must be sorted and unique")


def _utc_timestamp(value: str) -> datetime:
    if _UTC_TIMESTAMP.fullmatch(value) is None:
        raise RuntimePrivilegeError("privilege_receipt_timestamp", "Runtime privilege receipt timestamp is invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as error:
        raise RuntimePrivilegeError(
            "privilege_receipt_timestamp", "Runtime privilege receipt timestamp is invalid"
        ) from error


PRIVILEGE_APPROVAL_CONFIRMATION = _CONFIRMATION

__all__ = [
    "PRIVILEGE_APPROVAL_CONFIRMATION",
    "EnvironmentPrivilegeProfile",
    "ExecutablePrivilegeProfile",
    "PrivilegeRootCapability",
    "RuntimePrivilegeApprovalReceipt",
    "RuntimePrivilegeAssessment",
    "RuntimePrivilegeEnvelope",
    "RuntimePrivilegeError",
    "RuntimeProfilePrivilege",
    "assess_privilege_change",
    "build_privilege_envelope_from_process_catalog",
    "format_utc_timestamp",
    "parse_privilege_approval_receipt",
    "parse_privilege_envelope",
    "privilege_approval_receipt_payload",
    "privilege_envelope_payload",
    "validate_privilege_approval_receipt",
]
