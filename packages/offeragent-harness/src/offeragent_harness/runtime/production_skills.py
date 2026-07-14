"""Production composition for lazy, file-backed Claude-style Skills."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from offeragent_harness.config import HarnessConfig
from offeragent_harness.ports.cancellation import CancellationToken
from offeragent_harness.ports.skills import (
    SkillTrustVerificationRequest,
    SkillTrustVerificationResult,
    SkillTrustVerifier,
)
from offeragent_harness.ports.system import Clock, IdGenerator
from offeragent_harness.ports.unit_of_work import UnitOfWorkFactory
from offeragent_harness.skills import (
    EntitySkillStateStore,
    SkillAuthority,
    SkillCatalog,
    SkillLayer,
    SkillLimits,
    SkillRoot,
    SkillToolExecutor,
)
from offeragent_harness.skills.catalog import SkillCatalogStatus
from offeragent_harness.skills.frontmatter import parse_skill_document
from offeragent_harness.skills.models import SkillDescriptor
from offeragent_harness.skills.tools import SkillAuthorityProvider, skill_tool_definitions
from offeragent_harness.tools import ToolCall, ToolDefinition

_SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_MAX_ENABLED_SKILLS = 256


class ProductionSkillBundleError(RuntimeError):
    pass


class _ReleaseManifestTrust(Protocol):
    @property
    def version_directory(self) -> Path: ...

    @property
    def manifest_hash(self) -> str: ...

    @property
    def manifest(self) -> object: ...


class _ReleaseSkillRecord(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def sha256(self) -> str: ...


class ReleaseManifestSkillTrustVerifier:
    """Builtin Skills are trusted only when their exact file is signed in Runtime."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        manifest_trust: _ReleaseManifestTrust | None = None,
        limits: SkillLimits | None = None,
    ) -> None:
        self._runtime_root = _absolute_path(runtime_root, "Runtime root")
        self._manifest_trust = manifest_trust
        self._limits = limits or SkillLimits()

    async def verify(self, request: SkillTrustVerificationRequest) -> SkillTrustVerificationResult:
        try:
            token = await asyncio.to_thread(self._verify_sync, request)
        except Exception:
            return SkillTrustVerificationResult(
                False,
                "release-manifest-v1",
                None,
                "Builtin Skill is absent from or differs from the activated signed Runtime manifest",
            )
        return SkillTrustVerificationResult(True, "release-manifest-v1", token, None)

    def _verify_sync(self, request: SkillTrustVerificationRequest) -> str:
        if request.root_id != "runtime-builtin" or request.layer != SkillLayer.BUILTIN.value:
            raise ValueError("release manifest verifier only authorizes builtin Skills")
        trust = self._manifest_trust or _load_installed_release_trust(self._runtime_root)
        runtime = self._runtime_root.resolve(strict=True)
        if Path(trust.version_directory).resolve(strict=True) != runtime:
            raise ValueError("release manifest belongs to another Runtime root")
        manifest = trust.manifest
        files = getattr(manifest, "files", None)
        manifest_hash = getattr(trust, "manifest_hash", None)
        signing_key_id = getattr(manifest, "signing_key_id", None)
        if (
            not isinstance(files, tuple)
            or not isinstance(signing_key_id, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(manifest_hash)) is None
        ):
            raise ValueError("release manifest trust view is invalid")
        expected_path = f"skills/{request.package_path}/SKILL.md"
        matches = [
            record
            for record in files
            if getattr(record, "kind", None) == "skill" and getattr(record, "path", None) == expected_path
        ]
        if len(matches) != 1:
            raise ValueError("Builtin Skill manifest record is absent or ambiguous")
        record = matches[0]
        content = _read_manifest_skill(runtime, expected_path, record, maximum=self._limits.max_skill_bytes)
        document = parse_skill_document(content, self._limits)
        if document.metadata.name != request.name or document.metadata.content_hash != request.metadata_hash:
            raise ValueError("Builtin Skill metadata differs from the signed manifest file")
        typed = cast(_ReleaseSkillRecord, record)
        material = "\0".join(
            (str(manifest_hash), signing_key_id, typed.path, typed.sha256, request.metadata_hash)
        ).encode()
        return f"sha256:{hashlib.sha256(material).hexdigest()}"


