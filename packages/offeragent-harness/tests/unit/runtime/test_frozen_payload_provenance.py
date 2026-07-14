from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest
from scripts.frozen_payload_provenance import (
    OFFERAGENT_COMPONENT,
    PROVENANCE_PATH,
    SPDX_PATH,
    AuthenticodeTransform,
    FrozenPayloadError,
    SourceClassifier,
    assert_payload_provenance,
    build_payload_provenance,
    capture_pyinstaller_target,
    make_source_record,
    merge_frozen_evidence,
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


def test_undeclared_post_capture_executable_tamper_is_rejected(tmp_path: Path) -> None:
    project, python, root = _target_tree(tmp_path)
    classifier = SourceClassifier(project_root=project, python_root=python, distributions=())
    evidence = capture_pyinstaller_target(
        name="target",
        root=root,
        work_root=tmp_path / "build" / "target",
        classifier=classifier,
    )
    executable = root / "target.exe"
    executable.write_bytes(executable.read_bytes() + b"-undeclared-mutation")

    with pytest.raises(FrozenPayloadError, match="post-capture payload mutation"):
        build_payload_provenance(
            runtime=root,
            evidence=evidence,
            static_sources={},
            architecture="x64",
            build_commit="test",
            runtime_version="2.0.0",
            source_date_epoch=1_784_000_000,
            runtime_dependency_closure_sha256="sha256:" + "1" * 64,
            uv_lock_sha256="sha256:" + "2" * 64,
            require_release_targets=False,
            require_signed_executables=False,
        )


def test_declared_authenticode_executable_transform_is_accepted(tmp_path: Path) -> None:
    project, python, root = _target_tree(tmp_path)
    classifier = SourceClassifier(project_root=project, python_root=python, distributions=())
    evidence = capture_pyinstaller_target(
        name="target",
        root=root,
        work_root=tmp_path / "build" / "target",
        classifier=classifier,
    )
    executable = root / "target.exe"
    pre_sign_sha256 = evidence.files["target.exe"].captured_sha256
    executable.write_bytes(executable.read_bytes() + b"-valid-authenticode-transform")
    post_sign_sha256 = "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest()

    document = build_payload_provenance(
        runtime=root,
        evidence=evidence,
        static_sources={},
        architecture="x64",
        build_commit="test",
        runtime_version="2.0.0",
        source_date_epoch=1_784_000_000,
        runtime_dependency_closure_sha256="sha256:" + "1" * 64,
        uv_lock_sha256="sha256:" + "2" * 64,
        executable_transforms={
            "target.exe": AuthenticodeTransform(
                "target.exe",
                pre_sign_sha256,
                post_sign_sha256,
                0x8664,
                True,
            )
        },
        require_release_targets=False,
    )

    assert document["transforms"] == [
        {
            "authenticodeVerified": True,
            "kind": "authenticode-sign-v1",
            "path": "target.exe",
            "peMachine": 0x8664,
            "postSignSha256": post_sign_sha256,
            "preSignSha256": pre_sign_sha256,
        }
    ]


def test_provenance_rejects_signed_payload_hash_tamper(tmp_path: Path) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"signed payload")
    source = make_source_record(OFFERAGENT_COMPONENT, "project:payload.bin", "project-file", payload)
    document = {
        "architecture": "x64",
        "buildCommit": "test",
        "builder": "scripts/build_windows_release.py",
        "components": [OFFERAGENT_COMPONENT.payload()],
        "files": [
            {
                "byteLength": payload.stat().st_size,
                "capturedByteLength": payload.stat().st_size,
                "capturedSha256": source.sha256,
                "componentId": OFFERAGENT_COMPONENT.spdx_id,
                "path": "payload.bin",
                "sha256": source.sha256,
                "sourceRefs": [source.identifier],
                "targets": [],
            }
        ],
        "runtimeDependencyClosureSha256": "sha256:" + "1" * 64,
        "runtimeVersion": "2.0.0",
        "schemaVersion": 1,
        "selfReferenceExclusions": [PROVENANCE_PATH, SPDX_PATH, "runtime-manifest.json", "runtime-manifest.sig"],
        "sourceDateEpoch": 1_784_000_000,
        "sources": [source.payload()],
        "targets": [],
        "transforms": [],
        "uvLockSha256": "sha256:" + "2" * 64,
    }

    with pytest.raises(FrozenPayloadError, match="hashes differ"):
        assert_payload_provenance(
            document,
            expected_files={"payload.bin": (payload.stat().st_size, "sha256:" + "f" * 64)},
            require_release_targets=False,
        )
