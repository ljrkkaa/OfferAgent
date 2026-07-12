"""Canonical JSON encoding and SHA-256 identities for tool arguments."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from offeragent_harness.models.json_types import thaw_json


class CanonicalJsonError(ValueError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the RFC 8785/JCS subset used for audit identities.

    JSON objects are ordered by UTF-16 code units and numbers use ECMAScript's
    fixed/scientific thresholds. Integers outside JavaScript's exact range fail
    closed and must be represented as strings by the tool schema. This keeps the
    Python Worker and generated TypeScript clients on one hash contract.
    """

    try:
        text = _encode_jcs(thaw_json(value), seen=set())
        return text.encode("utf-8")
    except (TypeError, UnicodeError, ValueError, RecursionError) as error:
        raise CanonicalJsonError(str(error)) from error


_MAX_SAFE_INTEGER = (1 << 53) - 1


def _encode_string(value: str) -> str:
    # JCS rejects lone surrogate code points (invalid Unicode scalar values).
    value.encode("utf-8", errors="strict")
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _encode_float(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if value == 0:
        return "0"
    negative = value < 0
    magnitude = -value if negative else value
    raw = repr(magnitude).lower()
    coefficient, separator, exponent_text = raw.partition("e")
    exponent = int(exponent_text) if separator else 0
    integer, dot, fraction = coefficient.partition(".")
    digits = (integer + (fraction if dot else "")).lstrip("0") or "0"
    exponent10 = exponent - len(fraction)
    while len(digits) > 1 and digits.endswith("0"):
        digits = digits[:-1]
        exponent10 += 1

    if 1e-6 <= magnitude < 1e21:
        decimal_position = len(digits) + exponent10
        if decimal_position <= 0:
            rendered = "0." + "0" * (-decimal_position) + digits
        elif decimal_position >= len(digits):
            rendered = digits + "0" * (decimal_position - len(digits))
        else:
            rendered = digits[:decimal_position] + "." + digits[decimal_position:]
    else:
        scientific_exponent = len(digits) + exponent10 - 1
        mantissa = digits[0] if len(digits) == 1 else digits[0] + "." + digits[1:]
        sign = "+" if scientific_exponent >= 0 else ""
        rendered = f"{mantissa}e{sign}{scientific_exponent}"
    return "-" + rendered if negative else rendered


def _encode_jcs(value: Any, *, seen: set[int]) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ValueError("integers outside the IEEE-754 safe range must be encoded as strings")
        return str(value)
    if isinstance(value, float):
        return _encode_float(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise ValueError("circular JSON object")
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        seen.add(identity)
        try:
            keys = sorted(value, key=lambda key: key.encode("utf-16-be", errors="strict"))
            return "{" + ",".join(f"{_encode_string(key)}:{_encode_jcs(value[key], seen=seen)}" for key in keys) + "}"
        finally:
            seen.remove(identity)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in seen:
            raise ValueError("circular JSON array")
        seen.add(identity)
        try:
            return "[" + ",".join(_encode_jcs(item, seen=seen) for item in value) + "]"
        finally:
            seen.remove(identity)
    raise TypeError(f"not a JSON-compatible value: {type(value).__name__}")


def canonical_json_sha256(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


__all__ = ["CanonicalJsonError", "canonical_json_bytes", "canonical_json_sha256"]