@dataclass(frozen=True, slots=True)
class PreparedSkillTrustEvidence:
    root_id: str
    package_path: str
    name: str
    layer: SkillLayer
    metadata_hash: str
    trust_state: str
    trust_token_hash: str


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
    skills_enabled: bool
    workspace_trusted: bool
    enabled_skill_names: frozenset[str]
    active_skill_names: frozenset[str]
    catalog_revision: int
    catalog_snapshot_hash: str
    catalog_status: SkillCatalogStatus
    authority: SkillAuthority
    trust_evidence: tuple[PreparedSkillTrustEvidence, ...]
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
    """One Worker-owned catalog; each Run receives an immutable authority slice."""

    def __init__(
        self,
        *,
        workspace_id: str,
        workspace_root: Path,
        runtime_root: Path,
        user_home: Path | None,
        unit_of_work: UnitOfWorkFactory,
        trust_verifier: SkillTrustVerifier,
        clock: Clock,
        ids: IdGenerator,
        limits: SkillLimits | None = None,
    ) -> None:
        if not workspace_id or "\x00" in workspace_id:
            raise ValueError("Production Skill factory requires a valid Workspace ID")
        home = user_home or Path.home()
        self.workspace_id = workspace_id
        self.workspace_root = _absolute_path(workspace_root, "Workspace root")
        self.runtime_root = _absolute_path(runtime_root, "Runtime root")
        self.user_root = _absolute_path(home, "User home") / ".claude" / "skills"
        self.builtin_root = self.runtime_root / "skills"
        self.workspace_skill_root = self.workspace_root / ".claude" / "skills"
        self._state_store = EntitySkillStateStore(unit_of_work, clock, ids)
        self._trust_verifier = trust_verifier
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
            await self._ensure_catalog(workspace_trusted, cancellation)
            return self._catalog

    async def prepare(
        self,
        effective_config: HarnessConfig,
        enabled_skill_names: Sequence[str],
        cancellation: CancellationToken,
        *,
        authority_ceiling: SkillAuthority | None = None,
    ) -> PreparedSkillBundle:
        names = _enabled_names(enabled_skill_names)
        ceiling = authority_ceiling or SkillAuthority(frozenset(), frozenset())
        authority = _narrow_authority(ceiling, names, workspace_trusted=effective_config.policy.workspace_trusted)
        if not effective_config.extensibility.skills_enabled:
            status = self._catalog.status()
            return PreparedSkillBundle(
                self.workspace_id,
                False,
                authority.workspace_trusted,
                frozenset(),
                frozenset(),
                self._catalog.snapshot.revision,
                self._catalog.snapshot.snapshot_hash,
                status,
                _narrow_authority(ceiling, frozenset(), workspace_trusted=authority.workspace_trusted),
                (),
                self._factory_nonce,
            )
        async with self._prepare_lock:
            await self._ensure_catalog(authority.workspace_trusted, cancellation)
            status = self._catalog.status()
            evidence = (
                () if status.partial else _active_evidence(self._catalog.snapshot.effective_descriptors, authority)
            )
            return PreparedSkillBundle(
                self.workspace_id,
                True,
                authority.workspace_trusted,
                authority.enabled_skills or frozenset(),
                frozenset(item.name for item in evidence),
                self._catalog.snapshot.revision,
                self._catalog.snapshot.snapshot_hash,
                status,
                authority,
                evidence,
                self._factory_nonce,
                _prompt_descriptors(self._catalog.snapshot.effective_descriptors, evidence),
            )

    def build_prepared(self, prepared: PreparedSkillBundle) -> ProductionSkillBundle:
        if prepared._factory_nonce is not self._factory_nonce or prepared.workspace_id != self.workspace_id:
            raise ProductionSkillBundleError("prepared Skill bundle belongs to another factory/Workspace")
        status = self._catalog.status()
        if not prepared.skills_enabled or not prepared.active_skill_names:
            return ProductionSkillBundle((), None, status)
        if (
            status.partial
            or self._catalog.snapshot.revision != prepared.catalog_revision
            or self._catalog.snapshot.snapshot_hash != prepared.catalog_snapshot_hash
        ):
            raise ProductionSkillBundleError("prepared Skill catalog drifted before Run binding")
        if (
            _active_evidence(self._catalog.snapshot.effective_descriptors, prepared.authority)
            != prepared.trust_evidence
        ):
            raise ProductionSkillBundleError("prepared Skill trust evidence drifted before Run binding")
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
        enabled_skill_names: Sequence[str],
        *,
        authority_ceiling: SkillAuthority,
    ) -> PreparedSkillBundle:
        if prepared._factory_nonce is not self._factory_nonce or prepared.workspace_id != self.workspace_id:
            raise ProductionSkillBundleError("parent prepared Skill bundle belongs to another factory/Workspace")
        authority = _narrow_authority(
            authority_ceiling,
            _enabled_names(enabled_skill_names) & prepared.enabled_skill_names,
            workspace_trusted=prepared.workspace_trusted,
        )
        if not prepared.skills_enabled:
            return PreparedSkillBundle(
                self.workspace_id,
                False,
                authority.workspace_trusted,
                frozenset(),
                frozenset(),
                prepared.catalog_revision,
                prepared.catalog_snapshot_hash,
                prepared.catalog_status,
                _narrow_authority(authority_ceiling, frozenset(), workspace_trusted=authority.workspace_trusted),
                (),
                self._factory_nonce,
            )
        status = self._catalog.status()
        if (
            status.partial
            or self._catalog.snapshot.revision != prepared.catalog_revision
            or self._catalog.snapshot.snapshot_hash != prepared.catalog_snapshot_hash
        ):
            raise ProductionSkillBundleError("parent prepared Skill catalog drifted before child narrowing")
        evidence = _active_evidence(self._catalog.snapshot.effective_descriptors, authority)
        parent = {(item.root_id, item.package_path, item.metadata_hash) for item in prepared.trust_evidence}
        if any((item.root_id, item.package_path, item.metadata_hash) not in parent for item in evidence):
            raise ProductionSkillBundleError("child Skill authority exceeded its parent's trusted Skill set")
        return PreparedSkillBundle(
            self.workspace_id,
            True,
            authority.workspace_trusted,
            authority.enabled_skills or frozenset(),
            frozenset(item.name for item in evidence),
            prepared.catalog_revision,
            prepared.catalog_snapshot_hash,
            status,
            authority,
            evidence,
            self._factory_nonce,
            _prompt_descriptors(self._catalog.snapshot.effective_descriptors, evidence),
        )

    async def _ensure_catalog(self, workspace_trusted: bool, cancellation: CancellationToken) -> None:
        if workspace_trusted and not self._catalog_scans_workspace:
            self._catalog = self._new_catalog(scan_workspace=True)
            self._catalog_initialized = False
            self._catalog_scans_workspace = True
        if not self._catalog_initialized:
            await self._catalog.initialize(cancellation)
            self._catalog_initialized = True
        else:
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
            trust_verifier=self._trust_verifier,
            state_store=self._state_store,
            limits=self._limits,
        )


