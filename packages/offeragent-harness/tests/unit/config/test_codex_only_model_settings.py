import pytest

from offeragent_harness.config import ModelProvider, ModelSettings, ModelWireApi
from offeragent_harness.config.migrations import validate_current_codex_config


def test_fresh_model_settings_are_codex_subscription_only_and_unselected() -> None:
    settings = ModelSettings()

    assert settings.provider is ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL
    assert settings.wire_api is ModelWireApi.RESPONSES
    assert settings.model == ""
    assert settings.account_binding is None
    assert settings.credential_handle is None
    assert settings.base_url == ""
    assert settings.proxy_url is None


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
    patch = validate_current_codex_config(
        {"model": {"model": "", "account_binding": None}}
    )

    assert patch.payload()["model"] == {"model": "", "account_binding": None}
