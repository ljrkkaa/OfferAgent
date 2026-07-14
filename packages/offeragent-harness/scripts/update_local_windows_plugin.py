"""One-command build and atomic update for the owner's local Obsidian plugin."""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from ctypes import wintypes
from pathlib import Path

try:
    from scripts.install_local_windows_plugin import install_local_plugin
except ModuleNotFoundError:
    from install_local_windows_plugin import install_local_plugin  # type: ignore[import-not-found,no-redef]

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_local_windows_plugin.py"
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_BLOCKING_PROCESS_NAMES = frozenset(
    {
        "obsidian.exe",
        "offeragent-host.exe",
        "offeragent-process-host.exe",
        "offeragent-worker.exe",
    }
)


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("usage", wintypes.DWORD),
        ("process_id", wintypes.DWORD),
        ("default_heap", ctypes.POINTER(ctypes.c_ulong)),
        ("module_id", wintypes.DWORD),
        ("threads", wintypes.DWORD),
        ("parent_process_id", wintypes.DWORD),
        ("priority_base", ctypes.c_long),
        ("flags", wintypes.DWORD),
        ("executable", wintypes.WCHAR * 260),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="构建并更新个人本机 OfferAgent Obsidian 插件")
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--ripgrep-executable", type=Path, required=True)
    args = parser.parse_args()
    running = _running_blocking_processes()
    if running:
        names = ", ".join(sorted(running))
        raise SystemExit(f"请先在插件中停止 Runtime 并退出 Obsidian; 仍在运行: {names}")
    with tempfile.TemporaryDirectory(prefix="offeragent-local-plugin-update-") as temporary:
        artifact = Path(temporary) / "offeragent-obsidian-plugin"
        subprocess.run(
            [
                sys.executable,
                str(BUILD_SCRIPT),
                "--output",
                str(artifact),
                "--ripgrep-executable",
                str(args.ripgrep_executable),
            ],
            cwd=ROOT,
            check=True,
        )
        installed = install_local_plugin(artifact, args.vault_root)
    print(installed)
    return 0


def _running_blocking_processes() -> set[str]:
    if os.name != "nt":
        raise SystemExit("personal local plugin update requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    raw_handle = int(handle) if handle else 0
    if raw_handle in {0, _INVALID_HANDLE_VALUE}:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    process_names: list[str] = []
    try:
        entry = _ProcessEntry32W()
        entry.size = ctypes.sizeof(_ProcessEntry32W)
        present = bool(kernel32.Process32FirstW(wintypes.HANDLE(raw_handle), ctypes.byref(entry)))
        while present:
            process_names.append(str(entry.executable))
            present = bool(kernel32.Process32NextW(wintypes.HANDLE(raw_handle), ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(raw_handle))
    return _blocking_process_names(process_names)


def _blocking_process_names(process_names: Iterable[str]) -> set[str]:
    normalized_names = (name.casefold() for name in process_names)
    return {name for name in normalized_names if name in _BLOCKING_PROCESS_NAMES}


if __name__ == "__main__":
    raise SystemExit(main())