def _enabled_names(values: Sequence[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)) or len(values) > _MAX_ENABLED_SKILLS:
        raise ValueError("enabled Skill names must be a bounded sequence")
    names = frozenset(values)
    if any(not isinstance(item, str) or _SKILL_NAME.fullmatch(item) is None for item in names):
        raise ValueError("enabled Skill names must use lowercase kebab-case")
    return names


def _narrow_authority(ceiling: SkillAuthority, names: frozenset[str], *, workspace_trusted: bool) -> SkillAuthority:
    selected = names if ceiling.enabled_skills is None else names & ceiling.enabled_skills
    return SkillAuthority(
        ceiling.available_tools,
        ceiling.policy_allowed_tools & ceiling.available_tools,
        frozenset(selected),
        workspace_trusted and ceiling.workspace_trusted,
    )


def _active_evidence(
    descriptors: tuple[SkillDescriptor, ...], authority: SkillAuthority
) -> tuple[PreparedSkillTrustEvidence, ...]:
    enabled = authority.enabled_skills or frozenset()
    return tuple(
        PreparedSkillTrustEvidence(
            item.root_id,
            item.package_path,
            item.name,
            item.layer,
            item.content_hash,
            item.trust_state.value,
            f"sha256:{hashlib.sha256((item.trust_token or '').encode()).hexdigest()}",
        )
        for item in sorted(
            descriptors, key=lambda value: (value.name, value.layer.priority, value.root_id, value.package_path)
        )
        if item.name in enabled
        and item.trust_token is not None
        and (item.layer is not SkillLayer.WORKSPACE or authority.workspace_trusted)
    )


