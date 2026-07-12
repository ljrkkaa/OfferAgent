"""Shared primitives for the OfferAgent wire protocol.

The protocol models deliberately use one strict configuration.  A DTO must never
silently coerce a value received from an IPC peer, ignore a field introduced by a
schema drift, or be mutated after validation.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, JsonValue
from pydantic.alias_generators import to_camel

JsonObject = dict[str, JsonValue]
JsonArray = list[JsonValue]
JsonScalar = str | int | float | bool | None


class WireModel(BaseModel):
    """Base class for every object that crosses a protocol boundary."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
        validate_default=True,
    )

    def to_wire(self, *, exclude_none: bool = False) -> dict[str, Any]:
        """Return a JSON-compatible representation using canonical wire aliases."""

        return self.model_dump(mode="json", by_alias=True, exclude_none=exclude_none)


class EmptyParams(WireModel):
    """A real, closed schema for commands that accept no parameters."""


class EmptyResult(WireModel):
    """A real, closed schema for commands that return no payload."""


WireModelT = TypeVar("WireModelT", bound=WireModel)


def validate_wire(model: type[WireModelT], value: object) -> WireModelT:
    """Validate a value with Pydantic's strict *JSON* semantics.

    Pydantic intentionally distinguishes strict Python inputs (where an enum must
    already be an enum instance) from strict JSON inputs (where its wire string is
    correct).  IPC adapters commonly decode the outer JSON before dispatching a
    method.  Re-encoding that already decoded subtree here preserves strict wire
    behavior without weakening validation.
    """

    if isinstance(value, model):
        return value
    if isinstance(value, bytes):
        return model.model_validate_json(value)
    if isinstance(value, bytearray):
        return model.model_validate_json(bytes(value))
    if isinstance(value, str):
        return model.model_validate_json(value)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return model.model_validate_json(encoded)


__all__ = [
    "EmptyParams",
    "EmptyResult",
    "JsonArray",
    "JsonObject",
    "JsonScalar",
    "WireModel",
    "validate_wire",
]
