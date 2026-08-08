from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import cast

import pytest
from scripts import build_local_windows_plugin

from offeragent_harness.documents import (
    BackendIdentity,
    CancellationCheckpoint,
    CanonicalParseFailure,
    CanonicalParseSuccess,
    DocumentErrorCode,
    DocumentMediaType,
    DocumentParser,
    DocumentParserConfig,
    DocumentParseRequest,
    DocumentSource,
    ExtractionMethod,
    PageProvenance,
    ParsedDocument,
    ParsedPage,
    decode_canonical_response,
    encode_canonical_request,
)
from offeragent_harness.foundation.canonical import canonical_json_bytes
from offeragent_harness.runtime import local_process_host
from offeragent_harness.runtime.document_parser_profile import (
    BUNDLED_DOCUMENT_PARSER_CONFIG,
    DOCUMENT_PARSER_MAX_REQUEST_BYTES,
    DOCUMENT_PARSER_MAX_RESPONSE_BYTES,
)
from offeragent_harness.runtime.local_process_host import LocalProcessHostError, run_document_extract


class _StaticParser:
    def __init__(self, *, text: str = "第一题: 介绍一下你自己") -> None:
        self._text = text

    def parse(self, request: DocumentParseRequest, cancellation: CancellationCheckpoint) -> ParsedDocument:
        cancellation.checkpoint()
        digest = request.source.expected_sha256
        assert digest is not None
        backend = BackendIdentity("fixture", "1")
        page = ParsedPage(
            provenance=PageProvenance(request.source.source_id, digest, 1, backend, backend),
            extraction_method=ExtractionMethod.EMBEDDED_TEXT,
            text=self._text,
            ocr_regions=(),
        )
        return ParsedDocument(
            request_id=request.request_id,
            source_id=request.source.source_id,
            absolute_path=request.source.absolute_path,
            source_sha256=digest,
            media_type=request.source.declared_media_type,
            source_byte_size=request.source.absolute_path.stat().st_size,
            parser_config_fingerprint=BUNDLED_DOCUMENT_PARSER_CONFIG.fingerprint,
            pages=(page,),
        )


def _staged_request(cwd: Path, *, document_directory: str = "document-000") -> tuple[Path, bytes]:
    source = cwd / document_directory / "source.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-fixture")
    request = DocumentParseRequest(
        request_id="request-1",
        source=DocumentSource(
            source_id="document-1",
            absolute_path=source,
            declared_media_type=DocumentMediaType.PDF,
            expected_sha256="sha256:" + "a" * 64,
        ),
    )
    return source, encode_canonical_request(request)


def test_document_extract_uses_the_shared_profile_and_returns_complete_canonical_output(
    tmp_path: Path,
) -> None:
    cwd = tmp_path.resolve()
    _source, payload = _staged_request(cwd)
    captured: list[DocumentParserConfig] = []

    def parser_factory(config: DocumentParserConfig) -> DocumentParser:
        captured.append(config)
        return cast(DocumentParser, _StaticParser())

    stdout = io.BytesIO()
    assert run_document_extract(io.BytesIO(payload), stdout, cwd=cwd, parser_factory=parser_factory) == 0

    assert captured == [BUNDLED_DOCUMENT_PARSER_CONFIG]
    assert len(stdout.getvalue()) <= DOCUMENT_PARSER_MAX_RESPONSE_BYTES
    response = decode_canonical_response(stdout.getvalue())
    assert isinstance(response, CanonicalParseSuccess)
    assert response.request_id == "request-1"
    assert response.result.text == "第一题: 介绍一下你自己"


def test_process_host_dispatches_only_the_fixed_document_extract_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def run_fixed(_stdin: object, _stdout: object) -> int:
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(local_process_host, "run_document_extract", run_fixed)
    info = local_process_host.VerifiedRuntimeInfo("1", "1", "1", "1", "1", "a" * 40)

    assert local_process_host.main(["document-extract"], info_loader=lambda _root, _image: info) == 0
    assert calls == 1


def test_document_extract_rejects_every_path_outside_the_exact_staging_shape(tmp_path: Path) -> None:
    cwd = (tmp_path / "run").resolve()
    cwd.mkdir()
    outside = (tmp_path / "outside.bin").resolve()
    outside.write_bytes(b"%PDF-fixture")
    payload = encode_canonical_request(
        DocumentParseRequest(
            request_id="request-escape",
            source=DocumentSource(
                source_id="document-escape",
                absolute_path=outside,
                declared_media_type=DocumentMediaType.PDF,
                expected_sha256="sha256:" + "b" * 64,
            ),
        )
    )

    with pytest.raises(LocalProcessHostError, match="document_source_path_invalid"):
        run_document_extract(io.BytesIO(payload), io.BytesIO(), cwd=cwd)


def test_document_extract_rejects_a_semantically_normalized_absolute_path(tmp_path: Path) -> None:
    cwd = tmp_path.resolve()
    source, payload = _staged_request(cwd)
    request = json.loads(payload)
    request["source"]["absolutePath"] = str(source.parent / "." / source.name).replace(
        f"{source.parent.name}{os.sep}",
        f"{source.parent.name}{os.sep}.{os.sep}",
    )
    payload_with_dot = canonical_json_bytes(request)
    assert payload_with_dot != payload

    with pytest.raises(LocalProcessHostError, match="document_source_path_noncanonical"):
        run_document_extract(io.BytesIO(payload_with_dot), io.BytesIO(), cwd=cwd)


