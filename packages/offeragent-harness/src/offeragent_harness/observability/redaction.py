"""Fail-closed conversion of classified values to bounded diagnostic JSON."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from offeragent_harness.models.json_types import JsonValue

from .models import DataClass, LogField

_SECRET_TEXT = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+\-/]+=*|(?:api[_-]?key|token|secret|password|cookie)\s*[:=]\s*\S+)"
)
_ABSOLUTE_WINDOWS_PATH = re.compile(r"(?i)(?:[A-Z]:\\|\\\\)[^\r\n\t\"']+")


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    include_paths: bool = False
    max_depth: int = 8
    max_collection_items: int = 256
    max_text_chars: int = 4096

    def __post_init__(self) -> None:
        if self.max_depth < 1 or self.max_collection_items < 1 or self.max_text_chars < 64:
            raise ValueError("redaction bounds must be positive")


def sanitize_fields(fields: Mapping[str, LogField], policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
    selected = policy or RedactionPolicy()
    if len(fields) > selected.max_collection_items:
        raise ValueError("observability field count exceeds limit")
    result: dict[str, JsonValue] = {}
    for key, field in fields.items():
        if not isinstance(key, str) or not key or len(key) > 128 or not isinstance(field, LogField):
            raise TypeError("observability fields require explicit LogField classification")
        result[key] = _sanitize(field.value, field.classification, selected, depth=0)
    return result


def _sanitize(value: Any, classification: DataClass, policy: RedactionPolicy, *, depth: int) -> JsonValue:
    if classification is DataClass.SECRET:
        return "<secret:redacted>"
    if classification is DataClass.CONTENT:
        payload = _stable_bytes(value)
        return f"<content:redacted bytes={len(payload)} sha256={hashlib.sha256(payload).hexdigest()[:16]}>"
    if classification is DataClass.PATH and not policy.include_paths:
        return "<path:redacted>"
    if classification is DataClass.PATH:
        text = str(PurePath(value))
        return _bounded_text(text, policy)
    return _sanitize_public(value, policy, depth=depth)


def _sanitize_public(value: Any, policy: RedactionPolicy, *, depth: int) -> JsonValue:
    if depth > policy.max_depth:
        return "<depth-limit>"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("observability numbers must be finite")
        return value
    if isinstance(value, str):
        text = _SECRET_TEXT.sub("<secret:redacted>", value)
        if not policy.include_paths:
            text = _ABSOLUTE_WINDOWS_PATH.sub("<path:redacted>", text)
        return _bounded_text(text, policy)
    if isinstance(value, Mapping):
        if len(value) > policy.max_collection_items:
            raise ValueError("observability object exceeds item limit")
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise TypeError("observability object keys must be bounded text")
            result[key] = _sanitize_public(item, policy, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        if len(value) > policy.max_collection_items:
            raise ValueError("observability array exceeds item limit")
        return [_sanitize_public(item, policy, depth=depth + 1) for item in value]
    raise TypeError(f"unclassified observability value is not JSON-safe: {type(value).__name__}")


def _bounded_text(value: str, policy: RedactionPolicy) -> str:
    normalized = value.replace("\x00", "�")
    if len(normalized) <= policy.max_text_chars:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{normalized[: policy.max_text_chars]}…<truncated sha256={digest}>"


def _stable_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return str(value).encode("utf-8", errors="replace")


__all__ = ["RedactionPolicy", "sanitize_fields"]
