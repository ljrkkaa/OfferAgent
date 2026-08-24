"""Generic, definition-driven preflight evidence and execution revalidation."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from offeragent_harness.error_codes import ResourceConflictCause
from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json

from .definitions import PreflightMode, ToolCall, ToolDefinition
from .results import ToolResult

if TYPE_CHECKING:
    from offeragent_harness.ports.cancellation import CancellationToken


class PreflightError(RuntimeError):
    pass


class PreflightConflict(PreflightError, ResourceConflictCause):
    def __init__(self, message: str, *, details: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        frozen = freeze_json(details or {})
        if not isinstance(frozen, FrozenJsonObject):
            raise TypeError("preflight conflict details must be a JSON object")
        self.details = frozen


class PreflightProviderUnavailable(PreflightError):
    pass


@dataclass(frozen=True)
class PreflightEvidence:
    provider_id: str
    state_hash: str
    artifact_ids: tuple[str, ...]
    lock_keys: tuple[str, ...]
    token: str
    facts: Mapping[str, object]

    def __post_init__(self) -> None:
        lock_keys = tuple(self.lock_keys)
        artifact_ids = tuple(self.artifact_ids)
        if re.fullmatch(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*", self.provider_id) is None:
            raise ValueError("preflight evidence requires a canonical provider ID")
        if not 1 <= len(self.token) <= 512:
            raise ValueError("preflight evidence requires provider and token identities")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.state_hash) is None:
            raise ValueError("preflight state_hash must be a canonical sha256 digest")
        if not 1 <= len(lock_keys) <= 256 or any(
            not isinstance(key, str) or not 1 <= len(key) <= 1024 for key in lock_keys
        ):
            raise ValueError("preflight evidence requires non-empty lock keys")
        if len(set(key.casefold() for key in lock_keys)) != len(lock_keys):
            raise ValueError("preflight lock keys must be case-insensitively unique")
        if len(artifact_ids) > 256 or any(
            not isinstance(artifact_id, str)
            or re.fullmatch(r"art_[A-Za-z0-9][A-Za-z0-9_-]{0,123}", artifact_id) is None
            for artifact_id in artifact_ids
        ):
            raise ValueError("preflight artifact IDs must be canonical and bounded")
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("preflight artifact IDs must be unique")
        frozen = freeze_json(self.facts)
        if not isinstance(frozen, FrozenJsonObject):
            raise TypeError("preflight evidence facts must be a JSON object")
        object.__setattr__(self, "lock_keys", lock_keys)
        object.__setattr__(self, "artifact_ids", artifact_ids)
        object.__setattr__(self, "facts", frozen)


@runtime_checkable
class PreflightProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    async def prepare(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        cancellation: CancellationToken,
    ) -> PreflightEvidence: ...

    async def revalidate(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        evidence: PreflightEvidence,
        cancellation: CancellationToken,
    ) -> None: ...

    async def complete(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        evidence: PreflightEvidence,
        result: ToolResult,
    ) -> None: ...


class PreflightRegistry:
    def __init__(self, providers: Iterable[PreflightProvider]) -> None:
        by_id: dict[str, PreflightProvider] = {}
        for provider in providers:
            if not provider.provider_id:
                raise ValueError("preflight provider ID cannot be empty")
            if provider.provider_id in by_id:
                raise ValueError(f"duplicate preflight provider {provider.provider_id!r}")
            by_id[provider.provider_id] = provider
        self._providers: Mapping[str, PreflightProvider] = MappingProxyType(by_id)

    @property
    def provider_ids(self) -> frozenset[str]:
        return frozenset(self._providers)

    def resolve(self, definition: ToolDefinition) -> PreflightProvider | None:
        if definition.preflight_mode is PreflightMode.NONE:
            return None
        provider_id = definition.preflight_provider
        assert provider_id is not None
        provider = self._providers.get(provider_id)
        if provider is None:
            raise PreflightProviderUnavailable(
                f"tool {definition.name!r}@{definition.version} requires unavailable preflight provider {provider_id!r}"
            )
        return provider


__all__ = [
    "PreflightConflict",
    "PreflightError",
    "PreflightEvidence",
    "PreflightProvider",
    "PreflightProviderUnavailable",
    "PreflightRegistry",
]
