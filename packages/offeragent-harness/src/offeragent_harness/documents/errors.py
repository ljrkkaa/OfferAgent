"""Stable failures exposed by the local document parsing boundary."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json


class DocumentErrorCode(str, Enum):
    INVALID_REQUEST = "document.invalid_request"
    NON_CANONICAL_REQUEST = "document.non_canonical_request"
    SOURCE_NOT_FOUND = "document.source_not_found"
    SOURCE_NOT_REGULAR = "document.source_not_regular"
    SOURCE_CHANGED = "document.source_changed"
    SOURCE_HASH_MISMATCH = "document.source_hash_mismatch"
    FILE_TOO_LARGE = "document.file_too_large"
    UNSUPPORTED_MEDIA_TYPE = "document.unsupported_media_type"
    MIME_MISMATCH = "document.mime_mismatch"
    PDF_MALFORMED = "document.pdf_malformed"
    PDF_ENCRYPTED = "document.pdf_encrypted"
    PDF_PAGE_LIMIT_EXCEEDED = "document.pdf_page_limit_exceeded"
    IMAGE_MALFORMED = "document.image_malformed"
    IMAGE_FRAME_LIMIT_EXCEEDED = "document.image_frame_limit_exceeded"
    RASTER_LIMIT_EXCEEDED = "document.raster_limit_exceeded"
    PAGE_TEXT_LIMIT_EXCEEDED = "document.page_text_limit_exceeded"
    TOTAL_TEXT_LIMIT_EXCEEDED = "document.total_text_limit_exceeded"
    OCR_REGION_LIMIT_EXCEEDED = "document.ocr_region_limit_exceeded"
    NO_EXTRACTABLE_TEXT = "document.no_extractable_text"
    OUTPUT_LIMIT_EXCEEDED = "document.output_limit_exceeded"
    BACKEND_UNAVAILABLE = "document.backend_unavailable"
    BACKEND_FAILED = "document.backend_failed"
    BACKEND_INVALID_OUTPUT = "document.backend_invalid_output"
    CANCELLED = "document.cancelled"
    INTERNAL_ERROR = "document.internal_error"


class DocumentParseError(Exception):
    """An expected, stable parser failure safe to serialize across a host boundary."""

    def __init__(
        self,
        code: DocumentErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if not message:
            raise ValueError("document parser errors require a message")
        frozen = freeze_json({} if details is None else details)
        if not isinstance(frozen, FrozenJsonObject):
            raise TypeError("document parser error details must be a JSON object")
        self.code = code
        self.details = frozen
        super().__init__(message)


class BackendUnavailableError(Exception):
    """A configured optional parsing dependency is not installed or loadable."""

    def __init__(self, backend: str) -> None:
        self.backend = backend
        super().__init__(f"backend is unavailable: {backend}")


class BackendInputError(Exception):
    """A backend deterministically rejected source bytes."""

    def __init__(self, backend: str, reason: str) -> None:
        self.backend = backend
        self.reason = reason
        super().__init__(f"{backend} rejected its input: {reason}")


class EncryptedPdfError(BackendInputError):
    def __init__(self, backend: str) -> None:
        super().__init__(backend, "encrypted_pdf")


class BackendExecutionError(Exception):
    """A dependency failed while decoding, rendering, or recognizing content."""

    def __init__(self, backend: str, operation: str) -> None:
        self.backend = backend
        self.operation = operation
        super().__init__(f"{backend} failed during {operation}")


class BackendOutputError(Exception):
    """A dependency returned a shape or value outside its declared interface."""

    def __init__(self, backend: str, reason: str) -> None:
        self.backend = backend
        self.reason = reason
        super().__init__(f"{backend} returned invalid output: {reason}")


__all__ = [
    "BackendExecutionError",
    "BackendInputError",
    "BackendOutputError",
    "BackendUnavailableError",
    "DocumentErrorCode",
    "DocumentParseError",
    "EncryptedPdfError",
]
