"""Verify the separately sealed driver against one installable Windows artifact."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from offeragent_harness.runtime.development_runtime_manifest import (
    DevelopmentRuntimeError,
    parse_development_manifest,
)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_QUALIFICATION_FILES = {
    "offeragent-qualification-driver.cjs",
    "qualification-manifest.json",
}
_PLUGIN_FILES = {
    "local-development-build.json",
    "main.js",
    "manifest.json",
    "migration",
    "runtime",
    "styles.css",
}


class WindowsProductArtifactError(RuntimeError):
    """The paired product/qualification outputs do not share one trusted identity."""


@dataclass(frozen=True)
class VerifiedWindowsProductArtifacts:
    plugin_root: Path
    qualification_root: Path
    driver: Path
    source_commit: str
    source_tree_sha256: str
    runtime_version: str
    plugin_version: str
    runtime_manifest_sha256: str


def verify_paired_windows_artifacts(
    plugin_root: Path,
    qualification_root: Path,
) -> VerifiedWindowsProductArtifacts:
    """Fail closed unless two clean outputs are sealed to the same build receipt."""

    plugin = _directory(plugin_root, "plugin artifact")
    qualification = _directory(qualification_root, "qualification artifact")
    _require_regular_tree(plugin)
    if {entry.name for entry in plugin.iterdir()} != _PLUGIN_FILES:
        raise WindowsProductArtifactError("plugin artifact file set is not exact")
    _require_regular_tree(qualification)
    actual = {entry.name for entry in qualification.iterdir()}
    if actual != _QUALIFICATION_FILES:
        raise WindowsProductArtifactError("qualification artifact file set is not exact")

    receipt_path = plugin / "local-development-build.json"
    runtime_manifest_path = (
        plugin / "runtime" / "windows-x64" / "local-development" / "development-runtime-manifest.json"
    )
    receipt_bytes, receipt = _canonical_object(receipt_path, "plugin build receipt")
    runtime_manifest_bytes = _regular_bytes(runtime_manifest_path, "Runtime manifest")
    _verify_plugin_artifact(plugin, receipt, runtime_manifest_path, runtime_manifest_bytes)
    manifest_bytes, manifest = _canonical_object(
        qualification / "qualification-manifest.json",
        "qualification manifest",
    )
    del manifest_bytes

    expected_keys = {
        "driver",
        "pluginBuildReceiptSha256",
        "pluginVersion",
        "runtimeManifestSha256",
        "runtimeVersion",
        "schemaVersion",
        "sourceCommit",
        "sourceTreeSha256",
    }
    if set(manifest) != expected_keys or manifest.get("schemaVersion") != 1:
        raise WindowsProductArtifactError("qualification manifest shape is invalid")
    driver_record = manifest.get("driver")
    if not isinstance(driver_record, dict) or set(driver_record) != {"path", "sha256", "size"}:
        raise WindowsProductArtifactError("qualification driver record is invalid")
    if driver_record.get("path") != "offeragent-qualification-driver.cjs":
        raise WindowsProductArtifactError("qualification driver path is invalid")
    driver = qualification / "offeragent-qualification-driver.cjs"
    driver_bytes = _regular_bytes(driver, "qualification driver")
    if driver_record.get("sha256") != _sha256(driver_bytes) or driver_record.get("size") != len(driver_bytes):
        raise WindowsProductArtifactError("qualification driver differs from its manifest")

    source_commit = manifest.get("sourceCommit")
    source_tree_sha256 = manifest.get("sourceTreeSha256")
    runtime_manifest_sha256 = manifest.get("runtimeManifestSha256")
    if not isinstance(source_commit, str) or _COMMIT.fullmatch(source_commit) is None:
        raise WindowsProductArtifactError("qualification source commit is invalid")
    for label, value in (
        ("source tree", source_tree_sha256),
        ("Runtime manifest", runtime_manifest_sha256),
        ("plugin build receipt", manifest.get("pluginBuildReceiptSha256")),
    ):
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise WindowsProductArtifactError(f"qualification {label} hash is invalid")

    if manifest.get("pluginBuildReceiptSha256") != _sha256(receipt_bytes):
        raise WindowsProductArtifactError("qualification manifest names another plugin build receipt")
    if runtime_manifest_sha256 != _sha256(runtime_manifest_bytes):
        raise WindowsProductArtifactError("qualification manifest names another Runtime manifest")
    if receipt.get("manifestSha256") != runtime_manifest_sha256:
        raise WindowsProductArtifactError("plugin receipt names another Runtime manifest")
    for manifest_key, receipt_key in (
        ("sourceTreeSha256", "sourceTreeSha256"),
        ("runtimeVersion", "runtimeVersion"),
        ("pluginVersion", "pluginVersion"),
    ):
        if manifest.get(manifest_key) != receipt.get(receipt_key):
            raise WindowsProductArtifactError(f"qualification {manifest_key} differs from the plugin receipt")
    try:
        runtime_manifest = parse_development_manifest(runtime_manifest_bytes)
    except DevelopmentRuntimeError as error:
        raise WindowsProductArtifactError("Runtime manifest is invalid") from error
    if runtime_manifest.build.commit != source_commit:
        raise WindowsProductArtifactError("qualification source commit differs from the Runtime manifest")

    runtime_version = manifest.get("runtimeVersion")
    plugin_version = manifest.get("pluginVersion")
    if not isinstance(runtime_version, str) or not runtime_version:
        raise WindowsProductArtifactError("qualification Runtime version is invalid")
    if not isinstance(plugin_version, str) or not plugin_version:
        raise WindowsProductArtifactError("qualification plugin version is invalid")
    return VerifiedWindowsProductArtifacts(
        plugin_root=plugin,
        qualification_root=qualification,
        driver=driver,
        source_commit=source_commit,
        source_tree_sha256=str(source_tree_sha256),
        runtime_version=runtime_version,
        plugin_version=plugin_version,
        runtime_manifest_sha256=str(runtime_manifest_sha256),
    )


def _directory(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise WindowsProductArtifactError(f"{label} must be absolute")
    _reject_reparse_chain(candidate)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise WindowsProductArtifactError(f"{label} is unavailable") from error
    if not resolved.is_dir():
        raise WindowsProductArtifactError(f"{label} is not a directory")
    return resolved


def _reject_reparse_chain(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise WindowsProductArtifactError("artifact path component is unavailable") from error
        if current.is_symlink() or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise WindowsProductArtifactError("artifact path contains a reparse point")


def _require_regular_tree(root: Path) -> None:
    for path in root.rglob("*"):
        try:
            info = path.lstat()
        except OSError as error:
            raise WindowsProductArtifactError("qualification artifact entry is unavailable") from error
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise WindowsProductArtifactError("qualification artifact contains a reparse point")
        if path.is_dir():
            continue
        if stat.S_IFMT(info.st_mode) != stat.S_IFREG or info.st_nlink != 1:
            raise WindowsProductArtifactError("qualification artifact contains a special or hard-linked file")


def _verify_plugin_artifact(
    plugin: Path,
    receipt: dict[str, Any],
    runtime_manifest_path: Path,
    runtime_manifest_bytes: bytes,
) -> None:
    expected_receipt_keys = {
        "developmentOnly",
        "manifestSha256",
        "pluginVersion",
        "runtimeVersion",
        "schemaVersion",
        "sourceTreeSha256",
        "targetVaultTemplateSha256",
    }
    if (
        set(receipt) != expected_receipt_keys
        or receipt.get("developmentOnly") is not True
        or receipt.get("schemaVersion") != 1
    ):
        raise WindowsProductArtifactError("plugin build receipt shape is invalid")
    try:
        runtime_manifest = parse_development_manifest(runtime_manifest_bytes)
    except DevelopmentRuntimeError as error:
        raise WindowsProductArtifactError("Runtime manifest is invalid") from error
    runtime_root = runtime_manifest_path.parent
    expected_runtime_files = {
        "development-runtime-manifest.json",
        *(record.path for record in runtime_manifest.files),
    }
    actual_runtime_files = {
        path.relative_to(runtime_root).as_posix() for path in runtime_root.rglob("*") if path.is_file()
    }
    if actual_runtime_files != expected_runtime_files:
        raise WindowsProductArtifactError("plugin Runtime file set differs from its manifest")
    for record in runtime_manifest.files:
        payload = _regular_bytes(runtime_root.joinpath(*record.path.split("/")), "Runtime file")
        if len(payload) != record.byte_length or _sha256(payload) != record.sha256:
            raise WindowsProductArtifactError("plugin Runtime file differs from its manifest")
    if (
        runtime_manifest.runtime_version != receipt.get("runtimeVersion")
        or runtime_manifest.plugin_version != receipt.get("pluginVersion")
        or runtime_manifest.build.source_tree_sha256 != receipt.get("sourceTreeSha256")
    ):
        raise WindowsProductArtifactError("plugin receipt differs from the Runtime identity")
    plugin_manifest = _json_object(plugin / "manifest.json", "Obsidian manifest")
    if plugin_manifest.get("isDesktopOnly") is not True or plugin_manifest.get("version") != receipt.get(
        "pluginVersion"
    ):
        raise WindowsProductArtifactError("Obsidian manifest differs from the plugin receipt")
    bundle = _regular_bytes(plugin / "main.js", "plugin bundle")
    manifest_hash = _sha256(runtime_manifest_bytes)
    if (
        b"OFFERAGENT_LOCAL_DEVELOPMENT_RUNTIME_V1" not in bundle
        or manifest_hash.encode("ascii") not in bundle
        or b"__OFFERAGENT_DEVELOPMENT_MANIFEST_SHA256__" in bundle
    ):
        raise WindowsProductArtifactError("plugin bundle anchor differs from the Runtime manifest")
    _regular_bytes(plugin / "styles.css", "plugin styles")
    migration_root = plugin / "migration"
    migration_files = {
        path.relative_to(migration_root).as_posix() for path in migration_root.rglob("*") if path.is_file()
    }
    if migration_files != {"target-vault/agent.md", "target-vault/obsidian-cli/SKILL.md"}:
        raise WindowsProductArtifactError("plugin migration template file set is not exact")


def _json_object(path: Path, label: str) -> dict[str, Any]:
    payload = _regular_bytes(path, label)
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise WindowsProductArtifactError(f"{label} is malformed") from error
    if not isinstance(value, dict):
        raise WindowsProductArtifactError(f"{label} is not an object")
    return value


def _regular_bytes(path: Path, label: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as error:
        raise WindowsProductArtifactError(f"{label} is unavailable") from error
    if (
        path.is_symlink()
        or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        or stat.S_IFMT(info.st_mode) != stat.S_IFREG
        or info.st_nlink != 1
    ):
        raise WindowsProductArtifactError(f"{label} is not a unique regular file")
    try:
        return path.read_bytes()
    except OSError as error:
        raise WindowsProductArtifactError(f"{label} is unavailable") from error


def _canonical_object(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    payload = _regular_bytes(path, label)
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise WindowsProductArtifactError(f"{label} is malformed") from error
    if not isinstance(value, dict):
        raise WindowsProductArtifactError(f"{label} is not an object")
    canonical = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    if payload != canonical:
        raise WindowsProductArtifactError(f"{label} is noncanonical")
    return payload, value


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
