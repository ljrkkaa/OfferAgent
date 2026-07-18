import pytest

from offeragent_harness.config import ModelSettings
from offeragent_harness.config.migrations import validate_current_codex_config
from offeragent_harness.config.models import ModelPatch, UiPatch, UiSettings


def test_fresh_model_settings_are_codex_subscription_only_and_unselected() -> None:
    settings = ModelSettings()

    assert set(ModelSettings.model_fields) == {
        "account_binding",
        "model",
        "proxy_url",
        "reasoning_effort",
    }
    assert set(ModelPatch.model_fields) == set(ModelSettings.model_fields)
    assert settings.model == ""
    assert settings.account_binding is None
    assert settings.proxy_url is None


def test_current_ui_config_has_no_retired_loopback_control_transport() -> None:
    assert set(UiSettings.model_fields) == {"locale", "show_diagnostics"}
    assert set(UiPatch.model_fields) == set(UiSettings.model_fields)


def test_current_persisted_model_selection_requires_an_account_binding() -> None:
    with pytest.raises(ValueError, match="account binding"):
        validate_current_codex_config({"model": {"model": "gpt-selected"}})

    patch = validate_current_codex_config(
        {
            "model": {
                "model": "gpt-selected",
                "account_binding": "sha256:" + "a" * 64,
            }
        }
    )

    assert patch.payload()["model"]["account_binding"] == "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "model_patch",
    [
        {"model": "gpt-selected"},
        {"account_binding": "sha256:" + "a" * 64},
        {"model": None, "account_binding": None},
        {"model": "", "account_binding": "sha256:" + "a" * 64},
        {"model": "gpt-selected", "account_binding": None},
    ],
)
def test_model_selection_updates_are_atomic(model_patch: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="atomic model selection"):
        validate_current_codex_config({"model": model_patch})


def test_model_selection_can_be_atomically_cleared() -> None:
    patch = validate_current_codex_config({"model": {"model": "", "account_binding": None}})

    assert patch.payload()["model"] == {"model": "", "account_binding": None}
