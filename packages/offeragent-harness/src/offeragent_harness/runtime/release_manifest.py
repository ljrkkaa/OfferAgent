"""Signed Windows Runtime release manifest and archive verification.

The detached Ed25519 signature is the release trust root.  Authenticode is an
additional executable identity check; neither it nor SHA-256 replaces the
manifest signature.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import stat
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .release_privileges import (
    RuntimePrivilegeEnvelope,
    RuntimePrivilegeError,
    build_privilege_envelope_from_process_catalog,
    parse_privilege_envelope,
    privilege_envelope_payload,
)

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,63}$")
_KEY_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PROTOCOL_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]{86}$")
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_RESERVED_NAMES = frozenset(
    {
        "CON",
        "CONIN$",
        "CONOUT$",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_SPECIAL_MANIFEST_FILES = frozenset({"runtime-manifest.json", "runtime-manifest.sig"})
_REQUIRED_EXECUTABLES = frozenset(
    {
        "offeragent-host.exe",
        "offeragent-process-host.exe",
        "offeragent-self-test.exe",
        "offeragent-worker.exe",
    }
)
_ALLOWED_KINDS = frozenset({"asset", "executable", "license", "provenance", "runtime", "sbom", "skill", "web"})
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
WINDOWS_PE_MACHINE_BY_ARCHITECTURE: Mapping[str, int] = MappingProxyType(
    {
        "arm64": 0xAA64,
        "x64": 0x8664,
    }
)


class ReleaseVerificationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class AuthenticodeChecker(Protocol):
    def verify(self, executable: Path) -> bool: ...


@dataclass(frozen=True, slots=True)
class RuntimePlatform:
    os_name: str
    architecture: str
    minimum_windows_build: int

    def __post_init__(self) -> None:
        if self.os_name != "windows" or self.architecture not in WINDOWS_PE_MACHINE_BY_ARCHITECTURE:
            raise ReleaseVerificationError(
                "platform_unsupported",
                "only native Windows x64 and arm64 releases are supported",
            )
        if self.minimum_windows_build < 10_240:
            raise ReleaseVerificationError("minimum_windows_invalid", "minimum Windows build is invalid")


@dataclass(frozen=True, slots=True)
class ProtocolCompatibility:
    minimum: str
    maximum: str
    schema_hash: str

    def __post_init__(self) -> None:
        minimum = _protocol_tuple(self.minimum)
        maximum = _protocol_tuple(self.maximum)
        if minimum > maximum:
            raise ReleaseVerificationError("protocol_range_invalid", "protocol compatibility range is inverted")
        if not _SHA256.fullmatch(self.schema_hash):
            raise ReleaseVerificationError("schema_hash_invalid", "release schemaHash is invalid")


@dataclass(frozen=True, slots=True)
class RuntimeFileRecord:
    path: str
    byte_length: int
    sha256: str
    kind: str
    authenticode: bool = False

    def __post_init__(self) -> None:
        _validate_archive_path(self.path, allow_directory=False)
        if self.path in _SPECIAL_MANIFEST_FILES:
            raise ReleaseVerificationError("manifest_self_reference", "manifest files are verified separately")
        if self.byte_length < 0 or self.byte_length > 2 * 1024 * 1024 * 1024:
            raise ReleaseVerificationError("file_size_invalid", "runtime file size is outside release limits")
        if not _SHA256.fullmatch(self.sha256):
            raise ReleaseVerificationError("file_hash_invalid", "runtime file SHA-256 is invalid")
        if self.kind not in _ALLOWED_KINDS:
            raise ReleaseVerificationError("file_kind_invalid", "runtime file kind is unsupported")
        if self.authenticode and not self.path.casefold().endswith(".exe"):
            raise ReleaseVerificationError(
                "authenticode_target_invalid", "Authenticode is only valid for PE executables"
            )


@dataclass(frozen=True, slots=True)
class BootstrapRecord:
    path: str
    byte_length: int
    sha256: str
    authenticode: bool
    dependencies: tuple[RuntimeFileRecord, ...] = ()

    def __post_init__(self) -> None:
        _validate_archive_path(self.path, allow_directory=False)
        if "/" in self.path or not self.path.casefold().endswith(".exe"):
            raise ReleaseVerificationError("bootstrap_path_invalid", "bootstrap helper must be a top-level .exe")
        if self.byte_length < 1 or not _SHA256.fullmatch(self.sha256):
            raise ReleaseVerificationError("bootstrap_identity_invalid", "bootstrap helper identity is invalid")
        if not self.authenticode:
            raise ReleaseVerificationError("bootstrap_unsigned", "bootstrap helper must require Authenticode")
        folded = [item.path.casefold() for item in self.dependencies]
        if len(set(folded)) != len(folded) or self.path.casefold() in folded:
            raise ReleaseVerificationError("bootstrap_dependency_collision", "bootstrap dependency paths collide")
        if tuple(sorted(self.dependencies, key=lambda item: item.path)) != self.dependencies:
            raise ReleaseVerificationError("bootstrap_dependencies_unsorted", "bootstrap dependencies must be sorted")


@dataclass(frozen=True, slots=True)
class RuntimeArchive:
    file_name: str
    content_digest: str
    maximum_expanded_bytes: int

    def __post_init__(self) -> None:
        _validate_archive_path(self.file_name, allow_directory=False)
        if "/" in self.file_name or not self.file_name.casefold().endswith(".zip"):
            raise ReleaseVerificationError("archive_name_invalid", "runtime archive must be a top-level ZIP")
        if not _SHA256.fullmatch(self.content_digest):
            raise ReleaseVerificationError("archive_digest_invalid", "runtime archive content digest is invalid")
        if not 1 <= self.maximum_expanded_bytes <= 4 * 1024 * 1024 * 1024:
            raise ReleaseVerificationError("archive_limit_invalid", "runtime archive expanded limit is invalid")


@dataclass(frozen=True, slots=True)
class RuntimeReleaseManifest:
    runtime_version: str
    core_version: str
    plugin_minimum_version: str
    plugin_maximum_version: str
    signing_key_id: str
    build_commit: str
    created_at: datetime
    platform: RuntimePlatform
    protocol: ProtocolCompatibility
    state_schema_version: int
    tool_abi_version: str
    archive: RuntimeArchive
    bootstrap: BootstrapRecord
    files: tuple[RuntimeFileRecord, ...]
    capabilities: tuple[str, ...]
    privilege_envelope: RuntimePrivilegeEnvelope | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version not in {1, 2}:
            raise ReleaseVerificationError("manifest_schema_unsupported", "unsupported runtime manifest schema")
        if (self.schema_version == 2) != (self.privilege_envelope is not None):
            raise ReleaseVerificationError(
                "manifest_privilege_envelope_required",
                "manifest schema v2 requires exactly one Runtime privilege envelope",
            )
        for version in (
            self.runtime_version,
            self.core_version,
            self.plugin_minimum_version,
            self.plugin_maximum_version,
            self.tool_abi_version,
        ):
            if not _VERSION.fullmatch(version):
                raise ReleaseVerificationError("version_invalid", "release version field is invalid")
        if _version_key(self.plugin_minimum_version) > _version_key(self.plugin_maximum_version):
            raise ReleaseVerificationError("plugin_range_invalid", "plugin compatibility range is inverted")
        if not _KEY_ID.fullmatch(self.signing_key_id) or not _COMMIT.fullmatch(self.build_commit):
            raise ReleaseVerificationError("release_identity_invalid", "release key/build identity is invalid")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ReleaseVerificationError("release_timestamp_invalid", "release timestamp must be timezone-aware")
        if self.state_schema_version < 1:
            raise ReleaseVerificationError("state_schema_invalid", "state schema version must be positive")
        if not self.files:
            raise ReleaseVerificationError("manifest_files_empty", "runtime manifest cannot be empty")
        paths = [record.path.casefold() for record in self.files]
        if len(set(paths)) != len(paths):
            raise ReleaseVerificationError("manifest_path_collision", "runtime paths collide on Windows")
        if tuple(sorted(self.files, key=lambda record: record.path)) != self.files:
            raise ReleaseVerificationError("manifest_files_unsorted", "runtime files must use canonical path order")
        by_path = {record.path.casefold(): record for record in self.files}
        if not _REQUIRED_EXECUTABLES <= set(by_path):
            raise ReleaseVerificationError("runtime_executable_missing", "Host/Worker/self-test executable is missing")
        for executable in _REQUIRED_EXECUTABLES:
            record = by_path[executable]
            if record.kind != "executable" or not record.authenticode:
                raise ReleaseVerificationError(
                    "runtime_executable_unsigned", "Host/Worker/self-test must require Authenticode"
                )
        required_categories = {"license", "provenance", "sbom", "web"}
        if not required_categories <= {record.kind for record in self.files}:
            raise ReleaseVerificationError(
                "release_metadata_missing", "release requires web, license, SBOM and provenance files"
            )
        if "web/index.html" not in by_path:
            raise ReleaseVerificationError("web_entry_missing", "local Web entry point is missing")
        if self.privilege_envelope is not None:
            catalog = by_path.get("process-catalog.v1.json")
            if (
                catalog is None
                or catalog.kind != "asset"
                or catalog.sha256 != self.privilege_envelope.process_catalog_sha256
            ):
                raise ReleaseVerificationError(
                    "privilege_catalog_identity",
                    "signed Runtime privilege envelope does not bind the canonical process catalog",
                )
        expected_digest = runtime_content_digest(self.files)
        if self.archive.content_digest != expected_digest:
            raise ReleaseVerificationError("archive_digest_mismatch", "archive content digest does not match files")
        total = sum(record.byte_length for record in self.files)
        if total > self.archive.maximum_expanded_bytes:
            raise ReleaseVerificationError("archive_expanded_limit", "manifest exceeds expanded byte limit")
        capabilities = tuple(sorted(set(self.capabilities)))
        if capabilities != self.capabilities or any(not _KEY_ID.fullmatch(item) for item in capabilities):
            raise ReleaseVerificationError("capabilities_invalid", "release capabilities must be sorted and unique")

    @property
    def by_path(self) -> Mapping[str, RuntimeFileRecord]:
        return {record.path: record for record in self.files}

    def supports_plugin(self, plugin_version: str) -> bool:
        if not _VERSION.fullmatch(plugin_version):
            raise ValueError("plugin version is invalid")
        return (
            _version_key(self.plugin_minimum_version)
            <= _version_key(plugin_version)
            <= _version_key(self.plugin_maximum_version)
        )

    def supports_protocol(self, protocol_version: str, schema_hash: str) -> bool:
        value = _protocol_tuple(protocol_version)
        return (
            _protocol_tuple(self.protocol.minimum) <= value <= _protocol_tuple(self.protocol.maximum)
            and schema_hash == self.protocol.schema_hash
        )


@dataclass(frozen=True, slots=True)
class VerifiedRuntimeBundle:
    root: Path
    archive: Path
    manifest_path: Path
    signature_path: Path
    bootstrap_path: Path
    manifest_bytes: bytes
    signature_bytes: bytes
    manifest: RuntimeReleaseManifest


@dataclass(frozen=True, slots=True)
class VerifiedInstalledManifest:
    root: Path
    manifest_bytes: bytes
    signature_bytes: bytes
    manifest_hash: str
    manifest: RuntimeReleaseManifest


class ReleaseKeyring:
    def __init__(self, public_keys: Mapping[str, bytes]) -> None:
        if not public_keys:
            raise ValueError("release keyring cannot be empty")
        parsed: dict[str, Ed25519PublicKey] = {}
        for key_id, raw in public_keys.items():
            if not _KEY_ID.fullmatch(key_id) or len(raw) != 32:
                raise ValueError("invalid Ed25519 release public key")
            parsed[key_id] = Ed25519PublicKey.from_public_bytes(bytes(raw))
        self._keys = parsed

    def verify(self, key_id: str, manifest_bytes: bytes, signature_bytes: bytes) -> None:
        try:
            key = self._keys[key_id]
        except KeyError as error:
            raise ReleaseVerificationError("release_key_unknown", "runtime release signing key is unknown") from error
        signature = decode_signature(signature_bytes)
        try:
            key.verify(signature, manifest_bytes)
        except InvalidSignature as error:
            raise ReleaseVerificationError(
                "release_signature_invalid", "runtime release signature is invalid"
            ) from error


class RuntimeBundleVerifier:
    def __init__(
        self,
        *,
        keyring: ReleaseKeyring,
        authenticode: AuthenticodeChecker,
        require_authenticode: bool = True,
    ) -> None:
        self._keyring = keyring
        self._authenticode = authenticode
        self._require_authenticode = require_authenticode

    def verify_bundle(
        self,
        bundle_root: Path,
        *,
        expected_architecture: str,
        windows_build: int,
        plugin_version: str,
        protocol_version: str,
        schema_hash: str,
    ) -> VerifiedRuntimeBundle:
        root = bundle_root.resolve(strict=True)
        manifest_path = root / "runtime-manifest.json"
        signature_path = root / "runtime-manifest.sig"
        _verify_regular_release_file(manifest_path)
        _verify_regular_release_file(signature_path)
        manifest_bytes = _read_bounded(manifest_path, 8 * 1024 * 1024)
        signature_bytes = _read_bounded(signature_path, 1024)
        manifest = parse_manifest(manifest_bytes)
        self._keyring.verify(manifest.signing_key_id, manifest_bytes, signature_bytes)
        if manifest.privilege_envelope is None:
            raise ReleaseVerificationError(
                "release_privilege_envelope_missing",
                "candidate Runtime does not contain a signed privilege envelope",
            )
        if expected_architecture != manifest.platform.architecture:
            raise ReleaseVerificationError("architecture_mismatch", "runtime architecture does not match Windows")
        if windows_build < manifest.platform.minimum_windows_build:
            raise ReleaseVerificationError("windows_too_old", "Windows build is below the runtime minimum")
        if not manifest.supports_plugin(plugin_version):
            raise ReleaseVerificationError("plugin_runtime_incompatible", "plugin version is outside runtime range")
        if not manifest.supports_protocol(protocol_version, schema_hash):
            raise ReleaseVerificationError("protocol_runtime_incompatible", "protocol/schema identity is incompatible")
        archive = _strict_child(root, manifest.archive.file_name)
        bootstrap_path = _strict_child(root, manifest.bootstrap.path)
        _verify_file_identity(
            bootstrap_path,
            byte_length=manifest.bootstrap.byte_length,
            sha256=manifest.bootstrap.sha256,
        )
        _verify_pe_architecture(bootstrap_path, manifest.platform.architecture)
        if self._require_authenticode and not self._authenticode.verify(bootstrap_path):
            raise ReleaseVerificationError("bootstrap_authenticode_invalid", "bootstrap Authenticode is invalid")
        for dependency in manifest.bootstrap.dependencies:
            dependency_path = _strict_child(root, dependency.path)
            _verify_file_identity(
                dependency_path,
                byte_length=dependency.byte_length,
                sha256=dependency.sha256,
            )
            if dependency.authenticode:
                _verify_pe_architecture(dependency_path, manifest.platform.architecture)
                if self._require_authenticode and not self._authenticode.verify(dependency_path):
                    raise ReleaseVerificationError(
                        "bootstrap_dependency_authenticode_invalid",
                        "bootstrap dependency Authenticode is invalid",
                    )
        if not archive.is_file():
            raise ReleaseVerificationError("archive_missing", "runtime archive is missing")
        _verify_regular_release_file(archive)
        outer_files: set[str] = set()
        for directory, directories, files in os.walk(root, topdown=True, followlinks=False):
            current = Path(directory)
            _reject_reparse(current, root)
            for name in directories:
                _reject_reparse(current / name, root)
            for name in files:
                file_path = current / name
                _verify_regular_release_file(file_path)
                outer_files.add(file_path.relative_to(root).as_posix())
        expected_outer = {
            "runtime-manifest.json",
            "runtime-manifest.sig",
            manifest.archive.file_name,
            manifest.bootstrap.path,
            *(item.path for item in manifest.bootstrap.dependencies),
        }
        if outer_files != expected_outer:
            raise ReleaseVerificationError("bundle_file_set_mismatch", "embedded bundle file set is not exact")
        return VerifiedRuntimeBundle(
            root=root,
            archive=archive,
            manifest_path=manifest_path,
            signature_path=signature_path,
            bootstrap_path=bootstrap_path,
            manifest_bytes=manifest_bytes,
            signature_bytes=signature_bytes,
            manifest=manifest,
        )

    def verify_installed_manifest(
        self,
        root: Path,
        *,
        expected_manifest_hash: str,
        expected_architecture: str,
    ) -> VerifiedInstalledManifest:
        """Re-verify current signed identity and privilege asset before comparison."""

        canonical_root = root.resolve(strict=True)
        manifest_path = canonical_root / "runtime-manifest.json"
        signature_path = canonical_root / "runtime-manifest.sig"
        _verify_regular_release_file(manifest_path)
        _verify_regular_release_file(signature_path)
        manifest_bytes = _read_bounded(manifest_path, 8 * 1024 * 1024)
        signature_bytes = _read_bounded(signature_path, 1024)
        manifest_hash = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
        if manifest_hash != expected_manifest_hash:
            raise ReleaseVerificationError(
                "installed_manifest_pointer_mismatch",
                "current pointer does not bind the installed signed manifest",
            )
        manifest = parse_manifest(manifest_bytes)
        self._keyring.verify(manifest.signing_key_id, manifest_bytes, signature_bytes)
        if manifest.platform.architecture != expected_architecture:
            raise ReleaseVerificationError(
                "installed_architecture_mismatch",
                "installed Runtime architecture does not match native Windows",
            )
        if manifest.runtime_version != canonical_root.name:
            raise ReleaseVerificationError(
                "installed_manifest_version_mismatch",
                "installed Runtime directory does not match its signed version",
            )
        if manifest.privilege_envelope is None:
            catalog = manifest.by_path.get("process-catalog.v1.json")
            if catalog is None:
                raise ReleaseVerificationError(
                    "installed_privilege_catalog_missing",
                    "legacy current Runtime has no signed process catalog identity",
                )
            _verify_file_identity(
                _strict_child(canonical_root, catalog.path),
                byte_length=catalog.byte_length,
                sha256=catalog.sha256,
            )
        else:
            _verify_privilege_catalog(canonical_root, manifest)
        return VerifiedInstalledManifest(
            root=canonical_root,
            manifest_bytes=manifest_bytes,
            signature_bytes=signature_bytes,
            manifest_hash=manifest_hash,
            manifest=manifest,
        )

    def verify_installed_tree(
        self,
        root: Path,
        bundle: VerifiedRuntimeBundle,
    ) -> None:
        canonical_root = root.resolve(strict=True)
        actual_files: set[str] = set()
        for directory, directories, files in os.walk(canonical_root, topdown=True, followlinks=False):
            current = Path(directory)
            _reject_reparse(current, canonical_root)
            for name in directories:
                _reject_reparse(current / name, canonical_root)
            for name in files:
                path = current / name
                _reject_reparse(path, canonical_root)
                relative = path.relative_to(canonical_root).as_posix()
                actual_files.add(relative)
        expected_files = set(bundle.manifest.by_path) | set(_SPECIAL_MANIFEST_FILES)
        if actual_files != expected_files:
            raise ReleaseVerificationError("runtime_file_set_mismatch", "installed runtime file set is not exact")
        if _read_bounded(canonical_root / "runtime-manifest.json", 8 * 1024 * 1024) != bundle.manifest_bytes:
            raise ReleaseVerificationError("inner_manifest_mismatch", "archive manifest differs from signed manifest")
        if _read_bounded(canonical_root / "runtime-manifest.sig", 1024) != bundle.signature_bytes:
            raise ReleaseVerificationError(
                "inner_signature_mismatch", "archive signature differs from detached signature"
            )
        for record in bundle.manifest.files:
            path = _strict_child(canonical_root, record.path)
            _verify_file_identity(path, byte_length=record.byte_length, sha256=record.sha256)
            if record.authenticode:
                _verify_pe_architecture(path, bundle.manifest.platform.architecture)
            if record.authenticode and self._require_authenticode and not self._authenticode.verify(path):
                raise ReleaseVerificationError(
                    "runtime_authenticode_invalid", "runtime executable failed Authenticode verification"
                )
        _verify_privilege_catalog(canonical_root, bundle.manifest)


class SafeRuntimeZipExtractor:
    def __init__(self, *, maximum_files: int = 100_000, maximum_compression_ratio: int = 200) -> None:
        if maximum_files < 1 or maximum_compression_ratio < 1:
            raise ValueError("invalid ZIP extraction limits")
        self._maximum_files = maximum_files
        self._maximum_compression_ratio = maximum_compression_ratio

    def extract(self, bundle: VerifiedRuntimeBundle, destination: Path) -> None:
        if destination.exists():
            raise ReleaseVerificationError("staging_exists", "runtime staging destination already exists")
        destination.mkdir(parents=False)
        root = destination.resolve(strict=True)
        _verify_regular_release_file(bundle.archive)
        expected_files = set(bundle.manifest.by_path) | set(_SPECIAL_MANIFEST_FILES)
        with zipfile.ZipFile(bundle.archive, "r") as archive:
            infos = archive.infolist()
            file_infos = [info for info in infos if not info.is_dir()]
            if len(file_infos) > self._maximum_files:
                raise ReleaseVerificationError("archive_file_limit", "runtime ZIP contains too many files")
            seen: set[str] = set()
            expanded = 0
            for info in infos:
                path = _validate_archive_path(info.filename, allow_directory=info.is_dir())
                folded = path.casefold()
                if folded in seen:
                    raise ReleaseVerificationError("archive_path_collision", "runtime ZIP paths collide on Windows")
                seen.add(folded)
                _validate_zip_type(info)
                if info.flag_bits & 0x1:
                    raise ReleaseVerificationError("archive_encrypted", "encrypted ZIP entries are forbidden")
                if info.file_size < 0 or info.compress_size < 0:
                    raise ReleaseVerificationError("archive_size_invalid", "runtime ZIP entry size is invalid")
                if not info.is_dir():
                    expanded += info.file_size
                    compressed = max(1, info.compress_size)
                    if info.file_size > compressed * self._maximum_compression_ratio:
                        raise ReleaseVerificationError(
                            "archive_ratio_limit", "runtime ZIP compression ratio is excessive"
                        )
            if expanded > bundle.manifest.archive.maximum_expanded_bytes:
                raise ReleaseVerificationError("archive_expanded_limit", "runtime ZIP exceeds expanded byte limit")
            actual_files = {info.filename for info in file_infos}
            if actual_files != expected_files:
                raise ReleaseVerificationError("archive_file_set_mismatch", "runtime ZIP file set is not exact")
            for info in infos:
                relative = PurePosixPath(info.filename.rstrip("/"))
                target = root.joinpath(*relative.parts)
                _ensure_within(root, target)
                if info.is_dir():
                    _mkdir_verified(root, target)
                    continue
                _mkdir_verified(root, target.parent)
                _reject_reparse_chain(root, target.parent)
                try:
                    with archive.open(info, "r") as source, target.open("xb", buffering=0) as output:
                        remaining = info.file_size
                        while remaining:
                            chunk = source.read(min(64 * 1024, remaining))
                            if not chunk:
                                raise ReleaseVerificationError(
                                    "archive_entry_truncated", "runtime ZIP entry ended before declared size"
                                )
                            output.write(chunk)
                            remaining -= len(chunk)
                        if source.read(1):
                            raise ReleaseVerificationError(
                                "archive_entry_overflow", "runtime ZIP entry exceeded declared size"
                            )
                        os.fsync(output.fileno())
                except FileExistsError as error:
                    raise ReleaseVerificationError(
                        "archive_target_exists", "runtime ZIP target already exists"
                    ) from error
                _reject_reparse(target, root)


def parse_manifest(payload: bytes) -> RuntimeReleaseManifest:
    if not payload or len(payload) > 8 * 1024 * 1024:
        raise ReleaseVerificationError("manifest_size_invalid", "runtime manifest size is invalid")
    try:
        text = payload.decode("utf-8", errors="strict")
        raw = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseVerificationError("manifest_malformed", "runtime manifest is malformed") from error
    value = _mapping(raw, "manifest")
    expected = {
        "archive",
        "bootstrap",
        "buildCommit",
        "capabilities",
        "coreVersion",
        "createdAt",
        "files",
        "platform",
        "pluginMaximumVersion",
        "pluginMinimumVersion",
        "protocol",
        "runtimeVersion",
        "schemaVersion",
        "signingKeyId",
        "stateSchemaVersion",
        "toolAbiVersion",
    }
    schema_version = _integer(value.get("schemaVersion"), "schemaVersion")
    if schema_version == 2:
        expected.add("privilegeEnvelope")
    _exact_keys(value, expected, "manifest")
    platform = _mapping(value["platform"], "platform")
    _exact_keys(platform, {"architecture", "minimumWindowsBuild", "os"}, "platform")
    protocol = _mapping(value["protocol"], "protocol")
    _exact_keys(protocol, {"maximum", "minimum", "schemaHash"}, "protocol")
    archive = _mapping(value["archive"], "archive")
    _exact_keys(archive, {"contentDigest", "fileName", "maximumExpandedBytes"}, "archive")
    bootstrap = _mapping(value["bootstrap"], "bootstrap")
    _exact_keys(bootstrap, {"authenticode", "byteLength", "dependencies", "path", "sha256"}, "bootstrap")
    bootstrap_dependencies: list[RuntimeFileRecord] = []
    for item in _sequence(bootstrap["dependencies"], "bootstrap.dependencies"):
        dependency = _mapping(item, "bootstrap.dependency")
        _exact_keys(
            dependency,
            {"authenticode", "byteLength", "kind", "path", "sha256"},
            "bootstrap.dependency",
        )
        bootstrap_dependencies.append(
            RuntimeFileRecord(
                path=_text(dependency["path"], "bootstrap.dependency.path"),
                byte_length=_integer(dependency["byteLength"], "bootstrap.dependency.byteLength"),
                sha256=_text(dependency["sha256"], "bootstrap.dependency.sha256"),
                kind=_text(dependency["kind"], "bootstrap.dependency.kind"),
                authenticode=_boolean(dependency["authenticode"], "bootstrap.dependency.authenticode"),
            )
        )
    files_raw = _sequence(value["files"], "files")
    files: list[RuntimeFileRecord] = []
    for item in files_raw:
        record = _mapping(item, "file")
        _exact_keys(record, {"authenticode", "byteLength", "kind", "path", "sha256"}, "file")
        files.append(
            RuntimeFileRecord(
                path=_text(record["path"], "file.path"),
                byte_length=_integer(record["byteLength"], "file.byteLength"),
                sha256=_text(record["sha256"], "file.sha256"),
                kind=_text(record["kind"], "file.kind"),
                authenticode=_boolean(record["authenticode"], "file.authenticode"),
            )
        )
    capabilities = tuple(_text(item, "capability") for item in _sequence(value["capabilities"], "capabilities"))
    try:
        created_at = datetime.fromisoformat(_text(value["createdAt"], "createdAt").replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseVerificationError("release_timestamp_invalid", "release timestamp is malformed") from error
    privilege_envelope: RuntimePrivilegeEnvelope | None = None
    if schema_version == 2:
        try:
            privilege_envelope = parse_privilege_envelope(value["privilegeEnvelope"])
        except RuntimePrivilegeError as error:
            raise ReleaseVerificationError(error.code, str(error)) from error
    manifest = RuntimeReleaseManifest(
        runtime_version=_text(value["runtimeVersion"], "runtimeVersion"),
        core_version=_text(value["coreVersion"], "coreVersion"),
        plugin_minimum_version=_text(value["pluginMinimumVersion"], "pluginMinimumVersion"),
        plugin_maximum_version=_text(value["pluginMaximumVersion"], "pluginMaximumVersion"),
        signing_key_id=_text(value["signingKeyId"], "signingKeyId"),
        build_commit=_text(value["buildCommit"], "buildCommit"),
        created_at=created_at.astimezone(timezone.utc),
        platform=RuntimePlatform(
            _text(platform["os"], "platform.os"),
            _text(platform["architecture"], "platform.architecture"),
            _integer(platform["minimumWindowsBuild"], "platform.minimumWindowsBuild"),
        ),
        protocol=ProtocolCompatibility(
            _text(protocol["minimum"], "protocol.minimum"),
            _text(protocol["maximum"], "protocol.maximum"),
            _text(protocol["schemaHash"], "protocol.schemaHash"),
        ),
        state_schema_version=_integer(value["stateSchemaVersion"], "stateSchemaVersion"),
        tool_abi_version=_text(value["toolAbiVersion"], "toolAbiVersion"),
        archive=RuntimeArchive(
            _text(archive["fileName"], "archive.fileName"),
            _text(archive["contentDigest"], "archive.contentDigest"),
            _integer(archive["maximumExpandedBytes"], "archive.maximumExpandedBytes"),
        ),
        bootstrap=BootstrapRecord(
            _text(bootstrap["path"], "bootstrap.path"),
            _integer(bootstrap["byteLength"], "bootstrap.byteLength"),
            _text(bootstrap["sha256"], "bootstrap.sha256"),
            _boolean(bootstrap["authenticode"], "bootstrap.authenticode"),
            tuple(bootstrap_dependencies),
        ),
        files=tuple(files),
        capabilities=capabilities,
        privilege_envelope=privilege_envelope,
        schema_version=schema_version,
    )
    if canonical_manifest_bytes(manifest) != payload:
        raise ReleaseVerificationError("manifest_noncanonical", "runtime manifest is not canonical JSON")
    return manifest


def canonical_manifest_bytes(manifest: RuntimeReleaseManifest) -> bytes:
    payload = {
        "archive": {
            "contentDigest": manifest.archive.content_digest,
            "fileName": manifest.archive.file_name,
            "maximumExpandedBytes": manifest.archive.maximum_expanded_bytes,
        },
        "bootstrap": {
            "authenticode": manifest.bootstrap.authenticode,
            "byteLength": manifest.bootstrap.byte_length,
            "dependencies": [
                {
                    "authenticode": record.authenticode,
                    "byteLength": record.byte_length,
                    "kind": record.kind,
                    "path": record.path,
                    "sha256": record.sha256,
                }
                for record in manifest.bootstrap.dependencies
            ],
            "path": manifest.bootstrap.path,
            "sha256": manifest.bootstrap.sha256,
        },
        "buildCommit": manifest.build_commit,
        "capabilities": list(manifest.capabilities),
        "coreVersion": manifest.core_version,
        "createdAt": manifest.created_at.isoformat().replace("+00:00", "Z"),
        "files": [
            {
                "authenticode": record.authenticode,
                "byteLength": record.byte_length,
                "kind": record.kind,
                "path": record.path,
                "sha256": record.sha256,
            }
            for record in manifest.files
        ],
        "platform": {
            "architecture": manifest.platform.architecture,
            "minimumWindowsBuild": manifest.platform.minimum_windows_build,
            "os": manifest.platform.os_name,
        },
        "pluginMaximumVersion": manifest.plugin_maximum_version,
        "pluginMinimumVersion": manifest.plugin_minimum_version,
        "protocol": {
            "maximum": manifest.protocol.maximum,
            "minimum": manifest.protocol.minimum,
            "schemaHash": manifest.protocol.schema_hash,
        },
        "runtimeVersion": manifest.runtime_version,
        "schemaVersion": manifest.schema_version,
        "signingKeyId": manifest.signing_key_id,
        "stateSchemaVersion": manifest.state_schema_version,
        "toolAbiVersion": manifest.tool_abi_version,
    }
    if manifest.privilege_envelope is not None:
        payload["privilegeEnvelope"] = privilege_envelope_payload(manifest.privilege_envelope)
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def encode_signature(signature: bytes) -> bytes:
    if len(signature) != 64:
        raise ValueError("Ed25519 signature must contain 64 bytes")
    return base64.urlsafe_b64encode(signature).rstrip(b"=") + b"\n"


def decode_signature(payload: bytes) -> bytes:
    if len(payload) != 87 or not payload.endswith(b"\n"):
        raise ReleaseVerificationError("signature_encoding_invalid", "release signature encoding is invalid")
    text = payload[:-1].decode("ascii", errors="strict")
    if not _BASE64URL.fullmatch(text):
        raise ReleaseVerificationError("signature_encoding_invalid", "release signature is not canonical base64url")
    decoded = base64.urlsafe_b64decode(text + "==")
    if len(decoded) != 64 or encode_signature(decoded) != payload:
        raise ReleaseVerificationError("signature_encoding_invalid", "release signature length is invalid")
    return decoded


def runtime_content_digest(files: Sequence[RuntimeFileRecord]) -> str:
    payload = [
        {"byteLength": item.byte_length, "path": item.path, "sha256": item.sha256}
        for item in sorted(files, key=lambda value: value.path)
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _verify_privilege_catalog(root: Path, manifest: RuntimeReleaseManifest) -> None:
    envelope = manifest.privilege_envelope
    if envelope is None:
        raise ReleaseVerificationError(
            "release_privilege_envelope_missing",
            "Runtime has no signed privilege envelope",
        )
    record = manifest.by_path.get("process-catalog.v1.json")
    if record is None:
        raise ReleaseVerificationError("privilege_catalog_missing", "Runtime process catalog is missing")
    path = _strict_child(root, record.path)
    _verify_file_identity(path, byte_length=record.byte_length, sha256=record.sha256)
    try:
        rebuilt = build_privilege_envelope_from_process_catalog(_read_bounded(path, 32 * 1024))
    except RuntimePrivilegeError as error:
        raise ReleaseVerificationError(error.code, str(error)) from error
    if privilege_envelope_payload(rebuilt) != privilege_envelope_payload(envelope):
        raise ReleaseVerificationError(
            "privilege_catalog_envelope_mismatch",
            "signed Runtime privilege envelope was not derived from its process catalog",
        )


def _validate_archive_path(value: str, *, allow_directory: bool) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ReleaseVerificationError("archive_path_invalid", "archive path must be canonical POSIX text")
    directory = value.endswith("/")
    if directory != allow_directory:
        raise ReleaseVerificationError("archive_path_kind", "archive path directory marker is invalid")
    raw = value[:-1] if directory else value
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or raw.startswith(("//", "\\")):
        raise ReleaseVerificationError("archive_path_absolute", "absolute/UNC/device archive paths are forbidden")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReleaseVerificationError("archive_path_traversal", "archive traversal/dot segments are forbidden")
    for part in parts:
        if part[-1] in {".", " "} or any(character in _WINDOWS_FORBIDDEN or ord(character) < 32 for character in part):
            raise ReleaseVerificationError("archive_path_windows", "archive path is ambiguous on Windows")
        if part.split(".", 1)[0].upper() in _RESERVED_NAMES:
            raise ReleaseVerificationError("archive_path_device", "archive path contains a reserved device name")
    return value


def _validate_zip_type(info: zipfile.ZipInfo) -> None:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ReleaseVerificationError("archive_special_file", "ZIP symlink/special entries are forbidden")
    dos_attributes = info.external_attr & 0xFFFF
    if dos_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise ReleaseVerificationError("archive_reparse", "ZIP reparse entries are forbidden")


def _verify_file_identity(path: Path, *, byte_length: int, sha256: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise ReleaseVerificationError("runtime_file_missing", "runtime file is missing") from error
    if path.is_symlink() or stat.S_IFMT(info.st_mode) != stat.S_IFREG:
        raise ReleaseVerificationError("runtime_file_type", "runtime file is not a regular file")
    if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT or info.st_nlink != 1:
        raise ReleaseVerificationError("runtime_file_link", "runtime reparse/hard-linked files are forbidden")
    if info.st_size != byte_length:
        raise ReleaseVerificationError("runtime_file_size", "runtime file length differs from manifest")
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    if f"sha256:{digest.hexdigest()}" != sha256:
        raise ReleaseVerificationError("runtime_file_hash", "runtime file hash differs from manifest")


def _verify_regular_release_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise ReleaseVerificationError("release_file_unavailable", "release file is unavailable") from error
    if path.is_symlink() or stat.S_IFMT(info.st_mode) != stat.S_IFREG:
        raise ReleaseVerificationError("release_file_type", "release input is not a regular file")
    if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT or info.st_nlink != 1:
        raise ReleaseVerificationError("release_file_link", "release input cannot be reparse/hard-linked")


def windows_pe_machine_for_architecture(architecture: str) -> int:
    """Return the only accepted COFF machine for a signed Runtime architecture."""

    try:
        return WINDOWS_PE_MACHINE_BY_ARCHITECTURE[architecture]
    except KeyError as error:
        raise ReleaseVerificationError(
            "platform_unsupported",
            "only native Windows x64 and arm64 releases are supported",
        ) from error


def architecture_for_windows_pe_machine(machine: int) -> str:
    """Map a native Windows machine identity while rejecting x86 and unknown machines."""

    for architecture, expected_machine in WINDOWS_PE_MACHINE_BY_ARCHITECTURE.items():
        if machine == expected_machine:
            return architecture
    raise ReleaseVerificationError(
        "platform_unsupported",
        "native Windows machine must be x64 or arm64",
    )


def native_windows_architecture() -> str:
    """Read the native OS machine from IsWow64Process2, not the emulated process machine."""

    if os.name != "nt":
        raise ReleaseVerificationError("platform_unsupported", "native Windows architecture is unavailable")
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        is_wow64_process2 = kernel32.IsWow64Process2
        get_current_process.argtypes = []
        get_current_process.restype = ctypes.c_void_p
        is_wow64_process2.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ushort),
            ctypes.POINTER(ctypes.c_ushort),
        ]
        is_wow64_process2.restype = ctypes.c_int
        process_machine = ctypes.c_ushort()
        native_machine = ctypes.c_ushort()
        if not is_wow64_process2(
            get_current_process(),
            ctypes.byref(process_machine),
            ctypes.byref(native_machine),
        ):
            raise OSError(ctypes.get_last_error(), "IsWow64Process2 failed")
    except (AttributeError, OSError) as error:
        raise ReleaseVerificationError(
            "platform_unsupported",
            "native Windows architecture could not be verified",
        ) from error
    return architecture_for_windows_pe_machine(native_machine.value)


def _verify_pe_architecture(path: Path, architecture: str) -> None:
    """Reject mislabeled or polyglot executables before Authenticode verification."""

    try:
        with path.open("rb", buffering=0) as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                raise ReleaseVerificationError("pe_header_invalid", "runtime executable is not a PE image")
            pe_offset = int.from_bytes(header[60:64], "little")
            if pe_offset < 64 or pe_offset > 64 * 1024 * 1024:
                raise ReleaseVerificationError("pe_header_invalid", "runtime PE header offset is invalid")
            stream.seek(pe_offset)
            coff = stream.read(24)
    except OSError as error:
        raise ReleaseVerificationError("release_file_unavailable", "runtime executable cannot be inspected") from error
    if len(coff) != 24 or coff[:4] != b"PE\0\0":
        raise ReleaseVerificationError("pe_header_invalid", "runtime executable has no valid PE signature")
    if int.from_bytes(coff[4:6], "little") != windows_pe_machine_for_architecture(architecture):
        raise ReleaseVerificationError(
            "pe_architecture_invalid",
            "runtime executable machine does not match the signed Runtime architecture",
        )


def _strict_child(root: Path, relative: str) -> Path:
    _validate_archive_path(relative, allow_directory=False)
    candidate = root.joinpath(*relative.split("/")).resolve(strict=True)
    _ensure_within(root, candidate)
    return candidate


def _ensure_within(root: Path, candidate: Path) -> None:
    try:
        if os.path.commonpath((os.path.normcase(root), os.path.normcase(candidate))) != os.path.normcase(root):
            raise ValueError
    except ValueError as error:
        raise ReleaseVerificationError("archive_path_escape", "runtime archive path escaped staging") from error


def _mkdir_verified(root: Path, directory: Path) -> None:
    _ensure_within(root, directory)
    relative = directory.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        _reject_reparse(current, root)
        if not current.is_dir():
            raise ReleaseVerificationError("archive_parent_type", "runtime archive parent is not a directory")


def _reject_reparse_chain(root: Path, directory: Path) -> None:
    current = root
    for part in directory.relative_to(root).parts:
        current /= part
        _reject_reparse(current, root)


def _reject_reparse(path: Path, root: Path) -> None:
    _ensure_within(root, path)
    try:
        info = path.lstat()
    except OSError as error:
        raise ReleaseVerificationError("runtime_path_unavailable", "runtime path cannot be inspected") from error
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise ReleaseVerificationError("runtime_reparse", "runtime symlink/junction/reparse point is forbidden")


def _read_bounded(path: Path, maximum: int) -> bytes:
    try:
        with path.open("rb", buffering=0) as stream:
            payload = stream.read(maximum + 1)
    except OSError as error:
        raise ReleaseVerificationError("release_file_unavailable", "release file is unavailable") from error
    if not payload or len(payload) > maximum:
        raise ReleaseVerificationError("release_file_size", "release file exceeds its hard limit")
    return payload


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ReleaseVerificationError("manifest_type", f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ReleaseVerificationError("manifest_type", f"{label} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ReleaseVerificationError("manifest_fields", f"{label} fields are not exact")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ReleaseVerificationError("manifest_type", f"{label} must be text")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ReleaseVerificationError("manifest_type", f"{label} must be an integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ReleaseVerificationError("manifest_type", f"{label} must be boolean")
    return value


def _protocol_tuple(value: str) -> tuple[int, int]:
    match = _PROTOCOL_VERSION.fullmatch(value)
    if match is None:
        raise ReleaseVerificationError("protocol_version_invalid", "protocol version is invalid")
    return int(match.group(1)), int(match.group(2))


def _version_key(value: str) -> tuple[tuple[int, object], ...]:
    if not _VERSION.fullmatch(value):
        raise ReleaseVerificationError("version_invalid", "release version is invalid")
    parts = re.split(r"[.+_-]", value)
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold()) for part in parts)


__all__ = [
    "WINDOWS_PE_MACHINE_BY_ARCHITECTURE",
    "AuthenticodeChecker",
    "BootstrapRecord",
    "ProtocolCompatibility",
    "ReleaseKeyring",
    "ReleaseVerificationError",
    "RuntimeArchive",
    "RuntimeBundleVerifier",
    "RuntimeFileRecord",
    "RuntimePlatform",
    "RuntimeReleaseManifest",
    "SafeRuntimeZipExtractor",
    "VerifiedInstalledManifest",
    "VerifiedRuntimeBundle",
    "architecture_for_windows_pe_machine",
    "canonical_manifest_bytes",
    "decode_signature",
    "encode_signature",
    "native_windows_architecture",
    "parse_manifest",
    "runtime_content_digest",
    "windows_pe_machine_for_architecture",
]
