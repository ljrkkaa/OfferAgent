"""Immutable, capability-filtered Tool Registry snapshots."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from offeragent_harness.error_codes import ResourceNotFoundCause

from .canonical import canonical_json_sha256
from .definitions import PreflightMode, ResultSensitivity, ToolDefinition


class ToolRegistryError(LookupError):
    pass


class DuplicateToolDefinition(ToolRegistryError):
    pass


class ToolPreflightUnavailable(ToolRegistryError):
    pass


class ToolResultSensitivityUnavailable(ToolRegistryError):
    pass


class ToolNotFound(ToolRegistryError, ResourceNotFoundCause):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"tool {name!r} is not registered")


class ToolVersionUnavailable(ToolRegistryError):
    def __init__(self, name: str, version: str, available_versions: tuple[str, ...]) -> None:
        self.name = name
        self.version = version
        self.available_versions = available_versions
        super().__init__(f"tool {name!r} version {version!r} is unavailable; registered={available_versions!r}")


class ToolCapabilityUnavailable(ToolRegistryError):
    def __init__(self, definition: ToolDefinition, missing_capabilities: frozenset[str]) -> None:
        self.definition = definition
        self.missing_capabilities = missing_capabilities
        super().__init__(
            f"tool {definition.name!r}@{definition.version} requires unavailable capabilities "
            f"{sorted(missing_capabilities)!r}"
        )


class ToolRegistry:
    """A Run-stable registry snapshot; definitions cannot be added in place."""

    snapshot_id: str
    snapshot_hash: str
    _definitions: tuple[ToolDefinition, ...]
    _by_key: Mapping[tuple[str, str], ToolDefinition]
    _by_name: Mapping[str, tuple[ToolDefinition, ...]]
    _frozen: bool
    __slots__ = ("_by_key", "_by_name", "_definitions", "_frozen", "snapshot_hash", "snapshot_id")

    def __init__(
        self,
        snapshot_id: str,
        definitions: Iterable[ToolDefinition],
        *,
        preflight_provider_ids: frozenset[str] = frozenset(),
    ) -> None:
        if not snapshot_id:
            raise ValueError("registry snapshot_id must not be empty")
        by_key: dict[tuple[str, str], ToolDefinition] = {}
        by_name: dict[str, list[ToolDefinition]] = {}
        for definition in definitions:
            if definition.result_sensitivity is ResultSensitivity.UNKNOWN:
                raise ToolResultSensitivityUnavailable(
                    f"tool {definition.name!r}@{definition.version} has no immutable result sensitivity"
                )
            if definition.preflight_mode is PreflightMode.REQUIRED and (
                definition.preflight_provider not in preflight_provider_ids
            ):
                raise ToolPreflightUnavailable(
                    f"tool {definition.name!r}@{definition.version} requires unavailable preflight provider "
                    f"{definition.preflight_provider!r}"
                )
            key = (definition.name, definition.version)
            if key in by_key:
                raise DuplicateToolDefinition(f"duplicate tool definition {definition.name!r}@{definition.version}")
            by_key[key] = definition
            by_name.setdefault(definition.name, []).append(definition)
        ordered = tuple(sorted(by_key.values(), key=lambda item: (item.name, item.version)))
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "_definitions", ordered)
        object.__setattr__(self, "_by_key", MappingProxyType(dict(by_key)))
        object.__setattr__(
            self,
            "_by_name",
            MappingProxyType(
                {name: tuple(sorted(items, key=lambda item: item.version)) for name, items in sorted(by_name.items())}
            ),
        )
        object.__setattr__(
            self,
            "snapshot_hash",
            canonical_json_sha256(
                {
                    "snapshotId": snapshot_id,
                    "definitions": [self._definition_identity(item) for item in ordered],
                }
            ),
        )
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_frozen", False):
            raise AttributeError("ToolRegistry snapshots are immutable")
        object.__setattr__(self, name, value)

    @staticmethod
    def _definition_identity(definition: ToolDefinition) -> Mapping[str, object]:
        return {
            "name": definition.name,
            "version": definition.version,
            "description": definition.description,
            "inputSchema": definition.input_schema,
            "outputSchema": definition.output_schema,
            "executorLocation": definition.executor_location.value,
            "risk": definition.risk.value,
            "sideEffectClass": definition.side_effect_class.value,
            "requiredCapabilities": sorted(definition.required_capabilities),
            "concurrencySafe": definition.concurrency_safe,
            "idempotent": definition.idempotent,
            "retryable": definition.retryable,
            "timeoutMs": definition.timeout_ms,
            "outputLimitBytes": definition.output_limit_bytes,
            "preflightMode": definition.preflight_mode.value,
            "preflightProvider": definition.preflight_provider,
            "approvalEvidence": definition.approval_evidence.value,
            "resultSensitivity": definition.result_sensitivity.value,
        }

    def __len__(self) -> int:
        return len(self._definitions)

    def __iter__(self) -> Iterator[ToolDefinition]:
        return iter(self._definitions)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    def versions(self, name: str) -> tuple[str, ...]:
        return tuple(item.version for item in self._by_name.get(name, ()))

    def get(self, name: str, version: str) -> ToolDefinition:
        definitions = self._by_name.get(name)
        if definitions is None:
            raise ToolNotFound(name)
        definition = self._by_key.get((name, version))
        if definition is None:
            raise ToolVersionUnavailable(name, version, tuple(item.version for item in definitions))
        return definition

    def resolve(self, name: str, version: str, capabilities: frozenset[str]) -> ToolDefinition:
        definition = self.get(name, version)
        missing = definition.required_capabilities - capabilities
        if missing:
            raise ToolCapabilityUnavailable(definition, frozenset(missing))
        return definition

    def catalog(
        self,
        capabilities: frozenset[str],
        *,
        allowed_tools: frozenset[str] | None = None,
    ) -> tuple[ToolDefinition, ...]:
        return tuple(
            definition
            for definition in self._definitions
            if definition.required_capabilities <= capabilities
            and (allowed_tools is None or definition.name in allowed_tools)
        )


ToolRegistrySnapshot = ToolRegistry

__all__ = [
    "DuplicateToolDefinition",
    "ToolCapabilityUnavailable",
    "ToolNotFound",
    "ToolPreflightUnavailable",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolRegistrySnapshot",
    "ToolResultSensitivityUnavailable",
    "ToolVersionUnavailable",
]
