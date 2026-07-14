"""Hash-pinned manifest for one explicitly local development Runtime.

This is deliberately not a release-signature fallback.  The format carries a
mandatory ``developmentOnly`` marker, is rejected by the release verifier, and
is consumed only by development-specific frozen entry points.  Every consumer
revalidates the exact tree before it uses an executable or a built-in asset.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash

from .host_supervisor import VerifiedWorkerExecutable
from .release_manifest import (
    ProtocolCompatibility,
    ReleaseVerificationError,
    RuntimeFileRecord,
    native_windows_architecture,
)

DEVELOPMENT_MANIFEST_NAME = "development-runtime-manifest.json"
DEVELOPMENT_SIGNING_KEY_ID = "local-development-hash-pin"
REQUIRED_DEVELOPMENT_EXECUTABLES = frozenset(
    {
        "offeragent-host.exe",
        "offeragent-process-host.exe",
        "offeragent-self-test.exe",
        "offeragent-worker.exe",
    }
)

_MAXIMUM_MANIFEST_BYTES = 8 * 1024 * 1024
_MAXIMUM_FILES = 20_000
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class DevelopmentRuntimeError(RuntimeError):
    """Sanitized local-development manifest/identity failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _DuplicateKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DevelopmentBuildIdentity:
    commit: str
    source_tree_sha256: str

    def __post_init__(self) -> None:
        if _COMMIT.fullmatch(self.commit) is None or _SHA256.fullmatch(self.source_tree_sha256) is None:
            raise DevelopmentRuntimeError("development_build_invalid", "development build identity is invalid")


@dataclass(frozen=True, slots=True)
class DevelopmentRuntimeManifest:
    runtime_version: str
    core_version: str
    plugin_version: str
    build: DevelopmentBuildIdentity
    protocol: ProtocolCompatibility
    state_schema_version: int
    tool_abi_version: str
    runtime_content_sha256: str
    files: tuple[RuntimeFileRecord, ...]
    architecture: str = "x64"
    minimum_windows_build: int = 10_240
    development_only: bool = True
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.development_only is not True:
            raise DevelopmentRuntimeError(
                "development_marker_missing",
                "local Runtime must carry the mandatory development-only marker",
            )
        if self.architecture != "x64" or self.minimum_windows_build < 10_240:
            raise DevelopmentRuntimeError(
                "development_platform_invalid",
                "local development Runtime must be native Windows x64",
            )
        if any(
            _VERSION.fullmatch(value) is None
            for value in (self.runtime_version, self.core_version, self.plugin_version, self.tool_abi_version)
        ):
            raise DevelopmentRuntimeError("development_version_invalid", "development version is invalid")
        if self.state_schema_version < 1:
            raise DevelopmentRuntimeError("development_state_schema_invalid", "state schema version is invalid")
        if not self.files or len(self.files) > _MAXIMUM_FILES:
            raise DevelopmentRuntimeError("development_files_invalid", "development file list is outside limits")
        paths = tuple(record.path for record in self.files)
        if paths != tuple(sorted(paths)) or len({path.casefold() for path in paths}) != len(paths):
            raise DevelopmentRuntimeError(
                "development_files_noncanonical",
                "development file records must be sorted and unique on Windows",
            )
        by_path = {record.path: record for record in self.files}
        if not REQUIRED_DEVELOPMENT_EXECUTABLES <= by_path.keys():
            raise DevelopmentRuntimeError(
                "development_executable_missing",
                "Host, Worker, self-test and process-host executables are required",
            )
        if any(
            by_path[path].kind != "executable" or by_path[path].authenticode
            for path in REQUIRED_DEVELOPMENT_EXECUTABLES
        ):
            raise DevelopmentRuntimeError(
                "development_executable_record_invalid",
                "development executables must be explicit unsigned hash-pinned records",
            )
        required_assets = {"process-catalog.v1.json", "web/index.html"}
        if not required_assets <= by_path.keys() or not any(
            record.path.startswith("skills/") and record.path.endswith("/SKILL.md") for record in self.files
        ):
            raise DevelopmentRuntimeError(
                "development_assets_missing",
                "development Runtime is missing process, Web, or Skill assets",
            )
        if not _SHA256.fullmatch(self.runtime_content_sha256):
            raise DevelopmentRuntimeError(
                "development_content_digest_invalid",
                "development Runtime content digest is invalid",
            )
        if development_runtime_content_digest(self.files) != self.runtime_content_sha256:
            raise DevelopmentRuntimeError(
                "development_content_digest_mismatch",
                "development Runtime content digest differs from its file records",
            )

    @property
    def signing_key_id(self) -> str:
        """Compatibility view used by the existing built-in Skill verifier."""

        return DEVELOPMENT_SIGNING_KEY_ID

    @property
    def build_commit(self) -> str:
        return self.build.commit

    @property
    def platform(self) -> object:
        return _DevelopmentPlatform(self.architecture, self.minimum_windows_build)

    @property
    def by_path(self) -> Mapping[str, RuntimeFileRecord]:
        return MappingProxyType({record.path: record for record in self.files})


