from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from offeragent_harness.runtime.host_supervisor import (
    SupervisedWorkspaceIdentity,
    VerifiedWorkerExecutable,
    WorkerLaunchRequest,
)
from offeragent_harness.runtime.windows_process import (
    ExecutableVerificationError,
    PinnedWorkerExecutableVerifier,
    WindowsProcessError,
    WindowsWorkerJob,
    current_user_profile_directory,
    fixed_worker_argv,
    minimal_worker_environment,
    trusted_worker_environment,
)
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


@dataclass
class FakeManifestTrust:
    authorized: bool = True
    calls: list[VerifiedWorkerExecutable] = field(default_factory=list)

    def authorizes(self, expected: VerifiedWorkerExecutable) -> bool:
        self.calls.append(expected)
        return self.authorized


@dataclass
class FakeAuthenticode:
    valid: bool = True
    calls: list[Path] = field(default_factory=list)

    def verify(self, executable: Path) -> bool:
        self.calls.append(executable)
        return self.valid


def executable(path: Path, content: bytes) -> VerifiedWorkerExecutable:
    return VerifiedWorkerExecutable(
        path,
        path.parent,
        "1.2.3",
        "sha256:" + hashlib.sha256(content).hexdigest(),
    )


def test_fixed_production_argv_has_no_arbitrary_or_sensitive_fields() -> None:
    release = VerifiedWorkerExecutable(
        Path(r"C:\OfferAgent\1.2.3\OfferAgentWorker.exe"),
        Path(r"C:\OfferAgent\1.2.3"),
        "1.2.3",
        "sha256:" + "c" * 64,
    )
    request = WorkerLaunchRequest(workspace(), release)

    arguments = fixed_worker_argv(request)

    assert arguments[0] == str(release.executable)
    assert arguments[1:5] == ["--offeragent-runtime-mode", "worker", "--transport", "named-pipe"]
    assert "stdio" not in arguments
    assert all("token" not in value.casefold() and "secret" not in value.casefold() for value in arguments)
    assert release.file_sha256 not in arguments


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file sharing semantics")
def test_verified_executable_handle_blocks_replacement_until_suspended_spawn_commits(tmp_path: Path) -> None:
    content = b"signed-worker-image"
    worker = tmp_path / "OfferAgentWorker.exe"
    replacement = tmp_path / "replacement.exe"
    worker.write_bytes(content)
    replacement.write_bytes(b"attacker-controlled")
    expected = executable(worker, content)
    trust = FakeManifestTrust()
    signature = FakeAuthenticode()
    verifier = PinnedWorkerExecutableVerifier(manifest_trust=trust, authenticode=signature)

    with verifier.open_verified(expected) as lease:
        assert lease.identity_unchanged()
        assert lease.content_unchanged()
        with pytest.raises(OSError):
            os.replace(replacement, worker)
        assert worker.read_bytes() == content

    os.replace(replacement, worker)
    assert worker.read_bytes() == b"attacker-controlled"
    assert trust.calls == [expected]
    assert signature.calls == [worker.resolve()]


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file APIs")
def test_manifest_trust_and_authenticode_are_both_mandatory(tmp_path: Path) -> None:
    content = b"worker"
    worker = tmp_path / "OfferAgentWorker.exe"
    worker.write_bytes(content)
    expected = executable(worker, content)

    untrusted = PinnedWorkerExecutableVerifier(
        manifest_trust=FakeManifestTrust(authorized=False),
        authenticode=FakeAuthenticode(),
    )
    with pytest.raises(ExecutableVerificationError, match="signed manifest"):
        untrusted.open_verified(expected)

    unsigned = PinnedWorkerExecutableVerifier(
        manifest_trust=FakeManifestTrust(),
        authenticode=FakeAuthenticode(valid=False),
    )
    with pytest.raises(ExecutableVerificationError, match="Authenticode"):
        unsigned.open_verified(expected)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file APIs")
def test_hash_failure_releases_the_lock_handle(tmp_path: Path) -> None:
    worker = tmp_path / "OfferAgentWorker.exe"
    replacement = tmp_path / "replacement.exe"
    worker.write_bytes(b"different")
    replacement.write_bytes(b"replacement")
    expected = executable(worker, b"expected")
    verifier = PinnedWorkerExecutableVerifier(
        manifest_trust=FakeManifestTrust(),
        authenticode=FakeAuthenticode(),
    )

    with pytest.raises(ExecutableVerificationError, match="manifest"):
        verifier.open_verified(expected)

    os.replace(replacement, worker)
    assert worker.read_bytes() == b"replacement"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file sharing semantics")
def test_preexisting_writer_is_rejected_before_hash_can_become_stale(tmp_path: Path) -> None:
    content = b"worker"
    worker = tmp_path / "OfferAgentWorker.exe"
    worker.write_bytes(content)
    verifier = PinnedWorkerExecutableVerifier(
        manifest_trust=FakeManifestTrust(),
        authenticode=FakeAuthenticode(),
    )

    with worker.open("r+b") as writable:
        with pytest.raises(WindowsProcessError, match="CreateFileW"):
            verifier.open_verified(executable(worker, content))
        writable.seek(0)
        writable.write(b"attack")

    assert worker.read_bytes().startswith(b"attack")


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Known Folder APIs")
def test_production_environment_ignores_poisoned_parent_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemRoot", r"Z:\attacker")
    monkeypatch.setenv("WINDIR", r"Z:\attacker")
    monkeypatch.setenv("LOCALAPPDATA", r"Z:\attacker")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker")
    monkeypatch.setenv("PYTHONPATH", r"Z:\inject")

    environment = trusted_worker_environment()

    assert environment["SystemRoot"].casefold() != r"Z:\attacker".casefold()
    assert environment["LOCALAPPDATA"].casefold() != r"Z:\attacker".casefold()
    assert set(environment) == {
        "APPDATA",
        "LOCALAPPDATA",
        "SystemRoot",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    assert all(Path(value).is_absolute() and Path(value).is_dir() for value in environment.values())
    assert current_user_profile_directory() == Path(environment["USERPROFILE"])


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path rules")
def test_test_environment_allowlist_drops_secrets_and_rejects_relative_system_paths(tmp_path: Path) -> None:
    selected = minimal_worker_environment(
        {
            "SystemRoot": os.environ["SystemRoot"],
            "TEMP": str(tmp_path),
            "PATH": "should-not-inherit",
            "OPENAI_API_KEY": "secret",
            "HTTP_PROXY": "http://attacker",
            "PYTHONHOME": "inject",
        }
    )
    assert set(selected) == {"SystemRoot", "TEMP"}
    assert "secret" not in repr(selected)

    with pytest.raises(WindowsProcessError, match="absolute"):
        minimal_worker_environment({"SystemRoot": "relative\\windows"})


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
@pytest.mark.asyncio
async def test_job_object_is_current_user_only_kill_on_close_without_breakaway() -> None:
    job = WindowsWorkerJob(workspace())
    try:
        flags = job.configured_limit_flags
        assert flags & 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        assert flags & 0x00000400  # JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        assert flags & (0x00000800 | 0x00001000) == 0  # no breakaway modes
        sddl = kernel_object_security_sddl(job.native_job_handle)
        trustees = re.findall(r"\([^)]*;;;([^)]+)\)", sddl)
        assert trustees == [current_windows_identity().sid]
        assert all(alias not in trustees for alias in ("SY", "BA", "WD", "AU"))
        assert not kernel_handle_is_inheritable(job.native_job_handle)
        assert await job.wait_empty(1)
    finally:
        job.close()
