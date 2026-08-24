from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

import offeragent_harness.documents.adapters.rapidocr as rapidocr_adapter_module
from offeragent_harness.documents import (
    DocumentMediaType,
    DocumentParserConfig,
    DocumentParseRequest,
    DocumentSource,
    OcrPoint,
    RasterImage,
)
from offeragent_harness.documents.adapters import (
    LocalModelFile,
    PillowImageDecoder,
    PyMuPdfBackend,
    RapidOcrAdapter,
    RapidOcrExecutionProfile,
    RapidOcrExecutionProvider,
    RapidOcrLocalModels,
    RasterArrayColorOrder,
    build_bundled_parser,
)
from offeragent_harness.documents.errors import (
    BackendInputError,
    BackendOutputError,
    BackendUnavailableError,
)


def _runtime_dependency(name: str) -> Any:
    return __import__(name, fromlist=["*"])


Image: Any = _runtime_dependency("PIL.Image")
ImageDraw: Any = _runtime_dependency("PIL.ImageDraw")
ImageFont: Any = _runtime_dependency("PIL.ImageFont")


def _model_file(path: Path, data: bytes) -> LocalModelFile:
    path.write_bytes(data)
    return LocalModelFile(
        relative_path=path.name,
        byte_size=len(data),
        sha256=f"sha256:{hashlib.sha256(data).hexdigest()}",
    )


def _config() -> DocumentParserConfig:
    return DocumentParserConfig(
        max_file_bytes=64 * 1024 * 1024,
        max_pdf_pages=100,
        max_image_frames=10,
        max_raster_dimension=16_384,
        max_raster_pixels=40_000_000,
        max_page_characters=250_000,
        max_total_characters=1_000_000,
        max_page_text_bytes=1_000_000,
        max_total_text_bytes=4_000_000,
        max_ocr_regions_per_page=10_000,
        pdf_render_dpi=160,
        pdf_sort_text=True,
    )


class Token:
    def checkpoint(self) -> None:
        return


def test_local_rapidocr_models_are_contained_and_hash_verified(tmp_path: Path) -> None:
    models = RapidOcrLocalModels(
        model_root=tmp_path.resolve(),
        detector=_model_file(tmp_path / "det.onnx", b"detector"),
        classifier=_model_file(tmp_path / "cls.onnx", b"classifier"),
        recognizer=_model_file(tmp_path / "rec.onnx", b"recognizer"),
        recognition_keys=_model_file(tmp_path / "keys.txt", b"a\nb\n"),
    )

    paths, fingerprint = models.verify()

    assert set(paths) == {"detector", "classifier", "recognizer", "recognitionKeys"}
    assert all(Path(path).is_relative_to(tmp_path) for path in paths.values())
    assert fingerprint.startswith("sha256:")

    (tmp_path / "rec.onnx").write_bytes(b"tampered!")
    with pytest.raises(BackendInputError, match="size_mismatch"):
        models.verify()


def test_cuda_profile_fails_closed_when_cuda_provider_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = RapidOcrLocalModels(
        model_root=tmp_path.resolve(),
        detector=_model_file(tmp_path / "det.onnx", b"detector"),
        classifier=_model_file(tmp_path / "cls.onnx", b"classifier"),
        recognizer=_model_file(tmp_path / "rec.onnx", b"recognizer"),
        recognition_keys=None,
    )

    class CpuOnlyOnnxRuntime:
        @staticmethod
        def preload_dlls(**_kwargs: object) -> None:
            return

        @staticmethod
        def get_available_providers() -> tuple[str, ...]:
            return ("CPUExecutionProvider",)

        @staticmethod
        def get_device() -> str:
            return "CPU"

    original_loader = rapidocr_adapter_module._load_runtime_dependency

    def load_dependency(name: str) -> Any:
        if name == "onnxruntime":
            return CpuOnlyOnnxRuntime()
        return original_loader(name)

    monkeypatch.setattr(rapidocr_adapter_module, "_load_runtime_dependency", load_dependency)

    with pytest.raises(BackendUnavailableError, match="rapidocr-cuda"):
        RapidOcrAdapter.from_installed(
            models=models,
            params={},
            array_color_order=RasterArrayColorOrder.BGR,
            execution=RapidOcrExecutionProfile(RapidOcrExecutionProvider.CUDA),
        )


def test_parser_fingerprint_includes_ocr_execution_profile() -> None:
    cpu = _config()
    cuda = replace(cpu, ocr_execution_provider="cuda")

    assert cpu.fingerprint != cuda.fingerprint


