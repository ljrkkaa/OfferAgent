"""Deterministic, human-readable names for persisted knowledge artifacts."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

from offeragent_harness.foundation.canonical import canonical_json_bytes

_SEPARATORS = re.compile(r"[^a-z0-9]+")
_IDENTIFIER_PREFIX = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SOURCE_ID_PATTERN = r"^src-[a-z0-9][a-z0-9_-]{1,123}$"


def readable_slug(label: str, *, fallback: str = "item", max_length: int = 96) -> str:
    """Return a portable ASCII slug, with a deterministic fallback for non-Latin labels."""

    if not isinstance(label, str) or not label.strip() or max_length < 3:
        raise ValueError("readable slug input is invalid")
    normalized = unicodedata.normalize("NFKD", label).encode("ascii", errors="ignore").decode("ascii")
    slug = _SEPARATORS.sub("-", normalized.casefold()).strip("-")
    safe_fallback = _SEPARATORS.sub("-", fallback.casefold()).strip("-")
    if not safe_fallback:
        raise ValueError("readable slug fallback is invalid")
    return (slug or safe_fallback)[:max_length].rstrip("-")


def stable_readable_id(
    prefix: str,
    label: str,
    *,
    namespace: str,
    max_length: int = 127,
    digest_length: int = 10,
) -> str:
    """Build a readable identifier with a short digest used only for stable disambiguation."""

    if _IDENTIFIER_PREFIX.fullmatch(prefix) is None or not namespace or not 8 <= digest_length <= 32:
        raise ValueError("readable identifier configuration is invalid")
    digest = hashlib.sha256(canonical_json_bytes({"label": label, "namespace": namespace})).hexdigest()
    slug_limit = max_length - len(prefix) - digest_length - 2
    if slug_limit < 3:
        raise ValueError("readable identifier length is too small")
    slug = readable_slug(label, fallback="item", max_length=slug_limit)
    return f"{prefix}-{slug}-{digest[:digest_length]}"


def unique_readable_slugs(
    items: Iterable[tuple[str, str]],
    *,
    fallback: str = "item",
    max_length: int = 96,
) -> dict[str, str]:
    """Allocate readable slugs, adding a short suffix only to actual collisions."""

    pairs = tuple(items)
    identities = [identity for identity, _ in pairs]
    if any(not identity for identity in identities) or len(identities) != len(set(identities)):
        raise ValueError("slug allocation identities must be non-empty and unique")
    bases = {identity: readable_slug(label, fallback=fallback, max_length=max_length) for identity, label in pairs}
    counts: dict[str, int] = {}
    for base in bases.values():
        counts[base] = counts.get(base, 0) + 1
    result: dict[str, str] = {}
    for identity, _ in pairs:
        base = bases[identity]
        if counts[base] == 1:
            result[identity] = base
            continue
        suffix = hashlib.sha256(identity.encode("utf-8", errors="strict")).hexdigest()[:10]
        stem = base[: max_length - len(suffix) - 1].rstrip("-")
        result[identity] = f"{stem}-{suffix}"
    if len(set(result.values())) != len(result):
        raise ValueError("slug allocation encountered a digest collision")
    return result


__all__ = [
    "SOURCE_ID_PATTERN",
    "readable_slug",
    "stable_readable_id",
    "unique_readable_slugs",
]
