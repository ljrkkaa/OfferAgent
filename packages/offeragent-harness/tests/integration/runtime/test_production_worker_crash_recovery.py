from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config

BEFORE_CONTENT = b"BEFORE_PAYLOAD\n"
_HELPER = Path(__file__).with_name("production_worker_crash_recovery_worker.py")

pytestmark = pytest.mark.skipif(os.name != "nt", reason="production Worker crash recovery requires Windows")


def _fixture(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    ensure_portable_workspace_config(vault)
    (vault / "agent.md").write_text("# OfferAgent\n\nUse current Vault evidence.\n", encoding="utf-8")
    (vault / "note.md").write_bytes(BEFORE_CONTENT)
    return tmp_path


def _run(root: Path, action: str, stage: str = "none") -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source = Path(__file__).parents[3] / "src"
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source), environment.get("PYTHONPATH", "")) if part
    )
    command = [sys.executable, str(_HELPER), action, str(root)]
    if stage != "none":
        command.append(stage)
    return subprocess.run(
        command,
        cwd=Path(__file__).parents[3],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _payload(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert completed.returncode == 0, completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, "recovery helper produced no result"
    value = json.loads(lines[-1])
    assert isinstance(value, dict)
    return value


def test_real_production_composition_adopts_unknown_plugin_apply_from_exact_journal_without_replay(
    tmp_path: Path,
) -> None:
    root = _fixture(tmp_path)

    recovered = _payload(_run(root, "plugin-recover"))

    assert recovered["readyBeforeShutdown"] is True
    assert recovered["plansScanned"] == 1
    assert recovered["journalState"] == "completed"
    assert recovered["resultStatus"] == "succeeded"
    assert recovered["resultBatchId"] == "batch_production_plugin_recovery"
    assert recovered["pendingToolCallCount"] == 0
    assert recovered["recoveredToolCallCount"] == 1
    assert recovered["pluginJournalUnchanged"] is True
    assert recovered["vaultSentinelUnchanged"] is True
