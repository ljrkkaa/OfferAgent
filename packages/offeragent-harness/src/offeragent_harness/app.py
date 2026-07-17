from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from offeragent_harness import __version__
from offeragent_harness.error_codes import RuntimeNotReadyCause
from offeragent_harness.ports import Clock, EventSink, HookLifecyclePort, IdGenerator, UnitOfWorkFactory
from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.runtime import (
    BufferedEventSink,
    RecoveryCoordinator,
    RecoveryLookup,
    RecoveryPlanApplier,
    RuntimeStartupCoordinator,
    RuntimeStartupReport,
    TurnManager,
)
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.runtime.harness_service import HarnessService, HookContextFactory, RunComponentsFactory
from offeragent_harness.tools.registry import ToolRegistry


class ApplicationNotReady(RuntimeError, RuntimeNotReadyCause):
    pass


@dataclass(frozen=True, slots=True)
class ApplicationIdentity:
    runtime_version: str
    core_version: str
    protocol_version: str
    schema_hash: str


@dataclass(slots=True)
class HarnessApplication:
    """Cold composition root; transports may only use :meth:`require_ready`."""

    identity: ApplicationIdentity
    _harness: HarnessService
    event_sink: BufferedEventSink
    startup_coordinator: RuntimeStartupCoordinator | None = None
    startup_report: RuntimeStartupReport | None = field(default=None, init=False)
    _start_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _stopped: bool = field(default=False, init=False, repr=False)

    @property
    def ready(self) -> bool:
        return self.startup_report is not None and not self._stopped

    @property
    def harness(self) -> HarnessService:
        """Ready-gated Harness surface exposed to local transports."""

        return self.require_ready()

    async def start(self) -> RuntimeStartupReport:
        """Complete the recovery barrier before the Worker can expose transports."""

        async with self._start_lock:
            if self._stopped:
                raise ApplicationNotReady("application has already stopped")
            if self.startup_report is not None:
                return self.startup_report
            if self.startup_coordinator is None:
                raise ApplicationNotReady("cold application has no configured RuntimeStartupCoordinator")
            report = await self.startup_coordinator.bootstrap()
            self.startup_report = report
            return report

    def require_ready(self) -> HarnessService:
        """Readiness gate every stdio or Loopback adapter must cross."""

        if not self.ready:
            raise ApplicationNotReady("Worker startup recovery has not completed")
        return self._harness

    async def shutdown(self, *, grace_seconds: float = 10.0) -> None:
        self._stopped = True
        await self._harness.shutdown(grace_seconds=grace_seconds)
        await self.event_sink.close(timeout_seconds=grace_seconds)


def create_application(
    *,
    unit_of_work: UnitOfWorkFactory,
    event_sink: EventSink,
    clock: Clock,
    ids: IdGenerator,
    components: RunComponentsFactory,
    turn_manager: TurnManager | None = None,
    approval_manager: ApprovalManager | None = None,
    recovery_registry: ToolRegistry | None = None,
    recovery_lookup: RecoveryLookup | None = None,
    hooks: HookLifecyclePort | None = None,
    hook_context_factory: HookContextFactory | None = None,
) -> HarnessApplication:
    """Construct a cold application; this function never implies readiness."""

    if recovery_registry is None and recovery_lookup is not None:
        raise ValueError("recovery_lookup requires recovery_registry")
    identity = ApplicationIdentity(
        runtime_version=__version__,
        core_version=__version__,
        protocol_version=PROTOCOL_VERSION,
        schema_hash=schema_hash(),
    )
    buffered_sink = BufferedEventSink(event_sink)
    harness = HarnessService(
        unit_of_work=unit_of_work,
        event_sink=buffered_sink,
        clock=clock,
        ids=ids,
        components=components,
        turn_manager=turn_manager,
        approval_manager=approval_manager,
        hooks=hooks,
        hook_context_factory=hook_context_factory,
    )
    startup_coordinator = (
        None
        if recovery_registry is None
        else RuntimeStartupCoordinator(
            recovery=RecoveryCoordinator(
                unit_of_work=unit_of_work,
                registry=recovery_registry,
                lookup=recovery_lookup,
                clock=clock,
            ),
            applier=RecoveryPlanApplier(
                unit_of_work=unit_of_work,
                clock=clock,
                ids=ids,
            ),
            harness=harness,
        )
    )
    return HarnessApplication(
        identity=identity,
        _harness=harness,
        event_sink=buffered_sink,
        startup_coordinator=startup_coordinator,
    )


async def start_application(
    *,
    unit_of_work: UnitOfWorkFactory,
    event_sink: EventSink,
    clock: Clock,
    ids: IdGenerator,
    components: RunComponentsFactory,
    recovery_registry: ToolRegistry,
    recovery_lookup: RecoveryLookup | None = None,
    turn_manager: TurnManager | None = None,
    approval_manager: ApprovalManager | None = None,
    hooks: HookLifecyclePort | None = None,
    hook_context_factory: HookContextFactory | None = None,
) -> HarnessApplication:
    """Production startup API: return only after recovery has made the Worker ready."""

    application = create_application(
        unit_of_work=unit_of_work,
        event_sink=event_sink,
        clock=clock,
        ids=ids,
        components=components,
        turn_manager=turn_manager,
        approval_manager=approval_manager,
        recovery_registry=recovery_registry,
        recovery_lookup=recovery_lookup,
        hooks=hooks,
        hook_context_factory=hook_context_factory,
    )
    try:
        await application.start()
    except BaseException:
        try:
            await application.shutdown()
        except BaseException as cleanup_error:
            application._harness.diagnostics.cleanup_failures.append(
                f"failed to clean cold application after startup error: {type(cleanup_error).__name__}: {cleanup_error}"
            )
        raise
    return application


__all__ = [
    "ApplicationIdentity",
    "ApplicationNotReady",
    "HarnessApplication",
    "create_application",
    "start_application",
]
