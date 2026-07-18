"""Narrow compatibility transforms for retired configuration fields."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import ConfigPatch

_RETIRED_UPDATE_KEYS = frozenset({"automatic_check", "automatic_install", "channel"})
_RETIRED_UPDATE_CHANNELS = frozenset({"beta", "disabled", "stable"})
_CURRENT_MODEL_KEYS = frozenset({"account_binding", "model", "proxy_url", "reasoning_effort"})
_RETIRED_MODEL_KEYS = frozenset(
    {
        "allow_remote_https",
        "base_url",
        "credential_handle",
        "organization_id",
        "project_id",
        "provider",
        "service_tier",
        "temperature",
        "wire_api",
    }
)
_RETIRED_UI_KEYS = frozenset({"loopback_web_enabled", "persistent_web_lease"})


@dataclass(frozen=True, slots=True)
class LegacyConfigProjection:
    patch: ConfigPatch
    retired_fields: tuple[str, ...]
    retired_provider_ids: tuple[str, ...]


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

    return project_legacy_codex_config_with_report(value).patch


def project_legacy_codex_config_with_report(value: object) -> LegacyConfigProjection:
    """Project a legacy config and report identities without retaining values."""

    stripped = strip_retired_update_fields(value)
    projected = dict(stripped)
    retired_fields: set[str] = set()
    if isinstance(value, Mapping) and "update" in value:
        retired_fields.add("update")
    original_network = value.get("network") if isinstance(value, Mapping) else None
    if isinstance(original_network, Mapping) and "update_network_enabled" in original_network:
        retired_fields.add("network.update_network_enabled")

    legacy_model = projected.pop("model", None)
    providers: set[str] = set()
    if isinstance(legacy_model, Mapping):
        unknown = set(legacy_model) - _CURRENT_MODEL_KEYS - _RETIRED_MODEL_KEYS
        if unknown:
            raise ValueError("legacy model configuration contains unknown fields")
        _validate_retired_model_fields(legacy_model)
        provider = legacy_model.get("provider")
        if isinstance(provider, str) and provider:
            providers.add(provider)
        retired_fields.update(f"model.{key}" for key in set(legacy_model) & (_RETIRED_MODEL_KEYS | {"model"}))
        model = {
            key: legacy_model[key] for key in _CURRENT_MODEL_KEYS - {"account_binding", "model"} if key in legacy_model
        }
        if model:
            projected["model"] = model
        else:
            projected.pop("model", None)
    elif legacy_model is not None:
        raise ValueError("legacy model configuration must be an object")

    legacy_ui = projected.get("ui")
    if isinstance(legacy_ui, Mapping):
        unknown_retired = set(legacy_ui) & _RETIRED_UI_KEYS
        for key in unknown_retired:
            value = legacy_ui[key]
            if value is not None and type(value) is not bool:
                raise ValueError("retired loopback Web setting is invalid")
        if unknown_retired:
            ui = {key: item for key, item in legacy_ui.items() if key not in _RETIRED_UI_KEYS}
            projected["ui"] = ui
            retired_fields.update(f"ui.{key}" for key in unknown_retired)
    elif legacy_ui is not None:
        raise ValueError("legacy UI configuration must be an object")

    return LegacyConfigProjection(
        validate_current_codex_config(projected),
        tuple(sorted(retired_fields)),
        tuple(sorted(providers)),
    )


def project_previous_codex_config_with_report(value: object) -> LegacyConfigProjection:
    """Migrate the previous Codex-only schema without discarding its bound selection."""

    if not isinstance(value, Mapping):
        raise ValueError("previous configuration payload must be an object")
    projected = dict(value)
    retired_fields: set[str] = set()
    legacy_ui = projected.get("ui")
    if isinstance(legacy_ui, Mapping):
        retired = set(legacy_ui) & _RETIRED_UI_KEYS
        for key in retired:
            item = legacy_ui[key]
            if item is not None and type(item) is not bool:
                raise ValueError("retired loopback Web setting is invalid")
        if retired:
            projected["ui"] = {key: item for key, item in legacy_ui.items() if key not in _RETIRED_UI_KEYS}
            retired_fields.update(f"ui.{key}" for key in retired)
    elif legacy_ui is not None:
        raise ValueError("previous UI configuration must be an object")
    return LegacyConfigProjection(
        validate_current_codex_config(projected),
        tuple(sorted(retired_fields)),
        (),
    )


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


def _validate_retired_model_fields(value: Mapping[str, Any]) -> None:
    string_fields = {
        "base_url",
        "credential_handle",
        "organization_id",
        "project_id",
        "provider",
        "service_tier",
        "wire_api",
    }
    for key in string_fields & set(value):
        item = value[key]
        if item is not None and not isinstance(item, str):
            raise ValueError("retired model string field is invalid")
    if "allow_remote_https" in value:
        item = value["allow_remote_https"]
        if item is not None and type(item) is not bool:
            raise ValueError("retired model network field is invalid")
    if "temperature" in value:
        item = value["temperature"]
        if item is not None and (type(item) not in {int, float} or not 0 <= item <= 2):
            raise ValueError("retired model sampling field is invalid")


__all__ = [
    "LegacyConfigProjection",
    "project_legacy_codex_config",
    "project_legacy_codex_config_with_report",
    "project_previous_codex_config_with_report",
    "strip_retired_update_fields",
    "validate_current_codex_config",
]
