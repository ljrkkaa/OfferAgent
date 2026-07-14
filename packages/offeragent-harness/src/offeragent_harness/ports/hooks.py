"""Ports through which the unique Harness invokes lifecycle Hooks."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from offeragent_harness.hooks import HookDefinition, HookInvocation, HookLayer, HookOutcome, HookOutput

from .cancellation import CancellationToken


@runtime_checkable
class HookLifecyclePort(Protocol):
    async def invoke(self, invocation: HookInvocation, cancellation: CancellationToken) -> HookOutcome: ...


@runtime_checkable
class HookLayerSource(Protocol):
    async def layers_for(self, invocation: HookInvocation) -> Sequence[HookLayer]: ...


@runtime_checkable
class HookHandler(Protocol):
    async def invoke(
        self,
        definition: HookDefinition,
        invocation: HookInvocation,
        cancellation: CancellationToken,
    ) -> HookOutput: ...


__all__ = ["HookHandler", "HookLayerSource", "HookLifecyclePort"]
