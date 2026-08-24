"""Dependency seams for PDF decoding, image decoding, and OCR inference."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class BackendIdentity:
    name: str
    version: str
    model: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.version:
            raise ValueError("backend identity requires name and version")
        if self.model is not None and not self.model:
            raise ValueError("backend model must be non-empty when provided")

    def to_json(self) -> dict[str, str]:
        value = {"name": self.name, "version": self.version}
        if self.model is not None:
            value["model"] = self.model
        return value


@dataclass(frozen=True, slots=True)
class RasterImage:
    """A dependency-neutral, packed 8-bit RGB raster."""

    width: int
    height: int
    rgb: bytes

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("raster dimensions must be positive")
        if len(self.rgb) != self.width * self.height * 3:
            raise ValueError("RGB raster byte length does not match its dimensions")

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass(frozen=True, slots=True)
class ImageInfo:
    width: int
    height: int
    frame_count: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or self.frame_count <= 0:
            raise ValueError("image dimensions and frame count must be positive")


@dataclass(frozen=True, slots=True)
class OcrPoint:
    x: float
    y: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.x) or not math.isfinite(self.y):
            raise ValueError("OCR coordinates must be finite")

    def to_json(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True, slots=True)
class OcrRegion:
    polygon: tuple[OcrPoint, OcrPoint, OcrPoint, OcrPoint]
    text: str
    confidence: float

    def __post_init__(self) -> None:
        if len(self.polygon) != 4:
            raise ValueError("OCR regions require exactly four polygon points")
        if not self.text:
            raise ValueError("OCR region text must not be empty")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("OCR confidence must be between zero and one")

    def to_json(self) -> dict[str, object]:
        return {
            "polygon": [point.to_json() for point in self.polygon],
            "text": self.text,
            "confidence": self.confidence,
        }


@runtime_checkable
class CancellationCheckpoint(Protocol):
    def checkpoint(self) -> None: ...


@runtime_checkable
class PdfDocument(Protocol):
    @property
    def page_count(self) -> int: ...

    def extract_text(self, page_index: int, *, sort: bool) -> str: ...

    def page_size_points(self, page_index: int) -> tuple[float, float]: ...

    def render_rgb(self, page_index: int, *, dpi: int) -> RasterImage: ...

    def close(self) -> None: ...


@runtime_checkable
class PdfBackend(Protocol):
    @property
    def identity(self) -> BackendIdentity: ...

    def open_pdf(self, data: bytes) -> PdfDocument: ...


@runtime_checkable
class ImageDecoder(Protocol):
    @property
    def identity(self) -> BackendIdentity: ...

    def inspect(self, data: bytes, *, media_type: str) -> ImageInfo: ...

    def decode_frame_rgb(self, data: bytes, *, media_type: str, frame_index: int) -> RasterImage: ...


@runtime_checkable
class OcrEngine(Protocol):
    @property
    def identity(self) -> BackendIdentity: ...

    def recognize(self, image: RasterImage) -> Sequence[OcrRegion]: ...


__all__ = [
    "BackendIdentity",
    "CancellationCheckpoint",
    "ImageDecoder",
    "ImageInfo",
    "OcrEngine",
    "OcrPoint",
    "OcrRegion",
    "PdfBackend",
    "PdfDocument",
    "RasterImage",
]
