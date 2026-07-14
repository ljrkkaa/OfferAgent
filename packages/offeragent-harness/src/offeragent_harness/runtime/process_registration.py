"""Workspace-scoped, explicitly confirmed user Process registrations.

The durable catalog is configuration only.  A Worker snapshots available
profiles once at startup and shares that immutable snapshot with Shell and
Hooks through the one ``ProcessSupervisorService``.  Management RPCs
may update the next-start catalog, but never mutate a running supervisor.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Any, Literal, cast

from offeragent_harness.error_codes import ResourceConflictCause
from offeragent_harness.ports import Clock, IdGenerator, ProcessStdinMode, UnitOfWorkFactory
from offeragent_harness.tools import canonical_json_sha256

from .process_supervisor import (
    ExecutableTrust,
    ProcessEnvironmentProfile,
    ProcessExecutableProfile,
    ProcessFilesystemAccess,
    ProcessFilesystemCapability,
    ProcessProfileError,
)
from .windows_process import AuthenticodeVerifier

PROCESS_REGISTRATION_COLLECTION = "process_registration_catalogs"
PROCESS_REGISTRATION_RECEIPT_COLLECTION = "process_registration_receipts"

_SCHEMA_VERSION = 1
_CATALOG_ENTITY_PREFIX = "process_catalog_"
_RECEIPT_ENTITY_PREFIX = "process_receipt_"
_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9][A-Za-z0-9_-]{0,123}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHALLENGE_ID = re.compile(r"^process-probe_[A-Za-z0-9_-]{16,128}$")
_ARGUMENT_PATTERN_MAX = 4096
_MAX_EXECUTABLE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_REGISTRATIONS = 128
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_CHALLENGE_TTL = timedelta(minutes=5)


class ProcessRegistrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ProcessRegistrationConflict(ProcessRegistrationError, ResourceConflictCause):
    pass


@dataclass(frozen=True, slots=True)
class ProcessFilesystemRegistration:
    root_id: str
    relative_path: str
    access: ProcessFilesystemAccess

    def capability(self) -> ProcessFilesystemCapability:
        return ProcessFilesystemCapability(self.root_id, self.relative_path, self.access)


@dataclass(frozen=True, slots=True)
class ExecutableRegistrationProposal:
    executable_id: str
    executable_path: str
    fixed_arguments: tuple[str, ...]
    minimum_variable_arguments: int
    maximum_variable_arguments: int
    variable_argument_pattern: str
    environment_profile_ids: frozenset[str]
    allowed_stdin_modes: frozenset[ProcessStdinMode]
    allowed_cwd_root_ids: frozenset[str]
    appcontainer_filesystem: tuple[ProcessFilesystemRegistration, ...]
    expected_revision: int = 0
    expected_content_hash: str | None = None

    def __post_init__(self) -> None:
        if _PROFILE_ID.fullmatch(self.executable_id) is None:
            raise ValueError("executable registration ID is invalid")
        if not self.executable_path or len(self.executable_path) > 32_767 or "\x00" in self.executable_path:
            raise ValueError("executable registration path is invalid")
        if not 0 <= self.minimum_variable_arguments <= self.maximum_variable_arguments <= 128:
            raise ValueError("executable registration argv bounds are invalid")
        if not self.variable_argument_pattern or len(self.variable_argument_pattern) > _ARGUMENT_PATTERN_MAX:
            raise ValueError("executable registration argv pattern is invalid")
        if not self.environment_profile_ids or not self.allowed_stdin_modes or not self.allowed_cwd_root_ids:
            raise ValueError("executable registration capabilities cannot be empty")
        if not self.appcontainer_filesystem:
            raise ValueError("network-denied executable requires narrow filesystem grants")
        if not self.allowed_cwd_root_ids <= {item.root_id for item in self.appcontainer_filesystem}:
            raise ValueError("every executable cwd root requires a narrow filesystem grant")
        _expected_record(self.expected_revision, self.expected_content_hash)
        object.__setattr__(self, "fixed_arguments", tuple(self.fixed_arguments))
        object.__setattr__(self, "environment_profile_ids", frozenset(self.environment_profile_ids))
        object.__setattr__(self, "allowed_stdin_modes", frozenset(self.allowed_stdin_modes))
        object.__setattr__(self, "allowed_cwd_root_ids", frozenset(self.allowed_cwd_root_ids))
        object.__setattr__(self, "appcontainer_filesystem", tuple(self.appcontainer_filesystem))


@dataclass(frozen=True, slots=True)
class EnvironmentRegistrationProposal:
    profile_id: str
    allowed_names: frozenset[str]
    allowed_secret_names: frozenset[str]
    expected_revision: int = 0
    expected_content_hash: str | None = None

    def __post_init__(self) -> None:
        ProcessEnvironmentProfile(self.profile_id, self.allowed_names, self.allowed_secret_names)
        _expected_record(self.expected_revision, self.expected_content_hash)
        object.__setattr__(self, "allowed_names", frozenset(item.upper() for item in self.allowed_names))
        object.__setattr__(
            self,
            "allowed_secret_names",
            frozenset(item.upper() for item in self.allowed_secret_names),
        )


@dataclass(frozen=True, slots=True)
class ProcessExecutableRegistration:
    workspace_id: str
    executable_id: str
    revision: int
    content_hash: str
    canonical_path: str
    fixed_root: str
    trust: ExecutableTrust
    authenticode_verified: bool
    file_sha256: str
    file_device: int
    file_index: int
    file_size: int
    profile_fingerprint: str
    fixed_arguments: tuple[str, ...]
    minimum_variable_arguments: int
    maximum_variable_arguments: int
    variable_argument_pattern: str
    environment_profile_ids: frozenset[str]
    allowed_stdin_modes: frozenset[ProcessStdinMode]
    allowed_cwd_root_ids: frozenset[str]
    appcontainer_filesystem: tuple[ProcessFilesystemRegistration, ...]

    def __post_init__(self) -> None:
        if not self.workspace_id or "\x00" in self.workspace_id or _PROFILE_ID.fullmatch(self.executable_id) is None:
            raise ValueError("executable registration identity is invalid")
        if self.revision < 1 or _DIGEST.fullmatch(self.content_hash) is None:
            raise ValueError("executable registration version is invalid")
        if _DIGEST.fullmatch(self.file_sha256) is None or _DIGEST.fullmatch(self.profile_fingerprint) is None:
            raise ValueError("executable registration digest is invalid")
        if self.file_device < 0 or self.file_index <= 0 or not 0 < self.file_size <= _MAX_EXECUTABLE_BYTES:
            raise ValueError("executable registration file identity is invalid")
        if self.trust is ExecutableTrust.SIGNED_RELEASE:
            raise ValueError("user executable cannot claim signed release trust")
        if self.authenticode_verified != (self.trust is ExecutableTrust.OS_AUTHENTICODE):
            raise ValueError("executable registration Authenticode status and trust differ")
        if self.content_hash != canonical_json_sha256(_executable_content_value(self)):
            raise ValueError("executable registration content hash differs")

    def to_profile(self) -> ProcessExecutableProfile:
        explicit = _explicit_windows_executable(self.canonical_path)
        captured = _capture_executable(explicit)
        if (
            str(captured.path) != self.canonical_path
            or str(captured.fixed_root) != self.fixed_root
            or captured.file_sha256 != self.file_sha256
            or captured.file_device != self.file_device
            or captured.file_index != self.file_index
            or captured.file_size != self.file_size
        ):
            raise ProcessRegistrationError(
                "process_registration_drift",
                "registered executable file identity or content changed",
            )
        profile = _profile_from_registration(self)
        if profile.fingerprint != self.profile_fingerprint:
            raise ProcessRegistrationError(
                "process_registration_drift",
                "registered executable profile fingerprint changed",
            )
        return profile


@dataclass(frozen=True, slots=True)
class ProcessEnvironmentRegistration:
    workspace_id: str
    profile_id: str
    revision: int
    content_hash: str
    allowed_names: frozenset[str]
    allowed_secret_names: frozenset[str]

    def __post_init__(self) -> None:
        if not self.workspace_id or "\x00" in self.workspace_id or self.revision < 1:
            raise ValueError("environment registration identity is invalid")
        profile = self.to_profile()
        object.__setattr__(self, "allowed_names", profile.allowed_names)
        object.__setattr__(self, "allowed_secret_names", profile.allowed_secret_names)
        if self.content_hash != canonical_json_sha256(_environment_content_value(self)):
            raise ValueError("environment registration content hash differs")

    def to_profile(self) -> ProcessEnvironmentProfile:
        return ProcessEnvironmentProfile(self.profile_id, self.allowed_names, self.allowed_secret_names)


@dataclass(frozen=True, slots=True)
class ProcessRegistrationCatalog:
    workspace_id: str
    revision: int
    snapshot_hash: str
    executables: tuple[ProcessExecutableRegistration, ...] = ()
    environments: tuple[ProcessEnvironmentRegistration, ...] = ()

    def __post_init__(self) -> None:
        if not self.workspace_id or "\x00" in self.workspace_id or self.revision < 0:
            raise ValueError("Process registration catalog identity is invalid")
        if len(self.executables) > _MAX_REGISTRATIONS or len(self.environments) > _MAX_REGISTRATIONS:
            raise ValueError("Process registration catalog exceeds limits")
        if tuple(sorted(self.executables, key=lambda item: item.executable_id)) != self.executables:
            raise ValueError("executable registrations must be sorted")
        if tuple(sorted(self.environments, key=lambda item: item.profile_id)) != self.environments:
            raise ValueError("environment registrations must be sorted")
        if len({item.executable_id for item in self.executables}) != len(self.executables):
            raise ValueError("executable registration IDs must be unique")
        if len({item.profile_id for item in self.environments}) != len(self.environments):
            raise ValueError("environment registration IDs must be unique")
        if any(item.workspace_id != self.workspace_id for item in self.executables) or any(
            item.workspace_id != self.workspace_id for item in self.environments
        ):
            raise ValueError("Process registration catalog crossed Workspace scope")
        if self.snapshot_hash != _catalog_snapshot_hash(self.revision, self.executables, self.environments):
            raise ValueError("Process registration catalog snapshot hash differs")


@dataclass(frozen=True, slots=True)
class ProcessRegistrationAvailability:
    kind: Literal["executable", "environment"]
    registration_id: str
    revision: int
    content_hash: str
    available: bool
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessRegistrationRuntimeSnapshot:
    catalog: ProcessRegistrationCatalog
    executable_profiles: tuple[ProcessExecutableProfile, ...]
    environment_profiles: tuple[ProcessEnvironmentProfile, ...]
    availability: tuple[ProcessRegistrationAvailability, ...]


@dataclass(frozen=True, slots=True)
class ProcessRegistrationProbe:
    challenge_id: str
    kind: Literal["executable", "environment"]
    registration_id: str
    content_hash: str
    expires_at: datetime
    executable: ProcessExecutableRegistration | None = None
    environment: ProcessEnvironmentRegistration | None = None


@dataclass(frozen=True, slots=True)
class ProcessRegistrationMutationResult:
    client_request_id: str
    kind: Literal["executable", "environment"]
    registration_id: str
    catalog_revision: int
    snapshot_hash: str
    record_revision: int
    record_content_hash: str
    deleted: bool
    restart_required: Literal[True] = True


@dataclass(frozen=True, slots=True)
class _CapturedExecutable:
    path: Path
    fixed_root: Path
    file_sha256: str
    file_device: int
    file_index: int
    file_size: int


@dataclass(frozen=True, slots=True)
class _PendingProbe:
    client_id: str
    expires_monotonic: float
    probe: ProcessRegistrationProbe
    expected_revision: int
    expected_content_hash: str | None
    executable_proposal: ExecutableRegistrationProposal | None
    environment_proposal: EnvironmentRegistrationProposal | None


class WorkspaceProcessRegistrationService:
    """Own the durable next-start catalog and current-Pipe confirmation challenges."""

    def __init__(
        self,
        *,
        workspace_id: str,
        unit_of_work: UnitOfWorkFactory,
        clock: Clock,
        ids: IdGenerator,
        authenticode: AuthenticodeVerifier,
        builtin_executables: Sequence[ProcessExecutableProfile],
        builtin_environments: Sequence[ProcessEnvironmentProfile],
        allowed_workspace_root_ids: frozenset[str],
        active_catalog_revision: int = 0,
    ) -> None:
        if not workspace_id or "\x00" in workspace_id:
            raise ValueError("Process registration service requires a Workspace identity")
        self._workspace_id = workspace_id
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._ids = ids
        self._authenticode = authenticode
        self._builtin_executables = MappingProxyType({item.executable_id: item for item in builtin_executables})
        self._builtin_environments = MappingProxyType({item.profile_id: item for item in builtin_environments})
        if len(self._builtin_executables) != len(tuple(builtin_executables)) or len(self._builtin_environments) != len(
            tuple(builtin_environments)
        ):
            raise ValueError("builtin Process registration IDs must be unique")
        roots = frozenset(allowed_workspace_root_ids)
        if not roots or any(_PROFILE_ID.fullmatch(item) is None for item in roots):
            raise ValueError("Process registration workspace roots are invalid")
        self._allowed_workspace_root_ids = roots
        if active_catalog_revision < 0:
            raise ValueError("active Process registration catalog revision cannot be negative")
        self._active_catalog_revision = active_catalog_revision
        self._challenges: dict[str, _PendingProbe] = {}
        self._lock = asyncio.Lock()

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def active_catalog_revision(self) -> int:
        """Catalog revision captured by the immutable running supervisor."""

        return self._active_catalog_revision

    async def catalog(self) -> ProcessRegistrationCatalog:
        async with self._unit_of_work.begin() as unit_of_work:
            raw = await unit_of_work.entities.get(PROCESS_REGISTRATION_COLLECTION, self._catalog_entity_id)
        return _empty_catalog(self._workspace_id) if raw is None else _parse_catalog(raw, self._workspace_id)

    async def runtime_snapshot(self) -> ProcessRegistrationRuntimeSnapshot:
        catalog = await self.catalog()
        environments: list[ProcessEnvironmentProfile] = []
        executables: list[ProcessExecutableProfile] = []
        availability: list[ProcessRegistrationAvailability] = []
        environment_ids = set(self._builtin_environments)
        for environment_record in catalog.environments:
            try:
                environment_profile = environment_record.to_profile()
                environments.append(environment_profile)
                environment_ids.add(environment_profile.profile_id)
                availability.append(_availability(environment_record, True, None))
            except (ProcessProfileError, TypeError, ValueError):
                availability.append(_availability(environment_record, False, "configuration_drift"))
        for executable_record in catalog.executables:
            try:
                if not executable_record.environment_profile_ids <= environment_ids:
                    raise ProcessRegistrationError(
                        "process_registration_environment_unavailable",
                        "registered executable environment is unavailable",
                    )
                if executable_record.trust is ExecutableTrust.OS_AUTHENTICODE and not await asyncio.to_thread(
                    self._authenticode.verify,
                    Path(executable_record.canonical_path),
                ):
                    raise ProcessRegistrationError(
                        "process_registration_authenticode_drift",
                        "registered executable no longer passes offline Authenticode",
                    )
                executable_profile = await asyncio.to_thread(executable_record.to_profile)
                executables.append(executable_profile)
                availability.append(_availability(executable_record, True, None))
            except (OSError, ProcessProfileError, ProcessRegistrationError, TypeError, ValueError):
                availability.append(_availability(executable_record, False, "file_or_profile_drift"))
        return ProcessRegistrationRuntimeSnapshot(
            catalog=catalog,
            executable_profiles=tuple(sorted(executables, key=lambda item: item.executable_id)),
            environment_profiles=tuple(sorted(environments, key=lambda item: item.profile_id)),
            availability=tuple(sorted(availability, key=lambda item: (item.kind, item.registration_id))),
        )

    async def probe_executable(
        self,
        *,
        client_id: str,
        proposal: ExecutableRegistrationProposal,
    ) -> ProcessRegistrationProbe:
        self._validate_client(client_id)
        if proposal.executable_id in self._builtin_executables:
            raise ProcessRegistrationConflict(
                "process_registration_builtin_collision",
                "user executable ID collides with the signed builtin catalog",
            )
        if not proposal.allowed_cwd_root_ids <= self._allowed_workspace_root_ids or any(
            item.root_id not in self._allowed_workspace_root_ids for item in proposal.appcontainer_filesystem
        ):
            raise ProcessRegistrationError(
                "process_registration_root_denied",
                "user executable requested an unknown Workspace root",
            )
        environments = {**self._builtin_environments}
        snapshot = await self.runtime_snapshot()
        environments.update({item.profile_id: item for item in snapshot.environment_profiles})
        if not proposal.environment_profile_ids <= environments.keys():
            raise ProcessRegistrationError(
                "process_registration_environment_unavailable",
                "user executable references an unavailable environment profile",
            )
        record = await asyncio.to_thread(self._probe_executable_sync, proposal)
        return await self._store_probe(
            client_id=client_id,
            kind="executable",
            registration_id=proposal.executable_id,
            content_hash=record.content_hash,
            executable=record,
            environment=None,
            expected_revision=proposal.expected_revision,
            expected_content_hash=proposal.expected_content_hash,
            executable_proposal=proposal,
            environment_proposal=None,
        )

    async def probe_environment(
        self,
        *,
        client_id: str,
        proposal: EnvironmentRegistrationProposal,
    ) -> ProcessRegistrationProbe:
        self._validate_client(client_id)
        if proposal.profile_id in self._builtin_environments:
            raise ProcessRegistrationConflict(
                "process_registration_builtin_collision",
                "user environment ID collides with the signed builtin catalog",
            )
        record = _environment_record(self._workspace_id, proposal, revision=1)
        return await self._store_probe(
            client_id=client_id,
            kind="environment",
            registration_id=proposal.profile_id,
            content_hash=record.content_hash,
            executable=None,
            environment=record,
            expected_revision=proposal.expected_revision,
            expected_content_hash=proposal.expected_content_hash,
            executable_proposal=None,
            environment_proposal=proposal,
        )

    async def confirm(
        self,
        *,
        client_id: str,
        challenge_id: str,
        expected_probe_content_hash: str,
        expected_catalog_revision: int,
        client_request_id: str,
    ) -> ProcessRegistrationMutationResult:
        self._validate_client(client_id)
        _mutation_identity(client_request_id, expected_catalog_revision)
        if _CHALLENGE_ID.fullmatch(challenge_id) is None or _DIGEST.fullmatch(expected_probe_content_hash) is None:
            raise ValueError("Process registration confirmation identity is invalid")
        request = {
            "challengeId": challenge_id,
            "clientRequestId": client_request_id,
            "expectedCatalogRevision": expected_catalog_revision,
            "expectedProbeContentHash": expected_probe_content_hash,
            "method": "process/registrations/confirm",
        }
        request_hash = canonical_json_sha256(request)
        replay = await self._replay_receipt(client_request_id, request_hash)
        if replay is not None:
            return replay
        async with self._lock:
            pending = self._challenges.get(challenge_id)
            if pending is None or pending.client_id != client_id:
                raise ProcessRegistrationError(
                    "process_registration_probe_unavailable",
                    "Process registration probe expired, was consumed, or belongs to another Pipe connection",
                )
            self._challenges.pop(challenge_id, None)
            if (
                self._clock.monotonic() >= pending.expires_monotonic
                or pending.probe.content_hash != expected_probe_content_hash
            ):
                raise ProcessRegistrationError(
                    "process_registration_probe_unavailable",
                    "Process registration probe expired, was consumed, or belongs to another Pipe connection",
                )
            if pending.executable_proposal is not None:
                refreshed = await asyncio.to_thread(self._probe_executable_sync, pending.executable_proposal)
                if refreshed.content_hash != pending.probe.content_hash:
                    raise ProcessRegistrationError(
                        "process_registration_probe_drift",
                        "executable changed after the confirmation probe",
                    )
                executable = refreshed
                environment = None
            else:
                assert pending.environment_proposal is not None
                environment = _environment_record(self._workspace_id, pending.environment_proposal, revision=1)
                executable = None
            return await self._commit_registration(
                pending=pending,
                executable=executable,
                environment=environment,
                expected_catalog_revision=expected_catalog_revision,
                client_request_id=client_request_id,
                request_hash=request_hash,
            )

    async def delete(
        self,
        *,
        client_id: str,
        kind: Literal["executable", "environment"],
        registration_id: str,
        expected_catalog_revision: int,
        expected_revision: int,
        expected_content_hash: str,
        client_request_id: str,
    ) -> ProcessRegistrationMutationResult:
        self._validate_client(client_id)
        _mutation_identity(client_request_id, expected_catalog_revision)
        if kind not in {"executable", "environment"} or _PROFILE_ID.fullmatch(registration_id) is None:
            raise ValueError("Process registration deletion target is invalid")
        _expected_record(expected_revision, expected_content_hash)
        request = {
            "clientRequestId": client_request_id,
            "expectedCatalogRevision": expected_catalog_revision,
            "expectedContentHash": expected_content_hash,
            "expectedRevision": expected_revision,
            "kind": kind,
            "method": "process/registrations/delete",
            "registrationId": registration_id,
        }
        request_hash = canonical_json_sha256(request)
        replay = await self._replay_receipt(client_request_id, request_hash)
        if replay is not None:
            return replay
        async with self._lock:
            async with self._unit_of_work.begin() as unit_of_work:
                raw = await unit_of_work.entities.get(PROCESS_REGISTRATION_COLLECTION, self._catalog_entity_id)
                catalog = _empty_catalog(self._workspace_id) if raw is None else _parse_catalog(raw, self._workspace_id)
                if catalog.revision != expected_catalog_revision:
                    raise ProcessRegistrationConflict(
                        "process_registration_revision_conflict",
                        "Process registration catalog revision changed",
                    )
                executables = list(catalog.executables)
                environments = list(catalog.environments)
                if kind == "executable":
                    current: ProcessExecutableRegistration | ProcessEnvironmentRegistration | None = next(
                        (item for item in executables if item.executable_id == registration_id),
                        None,
                    )
                else:
                    current = next(
                        (item for item in environments if item.profile_id == registration_id),
                        None,
                    )
                if (
                    current is None
                    or current.revision != expected_revision
                    or current.content_hash != expected_content_hash
                ):
                    raise ProcessRegistrationConflict(
                        "process_registration_record_conflict",
                        "Process registration revision or content hash changed",
                    )
                if kind == "environment" and any(
                    registration_id in item.environment_profile_ids for item in catalog.executables
                ):
                    raise ProcessRegistrationConflict(
                        "process_registration_in_use",
                        "environment registration is still referenced by an executable",
                    )
                if isinstance(current, ProcessExecutableRegistration):
                    executables.remove(current)
                else:
                    environments.remove(current)
                updated = _catalog(
                    self._workspace_id,
                    catalog.revision + 1,
                    tuple(executables),
                    tuple(environments),
                )
                result = ProcessRegistrationMutationResult(
                    client_request_id,
                    kind,
                    registration_id,
                    updated.revision,
                    updated.snapshot_hash,
                    current.revision,
                    current.content_hash,
                    True,
                )
                await self._write_catalog_and_receipt(
                    unit_of_work,
                    catalog,
                    updated,
                    client_request_id,
                    request_hash,
                    result,
                )
                await unit_of_work.commit()
                return result

    async def _store_probe(
        self,
        *,
        client_id: str,
        kind: Literal["executable", "environment"],
        registration_id: str,
        content_hash: str,
        executable: ProcessExecutableRegistration | None,
        environment: ProcessEnvironmentRegistration | None,
        expected_revision: int,
        expected_content_hash: str | None,
        executable_proposal: ExecutableRegistrationProposal | None,
        environment_proposal: EnvironmentRegistrationProposal | None,
    ) -> ProcessRegistrationProbe:
        challenge_id = self._ids.new_id("process-probe")
        if _CHALLENGE_ID.fullmatch(challenge_id) is None:
            raise ProcessRegistrationError(
                "process_registration_probe_identity",
                "Process registration probe generator returned an invalid identity",
            )
        expires_at = self._clock.utcnow() + _CHALLENGE_TTL
        probe = ProcessRegistrationProbe(
            challenge_id,
            kind,
            registration_id,
            content_hash,
            expires_at,
            executable,
            environment,
        )
        pending = _PendingProbe(
            client_id,
            self._clock.monotonic() + _CHALLENGE_TTL.total_seconds(),
            probe,
            expected_revision,
            expected_content_hash,
            executable_proposal,
            environment_proposal,
        )
        async with self._lock:
            now = self._clock.monotonic()
            self._challenges = {key: value for key, value in self._challenges.items() if value.expires_monotonic > now}
            self._challenges[challenge_id] = pending
        return probe

    def _probe_executable_sync(self, proposal: ExecutableRegistrationProposal) -> ProcessExecutableRegistration:
        first = _capture_executable(_explicit_windows_executable(proposal.executable_path))
        authenticode_verified = bool(self._authenticode.verify(first.path))
        second = _capture_executable(first.path)
        if first != second:
            raise ProcessRegistrationError(
                "process_registration_probe_drift",
                "executable changed during offline trust verification",
            )
        trust = ExecutableTrust.OS_AUTHENTICODE if authenticode_verified else ExecutableTrust.FIXED_HASH
        profile = ProcessExecutableProfile(
            executable_id=proposal.executable_id,
            executable=first.path,
            fixed_root=first.fixed_root,
            trust=trust,
            file_sha256=first.file_sha256,
            fixed_arguments=proposal.fixed_arguments,
            minimum_variable_arguments=proposal.minimum_variable_arguments,
            maximum_variable_arguments=proposal.maximum_variable_arguments,
            variable_argument_pattern=proposal.variable_argument_pattern,
            allow_shell_metacharacters=False,
            environment_profiles=proposal.environment_profile_ids,
            allowed_stdin_modes=proposal.allowed_stdin_modes,
            allowed_cwd_roots=proposal.allowed_cwd_root_ids,
            allow_network=False,
            appcontainer_filesystem=tuple(item.capability() for item in proposal.appcontainer_filesystem),
        )
        content_value = _executable_content_fields(
            executable_id=proposal.executable_id,
            canonical_path=str(first.path),
            fixed_root=str(first.fixed_root),
            trust=trust,
            authenticode_verified=authenticode_verified,
            file_sha256=first.file_sha256,
            file_device=first.file_device,
            file_index=first.file_index,
            file_size=first.file_size,
            profile_fingerprint=profile.fingerprint,
            fixed_arguments=proposal.fixed_arguments,
            minimum_variable_arguments=proposal.minimum_variable_arguments,
            maximum_variable_arguments=proposal.maximum_variable_arguments,
            variable_argument_pattern=proposal.variable_argument_pattern,
            environment_profile_ids=proposal.environment_profile_ids,
            allowed_stdin_modes=proposal.allowed_stdin_modes,
            allowed_cwd_root_ids=proposal.allowed_cwd_root_ids,
            appcontainer_filesystem=proposal.appcontainer_filesystem,
        )
        return ProcessExecutableRegistration(
            workspace_id=self._workspace_id,
            executable_id=proposal.executable_id,
            revision=1,
            content_hash=canonical_json_sha256(content_value),
            canonical_path=str(first.path),
            fixed_root=str(first.fixed_root),
            trust=trust,
            authenticode_verified=authenticode_verified,
            file_sha256=first.file_sha256,
            file_device=first.file_device,
            file_index=first.file_index,
            file_size=first.file_size,
            profile_fingerprint=profile.fingerprint,
            fixed_arguments=proposal.fixed_arguments,
            minimum_variable_arguments=proposal.minimum_variable_arguments,
            maximum_variable_arguments=proposal.maximum_variable_arguments,
            variable_argument_pattern=proposal.variable_argument_pattern,
            environment_profile_ids=proposal.environment_profile_ids,
            allowed_stdin_modes=proposal.allowed_stdin_modes,
            allowed_cwd_root_ids=proposal.allowed_cwd_root_ids,
            appcontainer_filesystem=proposal.appcontainer_filesystem,
        )

    async def _commit_registration(
        self,
        *,
        pending: _PendingProbe,
        executable: ProcessExecutableRegistration | None,
        environment: ProcessEnvironmentRegistration | None,
        expected_catalog_revision: int,
        client_request_id: str,
        request_hash: str,
    ) -> ProcessRegistrationMutationResult:
        async with self._unit_of_work.begin() as unit_of_work:
            receipt = await unit_of_work.entities.get(
                PROCESS_REGISTRATION_RECEIPT_COLLECTION,
                self._receipt_entity_id(client_request_id),
            )
            if receipt is not None:
                return _parse_receipt(receipt, self._workspace_id, client_request_id, request_hash)
            raw = await unit_of_work.entities.get(PROCESS_REGISTRATION_COLLECTION, self._catalog_entity_id)
            catalog = _empty_catalog(self._workspace_id) if raw is None else _parse_catalog(raw, self._workspace_id)
            if catalog.revision != expected_catalog_revision:
                raise ProcessRegistrationConflict(
                    "process_registration_revision_conflict",
                    "Process registration catalog revision changed",
                )
            executables = list(catalog.executables)
            environments = list(catalog.environments)
            if executable is not None:
                registration_id = executable.executable_id
                kind: Literal["executable", "environment"] = "executable"
                current_executable = next(
                    (item for item in executables if item.executable_id == registration_id),
                    None,
                )
                _require_expected_current(
                    current_executable,
                    pending.expected_revision,
                    pending.expected_content_hash,
                )
                next_revision = 1 if current_executable is None else current_executable.revision + 1
                executable_candidate = replace(
                    executable,
                    revision=next_revision,
                )
                if current_executable is None:
                    executables.append(executable_candidate)
                else:
                    executables[executables.index(current_executable)] = executable_candidate
                candidate_revision = executable_candidate.revision
                candidate_content_hash = executable_candidate.content_hash
            else:
                assert environment is not None
                registration_id = environment.profile_id
                kind = "environment"
                current_environment = next(
                    (item for item in environments if item.profile_id == registration_id),
                    None,
                )
                _require_expected_current(
                    current_environment,
                    pending.expected_revision,
                    pending.expected_content_hash,
                )
                next_revision = 1 if current_environment is None else current_environment.revision + 1
                environment_candidate = replace(
                    environment,
                    revision=next_revision,
                )
                if current_environment is None:
                    environments.append(environment_candidate)
                else:
                    environments[environments.index(current_environment)] = environment_candidate
                candidate_revision = environment_candidate.revision
                candidate_content_hash = environment_candidate.content_hash
            updated = _catalog(
                self._workspace_id,
                catalog.revision + 1,
                tuple(executables),
                tuple(environments),
            )
            result = ProcessRegistrationMutationResult(
                client_request_id,
                kind,
                registration_id,
                updated.revision,
                updated.snapshot_hash,
                candidate_revision,
                candidate_content_hash,
                False,
            )
            await self._write_catalog_and_receipt(
                unit_of_work,
                catalog,
                updated,
                client_request_id,
                request_hash,
                result,
            )
            await unit_of_work.commit()
            return result

    async def _write_catalog_and_receipt(
        self,
        unit_of_work: Any,
        current: ProcessRegistrationCatalog,
        updated: ProcessRegistrationCatalog,
        client_request_id: str,
        request_hash: str,
        result: ProcessRegistrationMutationResult,
    ) -> None:
        catalog_revision = await unit_of_work.entities.put(
            PROCESS_REGISTRATION_COLLECTION,
            self._catalog_entity_id,
            _catalog_value(updated),
            expected_revision=current.revision,
        )
        if catalog_revision != updated.revision:
            raise ProcessRegistrationError(
                "process_registration_storage_revision",
                "Process registration storage revision is inconsistent",
            )
        receipt_revision = await unit_of_work.entities.put(
            PROCESS_REGISTRATION_RECEIPT_COLLECTION,
            self._receipt_entity_id(client_request_id),
            _receipt_value(self._workspace_id, client_request_id, request_hash, result),
            expected_revision=0,
        )
        if receipt_revision != 1:
            raise ProcessRegistrationError(
                "process_registration_receipt_revision",
                "Process registration receipt revision is inconsistent",
            )

    async def _replay_receipt(
        self,
        client_request_id: str,
        request_hash: str,
    ) -> ProcessRegistrationMutationResult | None:
        async with self._unit_of_work.begin() as unit_of_work:
            raw = await unit_of_work.entities.get(
                PROCESS_REGISTRATION_RECEIPT_COLLECTION,
                self._receipt_entity_id(client_request_id),
            )
        return None if raw is None else _parse_receipt(raw, self._workspace_id, client_request_id, request_hash)

    @property
    def _catalog_entity_id(self) -> str:
        return _CATALOG_ENTITY_PREFIX + hashlib.sha256(self._workspace_id.encode()).hexdigest()

    def _receipt_entity_id(self, client_request_id: str) -> str:
        return (
            _RECEIPT_ENTITY_PREFIX + hashlib.sha256(f"{self._workspace_id}\0{client_request_id}".encode()).hexdigest()
        )

    @staticmethod
    def _validate_client(client_id: str) -> None:
        if not client_id or len(client_id) > 256 or "\x00" in client_id:
            raise ValueError("Process registration Pipe connection identity is invalid")


def merge_process_registration_snapshot(
    builtin_executables: Sequence[ProcessExecutableProfile],
    builtin_environments: Sequence[ProcessEnvironmentProfile],
    user: ProcessRegistrationRuntimeSnapshot,
) -> tuple[tuple[ProcessExecutableProfile, ...], tuple[ProcessEnvironmentProfile, ...]]:
    """Merge one startup snapshot without changing profile construction rules."""

    executables = {item.executable_id: item for item in builtin_executables}
    environments = {item.profile_id: item for item in builtin_environments}
    if len(executables) != len(tuple(builtin_executables)) or len(environments) != len(tuple(builtin_environments)):
        raise ValueError("builtin Process catalog IDs must be unique")
    for environment_profile in user.environment_profiles:
        if environment_profile.profile_id in environments:
            raise ProcessRegistrationConflict(
                "process_registration_builtin_collision",
                "user environment registration collides with the signed builtin catalog",
            )
        environments[environment_profile.profile_id] = environment_profile
    for executable_profile in user.executable_profiles:
        if executable_profile.executable_id in executables:
            raise ProcessRegistrationConflict(
                "process_registration_builtin_collision",
                "user executable registration collides with the signed builtin catalog",
            )
        if not executable_profile.environment_profiles <= environments.keys():
            continue
        executables[executable_profile.executable_id] = executable_profile
    return (
        tuple(sorted(executables.values(), key=lambda item: item.executable_id)),
        tuple(sorted(environments.values(), key=lambda item: item.profile_id)),
    )


def _explicit_windows_executable(value: str) -> Path:
    windows = PureWindowsPath(value)
    if (
        not windows.is_absolute()
        or not windows.drive
        or windows.root != "\\"
        or value.startswith(("\\\\", "//"))
        or windows.drive.startswith(("\\\\", "//"))
        or any(part in {"", ".", ".."} for part in windows.parts[1:])
        or windows.suffix.casefold() != ".exe"
        or windows.parent == PureWindowsPath(windows.anchor)
    ):
        raise ProcessRegistrationError(
            "process_registration_path_invalid",
            "Process registration requires an explicit local absolute .exe path",
        )
    lexical = Path(value)
    _reject_reparse_chain(lexical)
    try:
        canonical = lexical.resolve(strict=True)
    except OSError as error:
        raise ProcessRegistrationError(
            "process_registration_file_unavailable",
            "selected executable is unavailable",
        ) from error
    _reject_reparse_chain(canonical)
    return canonical


def _reject_reparse_chain(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise ProcessRegistrationError(
                "process_registration_file_unavailable",
                "selected executable path is unavailable",
            ) from error
        if current.is_symlink() or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ProcessRegistrationError(
                "process_registration_reparse_denied",
                "selected executable path contains a reparse point",
            )


def _capture_executable(path: Path) -> _CapturedExecutable:
    try:
        info = path.lstat()
        if (
            path.is_symlink()
            or stat.S_IFMT(info.st_mode) != stat.S_IFREG
            or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            or info.st_nlink != 1
            or info.st_size < 1
            or info.st_size > _MAX_EXECUTABLE_BYTES
        ):
            raise ProcessRegistrationError(
                "process_registration_file_type_denied",
                "selected executable must be one regular, non-linked file",
            )
        with path.open("rb", buffering=0) as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except ProcessRegistrationError:
        raise
    except OSError as error:
        raise ProcessRegistrationError(
            "process_registration_file_unavailable",
            "selected executable cannot be read",
        ) from error
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink)
    if (
        before_identity != after_identity
        or before.st_ino <= 0
        or before.st_nlink != 1
        or getattr(before, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ProcessRegistrationError(
            "process_registration_file_drift",
            "selected executable changed while its identity was captured",
        )
    _reject_reparse_chain(path)
    return _CapturedExecutable(
        path=path,
        fixed_root=path.parent.resolve(strict=True),
        file_sha256=f"sha256:{digest.hexdigest()}",
        file_device=int(before.st_dev),
        file_index=int(before.st_ino),
        file_size=int(before.st_size),
    )


def _profile_from_registration(
    record: ProcessExecutableRegistration,
    *,
    verify_registration: bool = True,
) -> ProcessExecutableProfile:
    profile = ProcessExecutableProfile(
        executable_id=record.executable_id,
        executable=Path(record.canonical_path),
        fixed_root=Path(record.fixed_root),
        trust=record.trust,
        file_sha256=record.file_sha256,
        fixed_arguments=record.fixed_arguments,
        minimum_variable_arguments=record.minimum_variable_arguments,
        maximum_variable_arguments=record.maximum_variable_arguments,
        variable_argument_pattern=record.variable_argument_pattern,
        allow_shell_metacharacters=False,
        environment_profiles=record.environment_profile_ids,
        allowed_stdin_modes=record.allowed_stdin_modes,
        allowed_cwd_roots=record.allowed_cwd_root_ids,
        allow_network=False,
        appcontainer_filesystem=tuple(item.capability() for item in record.appcontainer_filesystem),
    )
    if verify_registration and (
        profile.captured_file_device != record.file_device
        or profile.captured_file_index != record.file_index
        or profile.captured_file_size != record.file_size
        or profile.captured_content_sha256 != record.file_sha256
    ):
        raise ProcessRegistrationError(
            "process_registration_drift",
            "registered executable file identity changed",
        )
    return profile


def _environment_record(
    workspace_id: str,
    proposal: EnvironmentRegistrationProposal,
    *,
    revision: int,
) -> ProcessEnvironmentRegistration:
    profile = ProcessEnvironmentProfile(
        proposal.profile_id,
        proposal.allowed_names,
        proposal.allowed_secret_names,
    )
    content_hash = canonical_json_sha256(
        {
            "allowedNames": sorted(profile.allowed_names),
            "allowedSecretNames": sorted(profile.allowed_secret_names),
            "profileId": profile.profile_id,
        }
    )
    return ProcessEnvironmentRegistration(
        workspace_id,
        profile.profile_id,
        revision,
        content_hash,
        profile.allowed_names,
        profile.allowed_secret_names,
    )


def _availability(
    record: ProcessExecutableRegistration | ProcessEnvironmentRegistration,
    available: bool,
    reason: str | None,
) -> ProcessRegistrationAvailability:
    if isinstance(record, ProcessExecutableRegistration):
        return ProcessRegistrationAvailability(
            "executable", record.executable_id, record.revision, record.content_hash, available, reason
        )
    return ProcessRegistrationAvailability(
        "environment", record.profile_id, record.revision, record.content_hash, available, reason
    )


def _expected_record(revision: int, content_hash: str | None) -> None:
    if revision < 0 or (revision == 0) != (content_hash is None):
        raise ValueError("expected Process registration revision/hash pair is invalid")
    if content_hash is not None and _DIGEST.fullmatch(content_hash) is None:
        raise ValueError("expected Process registration content hash is invalid")


def _require_expected_current(
    current: ProcessExecutableRegistration | ProcessEnvironmentRegistration | None,
    expected_revision: int,
    expected_content_hash: str | None,
) -> None:
    if current is None:
        if expected_revision != 0 or expected_content_hash is not None:
            raise ProcessRegistrationConflict(
                "process_registration_record_conflict",
                "Process registration was deleted or replaced",
            )
        return
    if current.revision != expected_revision or current.content_hash != expected_content_hash:
        raise ProcessRegistrationConflict(
            "process_registration_record_conflict",
            "Process registration revision or content hash changed",
        )


def _mutation_identity(client_request_id: str, expected_catalog_revision: int) -> None:
    if _REQUEST_ID.fullmatch(client_request_id) is None or expected_catalog_revision < 0:
        raise ValueError("Process registration mutation identity is invalid")


def _empty_catalog(workspace_id: str) -> ProcessRegistrationCatalog:
    return _catalog(workspace_id, 0, (), ())


def _catalog(
    workspace_id: str,
    revision: int,
    executables: Sequence[ProcessExecutableRegistration],
    environments: Sequence[ProcessEnvironmentRegistration],
) -> ProcessRegistrationCatalog:
    sorted_executables = tuple(sorted(executables, key=lambda item: item.executable_id))
    sorted_environments = tuple(sorted(environments, key=lambda item: item.profile_id))
    return ProcessRegistrationCatalog(
        workspace_id,
        revision,
        _catalog_snapshot_hash(revision, sorted_executables, sorted_environments),
        sorted_executables,
        sorted_environments,
    )


def _catalog_snapshot_hash(
    revision: int,
    executables: Sequence[ProcessExecutableRegistration],
    environments: Sequence[ProcessEnvironmentRegistration],
) -> str:
    return canonical_json_sha256(
        {
            "executables": [
                {"contentHash": item.content_hash, "executableId": item.executable_id, "revision": item.revision}
                for item in executables
            ],
            "environments": [
                {"contentHash": item.content_hash, "profileId": item.profile_id, "revision": item.revision}
                for item in environments
            ],
            "revision": revision,
        }
    )


def _executable_content_value(
    record: ProcessExecutableRegistration,
    *,
    profile_fingerprint: str | None = None,
) -> dict[str, Any]:
    return _executable_content_fields(
        executable_id=record.executable_id,
        canonical_path=record.canonical_path,
        fixed_root=record.fixed_root,
        trust=record.trust,
        authenticode_verified=record.authenticode_verified,
        file_sha256=record.file_sha256,
        file_device=record.file_device,
        file_index=record.file_index,
        file_size=record.file_size,
        profile_fingerprint=profile_fingerprint or record.profile_fingerprint,
        fixed_arguments=record.fixed_arguments,
        minimum_variable_arguments=record.minimum_variable_arguments,
        maximum_variable_arguments=record.maximum_variable_arguments,
        variable_argument_pattern=record.variable_argument_pattern,
        environment_profile_ids=record.environment_profile_ids,
        allowed_stdin_modes=record.allowed_stdin_modes,
        allowed_cwd_root_ids=record.allowed_cwd_root_ids,
        appcontainer_filesystem=record.appcontainer_filesystem,
    )


def _executable_content_fields(
    *,
    executable_id: str,
    canonical_path: str,
    fixed_root: str,
    trust: ExecutableTrust,
    authenticode_verified: bool,
    file_sha256: str,
    file_device: int,
    file_index: int,
    file_size: int,
    profile_fingerprint: str,
    fixed_arguments: Sequence[str],
    minimum_variable_arguments: int,
    maximum_variable_arguments: int,
    variable_argument_pattern: str,
    environment_profile_ids: frozenset[str],
    allowed_stdin_modes: frozenset[ProcessStdinMode],
    allowed_cwd_root_ids: frozenset[str],
    appcontainer_filesystem: Sequence[ProcessFilesystemRegistration],
) -> dict[str, Any]:
    return {
        "allowNetwork": False,
        "allowedCwdRootIds": sorted(allowed_cwd_root_ids),
        "allowedStdinModes": sorted(item.value for item in allowed_stdin_modes),
        "appContainerFilesystem": [
            {"access": item.access.value, "relativePath": item.relative_path, "rootId": item.root_id}
            for item in appcontainer_filesystem
        ],
        "authenticodeVerified": authenticode_verified,
        "canonicalPath": canonical_path,
        "environmentProfileIds": sorted(environment_profile_ids),
        "executableId": executable_id,
        # Windows file identifiers routinely exceed JavaScript's exact integer
        # range, so the cross-language canonical catalog stores them as decimal
        # strings while the runtime model retains integers.
        "fileDevice": str(file_device),
        "fileIndex": str(file_index),
        "fileSha256": file_sha256,
        "fileSize": file_size,
        "fixedArguments": list(fixed_arguments),
        "fixedRoot": fixed_root,
        "maximumVariableArguments": maximum_variable_arguments,
        "minimumVariableArguments": minimum_variable_arguments,
        "profileFingerprint": profile_fingerprint,
        "trust": trust.value,
        "variableArgumentPattern": variable_argument_pattern,
    }


def _environment_content_value(record: ProcessEnvironmentRegistration) -> dict[str, Any]:
    return {
        "allowedNames": sorted(record.allowed_names),
        "allowedSecretNames": sorted(record.allowed_secret_names),
        "profileId": record.profile_id,
    }


def _catalog_value(catalog: ProcessRegistrationCatalog) -> dict[str, Any]:
    return {
        "catalogRevision": catalog.revision,
        "environments": [
            {
                **_environment_content_value(item),
                "contentHash": item.content_hash,
                "revision": item.revision,
                "workspaceId": item.workspace_id,
            }
            for item in catalog.environments
        ],
        "executables": [
            {
                **_executable_content_value(item),
                "contentHash": item.content_hash,
                "revision": item.revision,
                "workspaceId": item.workspace_id,
            }
            for item in catalog.executables
        ],
        "schemaVersion": _SCHEMA_VERSION,
        "snapshotHash": catalog.snapshot_hash,
        "workspaceId": catalog.workspace_id,
    }


def _parse_catalog(value: object, workspace_id: str) -> ProcessRegistrationCatalog:
    raw = _exact_mapping(
        value,
        {"catalogRevision", "environments", "executables", "schemaVersion", "snapshotHash", "workspaceId"},
        "Process registration catalog",
    )
    if _integer(raw["schemaVersion"], "schemaVersion", minimum=1) != _SCHEMA_VERSION:
        raise ProcessRegistrationError("process_registration_schema", "Process registration schema is unsupported")
    if _text(raw["workspaceId"], "workspaceId", 1024) != workspace_id:
        raise ProcessRegistrationError(
            "process_registration_workspace_mismatch",
            "Process registration catalog belongs to another Workspace",
        )
    executables = tuple(_parse_executable(item, workspace_id) for item in _sequence(raw["executables"], "executables"))
    environments = tuple(
        _parse_environment(item, workspace_id) for item in _sequence(raw["environments"], "environments")
    )
    return ProcessRegistrationCatalog(
        workspace_id,
        _integer(raw["catalogRevision"], "catalogRevision", minimum=0),
        _digest(raw["snapshotHash"], "snapshotHash"),
        executables,
        environments,
    )


def _parse_executable(value: object, workspace_id: str) -> ProcessExecutableRegistration:
    keys = {
        "allowNetwork",
        "allowedCwdRootIds",
        "allowedStdinModes",
        "appContainerFilesystem",
        "authenticodeVerified",
        "canonicalPath",
        "contentHash",
        "environmentProfileIds",
        "executableId",
        "fileDevice",
        "fileIndex",
        "fileSha256",
        "fileSize",
        "fixedArguments",
        "fixedRoot",
        "maximumVariableArguments",
        "minimumVariableArguments",
        "profileFingerprint",
        "revision",
        "trust",
        "variableArgumentPattern",
        "workspaceId",
    }
    raw = _exact_mapping(value, keys, "executable registration")
    if _boolean(raw["allowNetwork"], "allowNetwork"):
        raise ProcessRegistrationError(
            "process_registration_network_denied",
            "persisted user executable cannot authorize network access",
        )
    try:
        trust = ExecutableTrust(_text(raw["trust"], "trust", 32))
        stdin_modes = frozenset(ProcessStdinMode(item) for item in _text_sequence(raw["allowedStdinModes"]))
    except ValueError as error:
        raise ProcessRegistrationError(
            "process_registration_enum",
            "persisted Process registration enum is invalid",
        ) from error
    filesystems = tuple(
        ProcessFilesystemRegistration(
            _text(item["rootId"], "rootId", 128),
            _text(item["relativePath"], "relativePath", 1024),
            ProcessFilesystemAccess(_text(item["access"], "access", 32)),
        )
        for item in (
            _exact_mapping(entry, {"access", "relativePath", "rootId"}, "filesystem registration")
            for entry in _sequence(raw["appContainerFilesystem"], "appContainerFilesystem")
        )
    )
    return ProcessExecutableRegistration(
        workspace_id=_workspace(raw["workspaceId"], workspace_id),
        executable_id=_text(raw["executableId"], "executableId", 128),
        revision=_integer(raw["revision"], "revision", minimum=1),
        content_hash=_digest(raw["contentHash"], "contentHash"),
        canonical_path=_text(raw["canonicalPath"], "canonicalPath", 32_767),
        fixed_root=_text(raw["fixedRoot"], "fixedRoot", 32_767),
        trust=trust,
        authenticode_verified=_boolean(raw["authenticodeVerified"], "authenticodeVerified"),
        file_sha256=_digest(raw["fileSha256"], "fileSha256"),
        file_device=_decimal_integer(raw["fileDevice"], "fileDevice", minimum=0),
        file_index=_decimal_integer(raw["fileIndex"], "fileIndex", minimum=1),
        file_size=_integer(raw["fileSize"], "fileSize", minimum=1),
        profile_fingerprint=_digest(raw["profileFingerprint"], "profileFingerprint"),
        fixed_arguments=tuple(_text_sequence(raw["fixedArguments"], maximum=128, item_maximum=4096)),
        minimum_variable_arguments=_integer(raw["minimumVariableArguments"], "minimumVariableArguments", minimum=0),
        maximum_variable_arguments=_integer(raw["maximumVariableArguments"], "maximumVariableArguments", minimum=0),
        variable_argument_pattern=_text(raw["variableArgumentPattern"], "variableArgumentPattern", 4096),
        environment_profile_ids=frozenset(_text_sequence(raw["environmentProfileIds"], maximum=128)),
        allowed_stdin_modes=stdin_modes,
        allowed_cwd_root_ids=frozenset(_text_sequence(raw["allowedCwdRootIds"], maximum=128)),
        appcontainer_filesystem=filesystems,
    )


def _parse_environment(value: object, workspace_id: str) -> ProcessEnvironmentRegistration:
    raw = _exact_mapping(
        value,
        {"allowedNames", "allowedSecretNames", "contentHash", "profileId", "revision", "workspaceId"},
        "environment registration",
    )
    return ProcessEnvironmentRegistration(
        workspace_id=_workspace(raw["workspaceId"], workspace_id),
        profile_id=_text(raw["profileId"], "profileId", 128),
        revision=_integer(raw["revision"], "revision", minimum=1),
        content_hash=_digest(raw["contentHash"], "contentHash"),
        allowed_names=frozenset(_text_sequence(raw["allowedNames"], maximum=128)),
        allowed_secret_names=frozenset(_text_sequence(raw["allowedSecretNames"], maximum=128)),
    )


def _receipt_value(
    workspace_id: str,
    client_request_id: str,
    request_hash: str,
    result: ProcessRegistrationMutationResult,
) -> dict[str, Any]:
    return {
        "clientRequestId": client_request_id,
        "requestHash": request_hash,
        "result": {
            "catalogRevision": result.catalog_revision,
            "clientRequestId": result.client_request_id,
            "deleted": result.deleted,
            "kind": result.kind,
            "recordContentHash": result.record_content_hash,
            "recordRevision": result.record_revision,
            "registrationId": result.registration_id,
            "restartRequired": True,
            "snapshotHash": result.snapshot_hash,
        },
        "schemaVersion": _SCHEMA_VERSION,
        "workspaceId": workspace_id,
    }


def _parse_receipt(
    value: object,
    workspace_id: str,
    client_request_id: str,
    request_hash: str,
) -> ProcessRegistrationMutationResult:
    raw = _exact_mapping(
        value,
        {"clientRequestId", "requestHash", "result", "schemaVersion", "workspaceId"},
        "Process registration receipt",
    )
    if (
        _integer(raw["schemaVersion"], "schemaVersion", minimum=1) != _SCHEMA_VERSION
        or _workspace(raw["workspaceId"], workspace_id) != workspace_id
        or _text(raw["clientRequestId"], "clientRequestId", 128) != client_request_id
        or _digest(raw["requestHash"], "requestHash") != request_hash
    ):
        raise ProcessRegistrationConflict(
            "process_registration_idempotency_conflict",
            "clientRequestId is already bound to another Process registration request",
        )
    result = _exact_mapping(
        raw["result"],
        {
            "catalogRevision",
            "clientRequestId",
            "deleted",
            "kind",
            "recordContentHash",
            "recordRevision",
            "registrationId",
            "restartRequired",
            "snapshotHash",
        },
        "Process registration receipt result",
    )
    raw_kind = _text(result["kind"], "kind", 32)
    if raw_kind not in {"executable", "environment"} or not _boolean(result["restartRequired"], "restartRequired"):
        raise ProcessRegistrationError(
            "process_registration_receipt_corrupt",
            "Process registration receipt result is invalid",
        )
    return ProcessRegistrationMutationResult(
        client_request_id=_text(result["clientRequestId"], "clientRequestId", 128),
        kind=cast(Literal["executable", "environment"], raw_kind),
        registration_id=_text(result["registrationId"], "registrationId", 128),
        catalog_revision=_integer(result["catalogRevision"], "catalogRevision", minimum=1),
        snapshot_hash=_digest(result["snapshotHash"], "snapshotHash"),
        record_revision=_integer(result["recordRevision"], "recordRevision", minimum=1),
        record_content_hash=_digest(result["recordContentHash"], "recordContentHash"),
        deleted=_boolean(result["deleted"], "deleted"),
    )


def _exact_mapping(value: object, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value) or set(value) != keys:
        raise ProcessRegistrationError("process_registration_storage_corrupt", f"{label} fields are invalid")
    return value


def _sequence(value: object, label: str, *, maximum: int = _MAX_REGISTRATIONS) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise ProcessRegistrationError("process_registration_storage_corrupt", f"{label} must be a bounded array")
    return value


def _text_sequence(value: object, *, maximum: int = 128, item_maximum: int = 128) -> tuple[str, ...]:
    values = _sequence(value, "text list", maximum=maximum)
    return tuple(_text(item, "text list item", item_maximum) for item in values)


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ProcessRegistrationError("process_registration_storage_corrupt", f"{label} is invalid")
    return value


def _integer(value: object, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum or value > 9_223_372_036_854_775_807:
        raise ProcessRegistrationError("process_registration_storage_corrupt", f"{label} is invalid")
    return value


def _decimal_integer(value: object, label: str, *, minimum: int) -> int:
    text = _text(value, label, 32)
    if re.fullmatch(r"0|[1-9][0-9]{0,31}", text) is None:
        raise ProcessRegistrationError("process_registration_storage_corrupt", f"{label} is invalid")
    result = int(text)
    if result < minimum:
        raise ProcessRegistrationError("process_registration_storage_corrupt", f"{label} is invalid")
    return result


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ProcessRegistrationError("process_registration_storage_corrupt", f"{label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    text = _text(value, label, 71)
    if _DIGEST.fullmatch(text) is None:
        raise ProcessRegistrationError("process_registration_storage_corrupt", f"{label} is invalid")
    return text


def _workspace(value: object, expected: str) -> str:
    workspace_id = _text(value, "workspaceId", 1024)
    if workspace_id != expected:
        raise ProcessRegistrationError(
            "process_registration_workspace_mismatch",
            "Process registration belongs to another Workspace",
        )
    return workspace_id


__all__ = [
    "PROCESS_REGISTRATION_COLLECTION",
    "PROCESS_REGISTRATION_RECEIPT_COLLECTION",
    "EnvironmentRegistrationProposal",
    "ExecutableRegistrationProposal",
    "ProcessEnvironmentRegistration",
    "ProcessExecutableRegistration",
    "ProcessFilesystemRegistration",
    "ProcessRegistrationAvailability",
    "ProcessRegistrationCatalog",
    "ProcessRegistrationConflict",
    "ProcessRegistrationError",
    "ProcessRegistrationMutationResult",
    "ProcessRegistrationProbe",
    "ProcessRegistrationRuntimeSnapshot",
    "WorkspaceProcessRegistrationService",
    "merge_process_registration_snapshot",
]
