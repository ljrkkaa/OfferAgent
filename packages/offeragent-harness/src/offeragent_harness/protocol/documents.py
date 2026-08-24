"""Durable DTOs for deterministic local PDF and image text extraction.

The original :class:`DocumentContentBlock` in ``turn/start`` remains the
authoritative input.  These records only identify that input by index and record
the extraction attempt, terminal outcome, and byte-precise page provenance.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from ._base import JsonObject, WireModel
from .content import DocumentPageLocator
from .ids import ArtifactId, DocumentId


class DocumentExtractionMethod(str, Enum):
    EMBEDDED_TEXT = "embedded_text"
    OCR = "ocr"


class DocumentExtractionWarningCode(str, Enum):
    PAGE_NO_TEXT = "page_no_text"
    LOW_OCR_CONFIDENCE = "low_ocr_confidence"
    PARTIAL_PAGE = "partial_page"


class DocumentExtractionFailureCode(str, Enum):
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_UNREADABLE = "source_unreadable"
    SOURCE_INTEGRITY_MISMATCH = "source_integrity_mismatch"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    DOCUMENT_ENCRYPTED = "document_encrypted"
    DOCUMENT_LIMIT_EXCEEDED = "document_limit_exceeded"
    NO_EXTRACTABLE_TEXT = "no_extractable_text"
    PARSER_UNAVAILABLE = "parser_unavailable"
    PARSER_FAILED = "parser_failed"
    OUTPUT_INTEGRITY_MISMATCH = "output_integrity_mismatch"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"


class DocumentPageProvenance(WireModel):
    """Maps a UTF-8 byte span in the extracted text artifact to one source page."""

    locator: DocumentPageLocator
    extraction_method: DocumentExtractionMethod
    utf8_start_byte: int = Field(ge=0)
    utf8_end_byte: int = Field(ge=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _is_one_nonempty_page_span(self) -> DocumentPageProvenance:
        if self.locator.page_start != self.locator.page_end:
            raise ValueError("document page provenance must identify exactly one page")
        if self.utf8_end_byte <= self.utf8_start_byte:
            raise ValueError("utf8EndByte must be greater than utf8StartByte")
        if self.confidence is not None and self.extraction_method is not DocumentExtractionMethod.OCR:
            raise ValueError("confidence is only valid for OCR provenance")
        return self


class DocumentExtractionWarning(WireModel):
    code: DocumentExtractionWarningCode
    user_visible_message: str = Field(min_length=1, max_length=4096)
    locator: DocumentPageLocator | None = None


class DocumentExtractionFailure(WireModel):
    code: DocumentExtractionFailureCode
    retryable: bool
    user_visible_message: str = Field(min_length=1, max_length=4096)
    locator: DocumentPageLocator | None = None
    details: JsonObject = Field(default_factory=dict)


class DocumentExtractionStartedPayload(WireModel):
    document_id: DocumentId
    input_block_index: int = Field(ge=0, le=255)
    attempt: int = Field(default=1, ge=1, le=100)


class DocumentExtractionCompletedPayload(WireModel):
    document_id: DocumentId
    attempt: int = Field(default=1, ge=1, le=100)
    text_artifact_id: ArtifactId
    page_count: int = Field(ge=1, le=1_000_000)
    page_provenance: list[DocumentPageProvenance] = Field(min_length=1, max_length=10_000)
    warnings: list[DocumentExtractionWarning] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def _provenance_is_ordered_and_within_document(self) -> DocumentExtractionCompletedPayload:
        previous_page = 0
        previous_end_byte = 0
        for provenance in self.page_provenance:
            page = provenance.locator.page_start
            if page > self.page_count:
                raise ValueError("page provenance cannot exceed pageCount")
            if page <= previous_page:
                raise ValueError("page provenance must be unique and ordered by page")
            if provenance.utf8_start_byte < previous_end_byte:
                raise ValueError("page provenance byte spans must be ordered and non-overlapping")
            previous_page = page
            previous_end_byte = provenance.utf8_end_byte
        for warning in self.warnings:
            if warning.locator is not None and warning.locator.page_end > self.page_count:
                raise ValueError("warning locator cannot exceed pageCount")
        return self


class DocumentExtractionFailedPayload(WireModel):
    document_id: DocumentId
    attempt: int = Field(default=1, ge=1, le=100)
    failure: DocumentExtractionFailure


__all__ = [
    "DocumentExtractionCompletedPayload",
    "DocumentExtractionFailedPayload",
    "DocumentExtractionFailure",
    "DocumentExtractionFailureCode",
    "DocumentExtractionMethod",
    "DocumentExtractionStartedPayload",
    "DocumentExtractionWarning",
    "DocumentExtractionWarningCode",
    "DocumentPageProvenance",
]
