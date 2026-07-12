"""Immutable JSON values used at domain boundaries."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | Mapping[str, "JsonValue"] | Sequence["JsonValue"]


class FrozenJsonObject(Mapping[str, "FrozenJsonValue"]):
    """A small immutable mapping with value equality.

    ``MappingProxyType`` is awkward to deepcopy and serialize.  This wrapper keeps
    domain snapshots immutable while remaining a normal ``Mapping``.
    """

    __slots__ = ("_data",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._data = {key: freeze_json(value) for key, value in values.items()}

    def __getitem__(self, key: str) -> FrozenJsonValue:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"FrozenJsonObject({self._data!r})"


FrozenJsonValue: TypeAlias = JsonScalar | FrozenJsonObject | tuple["FrozenJsonValue", ...]


def freeze_json(value: Any) -> FrozenJsonValue:
    """Validate and recursively freeze a JSON-compatible value."""

    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return FrozenJsonObject(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"not a JSON-compatible value: {type(value).__name__}")


def thaw_json(value: FrozenJsonValue | Any) -> Any:
    """Return mutable, stdlib-JSON-compatible data."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_json(item) for item in value]
    return value
