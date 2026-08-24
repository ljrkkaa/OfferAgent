from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from offeragent_harness.runtime.process_identity import SupervisedWorkspaceIdentity
from offeragent_harness.runtime.windows_process import WindowsWorkerJob, current_user_profile_directory
from offeragent_harness.runtime.windows_process_supervisor import WindowsSupervisedProcessBackend
from offeragent_harness.runtime.windows_security import (
    current_windows_identity,
    kernel_handle_is_inheritable,
    kernel_object_security_sddl,
)


def workspace() -> SupervisedWorkspaceIdentity:
    return SupervisedWorkspaceIdentity(
        "wsi_12345678-1234-4234-8234-123456789abc",
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    )


class _RuntimeEnvironmentKernel32:
    def GetWindowsDirectoryW(self, buffer: object, _size: int) -> int:
        buffer.value = r"C:\Windows"  # type: ignore[attr-defined]
        return len(buffer.value)  # type: ignore[attr-defined]


def test_current_user_supervisor_supplies_fixed_windows_runtime_environment() -> None:
    backend = object.__new__(WindowsSupervisedProcessBackend)
    object.__setattr__(backend, "_kernel32", _RuntimeEnvironmentKernel32())

    environment = backend._current_user_environment(
        {
            "systemroot": r"Z:\poisoned-windows",
            "WiNdIr": r"Z:\poisoned-windows",
            "temp": r"Z:\poisoned-temp",
            "TMP": r"Z:\poisoned-temp",
            "LANG": "zh_CN.UTF-8",
        },
        temporary_directory=Path(r"C:\scratch\run"),
    )

    assert environment == {
        "LANG": "zh_CN.UTF-8",
        "SystemRoot": r"C:\Windows",
        "TEMP": r"C:\scratch\run",
        "TMP": r"C:\scratch\run",
        "WINDIR": r"C:\Windows",
    }


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Known Folder APIs")
def test_current_user_profile_ignores_poisoned_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERPROFILE", r"Z:\attacker")

    profile = current_user_profile_directory()

    assert profile.is_absolute()
    assert profile.is_dir()
    assert profile != Path(r"Z:\attacker")


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
@pytest.mark.asyncio
async def test_job_object_is_current_user_only_kill_on_close_without_breakaway() -> None:
    job = WindowsWorkerJob(workspace())
    try:
        flags = job.configured_limit_flags
        assert flags & 0x00002000
        assert flags & 0x00000400
        assert flags & (0x00000800 | 0x00001000) == 0
        sddl = kernel_object_security_sddl(job.native_job_handle)
        trustees = re.findall(r"\([^)]*;;;([^)]+)\)", sddl)
        assert trustees == [current_windows_identity().sid]
        assert all(alias not in trustees for alias in ("SY", "BA", "WD", "AU"))
        assert not kernel_handle_is_inheritable(job.native_job_handle)
        assert await job.wait_empty(1)
    finally:
        job.close()
