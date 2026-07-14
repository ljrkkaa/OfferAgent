"""Independent fail-closed audit for a native Windows x64 or arm64 payload."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.runtime.production_process_catalog import (
    PROCESS_CATALOG_PATH,
    validate_process_catalog_payload,
)
from offeragent_harness.runtime.release_manifest import (
    ReleaseKeyring,
    ReleaseVerificationError,
    RuntimeBundleVerifier,
    SafeRuntimeZipExtractor,
    native_windows_architecture,
)
from offeragent_harness.runtime.release_trust import load_embedded_release_keys
from offeragent_harness.runtime.windows_authenticode import WindowsAuthenticodeVerifier

try:
    from scripts.frozen_payload_provenance import (
        OFFERAGENT_COMPONENT,
        PROVENANCE_PATH,
        SPDX_PATH,
        assert_payload_provenance,
        canonical_json,
    )
    from scripts.release_dependency_policy import assert_runtime_spdx_document, assert_sbom_packages_allowed
except ModuleNotFoundError:  # Direct `python scripts/audit_windows_release.py` execution.
    from frozen_payload_provenance import (  # type: ignore[import-not-found,no-redef]
        OFFERAGENT_COMPONENT,
        PROVENANCE_PATH,
        SPDX_PATH,
        assert_payload_provenance,
        canonical_json,
    )
    from release_dependency_policy import (  # type: ignore[import-not-found,no-redef]
        assert_runtime_spdx_document,
        assert_sbom_packages_allowed,
    )

FORBIDDEN = {"django", "node", "node_modules", "postgres", "psycopg", "khoj"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--architecture", choices=("x64", "arm64"), required=True)
    parser.add_argument("--plugin-version", required=True)
    args = parser.parse_args()
    try:
        native_architecture = native_windows_architecture()
    except ReleaseVerificationError as error:
        raise SystemExit(f"release audit requires native Windows x64 or arm64: {error}") from error
    if native_architecture != args.architecture:
        raise SystemExit(
            f"release audit architecture {args.architecture} does not match native Windows {native_architecture}"
        )
    payload = args.payload.resolve(strict=True)
    verifier = RuntimeBundleVerifier(
        keyring=ReleaseKeyring(load_embedded_release_keys()),
        authenticode=WindowsAuthenticodeVerifier(),
        require_authenticode=True,
    )
    build = int(sys.getwindowsversion().build)
    bundle = verifier.verify_bundle(
        payload,
        expected_architecture=args.architecture,
        windows_build=build,
        plugin_version=args.plugin_version,
        protocol_version=PROTOCOL_VERSION,
        schema_hash=schema_hash(),
    )
    with tempfile.TemporaryDirectory(prefix="offeragent-audit-", dir=payload.parent) as temporary:
        extracted = Path(temporary) / "runtime"
        SafeRuntimeZipExtractor().extract(bundle, extracted)
        verifier.verify_installed_tree(extracted, bundle)
        catalog_record = bundle.manifest.by_path.get(PROCESS_CATALOG_PATH)
        if catalog_record is None or catalog_record.kind != "asset":
            raise SystemExit("signed manifest has no canonical Process catalog asset")
        try:
            validate_process_catalog_payload((extracted / PROCESS_CATALOG_PATH).read_bytes())
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise SystemExit(f"invalid signed Process catalog asset: {error}") from error
        forbidden = [
            item.relative_to(extracted).as_posix()
            for item in extracted.rglob("*")
            if item.is_file() and any(part.casefold() in FORBIDDEN for part in item.parts)
        ]
        if forbidden:
            raise SystemExit(f"forbidden server/runtime dependency found: {forbidden[:10]}")
        try:
            spdx_payload = (extracted / SPDX_PATH).read_bytes()
            provenance_payload = (extracted / PROVENANCE_PATH).read_bytes()
            sbom = json.loads(spdx_payload.decode("utf-8", errors="strict"))
            provenance = json.loads(provenance_payload.decode("utf-8", errors="strict"))
            if canonical_json(sbom) != spdx_payload or canonical_json(provenance) != provenance_payload:
                raise RuntimeError("Runtime SBOM or payload provenance is not canonical JSON")
            provenance_expected = {
                record.path: (record.byte_length, record.sha256)
                for record in bundle.manifest.files
                if record.path not in {SPDX_PATH, PROVENANCE_PATH}
            }
            expected_executables = {record.path for record in bundle.manifest.files if record.authenticode}
            if not isinstance(provenance, dict) or (
                provenance.get("architecture") != bundle.manifest.platform.architecture
                or provenance.get("buildCommit") != bundle.manifest.build_commit
                or provenance.get("runtimeVersion") != bundle.manifest.runtime_version
                or provenance.get("sourceDateEpoch") != int(bundle.manifest.created_at.timestamp())
            ):
                raise RuntimeError("Runtime payload provenance identity differs from signed manifest")
            ownership = assert_payload_provenance(
                provenance,
                expected_files=provenance_expected,
                expected_executables=expected_executables,
            )
            spdx_expected = {
                record.path: (record.byte_length, record.sha256)
                for record in bundle.manifest.files
                if record.path != SPDX_PATH
            }
            spdx_ownership = dict(ownership)
            spdx_ownership[PROVENANCE_PATH] = OFFERAGENT_COMPONENT.spdx_id
            packages = sbom.get("packages") if isinstance(sbom, dict) else None
            assert_sbom_packages_allowed(packages)
            provenance_components = provenance.get("components") if isinstance(provenance, dict) else None
            if (
                not isinstance(packages, list)
                or not isinstance(provenance_components, list)
                or {item.get("SPDXID") for item in packages if isinstance(item, dict)}
                != {item.get("id") for item in provenance_components if isinstance(item, dict)}
            ):
                raise RuntimeError("Runtime SPDX packages differ from frozen payload provenance")
            assert_runtime_spdx_document(
                sbom,
                expected_files=spdx_expected,
                expected_ownership=spdx_ownership,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError) as error:
            raise SystemExit(f"invalid Runtime payload provenance/SPDX closure: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
