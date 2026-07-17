"""Opaque secret handles and non-exporting SecretStore boundary."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Generic, Protocol, TypeVar, runtime_checkable

_HANDLE_RE = re.compile(r"secret:v1:[0-9a-f]{32}")
_PROVIDER_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_SCOPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}")
T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)


class SecretKind(str, Enum):
    MODEL_PROVIDER = "model-provider"


@dataclass(frozen=True, slots=True)
class SecretHandle:
    opaque_id: str

    def __post_init__(self) -> None:
        if _HANDLE_RE.fullmatch(self.opaque_id) is None:
            raise ValueError("invalid opaque SecretHandle")

    def __str__(self) -> str:
        return self.opaque_id

    def __repr__(self) -> str:
        return f"SecretHandle({self.opaque_id!r})"


@dataclass(frozen=True, slots=True)
class SecretMetadata:
    handle: SecretHandle
    scope_id: str
    kind: SecretKind
    provider_id: str
    version: int
    created_at: datetime
    rotated_at: datetime

    def __post_init__(self) -> None:
        if _SCOPE_RE.fullmatch(self.scope_id) is None:
            raise ValueError("secret scope_id is invalid")
        if _PROVIDER_RE.fullmatch(self.provider_id) is None:
            raise ValueError("provider_id must be canonical lowercase ASCII")
        if self.version < 1:
            raise ValueError("secret version must be positive")
        for value in (self.created_at, self.rotated_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("secret timestamps must be timezone-aware")


class SecretInput:
    """One-shot mutable input whose repr/string never expose plaintext."""

    def __init__(self, value: str | bytes | bytearray) -> None:
        encoded = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if not encoded or len(encoded) > 1_048_576 or b"\x00" in encoded:
            raise ValueError("secret must contain 1..1048576 non-NUL bytes")
        self._buffer = bytearray(encoded)
        self._consumed = False

    def take(self) -> bytearray:
        if self._consumed:
            raise RuntimeError("SecretInput was already consumed")
        self._consumed = True
        buffer = self._buffer
        self._buffer = bytearray()
        return buffer

    def close(self) -> None:
        _zero(self._buffer)
        self._buffer.clear()
        self._consumed = True

    def __repr__(self) -> str:
        return "<SecretInput redacted>"

    __str__ = __repr__


@runtime_checkable
class SecretResolver(Protocol):
    def consume(
        self,
        handle: SecretHandle,
        *,
        scope_id: str,
        expected_kind: SecretKind,
        expected_provider_id: str,
        consumer: Callable[[memoryview], T],
    ) -> T:
        """Consume plaintext only when the handle has the caller's exact binding."""
        ...


@runtime_checkable
class SecretStore(SecretResolver, Protocol):
    def create(
        self,
        *,
        scope_id: str,
        kind: SecretKind,
        provider_id: str,
        secret: SecretInput,
    ) -> SecretMetadata: ...

    def rotate(
        self,
        handle: SecretHandle,
        *,
        scope_id: str,
        expected_version: int,
        secret: SecretInput,
    ) -> SecretMetadata: ...

    def metadata(self, handle: SecretHandle, *, scope_id: str) -> SecretMetadata: ...

    def list_metadata(self, *, scope_id: str) -> tuple[SecretMetadata, ...]: ...

    def delete(self, handle: SecretHandle, *, scope_id: str, expected_version: int) -> None: ...


class SecretConsumer(Generic[T_co], Protocol):
    def __call__(self, value: memoryview) -> T_co: ...


def _zero(buffer: bytearray) -> None:
    for index in range(len(buffer)):
        buffer[index] = 0


__all__ = [
    "SecretConsumer",
    "SecretHandle",
    "SecretInput",
    "SecretKind",
    "SecretMetadata",
    "SecretResolver",
    "SecretStore",
]
