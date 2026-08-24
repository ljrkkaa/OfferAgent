"""Production Shell capability bundle bound to one prepared Run snapshot."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from offeragent_harness.config import HarnessConfig
from offeragent_harness.ports import CancellationToken, Clock, ProcessSupervisor, UnitOfWorkFactory
from offeragent_harness.shell import ShellCommandProfile, ShellToolExecutor
from offeragent_harness.shell.state import (
    EntityShellProfileStateStore,
    ShellProfileRecord,
    ShellProfileService,
    ShellProfileTrust,
)
from offeragent_harness.tools import ToolDefinition
from offeragent_harness.tools.artifacts import ToolArtifactManager

_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_MAX_CWD_ROOTS = 32


class ProductionShellBundleError(RuntimeError):
    """A prepared Shell authority snapshot cannot be safely bound."""


@dataclass(frozen=True, slots=True)
class PreparedShellProfileEvidence:
    profile_id: str
    content_hash: str
    executable_profile_fingerprint: str
    record_revision: int
    trust: ShellProfileTrust
    definition: ToolDefinition


@dataclass(frozen=True, slots=True)
class PreparedShellBundle:
    workspace_id: str
    shell_enabled: bool
    workspace_trusted: bool
    read_only: bool
    allowed_cwd_root_ids: frozenset[str]
    catalog_revision: int
    catalog_snapshot_hash: str
    profiles: tuple[PreparedShellProfileEvidence, ...]
    _factory_nonce: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ProductionShellBundle:
    definitions: tuple[ToolDefinition, ...]
    executor: ShellToolExecutor | None
    catalog_revision: int
    catalog_snapshot_hash: str


class ProductionShellBundleFactory:
    """Keep profile management durable and freeze its authority per Run."""

    def __init__(
        self,
        *,
        workspace_id: str,
        builtin_profiles: Sequence[ShellCommandProfile],
        unit_of_work: UnitOfWorkFactory,
        processes: ProcessSupervisor,
        clock: Clock,
    ) -> None:
        if not workspace_id or "\x00" in workspace_id:
            raise ValueError("Production Shell factory requires a Workspace ID")
        self.workspace_id = workspace_id
        self._processes = processes
        self._clock = clock
        self._profiles = ShellProfileService(
            workspace_id=workspace_id,
            builtin_profiles=builtin_profiles,
            state_store=EntityShellProfileStateStore(unit_of_work),
        )
        self._factory_nonce = object()

    @property
    def profiles(self) -> ShellProfileService:
        return self._profiles

    def definitions_for(self, prepared: PreparedShellBundle) -> tuple[ToolDefinition, ...]:
        """Return the immutable definitions captured by ``prepare`` without an artifact ledger."""

        self._validate_prepared(prepared)
        return tuple(item.definition for item in prepared.profiles)

    def narrow_prepared(
        self,
        parent: PreparedShellBundle,
        definitions: Sequence[ToolDefinition],
        *,
        allowed_cwd_root_ids: Sequence[str],
    ) -> PreparedShellBundle:
        """Derive a child snapshot from the parent's frozen profiles without rescanning."""

        self._validate_prepared(parent)
        allowed_roots = _allowed_roots(allowed_cwd_root_ids) & parent.allowed_cwd_root_ids
        requested = {(item.name, item.version, item.fingerprint) for item in definitions}
        available = {
            (item.definition.name, item.definition.version, item.definition.fingerprint) for item in parent.profiles
        }
        if not requested <= available:
            raise ProductionShellBundleError("child Shell definitions are outside the parent snapshot")
        profiles = tuple(
            item
            for item in parent.profiles
            if (
                item.definition.name,
                item.definition.version,
                item.definition.fingerprint,
            )
            in requested
            and _profile_root(self._profiles.snapshot.records, item.profile_id) in allowed_roots
        )
        return PreparedShellBundle(
            parent.workspace_id,
            parent.shell_enabled,
            parent.workspace_trusted,
            parent.read_only,
            allowed_roots,
            parent.catalog_revision,
            parent.catalog_snapshot_hash,
            profiles,
            self._factory_nonce,
        )

    async def prepare(
        self,
        effective_config: HarnessConfig,
        allowed_cwd_root_ids: Sequence[str],
        cancellation: CancellationToken,
    ) -> PreparedShellBundle:
        cancellation.checkpoint()
        allowed_roots = _allowed_roots(allowed_cwd_root_ids)
        shell_enabled = effective_config.execution.shell_enabled
        trusted = effective_config.policy.workspace_trusted
        read_only = effective_config.policy.read_only
        if not shell_enabled or not trusted or read_only:
            return PreparedShellBundle(
                self.workspace_id,
                shell_enabled,
                trusted,
                read_only,
                allowed_roots,
                0,
                "sha256:" + "0" * 64,
                (),
                self._factory_nonce,
            )
        snapshot = await self._profiles.initialize(cancellation)
        cancellation.checkpoint()
        evidence = _active_evidence(snapshot.records, allowed_roots)
        return PreparedShellBundle(
            self.workspace_id,
            True,
            True,
            False,
            allowed_roots,
            snapshot.revision,
            snapshot.snapshot_hash,
            evidence,
            self._factory_nonce,
        )

    def build_prepared(
        self,
        prepared: PreparedShellBundle,
        artifacts: ToolArtifactManager,
    ) -> ProductionShellBundle:
        self._validate_prepared(prepared)
        if not prepared.shell_enabled or not prepared.workspace_trusted or prepared.read_only or not prepared.profiles:
            return ProductionShellBundle(
                (),
                None,
                prepared.catalog_revision,
                prepared.catalog_snapshot_hash,
            )
        if not self._profiles.initialized:
            raise ProductionShellBundleError("prepared Shell profile catalog is unavailable")
        snapshot = self._profiles.snapshot
        evidence = _active_evidence(snapshot.records, prepared.allowed_cwd_root_ids)
        if (
            snapshot.revision != prepared.catalog_revision
            or snapshot.snapshot_hash != prepared.catalog_snapshot_hash
            or evidence != prepared.profiles
        ):
            raise ProductionShellBundleError("prepared Shell profile catalog drifted before Run binding")
        active_by_id = {
            record.profile.profile_id: record.profile
            for record in snapshot.records
            if record.enabled
            and record.trust in {ShellProfileTrust.SIGNED, ShellProfileTrust.CONFIRMED}
            and record.profile.cwd_root_id in prepared.allowed_cwd_root_ids
        }
        profiles = tuple(active_by_id[item.profile_id] for item in evidence)
        executor = ShellToolExecutor(
            profiles,
            processes=self._processes,
            clock=self._clock,
            artifact_budget=artifacts,
        )
        return ProductionShellBundle(
            executor.definitions,
            executor,
            snapshot.revision,
            snapshot.snapshot_hash,
        )

    def _validate_prepared(self, prepared: PreparedShellBundle) -> None:
        if prepared._factory_nonce is not self._factory_nonce or prepared.workspace_id != self.workspace_id:
            raise ProductionShellBundleError("prepared Shell bundle belongs to another factory/Workspace")


