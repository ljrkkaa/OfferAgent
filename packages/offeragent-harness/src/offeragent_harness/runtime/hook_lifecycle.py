"""Worker lifecycle adapter for Hook events outside an AgentRun."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from offeragent_harness.error_codes import PolicyDeniedCause
from offeragent_harness.hooks import (
    HookDecision,
    HookEvent,
    HookExecutionContext,
    HookInvocation,
    HookOutcome,
)
from offeragent_harness.ports import CancellationToken, HookLifecyclePort


class LifecycleHookDenied(RuntimeError, PolicyDeniedCause):
    def __init__(self, event: HookEvent, decision: HookDecision) -> None:
        self.event = event
        self.decision = decision
        super().__init__(f"{event.value} Hook returned {decision.value}")


class WorkerLifecycleHooks:
    """Bind Session/Worker events to the same HookLifecyclePort as Agent tools."""

    def __init__(self, hooks: HookLifecyclePort, context: HookExecutionContext) -> None:
        self._hooks = hooks
        self._context = context

    async def session_start(
        self,
        *,
        connection_id: str,
        cancellation: CancellationToken,
    ) -> HookOutcome:
        outcome = await self._invoke(
            HookEvent.SESSION_START,
            f"session-start:{self._context.session_id}:{connection_id}",
            f"session:{self._context.session_id}",
            None,
            {"connectionId": connection_id},
            cancellation,
        )
        if outcome.decision is not HookDecision.CONTINUE:
            raise LifecycleHookDenied(HookEvent.SESSION_START, outcome.decision)
        return outcome

    async def runtime_shutdown(
        self,
        *,
        shutdown_id: str,
        reason_code: str,
        cancellation: CancellationToken,
    ) -> HookOutcome:
        # Shutdown cannot be vetoed indefinitely. deny/ask are audited while
        # the Host retains ownership of process termination.
        return await self._invoke(
            HookEvent.RUNTIME_SHUTDOWN,
            f"runtime-shutdown:{shutdown_id}",
            f"runtime:{shutdown_id}",
            None,
            {"reasonCode": reason_code},
            cancellation,
        )

    async def _invoke(
        self,
        event: HookEvent,
        invocation_id: str,
        chain_id: str,
        run_id: str | None,
        facts: Mapping[str, Any],
        cancellation: CancellationToken,
    ) -> HookOutcome:
        return await self._hooks.invoke(
            HookInvocation(
                invocation_id=invocation_id,
                chain_id=chain_id,
                event=event,
                context=self._context,
                run_id=run_id,
                facts=facts,
            ),
            cancellation,
        )


__all__ = ["LifecycleHookDenied", "WorkerLifecycleHooks"]