@dataclass(frozen=True, slots=True)
class _DevelopmentPlatform:
    architecture: str
    minimum_windows_build: int
    os_name: str = "windows"


class InstalledDevelopmentRuntimeTrust:
    """Verify and expose one immutable-by-hash local development Runtime tree."""

    def __init__(self, version_directory: Path) -> None:
        try:
            root = Path(version_directory).resolve(strict=True)
        except OSError as error:
            raise DevelopmentRuntimeError(
                "development_root_unavailable",
                "development Runtime root is absent",
            ) from error
        if not root.is_dir() or _is_reparse(root):
            raise DevelopmentRuntimeError("development_root_invalid", "development Runtime root is unsafe")
        manifest_path = root / DEVELOPMENT_MANIFEST_NAME
        manifest_bytes = _read_bounded(manifest_path, _MAXIMUM_MANIFEST_BYTES)
        self.manifest = parse_development_manifest(manifest_bytes)
        self.manifest_bytes = manifest_bytes
        self.manifest_hash = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
        self.version_directory = root
        self._verify_platform_and_protocol()
        self._verify_exact_tree()

    def worker_executable(self) -> VerifiedWorkerExecutable:
        self._verify_manifest()
        record = self.manifest.by_path["offeragent-worker.exe"]
        executable = _verify_record(self.version_directory, record)
        self._verify_manifest()
        return VerifiedWorkerExecutable(
            executable=executable,
            version_directory=self.version_directory,
            runtime_version=self.manifest.runtime_version,
            file_sha256=record.sha256,
        )

    def authorizes(self, expected: VerifiedWorkerExecutable) -> bool:
        try:
            authorized = self.worker_executable()
            return (
                expected.runtime_version == authorized.runtime_version
                and expected.file_sha256 == authorized.file_sha256
                and expected.version_directory.resolve(strict=True) == self.version_directory
                and expected.executable.resolve(strict=True) == authorized.executable.resolve(strict=True)
                and self.verify_file(expected.executable)
            )
        except (DevelopmentRuntimeError, OSError, ValueError):
            return False

    def verify_file(self, path: Path) -> bool:
        """Re-hash one manifest member through an opened regular-file handle."""

        try:
            self._verify_manifest()
            candidate = Path(path).resolve(strict=True)
            relative = candidate.relative_to(self.version_directory).as_posix()
            record = self.manifest.by_path.get(relative)
            verified = record is not None and _verify_record(self.version_directory, record) == candidate
            self._verify_manifest()
            return verified
        except (DevelopmentRuntimeError, OSError, ValueError):
            return False

    def _verify_platform_and_protocol(self) -> None:
        try:
            architecture = native_windows_architecture()
        except RuntimeError as error:
            raise DevelopmentRuntimeError(
                "development_platform_unsupported",
                "local development Runtime requires native Windows x64",
            ) from error
        if architecture != "x64" or self.manifest.architecture != architecture:
            raise DevelopmentRuntimeError(
                "development_architecture_mismatch",
                "development Runtime architecture differs from this Windows process",
            )
        if (
            self.manifest.protocol.minimum != PROTOCOL_VERSION
            or self.manifest.protocol.maximum != PROTOCOL_VERSION
            or self.manifest.protocol.schema_hash != schema_hash()
        ):
            raise DevelopmentRuntimeError(
                "development_protocol_mismatch",
                "development Runtime protocol/schema differs from the frozen code",
            )

    def _verify_exact_tree(self) -> None:
        self._verify_manifest()
        actual = _regular_tree(self.version_directory)
        expected = {DEVELOPMENT_MANIFEST_NAME, *(record.path for record in self.manifest.files)}
        if set(actual) != expected:
            raise DevelopmentRuntimeError(
                "development_file_set_mismatch",
                "development Runtime file set differs from its canonical manifest",
            )
        for record in self.manifest.files:
            _verify_record(self.version_directory, record)
        self._verify_manifest()

    def _verify_manifest(self) -> None:
        payload = _read_bounded(self.version_directory / DEVELOPMENT_MANIFEST_NAME, _MAXIMUM_MANIFEST_BYTES)
        if payload != self.manifest_bytes:
            raise DevelopmentRuntimeError(
                "development_manifest_changed",
                "development manifest changed after trust establishment",
            )


