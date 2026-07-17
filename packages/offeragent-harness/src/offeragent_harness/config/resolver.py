"""Deterministic field-level configuration layering."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Any

from .models import ConfigLayer, ConfigPatch, ConfigScope, HarnessConfig, RunConfigSnapshot

_SCOPE_ORDER = {
    ConfigScope.MANAGED: 0,
    ConfigScope.USER: 1,
    ConfigScope.WORKSPACE: 2,
    ConfigScope.SESSION: 3,
    ConfigScope.RUN: 4,
}
_MANAGED_FALSE_LOCKS = frozenset(
    {
        "network.model_provider_enabled",
        "memory.memory_enabled",
        "execution.shell_enabled",
        "model.allow_remote_https",
        "policy.workspace_trusted",
        "policy.allow_bypass",
        "execution.subagents_enabled",
        "extensibility.hooks_enabled",
        "ui.loopback_web_enabled",
        "ui.persistent_web_lease",
        "telemetry.enabled",
        "telemetry.include_content",
    }
)
_MANAGED_TRUE_LOCKS = frozenset(
    {
        "policy.read_only",
        "policy.approve_vault_writes",
        "policy.approve_shell",
        "policy.approve_network",
    }
)


def resolve_config(
    layers: Sequence[ConfigLayer],
    *,
    captured_at: datetime,
    run_override: ConfigPatch | None = None,
) -> RunConfigSnapshot:
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("configuration capture time must be timezone-aware")
    ordered = sorted(layers, key=lambda item: (_SCOPE_ORDER[item.scope], item.owner_id))
    if len({(item.scope, item.owner_id) for item in ordered}) != len(ordered):
        raise ValueError("duplicate configuration layer owner")
    base = HarnessConfig().model_dump(mode="json")
    sources = {path: "default" for path in _flatten(base)}
    revisions: dict[str, int] = {}
    managed_locks: dict[str, Any] = {}
    for layer in ordered:
        payload = layer.patch.payload()
        _apply_patch(base, payload, sources, layer.scope.value, managed_locks)
        revisions[f"{layer.scope.value}:{layer.owner_id}"] = layer.revision
        if layer.scope is ConfigScope.MANAGED:
            flattened = _flatten(payload)
            managed_locks.update(
                {
                    path: value
                    for path, value in flattened.items()
                    if (path in _MANAGED_FALSE_LOCKS and value is False)
                    or (path in _MANAGED_TRUE_LOCKS and value is True)
                    or (path == "update.channel" and value == "disabled")
                }
            )
    if run_override is not None:
        _apply_patch(base, run_override.payload(), sources, ConfigScope.RUN.value, managed_locks)
        revisions["run:override"] = 0
    config = HarnessConfig.model_validate(base)
    canonical = json.dumps(
        {"config": config.model_dump(mode="json"), "sources": sources, "revisions": revisions},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return RunConfigSnapshot(
        config=config,
        sources=MappingProxyType(dict(sorted(sources.items()))),
        layer_revisions=MappingProxyType(dict(sorted(revisions.items()))),
        fingerprint=f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        captured_at=captured_at,
    )


def merge_patch(current: ConfigPatch, update: ConfigPatch) -> ConfigPatch:
    payload = current.payload()
    _merge(payload, update.payload())
    return ConfigPatch.model_validate(payload)


def changed_paths(patch: ConfigPatch) -> tuple[str, ...]:
    return tuple(sorted(_flatten(patch.payload())))


def _apply_patch(
    target: dict[str, Any],
    patch: Mapping[str, Any],
    sources: dict[str, str],
    source: str,
    managed_locks: Mapping[str, Any],
) -> None:
    for path, value in _flatten(patch).items():
        if path in managed_locks and value != managed_locks[path]:
            continue
        _set_path(target, path, value)
        sources[path] = source


def _merge(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _flatten(value: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, Mapping):
            flattened.update(_flatten(item, path))
        else:
            flattened[path] = item
    return flattened


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = target
    for part in parts[:-1]:
        nested = current.get(part)
        if not isinstance(nested, dict):
            raise ValueError(f"configuration path {path!r} crosses a scalar")
        current = nested
    current[parts[-1]] = value


__all__ = ["changed_paths", "merge_patch", "resolve_config"]
