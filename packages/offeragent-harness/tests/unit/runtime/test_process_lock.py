from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from offeragent_harness.runtime.process_lock import (
    MutexWaitResult,
    ProcessAlreadyRunning,
    ProcessLock,
    ProcessLockError,
    WindowsMutexBackend,
    worker_mutex_name,
)
from offeragent_harness.runtime.windows_security import (
    current_user_security_attributes,
    current_windows_identity,
    kernel_handle_is_inheritable,
    kernel_object_security_sddl,
)

_NAME = "Local\\OfferAgent.SecretStore.test"
_ROOT_IDENTITY_A = "sha256:" + "a" * 64
_ROOT_IDENTITY_B = "sha256:" + "b" * 64
_TEST_SID = "S-1-5-21-100-200-300-1001"


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
    lock = ProcessLock(_NAME, backend=backend)

    with lock:
        assert lock.acquired

    assert not lock.acquired
    assert backend.released == [42]
    assert backend.closed == [42]


def test_timeout_reports_existing_owner_and_only_closes_handle() -> None:
    backend = FakeMutexBackend(wait_result=MutexWaitResult.TIMEOUT)
    lock = ProcessLock(_NAME, backend=backend)

    with pytest.raises(ProcessAlreadyRunning):
        lock.acquire()

    assert backend.released == []
    assert backend.closed == [42]


def test_abandoned_mutex_is_acquired_for_crash_recovery() -> None:
    backend = FakeMutexBackend(wait_result=MutexWaitResult.ABANDONED)
    lock = ProcessLock(worker_mutex_name(_ROOT_IDENTITY_A, sid=_TEST_SID), backend=backend)

    assert lock.acquire() is MutexWaitResult.ABANDONED
    lock.release()
    assert backend.released == [42]


def test_lock_instance_cannot_be_acquired_twice() -> None:
    backend = FakeMutexBackend()
    lock = ProcessLock(_NAME, backend=backend)
    lock.acquire()
    try:
        with pytest.raises(ProcessLockError):
            lock.acquire()
    finally:
        lock.release()


def test_worker_mutex_names_are_current_user_and_vault_scoped() -> None:
    first = worker_mutex_name(_ROOT_IDENTITY_A, sid=_TEST_SID)

    assert first == worker_mutex_name(_ROOT_IDENTITY_A, sid=_TEST_SID)
    assert first != worker_mutex_name(_ROOT_IDENTITY_B, sid=_TEST_SID)
    assert first.startswith("Local\\OfferAgent.Worker.")
    assert _TEST_SID not in first


@pytest.mark.parametrize(
    "identity",
    ("", "a" * 64, "sha256:" + "A" * 64, "sha256:" + "a" * 63),
)
def test_worker_mutex_name_rejects_noncanonical_root_identity(identity: str) -> None:
    with pytest.raises(ValueError, match="canonical root identity"):
        worker_mutex_name(identity, sid=_TEST_SID)


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
    name = worker_mutex_name(_ROOT_IDENTITY_A)
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


@pytest.mark.skipif(os.name != "nt", reason="requires Windows named mutexes")
def test_distinct_vault_mutexes_can_be_held_concurrently() -> None:
    first = worker_mutex_name(_ROOT_IDENTITY_A)
    second = worker_mutex_name(_ROOT_IDENTITY_B)
    script = """
import sys
from offeragent_harness.runtime.process_lock import ProcessLock
with ProcessLock(sys.argv[1]):
    pass
"""
    with ProcessLock(first):
        completed = subprocess.run(
            [sys.executable, "-c", script, second],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(os.name != "nt", reason="requires Windows named mutexes")
def test_worker_mutex_is_abandoned_and_recoverable_after_process_crash(tmp_path: Path) -> None:
    name = worker_mutex_name(_ROOT_IDENTITY_A)
    ready = tmp_path / "ready"
    crash = tmp_path / "crash"
    script = """
import os
import sys
import time
from pathlib import Path
from offeragent_harness.runtime.process_lock import ProcessLock
lock = ProcessLock(sys.argv[1])
lock.acquire()
Path(sys.argv[2]).write_text("ready", encoding="ascii")
while not Path(sys.argv[3]).exists():
    time.sleep(0.01)
os._exit(23)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, name, str(ready), str(crash)],
        stderr=subprocess.PIPE,
        text=True,
    )
    backend = WindowsMutexBackend()
    handle: int | None = None
    acquired = False
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            stderr = process.communicate(timeout=1)[1]
            pytest.fail(f"crash worker did not acquire the mutex: {stderr}")
        handle = backend.create(name)
        crash.write_text("crash", encoding="ascii")
        assert process.wait(timeout=10) == 23
        assert backend.wait(handle, 10_000) is MutexWaitResult.ABANDONED
        acquired = True
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if handle is not None:
            if acquired:
                backend.release(handle)
            backend.close(handle)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows named mutex security")
def test_named_mutex_dacl_contains_only_current_sid_and_handle_is_not_inheritable() -> None:
    backend = WindowsMutexBackend()
    handle = backend.create(_NAME)
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