class DevelopmentManifestHashVerifier:
    """Opened-image verifier injected only by the development Host composition."""

    def __init__(self, trust: InstalledDevelopmentRuntimeTrust) -> None:
        self._trust = trust

    def verify(self, executable: Path) -> bool:
        return self._trust.verify_file(executable)


def development_runtime_content_digest(files: Sequence[RuntimeFileRecord]) -> str:
    records = [_file_record_payload(record) for record in files]
    digest = hashlib.sha256(_canonical_json({"files": records})).hexdigest()
    return f"sha256:{digest}"


def canonical_development_manifest_bytes(manifest: DevelopmentRuntimeManifest) -> bytes:
    value = {
        "build": {
            "commit": manifest.build.commit,
            "sourceTreeSha256": manifest.build.source_tree_sha256,
        },
        "coreVersion": manifest.core_version,
        "developmentOnly": True,
        "files": [_file_record_payload(record) for record in manifest.files],
        "platform": {
            "architecture": manifest.architecture,
            "minimumWindowsBuild": manifest.minimum_windows_build,
            "os": "windows",
        },
        "pluginVersion": manifest.plugin_version,
        "protocol": {
            "maximum": manifest.protocol.maximum,
            "minimum": manifest.protocol.minimum,
            "schemaHash": manifest.protocol.schema_hash,
        },
        "runtimeContentSha256": manifest.runtime_content_sha256,
        "runtimeVersion": manifest.runtime_version,
        "schemaVersion": manifest.schema_version,
        "stateSchemaVersion": manifest.state_schema_version,
        "toolAbiVersion": manifest.tool_abi_version,
    }
    return _canonical_json(value) + b"\n"


def parse_development_manifest(payload: bytes) -> DevelopmentRuntimeManifest:
    if not payload or len(payload) > _MAXIMUM_MANIFEST_BYTES:
        raise DevelopmentRuntimeError("development_manifest_size", "development manifest is outside limits")
    try:
        raw = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, _DuplicateKey) as error:
        raise DevelopmentRuntimeError("development_manifest_malformed", "development manifest is malformed") from error
    value = _mapping(raw, "manifest")
    _exact_keys(
        value,
        {
            "build",
            "coreVersion",
            "developmentOnly",
            "files",
            "platform",
            "pluginVersion",
            "protocol",
            "runtimeContentSha256",
            "runtimeVersion",
            "schemaVersion",
            "stateSchemaVersion",
            "toolAbiVersion",
        },
        "manifest",
    )
    build = _mapping(value["build"], "build")
    _exact_keys(build, {"commit", "sourceTreeSha256"}, "build")
    platform = _mapping(value["platform"], "platform")
    _exact_keys(platform, {"architecture", "minimumWindowsBuild", "os"}, "platform")
    protocol = _mapping(value["protocol"], "protocol")
    _exact_keys(protocol, {"maximum", "minimum", "schemaHash"}, "protocol")
    files: list[RuntimeFileRecord] = []
    for item in _sequence(value["files"], "files"):
        record = _mapping(item, "file")
        _exact_keys(record, {"byteLength", "kind", "path", "sha256"}, "file")
        try:
            files.append(
                RuntimeFileRecord(
                    path=_text(record["path"], "file.path"),
                    byte_length=_integer(record["byteLength"], "file.byteLength"),
                    sha256=_text(record["sha256"], "file.sha256"),
                    kind=_text(record["kind"], "file.kind"),
                    authenticode=False,
                )
            )
        except ReleaseVerificationError as error:
            raise DevelopmentRuntimeError("development_file_invalid", "development file record is invalid") from error
    if platform["os"] != "windows":
        raise DevelopmentRuntimeError("development_platform_invalid", "development Runtime OS is invalid")
    try:
        compatibility = ProtocolCompatibility(
            minimum=_text(protocol["minimum"], "protocol.minimum"),
            maximum=_text(protocol["maximum"], "protocol.maximum"),
            schema_hash=_text(protocol["schemaHash"], "protocol.schemaHash"),
        )
        manifest = DevelopmentRuntimeManifest(
            runtime_version=_text(value["runtimeVersion"], "runtimeVersion"),
            core_version=_text(value["coreVersion"], "coreVersion"),
            plugin_version=_text(value["pluginVersion"], "pluginVersion"),
            build=DevelopmentBuildIdentity(
                _text(build["commit"], "build.commit"),
                _text(build["sourceTreeSha256"], "build.sourceTreeSha256"),
            ),
            protocol=compatibility,
            state_schema_version=_integer(value["stateSchemaVersion"], "stateSchemaVersion"),
            tool_abi_version=_text(value["toolAbiVersion"], "toolAbiVersion"),
            runtime_content_sha256=_text(value["runtimeContentSha256"], "runtimeContentSha256"),
            files=tuple(files),
            architecture=_text(platform["architecture"], "platform.architecture"),
            minimum_windows_build=_integer(platform["minimumWindowsBuild"], "platform.minimumWindowsBuild"),
            development_only=value["developmentOnly"] is True,
            schema_version=_integer(value["schemaVersion"], "schemaVersion"),
        )
    except (ReleaseVerificationError, TypeError, ValueError) as error:
        raise DevelopmentRuntimeError("development_manifest_invalid", "development manifest is invalid") from error
    if canonical_development_manifest_bytes(manifest) != payload:
        raise DevelopmentRuntimeError(
            "development_manifest_noncanonical",
            "development manifest bytes are not canonical",
        )
    return manifest


