"""Durable, fail-closed Shell profile activation and trust state.

The persisted document contains only bounded, non-secret profile configuration.
Executable paths and arbitrary argv never enter Run configuration: profiles bind
to the Worker's separately registered executable ID and captured fingerprint.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import CancellationToken, UnitOfWorkFactory
from offeragent_harness.tools import SideEffectClass, canonical_json_sha256

from .profiles import ShellCommandProfile

_COLLECTION = "shell_profile_catalogs"
_MAX_PROFILES = 256


class ShellProfileSource(str, Enum):
    SIGNED_BUILTIN = "signed_builtin"
    USER = "user"


class ShellProfileTrust(str, Enum):
    SIGNED = "signed"
    CONFIRMED = "confirmed"
    CONFIRMATION_REQUIRED = "confirmation_required"


@dataclass(frozen=True, slots=True)
class ShellProfileRecord:
    profile: ShellCommandProfile
    source: ShellProfileSource
    trust: ShellProfileTrust
    enabled: bool
    revision: int
    content_hash: str

    def __post_init__(self) -> None:
        if self.revision < 1 or self.content_hash != shell_profile_hash(self.profile):
            raise ValueError("Shell profile record revision/content hash is invalid")
        if self.source is ShellProfileSource.SIGNED_BUILTIN and self.trust is not ShellProfileTrust.SIGNED:
            raise ValueError("builtin Shell profiles require signed trust")
        if self.source is ShellProfileSource.USER and self.trust is ShellProfileTrust.SIGNED:
            raise ValueError("user Shell profiles cannot claim signed trust")
        if self.enabled and self.trust is ShellProfileTrust.CONFIRMATION_REQUIRED:
            raise ValueError("unconfirmed Shell profiles cannot be enabled")


@dataclass(frozen=True, slots=True)
class ShellProfileSnapshot:
    workspace_id: str
    records: tuple[ShellProfileRecord, ...]
    revision: int
    idempotency_key: str
    snapshot_hash: str

    def __post_init__(self) -> None:
        if not self.workspace_id or "\x00" in self.workspace_id or self.revision < 1:
            raise ValueError("Shell profile snapshot identity/revision is invalid")
        if not self.idempotency_key or "\x00" in self.idempotency_key:
            raise ValueError("Shell profile snapshot idempotency key is invalid")
        if len(self.records) > _MAX_PROFILES:
            raise ValueError("Shell profile snapshot exceeds the profile limit")
        identities = [record.profile.profile_id for record in self.records]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("Shell profile snapshot records must be uniquely sorted")
        if self.snapshot_hash != _records_hash(self.records):
            raise ValueError("Shell profile snapshot hash is invalid")


class EntityShellProfileStateStore:
    """One CAS-protected catalog document per Workspace."""

    def __init__(self, unit_of_work: UnitOfWorkFactory) -> None:
        self._unit_of_work = unit_of_work

    async def load(self, workspace_id: str) -> ShellProfileSnapshot | None:
        async with self._unit_of_work.begin() as unit_of_work:
            raw = await unit_of_work.entities.get(_COLLECTION, _entity_id(workspace_id))
        return None if raw is None else _parse_snapshot(raw, workspace_id)

    async def save(
        self,
        workspace_id: str,
        records: Sequence[ShellProfileRecord],
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ShellProfileSnapshot:
        ordered = tuple(sorted(records, key=lambda item: item.profile.profile_id))
        candidate = ShellProfileSnapshot(
            workspace_id,
            ordered,
            expected_revision + 1,
            idempotency_key,
            _records_hash(ordered),
        )
        current = await self.load(workspace_id)
        if current is not None and current.idempotency_key == idempotency_key:
            if current.records != candidate.records:
                raise ValueError("Shell profile idempotency key is bound to another catalog")
            return current
        async with self._unit_of_work.begin() as unit_of_work:
            revision = await unit_of_work.entities.put(
                _COLLECTION,
                _entity_id(workspace_id),
                _snapshot_value(candidate),
                expected_revision=expected_revision,
            )
            if revision != candidate.revision:
                raise ValueError("Shell profile entity revision is inconsistent")
            await unit_of_work.commit()
        return candidate


class ShellProfileService:
    """Merge signed builtins and manage explicitly confirmed user profiles."""

    def __init__(
        self,
        *,
        workspace_id: str,
        builtin_profiles: Sequence[ShellCommandProfile],
        state_store: EntityShellProfileStateStore,
    ) -> None:
        if not workspace_id or "\x00" in workspace_id:
            raise ValueError("Shell profile service requires a Workspace ID")
        builtins = {profile.profile_id: profile for profile in builtin_profiles}
        if len(builtins) != len(builtin_profiles) or len(builtins) > _MAX_PROFILES:
            raise ValueError("signed builtin Shell profile IDs must be bounded and unique")
        self.workspace_id = workspace_id
        self._builtins = builtins
        self._state_store = state_store
        self._records: dict[str, ShellProfileRecord] = {}
        self._snapshot: ShellProfileSnapshot | None = None
        self._lock = asyncio.Lock()

    @property
    def initialized(self) -> bool:
        return self._snapshot is not None

    @property
    def snapshot(self) -> ShellProfileSnapshot:
        if self._snapshot is None:
            raise RuntimeError("Shell profile service is not initialized")
        return self._snapshot

    @property
    def records(self) -> tuple[ShellProfileRecord, ...]:
        return self.snapshot.records

    @property
    def active_profiles(self) -> tuple[ShellCommandProfile, ...]:
        return tuple(
            record.profile
            for record in self.records
            if record.enabled and record.trust in {ShellProfileTrust.SIGNED, ShellProfileTrust.CONFIRMED}
        )

    async def initialize(self, cancellation: CancellationToken) -> ShellProfileSnapshot:
        cancellation.checkpoint()
        async with self._lock:
            if self._snapshot is not None:
                return self._snapshot
            persisted = await self._state_store.load(self.workspace_id)
            cancellation.checkpoint()
            records = {} if persisted is None else {item.profile.profile_id: item for item in persisted.records}
            merged: dict[str, ShellProfileRecord] = {
                profile_id: record
                for profile_id, record in records.items()
                if record.source is ShellProfileSource.USER and profile_id not in self._builtins
            }
            changed = persisted is None or len(merged) != len(records)
            for profile_id, profile in self._builtins.items():
                current = records.get(profile_id)
                content_hash = shell_profile_hash(profile)
                if (
                    current is not None
                    and current.source is ShellProfileSource.SIGNED_BUILTIN
                    and current.content_hash == content_hash
                ):
                    merged[profile_id] = ShellProfileRecord(
                        profile,
                        ShellProfileSource.SIGNED_BUILTIN,
                        ShellProfileTrust.SIGNED,
                        current.enabled,
                        current.revision,
                        content_hash,
                    )
                else:
                    merged[profile_id] = ShellProfileRecord(
                        profile,
                        ShellProfileSource.SIGNED_BUILTIN,
                        ShellProfileTrust.SIGNED,
                        False,
                        1 if current is None else current.revision + 1,
                        content_hash,
                    )
                    changed = True
            ordered = tuple(sorted(merged.values(), key=lambda item: item.profile.profile_id))
            if not changed and persisted is not None:
                snapshot = ShellProfileSnapshot(
                    self.workspace_id,
                    ordered,
                    persisted.revision,
                    persisted.idempotency_key,
                    _records_hash(ordered),
                )
            else:
                expected = 0 if persisted is None else persisted.revision
                digest = _records_hash(ordered).removeprefix("sha256:")
                snapshot = await self._state_store.save(
                    self.workspace_id,
                    ordered,
                    expected_revision=expected,
                    idempotency_key=f"shell-profile-init:{self.workspace_id}:{digest}",
                )
            self._records = {item.profile.profile_id: item for item in snapshot.records}
            self._snapshot = snapshot
            return snapshot

    async def install_user_profile(
        self,
        profile: ShellCommandProfile,
        *,
        expected_revision: int,
        idempotency_key: str,
        cancellation: CancellationToken,
    ) -> ShellProfileRecord:
        if profile.profile_id in self._builtins:
            raise ValueError("user Shell profile cannot replace a signed builtin")
        if (
            profile.risk is RiskClass.READ
            or profile.side_effect_class in {SideEffectClass.NONE, SideEffectClass.READ}
            or profile.concurrency_safe
            or profile.retryable
        ):
            raise ValueError("user Shell profiles must remain approval-gated and non-concurrent")
        cancellation.checkpoint()
        async with self._lock:
            self._require_initialized()
            current = self._records.get(profile.profile_id)
            actual = 0 if current is None else current.revision
            if current is not None and current.source is not ShellProfileSource.USER:
                raise ValueError("user Shell profile cannot replace a signed builtin")
            if actual != expected_revision:
                raise ValueError(f"Shell profile expected revision {expected_revision}, actual {actual}")
            record = ShellProfileRecord(
                profile,
                ShellProfileSource.USER,
                ShellProfileTrust.CONFIRMATION_REQUIRED,
                False,
                actual + 1,
                shell_profile_hash(profile),
            )
            return await self._persist_record(record, idempotency_key, cancellation)

    async def confirm_user_profile(
        self,
        profile_id: str,
        content_hash: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        cancellation: CancellationToken,
    ) -> ShellProfileRecord:
        cancellation.checkpoint()
        async with self._lock:
            record = self._user_record(profile_id, expected_revision)
            if record.content_hash != content_hash:
                raise ValueError("Shell profile confirmation is bound to another content hash")
            confirmed = ShellProfileRecord(
                record.profile,
                record.source,
                ShellProfileTrust.CONFIRMED,
                False,
                record.revision + 1,
                record.content_hash,
            )
            return await self._persist_record(confirmed, idempotency_key, cancellation)

    async def set_enabled(
        self,
        profile_id: str,
        enabled: bool,
        *,
        expected_revision: int,
        idempotency_key: str,
        cancellation: CancellationToken,
    ) -> ShellProfileRecord:
        if not isinstance(enabled, bool):
            raise TypeError("Shell profile enabled state must be boolean")
        cancellation.checkpoint()
        async with self._lock:
            self._require_initialized()
            record = self._records.get(profile_id)
            if record is None or record.revision != expected_revision:
                actual = 0 if record is None else record.revision
                raise ValueError(f"Shell profile expected revision {expected_revision}, actual {actual}")
            if enabled and record.trust is ShellProfileTrust.CONFIRMATION_REQUIRED:
                raise ValueError("Shell profile must be persistently trusted before it is enabled")
            updated = ShellProfileRecord(
                record.profile,
                record.source,
                record.trust,
                enabled,
                record.revision + 1,
                record.content_hash,
            )
            return await self._persist_record(updated, idempotency_key, cancellation)

    def _require_initialized(self) -> None:
        if self._snapshot is None:
            raise RuntimeError("Shell profile service is not initialized")

    def _user_record(self, profile_id: str, expected_revision: int) -> ShellProfileRecord:
        self._require_initialized()
        record = self._records.get(profile_id)
        if record is None or record.source is not ShellProfileSource.USER:
            raise ValueError("user Shell profile is unavailable")
        if record.revision != expected_revision:
            raise ValueError(f"Shell profile expected revision {expected_revision}, actual {record.revision}")
        return record

    async def _persist_record(
        self,
        record: ShellProfileRecord,
        idempotency_key: str,
        cancellation: CancellationToken,
    ) -> ShellProfileRecord:
        assert self._snapshot is not None
        if not idempotency_key or "\x00" in idempotency_key or len(idempotency_key) > 512:
            raise ValueError("Shell profile idempotency key is invalid")
        records = dict(self._records)
        records[record.profile.profile_id] = record
        snapshot = await self._state_store.save(
            self.workspace_id,
            tuple(records.values()),
            expected_revision=self._snapshot.revision,
            idempotency_key=idempotency_key,
        )
        cancellation.checkpoint()
        self._snapshot = snapshot
        self._records = {item.profile.profile_id: item for item in snapshot.records}
        return self._records[record.profile.profile_id]


def shell_profile_hash(profile: ShellCommandProfile) -> str:
    return canonical_json_sha256(_profile_value(profile))


def _entity_id(workspace_id: str) -> str:
    return f"shell-profiles-{hashlib.sha256(workspace_id.encode()).hexdigest()}"


def _records_hash(records: Sequence[ShellProfileRecord]) -> str:
    return canonical_json_sha256([_record_value(record) for record in records])


def _profile_value(profile: ShellCommandProfile) -> dict[str, Any]:
    return {
        "profileId": profile.profile_id,
        "description": profile.description,
        "executableId": profile.executable_id,
        "executableProfileFingerprint": profile.executable_profile_fingerprint,
        "fixedArguments": list(profile.fixed_arguments),
        "minimumVariableArguments": profile.minimum_variable_arguments,
        "maximumVariableArguments": profile.maximum_variable_arguments,
        "variableArgumentPattern": profile.variable_argument_pattern,
        "cwdRootId": profile.cwd_root_id,
        "environmentProfileId": profile.environment_profile_id,
        "environmentAllowlist": sorted(profile.environment_allowlist),
        "environment": {key: profile.environment[key] for key in sorted(profile.environment)},
        "timeoutMs": profile.timeout_ms,
        "inlineOutputLimitBytes": profile.inline_output_limit_bytes,
        "artifactOutputLimitBytes": profile.artifact_output_limit_bytes,
        "allowNetwork": profile.allow_network,
        "risk": profile.risk.value,
        "sideEffectClass": profile.side_effect_class.value,
        "concurrencySafe": profile.concurrency_safe,
        "idempotent": profile.idempotent,
        "retryable": profile.retryable,
        "version": profile.version,
    }


def _record_value(record: ShellProfileRecord) -> dict[str, Any]:
    return {
        "profile": _profile_value(record.profile),
        "source": record.source.value,
        "trust": record.trust.value,
        "enabled": record.enabled,
        "revision": record.revision,
        "contentHash": record.content_hash,
    }


def _snapshot_value(snapshot: ShellProfileSnapshot) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "workspaceId": snapshot.workspace_id,
        "recordRevision": snapshot.revision,
        "idempotencyKey": snapshot.idempotency_key,
        "snapshotHash": snapshot.snapshot_hash,
        "profiles": [_record_value(record) for record in snapshot.records],
    }


def _parse_snapshot(raw: object, workspace_id: str) -> ShellProfileSnapshot:
    if not isinstance(raw, Mapping) or set(raw) != {
        "schemaVersion",
        "workspaceId",
        "recordRevision",
        "idempotencyKey",
        "snapshotHash",
        "profiles",
    }:
        raise ValueError("persisted Shell profile catalog fields are invalid")
    revision = raw["recordRevision"]
    profiles = raw["profiles"]
    if (
        raw["schemaVersion"] != 1
        or raw["workspaceId"] != workspace_id
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(raw["idempotencyKey"], str)
        or not isinstance(raw["snapshotHash"], str)
        or not isinstance(profiles, list)
        or len(profiles) > _MAX_PROFILES
    ):
        raise ValueError("persisted Shell profile catalog types are invalid")
    records = tuple(_parse_record(value) for value in profiles)
    return ShellProfileSnapshot(
        workspace_id,
        records,
        revision,
        str(raw["idempotencyKey"]),
        str(raw["snapshotHash"]),
    )


def _parse_record(raw: object) -> ShellProfileRecord:
    if not isinstance(raw, Mapping) or set(raw) != {
        "profile",
        "source",
        "trust",
        "enabled",
        "revision",
        "contentHash",
    }:
        raise ValueError("persisted Shell profile record fields are invalid")
    revision = raw["revision"]
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(raw["enabled"], bool)
        or not isinstance(raw["contentHash"], str)
    ):
        raise ValueError("persisted Shell profile record types are invalid")
    try:
        source = ShellProfileSource(raw["source"])
        trust = ShellProfileTrust(raw["trust"])
    except (TypeError, ValueError) as error:
        raise ValueError("persisted Shell profile source/trust is invalid") from error
    return ShellProfileRecord(
        _parse_profile(raw["profile"]),
        source,
        trust,
        bool(raw["enabled"]),
        revision,
        str(raw["contentHash"]),
    )


def _parse_profile(raw: object) -> ShellCommandProfile:
    expected = {
        "profileId",
        "description",
        "executableId",
        "executableProfileFingerprint",
        "fixedArguments",
        "minimumVariableArguments",
        "maximumVariableArguments",
        "variableArgumentPattern",
        "cwdRootId",
        "environmentProfileId",
        "environmentAllowlist",
        "environment",
        "timeoutMs",
        "inlineOutputLimitBytes",
        "artifactOutputLimitBytes",
        "allowNetwork",
        "risk",
        "sideEffectClass",
        "concurrencySafe",
        "idempotent",
        "retryable",
        "version",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("persisted Shell profile fields are invalid")
    strings = (
        "profileId",
        "description",
        "executableId",
        "executableProfileFingerprint",
        "variableArgumentPattern",
        "cwdRootId",
        "environmentProfileId",
        "version",
    )
    integers = (
        "minimumVariableArguments",
        "maximumVariableArguments",
        "timeoutMs",
        "inlineOutputLimitBytes",
        "artifactOutputLimitBytes",
    )
    booleans = ("allowNetwork", "concurrencySafe", "idempotent", "retryable")
    fixed = raw["fixedArguments"]
    allowlist = raw["environmentAllowlist"]
    environment = raw["environment"]
    if (
        any(not isinstance(raw[key], str) for key in strings)
        or any(isinstance(raw[key], bool) or not isinstance(raw[key], int) for key in integers)
        or any(not isinstance(raw[key], bool) for key in booleans)
        or not isinstance(fixed, list)
        or any(not isinstance(item, str) for item in fixed)
        or not isinstance(allowlist, list)
        or any(not isinstance(item, str) for item in allowlist)
        or not isinstance(environment, Mapping)
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items())
    ):
        raise ValueError("persisted Shell profile types are invalid")
    try:
        risk = RiskClass(raw["risk"])
        side_effect = SideEffectClass(raw["sideEffectClass"])
    except (TypeError, ValueError) as error:
        raise ValueError("persisted Shell profile risk is invalid") from error
    return ShellCommandProfile(
        profile_id=str(raw["profileId"]),
        description=str(raw["description"]),
        executable_id=str(raw["executableId"]),
        executable_profile_fingerprint=str(raw["executableProfileFingerprint"]),
        fixed_arguments=tuple(fixed),
        risk=risk,
        side_effect_class=side_effect,
        cwd_root_id=str(raw["cwdRootId"]),
        environment_profile_id=str(raw["environmentProfileId"]),
        environment_allowlist=frozenset(allowlist),
        environment=dict(environment),
        minimum_variable_arguments=int(raw["minimumVariableArguments"]),
        maximum_variable_arguments=int(raw["maximumVariableArguments"]),
        variable_argument_pattern=str(raw["variableArgumentPattern"]),
        timeout_ms=int(raw["timeoutMs"]),
        inline_output_limit_bytes=int(raw["inlineOutputLimitBytes"]),
        artifact_output_limit_bytes=int(raw["artifactOutputLimitBytes"]),
        allow_network=bool(raw["allowNetwork"]),
        concurrency_safe=bool(raw["concurrencySafe"]),
        idempotent=bool(raw["idempotent"]),
        retryable=bool(raw["retryable"]),
        version=str(raw["version"]),
    )


__all__ = [
    "EntityShellProfileStateStore",
    "ShellProfileRecord",
    "ShellProfileService",
    "ShellProfileSnapshot",
    "ShellProfileSource",
    "ShellProfileTrust",
    "shell_profile_hash",
]
