from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from scripts import update_local_windows_plugin as updater


def test_blocking_process_names_uses_exact_case_insensitive_image_names() -> None:
    assert updater._blocking_process_names(
        (
            "Obsidian.EXE",
            "OfferAgent-Helper.exe",
            "OFFERAGENT-PROCESS-HOST.EXE",
            "offeragent-worker.exe",
            "prefix-offeragent-process-host.exe",
            "offeragent-process-host.exe.backup",
            "unrelated.exe",
        )
    ) == {
        "obsidian.exe",
        "offeragent-process-host.exe",
        "offeragent-worker.exe",
    }


def test_main_blocks_before_build_or_install_when_process_host_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_calls: list[object] = []
    install_calls: list[object] = []
    monkeypatch.setattr(updater, "_running_blocking_processes", lambda: {"offeragent-process-host.exe"})
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: build_calls.append((args, kwargs)))
    monkeypatch.setattr(
        updater,
        "install_local_plugin",
        lambda *args, **kwargs: install_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "update_local_windows_plugin.py",
            "--vault-root",
            str(tmp_path),
            "--ripgrep-executable",
            str(tmp_path / "rg.exe"),
        ],
    )

    with pytest.raises(SystemExit, match=r"offeragent-process-host\.exe"):
        updater.main()

    assert build_calls == []
    assert install_calls == []


@pytest.mark.skipif(os.name != "nt", reason="Toolhelp process enumeration is Windows-only")
def test_toolhelp_detects_a_running_process_host_image(tmp_path: Path) -> None:
    source = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
    executable = tmp_path / "offeragent-process-host.exe"
    shutil.copy2(source, executable)
    process = subprocess.Popen(
        [str(executable), "/d", "/c", "ping 127.0.0.1 -n 6 >nul"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if "offeragent-process-host.exe" in updater._running_blocking_processes():
                break
            time.sleep(0.05)
        else:
            pytest.fail("Toolhelp did not report the running offeragent-process-host.exe image")
    finally:
        process.terminate()
        process.wait(timeout=5)
