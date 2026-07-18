"""Optional asynchronous preparation hook owned by the canonical Agent Loop."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol

from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.ports.cancellation import CancellationToken

from .state import RunPhase, RunState


class RunPreparationFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        error_code: ErrorCode = ErrorCode.INTERNAL_ERROR,
        failure_category: str = "runtime",
        details: Mapping[str, object] | None = None,
    ) -> None:
        if not code or not message:
            raise ValueError("Run preparation failures require a code and message")
        if failure_category not in {"budget", "model", "runtime", "tool"}:
            raise ValueError("Run preparation failure category is invalid")
        self.code = code
        self.retryable = retryable
        self.error_code = error_code
        self.failure_category = failure_category
        self.details = MappingProxyType(dict(details or {}))
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