def _prompt_descriptors(
    descriptors: tuple[SkillDescriptor, ...], evidence: tuple[PreparedSkillTrustEvidence, ...]
) -> tuple[PreparedSkillPromptDescriptor, ...]:
    active = {(item.root_id, item.package_path, item.metadata_hash) for item in evidence}
    return tuple(
        PreparedSkillPromptDescriptor(
            item.root_id,
            item.package_path,
            item.name,
            item.description,
            tuple(sorted(item.allowed_tools)),
            item.content_hash,
        )
        for item in sorted(
            descriptors, key=lambda value: (value.name, value.layer.priority, value.root_id, value.package_path)
        )
        if (item.root_id, item.package_path, item.content_hash) in active
    )


def _absolute_path(path: Path, label: str) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise ValueError(f"{label} must be an explicit absolute path")
    return Path(os.path.abspath(os.fspath(raw)))


def _load_installed_release_trust(runtime_root: Path) -> _ReleaseManifestTrust:
    from offeragent_harness.runtime.release_manifest import ReleaseKeyring
    from offeragent_harness.runtime.release_trust import InstalledReleaseManifestTrust, load_embedded_release_keys

    return InstalledReleaseManifestTrust(runtime_root, keyring=ReleaseKeyring(load_embedded_release_keys()))


def _read_manifest_skill(runtime_root: Path, relative: str, record: object, *, maximum: int) -> bytes:
    length = getattr(record, "byte_length", None)
    expected_hash = getattr(record, "sha256", None)
    if type(length) is not int or not 1 <= length <= maximum or not isinstance(expected_hash, str):
        raise ValueError("release Skill record is invalid")
    path = runtime_root.joinpath(*relative.split("/"))
    _verify_manifest_skill_path(runtime_root, path, expected_size=length)
    with path.open("rb", buffering=0) as stream:
        content = stream.read(length + 1)
    _verify_manifest_skill_path(runtime_root, path, expected_size=length)
    if len(content) != length or f"sha256:{hashlib.sha256(content).hexdigest()}" != expected_hash:
        raise ValueError("release Skill differs from its signed record")
    return content


def _verify_manifest_skill_path(runtime_root: Path, path: Path, *, expected_size: int) -> None:
    root = runtime_root / "skills"
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError("release Skill escapes the builtin root") from error
    for current in (
        runtime_root,
        root,
        *(root / Path(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)),
    ):
        info = os.stat(current, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) & 0x0400:
            raise ValueError("release Skill path contains a reparse point")
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
        raise ValueError("release Skill file identity is unsafe")


__all__ = [
    "PreparedSkillBundle",
    "PreparedSkillPromptDescriptor",
    "PreparedSkillTrustEvidence",
    "ProductionSkillBundle",
    "ProductionSkillBundleError",
    "ProductionSkillBundleFactory",
    "ReleaseManifestSkillTrustVerifier",
]
