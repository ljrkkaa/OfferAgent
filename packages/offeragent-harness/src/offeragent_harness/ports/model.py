"""The sole model inference boundary."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from offeragent_harness.models import ModelEvent, ModelRequest

from .cancellation import CancellationToken


@runtime_checkable
class ModelGateway(Protocol):
    def stream(self, request: ModelRequest, cancellation: CancellationToken) -> AsyncIterator[ModelEvent]: ...


__all__ = ["ModelGateway"]
