from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from offeragent_harness.models import ModelUsage
from offeragent_harness.ports import CancellationToken

from .state import RunState


@dataclass(frozen=True, slots=True)
class CompositionEvent:
    text_delta: str | None = None
    usage: ModelUsage | None = None

    def __post_init__(self) -> None:
        if (self.text_delta is None) == (self.usage is None):
            raise ValueError("composition event must contain exactly one of text_delta or usage")
        if self.text_delta == "":
            raise ValueError("empty text deltas are not allowed")


class Composer(Protocol):
    def stream(
        self,
        state: RunState,
        *,
        partial: bool,
        cancellation: CancellationToken,
    ) -> AsyncIterator[CompositionEvent]: ...


__all__ = ["Composer", "CompositionEvent"]
