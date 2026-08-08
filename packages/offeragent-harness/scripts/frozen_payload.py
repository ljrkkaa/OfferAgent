"""Closed, file-level evidence for the personal PyInstaller onedir Runtime.

The logical ``Requires-Dist`` graph is useful policy input, but it is not a
description of a frozen application. PyInstaller can copy CPython files,
Windows runtime DLLs, extension modules, package data and build-tool code that
do not appear as ordinary dependency edges.  This module consumes the five
TOCs produced for each onedir target and builds a closed, path-by-path account
of the material that actually enters the local Runtime tree.

Absolute build paths are deliberately never serialized.  They are used only
while classifying a source and are replaced by a bounded component locator and
content digest in the in-memory build evidence.
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

REQUIRED_TARGETS = {
    "offeragent-process-host": "offeragent-process-host.exe",
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
    _source_sha_by_stable_identity: dict[tuple[str, str, str], str] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _source_index_ready: bool = field(default=False, init=False, repr=False)

    def register_component(self, component: Component) -> None:
        current = self.components.get(component.spdx_id)
        if current is not None and current != component:
            raise FrozenPayloadError(f"component identity collision: {component.spdx_id}")
        self.components[component.spdx_id] = component

    def register_source(self, source: SourceRecord) -> None:
        self._ensure_source_index()
        stable_identity = (source.locator, source.component_id, source.kind)
        current_sha256 = self._source_sha_by_stable_identity.get(stable_identity)
        if current_sha256 is not None and current_sha256 != source.sha256:
            raise FrozenPayloadError(f"source bytes differ for stable locator/component/kind: {source.locator}")
        current = self.sources.get(source.identifier)
        if current is not None and current != source:
            raise FrozenPayloadError(f"source identity collision: {source.identifier}")
        self.sources[source.identifier] = source
        self._source_sha_by_stable_identity[stable_identity] = source.sha256

    def _ensure_source_index(self) -> None:
        if self._source_index_ready:
            return
        for source in self.sources.values():
            stable_identity = (source.locator, source.component_id, source.kind)
            current_sha256 = self._source_sha_by_stable_identity.get(stable_identity)
            if current_sha256 is not None and current_sha256 != source.sha256:
                raise FrozenPayloadError(f"source bytes differ for stable locator/component/kind: {source.locator}")
            self._source_sha_by_stable_identity[stable_identity] = source.sha256
        self._source_index_ready = True


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
    """Merge target evidence while rejecting conflicting ownership."""

    result = FrozenRuntimeEvidence()
    merged_paths_by_casefold: dict[str, str] = {}
    for merged_path in sorted(merged_root.rglob("*")):
        if merged_path.is_symlink():
            raise FrozenPayloadError("merged PyInstaller tree contains a symlink")
        if merged_path.is_dir():
            continue
        if not merged_path.is_file() or merged_path.stat().st_nlink != 1:
            raise FrozenPayloadError("merged PyInstaller tree contains a hard link or special file")
        relative = merged_path.relative_to(merged_root).as_posix()
        folded_path = relative.casefold()
        current_path = merged_paths_by_casefold.get(folded_path)
        if current_path is not None and current_path != relative:
            raise FrozenPayloadError("merged PyInstaller tree contains a case-insensitive path collision")
        merged_paths_by_casefold[folded_path] = relative
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
            if _safe_payload_path(path) != path or file_record.path != path:
                raise FrozenPayloadError("frozen file path identity differs from its evidence key")
            folded_path = path.casefold()
            canonical_path = merged_paths_by_casefold.get(folded_path)
            if canonical_path is None:
                raise FrozenPayloadError(f"captured target file is absent from merged PyInstaller tree: {path}")
            current = result.files.get(canonical_path)
            if current is None:
                result.files[canonical_path] = FrozenFile(
                    canonical_path,
                    file_record.component_id,
                    file_record.captured_byte_length,
                    file_record.captured_sha256,
                    file_record.source_refs,
                    file_record.targets,
                )
                continue
            if current.component_id != file_record.component_id:
                raise FrozenPayloadError(f"frozen file ownership differs across targets: {canonical_path}")
            if (
                current.captured_byte_length != file_record.captured_byte_length
                or current.captured_sha256 != file_record.captured_sha256
            ):
                raise FrozenPayloadError(f"frozen file capture differs across targets: {canonical_path}")
            result.files[canonical_path] = FrozenFile(
                canonical_path,
                current.component_id,
                current.captured_byte_length,
                current.captured_sha256,
                tuple(sorted(set(current.source_refs) | set(file_record.source_refs))),
                tuple(sorted(set(current.targets) | set(file_record.targets))),
            )
    merged_files = set(merged_paths_by_casefold.values())
    if merged_files != set(result.files):
        raise FrozenPayloadError(
            "merged PyInstaller tree differs from captured target TOCs: "
            f"uncaptured={sorted(merged_files - set(result.files))}, "
            f"missing={sorted(set(result.files) - merged_files)}"
        )
    for relative, frozen_file in result.files.items():
        merged_path = merged_root / Path(relative)
        if (
            merged_path.stat().st_size != frozen_file.captured_byte_length
            or _digest(merged_path) != frozen_file.captured_sha256
        ):
            raise FrozenPayloadError(f"merged PyInstaller file differs from captured bytes: {relative}")
    return result


def verify_project_source_snapshot(
    evidence: FrozenRuntimeEvidence,
    snapshot: Mapping[str, str],
) -> None:
    """Bind every frozen project input to the build-start source snapshot."""

    matched = 0
    for source in evidence.sources.values():
        if not source.locator.startswith("project:"):
            continue
        matched += 1
        if source.component_id != OFFERAGENT_COMPONENT.spdx_id:
            raise FrozenPayloadError(f"frozen project source has the wrong component: {source.locator}")
        expected_sha256 = snapshot.get(source.locator)
        if expected_sha256 is None:
            raise FrozenPayloadError(f"frozen project source is absent from source identity: {source.locator}")
        if source.sha256 != expected_sha256:
            raise FrozenPayloadError(f"frozen project source differs from source identity: {source.locator}")
    if matched == 0:
        raise FrozenPayloadError("frozen Runtime contains no project source records")


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


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


__all__ = [
    "FrozenFile",
    "FrozenPayloadError",
    "FrozenRuntimeEvidence",
    "FrozenTarget",
    "SourceClassifier",
    "SourceRecord",
    "capture_pyinstaller_target",
    "merge_frozen_evidence",
    "verify_project_source_snapshot",
]
