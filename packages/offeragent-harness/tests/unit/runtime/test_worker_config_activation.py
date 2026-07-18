import pytest

from offeragent_harness.config import HarnessConfig, ModelSettings
from offeragent_harness.runtime.config_service import ConfigServiceError, WorkerConfigActivation


def test_worker_activation_exposes_only_the_frozen_codex_proxy_to_runtime_transports() -> None:
    activation = WorkerConfigActivation()

    with pytest.raises(ConfigServiceError, match="not been frozen"):
        activation.codex_proxy_url()

    activation.freeze(
        HarnessConfig(model=ModelSettings(proxy_url="http://127.0.0.1:7896"))
    )

    assert activation.codex_proxy_url() == "http://127.0.0.1:7896"
    with pytest.raises(ConfigServiceError, match="already frozen"):
        activation.freeze(HarnessConfig(model=ModelSettings(proxy_url="http://[::1]:8080")))


def test_pending_proxy_change_cannot_split_catalog_and_inference_routes() -> None:
    activation = WorkerConfigActivation(
        HarnessConfig(model=ModelSettings(proxy_url="http://127.0.0.1:7896"))
    )
    desired = ModelSettings(
        model="gpt-selected",
        account_binding="sha256:" + "a" * 64,
        proxy_url="http://[::1]:8080",
    )

    active = activation.codex_model_transport_settings(desired)

    assert activation.codex_proxy_url() == "http://127.0.0.1:7896"
    assert active.proxy_url == activation.codex_proxy_url()
    assert active.model == desired.model
    assert active.account_binding == desired.account_binding
