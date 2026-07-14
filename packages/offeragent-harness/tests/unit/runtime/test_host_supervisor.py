from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.runtime.host_supervisor import (
    HostShuttingDown,
    HostSupervisor,
    LeaseKind,
    ManagedWorkerProcess,
    RestartPolicy,
    ResumeValidation,
    SupervisedWorkspaceIdentity,
    VerifiedWorkerExecutable,
    WorkerJob,
    WorkerLaunchRequest,
    WorkerReadiness,
    WorkerShutdownReceipt,
    WorkerStartupError,
    WorkerSupervisor,
    WorkerSupervisorRegistry,
    WorkerUnavailable,
)
from offeragent_harness.runtime.lifecycle import HostLifecycle, WorkerLifecycle
from offeragent_harness.testing.clock import ManualClock

SID = "S-1-5-21-100-200-300-1001"
NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


def workspace(
    seed: str = "a",
    *,
    instance: str = "12345678-1234-4234-8234-123456789abc",
) -> SupervisedWorkspaceIdentity:
    return SupervisedWorkspaceIdentity(
        f"wsi_{instance}",
        "sha256:" + seed * 64,
        "sha256:" + chr(ord(seed) + 1) * 64,
    )


EXECUTABLE = VerifiedWorkerExecutable(
    Path(r"C:\Program Files\OfferAgent\versions\1.0.0\OfferAgentWorker.exe"),
    Path(r"C:\Program Files\OfferAgent\versions\1.0.0"),
    "1.0.0",
    "sha256:" + "f" * 64,
)


@dataclass
class FakeLeaseLock:
    name: str
    acquired: bool = False
    released: bool = False

    def acquire(self, *, timeout_ms: int = 0) -> object:
        assert timeout_ms == 0
        assert not self.acquired
        self.acquired = True
        return object()

    def release(self) -> None:
        self.released = True


@dataclass
class FakeLockFactory:
    locks: list[FakeLeaseLock] = field(default_factory=list)

    def __call__(self, name: str) -> FakeLeaseLock:
        lock = FakeLeaseLock(name)
        self.locks.append(lock)
        return lock


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._exit = asyncio.Event()
        self.exit_code: int | None = None
        self.assigned = False
        self.resumed = False
        self.closed = False
        self.terminate_calls: list[int] = []

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def native_process_handle(self) -> int:
        return self._pid + 10_000

    def resume(self) -> None:
        assert self.assigned, "Worker must join its Job while still suspended"
        self.resumed = True

    def poll(self) -> int | None:
        return self.exit_code

    async def wait(self) -> int:
        await self._exit.wait()
        assert self.exit_code is not None
        return self.exit_code

    def terminate(self, exit_code: int) -> None:
        self.terminate_calls.append(exit_code)
        self.exit(exit_code)

    def close(self) -> None:
        self.closed = True

    def exit(self, code: int = 1) -> None:
        if self.exit_code is None:
            self.exit_code = code
            self._exit.set()


@dataclass
class FakeProcessBackend:
    processes: list[FakeProcess] = field(default_factory=list)
    requests: list[WorkerLaunchRequest] = field(default_factory=list)

    def spawn_suspended(self, request: WorkerLaunchRequest, *, job: WorkerJob) -> ManagedWorkerProcess:
        self.requests.append(request)
        process = FakeProcess(1_000 + len(self.processes))
        self.processes.append(process)
        job.assign(process)
        return process


class FakeJob:
    def __init__(self) -> None:
        self.process: FakeProcess | None = None
        self.assigned_handles: list[int] = []
        self.terminate_calls: list[int] = []
        self.closed = False
        self.report_leak = False

    @property
    def native_job_handle(self) -> int:
        return id(self)

    def assign(self, process: ManagedWorkerProcess) -> None:
        assert isinstance(process, FakeProcess)
        assert not process.resumed
        self.process = process
        process.assigned = True

    def assign_process_handle(self, process_handle: int) -> None:
        self.assigned_handles.append(process_handle)

    def terminate_tree(self, exit_code: int) -> None:
        self.terminate_calls.append(exit_code)
        if self.process is not None:
            self.process.exit(exit_code)

    def contains_process_handle(self, process_handle: int) -> bool:
        return self.process is not None and self.process.native_process_handle == process_handle

    async def wait_empty(self, timeout_seconds: float) -> bool:
        assert timeout_seconds > 0
        return not self.report_leak and (self.process is None or self.process.poll() is not None)

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeJobBackend:
    jobs: list[FakeJob] = field(default_factory=list)

    def create(self, workspace: SupervisedWorkspaceIdentity) -> WorkerJob:
        del workspace
        job = FakeJob()
        self.jobs.append(job)
        return job


