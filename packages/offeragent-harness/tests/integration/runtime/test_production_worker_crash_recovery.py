from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.vault import content_hash
from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config

CRASH_EXIT = 73
BEFORE_CONTENT = b"BEFORE_PAYLOAD\n"
AFTER_CONTENT = b"BEFORE_PAYLOAD\nAFTER_PAYLOAD\n"
_HELPER = Path(__file__).with_name("production_worker_crash_recovery_worker.py")

pytestmark = pytest.mark.skipif(os.name != "nt", reason="production Worker crash recovery requires Windows")


def _fixture(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    ensure_portable_workspace_config(vault)
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


def _manifest_files(root: Path) -> tuple[Path, ...]:
    return tuple((root / "state" / "vault-transactions").glob("*.json"))


def _cas_residue(root: Path) -> tuple[Path, ...]:
    return tuple((root / "vault").rglob(".offeragent-*"))


def _journal_rows(root: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(root / "state" / "state.sqlite") as database:
        rows = database.execute(
            """
            SELECT scope, idempotency_key, request_hash, state, started_at, completed_at, result_json
            FROM invocation_journal
            WHERE scope LIKE '%:vault.transaction:1'
            ORDER BY scope, idempotency_key
            """
        ).fetchall()
    return tuple(tuple(row) for row in rows)


def _approved_tool_approval_count(root: Path) -> int:
    with sqlite3.connect(root / "state" / "state.sqlite") as database:
        values = database.execute("SELECT value_json FROM entities WHERE collection = 'approvals'").fetchall()
    return sum('"approved"' in value for (value,) in values)


def _payload(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert completed.returncode == 0, completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, "recovery helper produced no result"
    value = json.loads(lines[-1])
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize(
    ("crash_stage", "manifest_state", "expected_content", "expected_result_status"),
    (
        ("published", "staged", BEFORE_CONTENT, "failed"),
        ("manifest_committed", "committed", AFTER_CONTENT, "succeeded"),
    ),
)
def test_real_production_worker_process_crash_recovers_before_ready_and_is_second_start_noop(
    tmp_path: Path,
    crash_stage: str,
    manifest_state: str,
    expected_content: bytes,
    expected_result_status: str,
) -> None:
    root = _fixture(tmp_path)

    crashed = _run(root, "crash", crash_stage)
    assert crashed.returncode == CRASH_EXIT, crashed.stderr
    assert (root / "vault" / "note.md").read_bytes() == AFTER_CONTENT
    manifests = _manifest_files(root)
    assert len(manifests) == 1
    manifest_bytes = manifests[0].read_bytes()
    assert BEFORE_CONTENT not in manifest_bytes
    assert AFTER_CONTENT not in manifest_bytes
    manifest = json.loads(manifest_bytes)
    assert manifest["state"] == manifest_state
    assert manifest["beforeHash"] == content_hash(BEFORE_CONTENT)
    assert manifest["afterHash"] == content_hash(AFTER_CONTENT)
    crashed_journal = _journal_rows(root)
    assert len(crashed_journal) == 1
    assert crashed_journal[0][3:] == ("started", crashed_journal[0][4], None, None)
    assert _approved_tool_approval_count(root) == 1

    recovered = _payload(_run(root, "recover"))
    assert recovered["readyBeforeShutdown"] is True
    assert recovered["harnessReadyBeforeShutdown"] is True
    observations = recovered["recoveryObservations"]
    assert observations
    assert all(observation["applicationReady"] is False for observation in observations)
    assert all(observation["harnessReady"] is False for observation in observations)
    assert recovered["plansScanned"] == 1
    assert (root / "vault" / "note.md").read_bytes() == expected_content
    assert content_hash((root / "vault" / "note.md").read_bytes()) == content_hash(expected_content)
    assert _manifest_files(root) == ()
    assert _cas_residue(root) == ()
    recovered_journal = _journal_rows(root)
    assert len(recovered_journal) == 1
    assert recovered_journal[0][3] == "completed"
    recovered_result = json.loads(str(recovered_journal[0][6]))
    assert recovered_result["status"] == expected_result_status

    restarted = _payload(_run(root, "recover"))
    assert restarted["readyBeforeShutdown"] is True
    assert restarted["harnessReadyBeforeShutdown"] is True
    assert restarted["recoveryObservations"] == []
    assert restarted["plansScanned"] == 0
    assert (root / "vault" / "note.md").read_bytes() == expected_content
    assert _journal_rows(root) == recovered_journal
    assert _manifest_files(root) == ()
    assert _cas_residue(root) == ()
