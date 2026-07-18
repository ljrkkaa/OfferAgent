"""Narrow compatibility transforms for retired configuration fields."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import ConfigPatch

_RETIRED_UPDATE_KEYS = frozenset({"automatic_check", "automatic_install", "channel"})
_RETIRED_UPDATE_CHANNELS = frozenset({"beta", "disabled", "stable"})
_CURRENT_MODEL_KEYS = frozenset({"account_binding", "model", "proxy_url", "reasoning_effort"})


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


def project_legacy_codex_config(value: object) -> ConfigPatch:
    """Validate and project one provider-aware legacy patch into the Codex-only schema.

    Legacy model identifiers have no live account/catalog proof and never cross
    the compatibility boundary. Provider, wire, endpoint, credential, header,
    tier, and sampling choices are retired as well.
    """

    legacy = ConfigPatch.model_validate(strip_retired_update_fields(value)).payload()
    projected = dict(legacy)
    legacy_model = legacy.get("model")
    if isinstance(legacy_model, Mapping):
        model = {
            key: legacy_model[key] for key in _CURRENT_MODEL_KEYS - {"account_binding", "model"} if key in legacy_model
        }
        if model:
            projected["model"] = model
        else:
            projected.pop("model", None)
    return validate_current_codex_config(projected)


def validate_current_codex_config(value: object) -> ConfigPatch:
    """Validate a current persisted patch without accepting retired decisions."""

    if not isinstance(value, Mapping):
        raise ValueError("configuration payload must be an object")
    model = value.get("model")
    if isinstance(model, Mapping) and not set(model) <= _CURRENT_MODEL_KEYS:
        raise ValueError("configuration contains retired model decision fields")
    if isinstance(model, Mapping):
        has_model = "model" in model
        has_binding = "account_binding" in model
        if has_model != has_binding:
            raise ValueError("model and account binding require an atomic model selection")
        if has_model:
            selected_model = model.get("model")
            account_binding = model.get("account_binding")
            clears_selection = selected_model == "" and account_binding is None
            selects_model = (
                isinstance(selected_model, str) and bool(selected_model) and isinstance(account_binding, str)
            )
            if not clears_selection and not selects_model:
                raise ValueError("model and account binding require an atomic model selection")
    return ConfigPatch.model_validate(value)


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


__all__ = [
    "project_legacy_codex_config",
    "strip_retired_update_fields",
    "validate_current_codex_config",
]