class FakeProbe:
    def __init__(self) -> None:
        self.ready_gate: asyncio.Event | None = None
        self.resume_gate: asyncio.Event | None = None
        self.resume_result = ResumeValidation(True, True, True, True, True)
        self.mutate: Callable[[WorkerReadiness], WorkerReadiness] = lambda value: value
        self.wait_cancelled = False
        self.resume_cancelled = False
        self.calls = 0

    async def wait_ready(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        runtime_version: str,
        deadline: datetime,
    ) -> WorkerReadiness:
        assert deadline > NOW
        try:
            if self.ready_gate is not None:
                await self.ready_gate.wait()
        except asyncio.CancelledError:
            self.wait_cancelled = True
            raise
        self.calls += 1
        result = WorkerReadiness(
            pid,
            runtime_version,
            expected.workspace_instance_id,
            expected.canonical_root_identity,
            expected.database_identity,
        )
        return self.mutate(result)

    async def revalidate_after_resume(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        deadline: datetime,
    ) -> ResumeValidation:
        del expected, pid, deadline
        try:
            if self.resume_gate is not None:
                await self.resume_gate.wait()
        except asyncio.CancelledError:
            self.resume_cancelled = True
            raise
        return self.resume_result


class FakeShutdownControl:
    def __init__(self, backend: FakeProcessBackend) -> None:
        self.backend = backend
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.cancelled = False
        self.exit_gracefully = True
        self.receipt = WorkerShutdownReceipt(True, True, True, True)

    async def reject_new_runs(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        deadline: datetime,
    ) -> None:
        del expected, pid, deadline
        self.calls.append("reject")
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def request_graceful_shutdown(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        deadline: datetime,
    ) -> WorkerShutdownReceipt:
        del expected, deadline
        self.calls.append("shutdown")
        if self.exit_gracefully:
            process = next(item for item in self.backend.processes if item.pid == pid)
            process.exit(0)
        return self.receipt


@dataclass
class WorkerFixture:
    worker: WorkerSupervisor
    process_backend: FakeProcessBackend
    job_backend: FakeJobBackend
    probe: FakeProbe
    control: FakeShutdownControl
    locks: FakeLockFactory


def make_worker(
    identity: SupervisedWorkspaceIdentity,
    clock: ManualClock,
    *,
    policy: RestartPolicy | None = None,
    probe: FakeProbe | None = None,
) -> WorkerFixture:
    process_backend = FakeProcessBackend()
    job_backend = FakeJobBackend()
    selected_probe = probe or FakeProbe()
    control = FakeShutdownControl(process_backend)
    locks = FakeLockFactory()
    worker = WorkerSupervisor(
        identity=identity,
        executable=EXECUTABLE,
        process_backend=process_backend,
        job_backend=job_backend,
        readiness_probe=selected_probe,
        shutdown_control=control,
        clock=clock,
        restart_policy=policy or RestartPolicy(idle_timeout_seconds=60),
        lock_factory=locks,
        current_user_sid=SID,
        jitter=lambda: 0.5,
    )
    return WorkerFixture(worker, process_backend, job_backend, selected_probe, control, locks)


