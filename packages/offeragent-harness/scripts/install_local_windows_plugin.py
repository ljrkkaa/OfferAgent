"""Atomically install a verified personal-development plugin without reading data.json."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any

from offeragent_harness.runtime.development_runtime_manifest import InstalledDevelopmentRuntimeTrust

PLUGIN_DIRECTORY_NAME = "offeragent-obsidian-plugin"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_replace = os.replace


class LocalPluginInstallError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="安装已验证的个人本机开发版 OfferAgent 插件")
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    args = parser.parse_args()
    target = install_local_plugin(args.artifact, args.vault_root)
    print(target)
    return 0


def install_local_plugin(artifact: Path, vault_root: Path) -> Path:
    source = _resolved_directory(artifact, "artifact")
    vault = _resolved_directory(vault_root, "Vault root")
    target = vault / ".obsidian" / "plugins" / PLUGIN_DIRECTORY_NAME
    plugin_parent = target.parent
    _ensure_local_directory(vault / ".obsidian")
    _ensure_local_directory(plugin_parent)
    _reject_reparse_chain(plugin_parent)
    _verify_artifact(source)
    try:
        source.relative_to(vault)
    except ValueError:
        pass
    else:
        raise LocalPluginInstallError("build artifact must remain outside the Vault")
    token = uuid.uuid4().hex
    staging = plugin_parent / f".{PLUGIN_DIRECTORY_NAME}.install-{token}"
    backup = plugin_parent / f".{PLUGIN_DIRECTORY_NAME}.backup-{token}"
    if staging.exists() or backup.exists():
        raise LocalPluginInstallError("install staging identity collided")
    activated = False
    backed_up = False
    try:
        shutil.copytree(source, staging)
        _verify_artifact(staging)
        if os.path.lexists(target):
            _reject_reparse_chain(target)
            _replace(target, backup)
            backed_up = True
            settings = backup / "data.json"
            if os.path.lexists(settings):
                _regular_file(settings)
                # Deliberately move the opaque file; never open or decode it.
                _replace(settings, staging / "data.json")
        _replace(staging, target)
        activated = True
        _verify_artifact(target, allow_data_json=True)
        if backed_up:
            _remove_tree_without_settings(backup)
        return target
    except BaseException:
        _rollback_install(target, staging, backup, activated=activated, backed_up=backed_up)
        raise
    finally:
        if os.path.lexists(staging) and not _contains_settings(staging):
            shutil.rmtree(staging, ignore_errors=True)


def _rollback_install(
    target: Path,
    staging: Path,
    backup: Path,
    *,
    activated: bool,
    backed_up: bool,
) -> None:
    failed = target.parent / f".{PLUGIN_DIRECTORY_NAME}.failed-{uuid.uuid4().hex}"
    candidate = target if activated and target.exists() else staging
    errors: list[BaseException] = []
    if activated and os.path.lexists(target):
        try:
            _replace(target, failed)
            candidate = failed
        except BaseException as error:
            errors.append(error)
            candidate = target
    if backed_up and os.path.lexists(backup):
        settings = candidate / "data.json"
        if os.path.lexists(settings) and not os.path.lexists(backup / "data.json"):
            try:
                _regular_file(settings)
                _replace(settings, backup / "data.json")
            except BaseException as error:
                errors.append(error)
        if not os.path.lexists(target):
            try:
                _replace(backup, target)
            except BaseException as error:
                errors.append(error)
        else:
            errors.append(LocalPluginInstallError("rollback target path remained occupied"))
    if os.path.lexists(failed) and not _contains_settings(failed):
        try:
            shutil.rmtree(failed, ignore_errors=True)
        except BaseException as error:
            errors.append(error)
    if errors:
        locations = _settings_locations(target, staging, backup, failed)
        suffix = ", ".join(str(path) for path in locations) if locations else "unknown recovery location"
        raise LocalPluginInstallError(
            f"automatic rollback was incomplete; preserved data.json recovery location(s): {suffix}"
        ) from errors[0]


def _verify_artifact(root: Path, *, allow_data_json: bool = False) -> None:
    _verify_regular_tree(root)
    expected_top_level = {
        "local-development-build.json",
        "main.js",
        "manifest.json",
        "runtime",
        "styles.css",
    }
    settings = root / "data.json"
    if os.path.lexists(settings):
        if not allow_data_json:
            raise LocalPluginInstallError("build artifact must not contain data.json")
        _regular_file(settings)
    actual = {item.name for item in root.iterdir() if item.name != "data.json"}
    if actual != expected_top_level:
        raise LocalPluginInstallError("plugin artifact top-level file set is not exact")
    for name in ("main.js", "manifest.json", "styles.css", "local-development-build.json"):
        _regular_file(root / name)
    receipt = _strict_canonical_json(root / "local-development-build.json")
    if (
        set(receipt)
        != {
            "developmentOnly",
            "manifestSha256",
            "pluginVersion",
            "runtimeVersion",
            "schemaVersion",
            "sourceTreeSha256",
        }
        or receipt.get("developmentOnly") is not True
        or receipt.get("schemaVersion") != 1
    ):
        raise LocalPluginInstallError("local development build receipt is invalid")
    manifest = _strict_canonical_json(root / "manifest.json", allow_pretty=True)
    if manifest.get("isDesktopOnly") is not True or manifest.get("version") != receipt.get("pluginVersion"):
        raise LocalPluginInstallError("Obsidian manifest differs from the local build receipt")
    runtime_root = root / "runtime" / "windows-x64" / "local-development"
    trust = InstalledDevelopmentRuntimeTrust(runtime_root)
    if (
        trust.manifest.runtime_version != receipt.get("runtimeVersion")
        or trust.manifest_hash != receipt.get("manifestSha256")
        or trust.manifest.build.source_tree_sha256 != receipt.get("sourceTreeSha256")
    ):
        raise LocalPluginInstallError("Runtime manifest differs from the local build receipt")
    bundle = (root / "main.js").read_text(
        encoding="utf-8",
        errors="strict",
    )
    if "OFFERAGENT_LOCAL_DEVELOPMENT_RUNTIME_V1" not in bundle:
        raise LocalPluginInstallError("plugin bundle is not the compile-time local development build")
    expected_manifest_hash = receipt.get("manifestSha256")
    if (
        not isinstance(expected_manifest_hash, str)
        or expected_manifest_hash not in bundle
        or "__OFFERAGENT_DEVELOPMENT_MANIFEST_SHA256__" in bundle
    ):
        raise LocalPluginInstallError("plugin bundle does not embed the verified Runtime manifest anchor")


def _strict_canonical_json(path: Path, *, allow_pretty: bool = False) -> dict[str, Any]:
    _regular_file(path)
    payload = path.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise LocalPluginInstallError("plugin JSON artifact is malformed") from error
    if not isinstance(value, dict):
        raise LocalPluginInstallError("plugin JSON artifact must be an object")
    canonical = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if not allow_pretty and payload != canonical:
        raise LocalPluginInstallError("plugin JSON artifact is noncanonical")
    return value


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _resolved_directory(path: Path, label: str) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise LocalPluginInstallError(f"{label} must be absolute")
    _reject_reparse_chain(raw)
    try:
        result = raw.resolve(strict=True)
    except OSError as error:
        raise LocalPluginInstallError(f"{label} is unavailable") from error
    if not result.is_dir():
        raise LocalPluginInstallError(f"{label} is not a directory")
    _reject_reparse(result)
    return result


def _regular_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise LocalPluginInstallError("plugin artifact file is unavailable") from error
    if (
        path.is_symlink()
        or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        or stat.S_IFMT(info.st_mode) != stat.S_IFREG
        or info.st_nlink != 1
    ):
        raise LocalPluginInstallError("plugin artifact contains a non-regular file")


def _reject_reparse(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise LocalPluginInstallError("plugin path is unavailable") from error
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise LocalPluginInstallError("plugin path contains a reparse point")


def _reject_reparse_chain(path: Path) -> None:
    """Reject every existing component before resolving a caller-controlled path."""

    absolute = Path(os.path.abspath(path))
    anchor = Path(absolute.anchor)
    if not anchor.is_absolute():
        raise LocalPluginInstallError("plugin path has no local absolute anchor")
    current = anchor
    _reject_reparse(current)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise LocalPluginInstallError("plugin path component is unavailable") from error
        if (
            current.is_symlink()
            or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            or stat.S_IFMT(info.st_mode) != stat.S_IFDIR
        ):
            raise LocalPluginInstallError("plugin path component is not a local regular directory")


def _ensure_local_directory(path: Path) -> None:
    parent = path.parent
    _reject_reparse_chain(parent)
    try:
        path.mkdir(exist_ok=True)
    except OSError as error:
        raise LocalPluginInstallError("plugin directory could not be created") from error
    _reject_reparse_chain(path)


def _verify_regular_tree(root: Path) -> None:
    """Metadata-only walk; this intentionally never opens an opaque data.json."""

    _reject_reparse_chain(root)
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as error:
            raise LocalPluginInstallError("plugin artifact tree is unavailable") from error
        for entry in entries:
            try:
                # ``DirEntry.stat(follow_symlinks=False).st_nlink`` is 0 for
                # ordinary files on some Windows/Python combinations.  The
                # Path/lstat API returns the real link count used elsewhere in
                # this installer, while still remaining metadata-only.
                entry_path = Path(entry.path)
                info = entry_path.lstat()
            except OSError as error:
                raise LocalPluginInstallError("plugin artifact entry is unavailable") from error
            if entry_path.is_symlink() or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise LocalPluginInstallError("plugin artifact contains a reparse point")
            file_type = stat.S_IFMT(info.st_mode)
            if file_type == stat.S_IFDIR:
                pending.append(entry_path)
            elif file_type != stat.S_IFREG or info.st_nlink != 1:
                raise LocalPluginInstallError("plugin artifact contains a special or hard-linked file")


def _contains_settings(root: Path) -> bool:
    return os.path.lexists(root / "data.json")


def _settings_locations(*roots: Path) -> tuple[Path, ...]:
    return tuple(root / "data.json" for root in roots if _contains_settings(root))


def _remove_tree_without_settings(root: Path) -> None:
    if not os.path.lexists(root):
        return
    if _contains_settings(root):
        raise LocalPluginInstallError(f"refusing to remove recovery tree containing data.json: {root}")
    shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
