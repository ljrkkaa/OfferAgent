from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from offeragent_harness.runtime.release_privileges import (
    PRIVILEGE_APPROVAL_CONFIRMATION,
    RuntimePrivilegeApprovalReceipt,
    RuntimePrivilegeError,
    assess_privilege_change,
    build_privilege_envelope_from_process_catalog,
    format_utc_timestamp,
    parse_privilege_envelope,
    privilege_envelope_payload,
    validate_privilege_approval_receipt,
)

CATALOG = (Path(__file__).resolve().parents[3] / "packaging" / "process-catalog.v1.json").read_bytes()
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def _catalog_with_plain_environment(name: str) -> bytes:
    raw = json.loads(CATALOG)
    raw["environmentProfiles"][0]["allowedNames"] = [name]
    return (json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _receipt(
    *, diff_hash: str, old: str | None, new: str, receipt_id: str = "1" * 64
) -> RuntimePrivilegeApprovalReceipt:
    """Build a strictly typed privilege approval receipt for validation tests."""
    return RuntimePrivilegeApprovalReceipt(
        receipt_id=receipt_id,
        issued_at=format_utc_timestamp(NOW),
        expires_at=format_utc_timestamp(NOW + timedelta(minutes=5)),
        confirmation=PRIVILEGE_APPROVAL_CONFIRMATION,
        old_manifest_hash=None if old is None else "sha256:" + "a" * 64,
        new_manifest_hash="sha256:" + "b" * 64,
        old_privilege_fingerprint=old,
        new_privilege_fingerprint=new,
        diff_hash=diff_hash,
    )


def test_catalog_build_is_canonical_deterministic_and_round_trips_strictly() -> None:
    first = build_privilege_envelope_from_process_catalog(CATALOG)
    second = build_privilege_envelope_from_process_catalog(CATALOG)

    assert first == second
    assert first.local_process_network is False
    assert first.allowed_network_categories == ("model", "signed_update")
    assert {item.kind for item in first.profile_privileges} == {"hook", "shell"}
    assert all(not item.allow_network for item in first.profile_privileges)
    assert parse_privilege_envelope(privilege_envelope_payload(first)) == first

    tampered = privilege_envelope_payload(first)
    tampered["fingerprint"] = "sha256:" + "0" * 64
    with pytest.raises(RuntimePrivilegeError, match="fingerprint"):
        parse_privilege_envelope(tampered)


def test_only_provable_privilege_narrowing_is_automatic() -> None:
    narrow = build_privilege_envelope_from_process_catalog(CATALOG)
    expanded = build_privilege_envelope_from_process_catalog(_catalog_with_plain_environment("OFFERAGENT_SAFE"))

    assert assess_privilege_change(expanded, narrow).automatic is True
    expansion = assess_privilege_change(narrow, expanded)
    assert expansion.automatic is False
    assert expansion.diff["old"] is not None and expansion.diff["new"] is not None
    assert expansion.diff_hash.startswith("sha256:")
    assert assess_privilege_change(None, narrow).automatic is False


def test_catalog_noncanonical_or_shell_executable_privilege_drift_is_rejected() -> None:
    with pytest.raises(RuntimePrivilegeError, match="canonical"):
        build_privilege_envelope_from_process_catalog(CATALOG + b" ")

    raw = json.loads(CATALOG)
    raw["signedShellProfiles"][0]["fixedArguments"] = ["different"]
    payload = (json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with pytest.raises(RuntimePrivilegeError, match="executable privilege"):
        build_privilege_envelope_from_process_catalog(payload)


def test_receipt_is_hash_bound_short_lived_and_one_time() -> None:
    old = build_privilege_envelope_from_process_catalog(CATALOG)
    new = build_privilege_envelope_from_process_catalog(_catalog_with_plain_environment("OFFERAGENT_SAFE"))
    assessment = assess_privilege_change(old, new)
    receipt = _receipt(diff_hash=assessment.diff_hash, old=old.fingerprint, new=new.fingerprint)

    validate_privilege_approval_receipt(
        receipt,
        assessment=assessment,
        old_manifest_hash="sha256:" + "a" * 64,
        new_manifest_hash="sha256:" + "b" * 64,
        now=NOW + timedelta(minutes=1),
        consumed_receipt_ids=(),
    )
    with pytest.raises(RuntimePrivilegeError, match="already consumed"):
        validate_privilege_approval_receipt(
            receipt,
            assessment=assessment,
            old_manifest_hash="sha256:" + "a" * 64,
            new_manifest_hash="sha256:" + "b" * 64,
            now=NOW + timedelta(minutes=1),
            consumed_receipt_ids=(receipt.receipt_id,),
        )
    with pytest.raises(RuntimePrivilegeError, match="does not bind"):
        validate_privilege_approval_receipt(
            receipt,
            assessment=assessment,
            old_manifest_hash="sha256:" + "a" * 64,
            new_manifest_hash="sha256:" + "c" * 64,
            now=NOW + timedelta(minutes=1),
            consumed_receipt_ids=(),
        )
