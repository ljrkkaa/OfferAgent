"""Workspace-scoped Vault read and transaction boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json
from offeragent_harness.tools import ToolResult

from .cancellation import CancellationToken


class VaultEntryKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True)
class VaultEntry:
    resource_id: str
    relative_path: str
    kind: VaultEntryKind
    size: int
    modified_at: datetime
    content_hash: str | None
    workspace_revision: int


@dataclass(frozen=True)
class VaultRead:
    entry: VaultEntry
    content: bytes
    truncated: bool


@dataclass(frozen=True)
class VaultTransaction:
    transaction_id: str
    workspace_id: str
    operations: tuple[Mapping[str, Any], ...]
    base_revision: int
    idempotency_key: str
    approval_id: str | None

    def __post_init__(self) -> None:
        frozen = tuple(freeze_json(operation) for operation in self.operations)
        if any(not isinstance(operation, FrozenJsonObject) for operation in frozen):
            raise TypeError("Vault transaction operations must be JSON objects")
        object.__setattr__(self, "operations", frozen)


@runtime_checkable
class VaultPort(Protocol):
    async def stat(self, relative_path: str, cancellation: CancellationToken) -> VaultEntry | None: ...

    async def read(self, relative_path: str, cancellation: CancellationToken) -> VaultRead: ...

    async def list(self, relative_path: str, cancellation: CancellationToken) -> tuple[VaultEntry, ...]: ...

    async def execute_transaction(
        self, transaction: VaultTransaction, cancellation: CancellationToken
    ) -> ToolResult: ...


__all__ = ["VaultEntry", "VaultEntryKind", "VaultPort", "VaultRead", "VaultTransaction"]
