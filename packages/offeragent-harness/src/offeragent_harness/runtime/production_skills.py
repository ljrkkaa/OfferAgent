"""Production composition for Claude-style progressively disclosed Skills."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from offeragent_harness.config import HarnessConfig
from offeragent_harness.ports.cancellation import CancellationToken
from offeragent_harness.skills import (
    SkillAuthority,
    SkillCatalog,
    SkillLayer,
    SkillLimits,
    SkillRoot,
    SkillToolExecutor,
)
from offeragent_harness.skills.catalog import SkillCatalogStatus
from offeragent_harness.skills.models import SkillDescriptor
from offeragent_harness.skills.tools import SkillAuthorityProvider, skill_tool_definitions
from offeragent_harness.tools import ToolCall, ToolDefinition

_SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_MAX_ENABLED_SKILLS = 256


class ProductionSkillBundleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedSkillPromptDescriptor:
    root_id: str
    package_path: str
    name: str
    description: str
    allowed_tools: tuple[str, ...]
    metadata_hash: str


@dataclass(frozen=True, slots=True)
class PreparedSkillBundle:
    workspace_id: str
    workspace_trusted: bool
    active_skill_names: frozenset[str]
    catalog_revision: int
    catalog_snapshot_hash: str
    catalog_status: SkillCatalogStatus
    authority: SkillAuthority
    _factory_nonce: object = field(repr=False, compare=False)
    prompt_descriptors: tuple[PreparedSkillPromptDescriptor, ...] = ()


@dataclass(frozen=True, slots=True)
class ProductionSkillBundle:
    definitions: tuple[ToolDefinition, ...]
    executor: SkillToolExecutor | None
    catalog_status: SkillCatalogStatus


@dataclass(frozen=True, slots=True)
class _PreparedAuthorityProvider(SkillAuthorityProvider):
    workspace_id: str
    authority: SkillAuthority

    async def authority_for(self, call: ToolCall) -> SkillAuthority:
        if call.workspace_id != self.workspace_id:
            raise ValueError("Skill authority cannot cross Workspace boundaries")
        return self.authority


class ProductionSkillBundleFactory:
    """Own one live metadata catalog and bind an immutable Skill view to each Run.

    User Skills are local user configuration. Project Skills become available
    when the Workspace itself is trusted. The catalog is refreshed at Run
    preparation so edits are visible in the current session without a Worker
    restart. Full Skill bodies are never retained by this factory.
    """

    def __init__(
        self,
        *,
        workspace_id: str,
        workspace_root: Path,
        runtime_root: Path,
        user_home: Path,
        limits: SkillLimits | None = None,
    ) -> None:
        if not workspace_id or "\x00" in workspace_id:
            raise ValueError("Production Skill factory requires a valid Workspace ID")
        self.workspace_id = workspace_id
        self.workspace_root = _absolute_path(workspace_root, "Workspace root")
        self.runtime_root = _absolute_path(runtime_root, "Runtime root")
        self.user_root = _absolute_path(user_home, "User home") / ".claude" / "skills"
        self.builtin_root = self.runtime_root / "skills"
        self.workspace_skill_root = self.workspace_root / ".claude" / "skills"
        self._limits = limits or SkillLimits()
        self._factory_nonce = object()
        self._catalog = self._new_catalog(scan_workspace=False)
        self._catalog_initialized = False
        self._catalog_scans_workspace = False
        self._prepare_lock = asyncio.Lock()

    @property
    def catalog(self) -> SkillCatalog:
        return self._catalog

    async def catalog_for_management(self, *, workspace_trusted: bool, cancellation: CancellationToken) -> SkillCatalog:
        async with self._prepare_lock:
            await self._refresh_catalog(workspace_trusted, cancellation)
            return self._catalog

    async def prepare(
        self,
        effective_config: HarnessConfig,
        cancellation: CancellationToken,
        *,
        authority_ceiling: SkillAuthority | None = None,
    ) -> PreparedSkillBundle:
        ceiling = authority_ceiling or SkillAuthority(frozenset(), frozenset())
        authority = _narrow_authority(ceiling, None, workspace_trusted=effective_config.policy.workspace_trusted)
        async with self._prepare_lock:
            await self._refresh_catalog(authority.workspace_trusted, cancellation)
            status = self._catalog.status()
            descriptors = (
                () if status.partial else _active_descriptors(self._catalog.snapshot.effective_descriptors, authority)
            )
            return PreparedSkillBundle(
                self.workspace_id,
                authority.workspace_trusted,
                frozenset(item.name for item in descriptors),
                self._catalog.snapshot.revision,
                self._catalog.snapshot.snapshot_hash,
                status,
                authority,
                self._factory_nonce,
                _prompt_descriptors(descriptors),
            )

    def build_prepared(self, prepared: PreparedSkillBundle) -> ProductionSkillBundle:
        if prepared._factory_nonce is not self._factory_nonce or prepared.workspace_id != self.workspace_id:
            raise ProductionSkillBundleError("prepared Skill bundle belongs to another factory/Workspace")
        status = self._catalog.status()
        if not prepared.active_skill_names:
            return ProductionSkillBundle((), None, status)
        if (
            status.partial
            or self._catalog.snapshot.revision != prepared.catalog_revision
            or self._catalog.snapshot.snapshot_hash != prepared.catalog_snapshot_hash
        ):
            raise ProductionSkillBundleError("prepared Skill catalog drifted before Run binding")
        active = _active_descriptors(self._catalog.snapshot.effective_descriptors, prepared.authority)
        if frozenset(item.name for item in active) != prepared.active_skill_names:
            raise ProductionSkillBundleError("prepared Skill authority drifted before Run binding")
        return ProductionSkillBundle(
            skill_tool_definitions(),
            SkillToolExecutor(
                workspace_id=self.workspace_id,
                catalog=self._catalog,
                authority_provider=_PreparedAuthorityProvider(self.workspace_id, prepared.authority),
            ),
            status,
        )

    def narrow_prepared(
        self,
        prepared: PreparedSkillBundle,
        declared_skill_names: Sequence[str],
        *,
        authority_ceiling: SkillAuthority,
    ) -> PreparedSkillBundle:
        if prepared._factory_nonce is not self._factory_nonce or prepared.workspace_id != self.workspace_id:
            raise ProductionSkillBundleError("parent prepared Skill bundle belongs to another factory/Workspace")
        authority = _narrow_authority(
            authority_ceiling,
            _enabled_names(declared_skill_names) & prepared.active_skill_names,
            workspace_trusted=prepared.workspace_trusted,
        )
        status = self._catalog.status()
        if (
            status.partial
            or self._catalog.snapshot.revision != prepared.catalog_revision
            or self._catalog.snapshot.snapshot_hash != prepared.catalog_snapshot_hash
        ):
            raise ProductionSkillBundleError("parent prepared Skill catalog drifted before child narrowing")
        descriptors = _active_descriptors(self._catalog.snapshot.effective_descriptors, authority)
        if any(item.name not in prepared.active_skill_names for item in descriptors):
            raise ProductionSkillBundleError("child Skill authority exceeded its parent")
        return PreparedSkillBundle(
            self.workspace_id,
            authority.workspace_trusted,
            frozenset(item.name for item in descriptors),
            prepared.catalog_revision,
            prepared.catalog_snapshot_hash,
            status,
            authority,
            self._factory_nonce,
            _prompt_descriptors(descriptors),
        )

    async def _refresh_catalog(self, workspace_trusted: bool, cancellation: CancellationToken) -> None:
        if workspace_trusted != self._catalog_scans_workspace:
            self._catalog = self._new_catalog(scan_workspace=workspace_trusted)
            self._catalog_initialized = False
            self._catalog_scans_workspace = workspace_trusted
        if not self._catalog_initialized:
            await self._catalog.initialize(cancellation)
            self._catalog_initialized = True
            return
        await self._catalog.refresh(expected_revision=self._catalog.snapshot.revision, cancellation=cancellation)

    def _new_catalog(self, *, scan_workspace: bool) -> SkillCatalog:
        return SkillCatalog(
            workspace_id=self.workspace_id,
            roots=(
                SkillRoot(
                    self.workspace_id, "runtime-builtin", SkillLayer.BUILTIN, self.builtin_root, workspace_trusted=True
                ),
                SkillRoot(self.workspace_id, "user", SkillLayer.USER, self.user_root, workspace_trusted=True),
                SkillRoot(
                    self.workspace_id,
                    "workspace",
                    SkillLayer.WORKSPACE,
                    self.workspace_skill_root,
                    workspace_root=self.workspace_root,
                    workspace_trusted=scan_workspace,
                ),
            ),
            limits=self._limits,
        )


def _enabled_names(values: Sequence[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)) or len(values) > _MAX_ENABLED_SKILLS:
        raise ValueError("enabled Skill names must be a bounded sequence")
    names = frozenset(values)
    if any(not isinstance(item, str) or _SKILL_NAME.fullmatch(item) is None for item in names):
        raise ValueError("enabled Skill names must use lowercase kebab-case")
    return names


def _narrow_authority(
    ceiling: SkillAuthority,
    names: frozenset[str] | None,
    *,
    workspace_trusted: bool,
) -> SkillAuthority:
    if names is None:
        selected = ceiling.enabled_skills
    elif ceiling.enabled_skills is None:
        selected = names
    else:
        selected = names & ceiling.enabled_skills
    return SkillAuthority(
        ceiling.available_tools,
        ceiling.policy_allowed_tools & ceiling.available_tools,
        None if selected is None else frozenset(selected),
        workspace_trusted and ceiling.workspace_trusted,
    )


def _active_descriptors(
    descriptors: tuple[SkillDescriptor, ...], authority: SkillAuthority
) -> tuple[SkillDescriptor, ...]:
    return tuple(
        item
        for item in sorted(
            descriptors, key=lambda value: (value.name, value.layer.priority, value.root_id, value.package_path)
        )
        if (authority.enabled_skills is None or item.name in authority.enabled_skills)
        and (item.layer is not SkillLayer.WORKSPACE or authority.workspace_trusted)
    )


def _prompt_descriptors(descriptors: tuple[SkillDescriptor, ...]) -> tuple[PreparedSkillPromptDescriptor, ...]:
    return tuple(
        PreparedSkillPromptDescriptor(
            item.root_id,
            item.package_path,
            item.name,
            item.description,
            tuple(sorted(item.allowed_tools)),
            item.content_hash,
        )
        for item in descriptors
    )


def _absolute_path(path: Path, label: str) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise ValueError(f"{label} must be an explicit absolute path")
    return Path(os.path.abspath(os.fspath(raw)))


__all__ = [
    "PreparedSkillBundle",
    "PreparedSkillPromptDescriptor",
    "ProductionSkillBundle",
    "ProductionSkillBundleError",
    "ProductionSkillBundleFactory",
]