def _allowed_roots(values: Sequence[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)) or len(values) > _MAX_CWD_ROOTS:
        raise ValueError("Shell cwd roots must be a bounded capability sequence")
    roots = frozenset(values)
    if any(not isinstance(value, str) or _CAPABILITY_ID.fullmatch(value) is None for value in roots):
        raise ValueError("Shell cwd roots must be canonical capability IDs")
    return roots


def _active_evidence(
    records: Sequence[ShellProfileRecord],
    allowed_roots: frozenset[str],
) -> tuple[PreparedShellProfileEvidence, ...]:
    evidence = (
        PreparedShellProfileEvidence(
            record.profile.profile_id,
            record.content_hash,
            record.profile.executable_profile_fingerprint,
            record.revision,
            record.trust,
            record.profile.definition,
        )
        for record in records
        if record.enabled
        and record.trust in {ShellProfileTrust.SIGNED, ShellProfileTrust.CONFIRMED}
        and record.profile.cwd_root_id in allowed_roots
    )
    return tuple(sorted(evidence, key=lambda item: item.profile_id))


def _profile_root(records: Sequence[ShellProfileRecord], profile_id: str) -> str:
    for record in records:
        if record.profile.profile_id == profile_id:
            return record.profile.cwd_root_id
    raise ProductionShellBundleError("prepared Shell profile is no longer in the initialized catalog")


__all__ = [
    "PreparedShellBundle",
    "PreparedShellProfileEvidence",
    "ProductionShellBundle",
    "ProductionShellBundleError",
    "ProductionShellBundleFactory",
]
