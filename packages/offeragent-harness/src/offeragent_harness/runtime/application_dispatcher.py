"""One validated Application Command dispatcher shared by every local transport."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Protocol, TypeAlias

from offeragent_harness.ports import ApplicationCommandContext, CancellationToken, OperationCancelled
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.messages import (
    COMMAND_REGISTRY,
    validate_command_params,
    validate_command_result,
)

from .application_errors import map_application_exception


class ReadyApplication(Protocol):
    """Narrow readiness surface implemented by the one HarnessApplication."""

    def require_ready(self) -> object: ...


ApplicationCommandHandler: TypeAlias = Callable[
    [WireModel, CancellationToken, ApplicationCommandContext],
    Awaitable[WireModel | Mapping[str, object]],
]


class CommandHandlerConfigurationError(ValueError):
    pass


class RuntimeApplicationCommandDispatcher:
    """Validates DTOs, enforces readiness, then invokes one complete handler table.

    A partial table is a composition error.  This makes an unavailable service
    visible during Worker construction instead of turning a public command into
    a runtime ``unsupported`` placeholder.
    """

    def __init__(
        self,
        *,
        application: ReadyApplication,
        handlers: Mapping[str, ApplicationCommandHandler],
    ) -> None:
        expected = frozenset(COMMAND_REGISTRY)
        actual = frozenset(handlers)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise CommandHandlerConfigurationError(f"incomplete command handlers: missing={missing}, extra={extra}")
        if any(not callable(handler) for handler in handlers.values()):
            raise CommandHandlerConfigurationError("every Application Command handler must be callable")
        self._application = application
        self._handlers: Mapping[str, ApplicationCommandHandler] = MappingProxyType(dict(handlers))

    @property
    def methods(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def require_ready(self) -> None:
        self._application.require_ready()

    async def dispatch(
        self,
        method: str,
        params: Mapping[str, object],
        cancellation: CancellationToken,
        *,
        context: ApplicationCommandContext | None = None,
    ) -> object:
        try:
            self.require_ready()
            cancellation.checkpoint()
            validated_params = validate_command_params(method, params)
            handler = self._handlers[method]
            result = await handler(
                validated_params,
                cancellation,
                context or ApplicationCommandContext(),
            )
            cancellation.checkpoint()
            raw_result = result.to_wire() if isinstance(result, WireModel) else dict(result)
            return validate_command_result(method, raw_result).to_wire()
        except (OperationCancelled, asyncio.CancelledError) as error:
            raise map_application_exception(error) from None
        except Exception as error:
            raise map_application_exception(error) from None


__all__ = [
    "ApplicationCommandHandler",
    "CommandHandlerConfigurationError",
    "ReadyApplication",
    "RuntimeApplicationCommandDispatcher",
]
