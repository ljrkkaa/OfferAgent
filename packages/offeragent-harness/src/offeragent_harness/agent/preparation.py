"""Optional asynchronous preparation hook owned by the canonical Agent Loop."""

from __future__ import annotations

from typing import Protocol

from offeragent_harness.ports.cancellation import CancellationToken

from .state import RunPhase, RunState


class RunPreparationFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        if not code or not message:
            raise ValueError("Run preparation failures require a code and message")
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class RunPreparationPort(Protocol):
    """Prepare model context after the Loop durably enters a preparation phase."""

    async def prepare(
        self,
        state: RunState,
        phase: RunPhase,
        cancellation: CancellationToken,
    ) -> None: ...


__all__ = ["RunPreparationFailure", "RunPreparationPort"]
