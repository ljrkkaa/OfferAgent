"""Production composition for the bundled CUDA RapidOCR document parser."""

from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Protocol, cast

from ..errors import BackendUnavailableError
from ..models import DocumentParserConfig
from ..parser import DocumentParser
from .pillow import PillowImageDecoder
from .pymupdf import PyMuPdfBackend
from .rapidocr import (
    LocalModelFile,
    RapidOcrAdapter,
    RapidOcrExecutionProfile,
    RapidOcrExecutionProvider,
    RapidOcrLocalModels,
    RasterArrayColorOrder,
)

_DETECTOR = LocalModelFile(
    relative_path="PP-OCRv6_det_small.onnx",
    byte_size=9_929_594,
    sha256="sha256:090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
)
_CLASSIFIER = LocalModelFile(
    relative_path="ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    byte_size=585_532,
    sha256="sha256:e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
)
_RECOGNIZER = LocalModelFile(
    relative_path="PP-OCRv6_rec_small.onnx",
    byte_size=21_234_383,
    sha256="sha256:6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
)

BUNDLED_RAPIDOCR_EXECUTION_PROFILE = RapidOcrExecutionProfile(
    RapidOcrExecutionProvider.CUDA,
    device_id=0,
)


class _EnumValues(Protocol):
    @property
    def CH(self) -> object: ...

    @property
    def MOBILE(self) -> object: ...

    @property
    def SMALL(self) -> object: ...

    @property
    def PPOCRV4(self) -> object: ...

    @property
    def PPOCRV6(self) -> object: ...


class _RapidProfileModule(Protocol):
    @property
    def LangDet(self) -> _EnumValues: ...

    @property
    def LangCls(self) -> _EnumValues: ...

    @property
    def LangRec(self) -> _EnumValues: ...

    @property
    def ModelType(self) -> _EnumValues: ...

    @property
    def OCRVersion(self) -> _EnumValues: ...


def build_bundled_parser(
    config: DocumentParserConfig,
    *,
    execution: RapidOcrExecutionProfile = BUNDLED_RAPIDOCR_EXECUTION_PROFILE,
) -> DocumentParser:
    """Build the production parser using only hash-pinned models inside ``rapidocr``."""

    if config.ocr_execution_provider != execution.provider.value or config.ocr_device_id != execution.device_id:
        raise ValueError("document parser config and RapidOCR execution profile must match")

    model_root = _installed_rapidocr_model_root()
    try:
        rapidocr = cast(_RapidProfileModule, import_module("rapidocr"))
    except ImportError as error:
        raise BackendUnavailableError("rapidocr") from error
    models = RapidOcrLocalModels(
        model_root=model_root,
        detector=_DETECTOR,
        classifier=_CLASSIFIER,
        recognizer=_RECOGNIZER,
        recognition_keys=None,
    )
    ocr = RapidOcrAdapter.from_installed(
        models=models,
        params={
            "Global.log_level": "critical",
            "Global.text_score": 0.0,
            "Det.lang_type": rapidocr.LangDet.CH,
            "Det.model_type": rapidocr.ModelType.SMALL,
            "Det.ocr_version": rapidocr.OCRVersion.PPOCRV6,
            "Cls.lang_type": rapidocr.LangCls.CH,
            "Cls.model_type": rapidocr.ModelType.MOBILE,
            "Cls.ocr_version": rapidocr.OCRVersion.PPOCRV4,
            "Rec.lang_type": rapidocr.LangRec.CH,
            "Rec.model_type": rapidocr.ModelType.SMALL,
            "Rec.ocr_version": rapidocr.OCRVersion.PPOCRV6,
        },
        array_color_order=RasterArrayColorOrder.BGR,
        execution=execution,
    )
    return DocumentParser(
        config=config,
        pdf_backend=PyMuPdfBackend.from_installed(),
        image_decoder=PillowImageDecoder.from_installed(),
        ocr_engine=ocr,
    )


def _installed_rapidocr_model_root() -> Path:
    spec = find_spec("rapidocr")
    if spec is None or spec.origin is None:
        raise BackendUnavailableError("rapidocr")
    package_root = Path(spec.origin).resolve().parent
    return package_root / "models"


__all__ = ["BUNDLED_RAPIDOCR_EXECUTION_PROFILE", "build_bundled_parser"]
