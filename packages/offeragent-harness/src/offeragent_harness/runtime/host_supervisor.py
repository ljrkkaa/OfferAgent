"""Current-user Host and per-workspace Worker process supervision.

This module intentionally knows only process metadata and opaque, hashed
workspace identities.  It does not import the Harness, Agent loop, Vault,
models, tools, repositories, or event stores.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Protocol, TypeAlias, TypeVar, cast

from offeragent_harness.ports.system import Clock

from .lifecycle import HostLifecycle, LifecycleMachine, LifecycleSnapshot, WorkerLifecycle
from .process_lock import ProcessLock, host_mutex_name, worker_mutex_name

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_WORKSPACE_ID_RE = re.compile(r"wsi_[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_RUNTIME_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_LEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")


class SupervisorError(RuntimeError):
    pass


class HostShuttingDown(SupervisorError):
    pass


class WorkerIdentityConflict(SupervisorError):
    pass


class WorkerStartupError(SupervisorError):
    pass


class WorkerReadinessMismatch(WorkerStartupError):
    pass


class WorkerTreeLeak(SupervisorError):
    pass


class WorkerUnavailable(SupervisorError):
    pass


@dataclass(frozen=True, slots=True)
class SupervisedWorkspaceIdentity:
    """Opaque identity safe for the unprivileged Host to retain."""

    workspace_instance_id: str
    canonical_root_identity: str
    database_identity: str

    def __post_init__(self) -> None:
        if not _WORKSPACE_ID_RE.fullmatch(self.workspace_instance_id):
            raise ValueError("workspace_instance_id must be a canonical wsi_<uuid>")
        if not _SHA256_RE.fullmatch(self.canonical_root_identity):
            raise ValueError("canonical_root_identity must be a lowercase sha256 digest")
        if not _SHA256_RE.fullmatch(self.database_identity):
            raise ValueError("database_identity must be a lowercase sha256 digest")

    @property
    def registry_key(self) -> tuple[str, str]:
        return self.workspace_instance_id, self.canonical_root_identity


@dataclass(frozen=True, slots=True)
class VerifiedWorkerExecutable:
    executable: Path
    version_directory: Path
    runtime_version: str
    file_sha256: str

    def __post_init__(self) -> None:
        if not _RUNTIME_VERSION_RE.fullmatch(self.runtime_version):
            raise ValueError("runtime_version is not a canonical release identifier")
        if not _SHA256_RE.fullmatch(self.file_sha256):
            raise ValueError("file_sha256 must be a lowercase sha256 digest")


@dataclass(frozen=True, slots=True)
class WorkerLaunchRequest:
    """Fixed, non-secret launch material; there is no arbitrary argv or env."""

    workspace: SupervisedWorkspaceIdentity
    executable: VerifiedWorkerExecutable


@dataclass(frozen=True, slots=True)
class WorkerReadiness:
    pid: int
    runtime_version: str
    workspace_instance_id: str
    canonical_root_identity: str
    database_identity: str

    def __post_init__(self) -> None:
        if self.pid <= 0:
            raise ValueError("readiness PID must be positive")


@dataclass(frozen=True, slots=True)
class ResumeValidation:
    deadlines_valid: bool
    vault_hash_valid: bool
    named_pipe_client_valid: bool
    model_connection_valid: bool
    approvals_valid: bool

    @property
    def safe_to_resume(self) -> bool:
        return all(
            (
                self.deadlines_valid,
                self.vault_hash_valid,
                self.named_pipe_client_valid,
                self.model_connection_valid,
                self.approvals_valid,
            )
        )


class ManagedWorkerProcess(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def native_process_handle(self) -> int: ...

    def resume(self) -> None: ...

    def poll(self) -> int | None: ...

    async def wait(self) -> int: ...

    def terminate(self, exit_code: int) -> None: ...

    def close(self) -> None: ...


class WorkerProcessBackend(Protocol):
    def spawn_suspended(
        self,
        request: WorkerLaunchRequest,
        *,
        job: WorkerJob,
    ) -> ManagedWorkerProcess:
        """Atomically create the suspended process as a member of ``job``."""


class WorkerJob(Protocol):
    @property
    def native_job_handle(self) -> int: ...

    def assign(self, process: ManagedWorkerProcess) -> None: ...

    def assign_process_handle(self, process_handle: int) -> None:
        """Join a future Shell or parser child to the same ownership tree."""

    def terminate_tree(self, exit_code: int) -> None: ...

    def contains_process_handle(self, process_handle: int) -> bool: ...

    async def wait_empty(self, timeout_seconds: float) -> bool: ...

    def close(self) -> None: ...


class WorkerJobBackend(Protocol):
    def create(self, workspace: SupervisedWorkspaceIdentity) -> WorkerJob: ...


class WorkerReadinessProbe(Protocol):
    async def wait_ready(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        runtime_version: str,
        deadline: datetime,
    ) -> WorkerReadiness: ...

    async def revalidate_after_resume(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        deadline: datetime,
    ) -> ResumeValidation: ...


@dataclass(frozen=True, slots=True)
class WorkerShutdownReceipt:
    new_runs_rejected: bool
    active_runs_cancelled: bool
    interrupted_state_persisted: bool
    worker_state_flushed: bool

    @property
    def safely_committed(self) -> bool:
        return all(
            (
                self.new_runs_rejected,
                self.active_runs_cancelled,
                self.interrupted_state_persisted,
                self.worker_state_flushed,
            )
        )


class WorkerShutdownControl(Protocol):
    """Authenticated Worker control plane; persistence remains Worker-owned."""

    async def reject_new_runs(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        deadline: datetime,
    ) -> None: ...

    async def request_graceful_shutdown(
        self,
        expected: SupervisedWorkspaceIdentity,
        *,
        pid: int,
        deadline: datetime,
    ) -> WorkerShutdownReceipt: ...


class ProcessLease(Protocol):
    def acquire(self, *, timeout_ms: int = 0) -> object: ...

    def release(self) -> None: ...


LockFactory: TypeAlias = Callable[[str], ProcessLease]
StateListener: TypeAlias = Callable[[LifecycleSnapshot], Awaitable[None] | None]
WorkerFactory: TypeAlias = Callable[[SupervisedWorkspaceIdentity], "WorkerSupervisor"]
JitterSource: TypeAlias = Callable[[], float]
_DeadlineResult = TypeVar("_DeadlineResult")


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    startup_timeout_seconds: float = 30.0
    shutdown_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = 300.0
    initial_backoff_seconds: float = 1.0
    maximum_backoff_seconds: float = 30.0
    backoff_multiplier: float = 2.0
    jitter_ratio: float = 0.2
    crash_loop_limit: int = 5
    stable_reset_seconds: float = 120.0

    def __post_init__(self) -> None:
        positive = (
            self.startup_timeout_seconds,
            self.shutdown_timeout_seconds,
            self.idle_timeout_seconds,
            self.initial_backoff_seconds,
            self.maximum_backoff_seconds,
            self.stable_reset_seconds,
        )
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ValueError("supervisor timeouts and backoffs must be finite and positive")
        if not math.isfinite(self.backoff_multiplier) or self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be finite and at least one")
        if not math.isfinite(self.jitter_ratio) or not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")
        if self.crash_loop_limit < 1:
            raise ValueError("crash_loop_limit must be positive")
        if self.initial_backoff_seconds > self.maximum_backoff_seconds:
            raise ValueError("initial backoff cannot exceed the cap")

    def delay_for(self, consecutive_crashes: int, *, jitter_unit: float) -> float:
        if consecutive_crashes < 1:
            raise ValueError("consecutive_crashes must be positive")
        if not math.isfinite(jitter_unit) or not 0 <= jitter_unit <= 1:
            raise ValueError("jitter source must return a value between zero and one")
        base = min(
            self.maximum_backoff_seconds,
            self.initial_backoff_seconds * self.backoff_multiplier ** (consecutive_crashes - 1),
        )
        offset = (jitter_unit * 2 - 1) * self.jitter_ratio * base
        return max(0.0, min(self.maximum_backoff_seconds, base + offset))


class LeaseKind(str, Enum):
    CLIENT = "client"
    ACTIVE_RUN = "active-run"
    BACKGROUND_JOB = "background-job"
    WEB_PERSISTENCE = "web-persistence"


@dataclass(frozen=True, slots=True)
class WorkerHealth:
    workspace_instance_id: str
    state: WorkerLifecycle
    runtime_version: str
    pid: int | None
    consecutive_crashes: int
    client_count: int
    active_run_count: int
    background_job_count: int
    web_persistence_count: int


@dataclass(slots=True)
class WorkerLease:
    _supervisor: WorkerSupervisor
    kind: LeaseKind
    lease_id: str
    epoch: int
    _released: bool = field(default=False, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def release(self) -> None:
        async with self._lock:
            if self._released:
                return
            self._released = True
        await self._supervisor.release_lease(self.kind, self.lease_id, epoch=self.epoch)

    async def __aenter__(self) -> WorkerLease:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.release()


@dataclass(slots=True)
class WorkerAttachment:
    worker: WorkerSupervisor
    client_lease: WorkerLease

    async def detach(self) -> None:
        await self.client_lease.release()

    async def __aenter__(self) -> WorkerAttachment:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.detach()


class WorkerSupervisor:
    """Own one Worker process, mutex, Job Object, health, and idle leases."""

    def __init__(
        self,
        *,
        identity: SupervisedWorkspaceIdentity,
        executable: VerifiedWorkerExecutable,
        process_backend: WorkerProcessBackend,
        job_backend: WorkerJobBackend,
        readiness_probe: WorkerReadinessProbe,
        shutdown_control: WorkerShutdownControl,
        clock: Clock,
        restart_policy: RestartPolicy | None = None,
        lock_factory: LockFactory | None = None,
        current_user_sid: str | None = None,
        jitter: JitterSource | None = None,
    ) -> None:
        self.identity = identity
        self.executable = executable
        self._process_backend = process_backend
        self._job_backend = job_backend
        self._readiness_probe = readiness_probe
        self._shutdown_control = shutdown_control
        self._clock = clock
        self._policy = restart_policy or RestartPolicy()
        self._lock_factory = lock_factory or _process_lock_factory
        self._current_user_sid = current_user_sid
        self._jitter = jitter or random.SystemRandom().random
        self._lifecycle = LifecycleMachine.worker(now=clock.utcnow)
        self._operation_lock = asyncio.Lock()
        self._lease_lock = asyncio.Lock()
        self._worker_lock: ProcessLease | None = None
        self._process: ManagedWorkerProcess | None = None
        self._job: WorkerJob | None = None
        self._process_started_at: float | None = None
        self._supervision_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._stop_requested = asyncio.Event()
        self._closed = False
        self._suspended = False
        self._consecutive_crashes = 0
        self._idle_generation = 0
        self._lease_epoch = 0
        self._leases: dict[LeaseKind, dict[str, int]] = {kind: {} for kind in LeaseKind}
        self._listeners: list[StateListener] = []
        self._listener_errors: list[str] = []
        self._cleanup_errors: list[str] = []

    @property
    def snapshot(self) -> LifecycleSnapshot:
        return self._lifecycle.snapshot

    @property
    def pid(self) -> int | None:
        process = self._process
        return None if process is None else process.pid

    @property
    def consecutive_crashes(self) -> int:
        return self._consecutive_crashes

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return tuple((*self._listener_errors, *self._cleanup_errors))

    @property
    def job(self) -> WorkerJob | None:
        """Ownership surface used later by Shell child supervisors."""

        return self._job

    def add_state_listener(self, listener: StateListener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    async def start(self) -> LifecycleSnapshot:
        launched = False
        async with self._operation_lock:
            if self._closed:
                raise WorkerUnavailable("Worker supervisor has permanently stopped")
            current = self.snapshot.state
            if self._process is not None and current in {
                WorkerLifecycle.STARTING,
                WorkerLifecycle.READY,
                WorkerLifecycle.BUSY,
                WorkerLifecycle.IDLE,
                WorkerLifecycle.DEGRADED,
            }:
                return self.snapshot
            if current in {WorkerLifecycle.CRASHED, WorkerLifecycle.RECOVERING}:
                raise WorkerUnavailable("Worker is already in automatic recovery")
            if current is WorkerLifecycle.DEGRADED:
                raise WorkerUnavailable("crash-loop degraded Worker requires an explicit restart")
            self._stop_requested = asyncio.Event()
            self._ensure_worker_lock()
            await self._transition(WorkerLifecycle.STARTING, reason="workspace attach")
            try:
                process = await self._launch_once()
            except asyncio.CancelledError:
                await self._cleanup_launch()
                self._release_worker_lock()
                await self._transition_to_stopped("startup cancelled")
                raise
            except BaseException as error:
                await self._cleanup_launch()
                await self._mark_crashed_locked(f"startup failed: {type(error).__name__}")
                self._start_recovery_task(None)
                raise WorkerStartupError("Worker failed before its authenticated readiness barrier") from error
            self._start_recovery_task(process)
            launched = True
        if launched:
            await self._reconcile_activity()
        return self.snapshot

    async def restart_now(self) -> LifecycleSnapshot:
        """Explicit operator retry after a crash-loop circuit breaker."""

        async with self._operation_lock:
            if self._closed:
                raise WorkerUnavailable("Worker supervisor has permanently stopped")
            if self.snapshot.state is not WorkerLifecycle.DEGRADED or self._process is not None:
                raise WorkerUnavailable("explicit restart is only valid for a stopped crash-loop")
            self._consecutive_crashes = 0
            await self._transition(WorkerLifecycle.RECOVERING, reason="explicit retry")
            try:
                process = await self._launch_once()
            except BaseException as error:
                await self._cleanup_launch()
                await self._mark_crashed_locked(f"explicit restart failed: {type(error).__name__}")
                self._start_recovery_task(None)
                raise WorkerStartupError("explicit Worker restart failed") from error
            self._start_recovery_task(process)
        await self._reconcile_activity()
        return self.snapshot

    async def acquire_lease(self, kind: LeaseKind, lease_id: str) -> WorkerLease:
        if not _LEASE_ID_RE.fullmatch(lease_id):
            raise ValueError("lease_id must be 1..128 safe ASCII characters")
        async with self._lease_lock:
            if self._closed or self.snapshot.state in {
                WorkerLifecycle.COLD,
                WorkerLifecycle.STARTING,
                WorkerLifecycle.STOPPING,
                WorkerLifecycle.STOPPED,
                WorkerLifecycle.CRASHED,
                WorkerLifecycle.RECOVERING,
                WorkerLifecycle.DEGRADED,
            }:
                raise WorkerUnavailable("Worker is not ready to accept a lease")
            if lease_id in self._leases[kind]:
                raise ValueError(f"duplicate {kind.value} lease ID")
            epoch = self._lease_epoch
            self._leases[kind][lease_id] = epoch
            self._idle_generation += 1
            idle_task = self._idle_task
            self._idle_task = None
        if idle_task is not None:
            idle_task.cancel()
        await self._reconcile_activity()
        return WorkerLease(self, kind, lease_id, epoch)

    async def release_lease(self, kind: LeaseKind, lease_id: str, *, epoch: int | None = None) -> None:
        async with self._lease_lock:
            current_epoch = self._leases[kind].get(lease_id)
            if epoch is None or current_epoch == epoch:
                self._leases[kind].pop(lease_id, None)
            self._idle_generation += 1
        await self._reconcile_activity()

    async def suspend(self) -> None:
        self._suspended = True

    async def resume(self) -> ResumeValidation:
        if not self._suspended:
            raise SupervisorError("resume requested without a preceding suspend")
        process = self._process
        if process is None or process.poll() is not None:
            validation = ResumeValidation(False, False, False, False, False)
        else:
            deadline = self._clock.utcnow() + timedelta(seconds=self._policy.startup_timeout_seconds)
            try:
                validation = await self._before_deadline(
                    self._readiness_probe.revalidate_after_resume(
                        self.identity,
                        pid=process.pid,
                        deadline=deadline,
                    ),
                    deadline=deadline,
                    timeout_message="resume revalidation deadline expired",
                )
            except TimeoutError:
                validation = ResumeValidation(False, False, False, False, False)
        self._suspended = False
        if not validation.safe_to_resume:
            async with self._operation_lock:
                if self.snapshot.state in {
                    WorkerLifecycle.READY,
                    WorkerLifecycle.BUSY,
                    WorkerLifecycle.IDLE,
                }:
                    if self.snapshot.state is WorkerLifecycle.IDLE:
                        await self._transition(WorkerLifecycle.READY, reason="resume validation gate")
                    await self._transition(WorkerLifecycle.DEGRADED, reason="resume revalidation failed closed")
            return validation
        await self._reconcile_activity(allow_degraded_recovery=True)
        return validation

    async def stop(self, *, reason: str = "requested stop", permanent: bool = False) -> None:
        self._stop_requested.set()
        if permanent:
            self._closed = True
        idle_task = self._idle_task
        self._idle_task = None
        if idle_task is not None and idle_task is not asyncio.current_task():
            idle_task.cancel()
        supervision_task = self._supervision_task
        self._supervision_task = None
        if supervision_task is not None and supervision_task is not asyncio.current_task():
            supervision_task.cancel()
            await _await_cancelled(supervision_task)
        leak: WorkerTreeLeak | None = None
        cancellation: asyncio.CancelledError | None = None
        async with self._operation_lock:
            state = self.snapshot.state
            if state is WorkerLifecycle.STOPPED:
                self._release_worker_lock()
                return
            if state is WorkerLifecycle.COLD:
                await self._transition(WorkerLifecycle.STOPPED, reason=reason)
            elif state is WorkerLifecycle.CRASHED:
                await self._transition(WorkerLifecycle.STOPPED, reason=reason)
            else:
                if state is not WorkerLifecycle.STOPPING:
                    await self._transition(WorkerLifecycle.STOPPING, reason=reason)
                try:
                    cleanup_task = asyncio.create_task(self._graceful_then_cleanup(require_empty=True))
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError as error:
                        cancellation = error
                        await cleanup_task
                except WorkerTreeLeak as error:
                    leak = error
                await self._transition(WorkerLifecycle.STOPPED, reason=reason)
            await self._invalidate_leases_for_new_epoch()
            self._consecutive_crashes = 0
            self._release_worker_lock()
        if leak is not None:
            raise leak
        if cancellation is not None:
            raise cancellation

    async def close(self) -> None:
        await self.stop(reason="Host shutdown", permanent=True)

    async def health(self) -> WorkerHealth:
        async with self._lease_lock:
            counts = {kind: len(values) for kind, values in self._leases.items()}
        return WorkerHealth(
            workspace_instance_id=self.identity.workspace_instance_id,
            state=self.snapshot.state,  # type: ignore[arg-type]
            runtime_version=self.executable.runtime_version,
            pid=self.pid,
            consecutive_crashes=self._consecutive_crashes,
            client_count=counts[LeaseKind.CLIENT],
            active_run_count=counts[LeaseKind.ACTIVE_RUN],
            background_job_count=counts[LeaseKind.BACKGROUND_JOB],
            web_persistence_count=counts[LeaseKind.WEB_PERSISTENCE],
        )

    def _ensure_worker_lock(self) -> None:
        if self._worker_lock is not None:
            return
        name = worker_mutex_name(self.identity.canonical_root_identity, sid=self._current_user_sid)
        lock = self._lock_factory(name)
        lock.acquire(timeout_ms=0)
        self._worker_lock = lock

    def _release_worker_lock(self) -> None:
        lock = self._worker_lock
        self._worker_lock = None
        if lock is not None:
            lock.release()

    async def _launch_once(self) -> ManagedWorkerProcess:
        request = WorkerLaunchRequest(self.identity, self.executable)
        job = self._job_backend.create(self.identity)
        process: ManagedWorkerProcess | None = None
        assigned = False
        try:
            process = self._process_backend.spawn_suspended(request, job=job)
            assigned = True
            if process.pid <= 0 or process.native_process_handle <= 0:
                raise WorkerStartupError("process backend returned an invalid process identity")
            if not job.contains_process_handle(process.native_process_handle):
                raise WorkerStartupError("process backend did not atomically place Worker in its Job")
            self._job = job
            self._process = process
            process.resume()
            deadline = self._clock.utcnow() + timedelta(seconds=self._policy.startup_timeout_seconds)
            readiness = await self._before_deadline(
                self._readiness_probe.wait_ready(
                    self.identity,
                    pid=process.pid,
                    runtime_version=self.executable.runtime_version,
                    deadline=deadline,
                ),
                deadline=deadline,
                timeout_message="Worker readiness handshake deadline expired",
            )
            if self._stop_requested.is_set():
                raise asyncio.CancelledError
            self._validate_readiness(readiness, process)
            if process.poll() is not None:
                raise WorkerStartupError("Worker exited before readiness was committed")
            await self._transition(WorkerLifecycle.READY, reason="authenticated readiness matched")
            self._process_started_at = self._clock.monotonic()
            return process
        except BaseException:
            if process is not None:
                await self._cleanup_pair(job, process, assigned=assigned, require_empty=False)
            else:
                job.close()
            if self._job is job:
                self._job = None
            if self._process is process:
                self._process = None
            self._process_started_at = None
            raise

    def _validate_readiness(self, readiness: WorkerReadiness, process: ManagedWorkerProcess) -> None:
        expected = self.identity
        if readiness.pid != process.pid:
            raise WorkerReadinessMismatch("readiness PID does not match the owned process")
        if readiness.runtime_version != self.executable.runtime_version:
            raise WorkerReadinessMismatch("readiness runtime version does not match the verified executable")
        if readiness.workspace_instance_id != expected.workspace_instance_id:
            raise WorkerReadinessMismatch("readiness workspace instance does not match")
        if readiness.canonical_root_identity != expected.canonical_root_identity:
            raise WorkerReadinessMismatch("readiness canonical root identity does not match")
        if readiness.database_identity != expected.database_identity:
            raise WorkerReadinessMismatch("readiness database identity does not match")

    async def _supervise(self, process: ManagedWorkerProcess | None) -> None:
        current = process
        while not self._stop_requested.is_set():
            if current is not None:
                try:
                    exit_code = await current.wait()
                except asyncio.CancelledError:
                    return
                async with self._operation_lock:
                    if self._stop_requested.is_set() or current is not self._process:
                        return
                    stable = self._was_stable()
                    await self._cleanup_launch(require_empty=False)
                    if exit_code == 0:
                        # The Worker executable returns zero only after its
                        # process-level waiter has observed a successful,
                        # durable transport teardown.  Treat that contract as
                        # an intentional application shutdown, not a crash to
                        # be automatically restarted.
                        await self._transition_to_stopped("Worker completed explicit shutdown")
                        await self._invalidate_leases_for_new_epoch()
                        self._consecutive_crashes = 0
                        self._release_worker_lock()
                        return
                    if stable:
                        self._consecutive_crashes = 0
                    await self._mark_crashed_locked("Worker process exited unexpectedly")
            if self._consecutive_crashes >= self._policy.crash_loop_limit:
                async with self._operation_lock:
                    if self.snapshot.state is WorkerLifecycle.CRASHED:
                        await self._transition(WorkerLifecycle.RECOVERING, reason="crash-loop circuit breaker")
                    if self.snapshot.state is WorkerLifecycle.RECOVERING:
                        await self._transition(WorkerLifecycle.DEGRADED, reason="crash-loop circuit breaker open")
                    self._release_worker_lock()
                return
            async with self._operation_lock:
                if self._stop_requested.is_set():
                    return
                if self.snapshot.state is WorkerLifecycle.CRASHED:
                    await self._transition(WorkerLifecycle.RECOVERING, reason="exponential-backoff restart")
                delay = self._policy.delay_for(self._consecutive_crashes, jitter_unit=self._jitter())
                restart_deadline = self._clock.utcnow() + timedelta(seconds=delay)
            if not await self._sleep_or_stop(restart_deadline):
                return
            async with self._operation_lock:
                if self._stop_requested.is_set():
                    return
                try:
                    current = await self._launch_once()
                except asyncio.CancelledError:
                    return
                except BaseException as error:
                    await self._cleanup_launch()
                    await self._mark_crashed_locked(f"restart failed: {type(error).__name__}")
                    current = None
            if current is not None:
                await self._reconcile_activity()

    def _start_recovery_task(self, process: ManagedWorkerProcess | None) -> None:
        old_task = self._supervision_task
        if old_task is not None and not old_task.done():
            old_task.cancel()
        self._supervision_task = asyncio.create_task(
            self._supervise(process),
            name=f"offeragent-worker-supervisor-{self.identity.workspace_instance_id}",
        )

    async def _mark_crashed_locked(self, reason: str) -> None:
        state = self.snapshot.state
        if state is not WorkerLifecycle.CRASHED:
            await self._transition(WorkerLifecycle.CRASHED, reason=reason)
        self._consecutive_crashes += 1
        await self._invalidate_leases_for_new_epoch()

    def _was_stable(self) -> bool:
        started_at = self._process_started_at
        return started_at is not None and self._clock.monotonic() - started_at >= self._policy.stable_reset_seconds

    async def _cleanup_launch(self, *, require_empty: bool = False) -> None:
        process = self._process
        job = self._job
        self._process = None
        self._job = None
        self._process_started_at = None
        if process is None and job is None:
            return
        if process is None:
            assert job is not None
            job.close()
            return
        if job is None:
            if process.poll() is None:
                process.terminate(0xEE)
            process.close()
            return
        await self._cleanup_pair(job, process, assigned=True, require_empty=require_empty)

    async def _graceful_then_cleanup(self, *, require_empty: bool) -> None:
        process = self._process
        deadline = self._clock.utcnow() + timedelta(seconds=self._policy.shutdown_timeout_seconds)
        if process is not None and process.poll() is None:
            try:
                await self._deadline_only(
                    self._shutdown_control.reject_new_runs(
                        self.identity,
                        pid=process.pid,
                        deadline=deadline,
                    ),
                    deadline=deadline,
                )
                receipt = await self._deadline_only(
                    self._shutdown_control.request_graceful_shutdown(
                        self.identity,
                        pid=process.pid,
                        deadline=deadline,
                    ),
                    deadline=deadline,
                )
                if not receipt.safely_committed:
                    self._cleanup_errors.append("Worker graceful shutdown did not commit interrupted state")
            except TimeoutError:
                self._cleanup_errors.append("Worker graceful shutdown exceeded its deadline; forcing Job tree")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._cleanup_errors.append(f"Worker graceful shutdown failed: {type(error).__name__}")
        if self._job is not None:
            # Application shutdown can revoke its authenticated control plane
            # just before Host stop-all arrives.  A missing control receipt is
            # not permission to kill a Worker that is already completing its
            # own durable teardown.  Give the owned Job the remainder of the
            # original deadline to become empty; force it only after that
            # bounded grace window expires.
            remaining = max(0.001, (deadline - self._clock.utcnow()).total_seconds())
            try:
                if await self._job.wait_empty(remaining):
                    job = self._job
                    owned_process = self._process
                    self._job = None
                    self._process = None
                    self._process_started_at = None
                    job.close()
                    if owned_process is not None:
                        owned_process.close()
                    return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._cleanup_errors.append(f"graceful process exit confirmation failed: {type(error).__name__}")
        await self._cleanup_launch(require_empty=require_empty)

    async def _invalidate_leases_for_new_epoch(self) -> None:
        async with self._lease_lock:
            self._lease_epoch += 1
            for values in self._leases.values():
                values.clear()
            self._idle_generation += 1
            idle_task = self._idle_task
            self._idle_task = None
        if idle_task is not None and idle_task is not asyncio.current_task():
            idle_task.cancel()

    async def _cleanup_pair(
        self,
        job: WorkerJob,
        process: ManagedWorkerProcess,
        *,
        assigned: bool,
        require_empty: bool,
    ) -> None:
        empty = True
        try:
            if assigned:
                job.terminate_tree(0xEE)
                empty = await job.wait_empty(self._policy.shutdown_timeout_seconds)
            elif process.poll() is None:
                process.terminate(0xEE)
        except Exception as error:
            empty = False
            self._cleanup_errors.append(f"process-tree cleanup failed: {type(error).__name__}")
        finally:
            try:
                job.close()
            except BaseException as error:
                empty = False
                self._cleanup_errors.append(f"Job handle close failed: {type(error).__name__}")
            try:
                process.close()
            except BaseException as error:
                empty = False
                self._cleanup_errors.append(f"process handle close failed: {type(error).__name__}")
        if require_empty and not empty:
            raise WorkerTreeLeak("Worker Job Object did not confirm an empty process tree")

    async def _sleep_or_stop(self, deadline: datetime) -> bool:
        if deadline <= self._clock.utcnow():
            await asyncio.sleep(0)
            return not self._stop_requested.is_set()
        sleep = asyncio.create_task(self._clock.sleep_until(deadline))
        stopped = asyncio.create_task(self._stop_requested.wait())
        try:
            done, _ = await asyncio.wait({sleep, stopped}, return_when=asyncio.FIRST_COMPLETED)
            return sleep in done and not self._stop_requested.is_set()
        finally:
            for task in (sleep, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleep, stopped, return_exceptions=True)

    async def _before_deadline(
        self,
        operation: Awaitable[_DeadlineResult],
        *,
        deadline: datetime,
        timeout_message: str,
    ) -> _DeadlineResult:
        work = asyncio.ensure_future(operation)
        timeout = asyncio.create_task(self._clock.sleep_until(deadline))
        stopped = asyncio.create_task(self._stop_requested.wait())
        try:
            done, _ = await asyncio.wait({work, timeout, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return await work
            if stopped in done and self._stop_requested.is_set():
                raise asyncio.CancelledError
            raise TimeoutError(timeout_message)
        finally:
            for task in (work, timeout, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, timeout, stopped, return_exceptions=True)

    async def _deadline_only(
        self,
        operation: Awaitable[_DeadlineResult],
        *,
        deadline: datetime,
    ) -> _DeadlineResult:
        work = asyncio.ensure_future(operation)
        timeout = asyncio.create_task(self._clock.sleep_until(deadline))
        try:
            done, _ = await asyncio.wait({work, timeout}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return await work
            raise TimeoutError("Worker graceful shutdown deadline expired")
        finally:
            for task in (work, timeout):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, timeout, return_exceptions=True)

    async def _reconcile_activity(self, *, allow_degraded_recovery: bool = False) -> None:
        async with self._lease_lock:
            has_run = bool(self._leases[LeaseKind.ACTIVE_RUN] or self._leases[LeaseKind.BACKGROUND_JOB])
            has_any = any(self._leases.values())
            generation = self._idle_generation
        async with self._operation_lock:
            state = self.snapshot.state
            if state in {
                WorkerLifecycle.COLD,
                WorkerLifecycle.STARTING,
                WorkerLifecycle.STOPPING,
                WorkerLifecycle.STOPPED,
                WorkerLifecycle.CRASHED,
                WorkerLifecycle.RECOVERING,
            }:
                return
            if state is WorkerLifecycle.DEGRADED:
                if not allow_degraded_recovery:
                    return
                await self._transition(WorkerLifecycle.READY, reason="resume revalidation succeeded")
                state = WorkerLifecycle.READY
            if has_run:
                if state is not WorkerLifecycle.BUSY:
                    await self._transition(WorkerLifecycle.BUSY, reason="active Run or background lease")
                return
            if has_any:
                if state in {WorkerLifecycle.BUSY, WorkerLifecycle.IDLE}:
                    await self._transition(WorkerLifecycle.READY, reason="client or Web lease active")
                return
            if state is WorkerLifecycle.BUSY:
                await self._transition(WorkerLifecycle.READY, reason="all Run leases released")
                state = WorkerLifecycle.READY
            if state is not WorkerLifecycle.IDLE:
                await self._transition(WorkerLifecycle.IDLE, reason="no clients or active work")
            if self._idle_task is None or self._idle_task.done():
                idle_deadline = self._clock.utcnow() + timedelta(seconds=self._policy.idle_timeout_seconds)
                self._idle_task = asyncio.create_task(
                    self._idle_stop_after(generation, idle_deadline),
                    name=f"offeragent-worker-idle-{self.identity.workspace_instance_id}",
                )

    async def _idle_stop_after(self, generation: int, deadline: datetime) -> None:
        try:
            await self._clock.sleep_until(deadline)
            async with self._lease_lock:
                if generation != self._idle_generation or any(self._leases.values()):
                    return
            await self.stop(reason="idle timeout", permanent=False)
        except asyncio.CancelledError:
            return

    async def _transition(self, target: WorkerLifecycle, *, reason: str) -> LifecycleSnapshot:
        snapshot = await self._lifecycle.transition(
            target,
            expected_revision=self._lifecycle.snapshot.revision,
            reason=reason,
        )
        for listener in tuple(self._listeners):
            try:
                result = listener(snapshot)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                self._listener_errors.append(f"state listener failed: {type(error).__name__}")
        return snapshot

    async def _transition_to_stopped(self, reason: str) -> None:
        state = self.snapshot.state
        if state is WorkerLifecycle.STOPPED:
            return
        if state is WorkerLifecycle.CRASHED:
            await self._transition(WorkerLifecycle.STOPPED, reason=reason)
            return
        if state is WorkerLifecycle.COLD:
            await self._transition(WorkerLifecycle.STOPPED, reason=reason)
            return
        if state is not WorkerLifecycle.STOPPING:
            await self._transition(WorkerLifecycle.STOPPING, reason=reason)
        await self._transition(WorkerLifecycle.STOPPED, reason=reason)


class WorkerSupervisorRegistry:
    """In-process CAS registry with one-to-one workspace identity checks."""

    def __init__(self, factory: WorkerFactory) -> None:
        self._factory = factory
        self._workers: dict[tuple[str, str], WorkerSupervisor] = {}
        self._instance_index: dict[str, tuple[str, str]] = {}
        self._root_index: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, identity: SupervisedWorkspaceIdentity) -> tuple[WorkerSupervisor, bool]:
        key = identity.registry_key
        async with self._lock:
            existing = self._workers.get(key)
            if existing is not None:
                if existing.identity.database_identity != identity.database_identity:
                    raise WorkerIdentityConflict("database identity changed for an attached workspace")
                return existing, False
            instance_key = self._instance_index.get(identity.workspace_instance_id)
            root_key = self._root_index.get(identity.canonical_root_identity)
            if instance_key is not None and instance_key != key:
                raise WorkerIdentityConflict("workspace instance ID is already bound to another canonical root")
            if root_key is not None and root_key != key:
                raise WorkerIdentityConflict("canonical root is already bound to another workspace instance")
            worker = self._factory(identity)
            self._workers[key] = worker
            self._instance_index[identity.workspace_instance_id] = key
            self._root_index[identity.canonical_root_identity] = key
            return worker, True

    async def workers(self) -> tuple[WorkerSupervisor, ...]:
        async with self._lock:
            return tuple(self._workers[key] for key in sorted(self._workers))

    async def close_all(self) -> tuple[BaseException, ...]:
        workers = await self.workers()
        results = await asyncio.gather(*(worker.close() for worker in workers), return_exceptions=True)
        return tuple(result for result in results if isinstance(result, BaseException))


class HostSupervisor:
    """Current-user singleton that retains only opaque Worker health."""

    def __init__(
        self,
        *,
        workers: WorkerSupervisorRegistry,
        clock: Clock,
        lock_factory: LockFactory | None = None,
        current_user_sid: str | None = None,
    ) -> None:
        self._workers = workers
        self._clock = clock
        self._lock_factory = lock_factory or _process_lock_factory
        self._current_user_sid = current_user_sid
        self._lifecycle = LifecycleMachine.host(now=clock.utcnow)
        self._host_lock: ProcessLease | None = None
        self._start_lock = asyncio.Lock()
        self._health_lock = asyncio.Lock()
        self._attach_gate = asyncio.Condition()
        self._inflight_attaches = 0
        self._closing = False
        self._shutdown_errors: list[str] = []

    @property
    def snapshot(self) -> LifecycleSnapshot:
        return self._lifecycle.snapshot

    @property
    def shutdown_errors(self) -> tuple[str, ...]:
        return tuple(self._shutdown_errors)

    async def start(self) -> LifecycleSnapshot:
        async with self._start_lock:
            if self._closing:
                raise HostShuttingDown("Host shutdown has begun")
            if self.snapshot.state in {
                HostLifecycle.READY,
                HostLifecycle.DEGRADED,
                HostLifecycle.RESTARTING,
            }:
                return self.snapshot
            await self._host_transition(HostLifecycle.STARTING, reason="current-user Host bootstrap")
            try:
                name = host_mutex_name(sid=self._current_user_sid)
                lock = self._lock_factory(name)
                lock.acquire(timeout_ms=0)
                self._host_lock = lock
            except BaseException:
                await self._host_transition(HostLifecycle.STOPPED, reason="Host singleton acquisition failed")
                raise
            return await self._host_transition(HostLifecycle.READY, reason="Host singleton ready")

    async def attach(
        self,
        identity: SupervisedWorkspaceIdentity,
        *,
        client_id: str,
    ) -> WorkerAttachment:
        await self._enter_attach()
        lease: WorkerLease | None = None
        try:
            worker, created = await self._workers.get_or_create(identity)
            if created:
                worker.add_state_listener(self._worker_state_changed)
            await worker.start()
            lease = await worker.acquire_lease(LeaseKind.CLIENT, client_id)
            await self._refresh_health()
            return WorkerAttachment(worker, lease)
        except BaseException:
            if lease is not None:
                await lease.release()
            await self._refresh_health()
            raise
        finally:
            await self._leave_attach()

    async def suspend(self) -> None:
        workers = await self._workers.workers()
        await asyncio.gather(*(worker.suspend() for worker in workers))

    async def resume(self) -> tuple[ResumeValidation, ...]:
        workers = await self._workers.workers()
        results = tuple(await asyncio.gather(*(worker.resume() for worker in workers)))
        await self._refresh_health()
        return results

    async def health(self) -> tuple[WorkerHealth, ...]:
        workers = await self._workers.workers()
        return tuple(await asyncio.gather(*(worker.health() for worker in workers)))

    async def shutdown(self) -> None:
        async with self._start_lock:
            if self.snapshot.state is HostLifecycle.STOPPED:
                return
            async with self._attach_gate:
                self._closing = True
                await self._attach_gate.wait_for(lambda: self._inflight_attaches == 0)
            async with self._health_lock:
                if self.snapshot.state is HostLifecycle.ABSENT:
                    await self._host_transition(HostLifecycle.STOPPED, reason="Host stopped before start")
                elif self.snapshot.state is not HostLifecycle.STOPPING:
                    await self._host_transition(HostLifecycle.STOPPING, reason="Host shutdown")
            errors = await self._workers.close_all()
            self._shutdown_errors.extend(type(error).__name__ for error in errors)
            lock = self._host_lock
            self._host_lock = None
            if lock is not None:
                lock.release()
            async with self._health_lock:
                if cast(HostLifecycle, self.snapshot.state) is not HostLifecycle.STOPPED:
                    await self._host_transition(HostLifecycle.STOPPED, reason="Host and Worker Jobs stopped")

    async def _enter_attach(self) -> None:
        async with self._attach_gate:
            if self._closing or self.snapshot.state in {
                HostLifecycle.ABSENT,
                HostLifecycle.STARTING,
                HostLifecycle.STOPPING,
                HostLifecycle.STOPPED,
            }:
                raise HostShuttingDown("Host is not accepting workspace attaches")
            self._inflight_attaches += 1

    async def _leave_attach(self) -> None:
        async with self._attach_gate:
            self._inflight_attaches -= 1
            if self._inflight_attaches < 0:
                self._inflight_attaches = 0
                raise AssertionError("Host attach gate underflow")
            self._attach_gate.notify_all()

    async def _worker_state_changed(self, snapshot: LifecycleSnapshot) -> None:
        del snapshot
        if not self._closing:
            await self._refresh_health()

    async def _refresh_health(self) -> None:
        if self._closing:
            return
        workers = await self._workers.workers()
        states = {worker.snapshot.state for worker in workers}
        desired = HostLifecycle.READY
        if WorkerLifecycle.DEGRADED in states:
            desired = HostLifecycle.DEGRADED
        elif states.intersection({WorkerLifecycle.CRASHED, WorkerLifecycle.RECOVERING}):
            desired = HostLifecycle.RESTARTING
        async with self._health_lock:
            current = self.snapshot.state
            if current not in {HostLifecycle.READY, HostLifecycle.DEGRADED, HostLifecycle.RESTARTING}:
                return
            if current is desired:
                return
            await self._host_transition(desired, reason="sanitized Worker health projection")

    async def _host_transition(self, target: HostLifecycle, *, reason: str) -> LifecycleSnapshot:
        return await self._lifecycle.transition(
            target,
            expected_revision=self._lifecycle.snapshot.revision,
            reason=reason,
        )


def _process_lock_factory(name: str) -> ProcessLease:
    return ProcessLock(name)


async def _await_cancelled(task: asyncio.Task[object]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass
    except BaseException:
        pass


__all__ = [
    "HostShuttingDown",
    "HostSupervisor",
    "JitterSource",
    "LeaseKind",
    "LockFactory",
    "ManagedWorkerProcess",
    "ProcessLease",
    "RestartPolicy",
    "ResumeValidation",
    "SupervisedWorkspaceIdentity",
    "SupervisorError",
    "VerifiedWorkerExecutable",
    "WorkerAttachment",
    "WorkerFactory",
    "WorkerHealth",
    "WorkerIdentityConflict",
    "WorkerJob",
    "WorkerJobBackend",
    "WorkerLaunchRequest",
    "WorkerLease",
    "WorkerProcessBackend",
    "WorkerReadiness",
    "WorkerReadinessMismatch",
    "WorkerReadinessProbe",
    "WorkerShutdownControl",
    "WorkerShutdownReceipt",
    "WorkerStartupError",
    "WorkerSupervisor",
    "WorkerSupervisorRegistry",
    "WorkerTreeLeak",
    "WorkerUnavailable",
]
