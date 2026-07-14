"""Single-authority catalog for Claude-style, lazy local Skills."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from offeragent_harness.ports.cancellation import CancellationToken
from offeragent_harness.ports.skills import (
    SkillStateStore,
    SkillTrustDecision,
    SkillTrustVerificationRequest,
    SkillTrustVerifier,
)
from offeragent_harness.ports.storage import EntityRevisionConflict
from offeragent_harness.tools.canonical import canonical_json_sha256

from .filesystem import SecureSkillRoot, SkillHeaderRead
from .frontmatter import SkillMetadata, parse_skill_document, parse_skill_header
from .models import (
    LoadedSkill,
    SkillAuthority,
    SkillCatalogSnapshot,
    SkillDescriptor,
    SkillDiagnostic,
    SkillDiagnosticSeverity,
    SkillError,
    SkillErrorCode,
    SkillLayer,
    SkillLimits,
    SkillReloadResult,
    SkillRoot,
    SkillSelection,
    SkillSummary,
    SkillTrustState,
    UntrustedSkillInstruction,
)


@dataclass(frozen=True, slots=True)
class SkillCatalogStatus:
    revision: int
    snapshot_hash: str
    discovered_count: int
    enabled_count: int
    partial: bool
    diagnostics: tuple[SkillDiagnostic, ...]


class SkillCatalog:
    """The only owner of discovered Skill metadata for one Workspace.

    The catalog deliberately retains no Skill bodies.  A body is opened only by
    ``load`` after selection and trust checks; file identity is revalidated on
    every read.
    """

    def __init__(
        self,
        *,
        workspace_id: str,
        roots: tuple[SkillRoot, ...],
        trust_verifier: SkillTrustVerifier,
        state_store: SkillStateStore,
        limits: SkillLimits | None = None,
    ) -> None:
        if not workspace_id or "\x00" in workspace_id:
            raise ValueError("SkillCatalog workspace_id is invalid")
        self.limits = limits or SkillLimits()
        if not roots or len(roots) > self.limits.max_roots:
            raise ValueError("SkillCatalog requires explicit bounded roots")
        if len({root.root_id for root in roots}) != len(roots):
            raise ValueError("SkillCatalog root IDs must be unique")
        if any(root.workspace_id != workspace_id for root in roots):
            raise ValueError("SkillCatalog roots cannot cross Workspace boundaries")
        self.workspace_id = workspace_id
        self.roots = roots
        self._trust_verifier = trust_verifier
        self._state_store = state_store
        self._snapshot = SkillCatalogSnapshot(workspace_id, 0, _snapshot_hash(workspace_id, 0, (), ()), (), ())
        self._secure_roots: MappingProxyType[str, SecureSkillRoot] = MappingProxyType({})
        self._last_diagnostics: tuple[SkillDiagnostic, ...] = ()
        self._partial = False
        self._initialized = False
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> SkillCatalogSnapshot:
        return self._snapshot

    def status(self) -> SkillCatalogStatus:
        return SkillCatalogStatus(
            self._snapshot.revision,
            self._snapshot.snapshot_hash,
            len(self._snapshot.descriptors),
            len(self._snapshot.effective_descriptors),
            self._partial,
            self._last_diagnostics,
        )

    async def initialize(self, cancellation: CancellationToken) -> SkillReloadResult:
        return await self.rescan(expected_revision=self._snapshot.revision, cancellation=cancellation)

    async def refresh(self, *, expected_revision: int, cancellation: CancellationToken) -> SkillReloadResult:
        return await self.rescan(expected_revision=expected_revision, cancellation=cancellation)

    async def rescan(self, *, expected_revision: int, cancellation: CancellationToken) -> SkillReloadResult:
        async with self._lock:
            cancellation.checkpoint()
            if expected_revision != self._snapshot.revision:
                raise SkillError(SkillErrorCode.CAS_CONFLICT, "Skill catalog revision is stale")
            diagnostics: list[SkillDiagnostic] = []
            discovered: list[SkillDescriptor] = []
            secure_roots: dict[str, SecureSkillRoot] = {}
            old_by_origin = {item.origin_key: item for item in self._snapshot.descriptors}
            for root in self.roots:
                if root.layer is SkillLayer.WORKSPACE and not root.workspace_trusted:
                    diagnostics.append(
                        SkillDiagnostic(
                            SkillDiagnosticSeverity.WARNING,
                            SkillErrorCode.WORKSPACE_UNTRUSTED,
                            "Workspace Skills are unavailable until the Workspace is trusted",
                            root.root_id,
                        )
                    )
                    continue
                try:
                    secure = SecureSkillRoot(root, self.limits)
                    secure_roots[root.root_id] = secure
                    for item in await secure.discover(cancellation):
                        header = await secure.read_header(item, cancellation)
                        metadata, _ = parse_skill_header(header.content, self.limits)
                        descriptor = await self._descriptor(root, secure, item.package_path, header, metadata)
                        previous = old_by_origin.get(descriptor.origin_key)
                        if (
                            previous is not None
                            and previous.file_fact != descriptor.file_fact
                            and descriptor.layer in {SkillLayer.USER, SkillLayer.WORKSPACE}
                        ):
                            await self._put_trust(
                                SkillTrustDecision(
                                    self.workspace_id,
                                    descriptor.root_id,
                                    descriptor.package_path,
                                    descriptor.name,
                                    descriptor.content_hash,
                                    False,
                                ),
                                operation=f"file-change:{expected_revision}:{descriptor.root_id}:{descriptor.package_path}",
                            )
                            descriptor = replace(
                                descriptor,
                                trust_state=SkillTrustState.CONFIRMATION_REQUIRED,
                                trust_token=None,
                            )
                            diagnostics.append(
                                SkillDiagnostic(
                                    SkillDiagnosticSeverity.WARNING,
                                    SkillErrorCode.TRUST_CONFIRMATION_REQUIRED,
                                    "Skill file changed; trust must be confirmed again",
                                    descriptor.root_id,
                                    descriptor.package_path,
                                )
                            )
                        if descriptor.trust_state is SkillTrustState.CONFIRMATION_REQUIRED:
                            diagnostics.append(
                                SkillDiagnostic(
                                    SkillDiagnosticSeverity.WARNING,
                                    SkillErrorCode.TRUST_CONFIRMATION_REQUIRED,
                                    f"Skill {descriptor.name!r} requires explicit trust confirmation",
                                    descriptor.root_id,
                                    descriptor.package_path,
                                )
                            )
                        discovered.append(descriptor)
                except SkillError as error:
                    if error.code is SkillErrorCode.INVALID_ROOT and _optional_root_is_absent(root.path):
                        continue
                    diagnostics.append(
                        SkillDiagnostic(SkillDiagnosticSeverity.ERROR, error.code, str(error), root.root_id, error.path)
                    )
            try:
                effective = self._effective(tuple(discovered), diagnostics)
            except SkillError as error:
                diagnostics.append(
                    SkillDiagnostic(SkillDiagnosticSeverity.ERROR, error.code, str(error), path=error.path)
                )
                effective = ()
            if any(item.severity is SkillDiagnosticSeverity.ERROR for item in diagnostics):
                self._last_diagnostics = tuple(diagnostics)
                self._partial = True
                return SkillReloadResult(False, True, self._snapshot, self._last_diagnostics)
            ordered = tuple(
                sorted(discovered, key=lambda item: (item.name, item.layer.priority, item.root_id, item.package_path))
            )
            stable_diagnostics = tuple(diagnostics)
            if self._initialized and (
                ordered == self._snapshot.descriptors
                and effective == self._snapshot.effective_descriptors
                and stable_diagnostics == self._snapshot.diagnostics
            ):
                self._secure_roots = MappingProxyType(secure_roots)
                self._last_diagnostics = stable_diagnostics
                self._partial = False
                return SkillReloadResult(False, False, self._snapshot, stable_diagnostics)
            revision = self._snapshot.revision + 1
            snapshot = SkillCatalogSnapshot(
                self.workspace_id,
                revision,
                _snapshot_hash(self.workspace_id, revision, ordered, effective),
                ordered,
                effective,
                stable_diagnostics,
            )
            self._snapshot = snapshot
            self._secure_roots = MappingProxyType(secure_roots)
            self._last_diagnostics = stable_diagnostics
            self._partial = False
            self._initialized = True
            return SkillReloadResult(True, False, snapshot, stable_diagnostics)

    async def _descriptor(
        self,
        root: SkillRoot,
        secure: SecureSkillRoot,
        package_path: str,
        header: SkillHeaderRead,
        metadata: SkillMetadata,
    ) -> SkillDescriptor:
        token: str | None
        if root.layer is SkillLayer.BUILTIN:
            result = await self._trust_verifier.verify(
                SkillTrustVerificationRequest(
                    self.workspace_id,
                    root.root_id,
                    root.layer.value,
                    package_path,
                    metadata.name,
                    metadata.content_hash,
                )
            )
            if not result.verified or result.trust_token is None:
                raise SkillError(
                    SkillErrorCode.TRUST_INVALID, result.reason or "Builtin Skill release verification failed"
                )
            state, token = SkillTrustState.VERIFIED, result.trust_token
        else:
            record = await self._state_store.get_trust(self.workspace_id, root.root_id, package_path)
            decision = None if record is None else record.decision
            confirmed = (
                decision is not None
                and decision.confirmed
                and decision.name == metadata.name
                and decision.metadata_hash == metadata.content_hash
            )
            state = SkillTrustState.CONFIRMED if confirmed else SkillTrustState.CONFIRMATION_REQUIRED
            token = _confirmation_token(decision) if confirmed and decision is not None else None
        return SkillDescriptor(
            self.workspace_id,
            root.root_id,
            root.layer,
            secure.path,
            package_path,
            secure.path / package_path / "SKILL.md",
            metadata.name,
            metadata.description,
            metadata.allowed_tools,
            metadata.content_hash,
            header.fact,
            state,
            token,
            header.bytes_read,
        )

    def _effective(
        self,
        descriptors: tuple[SkillDescriptor, ...],
        diagnostics: list[SkillDiagnostic],
    ) -> tuple[SkillDescriptor, ...]:
        origins: set[tuple[str, str]] = set()
        for item in descriptors:
            if item.origin_key in origins:
                raise SkillError(SkillErrorCode.CONFLICT, f"duplicate Skill package: {item.package_path}")
            origins.add(item.origin_key)
        by_name: dict[str, list[SkillDescriptor]] = {}
        for item in descriptors:
            if item.trust_state.enabled:
                by_name.setdefault(item.name, []).append(item)
        effective: list[SkillDescriptor] = []
        for name, candidates in by_name.items():
            highest = max(item.layer.priority for item in candidates)
            selected = [item for item in candidates if item.layer.priority == highest]
            if len(selected) != 1:
                raise SkillError(SkillErrorCode.CONFLICT, f"same-layer Skill name conflict: {name}")
            effective.append(selected[0])
            if any(
                item.name == name and not item.trust_state.enabled and item.layer.priority > highest
                for item in descriptors
            ):
                diagnostics.append(
                    SkillDiagnostic(
                        SkillDiagnosticSeverity.WARNING,
                        SkillErrorCode.TRUST_CONFIRMATION_REQUIRED,
                        f"untrusted higher-priority Skill {name!r} did not shadow a trusted Skill",
                    )
                )
        return tuple(sorted(effective, key=lambda item: (item.name, item.root_id, item.package_path)))

    def list(self, *, include_shadowed: bool = True) -> tuple[SkillSummary, ...]:
        source = self._snapshot.descriptors if include_shadowed else self._snapshot.effective_descriptors
        return tuple(
            SkillSummary(
                item.root_id,
                item.package_path,
                item.name,
                item.description,
                item.layer,
                item.content_hash,
                item.trust_state.enabled,
                item.trust_state,
                item.allowed_tools,
            )
            for item in source
        )

    def select(self, *, name: str, authority: SkillAuthority) -> SkillSelection:
        if authority.enabled_skills is not None and name not in authority.enabled_skills:
            raise SkillError(SkillErrorCode.NOT_FOUND, f"Skill {name!r} is not enabled for this Run")
        descriptor = next((item for item in self._snapshot.effective_descriptors if item.name == name), None)
        if descriptor is None:
            if any(item.name == name for item in self._snapshot.descriptors):
                raise SkillError(SkillErrorCode.TRUST_CONFIRMATION_REQUIRED, f"Skill {name!r} is not trusted")
            raise SkillError(SkillErrorCode.NOT_FOUND, f"Skill {name!r} is not installed")
        if descriptor.layer is SkillLayer.WORKSPACE and not authority.workspace_trusted:
            raise SkillError(SkillErrorCode.WORKSPACE_UNTRUSTED, "Workspace Skills require Workspace trust")
        allowed = descriptor.allowed_tools & authority.available_tools & authority.policy_allowed_tools
        return SkillSelection(descriptor, frozenset(allowed), frozenset(descriptor.allowed_tools - allowed))

    async def load(self, selection: SkillSelection, cancellation: CancellationToken) -> LoadedSkill:
        descriptor = selection.descriptor
        secure = self._secure_roots.get(descriptor.root_id)
        if secure is None:
            raise SkillError(SkillErrorCode.INVALID_ROOT, "Skill root is not in the active catalog")
        try:
            read = await secure.read_skill(f"{descriptor.package_path}/SKILL.md", descriptor.file_fact, cancellation)
            if len(read.content) > self.limits.max_total_load_bytes:
                raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, "Skill exceeds Run load limit")
            document = parse_skill_document(read.content, self.limits)
            metadata = document.metadata
            if (
                metadata.name != descriptor.name
                or metadata.description != descriptor.description
                or metadata.allowed_tools != descriptor.allowed_tools
                or metadata.content_hash != descriptor.content_hash
            ):
                raise SkillError(SkillErrorCode.HASH_DRIFT, "SKILL.md metadata changed after discovery")
        except SkillError as error:
            if descriptor.layer in {SkillLayer.USER, SkillLayer.WORKSPACE} and error.code in {
                SkillErrorCode.HASH_DRIFT,
                SkillErrorCode.CHANGED_DURING_READ,
            }:
                await self._invalidate_trust(descriptor)
            raise
        return LoadedSkill(
            selection,
            UntrustedSkillInstruction(
                document.body,
                f"skill:{self.workspace_id}:{descriptor.root_id}:{descriptor.package_path}",
                descriptor.content_hash,
            ),
            len(read.content),
        )

    async def confirm_trust(
        self,
        *,
        root_id: str,
        package_path: str,
        expected_metadata_hash: str,
        expected_revision: int,
        confirmed: bool,
        cancellation: CancellationToken,
    ) -> SkillReloadResult:
        async with self._lock:
            if expected_revision != self._snapshot.revision:
                raise SkillError(SkillErrorCode.CAS_CONFLICT, "Skill trust confirmation used a stale catalog revision")
            descriptor = next(
                (
                    item
                    for item in self._snapshot.descriptors
                    if item.root_id == root_id and item.package_path == package_path
                ),
                None,
            )
            if descriptor is None:
                raise SkillError(SkillErrorCode.NOT_FOUND, "Skill trust target is not discovered")
            if descriptor.layer is SkillLayer.BUILTIN:
                raise SkillError(SkillErrorCode.CONFLICT, "Builtin Skill trust is release-bound")
            if descriptor.content_hash != expected_metadata_hash:
                raise SkillError(SkillErrorCode.HASH_DRIFT, "Skill metadata changed before confirmation")
            secure = self._secure_roots.get(root_id)
            if secure is None:
                raise SkillError(SkillErrorCode.INVALID_ROOT, "Skill root is not active")
            await secure.verify_skill_fact(f"{package_path}/SKILL.md", descriptor.file_fact, cancellation)
            await self._put_trust(
                SkillTrustDecision(
                    self.workspace_id, root_id, package_path, descriptor.name, expected_metadata_hash, confirmed
                ),
                operation=f"confirmation:{expected_revision}:{root_id}:{package_path}",
            )
        return await self.rescan(expected_revision=expected_revision, cancellation=cancellation)

    async def _invalidate_trust(self, descriptor: SkillDescriptor) -> None:
        await self._put_trust(
            SkillTrustDecision(
                self.workspace_id,
                descriptor.root_id,
                descriptor.package_path,
                descriptor.name,
                descriptor.content_hash,
                False,
            ),
            operation=f"read-drift:{self._snapshot.revision}:{descriptor.root_id}:{descriptor.package_path}",
        )

    async def _put_trust(self, decision: SkillTrustDecision, *, operation: str) -> None:
        current = await self._state_store.get_trust(decision.workspace_id, decision.root_id, decision.package_path)
        key = canonical_json_sha256(
            {
                "operation": operation,
                "rootId": decision.root_id,
                "path": decision.package_path,
                "hash": decision.metadata_hash,
                "confirmed": decision.confirmed,
            }
        )
        try:
            await self._state_store.put_trust(
                decision,
                expected_revision=0 if current is None else current.revision,
                idempotency_key=f"skill-trust:{key.removeprefix('sha256:')}",
            )
        except EntityRevisionConflict as error:
            raise SkillError(SkillErrorCode.CAS_CONFLICT, "Skill trust changed concurrently") from error


def _confirmation_token(decision: SkillTrustDecision) -> str:
    material = "\0".join(
        (
            decision.workspace_id,
            decision.root_id,
            decision.package_path,
            decision.metadata_hash,
            str(decision.confirmed),
        )
    ).encode()
    return f"confirmed:{hashlib.sha256(material).hexdigest()}"


def _snapshot_hash(
    workspace_id: str,
    revision: int,
    descriptors: tuple[SkillDescriptor, ...],
    effective: tuple[SkillDescriptor, ...],
) -> str:
    return canonical_json_sha256(
        {
            "workspaceId": workspace_id,
            "revision": revision,
            "skills": [
                {
                    "rootId": item.root_id,
                    "packagePath": item.package_path,
                    "name": item.name,
                    "layer": item.layer.value,
                    "description": item.description,
                    "allowedTools": sorted(item.allowed_tools),
                    "metadataHash": item.content_hash,
                    "trust": item.trust_state.value,
                    "file": [
                        str(item.file_fact.device),
                        str(item.file_fact.inode),
                        str(item.file_fact.size),
                        str(item.file_fact.mtime_ns),
                    ],
                }
                for item in descriptors
            ],
            "effective": [f"{item.root_id}:{item.package_path}" for item in effective],
        }
    )


def _optional_root_is_absent(path: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = os.stat(current, follow_symlinks=False)
        except OSError as error:
            if _is_missing_path_error(error):
                return True
            return False
        if stat.S_ISLNK(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) & 0x0400:
            return False
    return False


def _is_missing_path_error(error: OSError) -> bool:
    """Classify only the OS' canonical missing-file and missing-path errors."""

    return error.errno == errno.ENOENT or getattr(error, "winerror", None) in {2, 3}


__all__ = ["SkillCatalog", "SkillCatalogStatus"]