def test_document_extract_rejects_reparse_or_symlinked_staged_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = (tmp_path / "run").resolve()
    directory = cwd / "document-000"
    directory.mkdir(parents=True)
    target = (tmp_path / "outside.bin").resolve()
    target.write_bytes(b"%PDF-fixture")
    source = directory / "source.bin"
    try:
        source.symlink_to(target)
    except OSError:
        source.write_bytes(target.read_bytes())
        original = local_process_host._is_link_or_reparse
        monkeypatch.setattr(
            local_process_host,
            "_is_link_or_reparse",
            lambda path, metadata: path == source or original(path, metadata),
        )
    payload = encode_canonical_request(
        DocumentParseRequest(
            request_id="request-link",
            source=DocumentSource(
                source_id="document-link",
                absolute_path=source,
                declared_media_type=DocumentMediaType.PDF,
                expected_sha256="sha256:" + "c" * 64,
            ),
        )
    )

    with pytest.raises(LocalProcessHostError, match="document_source_path_unsafe"):
        run_document_extract(io.BytesIO(payload), io.BytesIO(), cwd=cwd)


def test_document_extract_rejects_oversized_stdin_before_parser_initialization(tmp_path: Path) -> None:
    initialized = False

    def parser_factory(config: DocumentParserConfig) -> DocumentParser:
        nonlocal initialized
        initialized = True
        return cast(DocumentParser, _StaticParser())

    with pytest.raises(LocalProcessHostError, match="input_size_invalid"):
        run_document_extract(
            io.BytesIO(b"x" * (DOCUMENT_PARSER_MAX_REQUEST_BYTES + 1)),
            io.BytesIO(),
            cwd=tmp_path.resolve(),
            parser_factory=parser_factory,
        )

    assert initialized is False


def test_document_extract_never_slices_an_oversized_response(tmp_path: Path) -> None:
    cwd = tmp_path.resolve()
    _source, payload = _staged_request(cwd)

    def parser_factory(config: DocumentParserConfig) -> DocumentParser:
        return cast(DocumentParser, _StaticParser(text="x" * DOCUMENT_PARSER_MAX_RESPONSE_BYTES))

    stdout = io.BytesIO()
    run_document_extract(io.BytesIO(payload), stdout, cwd=cwd, parser_factory=parser_factory)

    assert len(stdout.getvalue()) <= DOCUMENT_PARSER_MAX_RESPONSE_BYTES
    response = decode_canonical_response(stdout.getvalue())
    assert isinstance(response, CanonicalParseFailure)
    assert response.code is DocumentErrorCode.OUTPUT_LIMIT_EXCEEDED


def test_local_build_adds_exactly_the_three_pinned_ocr_models() -> None:
    add_data = build_local_windows_plugin._document_parser_add_data()
    models = [source.name for source, target in add_data if target == "rapidocr/models"]

    assert tuple(models) == build_local_windows_plugin.DOCUMENT_PARSER_MODEL_FILES
    assert {"PIL.Image", "onnxruntime", "pymupdf", "rapidocr"} <= set(
        build_local_windows_plugin.DOCUMENT_PARSER_HIDDEN_IMPORTS
    )
    assert all(source.is_file() and not source.is_symlink() for source, _target in add_data)


def test_local_build_adds_exactly_the_required_cuda_dlls() -> None:
    binaries = build_local_windows_plugin._document_parser_add_binaries()
    expected = {
        (filename, f"{package.replace('.', '/')}/bin")
        for package, filenames in build_local_windows_plugin.DOCUMENT_PARSER_CUDA_DLLS.items()
        for filename in filenames
    }

    assert {(source.name, target) for source, target in binaries} == expected
    assert all(source.is_file() and not source.is_symlink() for source, _ in binaries)


def test_frozen_parser_payload_rejects_extra_or_missing_models(tmp_path: Path) -> None:
    package_root = tmp_path / "_internal" / "rapidocr"
    models = package_root / "models"
    models.mkdir(parents=True)
    (package_root / "config.yaml").write_bytes(b"config")
    (package_root / "default_models.yaml").write_bytes(b"models")
    for name in build_local_windows_plugin.DOCUMENT_PARSER_MODEL_FILES:
        (models / name).write_bytes(b"model")
    for package, filenames in build_local_windows_plugin.DOCUMENT_PARSER_CUDA_DLLS.items():
        binary_root = tmp_path / "_internal" / Path(*package.split(".")) / "bin"
        binary_root.mkdir(parents=True)
        for name in filenames:
            (binary_root / name).write_bytes(b"MZ")

    build_local_windows_plugin._require_document_parser_payload(tmp_path)
    (models / "unapproved.onnx").write_bytes(b"model")

    with pytest.raises(RuntimeError, match="model set is not exact"):
        build_local_windows_plugin._require_document_parser_payload(tmp_path)
