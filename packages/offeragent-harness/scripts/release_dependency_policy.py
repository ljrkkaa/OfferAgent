"""Shared dependency policy for release SBOM generation and independent audit."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

FORBIDDEN_DISTRIBUTIONS = {
    "django",
    "django-apscheduler",
    "django-unfold",
    "khoj",
    "langchain",
    "langchain-community",
    "langchain-core",
    "pgserver",
    "psycopg",
    "psycopg2",
    "psycopg2-binary",
}


def normalize_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def assert_sbom_packages_allowed(packages: object) -> None:
    """Reject incomplete/malformed SBOMs and every retired server dependency."""

    if not isinstance(packages, list) or not packages:
        raise RuntimeError("Runtime SBOM must contain its Python distributions")
    names: list[str] = []
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise RuntimeError("Runtime SBOM contains a malformed Python distribution")
        name = normalize_distribution_name(package["name"])
        if not name:
            raise RuntimeError("Runtime SBOM contains an empty Python distribution")
        names.append(name)
    forbidden = sorted(name for name in names if name in FORBIDDEN_DISTRIBUTIONS or name.startswith("langchain-"))
    if forbidden:
        raise RuntimeError(f"Runtime SBOM contains retired server dependencies: {forbidden}")


def assert_runtime_spdx_document(
    document: object,
    *,
    expected_files: Mapping[str, tuple[int, str]] | None = None,
    expected_ownership: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Validate the standard SPDX 2.3 file/package/relationship closure."""

    expected_keys = {
        "SPDXID",
        "creationInfo",
        "dataLicense",
        "documentNamespace",
        "files",
        "name",
        "packages",
        "relationships",
        "spdxVersion",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise RuntimeError("Runtime SBOM document shape is invalid")
    if (
        document["SPDXID"] != "SPDXRef-DOCUMENT"
        or document["spdxVersion"] != "SPDX-2.3"
        or document["dataLicense"] != "CC0-1.0"
        or document["name"] != "OfferAgent Runtime"
        or not isinstance(document["documentNamespace"], str)
        or re.fullmatch(
            r"https://spdx\.offeragent\.invalid/runtime/[A-Za-z0-9._+/-]{1,240}",
            document["documentNamespace"],
        )
        is None
    ):
        raise RuntimeError("Runtime SPDX document identity is invalid")
    creation = document["creationInfo"]
    if (
        not isinstance(creation, dict)
        or set(creation) != {"created", "creators"}
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", creation.get("created", "")) is None
        or creation.get("creators") != ["Tool: OfferAgent scripts/build_windows_release.py"]
    ):
        raise RuntimeError("Runtime SPDX creation info is invalid")
    packages = document["packages"]
    assert_sbom_packages_allowed(packages)
    assert isinstance(packages, list)
    package_keys = {
        "SPDXID",
        "copyrightText",
        "downloadLocation",
        "filesAnalyzed",
        "licenseConcluded",
        "licenseDeclared",
        "name",
        "packageVerificationCode",
        "versionInfo",
    }
    identifiers: list[str] = []
    package_verification: dict[str, str] = {}
    for package in packages:
        if not isinstance(package, dict) or set(package) != package_keys:
            raise RuntimeError("Runtime SPDX package shape is invalid")
        identifier = package["SPDXID"]
        verification = package["packageVerificationCode"]
        if (
            not isinstance(identifier, str)
            or re.fullmatch(r"SPDXRef-Package-[A-Za-z0-9.-]{1,160}", identifier) is None
            or package["downloadLocation"] != "NOASSERTION"
            or package["filesAnalyzed"] is not True
            or package["copyrightText"] != "NOASSERTION"
            or not isinstance(package["licenseConcluded"], str)
            or package["licenseConcluded"] not in {"AGPL-3.0-or-later", "NOASSERTION"}
            or not isinstance(package["licenseDeclared"], str)
            or package["licenseDeclared"] not in {"AGPL-3.0-or-later", "NOASSERTION"}
            or not isinstance(package["name"], str)
            or not package["name"]
            or not isinstance(package["versionInfo"], str)
            or not package["versionInfo"]
            or not isinstance(verification, dict)
            or set(verification) != {"packageVerificationCodeValue"}
            or re.fullmatch(r"[a-f0-9]{40}", verification.get("packageVerificationCodeValue", "")) is None
        ):
            raise RuntimeError("Runtime SPDX package identity is invalid")
        identifiers.append(identifier)
        package_verification[identifier] = verification["packageVerificationCodeValue"]
    if len(set(identifiers)) != len(identifiers):
        raise RuntimeError("Runtime SPDX package identifiers are duplicated")
    files = document["files"]
    if not isinstance(files, list) or not files:
        raise RuntimeError("Runtime SPDX files are missing")
    file_ids: dict[str, str] = {}
    casefold_paths: set[str] = set()
    file_sha1: dict[str, str] = {}
    file_sha256: dict[str, str] = {}
    for file_record in files:
        if not isinstance(file_record, dict) or set(file_record) != {
            "SPDXID",
            "checksums",
            "copyrightText",
            "fileName",
            "licenseConcluded",
            "licenseInfoInFiles",
        }:
            raise RuntimeError("Runtime SPDX file shape is invalid")
        identifier = file_record["SPDXID"]
        filename = file_record["fileName"]
        checksums = file_record["checksums"]
        if (
            not isinstance(identifier, str)
            or re.fullmatch(r"SPDXRef-File-[a-f0-9]{64}", identifier) is None
            or not isinstance(filename, str)
            or not filename.startswith("./")
            or "\\" in filename
            or any(part in {"", ".", ".."} for part in filename[2:].split("/"))
            or not isinstance(checksums, list)
            or len(checksums) != 2
            or any(
                not isinstance(checksum, dict) or set(checksum) != {"algorithm", "checksumValue"}
                for checksum in checksums
            )
            or checksums[0].get("algorithm") != "SHA1"
            or re.fullmatch(r"[a-f0-9]{40}", checksums[0].get("checksumValue", "")) is None
            or checksums[1].get("algorithm") != "SHA256"
            or re.fullmatch(r"[a-f0-9]{64}", checksums[1].get("checksumValue", "")) is None
            or file_record["copyrightText"] != "NOASSERTION"
            or file_record["licenseConcluded"] != "NOASSERTION"
            or file_record["licenseInfoInFiles"] != ["NOASSERTION"]
        ):
            raise RuntimeError("Runtime SPDX file identity is invalid")
        path = filename[2:]
        expected_id = f"SPDXRef-File-{hashlib.sha256(path.encode('utf-8')).hexdigest()}"
        if (
            identifier != expected_id
            or path in file_ids
            or path.casefold() in casefold_paths
            or identifier in file_ids.values()
        ):
            raise RuntimeError("Runtime SPDX file identifier is duplicated or non-canonical")
        file_ids[path] = identifier
        casefold_paths.add(path.casefold())
        file_sha1[path] = checksums[0]["checksumValue"]
        file_sha256[path] = f"sha256:{checksums[1]['checksumValue']}"

    relationships = document["relationships"]
    if not isinstance(relationships, list):
        raise RuntimeError("Runtime SPDX relationships are malformed")
    expected_describes = [
        {
            "relatedSpdxElement": identifier,
            "relationshipType": "DESCRIBES",
            "spdxElementId": "SPDXRef-DOCUMENT",
        }
        for identifier in identifiers
    ]
    describes = relationships[: len(expected_describes)]
    if describes != expected_describes:
        raise RuntimeError("Runtime SPDX relationships do not exactly describe its packages")

    contains = relationships[len(expected_describes) : len(expected_describes) + len(files)]
    ownership: dict[str, str] = {}
    if len(contains) != len(files):
        raise RuntimeError("Runtime SPDX file relationships are missing")
    path_by_id = {identifier: path for path, identifier in file_ids.items()}
    for relation in contains:
        if (
            not isinstance(relation, dict)
            or set(relation) != {"relatedSpdxElement", "relationshipType", "spdxElementId"}
            or relation["relationshipType"] != "CONTAINS"
            or not isinstance(relation["relatedSpdxElement"], str)
            or relation["relatedSpdxElement"] not in path_by_id
            or not isinstance(relation["spdxElementId"], str)
            or relation["spdxElementId"] not in identifiers
        ):
            raise RuntimeError("Runtime SPDX file relationship is invalid")
        path = path_by_id[relation["relatedSpdxElement"]]
        if path in ownership:
            raise RuntimeError("Runtime SPDX file has duplicate ownership")
        ownership[path] = relation["spdxElementId"]
    expected_contains = [
        {
            "relatedSpdxElement": file_ids[path],
            "relationshipType": "CONTAINS",
            "spdxElementId": ownership[path],
        }
        for path in sorted(file_ids)
    ]
    if contains != expected_contains or set(ownership) != set(file_ids):
        raise RuntimeError("Runtime SPDX file ownership is not canonical")

    root = next((item for item in packages if normalize_distribution_name(item["name"]) == "offeragent-harness"), None)
    if root is None or root["licenseDeclared"] != "AGPL-3.0-or-later":
        raise RuntimeError("Runtime SPDX root package license is missing")
    root_id = root["SPDXID"]
    expected_dependencies = [
        {
            "relatedSpdxElement": identifier,
            "relationshipType": "DEPENDS_ON",
            "spdxElementId": root_id,
        }
        for identifier in sorted(set(identifiers) - {root_id})
    ]
    dependencies = relationships[len(expected_describes) + len(files) :]
    if dependencies != expected_dependencies:
        raise RuntimeError("Runtime SPDX dependency relationships are not canonical")

    for identifier in identifiers:
        verification = hashlib.sha1(
            "".join(sorted(file_sha1[path] for path, owner in ownership.items() if owner == identifier)).encode("ascii")
        ).hexdigest()
        if package_verification[identifier] != verification:
            raise RuntimeError("Runtime SPDX package verification code is invalid")
    if expected_files is not None:
        expected_hashes = {path: digest for path, (_length, digest) in expected_files.items()}
        if file_sha256 != expected_hashes:
            raise RuntimeError("Runtime SPDX file set or hashes differ from the signed payload")
    if expected_ownership is not None and ownership != dict(expected_ownership):
        raise RuntimeError("Runtime SPDX ownership differs from frozen payload provenance")
    return ownership


__all__ = [
    "FORBIDDEN_DISTRIBUTIONS",
    "assert_runtime_spdx_document",
    "assert_sbom_packages_allowed",
    "normalize_distribution_name",
]
