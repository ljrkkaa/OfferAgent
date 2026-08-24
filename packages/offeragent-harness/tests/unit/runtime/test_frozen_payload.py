from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest
from scripts.frozen_payload import (
    FrozenFile,
    FrozenPayloadError,
    FrozenRuntimeEvidence,
    SourceClassifier,
    SourceRecord,
    capture_pyinstaller_target,
    merge_frozen_evidence,
    verify_project_source_snapshot,
)


def _write_toc(work: Path, name: str, value: object) -> None:
    (work / f"{name}-00.toc").write_text(repr(value), encoding="utf-8", newline="")


def _target_tree(tmp_path: Path, *, duplicate: bool = False, omit_dll: bool = False) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    python = tmp_path / "python"
    root = tmp_path / "dist" / "target"
    work = tmp_path / "build" / "target"
    for directory in (project, python / "Lib", root / "_internal", work):
        directory.mkdir(parents=True, exist_ok=True)
    entrypoint = project / "entry.py"
    entrypoint.write_text("print('frozen')\n", encoding="utf-8")
    stdlib = python / "Lib" / "os.py"
    stdlib.write_text("name = 'stdlib'\n", encoding="utf-8")
    python_dll = python / "python312.dll"
    python_dll.write_bytes(b"cpython-dll")
    generated_exe = work / "target.exe"
    generated_exe.write_bytes(b"frozen-exe")
    (root / "target.exe").write_bytes(generated_exe.read_bytes())
    (root / "_internal" / "python312.dll").write_bytes(python_dll.read_bytes())

    collect = [
        ("target.exe", str(generated_exe), "EXECUTABLE"),
    ]
    if not omit_dll:
        collect.append(("python312.dll", str(python_dll), "BINARY"))
    if duplicate:
        collect.append(("python312.dll", str(python_dll), "BINARY"))
    _write_toc(work, "COLLECT", (collect,))
    _write_toc(
        work,
        "Analysis",
        (
            [
                ("entry", str(entrypoint), "PYSOURCE"),
                ("os", str(stdlib), "PYMODULE"),
            ],
        ),
    )
    _write_toc(work, "EXE", ([("run.exe", str(entrypoint), "EXECUTABLE")],))
    _write_toc(work, "PKG", ([("entry", str(entrypoint), "PYSOURCE")],))
    _write_toc(work, "PYZ", ([("os", str(stdlib), "PYMODULE")],))
    return project, python, root


def test_capture_rejects_collect_toc_that_omits_an_onedir_file(tmp_path: Path) -> None:
    project, python, root = _target_tree(tmp_path, omit_dll=True)
    classifier = SourceClassifier(project_root=project, python_root=python, distributions=())

    with pytest.raises(FrozenPayloadError, match="exactly cover"):
        capture_pyinstaller_target(
            name="target",
            root=root,
            work_root=tmp_path / "build" / "target",
            classifier=classifier,
        )


def test_capture_rejects_duplicate_collect_destination(tmp_path: Path) -> None:
    project, python, root = _target_tree(tmp_path, duplicate=True)
    classifier = SourceClassifier(project_root=project, python_root=python, distributions=())

    with pytest.raises(FrozenPayloadError, match="duplicate"):
        capture_pyinstaller_target(
            name="target",
            root=root,
            work_root=tmp_path / "build" / "target",
            classifier=classifier,
        )


def test_capture_rejects_missing_required_pyinstaller_toc(tmp_path: Path) -> None:
    project, python, root = _target_tree(tmp_path)
    (tmp_path / "build" / "target" / "PYZ-00.toc").unlink()
    classifier = SourceClassifier(project_root=project, python_root=python, distributions=())

    with pytest.raises(FrozenPayloadError, match="PYZ TOC"):
        capture_pyinstaller_target(
            name="target",
            root=root,
            work_root=tmp_path / "build" / "target",
            classifier=classifier,
        )


def test_capture_rejects_unknown_dll_source(tmp_path: Path) -> None:
    project, python, root = _target_tree(tmp_path)
    work = tmp_path / "build" / "target"
    unknown = tmp_path / "vendor" / "mystery.dll"
    unknown.parent.mkdir()
    unknown.write_bytes(b"unknown-vendor")
    (root / "_internal" / "mystery.dll").write_bytes(unknown.read_bytes())
    collect = ast.literal_eval((work / "COLLECT-00.toc").read_text(encoding="utf-8"))
    collect[0].append(("mystery.dll", str(unknown), "BINARY"))
    _write_toc(work, "COLLECT", collect)
    classifier = SourceClassifier(project_root=project, python_root=python, distributions=())

    with pytest.raises(FrozenPayloadError, match="unmapped PyInstaller source"):
        capture_pyinstaller_target(
            name="target",
            root=root,
            work_root=work,
            classifier=classifier,
        )


def test_post_capture_dll_tamper_is_rejected(tmp_path: Path) -> None:
    project, python, root = _target_tree(tmp_path)
    classifier = SourceClassifier(project_root=project, python_root=python, distributions=())
    evidence = capture_pyinstaller_target(
        name="target",
        root=root,
        work_root=tmp_path / "build" / "target",
        classifier=classifier,
    )
    (root / "_internal" / "python312.dll").write_bytes(b"post-capture-tamper")

    with pytest.raises(FrozenPayloadError, match="captured bytes"):
        merge_frozen_evidence(merged_root=root, targets=[evidence])


