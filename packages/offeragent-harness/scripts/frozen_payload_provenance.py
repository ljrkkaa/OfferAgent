"""File-level provenance for PyInstaller onedir release payloads.

The logical ``Requires-Dist`` graph is useful policy input, but it is not a
description of a frozen application.  PyInstaller can copy CPython files,
Windows runtime DLLs, extension modules, package data and build-tool code that
do not appear as ordinary dependency edges.  This module consumes the five
TOCs produced for each onedir target and builds a closed, path-by-path account
of the material that actually enters the runtime ZIP.

Absolute build paths are deliberately never serialized.  They are used only
while classifying a source and are replaced by a bounded component locator and
content digest in the canonical attestation.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from packaging.utils import canonicalize_name

PROVENANCE_PATH = "provenance/frozen-payload.v1.json"
SPDX_PATH = "sbom/runtime.spdx.json"
REQUIRED_TARGETS = {
    "offeragent-host": "offeragent-host.exe",
    "offeragent-process-host": "offeragent-process-host.exe",
    "offeragent-self-test": "offeragent-self-test.exe",
    "offeragent-worker": "offeragent-worker.exe",
}
_TOC_KINDS = ("Analysis", "COLLECT", "EXE", "PKG", "PYZ")
_CONTENT_TYPES = {
    "BINARY",
    "DATA",
    "EXECUTABLE",
    "EXTENSION",
    "PYMODULE",
    "PYSOURCE",
    "PYZ",
}
_KNOWN_TYPES = _CONTENT_TYPES | {"DEPENDENCY", "OPTION", "PKG", "SPLASH", "SYMLINK"}
_WINDOWS_RUNTIME_NAMES = {
    "ucrtbase.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
}
_WINDOWS_RUNTIME_PATTERNS = (
    re.compile(r"api-ms-win-(?:core|crt)-[a-z0-9-]+\.dll", re.IGNORECASE),
    re.compile(r"msvcp\d+(?:_\d+)?\.dll", re.IGNORECASE),
    re.compile(r"vcruntime\d+(?:_\d+)?\.dll", re.IGNORECASE),
)


class FrozenPayloadError(RuntimeError):
    """Raised when a frozen payload cannot be proven closed."""


@dataclass(frozen=True, slots=True)
class Component:
    spdx_id: str
    kind: str
    name: str
    version: str
    license_declared: str = "NOASSERTION"

    def payload(self) -> dict[str, str]:
        return {
            "id": self.spdx_id,
            "kind": self.kind,
            "licenseDeclared": self.license_declared,
            "name": self.name,
            "version": self.version,
        }


try:
    _OFFERAGENT_VERSION = importlib.metadata.version("offeragent-harness")
except importlib.metadata.PackageNotFoundError:
    _OFFERAGENT_VERSION = "0.1.0a0"

OFFERAGENT_COMPONENT = Component(
    "SPDXRef-Package-offeragent-harness",
    "project",
    "offeragent-harness",
    _OFFERAGENT_VERSION,
    "AGPL-3.0-or-later",
)
CPYTHON_COMPONENT = Component(
    "SPDXRef-Package-cpython",
    "cpython",
    "CPython",
    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
)
WINDOWS_COMPONENT = Component(
    "SPDXRef-Package-microsoft-windows-runtime",
    "windows-component",
    "Microsoft Windows Runtime",
    "NOASSERTION",
)


@dataclass(frozen=True, slots=True)
class SourceRecord:
    identifier: str
    component_id: str
    kind: str
    locator: str
    sha256: str

    def payload(self) -> dict[str, str]:
        return {
            "componentId": self.component_id,
            "id": self.identifier,
            "kind": self.kind,
            "locator": self.locator,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class FrozenFile:
    path: str
    component_id: str
    captured_byte_length: int
    captured_sha256: str
    source_refs: tuple[str, ...]
    targets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthenticodeTransform:
    path: str
    pre_sign_sha256: str
    post_sign_sha256: str
    pe_machine: int
    authenticode_verified: bool = True

    def payload(self) -> dict[str, object]:
        return {
            "authenticodeVerified": self.authenticode_verified,
            "kind": "authenticode-sign-v1",
            "path": self.path,
            "peMachine": self.pe_machine,
            "postSignSha256": self.post_sign_sha256,
            "preSignSha256": self.pre_sign_sha256,
        }


@dataclass(frozen=True, slots=True)
class FrozenTarget:
    name: str
    executable_path: str
    toc_sha256: tuple[tuple[str, str], ...]
    source_refs: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "executablePath": self.executable_path,
            "name": self.name,
            "sourceRefs": list(self.source_refs),
            "tocSha256": {key: value for key, value in self.toc_sha256},
        }


@dataclass(slots=True)
class FrozenRuntimeEvidence:
    components: dict[str, Component] = field(default_factory=dict)
    sources: dict[str, SourceRecord] = field(default_factory=dict)
    files: dict[str, FrozenFile] = field(default_factory=dict)
    targets: dict[str, FrozenTarget] = field(default_factory=dict)

    def register_component(self, component: Component) -> None:
        current = self.components.get(component.spdx_id)
        if current is not None and current != component:
            raise FrozenPayloadError(f"component identity collision: {component.spdx_id}")
        self.components[component.spdx_id] = component

    def register_source(self, source: SourceRecord) -> None:
        current = self.sources.get(source.identifier)
        if current is not None and current != source:
            raise FrozenPayloadError(f"source identity collision: {source.identifier}")
        self.sources[source.identifier] = source


@dataclass(frozen=True, slots=True)
class StaticPayloadSource:
    component: Component
    source_path: Path
    locator: str
    kind: str = "project-file"


@dataclass(frozen=True, slots=True)
class _DistributionFile:
    component: Component
    locator: str


class SourceClassifier:
    """Classify TOC source paths without serializing machine-local paths."""

    def __init__(
        self,
        *,
        project_root: Path,
        python_root: Path | None = None,
        windows_root: Path | None = None,
        distributions: Iterable[importlib.metadata.Distribution] | None = None,
    ) -> None:
        self.project_root = project_root.resolve(strict=True)
        self.python_root = (python_root or Path(sys.base_prefix)).resolve(strict=True)
        configured_windows_root = windows_root or Path(os.environ.get("SystemRoot", r"C:\Windows"))
        self.windows_root = configured_windows_root.resolve(strict=False)
        self._distribution_files: dict[str, list[_DistributionFile]] = {}
        for distribution in distributions if distributions is not None else importlib.metadata.distributions():
            name = distribution.metadata["Name"]
            version = distribution.version
            if not isinstance(name, str) or not name.strip() or not isinstance(version, str) or not version.strip():
                continue
            canonical = canonicalize_name(name)
            component = python_distribution_component(name.strip(), version.strip())
            for candidate in distribution.files or ():
                located = Path(str(distribution.locate_file(candidate)))
                if located.is_symlink():
                    continue
                try:
                    resolved = located.resolve(strict=True)
                except OSError:
                    continue
                key = _path_key(resolved)
                try:
                    relative_locator = _safe_locator(candidate)
                except FrozenPayloadError:
                    # Console-script RECORD entries may legally point outside
                    # site-packages. They are not valid frozen source locators.
                    continue
                record = _DistributionFile(component, f"python-distribution:{canonical}/{relative_locator}")
                values = self._distribution_files.setdefault(key, [])
                if record not in values:
                    values.append(record)

    def classify(self, source: Path, *, toc_type: str) -> tuple[Component, str, str]:
        if source.is_symlink():
            raise FrozenPayloadError("PyInstaller TOC source is a symlink")
        try:
            resolved = source.resolve(strict=True)
        except OSError as error:
            raise FrozenPayloadError("PyInstaller TOC source is missing") from error
        basename = resolved.name.casefold()
        if _is_windows_runtime_name(basename):
            if not (_is_relative_to(resolved, self.python_root) or _is_relative_to(resolved, self.windows_root)):
                raise FrozenPayloadError(f"untrusted Windows runtime source: {resolved.name}")
            return WINDOWS_COMPONENT, f"windows-runtime:{basename}", "windows-binary"

        matches = self._distribution_files.get(_path_key(resolved), [])
        identities = {value.component.spdx_id for value in matches}
        if len(identities) > 1:
            raise FrozenPayloadError("PyInstaller source belongs to multiple distributions")
        if matches:
            value = matches[0]
            return value.component, value.locator, _source_kind(toc_type)

        if _is_relative_to(resolved, self.project_root):
            relative = resolved.relative_to(self.project_root).as_posix()
            return OFFERAGENT_COMPONENT, f"project:{relative}", _source_kind(toc_type)
        if _is_relative_to(resolved, self.python_root):
            relative = resolved.relative_to(self.python_root).as_posix()
            return CPYTHON_COMPONENT, f"cpython:{relative}", _source_kind(toc_type)
        raise FrozenPayloadError(f"unmapped PyInstaller source: {resolved.name}")


def capture_pyinstaller_target(
    *,
    name: str,
    root: Path,
    work_root: Path,
    classifier: SourceClassifier,
) -> FrozenRuntimeEvidence:
    """Capture one onedir tree and prove COLLECT exactly accounts for it."""

    root = root.resolve(strict=True)
    work_root = work_root.resolve(strict=True)
    evidence = FrozenRuntimeEvidence()
    for component in (OFFERAGENT_COMPONENT, CPYTHON_COMPONENT, WINDOWS_COMPONENT):
        evidence.register_component(component)

    toc_paths = {kind: _one_toc(work_root, kind) for kind in _TOC_KINDS}
    toc_payloads = {kind: _read_toc(path) for kind, path in toc_paths.items()}
    collect_entries = _entries(toc_payloads["COLLECT"])
    if not collect_entries:
        raise FrozenPayloadError("PyInstaller COLLECT TOC is empty")

    actual_files = {
        path.relative_to(root).as_posix(): path
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
    if not actual_files:
        raise FrozenPayloadError("PyInstaller onedir contains no files")
    mapped: dict[str, tuple[Path, str]] = {}
    for destination, raw_source, toc_type in collect_entries:
        if toc_type not in _CONTENT_TYPES or not raw_source:
            raise FrozenPayloadError("PyInstaller COLLECT TOC contains a non-file entry")
        relative = _safe_payload_path(destination.replace("\\", "/"))
        candidates = [relative]
        if not relative.casefold().endswith(".exe"):
            candidates.append(f"_internal/{relative}")
        present = [candidate for candidate in candidates if candidate in actual_files]
        if len(present) != 1:
            raise FrozenPayloadError(f"PyInstaller COLLECT mapping is missing or ambiguous: {relative}")
        final_path = present[0]
        if final_path.casefold() in {value.casefold() for value in mapped}:
            raise FrozenPayloadError(f"duplicate PyInstaller COLLECT mapping: {final_path}")
        mapped[final_path] = (Path(raw_source), toc_type)
    if set(mapped) != set(actual_files):
        raise FrozenPayloadError("PyInstaller COLLECT TOC does not exactly cover the onedir tree")

    target_source_refs: set[str] = set()
    for kind in ("Analysis", "EXE", "PKG", "PYZ"):
        for _logical_name, raw_source, toc_type in _entries(toc_payloads[kind]):
            if toc_type not in _CONTENT_TYPES or not raw_source or raw_source == "-":
                continue
            source = Path(raw_source)
            registered = _register_toc_source(
                evidence,
                source=source,
                toc_type=toc_type,
                target=name,
                work_root=work_root,
                classifier=classifier,
            )
            if registered is not None:
                target_source_refs.add(registered.identifier)
        for raw_source in _standalone_file_paths(toc_payloads[kind]):
            registered = _register_toc_source(
                evidence,
                source=Path(raw_source),
                toc_type="DATA",
                target=name,
                work_root=work_root,
                classifier=classifier,
            )
            if registered is not None:
                target_source_refs.add(registered.identifier)

    for relative, (source_path, toc_type) in sorted(mapped.items()):
        if _is_relative_to(source_path.resolve(strict=True), work_root):
            component, locator, kind = _classify_generated_collect(
                source_path,
                destination=relative,
                target=name,
                toc_type=toc_type,
            )
        else:
            component, locator, kind = classifier.classify(source_path, toc_type=toc_type)
        evidence.register_component(component)
        source_record = _source_record(component, locator, kind, source_path)
        evidence.register_source(source_record)
        captured_path = actual_files[relative]
        captured_sha256 = _digest(captured_path)
        if source_record.sha256 != captured_sha256:
            raise FrozenPayloadError(f"PyInstaller COLLECT source bytes differ from output: {relative}")
        source_refs = {source_record.identifier}
        if relative.casefold().endswith(".exe"):
            source_refs.update(target_source_refs)
        evidence.files[relative] = FrozenFile(
            relative,
            component.spdx_id,
            captured_path.stat().st_size,
            captured_sha256,
            tuple(sorted(source_refs)),
            (name,),
        )

    executable = REQUIRED_TARGETS.get(name, f"{name}.exe")
    if executable not in evidence.files:
        raise FrozenPayloadError(f"PyInstaller target executable is missing: {name}")
    evidence.targets[name] = FrozenTarget(
        name,
        executable,
        tuple((kind, _digest(path)) for kind, path in sorted(toc_paths.items())),
        tuple(sorted(target_source_refs)),
    )
    return evidence


def merge_frozen_evidence(
    *,
    merged_root: Path,
    targets: Sequence[FrozenRuntimeEvidence],
) -> FrozenRuntimeEvidence:
    """Merge four target attestations while rejecting conflicting ownership."""

    result = FrozenRuntimeEvidence()
    for target in targets:
        for component in target.components.values():
            result.register_component(component)
        for source in target.sources.values():
            result.register_source(source)
        for name, record in target.targets.items():
            if name in result.targets:
                raise FrozenPayloadError(f"duplicate PyInstaller target: {name}")
            result.targets[name] = record
        for path, file_record in target.files.items():
            current = result.files.get(path)
            if current is None:
                result.files[path] = file_record
                continue
            if current.component_id != file_record.component_id:
                raise FrozenPayloadError(f"frozen file ownership differs across targets: {path}")
            if (
                current.captured_byte_length != file_record.captured_byte_length
                or current.captured_sha256 != file_record.captured_sha256
            ):
                raise FrozenPayloadError(f"frozen file capture differs across targets: {path}")
            result.files[path] = FrozenFile(
                path,
                current.component_id,
                current.captured_byte_length,
                current.captured_sha256,
                tuple(sorted(set(current.source_refs) | set(file_record.source_refs))),
                tuple(sorted(set(current.targets) | set(file_record.targets))),
            )
    merged_files = {
        path.relative_to(merged_root).as_posix()
        for path in merged_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if merged_files != set(result.files):
        raise FrozenPayloadError("merged PyInstaller tree differs from captured target TOCs")
    for relative, frozen_file in result.files.items():
        merged_path = merged_root / Path(relative)
        if (
            merged_path.stat().st_size != frozen_file.captured_byte_length
            or _digest(merged_path) != frozen_file.captured_sha256
        ):
            raise FrozenPayloadError(f"merged PyInstaller file differs from captured bytes: {relative}")
    return result


def build_payload_provenance(
    *,
    runtime: Path,
    evidence: FrozenRuntimeEvidence,
    static_sources: Mapping[str, StaticPayloadSource],
    architecture: str,
    build_commit: str,
    runtime_version: str,
    source_date_epoch: int,
    runtime_dependency_closure_sha256: str,
    uv_lock_sha256: str,
    executable_transforms: Mapping[str, AuthenticodeTransform] | None = None,
    require_release_targets: bool = True,
    require_signed_executables: bool = True,
) -> dict[str, object]:
    """Build the canonical non-self-referential payload provenance document."""

    components = dict(evidence.components)
    sources = dict(evidence.sources)
    files = dict(evidence.files)
    operational = {
        path.relative_to(runtime).as_posix(): path
        for path in runtime.rglob("*")
        if path.is_file() and path.relative_to(runtime).as_posix() not in {PROVENANCE_PATH, SPDX_PATH}
    }
    transforms = dict(executable_transforms or {})
    if any(relative != transform.path for relative, transform in transforms.items()):
        raise FrozenPayloadError("Authenticode transform key/path differs")
    for relative, static in static_sources.items():
        if relative in files:
            raise FrozenPayloadError(f"static source duplicates a frozen file mapping: {relative}")
        if relative not in operational:
            raise FrozenPayloadError(f"static source has no runtime file: {relative}")
        _validate_locator(static.locator)
        current_component = components.get(static.component.spdx_id)
        if current_component is not None and current_component != static.component:
            raise FrozenPayloadError(f"static component identity collision: {static.component.spdx_id}")
        components[static.component.spdx_id] = static.component
        source = _source_record(static.component, static.locator, static.kind, static.source_path)
        current_source = sources.get(source.identifier)
        if current_source is not None and current_source != source:
            raise FrozenPayloadError(f"static source identity collision: {source.identifier}")
        sources[source.identifier] = source
        files[relative] = FrozenFile(
            relative,
            static.component.spdx_id,
            static.source_path.stat().st_size,
            source.sha256,
            (source.identifier,),
            (),
        )
    if set(files) != set(operational):
        missing = sorted(set(operational) - set(files))
        extra = sorted(set(files) - set(operational))
        raise FrozenPayloadError(f"payload provenance is not closed; missing={missing[:3]}, extra={extra[:3]}")

    payload_files = []
    executable_paths = {relative for relative in files if relative.casefold().endswith(".exe")}
    if require_signed_executables and set(transforms) != executable_paths:
        raise FrozenPayloadError("Authenticode transforms do not exactly cover runtime executables")
    if set(transforms) - executable_paths:
        raise FrozenPayloadError("Authenticode transform targets a non-executable file")
    for relative, mapping in sorted(files.items()):
        path = operational[relative]
        final_sha256 = _digest(path)
        transform = transforms.get(relative)
        if transform is None:
            if path.stat().st_size != mapping.captured_byte_length or final_sha256 != mapping.captured_sha256:
                raise FrozenPayloadError(f"post-capture payload mutation is not allowed: {relative}")
        elif (
            transform.pre_sign_sha256 != mapping.captured_sha256
            or transform.post_sign_sha256 != final_sha256
            or transform.pre_sign_sha256 == transform.post_sign_sha256
            or transform.authenticode_verified is not True
        ):
            raise FrozenPayloadError(f"Authenticode transform does not match captured/final bytes: {relative}")
        payload_files.append(
            {
                "byteLength": path.stat().st_size,
                "capturedByteLength": mapping.captured_byte_length,
                "capturedSha256": mapping.captured_sha256,
                "componentId": mapping.component_id,
                "path": relative,
                "sha256": final_sha256,
                "sourceRefs": list(mapping.source_refs),
                "targets": list(mapping.targets),
            }
        )
    document: dict[str, object] = {
        "architecture": architecture,
        "buildCommit": build_commit,
        "builder": "scripts/build_windows_release.py",
        "components": [component.payload() for component in sorted(components.values(), key=lambda item: item.spdx_id)],
        "files": payload_files,
        "runtimeDependencyClosureSha256": runtime_dependency_closure_sha256,
        "runtimeVersion": runtime_version,
        "schemaVersion": 1,
        "selfReferenceExclusions": [PROVENANCE_PATH, SPDX_PATH, "runtime-manifest.json", "runtime-manifest.sig"],
        "sourceDateEpoch": source_date_epoch,
        "sources": [source.payload() for source in sorted(sources.values(), key=lambda item: item.identifier)],
        "targets": [target.payload() for target in sorted(evidence.targets.values(), key=lambda item: item.name)],
        "transforms": [transform.payload() for transform in sorted(transforms.values(), key=lambda item: item.path)],
        "uvLockSha256": uv_lock_sha256,
    }
    assert_payload_provenance(
        document,
        expected_files=_file_identity_map(operational),
        expected_executables=executable_paths,
        require_release_targets=require_release_targets,
        require_signed_executables=require_signed_executables,
    )
    return document


def build_spdx_document(
    *,
    runtime: Path,
    provenance: Mapping[str, object],
    created_at: str,
    document_namespace: str,
) -> dict[str, object]:
    """Create a standard SPDX 2.3 document with exact File relationships."""

    components = _components_from_provenance(provenance)
    ownership = _ownership_from_provenance(provenance)
    ownership[PROVENANCE_PATH] = OFFERAGENT_COMPONENT.spdx_id
    components.setdefault(OFFERAGENT_COMPONENT.spdx_id, OFFERAGENT_COMPONENT)
    actual = {
        path.relative_to(runtime).as_posix(): path
        for path in runtime.rglob("*")
        if path.is_file() and path.relative_to(runtime).as_posix() != SPDX_PATH
    }
    if set(actual) != set(ownership):
        raise FrozenPayloadError("SPDX ownership does not exactly cover the runtime payload")

    file_ids: dict[str, str] = {}
    files: list[dict[str, object]] = []
    sha1_by_component: dict[str, list[str]] = {}
    for relative, path in sorted(actual.items()):
        identifier = f"SPDXRef-File-{hashlib.sha256(relative.encode('utf-8')).hexdigest()}"
        file_ids[relative] = identifier
        sha1 = _digest_algorithm(path, "sha1")
        sha256 = _digest(path).removeprefix("sha256:")
        sha1_by_component.setdefault(ownership[relative], []).append(sha1)
        files.append(
            {
                "SPDXID": identifier,
                "checksums": [
                    {"algorithm": "SHA1", "checksumValue": sha1},
                    {"algorithm": "SHA256", "checksumValue": sha256},
                ],
                "copyrightText": "NOASSERTION",
                "fileName": f"./{relative}",
                "licenseConcluded": "NOASSERTION",
                "licenseInfoInFiles": ["NOASSERTION"],
            }
        )

    packages: list[dict[str, object]] = []
    for identifier, component in sorted(components.items()):
        file_hashes = sorted(sha1_by_component.get(identifier, ()))
        verification = hashlib.sha1("".join(file_hashes).encode("ascii")).hexdigest()
        packages.append(
            {
                "SPDXID": identifier,
                "copyrightText": "NOASSERTION",
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": True,
                "licenseConcluded": component.license_declared,
                "licenseDeclared": component.license_declared,
                "name": component.name,
                "packageVerificationCode": {"packageVerificationCodeValue": verification},
                "versionInfo": component.version,
            }
        )

    relationships: list[dict[str, str]] = []
    for component_id in sorted(components):
        relationships.append(
            {
                "relatedSpdxElement": component_id,
                "relationshipType": "DESCRIBES",
                "spdxElementId": "SPDXRef-DOCUMENT",
            }
        )
    for relative, component_id in sorted(ownership.items()):
        relationships.append(
            {
                "relatedSpdxElement": file_ids[relative],
                "relationshipType": "CONTAINS",
                "spdxElementId": component_id,
            }
        )
    for component_id in sorted(set(components) - {OFFERAGENT_COMPONENT.spdx_id}):
        relationships.append(
            {
                "relatedSpdxElement": component_id,
                "relationshipType": "DEPENDS_ON",
                "spdxElementId": OFFERAGENT_COMPONENT.spdx_id,
            }
        )
    return {
        "SPDXID": "SPDXRef-DOCUMENT",
        "creationInfo": {
            "created": created_at,
            "creators": ["Tool: OfferAgent scripts/build_windows_release.py"],
        },
        "dataLicense": "CC0-1.0",
        "documentNamespace": document_namespace,
        "files": files,
        "name": "OfferAgent Runtime",
        "packages": packages,
        "relationships": relationships,
        "spdxVersion": "SPDX-2.3",
    }


def assert_payload_provenance(
    document: object,
    *,
    expected_files: Mapping[str, tuple[int, str]],
    expected_executables: set[str] | None = None,
    require_release_targets: bool = True,
    require_signed_executables: bool = True,
) -> dict[str, str]:
    """Independently validate canonical provenance against signed file records."""

    expected_keys = {
        "architecture",
        "buildCommit",
        "builder",
        "components",
        "files",
        "runtimeDependencyClosureSha256",
        "runtimeVersion",
        "schemaVersion",
        "selfReferenceExclusions",
        "sourceDateEpoch",
        "sources",
        "targets",
        "transforms",
        "uvLockSha256",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise FrozenPayloadError("payload provenance document shape is invalid")
    if (
        document["schemaVersion"] != 1
        or document["builder"] != "scripts/build_windows_release.py"
        or not isinstance(document["architecture"], str)
        or document["architecture"] not in {"x64", "arm64"}
        or not isinstance(document["buildCommit"], str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", document["buildCommit"]) is None
        or not isinstance(document["runtimeVersion"], str)
        or re.fullmatch(r"[A-Za-z0-9._+-]{1,128}", document["runtimeVersion"]) is None
        or document["selfReferenceExclusions"]
        != [PROVENANCE_PATH, SPDX_PATH, "runtime-manifest.json", "runtime-manifest.sig"]
        or not isinstance(document["sourceDateEpoch"], int)
        or isinstance(document["sourceDateEpoch"], bool)
    ):
        raise FrozenPayloadError("payload provenance identity is invalid")
    for key in ("runtimeDependencyClosureSha256", "uvLockSha256"):
        if not _is_sha256(document[key]):
            raise FrozenPayloadError("payload provenance digest is invalid")

    components_raw = document["components"]
    if not isinstance(components_raw, list) or not components_raw:
        raise FrozenPayloadError("payload provenance components are missing")
    components: dict[str, str] = {}
    component_kinds: dict[str, str] = {}
    for raw in components_raw:
        if not isinstance(raw, dict) or set(raw) != {"id", "kind", "licenseDeclared", "name", "version"}:
            raise FrozenPayloadError("payload provenance component is malformed")
        identifier = raw["id"]
        if (
            not isinstance(identifier, str)
            or re.fullmatch(r"SPDXRef-Package-[A-Za-z0-9.-]{1,160}", identifier) is None
            or identifier in components
            or not isinstance(raw["kind"], str)
            or raw["kind"] not in {"project", "python-distribution", "cpython", "windows-component"}
            or not isinstance(raw["name"], str)
            or not raw["name"]
            or not isinstance(raw["version"], str)
            or not raw["version"]
            or not isinstance(raw["licenseDeclared"], str)
            or raw["licenseDeclared"] not in {"AGPL-3.0-or-later", "NOASSERTION"}
        ):
            raise FrozenPayloadError("payload provenance component identity is invalid")
        components[identifier] = raw["name"]
        component_kinds[identifier] = raw["kind"]
    if [raw["id"] for raw in components_raw] != sorted(components):
        raise FrozenPayloadError("payload provenance components are not canonical")
    if OFFERAGENT_COMPONENT.spdx_id not in components:
        raise FrozenPayloadError("payload provenance omits OfferAgent")

    sources_raw = document["sources"]
    if not isinstance(sources_raw, list) or not sources_raw:
        raise FrozenPayloadError("payload provenance sources are missing")
    sources: set[str] = set()
    source_components: dict[str, str] = {}
    source_hashes: dict[str, str] = {}
    for raw in sources_raw:
        if not isinstance(raw, dict) or set(raw) != {"componentId", "id", "kind", "locator", "sha256"}:
            raise FrozenPayloadError("payload provenance source is malformed")
        identifier = raw["id"]
        if (
            not isinstance(identifier, str)
            or re.fullmatch(r"source-[a-f0-9]{64}", identifier) is None
            or identifier in sources
            or not isinstance(raw["componentId"], str)
            or raw["componentId"] not in components
            or not isinstance(raw["kind"], str)
            or not raw["kind"]
            or not isinstance(raw["locator"], str)
            or not _is_sha256(raw["sha256"])
        ):
            raise FrozenPayloadError("payload provenance source identity is invalid")
        _validate_locator(raw["locator"])
        expected_id = _source_identifier(raw["componentId"], raw["locator"], raw["kind"], raw["sha256"])
        if identifier != expected_id:
            raise FrozenPayloadError("payload provenance source identifier is not canonical")
        sources.add(identifier)
        source_components[identifier] = raw["componentId"]
        source_hashes[identifier] = raw["sha256"]
    if [raw["id"] for raw in sources_raw] != sorted(sources):
        raise FrozenPayloadError("payload provenance sources are not canonical")

    targets_raw = document["targets"]
    if not isinstance(targets_raw, list):
        raise FrozenPayloadError("payload provenance targets are malformed")
    targets: dict[str, str] = {}
    for raw in targets_raw:
        if not isinstance(raw, dict) or set(raw) != {"executablePath", "name", "sourceRefs", "tocSha256"}:
            raise FrozenPayloadError("payload provenance target is malformed")
        name = raw["name"]
        tocs = raw["tocSha256"]
        refs = raw["sourceRefs"]
        if (
            not isinstance(name, str)
            or name in targets
            or not isinstance(raw["executablePath"], str)
            or not isinstance(tocs, dict)
            or set(tocs) != set(_TOC_KINDS)
            or any(not _is_sha256(value) for value in tocs.values())
            or not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) for ref in refs)
            or refs != sorted(set(refs))
            or any(ref not in sources for ref in refs)
        ):
            raise FrozenPayloadError("payload provenance target identity is invalid")
        targets[name] = raw["executablePath"]
    if [raw["name"] for raw in targets_raw] != sorted(targets):
        raise FrozenPayloadError("payload provenance targets are not canonical")
    if require_release_targets and targets != REQUIRED_TARGETS:
        raise FrozenPayloadError("payload provenance does not contain the four release targets")

    expected_executable_paths = (
        set(expected_executables)
        if expected_executables is not None
        else {path for path in expected_files if path.casefold().endswith(".exe")}
    )
    transforms_raw = document["transforms"]
    if not isinstance(transforms_raw, list):
        raise FrozenPayloadError("payload provenance transforms are malformed")
    transforms: dict[str, dict[str, object]] = {}
    expected_machine = {"x64": 0x8664, "arm64": 0xAA64}[document["architecture"]]
    for raw in transforms_raw:
        if not isinstance(raw, dict) or set(raw) != {
            "authenticodeVerified",
            "kind",
            "path",
            "peMachine",
            "postSignSha256",
            "preSignSha256",
        }:
            raise FrozenPayloadError("payload provenance transform is malformed")
        path = raw["path"]
        if (
            not isinstance(path, str)
            or _safe_payload_path(path) != path
            or path in transforms
            or path not in expected_executable_paths
            or raw["kind"] != "authenticode-sign-v1"
            or raw["authenticodeVerified"] is not True
            or raw["peMachine"] != expected_machine
            or not _is_sha256(raw["preSignSha256"])
            or not _is_sha256(raw["postSignSha256"])
            or raw["preSignSha256"] == raw["postSignSha256"]
        ):
            raise FrozenPayloadError("payload provenance Authenticode transform is invalid")
        transforms[path] = raw
    if [raw["path"] for raw in transforms_raw] != sorted(transforms):
        raise FrozenPayloadError("payload provenance transforms are not canonical")
    if require_signed_executables and set(transforms) != expected_executable_paths:
        raise FrozenPayloadError("payload provenance does not transform every signed executable")

    files_raw = document["files"]
    if not isinstance(files_raw, list) or not files_raw:
        raise FrozenPayloadError("payload provenance files are missing")
    ownership: dict[str, str] = {}
    observed: dict[str, tuple[int, str]] = {}
    observed_casefold: set[str] = set()
    for raw in files_raw:
        if not isinstance(raw, dict) or set(raw) != {
            "byteLength",
            "capturedByteLength",
            "capturedSha256",
            "componentId",
            "path",
            "sha256",
            "sourceRefs",
            "targets",
        }:
            raise FrozenPayloadError("payload provenance file is malformed")
        path = raw["path"]
        refs = raw["sourceRefs"]
        target_names = raw["targets"]
        component_id = raw["componentId"]
        if (
            not isinstance(path, str)
            or path in observed
            or path.casefold() in observed_casefold
            or _safe_payload_path(path) != path
            or not isinstance(component_id, str)
            or component_id not in components
            or not isinstance(raw["byteLength"], int)
            or isinstance(raw["byteLength"], bool)
            or raw["byteLength"] < 0
            or not isinstance(raw["capturedByteLength"], int)
            or isinstance(raw["capturedByteLength"], bool)
            or raw["capturedByteLength"] < 0
            or not _is_sha256(raw["capturedSha256"])
            or not _is_sha256(raw["sha256"])
            or not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) for ref in refs)
            or refs != sorted(set(refs))
            or any(ref not in sources for ref in refs)
            or not isinstance(target_names, list)
            or any(not isinstance(target, str) for target in target_names)
            or target_names != sorted(set(target_names))
            or any(target not in targets for target in target_names)
        ):
            raise FrozenPayloadError("payload provenance file identity is invalid")
        if component_kinds[component_id] not in {"project", "python-distribution", "cpython", "windows-component"}:
            raise FrozenPayloadError("payload provenance file has an unsupported owner")
        if not any(source_components[ref] == component_id for ref in refs):
            raise FrozenPayloadError("payload provenance file has no source owned by its component")
        if not any(
            source_components[ref] == component_id and source_hashes[ref] == raw["capturedSha256"] for ref in refs
        ):
            raise FrozenPayloadError("payload provenance capture hash is not backed by its source")
        transform = transforms.get(path)
        if transform is None:
            if raw["capturedByteLength"] != raw["byteLength"] or raw["capturedSha256"] != raw["sha256"]:
                raise FrozenPayloadError("payload provenance contains an undeclared post-capture mutation")
        elif transform["preSignSha256"] != raw["capturedSha256"] or transform["postSignSha256"] != raw["sha256"]:
            raise FrozenPayloadError("payload provenance Authenticode transform hashes differ from its file")
        observed[path] = (raw["byteLength"], raw["sha256"])
        observed_casefold.add(path.casefold())
        ownership[path] = component_id
    if [raw["path"] for raw in files_raw] != sorted(observed):
        raise FrozenPayloadError("payload provenance files are not canonical")
    if observed != dict(expected_files):
        raise FrozenPayloadError("payload provenance file set or hashes differ from the signed payload")
    for target, executable in targets.items():
        del target
        if ownership.get(executable) != OFFERAGENT_COMPONENT.spdx_id:
            raise FrozenPayloadError("payload provenance executable ownership is invalid")
    return ownership


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def make_source_record(component: Component, locator: str, kind: str, path: Path) -> SourceRecord:
    """Create a canonical source record for deterministic fixtures/static inputs."""

    return _source_record(component, locator, kind, path)


def _register_toc_source(
    evidence: FrozenRuntimeEvidence,
    *,
    source: Path,
    toc_type: str,
    target: str,
    work_root: Path,
    classifier: SourceClassifier,
) -> SourceRecord | None:
    try:
        resolved = source.resolve(strict=True)
    except OSError as error:
        raise FrozenPayloadError("PyInstaller TOC source is missing") from error
    if _is_relative_to(resolved, work_root):
        basename = resolved.name.casefold()
        if basename in {"pyz-00.pyz", f"{target}.pkg", f"{target}.exe", "base_library.zip"}:
            return None
        if "localpycs" not in {part.casefold() for part in resolved.parts}:
            raise FrozenPayloadError(f"unmapped generated PyInstaller source: {resolved.name}")
        if basename.startswith("pyimod"):
            component = _pyinstaller_component()
            kind = "pyinstaller-loader-bytecode"
        elif basename == "struct.pyc":
            component = CPYTHON_COMPONENT
            kind = "cpython-bytecode"
        else:
            raise FrozenPayloadError(f"unmapped generated PyInstaller bytecode: {resolved.name}")
        locator = f"pyinstaller-generated:{target}/localpycs/{resolved.name}"
    else:
        component, locator, kind = classifier.classify(resolved, toc_type=toc_type)
    evidence.register_component(component)
    record = _source_record(component, locator, kind, resolved)
    evidence.register_source(record)
    return record


def _classify_generated_collect(
    source: Path,
    *,
    destination: str,
    target: str,
    toc_type: str,
) -> tuple[Component, str, str]:
    basename = source.name.casefold()
    if toc_type == "EXECUTABLE" and destination.casefold().endswith(".exe"):
        return OFFERAGENT_COMPONENT, f"pyinstaller-generated:{target}/{source.name}", "frozen-executable"
    if basename == "base_library.zip" and toc_type == "DATA":
        return CPYTHON_COMPONENT, f"pyinstaller-generated:{target}/base_library.zip", "cpython-base-library"
    raise FrozenPayloadError(f"unmapped generated COLLECT source: {source.name}")


def _components_from_provenance(provenance: Mapping[str, object]) -> dict[str, Component]:
    result: dict[str, Component] = {}
    raw_components = provenance.get("components")
    if not isinstance(raw_components, list):
        raise FrozenPayloadError("payload provenance components are malformed")
    for raw in raw_components:
        if not isinstance(raw, dict):
            raise FrozenPayloadError("payload provenance component is malformed")
        component = Component(
            str(raw.get("id", "")),
            str(raw.get("kind", "")),
            str(raw.get("name", "")),
            str(raw.get("version", "")),
            str(raw.get("licenseDeclared", "")),
        )
        result[component.spdx_id] = component
    return result


def _ownership_from_provenance(provenance: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    raw_files = provenance.get("files")
    if not isinstance(raw_files, list):
        raise FrozenPayloadError("payload provenance files are malformed")
    for raw in raw_files:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("path"), str)
            or not isinstance(raw.get("componentId"), str)
        ):
            raise FrozenPayloadError("payload provenance file is malformed")
        result[raw["path"]] = raw["componentId"]
    return result


def _file_identity_map(files: Mapping[str, Path]) -> dict[str, tuple[int, str]]:
    return {relative: (path.stat().st_size, _digest(path)) for relative, path in sorted(files.items())}


def _one_toc(work_root: Path, kind: str) -> Path:
    matches = sorted(work_root.glob(f"{kind}-*.toc"))
    if len(matches) != 1:
        raise FrozenPayloadError(f"PyInstaller {kind} TOC is missing or ambiguous")
    return matches[0]


def _read_toc(path: Path) -> object:
    if path.stat().st_size > 64 * 1024 * 1024:
        raise FrozenPayloadError("PyInstaller TOC is too large")
    try:
        return ast.literal_eval(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, SyntaxError, ValueError) as error:
        raise FrozenPayloadError("PyInstaller TOC is malformed") from error


def _entries(value: object) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []

    def visit(item: object) -> None:
        if isinstance(item, (list, tuple)):
            if len(item) == 3 and isinstance(item[0], str) and isinstance(item[2], str):
                toc_type = item[2]
                if toc_type in _KNOWN_TYPES:
                    source = item[1]
                    if source is not None and not isinstance(source, str):
                        raise FrozenPayloadError("PyInstaller TOC source is malformed")
                    result.append((item[0], source or "", toc_type))
                    return
                if re.fullmatch(r"[A-Z_]{2,32}", toc_type):
                    raise FrozenPayloadError(f"unsupported PyInstaller TOC type: {toc_type}")
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)

    visit(value)
    return result


def _standalone_file_paths(value: object) -> list[str]:
    result: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, (list, tuple)):
            if len(item) == 3 and isinstance(item[2], str) and item[2] in _KNOWN_TYPES:
                return
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, str):
            try:
                path = Path(item)
                if path.is_absolute() and path.is_file():
                    result.add(str(path))
            except OSError:
                return

    visit(value)
    return sorted(result)


def python_distribution_component(name: str, version: str) -> Component:
    canonical = canonicalize_name(name)
    if canonical == "offeragent-harness":
        return Component(
            OFFERAGENT_COMPONENT.spdx_id,
            OFFERAGENT_COMPONENT.kind,
            name,
            version,
            OFFERAGENT_COMPONENT.license_declared,
        )
    identifier = re.sub(r"[^A-Za-z0-9.-]", "-", canonical)
    return Component(f"SPDXRef-Package-{identifier}", "python-distribution", name, version)


def _pyinstaller_component() -> Component:
    try:
        distribution = importlib.metadata.distribution("PyInstaller")
    except importlib.metadata.PackageNotFoundError as error:
        raise FrozenPayloadError("PyInstaller distribution metadata is missing") from error
    name = distribution.metadata["Name"]
    if not isinstance(name, str) or not name.strip():
        raise FrozenPayloadError("PyInstaller distribution metadata is malformed")
    return python_distribution_component(name.strip(), distribution.version)


def _source_record(component: Component, locator: str, kind: str, path: Path) -> SourceRecord:
    _validate_locator(locator)
    sha256 = _digest(path)
    identifier = _source_identifier(component.spdx_id, locator, kind, sha256)
    return SourceRecord(identifier, component.spdx_id, kind, locator, sha256)


def _source_identifier(component_id: object, locator: object, kind: object, sha256: object) -> str:
    payload = canonical_json({"componentId": component_id, "kind": kind, "locator": locator, "sha256": sha256})
    return f"source-{hashlib.sha256(payload).hexdigest()}"


def _safe_payload_path(value: str) -> str:
    if "\\" in value or "\0" in value or re.match(r"^[A-Za-z]:", value):
        raise FrozenPayloadError("payload path is not canonical")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise FrozenPayloadError("payload path escapes its root")
    normalized = path.as_posix()
    if normalized != value:
        raise FrozenPayloadError("payload path is not canonical")
    return normalized


def _safe_locator(value: object) -> str:
    text = str(value).replace("\\", "/")
    if any(part in {"", ".", ".."} for part in text.split("/")) or "\0" in text:
        raise FrozenPayloadError("source locator is not canonical")
    return text


def _validate_locator(value: str) -> None:
    prefix, separator, tail = value.partition(":")
    if (
        not value
        or len(value) > 1024
        or separator != ":"
        or prefix
        not in {
            "cpython",
            "project",
            "pyinstaller-generated",
            "python-distribution",
            "windows-runtime",
        }
        or not tail
        or ":" in tail
        or "\\" in value
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in tail.split("/"))
        or any(ord(character) < 0x20 for character in value)
    ):
        raise FrozenPayloadError("source locator leaks or contains an unsafe path")


def _source_kind(toc_type: str) -> str:
    return {
        "BINARY": "native-binary",
        "DATA": "data",
        "EXECUTABLE": "executable",
        "EXTENSION": "python-extension",
        "PYMODULE": "python-module",
        "PYSOURCE": "python-source",
        "PYZ": "python-module-archive",
    }.get(toc_type, "unknown")


def _is_windows_runtime_name(basename: str) -> bool:
    return basename in _WINDOWS_RUNTIME_NAMES or any(
        pattern.fullmatch(basename) for pattern in _WINDOWS_RUNTIME_PATTERNS
    )


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _digest(path: Path) -> str:
    return f"sha256:{_digest_algorithm(path, 'sha256')}"


def _digest_algorithm(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", value) is not None


__all__ = [
    "CPYTHON_COMPONENT",
    "OFFERAGENT_COMPONENT",
    "PROVENANCE_PATH",
    "REQUIRED_TARGETS",
    "SPDX_PATH",
    "WINDOWS_COMPONENT",
    "AuthenticodeTransform",
    "Component",
    "FrozenFile",
    "FrozenPayloadError",
    "FrozenRuntimeEvidence",
    "FrozenTarget",
    "SourceClassifier",
    "StaticPayloadSource",
    "assert_payload_provenance",
    "build_payload_provenance",
    "build_spdx_document",
    "canonical_json",
    "capture_pyinstaller_target",
    "make_source_record",
    "merge_frozen_evidence",
    "python_distribution_component",
]
