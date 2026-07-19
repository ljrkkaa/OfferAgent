from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from scripts.qualify_final_windows_product import qualify_final_windows_product


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
                "skips": ["credentialless live matrix is named"] if name == "gates" else [],
            }

        return run

    phases = {name: phase(name) for name in ("source", "gates", "build", "offline", "migrations", "install", "live")}

    report = qualify_final_windows_product(
        source_root=tmp_path,
        plugin_output=tmp_path / "plugin",
        qualification_output=tmp_path / "qualification",
        ripgrep_executable=tmp_path / "rg.exe",
        node_executable=tmp_path / "node.exe",
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        temporary_parent=tmp_path,
        review_base="4cf015ae02c59a8b15420ef99d340f95e5001cc6",
        _phase_functions=phases,
    )

    assert order == ["source", "gates", "build", "offline", "migrations", "install", "live"]
    assert report["schemaVersion"] == 1
    assert report["status"] == "passed"
    phase_report = report["phases"]
    commands = report["commands"]
    assert isinstance(phase_report, dict)
    assert isinstance(commands, list)
    assert set(phase_report) == set(order)
    assert len(commands) == len(order)
    assert report["skips"] == ["credentialless live matrix is named"]
    assert report["review"] == {
        "fixedBase": "4cf015ae02c59a8b15420ef99d340f95e5001cc6",
        "specs": [68, 79],
        "standardsFindings": 0,
        "specFindings": 0,
        "status": "passed",
    }
