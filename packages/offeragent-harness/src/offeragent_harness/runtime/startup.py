"""Worker startup barrier for durable Run recovery and canonical Loop resume."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from offeragent_harness.error_codes import RuntimeNotReadyCause

from .harness_service import HarnessService, RecoveryResumeRejected
from .recovery import RecoveryCoordinator, RecoveryDisposition
from .recovery_apply import RecoveryApplyResult, RecoveryPlanApplier
from .turn_manager import ActiveRun


class StartupFailurePhase(str, Enum):
    SCAN = "scan"
    APPLY = "apply"
    RESUME = "resume"
    SUBAGENT = "subagent"


class SubagentStartupRecovery(Protocol):
    async def recover(self) -> tuple[str, ...]: ...


class RuntimeStartupBlocked(RuntimeError, RuntimeNotReadyCause):
    """Startup cannot release any recovered Loop without operator/retry action."""

    def __init__(
        self,
        *,
        phase: StartupFailurePhase,
        run_id: str | None,
        applied_results: tuple[RecoveryApplyResult, ...],
        cause: Exception,
    ) -> None:
        self.phase = phase
        self.run_id = run_id
        self.applied_results = applied_results
        self.cause = cause
        target = "worker" if run_id is None else run_id
        super().__init__(f"runtime startup blocked during {phase.value} for {target}: {type(cause).__name__}: {cause}")


@dataclass(frozen=True, slots=True)
class RuntimeStartupReport:
    plans_scanned: int
    applied_results: tuple[RecoveryApplyResult, ...]
    active_runs: tuple[ActiveRun, ...]
    recovery_delivery_failures: int
    recovered_subagent_run_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.plans_scanned < 0 or self.plans_scanned != len(self.applied_results):
            raise ValueError("startup report plan/apply counts must match")
        expected_resumes = tuple(
            result.run.run_id for result in self.applied_results if result.disposition is RecoveryDisposition.RESUME
        )
        if tuple(active.run_id for active in self.active_runs) != expected_resumes:
            raise ValueError("startup report ActiveRuns must exactly match applied RESUME results")
        if self.recovery_delivery_failures < 0:
            raise ValueError("startup report delivery failure count cannot be negative")

    @property
    def resumed_run_ids(self) -> tuple[str, ...]:
        return tuple(active.run_id for active in self.active_runs)

    @property
    def terminalized_run_ids(self) -> tuple[str, ...]:
        return tuple(
            result.run.run_id for result in self.applied_results if result.disposition is not RecoveryDisposition.RESUME
        )

    @property
    def reconciled_run_ids(self) -> tuple[str, ...]:
        return tuple(result.run.run_id for result in self.applied_results if result.reconciled)

    @property
    def delivery_degraded(self) -> bool:
        return self.recovery_delivery_failures > 0


class RuntimeStartupCoordinator:
    """Apply every startup plan before releasing any existing Run into the Loop."""

    def __init__(
        self,
        *,
        recovery: RecoveryCoordinator,
        applier: RecoveryPlanApplier,
        harness: HarnessService,
        subagent_recovery: SubagentStartupRecovery | None = None,
    ) -> None:
        self._recovery = recovery
        self._applier = applier
        self._harness = harness
        self._subagent_recovery = subagent_recovery
        self._lock = asyncio.Lock()
        self._report: RuntimeStartupReport | None = None

    async def bootstrap(self) -> RuntimeStartupReport:
        async with self._lock:
            if self._report is not None:
                return self._report
            try:
                plans = await self._recovery.scan()
            except Exception as error:
                raise RuntimeStartupBlocked(
                    phase=StartupFailurePhase.SCAN,
                    run_id=None,
                    applied_results=(),
                    cause=error,
                ) from error

            applied: list[RecoveryApplyResult] = []
            for plan in plans:
                try:
                    applied.append(await self._applier.apply(plan))
                except Exception as error:
                    raise RuntimeStartupBlocked(
                        phase=StartupFailurePhase.APPLY,
                        run_id=plan.run_id,
                        applied_results=tuple(applied),
                        cause=error,
                    ) from error

            applied_results = tuple(applied)
            failures_before = len(self._harness.diagnostics.delivery_failures)
            await self._harness.publish_recovery_results(applied_results)
            failures_after = len(self._harness.diagnostics.delivery_failures)
            resume_results = tuple(
                result for result in applied_results if result.disposition is RecoveryDisposition.RESUME
            )
            recovered_subagents: tuple[str, ...] = ()

            async def recover_subagents() -> object:
                nonlocal recovered_subagents
                if self._subagent_recovery is not None:
                    recovered_subagents = await self._subagent_recovery.recover()
                return recovered_subagents

            try:
                active_runs = await self._harness.resume_recovered_runs(
                    resume_results,
                    before_release=recover_subagents,
                )
            except Exception as error:
                run_id = error.run_id if isinstance(error, RecoveryResumeRejected) else None
                phase = (
                    StartupFailurePhase.SUBAGENT
                    if isinstance(error, RecoveryResumeRejected) and error.code == "subagent_recovery_failed"
                    else StartupFailurePhase.RESUME
                )
                raise RuntimeStartupBlocked(
                    phase=phase,
                    run_id=run_id,
                    applied_results=applied_results,
                    cause=error,
                ) from error
            report = RuntimeStartupReport(
                plans_scanned=len(plans),
                applied_results=applied_results,
                active_runs=active_runs,
                recovery_delivery_failures=failures_after - failures_before,
                recovered_subagent_run_ids=recovered_subagents,
            )
            self._report = report
            return report

    async def start(self) -> RuntimeStartupReport:
        """Alias used by Worker composition roots."""

        return await self.bootstrap()


__all__ = [
    "RuntimeStartupBlocked",
    "RuntimeStartupCoordinator",
    "RuntimeStartupReport",
    "StartupFailurePhase",
    "SubagentStartupRecovery",
]