async def eventually(predicate: Callable[[], bool], *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


@pytest.mark.asyncio
async def test_concurrent_attach_spawns_one_worker_and_multi_vaults_are_isolated() -> None:
    clock = ManualClock(NOW)
    fixtures: dict[str, WorkerFixture] = {}

    def factory(identity: SupervisedWorkspaceIdentity) -> WorkerSupervisor:
        fixture = make_worker(identity, clock)
        fixtures[identity.workspace_instance_id] = fixture
        return fixture.worker

    registry = WorkerSupervisorRegistry(factory)
    host_locks = FakeLockFactory()
    host = HostSupervisor(workers=registry, clock=clock, lock_factory=host_locks, current_user_sid=SID)
    await host.start()

    first = workspace("a")
    attachments = await asyncio.gather(*(host.attach(first, client_id=f"client-{index}") for index in range(20)))
    second = workspace("c", instance="22345678-1234-4234-8234-123456789abc")
    second_attachment = await host.attach(second, client_id="other-vault")

    assert len(fixtures[first.workspace_instance_id].process_backend.processes) == 1
    assert len(fixtures[second.workspace_instance_id].process_backend.processes) == 1
    assert {item.workspace_instance_id for item in await host.health()} == {
        first.workspace_instance_id,
        second.workspace_instance_id,
    }
    assert (await fixtures[first.workspace_instance_id].worker.health()).client_count == 20
    assert all(attachment.worker is attachments[0].worker for attachment in attachments)

    await asyncio.gather(*(attachment.detach() for attachment in attachments))
    await second_attachment.detach()
    await host.shutdown()
    assert host.snapshot.state is HostLifecycle.STOPPED
    assert all(item.worker.snapshot.state is WorkerLifecycle.STOPPED for item in fixtures.values())
    assert all(lock.released for lock in host_locks.locks)


@pytest.mark.asyncio
async def test_worker_never_commits_ready_when_handshake_identity_mismatches() -> None:
    clock = ManualClock(NOW)
    probe = FakeProbe()
    probe.mutate = lambda value: WorkerReadiness(
        value.pid + 1,
        value.runtime_version,
        value.workspace_instance_id,
        value.canonical_root_identity,
        value.database_identity,
    )
    fixture = make_worker(workspace(), clock, probe=probe)

    with pytest.raises(WorkerStartupError, match="readiness"):
        await fixture.worker.start()

    assert fixture.worker.snapshot.state in {WorkerLifecycle.CRASHED, WorkerLifecycle.RECOVERING}
    assert fixture.job_backend.jobs[0].terminate_calls
    assert fixture.process_backend.processes[0].closed
    await fixture.worker.close()


@pytest.mark.asyncio
async def test_startup_cancellation_cancels_probe_and_leaves_no_process_tree() -> None:
    clock = ManualClock(NOW)
    probe = FakeProbe()
    probe.ready_gate = asyncio.Event()
    fixture = make_worker(workspace(), clock, probe=probe)

    start = asyncio.create_task(fixture.worker.start())
    await eventually(lambda: bool(fixture.process_backend.processes))
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start

    assert probe.wait_cancelled
    assert fixture.worker.snapshot.state is WorkerLifecycle.STOPPED
    assert fixture.process_backend.processes[0].closed
    assert fixture.job_backend.jobs[0].terminate_calls
    assert fixture.locks.locks[0].released


@pytest.mark.asyncio
async def test_crash_backoff_resets_after_stable_uptime_and_opens_circuit() -> None:
    clock = ManualClock(NOW)
    policy = RestartPolicy(
        idle_timeout_seconds=600,
        initial_backoff_seconds=2,
        maximum_backoff_seconds=8,
        crash_loop_limit=3,
        stable_reset_seconds=10,
    )
    fixture = make_worker(workspace(), clock, policy=policy)
    await fixture.worker.start()
    first = fixture.process_backend.processes[0]

    first.exit(11)
    await eventually(lambda: fixture.worker.snapshot.state is WorkerLifecycle.RECOVERING)
    assert fixture.worker.consecutive_crashes == 1
    clock.advance(timedelta(seconds=2))
    await eventually(lambda: len(fixture.process_backend.processes) == 2)
    await eventually(lambda: fixture.worker.snapshot.state is WorkerLifecycle.IDLE)

    clock.advance(timedelta(seconds=10))
    fixture.process_backend.processes[1].exit(12)
    await eventually(lambda: fixture.worker.snapshot.state is WorkerLifecycle.RECOVERING)
    assert fixture.worker.consecutive_crashes == 1
    clock.advance(timedelta(seconds=2))
    await eventually(lambda: len(fixture.process_backend.processes) == 3)
    await eventually(lambda: fixture.worker.snapshot.state is WorkerLifecycle.IDLE)
    fixture.process_backend.processes[2].exit(13)
    await eventually(lambda: fixture.worker.snapshot.state is WorkerLifecycle.RECOVERING)
    clock.advance(timedelta(seconds=4))
    await eventually(lambda: len(fixture.process_backend.processes) == 4)
    await eventually(lambda: fixture.worker.snapshot.state is WorkerLifecycle.IDLE)
    fixture.process_backend.processes[3].exit(14)
    await eventually(lambda: fixture.worker.snapshot.state is WorkerLifecycle.DEGRADED)

    assert fixture.worker.consecutive_crashes == 3
    assert len(fixture.process_backend.processes) == 4
    await fixture.worker.close()


@pytest.mark.asyncio
async def test_detach_does_not_kill_active_run_and_web_lease_prevents_idle_exit() -> None:
    clock = ManualClock(NOW)
    fixture = make_worker(
        workspace(),
        clock,
        policy=RestartPolicy(idle_timeout_seconds=5, stable_reset_seconds=60),
    )
    await fixture.worker.start()
    client = await fixture.worker.acquire_lease(LeaseKind.CLIENT, "client")
    run = await fixture.worker.acquire_lease(LeaseKind.ACTIVE_RUN, "run")

    await client.release()
    clock.advance(timedelta(seconds=30))
    await asyncio.sleep(0)
    assert fixture.worker.snapshot.state is WorkerLifecycle.BUSY
    assert fixture.process_backend.processes[0].poll() is None

    await run.release()
    web = await fixture.worker.acquire_lease(LeaseKind.WEB_PERSISTENCE, "web")
    clock.advance(timedelta(seconds=30))
    await asyncio.sleep(0)
    assert fixture.worker.snapshot.state.value == WorkerLifecycle.READY.value

    await web.release()
    clock.advance(timedelta(seconds=5))
    await eventually(lambda: fixture.worker.snapshot.state is WorkerLifecycle.STOPPED)
    assert fixture.control.calls == ["reject", "shutdown"]


@pytest.mark.asyncio
async def test_graceful_shutdown_commits_before_close_without_forced_job_kill() -> None:
    clock = ManualClock(NOW)
    fixture = make_worker(workspace(), clock)
    await fixture.worker.start()

    await fixture.worker.close()

    assert fixture.control.calls == ["reject", "shutdown"]
    assert fixture.job_backend.jobs[0].terminate_calls == []
    assert fixture.process_backend.processes[0].exit_code == 0
    assert fixture.worker.snapshot.state is WorkerLifecycle.STOPPED


@pytest.mark.asyncio
async def test_successful_application_shutdown_is_not_restarted_as_a_crash() -> None:
    clock = ManualClock(NOW)
    fixture = make_worker(
        workspace(),
        clock,
        policy=RestartPolicy(initial_backoff_seconds=1, idle_timeout_seconds=600),
    )
    await fixture.worker.start()

    fixture.process_backend.processes[0].exit(0)
    await eventually(lambda: fixture.worker.snapshot.state is WorkerLifecycle.STOPPED)
    clock.advance(timedelta(seconds=30))
    await asyncio.sleep(0)

    assert len(fixture.process_backend.processes) == 1
    assert fixture.worker.consecutive_crashes == 0
    assert fixture.worker.job is None
    assert fixture.locks.locks[0].released
    await fixture.worker.close()


@pytest.mark.asyncio
async def test_control_loss_waits_for_worker_natural_exit_before_job_kill() -> None:
    clock = ManualClock(NOW)
    fixture = make_worker(workspace(), clock)
    await fixture.worker.start()
    process = fixture.process_backend.processes[0]
    job = fixture.job_backend.jobs[0]
    wait_entered = asyncio.Event()
    allow_exit_confirmation = asyncio.Event()

    async def control_already_revoked(
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        deadline: datetime,
    ) -> None:
        del expected, pid, deadline
        fixture.control.calls.append("reject")
        raise OSError("application shutdown already revoked Worker control")

    async def wait_for_natural_exit(timeout_seconds: float) -> bool:
        assert timeout_seconds > 0
        wait_entered.set()
        await allow_exit_confirmation.wait()
        return process.poll() is not None

    fixture.control.reject_new_runs = control_already_revoked  # type: ignore[method-assign]
    job.wait_empty = wait_for_natural_exit  # type: ignore[method-assign]

    stopping = asyncio.create_task(fixture.worker.close())
    await wait_entered.wait()

    assert fixture.worker.snapshot.state is WorkerLifecycle.STOPPING
    assert process.poll() is None
    assert job.terminate_calls == []

    process.exit(0)
    allow_exit_confirmation.set()
    await stopping

    assert job.terminate_calls == []
    assert fixture.worker.snapshot.state.value == WorkerLifecycle.STOPPED.value
    assert any("graceful shutdown failed" in item for item in fixture.worker.diagnostics)


@pytest.mark.asyncio
async def test_graceful_shutdown_deadline_falls_back_to_job_tree_kill() -> None:
    clock = ManualClock(NOW)
    fixture = make_worker(
        workspace(),
        clock,
        policy=RestartPolicy(shutdown_timeout_seconds=3, idle_timeout_seconds=60),
    )
    fixture.control.gate = asyncio.Event()
    fixture.control.exit_gracefully = False
    await fixture.worker.start()

    stopping = asyncio.create_task(fixture.worker.close())
    await eventually(lambda: fixture.control.calls == ["reject"])
    clock.advance(timedelta(seconds=3))
    await stopping

    assert fixture.control.cancelled
    assert fixture.job_backend.jobs[0].terminate_calls
    assert any("deadline" in item for item in fixture.worker.diagnostics)
    assert fixture.worker.snapshot.state is WorkerLifecycle.STOPPED


@pytest.mark.asyncio
async def test_resume_failure_is_fail_closed_and_external_cancellation_propagates() -> None:
    clock = ManualClock(NOW)
    probe = FakeProbe()
    fixture = make_worker(workspace(), clock, probe=probe)
    await fixture.worker.start()
    await fixture.worker.suspend()
    probe.resume_result = ResumeValidation(True, False, True, True, True)

    result = await fixture.worker.resume()
    assert not result.safe_to_resume
    assert fixture.worker.snapshot.state is WorkerLifecycle.DEGRADED
    with pytest.raises(WorkerUnavailable):
        await fixture.worker.acquire_lease(LeaseKind.CLIENT, "unsafe")
    await fixture.worker.close()

    probe2 = FakeProbe()
    probe2.resume_gate = asyncio.Event()
    fixture2 = make_worker(workspace("c"), clock, probe=probe2)
    await fixture2.worker.start()
    await fixture2.worker.suspend()
    resume = asyncio.create_task(fixture2.worker.resume())
    await asyncio.sleep(0)
    resume.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resume
    assert probe2.resume_cancelled
    assert fixture2.worker.snapshot.state is WorkerLifecycle.IDLE
    await fixture2.worker.close()


@pytest.mark.asyncio
async def test_crash_invalidates_old_lease_epoch_without_releasing_new_owner() -> None:
    clock = ManualClock(NOW)
    fixture = make_worker(
        workspace(),
        clock,
        policy=RestartPolicy(initial_backoff_seconds=1, idle_timeout_seconds=600),
    )
    await fixture.worker.start()
    stale = await fixture.worker.acquire_lease(LeaseKind.ACTIVE_RUN, "same-run")
    fixture.process_backend.processes[0].exit(7)
    await eventually(lambda: fixture.worker.snapshot.state is WorkerLifecycle.RECOVERING)
    clock.advance(timedelta(seconds=1))
    await eventually(lambda: len(fixture.process_backend.processes) == 2)
    await eventually(lambda: fixture.worker.snapshot.state is WorkerLifecycle.IDLE)
    current = await fixture.worker.acquire_lease(LeaseKind.ACTIVE_RUN, "same-run")

    await stale.release()
    assert (await fixture.worker.health()).active_run_count == 1
    await current.release()
    await fixture.worker.close()


class GatedRegistry:
    def __init__(self, wrapped: WorkerSupervisorRegistry) -> None:
        self.wrapped = wrapped
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get_or_create(self, identity: SupervisedWorkspaceIdentity) -> tuple[WorkerSupervisor, bool]:
        self.entered.set()
        await self.release.wait()
        return await self.wrapped.get_or_create(identity)

    async def workers(self) -> tuple[WorkerSupervisor, ...]:
        return await self.wrapped.workers()

    async def close_all(self) -> tuple[BaseException, ...]:
        return await self.wrapped.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["registry", "startup", "lease"])
