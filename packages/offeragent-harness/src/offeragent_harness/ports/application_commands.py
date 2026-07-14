"""Ready-gated application command boundary used by local transports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .cancellation import CancellationToken


@dataclass(frozen=True, slots=True)
class ApplicationCommandContext:
    """Authenticated local-transport facts, never supplied by request JSON."""

    transport: str = "windows-named-pipe"
    client_id: str = "local-client"
    peer: str = "local"

    def __post_init__(self) -> None:
        if self.transport not in {"windows-named-pipe", "loopback-http", "loopback-websocket", "stdio-dev"}:
            raise ValueError("application command transport is invalid")
        if not self.client_id or not self.peer:
            raise ValueError("application command client identity must not be empty")


@runtime_checkable
class ApplicationCommandDispatcher(Protocol):
    """The only application surface a Pipe/Loopback transport may invoke.

    Concrete composition roots are responsible for adapting this port to the one
    ready ``HarnessApplication``.  A transport never owns a Harness, Agent Loop,
    model, tool registry, or persistence implementation.
    """

    def require_ready(self) -> None:
        """Raise when startup recovery has not opened the application gate."""

    async def dispatch(
        self,
        method: str,
        params: Mapping[str, Any],
        cancellation: CancellationToken,
        *,
        context: ApplicationCommandContext | None = None,
    ) -> object:
        """Dispatch one validated application command to the ready application."""


__all__ = ["ApplicationCommandContext", "ApplicationCommandDispatcher"]