def _file_record_payload(record: RuntimeFileRecord) -> dict[str, object]:
    return {
        "byteLength": record.byte_length,
        "kind": record.kind,
        "path": record.path,
        "sha256": record.sha256,
    }


def _regular_tree(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    folded_paths: set[str] = set()
    try:
        for item in sorted(root.rglob("*")):
            relative = item.relative_to(root).as_posix()
            info = item.lstat()
            if item.is_symlink() or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise DevelopmentRuntimeError(
                    "development_reparse_rejected",
                    "development Runtime contains a reparse point",
                )
            if stat.S_IFMT(info.st_mode) == stat.S_IFDIR:
                continue
            if stat.S_IFMT(info.st_mode) != stat.S_IFREG or info.st_nlink != 1:
                raise DevelopmentRuntimeError(
                    "development_special_file_rejected",
                    "development Runtime contains a special or hard-linked file",
                )
            folded = relative.casefold()
            if folded in folded_paths:
                raise DevelopmentRuntimeError(
                    "development_path_collision",
                    "development Runtime paths collide on Windows",
                )
            result[relative] = item
            folded_paths.add(folded)
    except OSError as error:
        raise DevelopmentRuntimeError(
            "development_tree_unavailable",
            "development Runtime tree is unavailable",
        ) from error
    return result


def _verify_record(root: Path, record: RuntimeFileRecord) -> Path:
    path = root.joinpath(*record.path.split("/"))
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        if _is_reparse(path):
            raise DevelopmentRuntimeError("development_reparse_rejected", "development file is a reparse point")
        with path.open("rb", buffering=0) as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise DevelopmentRuntimeError("development_file_unavailable", "development file is unavailable") from error
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if (
        before_identity != after_identity
        or stat.S_IFMT(before.st_mode) != stat.S_IFREG
        or before.st_nlink != 1
        or before.st_size != record.byte_length
        or f"sha256:{digest.hexdigest()}" != record.sha256
    ):
        raise DevelopmentRuntimeError(
            "development_file_identity_mismatch",
            "development file differs from its pinned manifest identity",
        )
    if path.resolve(strict=True) != resolved:
        raise DevelopmentRuntimeError("development_path_changed", "development file path changed during verification")
    return resolved


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _read_bounded(path: Path, maximum: int) -> bytes:
    try:
        resolved = path.resolve(strict=True)
        if _is_reparse(path):
            raise DevelopmentRuntimeError(
                "development_manifest_reparse",
                "development manifest is a reparse point",
            )
        with path.open("rb", buffering=0) as stream:
            before = os.fstat(stream.fileno())
            payload = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise DevelopmentRuntimeError(
            "development_manifest_unavailable",
            "development manifest is unavailable",
        ) from error
    if not payload or len(payload) > maximum:
        raise DevelopmentRuntimeError("development_manifest_size", "development manifest is outside limits")
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or stat.S_IFMT(before.st_mode) != stat.S_IFREG
        or before.st_nlink != 1
        or before.st_size != len(payload)
        or path.resolve(strict=True) != resolved
    ):
        raise DevelopmentRuntimeError(
            "development_manifest_identity_mismatch",
            "development manifest identity changed while it was read",
        )
    return payload


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(value)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DevelopmentRuntimeError("development_manifest_type", f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise DevelopmentRuntimeError("development_manifest_type", f"{label} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise DevelopmentRuntimeError("development_manifest_keys", f"{label} keys are invalid")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DevelopmentRuntimeError("development_manifest_type", f"{label} must be text")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise DevelopmentRuntimeError("development_manifest_type", f"{label} must be an integer")
    return value


__all__ = [
    "DEVELOPMENT_MANIFEST_NAME",
    "DevelopmentBuildIdentity",
    "DevelopmentManifestHashVerifier",
    "DevelopmentRuntimeError",
    "DevelopmentRuntimeManifest",
    "InstalledDevelopmentRuntimeTrust",
    "canonical_development_manifest_bytes",
    "development_runtime_content_digest",
    "parse_development_manifest",
]