async def test_attach_is_linearized_against_shutdown_at_every_async_boundary(stage: str) -> None:
    clock = ManualClock(NOW)
    probe = FakeProbe()
    fixture = make_worker(workspace(), clock, probe=probe)
    base_registry = WorkerSupervisorRegistry(lambda identity: fixture.worker)
    registry: Any = base_registry
    entered = asyncio.Event()
    release = asyncio.Event()
    observer: asyncio.Task[None] | None = None
    if stage == "registry":
        gated = GatedRegistry(base_registry)
        registry = gated
        entered = gated.entered
        release = gated.release
    elif stage == "startup":
        probe.ready_gate = release

        async def observe_start() -> None:
            await eventually(lambda: bool(fixture.process_backend.processes))
            entered.set()

        observer = asyncio.create_task(observe_start())
    else:
        original = fixture.worker.acquire_lease

        async def gated_lease(kind: LeaseKind, lease_id: str) -> Any:
            entered.set()
            await release.wait()
            return await original(kind, lease_id)

        fixture.worker.acquire_lease = gated_lease  # type: ignore[method-assign]

    host = HostSupervisor(workers=registry, clock=clock, lock_factory=FakeLockFactory(), current_user_sid=SID)
    await host.start()
    attaching = asyncio.create_task(host.attach(workspace(), client_id="racing-client"))
    await entered.wait()
    shutdown = asyncio.create_task(host.shutdown())
    await asyncio.sleep(0)
    with pytest.raises(HostShuttingDown):
        await host.attach(workspace(), client_id="late-client")
    release.set()

    attachment = await attaching
    await shutdown
    if observer is not None:
        await observer
    assert host.snapshot.state is HostLifecycle.STOPPED
    assert attachment.worker.snapshot.state is WorkerLifecycle.STOPPED
    assert all(process.closed for process in fixture.process_backend.processes)
    assert fixture.worker.job is None
    await attachment.detach()
