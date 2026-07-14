from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

import pytest

from offeragent_harness.runtime.process_lock import (
    MutexWaitResult,
    ProcessAlreadyRunning,
    ProcessLock,
    ProcessLockError,
    WindowsMutexBackend,
    host_mutex_name,
    installation_ledger_mutex_name,
    worker_mutex_name,
)
from offeragent_harness.runtime.windows_security import (
    current_user_security_attributes,
    current_windows_identity,
    kernel_handle_is_inheritable,
    kernel_object_security_sddl,
)


@dataclass
class FakeMutexBackend:
    wait_result: MutexWaitResult = MutexWaitResult.ACQUIRED
    next_handle: int = 42
    created_names: list[str] = field(default_factory=list)
    released: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)

    def create(self, name: str) -> int:
        self.created_names.append(name)
        return self.next_handle

    def wait(self, handle: int, timeout_ms: int) -> MutexWaitResult:
        assert handle == self.next_handle
        assert timeout_ms >= 0
        return self.wait_result

    def release(self, handle: int) -> None:
        self.released.append(handle)

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def test_process_lock_releases_and_closes_owned_handle() -> None:
    backend = FakeMutexBackend()
    lock = ProcessLock(host_mutex_name(sid="S-1-5-21-100-200-300-1001"), backend=backend)

    with lock:
        assert lock.acquired

    assert not lock.acquired
    assert backend.released == [42]
    assert backend.closed == [42]


def test_timeout_reports_existing_owner_and_only_closes_handle() -> None:
    backend = FakeMutexBackend(wait_result=MutexWaitResult.TIMEOUT)
    lock = ProcessLock(host_mutex_name(sid="S-1-5-21-100-200-300-1001"), backend=backend)

    with pytest.raises(ProcessAlreadyRunning):
        lock.acquire()

    assert backend.released == []
    assert backend.closed == [42]


def test_abandoned_mutex_is_acquired_for_crash_recovery() -> None:
    backend = FakeMutexBackend(wait_result=MutexWaitResult.ABANDONED)
    lock = ProcessLock(host_mutex_name(sid="S-1-5-21-100-200-300-1001"), backend=backend)

    assert lock.acquire() is MutexWaitResult.ABANDONED
    lock.release()
    assert backend.released == [42]


def test_lock_instance_cannot_be_acquired_twice() -> None:
    backend = FakeMutexBackend()
    lock = ProcessLock(host_mutex_name(sid="S-1-5-21-100-200-300-1001"), backend=backend)
    lock.acquire()
    try:
        with pytest.raises(ProcessLockError):
            lock.acquire()
    finally:
        lock.release()


def test_mutex_names_are_current_user_and_workspace_scoped() -> None:
    sid = "S-1-5-21-100-200-300-1001"
    first = worker_mutex_name("sha256:" + "a" * 64, sid=sid)
    second = worker_mutex_name("sha256:" + "b" * 64, sid=sid)

    assert first.startswith("Local\\OfferAgent.Worker.")
    assert first != second
    assert sid not in first
    assert host_mutex_name(sid=sid) == host_mutex_name(sid=sid)
    assert installation_ledger_mutex_name(sid=sid).startswith("Local\\OfferAgent.InstallationLedger.")
    assert installation_ledger_mutex_name(sid=sid) != host_mutex_name(sid=sid)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows token APIs")
def test_current_identity_and_security_descriptor_are_available() -> None:
    identity = current_windows_identity()
    assert identity.sid.startswith("S-")
    with current_user_security_attributes() as attributes:
        assert attributes.length > 0
        assert attributes.security_descriptor
        assert not attributes.inherit_handle


@pytest.mark.skipif(os.name != "nt", reason="requires Windows named mutexes")
def test_named_mutex_excludes_a_second_process() -> None:
    name = host_mutex_name()
    script = """
import sys
from offeragent_harness.runtime.process_lock import ProcessAlreadyRunning, ProcessLock
try:
    lock = ProcessLock(sys.argv[1])
    lock.acquire()
except ProcessAlreadyRunning:
    raise SystemExit(17)
else:
    lock.release()
    raise SystemExit(0)
"""
    with ProcessLock(name):
        completed = subprocess.run(
            [sys.executable, "-c", script, name],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert completed.returncode == 17, completed.stderr


@pytest.mark.skipif(os.name != "nt", reason="requires Windows named mutex security")
def test_named_mutex_dacl_contains_only_current_sid_and_handle_is_not_inheritable() -> None:
    backend = WindowsMutexBackend()
    handle = backend.create(host_mutex_name())
    acquired = False
    try:
        assert backend.wait(handle, 0) is MutexWaitResult.ACQUIRED
        acquired = True
        sddl = kernel_object_security_sddl(handle)
        trustees = re.findall(r"\([^)]*;;;([^)]+)\)", sddl)
        assert trustees == [current_windows_identity().sid]
        assert all(alias not in trustees for alias in ("SY", "BA", "WD", "AU"))
        assert not kernel_handle_is_inheritable(handle)
    finally:
        if acquired:
            backend.release(handle)
        backend.close(handle)
