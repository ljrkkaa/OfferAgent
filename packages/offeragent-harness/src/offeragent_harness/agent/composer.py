from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from offeragent_harness.models import ModelUsage
from offeragent_harness.ports import CancellationToken

from .state import RunState


@dataclass(frozen=True, slots=True)
class CompositionRetry:
    request_id: str
    retry_of_request_id: str
    projection: str
    projection_hash: str | None = None
    omitted_context_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_id or not self.retry_of_request_id or not self.projection:
            raise ValueError("composition retry identity and projection must not be empty")
        if self.request_id == self.retry_of_request_id:
            raise ValueError("composition retry requires a new request_id")
        if len(self.omitted_context_ids) != len(set(self.omitted_context_ids)):
            raise ValueError("composition retry omitted context IDs must be unique")


@dataclass(frozen=True, slots=True)
class CompositionEvent:
    text_delta: str | None = None
    reasoning_summary_delta: str | None = None
    usage: ModelUsage | None = None
    retry: CompositionRetry | None = None

    def __post_init__(self) -> None:
        if (
            sum(value is not None for value in (self.text_delta, self.reasoning_summary_delta, self.usage, self.retry))
            != 1
        ):
            raise ValueError("composition event must contain exactly one payload")
        if self.text_delta == "":
            raise ValueError("empty text deltas are not allowed")
        if self.reasoning_summary_delta == "":
            raise ValueError("empty reasoning summary deltas are not allowed")


class Composer(Protocol):
    def stream(
        self,
        state: RunState,
        *,
        partial: bool,
        cancellation: CancellationToken,
    ) -> AsyncIterator[CompositionEvent]: ...


__all__ = ["Composer", "CompositionEvent", "CompositionRetry"]
