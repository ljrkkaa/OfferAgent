"""Narrow compatibility transforms for retired configuration fields."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_RETIRED_UPDATE_KEYS = frozenset({"automatic_check", "automatic_install", "channel"})
_RETIRED_UPDATE_CHANNELS = frozenset({"beta", "disabled", "stable"})


def strip_retired_update_fields(value: object) -> dict[str, Any]:
    """Copy one legacy patch while removing only the retired update settings.

    The removed fields are validated against their former patch shape before
    being discarded.  This keeps the compatibility path fail-closed: an
    unknown member cannot hide inside the retired ``update`` object, and all
    non-retired members remain for ``ConfigPatch`` to validate strictly.
    """

    if not isinstance(value, Mapping):
        raise ValueError("legacy configuration payload must be an object")
    migrated = dict(value)
    if "update" in migrated:
        _validate_retired_update_patch(migrated.pop("update"))

    network = migrated.get("network")
    if isinstance(network, Mapping) and "update_network_enabled" in network:
        migrated_network = dict(network)
        retired = migrated_network.pop("update_network_enabled")
        if retired is not None and type(retired) is not bool:
            raise ValueError("retired update network flag is invalid")
        migrated["network"] = migrated_network
    return migrated


def _validate_retired_update_patch(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or not set(value) <= _RETIRED_UPDATE_KEYS:
        raise ValueError("retired update patch is invalid")
    channel = value.get("channel")
    if channel is not None and channel not in _RETIRED_UPDATE_CHANNELS:
        raise ValueError("retired update channel is invalid")
    for key in ("automatic_check", "automatic_install"):
        setting = value.get(key)
        if setting is not None and type(setting) is not bool:
            raise ValueError(f"retired update setting {key!r} is invalid")


__all__ = ["strip_retired_update_fields"]