@pytest.mark.parametrize(
    ("media_type", "pillow_format"), (("image/png", "PNG"), ("image/jpeg", "JPEG"), ("image/webp", "WEBP"))
)
def test_pillow_adapter_decodes_only_the_declared_format(media_type: str, pillow_format: str) -> None:
    encoded = BytesIO()
    Image.new("RGB", (3, 2), (10, 20, 30)).save(encoded, format=pillow_format)
    data = encoded.getvalue()
    decoder = PillowImageDecoder.from_installed()

    info = decoder.inspect(data, media_type=media_type)
    raster = decoder.decode_frame_rgb(data, media_type=media_type, frame_index=0)

    assert (info.width, info.height, info.frame_count) == (3, 2, 1)
    assert (raster.width, raster.height, len(raster.rgb)) == (3, 2, 18)
    wrong_media_type = "image/jpeg" if media_type != "image/jpeg" else "image/png"
    with pytest.raises(BackendInputError):
        decoder.inspect(data, media_type=wrong_media_type)


def test_pymupdf_adapter_extracts_text_and_renders_missing_text_pages() -> None:
    pymupdf: Any = _runtime_dependency("pymupdf")

    source = pymupdf.open()
    text_page = source.new_page(width=200, height=100)
    text_page.insert_text((20, 50), "interview question")
    source.new_page(width=100, height=80)
    data = source.tobytes()
    source.close()

    backend = PyMuPdfBackend.from_installed()
    document = backend.open_pdf(data)
    try:
        assert document.page_count == 2
        assert "interview question" in document.extract_text(0, sort=True)
        assert document.extract_text(1, sort=True) == ""
        assert document.page_size_points(1) == (100.0, 80.0)
        raster = document.render_rgb(1, dpi=144)
        assert (raster.width, raster.height) == (200, 160)
    finally:
        document.close()


@dataclass
class RapidOutput:
    boxes: object
    txts: object
    scores: object


class RapidEngine:
    def __init__(self, output: RapidOutput) -> None:
        self.output = output
        self.inputs: list[object] = []

    def __call__(self, image: object) -> RapidOutput:
        self.inputs.append(image)
        return self.output


def test_rapidocr_adapter_validates_result_shapes_without_dependency_objects() -> None:
    output = RapidOutput(
        boxes=(((0, 0), (10, 0), (10, 5), (0, 5)),),
        txts=("问题",),
        scores=(0.91,),
    )
    engine = RapidEngine(output)
    adapter = RapidOcrAdapter(
        engine,  # type: ignore[arg-type]
        package_version="fixture",
        model_id="sha256:" + "a" * 64,
        array_converter=lambda raster: (raster.width, raster.height),
        array_color_order=RasterArrayColorOrder.RGB,
    )

    regions = adapter.recognize(RasterImage(2, 2, b"\0" * 12))

    assert regions[0].text == "问题"
    assert regions[0].polygon[2] == OcrPoint(10.0, 5.0)
    assert engine.inputs == [(2, 2)]

    malformed = RapidOcrAdapter(
        RapidEngine(RapidOutput(boxes=(), txts=("orphan",), scores=())),  # type: ignore[arg-type]
        package_version="fixture",
        model_id="sha256:" + "a" * 64,
        array_converter=lambda raster: raster,
        array_color_order=RasterArrayColorOrder.RGB,
    )
    with pytest.raises(BackendOutputError, match="lengths differ"):
        malformed.recognize(RasterImage(1, 1, b"\0" * 3))


def test_bundled_offline_rapidocr_real_smoke(tmp_path: Path) -> None:
    image_path = (tmp_path / "ocr-smoke.png").resolve()
    image = Image.new("RGB", (1200, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.text((45, 95), "INTERVIEW 42", font=ImageFont.load_default(size=100), fill="black")
    image.save(image_path, format="PNG")
    data = image_path.read_bytes()

    parser = build_bundled_parser(
        _config(),
        execution=RapidOcrExecutionProfile(RapidOcrExecutionProvider.CPU),
    )
    parsed = parser.parse(
        DocumentParseRequest(
            request_id="real-ocr-smoke",
            source=DocumentSource(
                source_id="real-ocr-source",
                absolute_path=image_path,
                declared_media_type=DocumentMediaType.PNG,
                expected_sha256=f"sha256:{hashlib.sha256(data).hexdigest()}",
            ),
        ),
        Token(),
    )

    assert parsed.text == "INTERVIEW 42"
    assert len(parsed.pages[0].ocr_regions) == 1
    assert parsed.pages[0].provenance.extraction_backend.name == "rapidocr:bgr:cpu"
    assert parsed.pages[0].provenance.extraction_backend.version == "3.9.1"