def test_merge_rejects_cross_target_source_drift_by_stable_identity(tmp_path: Path) -> None:
    worker = FrozenRuntimeEvidence()
    process_host = FrozenRuntimeEvidence()
    worker.register_source(
        SourceRecord(
            "source-worker",
            "SPDXRef-Package-offeragent-harness",
            "python-source",
            "project:src/offeragent_harness/runtime/shared.py",
            "sha256:" + "a" * 64,
        )
    )
    process_host.register_source(
        SourceRecord(
            "source-process-host",
            "SPDXRef-Package-offeragent-harness",
            "python-source",
            "project:src/offeragent_harness/runtime/shared.py",
            "sha256:" + "b" * 64,
        )
    )

    with pytest.raises(FrozenPayloadError, match="stable locator/component/kind"):
        merge_frozen_evidence(merged_root=tmp_path, targets=[worker, process_host])


def test_merge_consolidates_identical_case_variant_paths(tmp_path: Path) -> None:
    relative = "_internal/VCRUNTIME140.dll"
    payload = b"same-runtime"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    merged_path = tmp_path / Path(relative)
    merged_path.parent.mkdir(parents=True)
    merged_path.write_bytes(payload)
    worker = FrozenRuntimeEvidence(
        files={
            relative: FrozenFile(
                relative,
                "SPDXRef-Package-microsoft-windows-runtime",
                len(payload),
                digest,
                ("source-worker",),
                ("offeragent-worker",),
            )
        }
    )
    process_host_path = "_internal/vcruntime140.dll"
    process_host = FrozenRuntimeEvidence(
        files={
            process_host_path: FrozenFile(
                process_host_path,
                "SPDXRef-Package-microsoft-windows-runtime",
                len(payload),
                digest,
                ("source-process-host",),
                ("offeragent-process-host",),
            )
        }
    )

    merged = merge_frozen_evidence(merged_root=tmp_path, targets=[process_host, worker])

    assert set(merged.files) == {relative}
    assert merged.files[relative] == FrozenFile(
        relative,
        "SPDXRef-Package-microsoft-windows-runtime",
        len(payload),
        digest,
        ("source-process-host", "source-worker"),
        ("offeragent-process-host", "offeragent-worker"),
    )


@pytest.mark.parametrize(
    ("component_id", "captured_byte_length", "captured_sha256", "message"),
    [
        ("SPDXRef-Package-wrong-owner", 12, "sha256:" + "a" * 64, "ownership differs"),
        (
            "SPDXRef-Package-microsoft-windows-runtime",
            13,
            "sha256:" + "b" * 64,
            "capture differs",
        ),
    ],
)
def test_merge_rejects_conflicting_case_variant_paths(
    tmp_path: Path,
    component_id: str,
    captured_byte_length: int,
    captured_sha256: str,
    message: str,
) -> None:
    relative = "_internal/VCRUNTIME140.dll"
    payload = b"same-runtime"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    merged_path = tmp_path / Path(relative)
    merged_path.parent.mkdir(parents=True)
    merged_path.write_bytes(payload)
    worker = FrozenRuntimeEvidence(
        files={
            relative: FrozenFile(
                relative,
                "SPDXRef-Package-microsoft-windows-runtime",
                len(payload),
                digest,
                (),
                ("offeragent-worker",),
            )
        }
    )
    process_host_path = "_internal/vcruntime140.dll"
    process_host = FrozenRuntimeEvidence(
        files={
            process_host_path: FrozenFile(
                process_host_path,
                component_id,
                captured_byte_length,
                captured_sha256,
                (),
                ("offeragent-process-host",),
            )
        }
    )

    with pytest.raises(FrozenPayloadError, match=message):
        merge_frozen_evidence(merged_root=tmp_path, targets=[worker, process_host])


def test_merge_rejects_file_record_path_that_differs_from_its_key(tmp_path: Path) -> None:
    relative = "_internal/runtime.dll"
    payload = b"runtime"
    merged_path = tmp_path / Path(relative)
    merged_path.parent.mkdir(parents=True)
    merged_path.write_bytes(payload)
    evidence = FrozenRuntimeEvidence(
        files={
            relative: FrozenFile(
                "_internal/other.dll",
                "SPDXRef-Package-microsoft-windows-runtime",
                len(payload),
                "sha256:" + hashlib.sha256(payload).hexdigest(),
                (),
                ("offeragent-worker",),
            )
        }
    )

    with pytest.raises(FrozenPayloadError, match="path identity"):
        merge_frozen_evidence(merged_root=tmp_path, targets=[evidence])


def test_frozen_project_source_must_match_the_build_start_snapshot(tmp_path: Path) -> None:
    project, python, root = _target_tree(tmp_path)
    evidence = capture_pyinstaller_target(
        name="target",
        root=root,
        work_root=tmp_path / "build" / "target",
        classifier=SourceClassifier(project_root=project, python_root=python, distributions=()),
    )
    project_sources = [source for source in evidence.sources.values() if source.locator.startswith("project:")]
    assert project_sources
    matching_snapshot = {source.locator: source.sha256 for source in project_sources}
    verify_project_source_snapshot(evidence, matching_snapshot)
    mismatched_snapshot = {source.locator: "sha256:" + "0" * 64 for source in project_sources}

    with pytest.raises(FrozenPayloadError, match="project source differs from source identity"):
        verify_project_source_snapshot(evidence, mismatched_snapshot)

    source = project_sources[0]
    evidence.sources[source.identifier] = SourceRecord(
        source.identifier,
        "SPDXRef-Package-wrong-owner",
        source.kind,
        source.locator,
        source.sha256,
    )
    with pytest.raises(FrozenPayloadError, match="project source has the wrong component"):
        verify_project_source_snapshot(evidence, matching_snapshot)
