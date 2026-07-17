from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from scripts import build_local_windows_plugin, local_windows_runtime_build
from scripts.local_windows_runtime_build import merge_identical_tree, normalize_pyinstaller_base_library


def test_pyinstaller_base_library_normalization_is_order_independent(tmp_path: Path) -> None:
    work = tmp_path / "build"
    root = tmp_path / "dist"
    (root / "_internal").mkdir(parents=True)
    work.mkdir()
    source = work / "base_library.zip"
    output = root / "_internal" / "base_library.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("b.pyc", b"b")
        archive.writestr("a.pyc", b"a")
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("a.pyc", b"a")
        archive.writestr("b.pyc", b"b")

    normalize_pyinstaller_base_library(root, work)

    assert source.read_bytes() == output.read_bytes()


def test_merge_identical_tree_accepts_shared_bytes_and_rejects_collisions(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "merged"
    for root, payload in ((first, b"same"), (second, b"same")):
        (root / "_internal").mkdir(parents=True)
        (root / "_internal" / "runtime.dll").write_bytes(payload)
    destination.mkdir()

    merge_identical_tree(first, destination)
    merge_identical_tree(second, destination)
    (second / "_internal" / "runtime.dll").write_bytes(b"different")

    with pytest.raises(RuntimeError, match="collision differs"):
        merge_identical_tree(second, destination)


def test_pyinstaller_environment_rebuilds_path_from_the_active_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", r"E:\miniconda;E:\untrusted-tools")
    monkeypatch.setenv("CONDA_PREFIX", r"E:\miniconda")
    monkeypatch.setenv("_CONDA_EXE", r"E:\miniconda\Scripts\conda.exe")

    environment = local_windows_runtime_build._pyinstaller_environment()

    assert "untrusted-tools" not in environment["PATH"].casefold()
    assert "CONDA_PREFIX" not in environment
    assert "_CONDA_EXE" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONSAFEPATH"] == "1"


def test_local_plugin_build_requires_the_pinned_python_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sys, "version_info", (3, 11, 0))

    with pytest.raises(SystemExit, match=r"CPython 3\.12"):
        build_local_windows_plugin.require_local_build_host(tmp_path / "rg.exe")


def test_local_plugin_build_checks_python_protocol_freshness(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def capture(command: list[str], **_: Any) -> None:
        commands.append(command)

    monkeypatch.setattr(subprocess, "run", capture)

    build_local_windows_plugin.run_static_gates()

    assert [sys.executable, "-m", "offeragent_harness.protocol.schemas", "check"] in commands


def test_local_plugin_build_rejects_the_retired_static_gate_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_local_windows_plugin.py",
            "--output",
            str(tmp_path / "output"),
            "--ripgrep-executable",
            str(tmp_path / "rg.exe"),
            "--skip-static-checks",
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        build_local_windows_plugin.main()

    assert "unrecognized arguments: --skip-static-checks" in capsys.readouterr().err


def test_local_runtime_root_rejects_every_unexpected_executable(tmp_path: Path) -> None:
    for name in build_local_windows_plugin.LOCAL_RUNTIME_EXES:
        (tmp_path / name).write_bytes(b"MZ")
    build_local_windows_plugin._require_exact_root_executables(tmp_path)
    (tmp_path / "retired-or-unknown.exe").write_bytes(b"MZ")

    with pytest.raises(RuntimeError, match=r"unexpected=.*retired-or-unknown\.exe"):
        build_local_windows_plugin._require_exact_root_executables(tmp_path)


def test_source_tree_identity_covers_the_complete_schema_tree() -> None:
    identity = build_local_windows_plugin.source_tree_identity()

    assert identity.schema_tree_sha256 == build_local_windows_plugin._schema_tree_digest(
        build_local_windows_plugin.ROOT / "schema"
    )


def test_nested_schema_change_updates_both_source_and_schema_identity(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    schema = repository / "schema"
    nested_schema = schema / "examples" / "initialize.json"
    nested_schema.parent.mkdir(parents=True)
    nested_schema.write_bytes(b'{"version":1}\n')
    source = repository / "src" / "worker.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    files = (source, nested_schema)

    before = build_local_windows_plugin._source_tree_identity_from_files(
        files,
        repository_root=repository,
        schema_root=schema,
        project_root=repository,
    )
    nested_schema.write_bytes(b'{"version":2}\n')
    after = build_local_windows_plugin._source_tree_identity_from_files(
        files,
        repository_root=repository,
        schema_root=schema,
        project_root=repository,
    )

    assert before.source_tree_sha256 != after.source_tree_sha256
    assert before.schema_tree_sha256 != after.schema_tree_sha256
    assert (
        dict(before.project_sources)["project:schema/examples/initialize.json"]
        != dict(after.project_sources)["project:schema/examples/initialize.json"]
    )


def test_frozen_runtime_schema_must_match_the_captured_source_snapshot(tmp_path: Path) -> None:
    source_schema = tmp_path / "source-schema"
    source_schema.mkdir()
    (source_schema / "protocol.json").write_bytes(b'{"version":1}\n')
    expected = build_local_windows_plugin._schema_tree_digest(source_schema)
    runtime_schema = tmp_path / "runtime" / "_internal" / "offeragent_harness" / "_schema"
    runtime_schema.mkdir(parents=True)
    (runtime_schema / "protocol.json").write_bytes(b'{"version":1}\n')

    build_local_windows_plugin._require_embedded_schema_identity(tmp_path / "runtime", expected)
    (runtime_schema / "protocol.json").write_bytes(b'{"version":2}\n')

    with pytest.raises(RuntimeError, match="schema differs from the source identity snapshot"):
        build_local_windows_plugin._require_embedded_schema_identity(tmp_path / "runtime", expected)


def test_source_change_check_rejects_schema_drift_even_if_overall_digest_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = build_local_windows_plugin.SourceTreeIdentity(
        source_tree_sha256="sha256:" + "a" * 64,
        schema_tree_sha256="sha256:" + "b" * 64,
        project_sources=(("project:src/worker.py", "sha256:" + "d" * 64),),
    )
    monkeypatch.setattr(
        build_local_windows_plugin,
        "source_tree_identity",
        lambda: build_local_windows_plugin.SourceTreeIdentity(
            source_tree_sha256=expected.source_tree_sha256,
            schema_tree_sha256="sha256:" + "c" * 64,
            project_sources=expected.project_sources,
        ),
    )

    with pytest.raises(RuntimeError, match="source tree changed"):
        build_local_windows_plugin.require_source_tree_unchanged(expected)
