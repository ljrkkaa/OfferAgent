from __future__ import annotations

import importlib.metadata
from email.message import Message

import pytest
from scripts.runtime_sbom import (
    RuntimeSbomError,
    assert_package_records_match_runtime_closure,
    runtime_closure_fingerprint,
    runtime_distribution_closure,
)


class _Distribution:
    def __init__(self, name: str, version: str, requires: list[str] | None = None) -> None:
        self._name = name
        self._version = version
        self._requires = requires

    @property
    def metadata(self) -> Message:
        value = Message()
        value["Name"] = self._name
        value["Version"] = self._version
        return value

    @property
    def version(self) -> str:
        return self._version

    @property
    def requires(self) -> list[str] | None:
        return self._requires


def test_runtime_distribution_closure_excludes_inactive_extras_and_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "root": _Distribution(
            "Root",
            "1.0",
            [
                "Core>=2,<3",
                "pytest>=8; extra == 'dev'",
                "old-win; python_version < '3.0'",
            ],
        ),
        "core": _Distribution("Core", "2.5", ["Leaf==4"]),
        "leaf": _Distribution("Leaf", "4", ["Core>=2"]),
    }
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: values[name.casefold()])

    result = runtime_distribution_closure(("Root",))

    assert [(item.canonical_name, item.version) for item in result] == [
        ("core", "2.5"),
        ("leaf", "4"),
        ("root", "1.0"),
    ]


def test_runtime_distribution_closure_fails_closed_on_unsatisfied_or_direct_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "root": _Distribution("Root", "1", ["Core>=3"]),
        "core": _Distribution("Core", "2", None),
    }
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: values[name.casefold()])
    with pytest.raises(RuntimeSbomError, match="outside"):
        runtime_distribution_closure(("Root",))

    values["root"] = _Distribution("Root", "1", ["Core @ https://example.invalid/core.whl"])
    with pytest.raises(RuntimeSbomError, match="direct URL"):
        runtime_distribution_closure(("Root",))


def test_real_runtime_closure_contains_runtime_only_dependencies() -> None:
    closure = runtime_distribution_closure()
    names = {item.canonical_name for item in closure}
    assert {"offeragent-harness", "anyio", "httpx", "jsonschema", "pydantic"} <= names
    assert names.isdisjoint({"cryptography", "pytest", "ruff", "mypy", "pyinstaller", "semgrep", "import-linter"})
    assert runtime_closure_fingerprint(closure).startswith("sha256:")


def test_sbom_package_records_must_equal_the_runtime_closure() -> None:
    closure = runtime_distribution_closure()
    records = [{"name": item.name, "version": item.version} for item in closure]
    resolved = assert_package_records_match_runtime_closure(records)
    assert [(item.canonical_name, item.version) for item in resolved] == [
        (item.canonical_name, item.version) for item in closure
    ]
    records.append({"name": "pytest", "version": "8.0"})
    with pytest.raises(RuntimeSbomError, match="differs"):
        assert_package_records_match_runtime_closure(records)
