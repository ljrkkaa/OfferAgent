"""Optional asynchronous preparation hook owned by the canonical Agent Loop."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol

from offeragent_harness.error_codes import ErrorCode
from offeragent_harness.ports.cancellation import CancellationToken

from .state import RunPhase, RunState

_DETAIL_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


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


def safe_preparation_failure_details(error: RunPreparationFailure) -> dict[str, object]:
    """Project only bounded, explicitly public preparation discriminators to durable events."""

    projected: dict[str, object] = {}
    image_index = error.details.get("imageIndex")
    if type(image_index) is int and 0 <= image_index <= 255:
        projected["imageIndex"] = image_index
    for key in ("reason", "runBindingCode"):
        value = error.details.get(key)
        if isinstance(value, str) and _DETAIL_TOKEN.fullmatch(value):
            projected[key] = value
    model_id = error.details.get("modelId")
    if isinstance(model_id, str) and _MODEL_ID.fullmatch(model_id):
        projected["modelId"] = model_id
    return projected


__all__ = ["RunPreparationFailure", "RunPreparationPort", "safe_preparation_failure_details"]
