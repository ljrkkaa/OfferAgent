from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from scripts.qualify_final_windows_product import (
    FinalWindowsProductQualificationError,
    _executable,
    _named_skips,
    _review_phase,
    qualify_final_windows_product,
)

_BASE = "4cf015ae02c59a8b15420ef99d340f95e5001cc6"
_HEAD = "d" * 40


def _attestation(path: Path, *, axis: str, specs: list[int], head: str = _HEAD) -> Path:
    payload = {
        "axis": axis,
        "findings": [],
        "fixedBase": _BASE,
        "head": head,
        "schemaVersion": 1,
        "specs": specs,
        "status": "passed",
    }
    path.write_bytes((json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode())
    return path.resolve()


def test_one_final_command_composes_every_required_phase_into_a_versioned_report(tmp_path: Path) -> None:
    order: list[str] = []

    def phase(name: str) -> Callable[[], dict[str, object]]:
        def run() -> dict[str, object]:
            order.append(name)
            return {
                "evidence": {"phase": name, "status": "passed"},
                "commands": [
                    {
                        "label": name,
                        "argv": [name],
                        "cwd": str(tmp_path),
                        "exitCode": 0,
                        "durationMs": 1,
                    }
                ],
                "skips": (
                    [{"command": "python tests", "name": "credentialless live matrix", "reason": "opt-in"}]
                    if name == "gates"
                    else []
                ),
            }

        return run

    phases = {
        name: phase(name) for name in ("source", "review", "gates", "build", "offline", "migrations", "install", "live")
    }

    report = qualify_final_windows_product(
        source_root=tmp_path,
        plugin_output=tmp_path / "plugin",
        qualification_output=tmp_path / "qualification",
        ripgrep_executable=tmp_path / "rg.exe",
        node_executable=tmp_path / "node.exe",
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        temporary_parent=tmp_path,
        review_base=_BASE,
        standards_review_attestation=tmp_path / "standards.json",
        spec_review_attestation=tmp_path / "spec.json",
        _phase_functions=phases,
    )

    assert order == ["source", "review", "gates", "build", "offline", "migrations", "install", "live"]
    assert report["schemaVersion"] == 1
    assert report["status"] == "passed"
    phase_report = report["phases"]
    commands = report["commands"]
    assert isinstance(phase_report, dict)
    assert isinstance(commands, list)
    assert set(phase_report) == set(order)
    assert len(commands) == len(order)
    assert report["skips"] == [{"command": "python tests", "name": "credentialless live matrix", "reason": "opt-in"}]
    assert report["review"] == {"phase": "review", "status": "passed"}


def test_review_phase_requires_canonical_zero_finding_attestations_bound_to_exact_head(tmp_path: Path) -> None:
    standards = _attestation(tmp_path / "standards.json", axis="standards", specs=[])
    spec = _attestation(tmp_path / "spec.json", axis="spec", specs=[68, 79])

    phase = _review_phase(
        head=_HEAD,
        review_base=_BASE,
        standards_attestation=standards,
        spec_attestation=spec,
        source_root=tmp_path,
    )

    assert phase["evidence"]["status"] == "passed"
    assert phase["evidence"]["head"] == _HEAD
    assert phase["evidence"]["standardsFindings"] == 0
    assert phase["evidence"]["specFindings"] == 0
    assert phase["evidence"]["attestations"]["standards"]["sha256"].startswith("sha256:")

    _attestation(spec, axis="spec", specs=[68, 79], head="e" * 40)
    with pytest.raises(FinalWindowsProductQualificationError, match="exact final HEAD"):
        _review_phase(
            head=_HEAD,
            review_base=_BASE,
            standards_attestation=standards,
            spec_attestation=spec,
            source_root=tmp_path,
        )


def test_review_attestation_rejects_a_reparse_path_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    standards = _attestation(tmp_path / "standards.json", axis="standards", specs=[])
    spec = _attestation(tmp_path / "spec.json", axis="spec", specs=[68, 79])
    monkeypatch.setattr("scripts.qualify_final_windows_product._path_has_reparse_component", lambda _path: True)

    with pytest.raises(FinalWindowsProductQualificationError, match="reparse point"):
        _review_phase(
            head=_HEAD,
            review_base=_BASE,
            standards_attestation=standards,
            spec_attestation=spec,
            source_root=tmp_path,
        )


def test_named_skips_capture_pytest_and_tap_name_reason_and_owning_command() -> None:
    pytest_output = "SKIPPED [1] tests/acceptance/test_live.py:17: live qualification is opt-in\n"
    tap_output = "ok 42 - installer preserves symlinks # SKIP Windows requires Developer Mode\n"

    assert _named_skips("python tests", pytest_output) == [
        {
            "command": "python tests",
            "name": "tests/acceptance/test_live.py:17",
            "reason": "live qualification is opt-in",
        }
    ]
    assert _named_skips("obsidian tests", tap_output) == [
        {
            "command": "obsidian tests",
            "name": "installer preserves symlinks",
            "reason": "Windows requires Developer Mode",
        }
    ]


def test_final_gate_rejects_a_caller_selected_review_base(tmp_path: Path) -> None:
    def passed() -> dict[str, object]:
        return {"evidence": {"status": "passed"}, "commands": [], "skips": []}

    phases = {
        name: passed for name in ("source", "review", "gates", "build", "offline", "migrations", "install", "live")
    }
    with pytest.raises(FinalWindowsProductQualificationError, match="fixed review base"):
        qualify_final_windows_product(
            source_root=tmp_path,
            plugin_output=tmp_path / "plugin",
            qualification_output=tmp_path / "qualification",
            ripgrep_executable=tmp_path / "rg.exe",
            node_executable=tmp_path / "node.exe",
            proxy_url="http://127.0.0.1:7896",
            model="gpt-5.5",
            temporary_parent=tmp_path,
            review_base=_HEAD,
            standards_review_attestation=tmp_path / "standards.json",
            spec_review_attestation=tmp_path / "spec.json",
            _phase_functions=phases,
        )


def test_gate_executable_prefers_the_current_python_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "python.exe"
    executable = tmp_path / "lint-imports.exe"
    python.write_bytes(b"python")
    executable.write_bytes(b"tool")
    monkeypatch.setattr("scripts.qualify_final_windows_product.sys.executable", str(python))
    monkeypatch.setattr("scripts.qualify_final_windows_product.shutil.which", lambda _name: None)

    assert _executable("lint-imports") == executable
