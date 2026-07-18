from __future__ import annotations

import sys
from pathlib import Path

import pytest

from offeragent_harness.runtime import development_composition, production_worker_composition
from offeragent_harness.runtime.production_worker_composition import WorkerCommandLine, _protocol_capabilities
from offeragent_harness.runtime.startup import RuntimeStartupBlocked, StartupFailurePhase


def test_protocol_capabilities_are_structural_not_workspace_policy() -> None:
    capabilities = _protocol_capabilities()

    assert capabilities.loopback_web is True
    assert capabilities.event_replay is True


def test_production_worker_enables_the_root_agent_contract_gate() -> None:
    source = Path(production_worker_composition.__file__).read_text(encoding="utf-8")

    assert 'required_root_initial_tool="agent_contract.read"' in source


def test_fused_production_worker_has_no_worker_local_vault_evidence_adapter() -> None:
    source = Path(production_worker_composition.__file__).read_text(encoding="utf-8")
    composition = source[source.index("class ProductionWorkerCompositionRoot") :]
    run_components = composition[
        composition.index("components = ProductionRunComponentsFactory(") : composition.index("cancellations =")
    ]

    assert "VaultFileSystem(" not in composition
    assert "WorkspaceInstructionRunPreparationAdapter(" not in composition
    assert "*read_executor.definitions" not in composition
    assert "skills=skill_factory" not in run_components


def test_worker_failure_code_exposes_only_stable_startup_phase() -> None:
    blocked = RuntimeStartupBlocked(
        phase=StartupFailurePhase.SCAN,
        run_id=None,
        applied_results=(),
        cause=KeyError("must-not-leak"),
    )

    assert development_composition._worker_failure_code(blocked) == "startup_scan_failed"
    assert development_composition._worker_failure_code(RuntimeError("must-not-leak")) == "RuntimeError"


def test_worker_main_holds_vault_lock_across_runtime_and_event_loop_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "offeragent-worker.exe"
    executable.write_bytes(b"worker")
    command = WorkerCommandLine(
        workspace_instance_id="wsi_018f0d5e-4b63-7d42-8e5a-010203040506",
        canonical_root_identity="sha256:" + "a" * 64,
        database_identity="sha256:" + "b" * 64,
        runtime_version="1.0.0",
    )
    events: list[str] = []

    class Trust:
        def verify_file(self, candidate: Path) -> bool:
            events.append("verify")
            return candidate == executable

    class Lock:
        def __init__(self, name: str) -> None:
            assert name.startswith("Local\\OfferAgent.Worker.")
            events.append("lock-created")

        def __enter__(self) -> Lock:
            events.append("lock-acquired")
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            events.append("lock-released")

    async def run_worker(actual: WorkerCommandLine, *, development_trust: object) -> None:
        assert actual is command
        assert isinstance(development_trust, Trust)
        events.append("runtime")

    def load_trust() -> Trust:
        events.append("trust")
        return Trust()

    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(development_composition, "parse_worker_arguments", lambda _: command)
    monkeypatch.setattr(development_composition, "load_development_trust", load_trust)
    monkeypatch.setattr(development_composition, "ProcessLock", Lock)
    monkeypatch.setattr(development_composition, "_run_worker", run_worker)

    assert development_composition.worker_main([]) == 0
    assert events == [
        "lock-created",
        "lock-acquired",
        "trust",
        "verify",
        "runtime",
        "lock-released",
    ]


def test_worker_main_releases_vault_lock_when_runtime_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "offeragent-worker.exe"
    executable.write_bytes(b"worker")
    command = WorkerCommandLine(
        workspace_instance_id="wsi_018f0d5e-4b63-7d42-8e5a-010203040506",
        canonical_root_identity="sha256:" + "a" * 64,
        database_identity="sha256:" + "b" * 64,
        runtime_version="1.0.0",
    )
    events: list[str] = []

    class Trust:
        def verify_file(self, candidate: Path) -> bool:
            return candidate == executable

    class Lock:
        def __init__(self, name: str) -> None:
            assert name.startswith("Local\\OfferAgent.Worker.")

        def __enter__(self) -> Lock:
            events.append("lock-acquired")
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            events.append("lock-released")

    async def fail_worker(actual: WorkerCommandLine, *, development_trust: object) -> None:
        events.append("runtime-failed")
        raise RuntimeError("boom")

    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(development_composition, "parse_worker_arguments", lambda _: command)
    monkeypatch.setattr(development_composition, "load_development_trust", Trust)
    monkeypatch.setattr(development_composition, "ProcessLock", Lock)
    monkeypatch.setattr(development_composition, "_run_worker", fail_worker)

    assert development_composition.worker_main([]) == 2
    assert events == ["lock-acquired", "runtime-failed", "lock-released"]
