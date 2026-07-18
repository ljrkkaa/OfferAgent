"""Revisioned, event-audited configuration service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from offeragent_harness.config import (
    RESTART_REQUIRED_PATHS,
    ConfigLayer,
    ConfigPatch,
    ConfigScope,
    HarnessConfig,
    ModelSettings,
    RunConfigSnapshot,
)
from offeragent_harness.config.files import ConfigFileLoad, ConfigFileStore
from offeragent_harness.config.migrations import (
    project_legacy_codex_config,
    project_previous_codex_config_with_report,
    validate_current_codex_config,
)
from offeragent_harness.config.resolver import changed_paths, merge_patch, resolve_config
from offeragent_harness.error_codes import ResourceConflictCause
from offeragent_harness.ports import Clock, EventSink, IdGenerator, NewEvent, StoredEvent, UnitOfWorkFactory

_CONFIG_COLLECTION = "config_layers"
_RECEIPT_COLLECTION = "config_receipts"
_SCHEMA_VERSION = 5
_LEGACY_SCHEMA_VERSIONS = frozenset({2, 3})
_PREVIOUS_SCHEMA_VERSION = 4
_LAYER_FIELDS = frozenset({"config", "eventSequence", "ownerId", "revision", "schemaVersion", "scope", "updatedAt"})
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}")


class ConfigServiceError(RuntimeError):
    pass


class ConfigRevisionConflict(ConfigServiceError, ResourceConflictCause):
    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"configuration expected revision {expected}, actual {actual}")


class ConfigIdempotencyConflict(ConfigServiceError, ResourceConflictCause):
    pass


class ConfigCorrupt(ConfigServiceError):
    pass


class ConfigUpdateStatus(str, Enum):
    APPLIED = "applied"
    RESTART_REQUIRED = "restart_required"


@dataclass(frozen=True, slots=True)
class ConfigUpdateCommand:
    scope: ConfigScope
    owner_id: str
    expected_revision: int
    idempotency_key: str
    actor_id: str
    patch: ConfigPatch

    def __post_init__(self) -> None:
        if self.scope is ConfigScope.RUN:
            raise ValueError("run overrides are snapshotted and cannot be persisted")
        for value, label in (
            (self.owner_id, "owner_id"),
            (self.idempotency_key, "idempotency_key"),
            (self.actor_id, "actor_id"),
        ):
            if _SAFE_ID.fullmatch(value) is None:
                raise ValueError(f"{label} must be canonical safe ASCII")
        if self.expected_revision < 0:
            raise ValueError("expected_revision cannot be negative")
        if not self.patch.payload():
            raise ValueError("configuration patch cannot be empty")
        validate_current_codex_config(self.patch.payload())


@dataclass(frozen=True, slots=True)
class ConfigUpdateResult:
    status: ConfigUpdateStatus
    scope: ConfigScope
    owner_id: str
    revision: int
    changed_fields: tuple[str, ...]
    restart_required: bool
    event_sequence: int
    replayed: bool = False

    @property
    def applied(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class ConfigBootstrapResult:
    file: ConfigFileLoad
    update: ConfigUpdateResult | None


@dataclass(frozen=True, slots=True)
class ConfigAuditRecord:
    audit_id: str
    scope: ConfigScope
    owner_id: str
    revision: int
    actor_id: str
    request_hash: str
    changed_fields: tuple[str, ...]
    restart_required: bool
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.revision < 1 or self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("configuration audit revision/timestamp is invalid")


class ConfigAuditSink(Protocol):
    async def record(self, audit: ConfigAuditRecord) -> None: ...


class NullConfigAuditSink:
    async def record(self, audit: ConfigAuditRecord) -> None:
        del audit


class WorkerConfigActivation:
    """Compare durable desired config with this Worker's immutable active config.

    A restart-sensitive setting is not active merely because its durable layer
    was committed.  The Worker freezes the effective values it activated during
    startup, before opening ingress.  Every later ``restartPending`` projection
    compares the current durable effective config with that frozen baseline.

    The baseline cannot be rebound in-process: clearing a pending restart
    requires constructing and starting a new Worker instance.
    """

    def __init__(self, active_config: HarnessConfig | None = None) -> None:
        self._active: tuple[tuple[str, str], ...] | None = None
        self._codex_proxy_url: str | None = None
        if active_config is not None:
            self.freeze(active_config)

    @property
    def frozen(self) -> bool:
        return self._active is not None

    def freeze(self, active_config: HarnessConfig) -> None:
        projection = _restart_sensitive_projection(active_config)
        if self._active is None:
            self._active = projection
            self._codex_proxy_url = active_config.model.proxy_url
            return
        if self._active != projection:
            raise ConfigServiceError("active Worker configuration is already frozen")

    def codex_proxy_url(self) -> str | None:
        if self._active is None:
            raise ConfigServiceError("active Worker configuration has not been frozen")
        return self._codex_proxy_url

    def codex_model_transport_settings(self, desired: ModelSettings) -> ModelSettings:
        """Project the active proxy onto a Run so catalog and inference cannot split before restart."""

        if self._active is None:
            raise ConfigServiceError("active Worker configuration has not been frozen")
        return desired.model_copy(update={"proxy_url": self._codex_proxy_url})

    def restart_pending(self, desired_config: HarnessConfig) -> bool:
        if self._active is None:
            raise ConfigServiceError("active Worker configuration has not been frozen")
        return self._active != _restart_sensitive_projection(desired_config)


class ConfigService:
    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        event_sink: EventSink,
        clock: Clock,
        ids: IdGenerator,
        audit_sink: ConfigAuditSink | None = None,
        allow_managed_updates: bool = False,
        scope_unit_of_work: Mapping[ConfigScope, UnitOfWorkFactory] | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._scope_unit_of_work = dict(scope_unit_of_work or {})
        if ConfigScope.RUN in self._scope_unit_of_work:
            raise ValueError("run overrides cannot have a persistence UnitOfWork")
        self._event_sink = event_sink
        self._clock = clock
        self._ids = ids
        self._audit_sink = audit_sink or NullConfigAuditSink()
        self._allow_managed_updates = allow_managed_updates
        self.delivery_failures: list[str] = []

    async def update(self, command: ConfigUpdateCommand) -> ConfigUpdateResult:
        if command.scope is ConfigScope.MANAGED and not self._allow_managed_updates:
            raise ConfigServiceError("managed configuration requires an authorized deployment adapter")
        return await self._apply_update(command)

    async def bootstrap_file(self, store: ConfigFileStore) -> ConfigBootstrapResult:
        """Import a validated bootstrap file through the audited UOW path."""

        loaded = await asyncio.to_thread(store.load)
        if loaded.layer.scope is ConfigScope.MANAGED and not self._allow_managed_updates:
            raise ConfigServiceError("managed bootstrap requires an authorized deployment adapter")
        if loaded.safe_mode or not loaded.layer.patch.payload():
            return ConfigBootstrapResult(loaded, None)
        current = await self.layer(loaded.layer.scope, loaded.layer.owner_id)
        if current.revision > 0:
            return ConfigBootstrapResult(loaded, None)
        encoded = json.dumps(
            loaded.layer.patch.payload(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        command = ConfigUpdateCommand(
            loaded.layer.scope,
            loaded.layer.owner_id,
            0,
            f"bootstrap-{hashlib.sha256(encoded).hexdigest()[:32]}",
            "config-bootstrap",
            loaded.layer.patch,
        )
        try:
            update = await self._apply_update(command)
        except ConfigRevisionConflict:
            if (await self.layer(loaded.layer.scope, loaded.layer.owner_id)).revision > 0:
                return ConfigBootstrapResult(loaded, None)
            raise
        return ConfigBootstrapResult(loaded, update)

    async def _apply_update(self, command: ConfigUpdateCommand) -> ConfigUpdateResult:
        request_hash = _request_hash(command)
        receipt_id = _receipt_id(command)
        stream_id = _stream_id(command.scope, command.owner_id)
        stored: tuple[StoredEvent, ...] = ()
        deferred: BaseException | None = None
        try:
            async with self._uow_for(command.scope).begin() as uow:
                existing_receipt = await uow.entities.get(_RECEIPT_COLLECTION, receipt_id)
                if existing_receipt is not None:
                    replay = _decode_receipt(existing_receipt, request_hash, replayed=True)
                    replay_layer = _decode_layer(
                        await uow.entities.get(_CONFIG_COLLECTION, _entity_id(replay.scope, replay.owner_id)),
                        replay.scope,
                        replay.owner_id,
                    )
                    replay_events = await uow.events.read(
                        _stream_id(replay.scope, replay.owner_id),
                        after_sequence=replay.event_sequence - 1,
                        limit=1,
                    )
                    if (
                        replay_layer.revision < replay.revision
                        or len(replay_events) != 1
                        or replay_events[0].sequence != replay.event_sequence
                    ):
                        raise ConfigCorrupt("configuration idempotency receipt is orphaned")
                    return replay
                entity_id = _entity_id(command.scope, command.owner_id)
                raw = await uow.entities.get(_CONFIG_COLLECTION, entity_id)
                current = _decode_layer(raw, command.scope, command.owner_id)
                if current.revision != command.expected_revision:
                    raise ConfigRevisionConflict(command.expected_revision, current.revision)
                merged = merge_patch(current.patch, command.patch)
                fields = changed_paths(command.patch)
                restart_required = any(path in RESTART_REQUIRED_PATHS for path in fields)
                now = self._clock.utcnow()
                revision = current.revision + 1
                event_sequence = current.event_sequence + 1
                event = NewEvent(
                    event_id=self._ids.new_id("event"),
                    event_type="config.changed",
                    payload={
                        "schemaVersion": 1,
                        "scope": command.scope.value,
                        "ownerId": command.owner_id,
                        "revision": revision,
                        "actorId": command.actor_id,
                        "requestHash": request_hash,
                        "changedFields": list(fields),
                        "restartRequired": restart_required,
                    },
                    occurred_at=now,
                    terminal=False,
                    idempotency_key=command.idempotency_key,
                )
                layer_payload = _encode_layer(
                    ConfigLayer(command.scope, command.owner_id, revision, merged, event_sequence),
                    updated_at=now,
                )
                await uow.entities.put(
                    _CONFIG_COLLECTION,
                    entity_id,
                    layer_payload,
                    expected_revision=current.revision,
                )
                stored = await uow.events.append(stream_id, current.event_sequence, (event,))
                result = ConfigUpdateResult(
                    ConfigUpdateStatus.RESTART_REQUIRED if restart_required else ConfigUpdateStatus.APPLIED,
                    command.scope,
                    command.owner_id,
                    revision,
                    fields,
                    restart_required,
                    event_sequence,
                )
                await uow.entities.put(
                    _RECEIPT_COLLECTION,
                    receipt_id,
                    _encode_receipt(result, request_hash),
                    expected_revision=0,
                )
                await uow.commit()
        except BaseException as error:
            recovered = await self._recover_receipt(command.scope, receipt_id, request_hash)
            if recovered is None:
                raise
            result, stored = recovered
            if not isinstance(error, Exception):
                deferred = error
        await self._deliver(stored)
        await self._audit(command, result, request_hash)
        if deferred is not None:
            raise deferred
        return result

    async def layer(self, scope: ConfigScope, owner_id: str) -> ConfigLayer:
        if scope is ConfigScope.RUN:
            raise ValueError("run overrides are not persisted layers")
        _safe(owner_id, "owner_id")
        async with self._uow_for(scope).begin() as uow:
            raw = await uow.entities.get(_CONFIG_COLLECTION, _entity_id(scope, owner_id))
        return _decode_layer(raw, scope, owner_id)

    async def snapshot(
        self,
        *,
        managed_owner_id: str,
        profile_id: str,
        workspace_id: str,
        session_id: str | None = None,
        run_override: ConfigPatch | None = None,
    ) -> RunConfigSnapshot:
        owners = (
            (ConfigScope.MANAGED, managed_owner_id),
            (ConfigScope.USER, profile_id),
            (ConfigScope.WORKSPACE, workspace_id),
        )
        selected = list(owners)
        if session_id is not None:
            selected.append((ConfigScope.SESSION, f"{workspace_id}:{session_id}"))
        for _, owner in selected:
            _safe(owner, "configuration owner")
        layers = [await self.layer(scope, owner) for scope, owner in selected]
        return resolve_config(layers, captured_at=self._clock.utcnow(), run_override=run_override)

    async def _recover_receipt(
        self,
        scope: ConfigScope,
        receipt_id: str,
        request_hash: str,
    ) -> tuple[ConfigUpdateResult, tuple[StoredEvent, ...]] | None:
        async with self._uow_for(scope).begin() as uow:
            raw = await uow.entities.get(_RECEIPT_COLLECTION, receipt_id)
            if raw is None:
                return None
            result = _decode_receipt(raw, request_hash, replayed=True)
            events = await uow.events.read(
                _stream_id(result.scope, result.owner_id),
                after_sequence=result.event_sequence - 1,
                limit=1,
            )
        if len(events) != 1 or events[0].sequence != result.event_sequence:
            raise ConfigCorrupt("configuration receipt event is missing")
        return result, events

    def _uow_for(self, scope: ConfigScope) -> UnitOfWorkFactory:
        return self._scope_unit_of_work.get(scope, self._unit_of_work)

    async def _deliver(self, events: Sequence[StoredEvent]) -> None:
        try:
            await self._event_sink.publish(events)
        except Exception as error:
            self.delivery_failures.append(type(error).__name__)

    async def _audit(
        self,
        command: ConfigUpdateCommand,
        result: ConfigUpdateResult,
        request_hash: str,
    ) -> None:
        try:
            await self._audit_sink.record(
                ConfigAuditRecord(
                    self._ids.new_id("audit"),
                    result.scope,
                    result.owner_id,
                    result.revision,
                    command.actor_id,
                    request_hash,
                    result.changed_fields,
                    result.restart_required,
                    self._clock.utcnow(),
                )
            )
        except Exception as error:
            self.delivery_failures.append(f"audit:{type(error).__name__}")


def _decode_layer(raw: Any, scope: ConfigScope, owner_id: str) -> ConfigLayer:
    if raw is None:
        return ConfigLayer(scope, owner_id, 0, ConfigPatch(), 0)
    if not isinstance(raw, Mapping):
        raise ConfigCorrupt("configuration layer is not an object")
    try:
        if set(raw) != _LAYER_FIELDS:
            raise ValueError("configuration layer fields are incompatible")
        schema_version = raw.get("schemaVersion")
        if (
            schema_version not in {*_LEGACY_SCHEMA_VERSIONS, _PREVIOUS_SCHEMA_VERSION, _SCHEMA_VERSION}
            or raw.get("scope") != scope.value
        ):
            raise ValueError("incompatible configuration layer")
        if raw.get("ownerId") != owner_id:
            raise ValueError("configuration owner mismatch")
        revision = raw["revision"]
        event_sequence = raw["eventSequence"]
        if type(revision) is not int or type(event_sequence) is not int or revision < 1 or event_sequence < 1:
            raise ValueError("configuration revision is invalid")
        config = raw["config"]
        if schema_version in _LEGACY_SCHEMA_VERSIONS:
            patch = project_legacy_codex_config(config)
        elif schema_version == _PREVIOUS_SCHEMA_VERSION:
            patch = project_previous_codex_config_with_report(config).patch
        else:
            patch = validate_current_codex_config(config)
        return ConfigLayer(
            scope,
            owner_id,
            revision,
            patch,
            event_sequence,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ConfigCorrupt("configuration layer is corrupt") from error


def _encode_layer(layer: ConfigLayer, *, updated_at: datetime) -> dict[str, Any]:
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "scope": layer.scope.value,
        "ownerId": layer.owner_id,
        "revision": layer.revision,
        "eventSequence": layer.event_sequence,
        "config": layer.patch.payload(),
        "updatedAt": updated_at.isoformat(),
    }


def _request_hash(command: ConfigUpdateCommand) -> str:
    payload = {
        "scope": command.scope.value,
        "ownerId": command.owner_id,
        "expectedRevision": command.expected_revision,
        "actorId": command.actor_id,
        "config": command.patch.payload(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _encode_receipt(result: ConfigUpdateResult, request_hash: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "requestHash": request_hash,
        "status": result.status.value,
        "scope": result.scope.value,
        "ownerId": result.owner_id,
        "revision": result.revision,
        "changedFields": list(result.changed_fields),
        "restartRequired": result.restart_required,
        "eventSequence": result.event_sequence,
    }


def _decode_receipt(raw: Any, request_hash: str, *, replayed: bool) -> ConfigUpdateResult:
    if not isinstance(raw, Mapping) or raw.get("requestHash") != request_hash:
        raise ConfigIdempotencyConflict("configuration idempotency key is bound to a different request")
    try:
        expected_fields = {
            "schemaVersion",
            "requestHash",
            "status",
            "scope",
            "ownerId",
            "revision",
            "changedFields",
            "restartRequired",
            "eventSequence",
        }
        if set(raw) != expected_fields or raw["schemaVersion"] != 1:
            raise ValueError("receipt fields are incompatible")
        revision = raw["revision"]
        event_sequence = raw["eventSequence"]
        restart_required = raw["restartRequired"]
        changed = raw["changedFields"]
        if (
            type(revision) is not int
            or type(event_sequence) is not int
            or type(restart_required) is not bool
            or not isinstance(changed, list)
            or any(not isinstance(item, str) for item in changed)
        ):
            raise ValueError("receipt values are invalid")
        return ConfigUpdateResult(
            ConfigUpdateStatus(raw["status"]),
            ConfigScope(raw["scope"]),
            str(raw["ownerId"]),
            revision,
            tuple(changed),
            restart_required,
            event_sequence,
            replayed,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ConfigCorrupt("configuration receipt is corrupt") from error


def _entity_id(scope: ConfigScope, owner_id: str) -> str:
    return f"{scope.value}:{owner_id}"


def _stream_id(scope: ConfigScope, owner_id: str) -> str:
    return f"config:{scope.value}:{owner_id}"


def _receipt_id(command: ConfigUpdateCommand) -> str:
    return f"{command.scope.value}:{command.owner_id}:{command.idempotency_key}"


def _safe(value: str, label: str) -> None:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be canonical safe ASCII")


def _restart_sensitive_projection(config: HarnessConfig) -> tuple[tuple[str, str], ...]:
    """Return an immutable canonical projection owned by RESTART_REQUIRED_PATHS."""

    raw: object = config.model_dump(mode="json")
    projected: list[tuple[str, str]] = []
    for path in sorted(RESTART_REQUIRED_PATHS):
        value = raw
        for component in path.split("."):
            if not isinstance(value, Mapping) or component not in value:
                raise ConfigServiceError(f"restart-required configuration path is invalid: {path}")
            value = value[component]
        projected.append(
            (
                path,
                json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True),
            )
        )
    return tuple(projected)


__all__ = [
    "ConfigAuditRecord",
    "ConfigAuditSink",
    "ConfigBootstrapResult",
    "ConfigCorrupt",
    "ConfigIdempotencyConflict",
    "ConfigRevisionConflict",
    "ConfigService",
    "ConfigServiceError",
    "ConfigUpdateCommand",
    "ConfigUpdateResult",
    "ConfigUpdateStatus",
    "NullConfigAuditSink",
    "WorkerConfigActivation",
]
