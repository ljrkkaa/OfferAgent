"""Byte-free Runtime metadata for one ordered USER image source."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class OrderedImageSourceManifest:
    """Attachment-Store facts without assigning a semantic task to the images."""

    captured_on: date
    ordered_image_content_hashes: tuple[str, ...]
    source_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        hashes = tuple(self.ordered_image_content_hashes)
        if not hashes:
            raise ValueError("an ordered USER image source cannot be empty")
        if any(_SHA256.fullmatch(content_hash) is None for content_hash in hashes):
            raise ValueError("ordered USER image hashes must be canonical sha256 digests")
        fingerprint = hashlib.sha256()
        for order, content_hash in enumerate(hashes):
            fingerprint.update(f"{order}\0{content_hash}\n".encode())
        object.__setattr__(self, "ordered_image_content_hashes", hashes)
        object.__setattr__(self, "source_fingerprint", f"sha256:{fingerprint.hexdigest()}")

    @property
    def image_count(self) -> int:
        return len(self.ordered_image_content_hashes)

    def to_context_metadata(self) -> dict[str, object]:
        """Project Store-authoritative facts into the owning USER fragment."""

        return {
            "type": "runtimeOrderedImageSource",
            "capturedOn": self.captured_on.isoformat(),
            "imageCount": self.image_count,
            "orderedImageContentHashes": list(self.ordered_image_content_hashes),
            "sourceFingerprint": self.source_fingerprint,
        }


def create_ordered_image_source_manifest(
    *,
    captured_on: date,
    ordered_image_content_hashes: Sequence[str],
) -> OrderedImageSourceManifest:
    """Create a generic image-source manifest without retaining attachment bytes."""

    return OrderedImageSourceManifest(captured_on, tuple(ordered_image_content_hashes))


__all__ = ["OrderedImageSourceManifest", "create_ordered_image_source_manifest"]
