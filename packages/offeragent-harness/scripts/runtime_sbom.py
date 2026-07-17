"""Deterministic logical distribution closure for the personal frozen Runtime.

The build environment intentionally contains test, lint, and packaging
tools.  This module follows active ``Requires-Dist`` edges to validate the
declared application graph and records its fingerprint in provenance.  It does
*not* claim to describe the shipped PyInstaller payload; file-level truth comes
from ``frozen_payload.py`` and the five TOCs for each local onedir target.
Every resolved version is still checked against its incoming specifier.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


class RuntimeSbomError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeDistribution:
    name: str
    canonical_name: str
    version: str
    distribution: importlib.metadata.Distribution


def runtime_distribution_closure(
    roots: Iterable[str] = ("offeragent-harness",),
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[RuntimeDistribution, ...]:
    """Resolve the exact active runtime dependency graph from package metadata.

    Extras are deliberately empty.  Missing metadata, malformed requirements,
    unsatisfied versions and ambiguous duplicate canonical names fail closed.
    """

    marker_environment: dict[str, str] = {
        key: str(value) for key, value in (default_environment() if environment is None else environment).items()
    }
    marker_environment["extra"] = ""
    pending: deque[Requirement] = deque()
    for root in roots:
        try:
            requirement = Requirement(root)
        except InvalidRequirement as error:
            raise RuntimeSbomError("Runtime SBOM root requirement is invalid") from error
        if requirement.extras or requirement.url is not None:
            raise RuntimeSbomError("Runtime SBOM roots cannot use extras or direct URLs")
        pending.append(requirement)
    if not pending:
        raise RuntimeSbomError("Runtime SBOM requires at least one root distribution")

    resolved: dict[str, RuntimeDistribution] = {}
    constraints: dict[str, list[Requirement]] = {}
    while pending:
        requirement = pending.popleft()
        if requirement.marker is not None and not requirement.marker.evaluate(marker_environment):
            continue
        canonical = canonicalize_name(requirement.name)
        constraints.setdefault(canonical, []).append(requirement)
        current = resolved.get(canonical)
        if current is not None:
            _require_satisfied(current, constraints[canonical])
            continue
        try:
            distribution = importlib.metadata.distribution(requirement.name)
        except importlib.metadata.PackageNotFoundError as error:
            raise RuntimeSbomError(f"Runtime dependency metadata is missing: {canonical}") from error
        name = distribution.metadata["Name"]
        version = distribution.version
        if not isinstance(name, str) or not name.strip() or not isinstance(version, str) or not version.strip():
            raise RuntimeSbomError(f"Runtime dependency metadata is malformed: {canonical}")
        actual_canonical = canonicalize_name(name)
        if actual_canonical != canonical:
            raise RuntimeSbomError(f"Runtime dependency canonical name changed: {canonical}")
        item = RuntimeDistribution(name.strip(), actual_canonical, version.strip(), distribution)
        _require_satisfied(item, constraints[canonical])
        resolved[canonical] = item

        for raw_requirement in distribution.requires or ():
            try:
                child = Requirement(raw_requirement)
            except InvalidRequirement as error:
                raise RuntimeSbomError(f"Runtime dependency requirement is malformed: {canonical}") from error
            if child.url is not None:
                raise RuntimeSbomError(f"Runtime dependency uses a direct URL: {canonical}")
            if child.marker is None or child.marker.evaluate(marker_environment):
                pending.append(child)

    if not resolved:
        raise RuntimeSbomError("Runtime SBOM dependency closure is empty")
    return tuple(resolved[name] for name in sorted(resolved))


def runtime_closure_fingerprint(items: Iterable[RuntimeDistribution]) -> str:
    identities = [
        {"name": item.canonical_name, "version": item.version}
        for item in sorted(items, key=lambda value: value.canonical_name)
    ]
    if not identities or len({item["name"] for item in identities}) != len(identities):
        raise RuntimeSbomError("Runtime dependency closure is empty or duplicated")
    payload = (json.dumps(identities, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def assert_package_records_match_runtime_closure(packages: object) -> tuple[RuntimeDistribution, ...]:
    """Independently compare SBOM name/version records with the active closure."""

    if not isinstance(packages, list):
        raise RuntimeSbomError("Runtime SBOM packages are malformed")
    actual: dict[str, str] = {}
    for package in packages:
        if not isinstance(package, dict):
            raise RuntimeSbomError("Runtime SBOM package record is malformed")
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not name.strip() or not isinstance(version, str) or not version.strip():
            raise RuntimeSbomError("Runtime SBOM package identity is malformed")
        canonical = canonicalize_name(name)
        if canonical in actual:
            raise RuntimeSbomError("Runtime SBOM package identity is duplicated")
        actual[canonical] = version.strip()
    expected_items = runtime_distribution_closure()
    expected = {item.canonical_name: item.version for item in expected_items}
    if actual != expected:
        raise RuntimeSbomError("Runtime SBOM differs from the declared runtime dependency closure")
    return expected_items


def _require_satisfied(item: RuntimeDistribution, requirements: Iterable[Requirement]) -> None:
    for requirement in requirements:
        if requirement.extras:
            raise RuntimeSbomError(f"Runtime dependency unexpectedly enables extras: {item.canonical_name}")
        if requirement.specifier and not requirement.specifier.contains(item.version, prereleases=True):
            raise RuntimeSbomError(
                f"Runtime dependency version is outside its declared constraint: {item.canonical_name}"
            )


__all__ = [
    "RuntimeDistribution",
    "RuntimeSbomError",
    "assert_package_records_match_runtime_closure",
    "runtime_closure_fingerprint",
    "runtime_distribution_closure",
]
