"""Fail-closed local parsing pipeline for PDF and raster image attachments."""

from __future__ import annotations

import hashlib
import math
import os
import stat
from collections.abc import Iterable
from pathlib import Path

from .errors import (
    BackendExecutionError,
    BackendInputError,
    BackendOutputError,
    BackendUnavailableError,
    DocumentErrorCode,
    DocumentParseError,
    EncryptedPdfError,
)
from .interfaces import CancellationCheckpoint, ImageDecoder, OcrEngine, OcrRegion, PdfBackend, RasterImage
from .models import (
    DocumentMediaType,
    DocumentParserConfig,
    DocumentParseRequest,
    ExtractionMethod,
    PageProvenance,
    ParsedDocument,
    ParsedPage,
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PDF_SIGNATURE = b"%PDF-"
_JPEG_SIGNATURE = b"\xff\xd8\xff"


def detect_media_type(data: bytes) -> DocumentMediaType:
    """Identify only the four explicitly supported formats by their magic bytes."""

    if data.startswith(_PDF_SIGNATURE):
        return DocumentMediaType.PDF
    if data.startswith(_PNG_SIGNATURE):
        return DocumentMediaType.PNG
    if data.startswith(_JPEG_SIGNATURE):
        return DocumentMediaType.JPEG
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return DocumentMediaType.WEBP
    raise DocumentParseError(
        DocumentErrorCode.UNSUPPORTED_MEDIA_TYPE,
        "source bytes do not match a supported PDF, PNG, JPEG, or WebP signature",
    )


class DocumentParser:
    """Coordinates untrusted decoders behind small dependency-neutral interfaces."""

    def __init__(
        self,
        *,
        config: DocumentParserConfig,
        pdf_backend: PdfBackend,
        image_decoder: ImageDecoder,
        ocr_engine: OcrEngine,
    ) -> None:
        self.config = config
        self._pdf = pdf_backend
        self._images = image_decoder
        self._ocr = ocr_engine

    def parse(self, request: DocumentParseRequest, cancellation: CancellationCheckpoint) -> ParsedDocument:
        cancellation.checkpoint()
        data = self._read_source(request.source.absolute_path, source_id=request.source.source_id)
        cancellation.checkpoint()
        digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
        if request.source.expected_sha256 is not None and digest != request.source.expected_sha256:
            raise DocumentParseError(
                DocumentErrorCode.SOURCE_HASH_MISMATCH,
                "source content does not match expected_sha256",
                details={
                    "sourceId": request.source.source_id,
                    "expectedSha256": request.source.expected_sha256,
                    "actualSha256": digest,
                },
            )
        detected = detect_media_type(data)
        if detected is not request.source.declared_media_type:
            raise DocumentParseError(
                DocumentErrorCode.MIME_MISMATCH,
                "declared media type does not match source magic bytes",
                details={
                    "sourceId": request.source.source_id,
                    "declaredMediaType": request.source.declared_media_type.value,
                    "detectedMediaType": detected.value,
                },
            )

        if detected is DocumentMediaType.PDF:
            pages = self._parse_pdf(data, request=request, source_sha256=digest, cancellation=cancellation)
        else:
            pages = self._parse_image(
                data,
                media_type=detected,
                request=request,
                source_sha256=digest,
                cancellation=cancellation,
            )
        if not any(page.text.strip() for page in pages):
            raise DocumentParseError(
                DocumentErrorCode.NO_EXTRACTABLE_TEXT,
                "document contains no extractable text",
                details={"sourceId": request.source.source_id, "pageCount": len(pages)},
            )
        cancellation.checkpoint()
        return ParsedDocument(
            request_id=request.request_id,
            source_id=request.source.source_id,
            absolute_path=request.source.absolute_path,
            source_sha256=digest,
            media_type=detected,
            source_byte_size=len(data),
            parser_config_fingerprint=self.config.fingerprint,
            pages=pages,
        )

    def _read_source(self, path: Path, *, source_id: str) -> bytes:
        try:
            path_metadata = path.lstat()
        except FileNotFoundError as error:
            raise DocumentParseError(
                DocumentErrorCode.SOURCE_NOT_FOUND,
                "document source does not exist",
                details={"sourceId": source_id},
            ) from error
        except OSError as error:
            raise DocumentParseError(
                DocumentErrorCode.SOURCE_NOT_REGULAR,
                "document source metadata could not be read",
                details={"sourceId": source_id},
            ) from error
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(path_metadata.st_mode):
            raise DocumentParseError(
                DocumentErrorCode.SOURCE_NOT_REGULAR,
                "document source must be a regular non-symlink file",
                details={"sourceId": source_id},
            )
        if path_metadata.st_size > self.config.max_file_bytes:
            raise DocumentParseError(
                DocumentErrorCode.FILE_TOO_LARGE,
                "document source exceeds max_file_bytes",
                details={
                    "sourceId": source_id,
                    "actualBytes": path_metadata.st_size,
                    "maxBytes": self.config.max_file_bytes,
                },
            )

        try:
            with path.open("rb") as handle:
                descriptor_before = os.fstat(handle.fileno())
                if not stat.S_ISREG(descriptor_before.st_mode):
                    raise DocumentParseError(
                        DocumentErrorCode.SOURCE_NOT_REGULAR,
                        "opened document source is not a regular file",
                        details={"sourceId": source_id},
                    )
                data = handle.read(self.config.max_file_bytes + 1)
                descriptor_after = os.fstat(handle.fileno())
        except DocumentParseError:
            raise
        except FileNotFoundError as error:
            raise DocumentParseError(
                DocumentErrorCode.SOURCE_NOT_FOUND,
                "document source disappeared before it could be read",
                details={"sourceId": source_id},
            ) from error
        except OSError as error:
            raise DocumentParseError(
                DocumentErrorCode.SOURCE_NOT_REGULAR,
                "document source could not be read",
                details={"sourceId": source_id},
            ) from error

        if len(data) > self.config.max_file_bytes:
            raise DocumentParseError(
                DocumentErrorCode.FILE_TOO_LARGE,
                "document source exceeds max_file_bytes",
                details={"sourceId": source_id, "maxBytes": self.config.max_file_bytes},
            )
        stable_fields_before = (
            descriptor_before.st_dev,
            descriptor_before.st_ino,
            descriptor_before.st_size,
            descriptor_before.st_mtime_ns,
        )
        stable_fields_after = (
            descriptor_after.st_dev,
            descriptor_after.st_ino,
            descriptor_after.st_size,
            descriptor_after.st_mtime_ns,
        )
        if stable_fields_before != stable_fields_after or len(data) != descriptor_after.st_size:
            raise DocumentParseError(
                DocumentErrorCode.SOURCE_CHANGED,
                "document source changed while it was being read",
                details={"sourceId": source_id},
            )
        return data

    def _parse_pdf(
        self,
        data: bytes,
        *,
        request: DocumentParseRequest,
        source_sha256: str,
        cancellation: CancellationCheckpoint,
    ) -> tuple[ParsedPage, ...]:
        try:
            document = self._pdf.open_pdf(data)
        except EncryptedPdfError as error:
            raise DocumentParseError(
                DocumentErrorCode.PDF_ENCRYPTED,
                "encrypted PDFs are not supported",
                details={"sourceId": request.source.source_id, "backend": error.backend},
            ) from error
        except BackendInputError as error:
            raise DocumentParseError(
                DocumentErrorCode.PDF_MALFORMED,
                "PDF backend rejected the source",
                details={"sourceId": request.source.source_id, "backend": error.backend},
            ) from error
        except BackendUnavailableError as error:
            raise self._backend_unavailable(error) from error
        except (BackendExecutionError, BackendOutputError) as error:
            raise self._backend_failure(error) from error
        except Exception as error:
            raise self._unexpected_backend_failure(self._pdf.identity.name, "open_pdf") from error

        completed = False
        try:
            page_count = document.page_count
            if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count <= 0:
                raise DocumentParseError(
                    DocumentErrorCode.PDF_MALFORMED,
                    "PDF must contain at least one page",
                    details={"sourceId": request.source.source_id},
                )
            if page_count > self.config.max_pdf_pages:
                raise DocumentParseError(
                    DocumentErrorCode.PDF_PAGE_LIMIT_EXCEEDED,
                    "PDF page count exceeds max_pdf_pages",
                    details={"pageCount": page_count, "maxPages": self.config.max_pdf_pages},
                )

            pages: list[ParsedPage] = []
            total_characters = 0
            total_bytes = 0
            for page_index in range(page_count):
                cancellation.checkpoint()
                text = document.extract_text(page_index, sort=self.config.pdf_sort_text)
                if not isinstance(text, str):
                    raise BackendOutputError(self._pdf.identity.name, "extract_text did not return a string")
                if text.strip():
                    regions: tuple[OcrRegion, ...] = ()
                    method = ExtractionMethod.EMBEDDED_TEXT
                    extraction_backend = self._pdf.identity
                else:
                    width_points, height_points = document.page_size_points(page_index)
                    self._check_pdf_page_raster_size(width_points, height_points, page_number=page_index + 1)
                    cancellation.checkpoint()
                    raster = document.render_rgb(page_index, dpi=self.config.pdf_render_dpi)
                    self._check_raster(raster, page_number=page_index + 1)
                    regions = self._recognize(raster, page_number=page_index + 1, cancellation=cancellation)
                    text = "\n".join(region.text for region in regions)
                    method = ExtractionMethod.OCR
                    extraction_backend = self._ocr.identity
                total_characters, total_bytes = self._enforce_text_limits(
                    text,
                    page_number=page_index + 1,
                    total_characters=total_characters,
                    total_bytes=total_bytes,
                    separator_before=page_index > 0,
                )
                pages.append(
                    ParsedPage(
                        provenance=PageProvenance(
                            source_id=request.source.source_id,
                            source_sha256=source_sha256,
                            page_number=page_index + 1,
                            input_backend=self._pdf.identity,
                            extraction_backend=extraction_backend,
                        ),
                        extraction_method=method,
                        text=text,
                        ocr_regions=regions,
                    )
                )
            result = tuple(pages)
            completed = True
            return result
        except DocumentParseError:
            raise
        except BackendUnavailableError as error:
            raise self._backend_unavailable(error) from error
        except (BackendExecutionError, BackendOutputError) as error:
            raise self._backend_failure(error) from error
        except Exception as error:
            raise self._unexpected_backend_failure(self._pdf.identity.name, "parse_pdf") from error
        finally:
            try:
                document.close()
            except Exception as error:
                if completed:
                    raise self._unexpected_backend_failure(self._pdf.identity.name, "close") from error

    def _parse_image(
        self,
        data: bytes,
        *,
        media_type: DocumentMediaType,
        request: DocumentParseRequest,
        source_sha256: str,
        cancellation: CancellationCheckpoint,
    ) -> tuple[ParsedPage, ...]:
        try:
            info = self._images.inspect(data, media_type=media_type.value)
            self._check_dimensions(info.width, info.height, page_number=1)
            if info.frame_count > self.config.max_image_frames:
                raise DocumentParseError(
                    DocumentErrorCode.IMAGE_FRAME_LIMIT_EXCEEDED,
                    "image frame count exceeds max_image_frames",
                    details={"frameCount": info.frame_count, "maxFrames": self.config.max_image_frames},
                )
            pages: list[ParsedPage] = []
            total_characters = 0
            total_bytes = 0
            for frame_index in range(info.frame_count):
                cancellation.checkpoint()
                raster = self._images.decode_frame_rgb(
                    data,
                    media_type=media_type.value,
                    frame_index=frame_index,
                )
                self._check_raster(raster, page_number=frame_index + 1)
                regions = self._recognize(raster, page_number=frame_index + 1, cancellation=cancellation)
                text = "\n".join(region.text for region in regions)
                total_characters, total_bytes = self._enforce_text_limits(
                    text,
                    page_number=frame_index + 1,
                    total_characters=total_characters,
                    total_bytes=total_bytes,
                    separator_before=frame_index > 0,
                )
                pages.append(
                    ParsedPage(
                        provenance=PageProvenance(
                            source_id=request.source.source_id,
                            source_sha256=source_sha256,
                            page_number=frame_index + 1,
                            input_backend=self._images.identity,
                            extraction_backend=self._ocr.identity,
                        ),
                        extraction_method=ExtractionMethod.OCR,
                        text=text,
                        ocr_regions=regions,
                    )
                )
            return tuple(pages)
        except DocumentParseError:
            raise
        except BackendInputError as error:
            raise DocumentParseError(
                DocumentErrorCode.IMAGE_MALFORMED,
                "image decoder rejected the source",
                details={"sourceId": request.source.source_id, "backend": error.backend},
            ) from error
        except BackendUnavailableError as error:
            raise self._backend_unavailable(error) from error
        except (BackendExecutionError, BackendOutputError) as error:
            raise self._backend_failure(error) from error
        except Exception as error:
            raise self._unexpected_backend_failure(self._images.identity.name, "parse_image") from error

    def _recognize(
        self,
        raster: RasterImage,
        *,
        page_number: int,
        cancellation: CancellationCheckpoint,
    ) -> tuple[OcrRegion, ...]:
        cancellation.checkpoint()
        try:
            output = self._ocr.recognize(raster)
        except (BackendExecutionError, BackendOutputError, BackendUnavailableError):
            raise
        except Exception as error:
            raise BackendExecutionError(self._ocr.identity.name, "recognize") from error
        regions = self._bounded_regions(output, page_number=page_number)
        cancellation.checkpoint()
        return regions

    def _bounded_regions(self, regions: Iterable[OcrRegion], *, page_number: int) -> tuple[OcrRegion, ...]:
        accepted: list[OcrRegion] = []
        for region in regions:
            if len(accepted) >= self.config.max_ocr_regions_per_page:
                raise DocumentParseError(
                    DocumentErrorCode.OCR_REGION_LIMIT_EXCEEDED,
                    "OCR output exceeds max_ocr_regions_per_page",
                    details={"pageNumber": page_number, "maxRegions": self.config.max_ocr_regions_per_page},
                )
            if not isinstance(region, OcrRegion):
                raise BackendOutputError(self._ocr.identity.name, "recognize returned a non-OcrRegion value")
            accepted.append(region)
        return tuple(accepted)

    def _check_pdf_page_raster_size(self, width_points: float, height_points: float, *, page_number: int) -> None:
        if (
            isinstance(width_points, bool)
            or isinstance(height_points, bool)
            or not isinstance(width_points, (int, float))
            or not isinstance(height_points, (int, float))
            or not math.isfinite(width_points)
            or not math.isfinite(height_points)
            or width_points <= 0
            or height_points <= 0
        ):
            raise BackendOutputError(self._pdf.identity.name, "page size must contain positive finite numbers")
        width = math.ceil(width_points * self.config.pdf_render_dpi / 72)
        height = math.ceil(height_points * self.config.pdf_render_dpi / 72)
        self._check_dimensions(width, height, page_number=page_number)

    def _check_raster(self, raster: RasterImage, *, page_number: int) -> None:
        if not isinstance(raster, RasterImage):
            raise BackendOutputError("raster_backend", "decoder did not return RasterImage")
        self._check_dimensions(raster.width, raster.height, page_number=page_number)

    def _check_dimensions(self, width: int, height: int, *, page_number: int) -> None:
        if isinstance(width, bool) or isinstance(height, bool) or width <= 0 or height <= 0:
            raise DocumentParseError(
                DocumentErrorCode.RASTER_LIMIT_EXCEEDED,
                "raster dimensions must be positive integers",
                details={"pageNumber": page_number},
            )
        if (
            width > self.config.max_raster_dimension
            or height > self.config.max_raster_dimension
            or width * height > self.config.max_raster_pixels
        ):
            raise DocumentParseError(
                DocumentErrorCode.RASTER_LIMIT_EXCEEDED,
                "raster dimensions exceed configured limits",
                details={
                    "pageNumber": page_number,
                    "width": width,
                    "height": height,
                    "maxDimension": self.config.max_raster_dimension,
                    "maxPixels": self.config.max_raster_pixels,
                },
            )

    def _enforce_text_limits(
        self,
        text: str,
        *,
        page_number: int,
        total_characters: int,
        total_bytes: int,
        separator_before: bool,
    ) -> tuple[int, int]:
        try:
            page_bytes = len(text.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as error:
            raise BackendOutputError("text_extractor", "text contains invalid Unicode scalar values") from error
        if len(text) > self.config.max_page_characters or page_bytes > self.config.max_page_text_bytes:
            raise DocumentParseError(
                DocumentErrorCode.PAGE_TEXT_LIMIT_EXCEEDED,
                "page text exceeds configured limits; text was not truncated",
                details={
                    "pageNumber": page_number,
                    "actualCharacters": len(text),
                    "actualUtf8Bytes": page_bytes,
                    "maxCharacters": self.config.max_page_characters,
                    "maxUtf8Bytes": self.config.max_page_text_bytes,
                },
            )
        separator_size = 2 if separator_before else 0
        next_characters = total_characters + separator_size + len(text)
        next_bytes = total_bytes + separator_size + page_bytes
        if next_characters > self.config.max_total_characters or next_bytes > self.config.max_total_text_bytes:
            raise DocumentParseError(
                DocumentErrorCode.TOTAL_TEXT_LIMIT_EXCEEDED,
                "document text exceeds configured limits; text was not truncated",
                details={
                    "pageNumber": page_number,
                    "actualCharacters": next_characters,
                    "actualUtf8Bytes": next_bytes,
                    "maxCharacters": self.config.max_total_characters,
                    "maxUtf8Bytes": self.config.max_total_text_bytes,
                },
            )
        return next_characters, next_bytes

    @staticmethod
    def _backend_unavailable(error: BackendUnavailableError) -> DocumentParseError:
        return DocumentParseError(
            DocumentErrorCode.BACKEND_UNAVAILABLE,
            "configured document backend is unavailable",
            details={"backend": error.backend},
        )

    @staticmethod
    def _backend_failure(error: BackendExecutionError | BackendOutputError) -> DocumentParseError:
        if isinstance(error, BackendOutputError):
            return DocumentParseError(
                DocumentErrorCode.BACKEND_INVALID_OUTPUT,
                "document backend returned invalid output",
                details={"backend": error.backend, "reason": error.reason},
            )
        return DocumentParseError(
            DocumentErrorCode.BACKEND_FAILED,
            "document backend failed",
            details={"backend": error.backend, "operation": error.operation},
        )

    @staticmethod
    def _unexpected_backend_failure(backend: str, operation: str) -> DocumentParseError:
        return DocumentParseError(
            DocumentErrorCode.BACKEND_FAILED,
            "document backend failed",
            details={"backend": backend, "operation": operation},
        )


__all__ = ["DocumentParser", "detect_media_type"]
