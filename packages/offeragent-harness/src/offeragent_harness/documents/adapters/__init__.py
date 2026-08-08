"""Optional local document dependency adapters."""

from .pillow import PillowImageDecoder
from .production import BUNDLED_RAPIDOCR_EXECUTION_PROFILE, build_bundled_parser
from .pymupdf import PyMuPdfBackend
from .rapidocr import (
    LocalModelFile,
    RapidOcrAdapter,
    RapidOcrExecutionProfile,
    RapidOcrExecutionProvider,
    RapidOcrLocalModels,
    RasterArrayColorOrder,
)

__all__ = [
    "BUNDLED_RAPIDOCR_EXECUTION_PROFILE",
    "LocalModelFile",
    "PillowImageDecoder",
    "PyMuPdfBackend",
    "RapidOcrAdapter",
    "RapidOcrExecutionProfile",
    "RapidOcrExecutionProvider",
    "RapidOcrLocalModels",
    "RasterArrayColorOrder",
    "build_bundled_parser",
]
