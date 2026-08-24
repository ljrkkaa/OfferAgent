"""Immutable document parsing configuration, request, and result models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from offeragent_harness.foundation.canonical import canonical_json_sha256

from .interfaces import BackendIdentity, OcrRegion

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class DocumentMediaType(str, Enum):
    PDF = "application/pdf"
    PNG = "image/png"
    JPEG = "image/jpeg"
    WEBP = "image/webp"


class ExtractionMethod(str, Enum):
    EMBEDDED_TEXT = "embedded_text"
    OCR = "ocr"


@dataclass(frozen=True, slots=True)
class DocumentParserConfig:
    """The single authoritative parser policy supplied by the host composition."""

    max_file_bytes: int
    max_pdf_pages: int
    max_image_frames: int
    max_raster_dimension: int
    max_raster_pixels: int
    max_page_characters: int
    max_total_characters: int
    max_page_text_bytes: int
    max_total_text_bytes: int
    max_ocr_regions_per_page: int
    pdf_render_dpi: int
    pdf_sort_text: bool
    ocr_execution_provider: str = "cpu"
    ocr_device_id: int = 0

    def __post_init__(self) -> None:
        integer_limits = {
            "max_file_bytes": self.max_file_bytes,
            "max_pdf_pages": self.max_pdf_pages,
            "max_image_frames": self.max_image_frames,
            "max_raster_dimension": self.max_raster_dimension,
            "max_raster_pixels": self.max_raster_pixels,
            "max_page_characters": self.max_page_characters,
            "max_total_characters": self.max_total_characters,
            "max_page_text_bytes": self.max_page_text_bytes,
            "max_total_text_bytes": self.max_total_text_bytes,
            "max_ocr_regions_per_page": self.max_ocr_regions_per_page,
            "pdf_render_dpi": self.pdf_render_dpi,
        }
        invalid = [name for name, value in integer_limits.items() if isinstance(value, bool) or value <= 0]
        if invalid:
            raise ValueError(f"document parser limits must be positive integers: {', '.join(invalid)}")
        if self.max_page_characters > self.max_total_characters:
            raise ValueError("max_page_characters cannot exceed max_total_characters")
        if self.max_page_text_bytes > self.max_total_text_bytes:
            raise ValueError("max_page_text_bytes cannot exceed max_total_text_bytes")
        if self.ocr_execution_provider not in {"cpu", "cuda"}:
            raise ValueError("ocr_execution_provider must be 'cpu' or 'cuda'")
        if isinstance(self.ocr_device_id, bool) or not isinstance(self.ocr_device_id, int) or self.ocr_device_id < 0:
            raise ValueError("ocr_device_id must be a non-negative integer")

    def to_json(self) -> dict[str, object]:
        return {
            "maxFileBytes": self.max_file_bytes,
            "maxPdfPages": self.max_pdf_pages,
            "maxImageFrames": self.max_image_frames,
            "maxRasterDimension": self.max_raster_dimension,
            "maxRasterPixels": self.max_raster_pixels,
            "maxPageCharacters": self.max_page_characters,
            "maxTotalCharacters": self.max_total_characters,
            "maxPageTextBytes": self.max_page_text_bytes,
            "maxTotalTextBytes": self.max_total_text_bytes,
            "maxOcrRegionsPerPage": self.max_ocr_regions_per_page,
            "pdfRenderDpi": self.pdf_render_dpi,
            "pdfSortText": self.pdf_sort_text,
            "ocrExecutionProvider": self.ocr_execution_provider,
            "ocrDeviceId": self.ocr_device_id,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_json_sha256(self.to_json())


@dataclass(frozen=True, slots=True)
class DocumentSource:
    source_id: str
    absolute_path: Path
    declared_media_type: DocumentMediaType
    expected_sha256: str | None

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("document source_id must not be empty")
        if not self.absolute_path.is_absolute():
            raise ValueError("document source path must be absolute")
        if self.expected_sha256 is not None and _SHA256_PATTERN.fullmatch(self.expected_sha256) is None:
            raise ValueError("expected_sha256 must be a lowercase sha256 identity")


@dataclass(frozen=True, slots=True)
class DocumentParseRequest:
    request_id: str
    source: DocumentSource

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("document request_id must not be empty")


@dataclass(frozen=True, slots=True)
class PageProvenance:
    source_id: str
    source_sha256: str
    page_number: int
    input_backend: BackendIdentity
    extraction_backend: BackendIdentity

    def __post_init__(self) -> None:
        if not self.source_id or _SHA256_PATTERN.fullmatch(self.source_sha256) is None:
            raise ValueError("page provenance requires a source id and SHA-256 identity")
        if self.page_number <= 0:
            raise ValueError("page numbers are one-based")

    def to_json(self) -> dict[str, object]:
        return {
            "sourceId": self.source_id,
            "sourceSha256": self.source_sha256,
            "pageNumber": self.page_number,
            "inputBackend": self.input_backend.to_json(),
            "extractionBackend": self.extraction_backend.to_json(),
        }


@dataclass(frozen=True, slots=True)
class ParsedPage:
    provenance: PageProvenance
    extraction_method: ExtractionMethod
    text: str
    ocr_regions: tuple[OcrRegion, ...]

    def __post_init__(self) -> None:
        if self.extraction_method is ExtractionMethod.EMBEDDED_TEXT and self.ocr_regions:
            raise ValueError("PDF text pages cannot contain OCR regions")
        if self.extraction_method is ExtractionMethod.OCR:
            joined = "\n".join(region.text for region in self.ocr_regions)
            if self.text != joined:
                raise ValueError("OCR page text must exactly match its ordered OCR regions")

    def to_json(self) -> dict[str, object]:
        return {
            "provenance": self.provenance.to_json(),
            "extractionMethod": self.extraction_method.value,
            "text": self.text,
            "characterCount": len(self.text),
            "utf8ByteCount": len(self.text.encode("utf-8", errors="strict")),
            "ocrRegions": [region.to_json() for region in self.ocr_regions],
        }


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    request_id: str
    source_id: str
    absolute_path: Path
    source_sha256: str
    media_type: DocumentMediaType
    source_byte_size: int
    parser_config_fingerprint: str
    pages: tuple[ParsedPage, ...]

    def __post_init__(self) -> None:
        if not self.request_id or not self.source_id:
            raise ValueError("parsed documents require request and source ids")
        if not self.absolute_path.is_absolute():
            raise ValueError("parsed document source path must be absolute")
        if _SHA256_PATTERN.fullmatch(self.source_sha256) is None:
            raise ValueError("parsed document requires a SHA-256 identity")
        if self.source_byte_size < 0:
            raise ValueError("source_byte_size cannot be negative")
        if _SHA256_PATTERN.fullmatch(self.parser_config_fingerprint) is None:
            raise ValueError("parser config fingerprint must be a SHA-256 identity")
        expected_numbers = tuple(range(1, len(self.pages) + 1))
        actual_numbers = tuple(page.provenance.page_number for page in self.pages)
        if actual_numbers != expected_numbers:
            raise ValueError("parsed document pages must be contiguous and one-based")
        if any(page.provenance.source_sha256 != self.source_sha256 for page in self.pages):
            raise ValueError("page and document source hashes must match")
        if any(page.provenance.source_id != self.source_id for page in self.pages):
            raise ValueError("page and document source ids must match")

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    def to_json(self) -> dict[str, object]:
        document_text = self.text
        cursor = 0
        serialized_pages: list[dict[str, object]] = []
        for index, page in enumerate(self.pages):
            if index:
                cursor += len(b"\n\n")
            page_json = page.to_json()
            page_json["utf8StartByte"] = cursor
            cursor += len(page.text.encode("utf-8", errors="strict"))
            page_json["utf8EndByte"] = cursor
            serialized_pages.append(page_json)
        return {
            "source": {
                "sourceId": self.source_id,
                "sha256": self.source_sha256,
                "mediaType": self.media_type.value,
                "byteSize": self.source_byte_size,
            },
            "parserConfigFingerprint": self.parser_config_fingerprint,
            "pageCount": len(self.pages),
            "text": document_text,
            "totalCharacters": len(document_text),
            "totalTextUtf8Bytes": len(document_text.encode("utf-8", errors="strict")),
            "pages": serialized_pages,
        }


__all__ = [
    "DocumentMediaType",
    "DocumentParseRequest",
    "DocumentParserConfig",
    "DocumentSource",
    "ExtractionMethod",
    "PageProvenance",
    "ParsedDocument",
    "ParsedPage",
]
