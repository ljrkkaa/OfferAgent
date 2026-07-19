from __future__ import annotations

import sys
from pathlib import Path

import pytest

from offeragent_harness.qualification.windows_product_driver import (
    QualificationDriverClient,
    QualificationDriverError,
    QualificationDriverEventTimeout,
)


def _fake_driver(path: Path) -> Path:
    driver = path / "fake_driver.py"
    driver.write_text(
        """
import json
import os
import sys

for raw in sys.stdin:
    request = json.loads(raw)
    if request["command"] == "emit":
        print(json.dumps({"event": "runtime.event", "value": {"type": "turn.completed"}}), flush=True)
        print(json.dumps({"id": request["id"], "ok": True, "result": {"accepted": True}}), flush=True)
    elif request["command"] == "fail":
        print(json.dumps({"id": request["id"], "ok": False, "error": "expected failure"}), flush=True)
    elif request["command"] == "environment":
        print(json.dumps({"id": request["id"], "ok": True, "result": {"value": os.environ.get("OA_TEST")}}), flush=True)
    elif request["command"] == "stop":
        print(json.dumps({"id": request["id"], "ok": True, "result": {"stopped": True}}), flush=True)
        break
""".lstrip(),
        encoding="utf-8",
    )
    return driver


def test_driver_client_separates_product_events_from_command_responses(tmp_path: Path) -> None:
    driver = _fake_driver(tmp_path)
    guard = tmp_path / "source"
    guard.mkdir()

    with QualificationDriverClient(
        executable=Path(sys.executable),
        driver=driver,
        working_directory=tmp_path,
        source_root_guard=guard,
    ) as client:
        assert client.process_id > 0
        assert client.request("emit", {}) == {"accepted": True}
        assert client.next_event(timeout=1) == {
            "event": "runtime.event",
            "value": {"type": "turn.completed"},
        }

    assert client.return_code == 0


def test_driver_client_surfaces_structured_driver_failures(tmp_path: Path) -> None:
    driver = _fake_driver(tmp_path)
    guard = tmp_path / "source"
    guard.mkdir()
    with QualificationDriverClient(
        executable=Path(sys.executable),
        driver=driver,
        working_directory=tmp_path,
        source_root_guard=guard,
    ) as client:
        with pytest.raises(QualificationDriverError, match="expected failure"):
            client.request("fail", {})


def test_driver_client_applies_bounded_environment_overrides(tmp_path: Path) -> None:
    driver = _fake_driver(tmp_path)
    guard = tmp_path / "source"
    guard.mkdir()
    with QualificationDriverClient(
        executable=Path(sys.executable),
        driver=driver,
        working_directory=tmp_path,
        source_root_guard=guard,
        environment_overrides={"OA_TEST": "offline"},
    ) as client:
        assert client.request("environment", {}) == {"value": "offline"}


def test_driver_client_names_a_bounded_event_poll_timeout(tmp_path: Path) -> None:
    driver = _fake_driver(tmp_path)
    guard = tmp_path / "source"
    guard.mkdir()
    with QualificationDriverClient(
        executable=Path(sys.executable),
        driver=driver,
        working_directory=tmp_path,
        source_root_guard=guard,
    ) as client:
        with pytest.raises(QualificationDriverEventTimeout):
            client.next_event(timeout=0.01)
