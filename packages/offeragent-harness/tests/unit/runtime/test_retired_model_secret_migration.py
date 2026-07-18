from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from offeragent_harness.runtime.retired_model_secrets import purge_retired_model_secrets


def _write_envelope(root: Path, *, identity: str, kind: str, provider: str, marker: str) -> tuple[Path, bytes]:
    handle = f"secret:v1:{identity * 32}"
    ciphertext = marker.encode()
    encoded = (
        json.dumps(
            {
                "version": 1,
                "handle": handle,
                "scopeId": "ws_test",
                "kind": kind,
                "providerId": provider,
                "secretVersion": 1,
                "createdAt": "2026-07-18T00:00:00+00:00",
                "rotatedAt": "2026-07-18T00:00:00+00:00",
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                "ciphertextSha256": hashlib.sha256(ciphertext).hexdigest(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    path = root / f"{hashlib.sha256(handle.encode('ascii')).hexdigest()}.secret"
    path.write_bytes(encoded)
    return path, encoded


def test_model_secret_purge_is_scoped_redacted_idempotent_and_preserves_other_secrets(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir()
    retired, _ = _write_envelope(
        root,
        identity="1",
        kind="model-provider",
        provider="deepseek",
        marker="sensitive-model-secret-bytes",
    )
    other, other_bytes = _write_envelope(
        root,
        identity="2",
        kind="tool",
        provider="registered-tool",
        marker="ordinary-secret-bytes",
    )

    report = purge_retired_model_secrets(root, scope_id="ws_test")

    assert not retired.exists()
    assert other.read_bytes() == other_bytes
    assert report.deleted_count == 1
    assert report.provider_ids == ("deepseek",)
    assert report.unclassified_count == 1
    assert "sensitive-model-secret-bytes" not in repr(report)

    repeated = purge_retired_model_secrets(root, scope_id="ws_test")
    assert repeated.deleted_count == 0
    assert repeated.provider_ids == ()
    assert other.read_bytes() == other_bytes


def test_model_secret_purge_never_guesses_corrupt_or_other_scope_envelopes(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir()
    corrupt = root / "corrupt.secret"
    corrupt.write_text('{"kind":"model-provider","providerId":"value-must-not-be-reported"', encoding="utf-8")
    malformed_other = root / "malformed-other.secret"
    malformed_other.write_text('{"kind":"tool"}', encoding="utf-8")
    other_scope, other_scope_bytes = _write_envelope(
        root,
        identity="3",
        kind="model-provider",
        provider="openai",
        marker="other-workspace-secret",
    )
    payload = json.loads(other_scope_bytes)
    payload["scopeId"] = "ws_other"
    other_scope.write_text(json.dumps(payload), encoding="utf-8")
    collision, collision_bytes = _write_envelope(
        root,
        identity="4",
        kind="model-provider",
        provider="deepseek",
        marker="malformed-collision-secret",
    )
    collision_payload = json.loads(collision_bytes)
    collision_payload["unexpected"] = True
    collision.write_text(json.dumps(collision_payload), encoding="utf-8")

    report = purge_retired_model_secrets(root, scope_id="ws_test")

    assert corrupt.exists()
    assert malformed_other.exists()
    assert other_scope.exists()
    assert collision.exists()
    assert report.deleted_count == 0
    assert report.provider_ids == ()
    assert report.unclassified_count == 3
    assert "value-must-not-be-reported" not in repr(report)
