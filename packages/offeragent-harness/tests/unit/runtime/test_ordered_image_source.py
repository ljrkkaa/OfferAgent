from __future__ import annotations

import json
from datetime import date

from offeragent_harness.runtime.ordered_image_source import create_ordered_image_source_manifest


def test_manifest_identifies_one_ordered_image_source_without_assigning_task_semantics_or_carrying_bytes() -> None:
    first_hash = "sha256:" + "a" * 64
    second_hash = "sha256:" + "b" * 64

    manifest = create_ordered_image_source_manifest(
        captured_on=date(2026, 7, 18),
        ordered_image_content_hashes=(first_hash, second_hash),
    )

    assert manifest.captured_on == date(2026, 7, 18)
    assert manifest.image_count == 2
    assert manifest.ordered_image_content_hashes == (first_hash, second_hash)
    assert manifest.source_fingerprint == ("sha256:0cd65e3ab3208489c6bfaa9d2ce2cb125ae8fbef34c9a965c1efe3f7d2302c37")
    metadata = manifest.to_context_metadata()
    assert metadata == {
        "type": "runtimeOrderedImageSource",
        "capturedOn": "2026-07-18",
        "imageCount": 2,
        "orderedImageContentHashes": [first_hash, second_hash],
        "sourceFingerprint": manifest.source_fingerprint,
    }
    serialized = json.dumps(metadata, sort_keys=True)
    assert "bytes" not in serialized
    assert "interview" not in serialized.lower()
