from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from offeragent_harness.runtime import development_composition, production_host_composition
from offeragent_harness.runtime.host_supervisor import RestartPolicy


def test_development_host_overrides_only_worker_cold_start_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = tmp_path / "offeragent-host.exe"
    host.write_bytes(b"host")

    class Trust:
        def verify_file(self, candidate: Path) -> bool:
            return candidate == host

        def worker_executable(self) -> object:
            return object()

    captured: dict[str, object] = {}
    application = object()
    monkeypatch.setattr(sys, "executable", str(host))
    monkeypatch.setattr(development_composition, "load_development_trust", Trust)
    monkeypatch.setattr(development_composition, "PinnedWorkerExecutableVerifier", lambda **_: object())
    monkeypatch.setattr(development_composition, "WindowsWorkerProcessBackend", lambda **_: object())
    monkeypatch.setattr(
        development_composition,
        "create_windows_host_application",
        lambda **kwargs: captured.update(kwargs) or application,
    )

    assert development_composition.create_development_host_application() is application
    policy = captured["worker_restart_policy"]
    assert isinstance(policy, RestartPolicy)
    assert policy.startup_timeout_seconds == 120.0
    assert policy.shutdown_timeout_seconds == RestartPolicy().shutdown_timeout_seconds
    assert policy.idle_timeout_seconds == RestartPolicy().idle_timeout_seconds


def test_signed_composition_keeps_shared_host_policy_default() -> None:
    parameter = inspect.signature(production_host_composition.create_windows_host_application).parameters[
        "worker_restart_policy"
    ]
    assert parameter.default is None
