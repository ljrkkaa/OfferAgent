"""Trust adapter for the already activated signed Runtime directory."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from .host_supervisor import VerifiedWorkerExecutable
from .process_supervisor import ExecutableTrust, ProcessExecutableProfile
from .release_manifest import (
    ReleaseKeyring,
    ReleaseVerificationError,
    RuntimeFileRecord,
    parse_manifest,
)

_PROCESS_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def load_embedded_release_keys() -> Mapping[str, bytes]:
    """Load Ed25519 trust roots frozen by the release build."""

    try:
        module = importlib.import_module("offeragent_harness._release_keys")
        encoded = module.PUBLIC_KEYS_BASE64URL
    except (ImportError, AttributeError) as error:
        raise ReleaseVerificationError("release_keys_missing", "signed executable has no embedded keys") from error
    if not isinstance(encoded, dict) or not encoded:
        raise ReleaseVerificationError("release_keys_invalid", "embedded release keyring is invalid")
    result: dict[str, bytes] = {}
    for key_id, value in encoded.items():
        if (
            not isinstance(key_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", key_id)
            or not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None
        ):
            raise ReleaseVerificationError("release_keys_invalid", "embedded release key is invalid")
        raw = base64.urlsafe_b64decode(value + "=")
        if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
            raise ReleaseVerificationError("release_keys_invalid", "embedded release key is non-canonical")
        result[key_id] = raw
    return result


class InstalledReleaseManifestTrust:
    """Pin Worker launches to current.json and its signed per-file identity."""

    def __init__(self, version_directory: Path, *, keyring: ReleaseKeyring) -> None:
        self.version_directory = version_directory.resolve(strict=True)
        manifest_path = self.version_directory / "runtime-manifest.json"
        signature_path = self.version_directory / "runtime-manifest.sig"
        self.manifest_bytes = _read_bounded(manifest_path, 8 * 1024 * 1024)
        signature = _read_bounded(signature_path, 1024)
        self.manifest = parse_manifest(self.manifest_bytes)
        keyring.verify(self.manifest.signing_key_id, self.manifest_bytes, signature)
        self.manifest_hash = f"sha256:{hashlib.sha256(self.manifest_bytes).hexdigest()}"
        self._verify_current_pointer()

    def worker_executable(self) -> VerifiedWorkerExecutable:
        record = self.manifest.by_path.get("offeragent-worker.exe")
        if record is None or not record.authenticode:
            raise ReleaseVerificationError("worker_release_missing", "signed Worker record is missing")
        return VerifiedWorkerExecutable(
            executable=self.version_directory / record.path,
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
            )
        except (OSError, ReleaseVerificationError):
            return False

    def _verify_current_pointer(self) -> None:
        pointer_path = self.version_directory.parent / "current.json"
        payload = _read_bounded(pointer_path, 64 * 1024)
        try:
            value = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ReleaseVerificationError("current_pointer_invalid", "Runtime current pointer is malformed") from error
        if not isinstance(value, dict):
            raise ReleaseVerificationError("current_pointer_invalid", "Runtime current pointer is invalid")
        if (
            value.get("currentVersion") != self.manifest.runtime_version
            or value.get("manifestHash") != self.manifest_hash
        ):
            raise ReleaseVerificationError("current_pointer_mismatch", "executable is not the activated Runtime")
        canonical = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        if canonical != payload:
            raise ReleaseVerificationError("current_pointer_noncanonical", "Runtime current pointer is not canonical")
        actual = os.path.normcase(str(self.version_directory))
        expected = os.path.normcase(str(self.version_directory.parent / self.manifest.runtime_version))
        if actual != expected:
            raise ReleaseVerificationError("runtime_directory_mismatch", "Runtime version directory is inconsistent")


class InstalledReleaseProcessManifestTrust:
    """Authorize a bounded process catalog against one activated release.

    The existing :meth:`InstalledReleaseManifestTrust.authorizes` deliberately
    remains Worker-specific.  Process execution uses this separately named
    adapter so a Worker launch record can never satisfy the process-profile
    trust protocol through accidental duck typing.
    """

    def __init__(
        self,
        release: InstalledReleaseManifestTrust,
        *,
        executable_bindings: Mapping[str, str],
    ) -> None:
        if not executable_bindings:
            raise ReleaseVerificationError(
                "process_catalog_empty",
                "signed process catalog contains no executable bindings",
            )
        bindings: dict[str, tuple[str, RuntimeFileRecord]] = {}
        for executable_id, relative_path in executable_bindings.items():
            if not isinstance(executable_id, str) or _PROCESS_PROFILE_ID.fullmatch(executable_id) is None:
                raise ReleaseVerificationError(
                    "process_catalog_identity_invalid",
                    "signed process catalog executable identity is invalid",
                )
            canonical_relative = _runtime_relative_path(relative_path)
            record = release.manifest.by_path.get(canonical_relative)
            if record is None or record.kind != "executable" or not record.authenticode:
                raise ReleaseVerificationError(
                    "process_catalog_executable_unsigned",
                    "signed process catalog references a missing or unsigned executable",
                )
            _verify_installed_manifest_record(release.version_directory, record)
            bindings[executable_id] = (canonical_relative, record)
        self._release = release
        self._bindings = MappingProxyType(bindings)

    @property
    def version_directory(self) -> Path:
        return self._release.version_directory

    @property
    def manifest_hash(self) -> str:
        return self._release.manifest_hash

    def record_for(self, executable_id: str) -> RuntimeFileRecord:
        try:
            return self._bindings[executable_id][1]
        except KeyError as error:
            raise ReleaseVerificationError(
                "process_catalog_executable_unknown",
                "process executable is absent from the signed process catalog",
            ) from error

    def authorizes(self, profile: ProcessExecutableProfile) -> bool:
        """Return false on every identity, path, hash or network-policy drift."""

        try:
            relative_path, record = self._bindings[profile.executable_id]
            root = self._release.version_directory.resolve(strict=True)
            if profile.trust is not ExecutableTrust.SIGNED_RELEASE or profile.allow_network:
                return False
            if profile.fixed_root.resolve(strict=True) != root:
                return False
            executable = profile.executable.resolve(strict=True)
            expected = _manifest_child(root, relative_path)
            if executable != expected:
                return False
            if profile.file_sha256 != record.sha256 or profile.captured_content_sha256 != record.sha256:
                return False
            _verify_installed_manifest_record(root, record)
            return True
        except (KeyError, OSError, ReleaseVerificationError, ValueError):
            return False


def _read_bounded(path: Path, maximum: int) -> bytes:
    try:
        with path.open("rb", buffering=0) as stream:
            payload = stream.read(maximum + 1)
    except OSError as error:
        raise ReleaseVerificationError("release_file_unavailable", "release file is unavailable") from error
    if not payload or len(payload) > maximum:
        raise ReleaseVerificationError("release_file_size", "release file exceeds limits")
    return payload


def _runtime_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value or "\\" in value:
        raise ReleaseVerificationError(
            "process_catalog_path_invalid",
            "process catalog executable path is invalid",
        )
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ReleaseVerificationError(
            "process_catalog_path_invalid",
            "process catalog executable path must be Runtime-relative",
        )
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ReleaseVerificationError(
            "process_catalog_path_invalid",
            "process catalog executable path contains traversal",
        )
    return value


def _manifest_child(root: Path, relative_path: str) -> Path:
    relative = _runtime_relative_path(relative_path)
    current = root
    for part in relative.split("/"):
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise ReleaseVerificationError(
                "process_catalog_file_unavailable",
                "signed process catalog file is unavailable",
            ) from error
        if current.is_symlink() or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ReleaseVerificationError(
                "process_catalog_reparse",
                "signed process catalog path contains a reparse point",
            )
    try:
        candidate = current.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as error:
        raise ReleaseVerificationError(
            "process_catalog_path_escape",
            "signed process catalog path escaped the activated Runtime",
        ) from error
    return candidate


def _verify_installed_manifest_record(root: Path, record: RuntimeFileRecord) -> None:
    path = _manifest_child(root, record.path)
    try:
        with path.open("rb", buffering=0) as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise ReleaseVerificationError(
            "process_catalog_file_unavailable",
            "signed process catalog file cannot be read",
        ) from error
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if (
        before_identity != after_identity
        or stat.S_IFMT(before.st_mode) != stat.S_IFREG
        or getattr(before, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        or before.st_nlink != 1
        or before.st_size != record.byte_length
        or f"sha256:{digest.hexdigest()}" != record.sha256
    ):
        raise ReleaseVerificationError(
            "process_catalog_file_identity",
            "signed process catalog file identity differs from the release manifest",
        )
    # Close a directory-swap window around the opened-handle verification.  The
    # Windows process backend performs one final opened-handle check immediately
    # before suspended CreateProcessW.
    if _manifest_child(root, record.path) != path:
        raise ReleaseVerificationError(
            "process_catalog_path_changed",
            "signed process catalog path changed during verification",
        )


__all__ = [
    "InstalledReleaseManifestTrust",
    "InstalledReleaseProcessManifestTrust",
    "load_embedded_release_keys",
]
