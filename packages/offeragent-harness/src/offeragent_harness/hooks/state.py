"""Persistent Hook layer configuration and explicit trust decisions."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from offeragent_harness.ports.cancellation import CancellationToken
from offeragent_harness.ports.unit_of_work import UnitOfWorkFactory
from offeragent_harness.tools import canonical_json_sha256

from .models import (
    HookCommandSpec,
    HookDefinition,
    HookEvent,
    HookImplementation,
    HookLayer,
    HookScope,
)

_MAX_LAYERS = 512
_SAFE_ENVIRONMENT = frozenset({"LANG", "LC_ALL", "TZ", "TEMP", "TMP", "SystemRoot"})
_SENSITIVE_ENVIRONMENT = ("AUTH", "COOKIE", "CREDENTIAL", "KEY", "PASSWORD", "PROXY", "SECRET", "TOKEN")
_SCOPE_ORDER = {
    HookScope.MANAGED: 0,
    HookScope.USER: 1,
    HookScope.WORKSPACE: 2,
    HookScope.SESSION: 3,
}


class HookLayerTrust(str, Enum):
    SIGNED = "signed"
    CONFIRMED = "confirmed"
    CONFIRMATION_REQUIRED = "confirmation_required"
    WORKSPACE_TRUST = "workspace_trust"


@dataclass(frozen=True, slots=True)
class HookLayerRecord:
    workspace_id: str
    layer: HookLayer
    trust: HookLayerTrust
    content_hash: str
    command_confirmations: Mapping[str, str]
    revision: int
    idempotency_key: str

    def __post_init__(self) -> None:
        if not self.workspace_id or "\x00" in self.workspace_id or self.revision < 1:
            raise ValueError("Hook layer record identity/revision is invalid")
        if not self.idempotency_key or "\x00" in self.idempotency_key:
            raise ValueError("Hook layer idempotency key is invalid")
        if self.content_hash != hook_layer_hash(self.layer):
            raise ValueError("Hook layer content hash is invalid")
        confirmations = dict(self.command_confirmations)
        command_hashes = {
            hook.hook_id: hook_definition_hash(hook)
            for hook in self.layer.hooks
            if hook.implementation is HookImplementation.COMMAND
        }
        if any(command_hashes.get(hook_id) != digest for hook_id, digest in confirmations.items()):
            raise ValueError("Hook command confirmation is stale or not command-bound")
        if self.layer.scope is HookScope.MANAGED:
            if self.trust is not HookLayerTrust.SIGNED or confirmations:
                raise ValueError("managed Hook layer must be signed")
        elif self.layer.scope is HookScope.WORKSPACE:
            if self.trust is not HookLayerTrust.WORKSPACE_TRUST:
                raise ValueError("workspace Hook layer requires dynamic Workspace trust")
        elif self.trust not in {HookLayerTrust.CONFIRMED, HookLayerTrust.CONFIRMATION_REQUIRED}:
            raise ValueError("user/session Hook layer requires explicit confirmation")
        object.__setattr__(self, "command_confirmations", confirmations)


@dataclass(frozen=True, slots=True)
class HookCatalogSnapshot:
    workspace_id: str
    records: tuple[HookLayerRecord, ...]
    revision: int
    snapshot_hash: str

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.records, key=_record_key))
        if ordered != self.records or len(ordered) > _MAX_LAYERS:
            raise ValueError("Hook catalog records must be bounded and sorted")
        keys = [(item.layer.scope, item.layer.owner_id) for item in ordered]
        if len(keys) != len(set(keys)):
            raise ValueError("Hook catalog contains duplicate layer owners")
        if self.revision != sum(item.revision for item in ordered) or self.snapshot_hash != _snapshot_hash(ordered):
            raise ValueError("Hook catalog revision/hash is invalid")


class EntityHookConfigurationStore:
    """CAS store with one entity per Workspace/scope/owner layer."""

    def __init__(self, unit_of_work: UnitOfWorkFactory) -> None:
        self._unit_of_work = unit_of_work

    async def list(self, workspace_id: str) -> tuple[HookLayerRecord, ...]:
        records = []
        after_id: str | None = None
        while True:
            async with self._unit_of_work.begin() as unit_of_work:
                page = await unit_of_work.entities.list(_collection(workspace_id), after_id=after_id, limit=100)
            for entity in page:
                record = _parse_record(entity.value, workspace_id)
                if record.revision != entity.revision:
                    raise ValueError("Hook layer entity revision differs from its strict payload")
                records.append(record)
            if len(page) < 100:
                break
            after_id = page[-1].entity_id
        if len(records) > _MAX_LAYERS:
            raise ValueError("Hook catalog exceeds its layer limit")
        return tuple(sorted(records, key=_record_key))

    async def get(self, workspace_id: str, scope: HookScope, owner_id: str) -> HookLayerRecord | None:
        async with self._unit_of_work.begin() as unit_of_work:
            raw = await unit_of_work.entities.get(_collection(workspace_id), _entity_id(scope, owner_id))
        return None if raw is None else _parse_record(raw, workspace_id)

    async def put(
        self,
        workspace_id: str,
        layer: HookLayer,
        trust: HookLayerTrust,
        command_confirmations: Mapping[str, str],
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> HookLayerRecord:
        candidate = HookLayerRecord(
            workspace_id,
            layer,
            trust,
            hook_layer_hash(layer),
            dict(command_confirmations),
            expected_revision + 1,
            idempotency_key,
        )
        current = await self.get(workspace_id, layer.scope, layer.owner_id)
        if current is not None and current.idempotency_key == idempotency_key:
            if not _same_record_content(current, candidate):
                raise ValueError("Hook configuration idempotency key is bound to another layer")
            return current
        try:
            async with self._unit_of_work.begin() as unit_of_work:
                revision = await unit_of_work.entities.put(
                    _collection(workspace_id),
                    _entity_id(layer.scope, layer.owner_id),
                    _record_value(candidate),
                    expected_revision=expected_revision,
                )
                if revision != candidate.revision:
                    raise ValueError("Hook layer entity revision is inconsistent")
                await unit_of_work.commit()
            return candidate
        except Exception:
            recovered = await self.get(workspace_id, layer.scope, layer.owner_id)
            if (
                recovered is not None
                and recovered.idempotency_key == idempotency_key
                and _same_record_content(recovered, candidate)
            ):
                return recovered
            raise


class HookConfigurationService:
    """Own managed truth and user/workspace/session confirmation state."""

    def __init__(
        self,
        *,
        workspace_id: str,
        managed_layer: HookLayer,
        signed_builtin_handler_ids: frozenset[str],
        store: EntityHookConfigurationStore,
    ) -> None:
        if not workspace_id or workspace_id.strip() != workspace_id or "\x00" in workspace_id:
            raise ValueError("production Hook configuration requires a canonical Workspace ID")
        if managed_layer.scope is not HookScope.MANAGED:
            raise ValueError("production Hook configuration requires one managed layer")
        self.workspace_id = workspace_id
        self.managed_owner_id = managed_layer.owner_id
        self._managed = managed_layer
        self._handler_ids = frozenset(signed_builtin_handler_ids)
        self._store = store
        self._records: dict[tuple[HookScope, str], HookLayerRecord] = {}
        self._snapshot: HookCatalogSnapshot | None = None
        self._lock = asyncio.Lock()
        self._validate_layer(managed_layer)

    @property
    def initialized(self) -> bool:
        return self._snapshot is not None

    @property
    def snapshot(self) -> HookCatalogSnapshot:
        if self._snapshot is None:
            raise RuntimeError("Hook configuration service is not initialized")
        return self._snapshot

    async def initialize(self, cancellation: CancellationToken) -> HookCatalogSnapshot:
        cancellation.checkpoint()
        async with self._lock:
            if self._snapshot is not None:
                return self._snapshot
            records = await self._store.list(self.workspace_id)
            cancellation.checkpoint()
            for item in records:
                if item.layer.scope is HookScope.MANAGED:
                    continue
                if item.layer.scope is HookScope.WORKSPACE and item.layer.owner_id != self.workspace_id:
                    raise ValueError("persisted workspace Hook owner differs from the canonical Workspace ID")
                self._validate_layer(item.layer)
            by_key = {(item.layer.scope, item.layer.owner_id): item for item in records}
            unexpected_managed = [
                item
                for item in records
                if item.layer.scope is HookScope.MANAGED and item.layer.owner_id != self.managed_owner_id
            ]
            if unexpected_managed:
                raise ValueError("persisted Hook catalog contains an unexpected managed owner")
            key = (HookScope.MANAGED, self.managed_owner_id)
            current = by_key.get(key)
            if current is None or current.content_hash != hook_layer_hash(self._managed):
                persisted = await self._store.put(
                    self.workspace_id,
                    self._managed,
                    HookLayerTrust.SIGNED,
                    {},
                    expected_revision=0 if current is None else current.revision,
                    idempotency_key=(
                        f"hook-managed:{self.workspace_id}:" + hook_layer_hash(self._managed).removeprefix("sha256:")
                    ),
                )
                by_key[key] = persisted
            self._set_snapshot(tuple(by_key.values()))
            return self.snapshot

    async def install_layer(
        self,
        layer: HookLayer,
        *,
        expected_revision: int,
        idempotency_key: str,
        cancellation: CancellationToken,
    ) -> HookLayerRecord:
        if layer.scope is HookScope.MANAGED:
            raise ValueError("managed Hook configuration comes only from the signed runtime")
        if layer.scope is HookScope.WORKSPACE and layer.owner_id != self.workspace_id:
            raise ValueError("workspace Hook owner must equal the canonical Workspace ID")
        self._validate_layer(layer)
        cancellation.checkpoint()
        async with self._lock:
            self._require_initialized()
            key = (layer.scope, layer.owner_id)
            current = self._records.get(key)
            if current is not None and current.idempotency_key == idempotency_key:
                if current.layer != layer:
                    raise ValueError("Hook configuration idempotency key is bound to another layer")
                return current
            actual = 0 if current is None else current.revision
            if actual != expected_revision:
                raise ValueError(f"Hook layer expected revision {expected_revision}, actual {actual}")
            expected_layer_revision = 1 if current is None else current.layer.revision + 1
            if layer.revision != expected_layer_revision:
                raise ValueError(f"Hook layer definition revision {layer.revision}, expected {expected_layer_revision}")
            if layer.scope is HookScope.WORKSPACE:
                confirmations = _preserved_command_confirmations(current, layer)
                trust = HookLayerTrust.WORKSPACE_TRUST
            else:
                confirmations = {}
                trust = HookLayerTrust.CONFIRMATION_REQUIRED
            record = await self._store.put(
                self.workspace_id,
                layer,
                trust,
                confirmations,
                expected_revision=actual,
                idempotency_key=idempotency_key,
            )
            cancellation.checkpoint()
            self._replace_record(record)
            return record

    async def confirm_layer(
        self,
        scope: HookScope,
        owner_id: str,
        content_hash: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        cancellation: CancellationToken,
    ) -> HookLayerRecord:
        if scope not in {HookScope.USER, HookScope.SESSION}:
            raise ValueError("only user/session Hook layers use whole-layer confirmation")
        cancellation.checkpoint()
        async with self._lock:
            replay = self._records.get((scope, owner_id))
            if replay is not None and replay.idempotency_key == idempotency_key:
                if replay.content_hash != content_hash or replay.trust is not HookLayerTrust.CONFIRMED:
                    raise ValueError("Hook confirmation idempotency key is bound to another decision")
                return replay
            record = self._record(scope, owner_id, expected_revision)
            if record.content_hash != content_hash:
                raise ValueError("Hook layer confirmation is bound to another content hash")
            updated = await self._store.put(
                self.workspace_id,
                record.layer,
                HookLayerTrust.CONFIRMED,
                {},
                expected_revision=record.revision,
                idempotency_key=idempotency_key,
            )
            cancellation.checkpoint()
            self._replace_record(updated)
            return updated

    async def confirm_workspace_command(
        self,
        owner_id: str,
        hook_id: str,
        definition_hash: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        cancellation: CancellationToken,
    ) -> HookLayerRecord:
        cancellation.checkpoint()
        async with self._lock:
            replay = self._records.get((HookScope.WORKSPACE, owner_id))
            if replay is not None and replay.idempotency_key == idempotency_key:
                if replay.command_confirmations.get(hook_id) != definition_hash:
                    raise ValueError("Hook command idempotency key is bound to another decision")
                return replay
            record = self._record(HookScope.WORKSPACE, owner_id, expected_revision)
            definition = next((item for item in record.layer.hooks if item.hook_id == hook_id), None)
            if definition is None or definition.implementation is not HookImplementation.COMMAND:
                raise ValueError("workspace command Hook is unavailable")
            if hook_definition_hash(definition) != definition_hash:
                raise ValueError("workspace command approval is bound to another definition hash")
            confirmations = dict(record.command_confirmations)
            confirmations[hook_id] = definition_hash
            updated = await self._store.put(
                self.workspace_id,
                record.layer,
                record.trust,
                confirmations,
                expected_revision=record.revision,
                idempotency_key=idempotency_key,
            )
            cancellation.checkpoint()
            self._replace_record(updated)
            return updated

    def effective_layers(
        self,
        *,
        principal_id: str,
        session_id: str,
        workspace_trusted: bool,
    ) -> tuple[HookLayer, ...]:
        self._require_initialized()
        selected: list[HookLayer] = []
        expected = (
            (HookScope.MANAGED, self.managed_owner_id),
            (HookScope.USER, principal_id),
            (HookScope.WORKSPACE, self.workspace_id),
            (HookScope.SESSION, session_id),
        )
        for scope, owner_id in expected:
            record = self._records.get((scope, owner_id))
            if record is None:
                continue
            if scope in {HookScope.USER, HookScope.SESSION} and record.trust is not HookLayerTrust.CONFIRMED:
                continue
            if scope is HookScope.WORKSPACE:
                if not workspace_trusted:
                    continue
                hooks = tuple(
                    hook
                    for hook in record.layer.hooks
                    if hook.implementation is HookImplementation.BUILTIN
                    or record.command_confirmations.get(hook.hook_id) == hook_definition_hash(hook)
                )
                selected.append(
                    HookLayer(
                        scope,
                        owner_id,
                        record.layer.revision,
                        hooks,
                        record.layer.denied_events,
                        record.layer.denied_hook_ids,
                    )
                )
            else:
                selected.append(record.layer)
        return tuple(selected)

    def _validate_layer(self, layer: HookLayer) -> None:
        if layer.scope is not HookScope.MANAGED and layer.revision < 1:
            raise ValueError("persisted non-managed Hook layer requires a positive definition revision")
        for definition in layer.hooks:
            if definition.implementation is HookImplementation.BUILTIN:
                if definition.handler_id not in self._handler_ids:
                    raise ValueError("in-process Hook handler is not a signed builtin ID")
                continue
            command = definition.command
            assert command is not None
            if command.executable_profile_fingerprint is None:
                raise ValueError("production command Hook requires an executable profile fingerprint")
            if command.artifact_output_limit_bytes < max(definition.output_limit_bytes, 64 * 1024):
                raise ValueError("command Hook Artifact limit must cover inline/stderr limits")
            if not command.allowed_environment <= _SAFE_ENVIRONMENT or any(
                any(fragment in name.upper() for fragment in _SENSITIVE_ENVIRONMENT)
                for name in command.allowed_environment
            ):
                raise ValueError("command Hook environment allowlist contains a secret/unsupported name")

    def _require_initialized(self) -> None:
        if self._snapshot is None:
            raise RuntimeError("Hook configuration service is not initialized")

    def _record(self, scope: HookScope, owner_id: str, expected_revision: int) -> HookLayerRecord:
        self._require_initialized()
        record = self._records.get((scope, owner_id))
        if record is None or record.revision != expected_revision:
            actual = 0 if record is None else record.revision
            raise ValueError(f"Hook layer expected revision {expected_revision}, actual {actual}")
        return record

    def _replace_record(self, record: HookLayerRecord) -> None:
        records = dict(self._records)
        records[(record.layer.scope, record.layer.owner_id)] = record
        self._set_snapshot(tuple(records.values()))

    def _set_snapshot(self, records: Sequence[HookLayerRecord]) -> None:
        ordered = tuple(sorted(records, key=_record_key))
        self._records = {(item.layer.scope, item.layer.owner_id): item for item in ordered}
        self._snapshot = HookCatalogSnapshot(
            self.workspace_id,
            ordered,
            sum(item.revision for item in ordered),
            _snapshot_hash(ordered),
        )


def hook_definition_hash(definition: HookDefinition) -> str:
    return canonical_json_sha256(_definition_value(definition))


def hook_layer_hash(layer: HookLayer) -> str:
    return canonical_json_sha256(_layer_value(layer))


def _preserved_command_confirmations(
    current: HookLayerRecord | None,
    layer: HookLayer,
) -> dict[str, str]:
    if current is None or current.layer.scope is not HookScope.WORKSPACE:
        return {}
    hashes = {
        hook.hook_id: hook_definition_hash(hook)
        for hook in layer.hooks
        if hook.implementation is HookImplementation.COMMAND
    }
    return {
        hook_id: digest for hook_id, digest in current.command_confirmations.items() if hashes.get(hook_id) == digest
    }


def _collection(workspace_id: str) -> str:
    return "hook_config_" + hashlib.sha256(workspace_id.encode()).hexdigest()[:32]


def _entity_id(scope: HookScope, owner_id: str) -> str:
    digest = hashlib.sha256(f"{scope.value}\0{owner_id}".encode()).hexdigest()
    return f"hook-layer-{scope.value}-{digest}"


def _record_key(record: HookLayerRecord) -> tuple[int, str]:
    return _SCOPE_ORDER[record.layer.scope], record.layer.owner_id


def _snapshot_hash(records: Sequence[HookLayerRecord]) -> str:
    return canonical_json_sha256([_record_semantics(record) for record in records])


def _same_record_content(left: HookLayerRecord, right: HookLayerRecord) -> bool:
    return _record_semantics(left) == _record_semantics(right)


def _record_semantics(record: HookLayerRecord) -> dict[str, Any]:
    return {
        "workspaceId": record.workspace_id,
        "layer": _layer_value(record.layer),
        "trust": record.trust.value,
        "contentHash": record.content_hash,
        "commandConfirmations": dict(sorted(record.command_confirmations.items())),
    }


def _record_value(record: HookLayerRecord) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        **_record_semantics(record),
        "recordRevision": record.revision,
        "idempotencyKey": record.idempotency_key,
    }


def _layer_value(layer: HookLayer) -> dict[str, Any]:
    return {
        "scope": layer.scope.value,
        "ownerId": layer.owner_id,
        "revision": layer.revision,
        "hooks": [_definition_value(item) for item in layer.hooks],
        "deniedEvents": sorted(item.value for item in layer.denied_events),
        "deniedHookIds": sorted(layer.denied_hook_ids),
    }


def _definition_value(definition: HookDefinition) -> dict[str, Any]:
    command = definition.command
    return {
        "hookId": definition.hook_id,
        "scope": definition.scope.value,
        "ownerId": definition.owner_id,
        "event": definition.event.value,
        "implementation": definition.implementation.value,
        "priority": definition.priority,
        "timeoutMs": definition.timeout_ms,
        "outputLimitBytes": definition.output_limit_bytes,
        "enabled": definition.enabled,
        "handlerId": definition.handler_id,
        "command": (
            None
            if command is None
            else {
                "executableId": command.executable_id,
                "arguments": list(command.arguments),
                "allowedEnvironment": sorted(command.allowed_environment),
                "executableProfileFingerprint": command.executable_profile_fingerprint,
                "cwdRootId": command.cwd_root_id,
                "cwd": command.cwd,
                "environmentProfileId": command.environment_profile_id,
                "artifactOutputLimitBytes": command.artifact_output_limit_bytes,
            }
        ),
    }


def _parse_record(raw: object, workspace_id: str) -> HookLayerRecord:
    expected = {
        "schemaVersion",
        "workspaceId",
        "layer",
        "trust",
        "contentHash",
        "commandConfirmations",
        "recordRevision",
        "idempotencyKey",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("persisted Hook layer record fields are invalid")
    revision = raw["recordRevision"]
    confirmations = raw["commandConfirmations"]
    if (
        raw["schemaVersion"] != 1
        or raw["workspaceId"] != workspace_id
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(raw["contentHash"], str)
        or not isinstance(raw["idempotencyKey"], str)
        or not isinstance(confirmations, Mapping)
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in confirmations.items())
    ):
        raise ValueError("persisted Hook layer record types are invalid")
    try:
        trust = HookLayerTrust(raw["trust"])
    except (TypeError, ValueError) as error:
        raise ValueError("persisted Hook layer trust is invalid") from error
    return HookLayerRecord(
        workspace_id,
        _parse_layer(raw["layer"]),
        trust,
        str(raw["contentHash"]),
        dict(confirmations),
        revision,
        str(raw["idempotencyKey"]),
    )


def _parse_layer(raw: object) -> HookLayer:
    expected = {"scope", "ownerId", "revision", "hooks", "deniedEvents", "deniedHookIds"}
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("persisted Hook layer fields are invalid")
    revision = raw["revision"]
    hooks = raw["hooks"]
    denied_events = raw["deniedEvents"]
    denied_ids = raw["deniedHookIds"]
    if (
        not isinstance(raw["ownerId"], str)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or not isinstance(hooks, list)
        or not isinstance(denied_events, list)
        or any(not isinstance(item, str) for item in denied_events)
        or not isinstance(denied_ids, list)
        or any(not isinstance(item, str) for item in denied_ids)
    ):
        raise ValueError("persisted Hook layer types are invalid")
    try:
        scope = HookScope(raw["scope"])
        events = frozenset(HookEvent(item) for item in denied_events)
    except (TypeError, ValueError) as error:
        raise ValueError("persisted Hook layer scope/event is invalid") from error
    return HookLayer(
        scope,
        str(raw["ownerId"]),
        revision,
        tuple(_parse_definition(item) for item in hooks),
        events,
        frozenset(denied_ids),
    )


def _parse_definition(raw: object) -> HookDefinition:
    expected = {
        "hookId",
        "scope",
        "ownerId",
        "event",
        "implementation",
        "priority",
        "timeoutMs",
        "outputLimitBytes",
        "enabled",
        "handlerId",
        "command",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("persisted Hook definition fields are invalid")
    strings = ("hookId", "ownerId")
    integers = ("priority", "timeoutMs", "outputLimitBytes")
    if (
        any(not isinstance(raw[key], str) for key in strings)
        or any(isinstance(raw[key], bool) or not isinstance(raw[key], int) for key in integers)
        or not isinstance(raw["enabled"], bool)
        or (raw["handlerId"] is not None and not isinstance(raw["handlerId"], str))
    ):
        raise ValueError("persisted Hook definition types are invalid")
    try:
        scope = HookScope(raw["scope"])
        event = HookEvent(raw["event"])
        implementation = HookImplementation(raw["implementation"])
    except (TypeError, ValueError) as error:
        raise ValueError("persisted Hook definition enum is invalid") from error
    command = None if raw["command"] is None else _parse_command(raw["command"])
    return HookDefinition(
        str(raw["hookId"]),
        scope,
        str(raw["ownerId"]),
        event,
        implementation,
        priority=int(raw["priority"]),
        timeout_ms=int(raw["timeoutMs"]),
        output_limit_bytes=int(raw["outputLimitBytes"]),
        enabled=bool(raw["enabled"]),
        handler_id=None if raw["handlerId"] is None else str(raw["handlerId"]),
        command=command,
    )


def _parse_command(raw: object) -> HookCommandSpec:
    expected = {
        "executableId",
        "arguments",
        "allowedEnvironment",
        "executableProfileFingerprint",
        "cwdRootId",
        "cwd",
        "environmentProfileId",
        "artifactOutputLimitBytes",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("persisted Hook command fields are invalid")
    arguments = raw["arguments"]
    environment = raw["allowedEnvironment"]
    artifact_limit = raw["artifactOutputLimitBytes"]
    text_fields = ("executableId", "cwdRootId", "cwd", "environmentProfileId")
    if (
        any(not isinstance(raw[key], str) for key in text_fields)
        or not isinstance(arguments, list)
        or any(not isinstance(item, str) for item in arguments)
        or not isinstance(environment, list)
        or any(not isinstance(item, str) for item in environment)
        or (
            raw["executableProfileFingerprint"] is not None and not isinstance(raw["executableProfileFingerprint"], str)
        )
        or isinstance(artifact_limit, bool)
        or not isinstance(artifact_limit, int)
    ):
        raise ValueError("persisted Hook command types are invalid")
    return HookCommandSpec(
        str(raw["executableId"]),
        tuple(arguments),
        frozenset(environment),
        None if raw["executableProfileFingerprint"] is None else str(raw["executableProfileFingerprint"]),
        str(raw["cwdRootId"]),
        str(raw["cwd"]),
        str(raw["environmentProfileId"]),
        artifact_limit,
    )


__all__ = [
    "EntityHookConfigurationStore",
    "HookCatalogSnapshot",
    "HookConfigurationService",
    "HookLayerRecord",
    "HookLayerTrust",
    "hook_definition_hash",
    "hook_layer_hash",
]
