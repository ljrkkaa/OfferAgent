"""Canonical JSON contract intended for a separately supervised parser host."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from offeragent_harness.foundation.canonical import CanonicalJsonError, canonical_json_bytes
from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json, thaw_json
from offeragent_harness.ports.cancellation import OperationCancelled

from .errors import DocumentErrorCode, DocumentParseError
from .interfaces import BackendIdentity, CancellationCheckpoint, OcrPoint, OcrRegion
from .models import (
    DocumentMediaType,
    DocumentParseRequest,
    DocumentSource,
    ExtractionMethod,
    PageProvenance,
    ParsedPage,
)
from .parser import DocumentParser

DOCUMENT_PARSER_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CanonicalPageResult:
    page: ParsedPage
    utf8_start_byte: int
    utf8_end_byte: int


@dataclass(frozen=True, slots=True)
class CanonicalDocumentResult:
    source_id: str
    source_sha256: str
    media_type: DocumentMediaType
    source_byte_size: int
    parser_config_fingerprint: str
    text: str
    pages: tuple[CanonicalPageResult, ...]


@dataclass(frozen=True, slots=True)
class CanonicalParseSuccess:
    request_id: str
    result: CanonicalDocumentResult


@dataclass(frozen=True, slots=True)
class CanonicalParseFailure:
    request_id: str | None
    code: DocumentErrorCode
    message: str
    details: FrozenJsonObject


CanonicalParseResponse: TypeAlias = CanonicalParseSuccess | CanonicalParseFailure


def encode_canonical_request(request: DocumentParseRequest) -> bytes:
    return canonical_json_bytes(
        {
            "schemaVersion": DOCUMENT_PARSER_SCHEMA_VERSION,
            "requestId": request.request_id,
            "source": {
                "sourceId": request.source.source_id,
                "absolutePath": str(request.source.absolute_path),
                "declaredMediaType": request.source.declared_media_type.value,
                "expectedSha256": request.source.expected_sha256,
            },
        }
    )


def decode_canonical_request(payload: bytes) -> DocumentParseRequest:
    value = _decode_canonical_json(payload, noun="request")
    return _request_from_json(value)


def decode_canonical_response(payload: bytes) -> CanonicalParseResponse:
    value = _decode_canonical_json(payload, noun="response")
    root = _require_object(value, location="response")
    ok = root.get("ok")
    if ok is True:
        _require_exact_keys(
            root,
            {"schemaVersion", "requestId", "ok", "result"},
            location="response",
        )
        _require_schema_version(root["schemaVersion"], location="response.schemaVersion")
        request_id = _require_non_empty_string(root["requestId"], location="response.requestId")
        return CanonicalParseSuccess(request_id=request_id, result=_result_from_json(root["result"]))
    if ok is False:
        _require_exact_keys(
            root,
            {"schemaVersion", "requestId", "ok", "error"},
            location="response",
        )
        _require_schema_version(root["schemaVersion"], location="response.schemaVersion")
        request_id_value = root["requestId"]
        if request_id_value is not None and (not isinstance(request_id_value, str) or not request_id_value):
            raise _invalid("response.requestId must be a non-empty string or null")
        error_value = _require_object(root["error"], location="response.error")
        _require_exact_keys(error_value, {"code", "message", "details"}, location="response.error")
        code_value = _require_non_empty_string(error_value["code"], location="response.error.code")
        try:
            code = DocumentErrorCode(code_value)
        except ValueError as error:
            raise _invalid("response.error.code is unknown") from error
        message = _require_non_empty_string(error_value["message"], location="response.error.message")
        details_value = _require_object(error_value["details"], location="response.error.details")
        frozen_details = freeze_json(details_value)
        if not isinstance(frozen_details, FrozenJsonObject):
            raise _invalid("response.error.details must be a JSON object")
        return CanonicalParseFailure(
            request_id=request_id_value,
            code=code,
            message=message,
            details=frozen_details,
        )
    raise _invalid("response.ok must be a boolean")


def _decode_canonical_json(payload: bytes, *, noun: str) -> object:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise DocumentParseError(
            DocumentErrorCode.INVALID_REQUEST,
            f"document parser {noun} must be UTF-8 JSON",
        ) from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise DocumentParseError(
            DocumentErrorCode.INVALID_REQUEST,
            f"document parser {noun} is not valid strict JSON",
        ) from error
    try:
        canonical = canonical_json_bytes(value)
    except CanonicalJsonError as error:
        raise DocumentParseError(
            DocumentErrorCode.INVALID_REQUEST,
            f"document parser {noun} is outside the canonical JSON subset",
        ) from error
    if canonical != payload:
        raise DocumentParseError(
            DocumentErrorCode.NON_CANONICAL_REQUEST,
            f"document parser {noun} bytes are not canonical JSON",
        )
    return value


def execute_canonical_request(
    payload: bytes,
    *,
    parser: DocumentParser,
    cancellation: CancellationCheckpoint,
    maximum_response_bytes: int,
) -> bytes:
    """Execute one complete host request and always return a canonical envelope."""

    request_id: str | None = None
    try:
        request = decode_canonical_request(payload)
        request_id = request.request_id
        result = parser.parse(request, cancellation)
        envelope: Mapping[str, Any] = {
            "schemaVersion": DOCUMENT_PARSER_SCHEMA_VERSION,
            "requestId": request_id,
            "ok": True,
            "result": result.to_json(),
        }
    except OperationCancelled as error:
        reason_code = error.reason.code.value
        envelope = _error_envelope(
            request_id,
            DocumentErrorCode.CANCELLED,
            "document parsing was cancelled",
            details={"reasonCode": reason_code},
        )
    except DocumentParseError as error:
        envelope = _error_envelope(
            request_id,
            error.code,
            str(error),
            details=thaw_json(error.details),
        )
    except Exception:
        envelope = _error_envelope(
            request_id,
            DocumentErrorCode.INTERNAL_ERROR,
            "document parser encountered an internal error",
            details={},
        )
    return _encode_bounded_response(
        envelope,
        request_id=request_id,
        maximum_response_bytes=maximum_response_bytes,
    )


def _request_from_json(value: object) -> DocumentParseRequest:
    root = _require_object(value, location="request")
    _require_exact_keys(root, {"schemaVersion", "requestId", "source"}, location="request")
    _require_schema_version(root["schemaVersion"], location="request.schemaVersion")
    request_id = _require_non_empty_string(root["requestId"], location="request.requestId")
    source_value = _require_object(root["source"], location="request.source")
    _require_exact_keys(
        source_value,
        {"sourceId", "absolutePath", "declaredMediaType", "expectedSha256"},
        location="request.source",
    )
    source_id = _require_non_empty_string(source_value["sourceId"], location="request.source.sourceId")
    absolute_path_value = _require_non_empty_string(
        source_value["absolutePath"],
        location="request.source.absolutePath",
    )
    declared_value = _require_non_empty_string(
        source_value["declaredMediaType"],
        location="request.source.declaredMediaType",
    )
    try:
        declared_media_type = DocumentMediaType(declared_value)
    except ValueError as error:
        raise _invalid("request.source.declaredMediaType is unsupported") from error
    expected_value = source_value["expectedSha256"]
    if expected_value is not None and not isinstance(expected_value, str):
        raise _invalid("request.source.expectedSha256 must be a string or null")
    try:
        return DocumentParseRequest(
            request_id=request_id,
            source=DocumentSource(
                source_id=source_id,
                absolute_path=Path(absolute_path_value),
                declared_media_type=declared_media_type,
                expected_sha256=expected_value,
            ),
        )
    except ValueError as error:
        raise _invalid(str(error)) from error


def _result_from_json(value: object) -> CanonicalDocumentResult:
    result = _require_object(value, location="response.result")
    _require_exact_keys(
        result,
        {
            "source",
            "parserConfigFingerprint",
            "text",
            "pageCount",
            "totalCharacters",
            "totalTextUtf8Bytes",
            "pages",
        },
        location="response.result",
    )
    source = _require_object(result["source"], location="response.result.source")
    _require_exact_keys(source, {"sourceId", "sha256", "mediaType", "byteSize"}, location="response.result.source")
    source_id = _require_non_empty_string(source["sourceId"], location="response.result.source.sourceId")
    source_sha256 = _require_sha256(source["sha256"], location="response.result.source.sha256")
    media_value = _require_non_empty_string(source["mediaType"], location="response.result.source.mediaType")
    try:
        media_type = DocumentMediaType(media_value)
    except ValueError as error:
        raise _invalid("response.result.source.mediaType is unsupported") from error
    source_byte_size = _require_non_negative_int(source["byteSize"], location="response.result.source.byteSize")
    config_fingerprint = _require_sha256(
        result["parserConfigFingerprint"],
        location="response.result.parserConfigFingerprint",
    )
    text = _require_string(result["text"], location="response.result.text")
    page_count = _require_non_negative_int(result["pageCount"], location="response.result.pageCount")
    total_characters = _require_non_negative_int(
        result["totalCharacters"],
        location="response.result.totalCharacters",
    )
    total_text_bytes = _require_non_negative_int(
        result["totalTextUtf8Bytes"],
        location="response.result.totalTextUtf8Bytes",
    )
    pages_value = result["pages"]
    if not isinstance(pages_value, list):
        raise _invalid("response.result.pages must be an array")
    pages = tuple(
        _page_from_json(page, expected_source_id=source_id, expected_sha256=source_sha256) for page in pages_value
    )
    if page_count != len(pages):
        raise _invalid("response.result.pageCount does not match pages")
    expected_numbers = tuple(range(1, len(pages) + 1))
    if tuple(page.page.provenance.page_number for page in pages) != expected_numbers:
        raise _invalid("response.result pages are not contiguous and one-based")
    expected_text = "\n\n".join(page.page.text for page in pages)
    if text != expected_text:
        raise _invalid("response.result.text does not match ordered page text")
    if total_characters != len(text) or total_text_bytes != len(text.encode("utf-8", errors="strict")):
        raise _invalid("response.result aggregate text counts are inconsistent")
    cursor = 0
    for index, page in enumerate(pages):
        if index:
            cursor += 2
        if page.utf8_start_byte != cursor:
            raise _invalid("response.result page byte ranges are not contiguous with separators")
        cursor += len(page.page.text.encode("utf-8", errors="strict"))
        if page.utf8_end_byte != cursor:
            raise _invalid("response.result page byte range does not match page text")
    return CanonicalDocumentResult(
        source_id=source_id,
        source_sha256=source_sha256,
        media_type=media_type,
        source_byte_size=source_byte_size,
        parser_config_fingerprint=config_fingerprint,
        text=text,
        pages=pages,
    )


def _page_from_json(value: object, *, expected_source_id: str, expected_sha256: str) -> CanonicalPageResult:
    page_value = _require_object(value, location="response.result.pages[]")
    _require_exact_keys(
        page_value,
        {
            "provenance",
            "extractionMethod",
            "text",
            "characterCount",
            "utf8ByteCount",
            "ocrRegions",
            "utf8StartByte",
            "utf8EndByte",
        },
        location="response.result.pages[]",
    )
    provenance_value = _require_object(page_value["provenance"], location="response.result.pages[].provenance")
    _require_exact_keys(
        provenance_value,
        {"sourceId", "sourceSha256", "pageNumber", "inputBackend", "extractionBackend"},
        location="response.result.pages[].provenance",
    )
    source_id = _require_non_empty_string(
        provenance_value["sourceId"],
        location="response.result.pages[].provenance.sourceId",
    )
    source_sha256 = _require_sha256(
        provenance_value["sourceSha256"],
        location="response.result.pages[].provenance.sourceSha256",
    )
    if source_id != expected_source_id or source_sha256 != expected_sha256:
        raise _invalid("response.result page provenance does not match source")
    page_number = _require_positive_int(
        provenance_value["pageNumber"],
        location="response.result.pages[].provenance.pageNumber",
    )
    input_backend = _backend_from_json(
        provenance_value["inputBackend"],
        location="response.result.pages[].provenance.inputBackend",
    )
    extraction_backend = _backend_from_json(
        provenance_value["extractionBackend"],
        location="response.result.pages[].provenance.extractionBackend",
    )
    method_value = _require_non_empty_string(
        page_value["extractionMethod"],
        location="response.result.pages[].extractionMethod",
    )
    try:
        method = ExtractionMethod(method_value)
    except ValueError as error:
        raise _invalid("response.result.pages[].extractionMethod is unsupported") from error
    text = _require_string(page_value["text"], location="response.result.pages[].text")
    character_count = _require_non_negative_int(
        page_value["characterCount"],
        location="response.result.pages[].characterCount",
    )
    text_byte_count = _require_non_negative_int(
        page_value["utf8ByteCount"],
        location="response.result.pages[].utf8ByteCount",
    )
    if character_count != len(text) or text_byte_count != len(text.encode("utf-8", errors="strict")):
        raise _invalid("response.result page text counts are inconsistent")
    regions_value = page_value["ocrRegions"]
    if not isinstance(regions_value, list):
        raise _invalid("response.result.pages[].ocrRegions must be an array")
    regions = tuple(_ocr_region_from_json(region) for region in regions_value)
    try:
        page = ParsedPage(
            provenance=PageProvenance(
                source_id=source_id,
                source_sha256=source_sha256,
                page_number=page_number,
                input_backend=input_backend,
                extraction_backend=extraction_backend,
            ),
            extraction_method=method,
            text=text,
            ocr_regions=regions,
        )
    except ValueError as error:
        raise _invalid(str(error)) from error
    start = _require_non_negative_int(
        page_value["utf8StartByte"],
        location="response.result.pages[].utf8StartByte",
    )
    end = _require_non_negative_int(
        page_value["utf8EndByte"],
        location="response.result.pages[].utf8EndByte",
    )
    if end < start:
        raise _invalid("response.result page UTF-8 range is inverted")
    return CanonicalPageResult(page=page, utf8_start_byte=start, utf8_end_byte=end)


def _backend_from_json(value: object, *, location: str) -> BackendIdentity:
    backend = _require_object(value, location=location)
    if set(backend) not in ({"name", "version"}, {"name", "version", "model"}):
        raise _invalid(f"{location} fields do not match the schema")
    name = _require_non_empty_string(backend["name"], location=f"{location}.name")
    version = _require_non_empty_string(backend["version"], location=f"{location}.version")
    model_value = backend.get("model")
    if model_value is not None and (not isinstance(model_value, str) or not model_value):
        raise _invalid(f"{location}.model must be a non-empty string when present")
    return BackendIdentity(name=name, version=version, model=model_value)


def _ocr_region_from_json(value: object) -> OcrRegion:
    region = _require_object(value, location="response.result.pages[].ocrRegions[]")
    _require_exact_keys(
        region,
        {"polygon", "text", "confidence"},
        location="response.result.pages[].ocrRegions[]",
    )
    polygon_value = region["polygon"]
    if not isinstance(polygon_value, list) or len(polygon_value) != 4:
        raise _invalid("OCR region polygon must contain four points")
    points: list[OcrPoint] = []
    for point_value in polygon_value:
        point = _require_object(point_value, location="OCR region point")
        _require_exact_keys(point, {"x", "y"}, location="OCR region point")
        x = _require_finite_number(point["x"], location="OCR region point.x")
        y = _require_finite_number(point["y"], location="OCR region point.y")
        points.append(OcrPoint(x=x, y=y))
    text = _require_non_empty_string(region["text"], location="OCR region text")
    confidence = _require_finite_number(region["confidence"], location="OCR region confidence")
    try:
        return OcrRegion(polygon=(points[0], points[1], points[2], points[3]), text=text, confidence=confidence)
    except ValueError as error:
        raise _invalid(str(error)) from error


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"JSON constant is not supported: {value}")


def _require_object(value: object, *, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise _invalid(f"{location} must be a JSON object")
    return value


def _require_exact_keys(value: Mapping[str, object], expected: set[str], *, location: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise _invalid(f"{location} fields do not match the schema", missing=missing, unknown=unknown)


def _require_non_empty_string(value: object, *, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{location} must be a non-empty string")
    return value


def _require_string(value: object, *, location: str) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{location} must be a string")
    return value


def _require_schema_version(value: object, *, location: str) -> None:
    if isinstance(value, bool) or value != DOCUMENT_PARSER_SCHEMA_VERSION:
        raise _invalid(f"{location} must equal {DOCUMENT_PARSER_SCHEMA_VERSION}")


def _require_non_negative_int(value: object, *, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(f"{location} must be a non-negative integer")
    return value


def _require_positive_int(value: object, *, location: str) -> int:
    result = _require_non_negative_int(value, location=location)
    if result == 0:
        raise _invalid(f"{location} must be positive")
    return result


def _require_finite_number(value: object, *, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"{location} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise _invalid(f"{location} must be a finite number")
    return result


def _require_sha256(value: object, *, location: str) -> str:
    text = _require_non_empty_string(value, location=location)
    if len(text) != 71 or not text.startswith("sha256:") or text.lower() != text:
        raise _invalid(f"{location} must be a lowercase SHA-256 identity")
    try:
        int(text[7:], 16)
    except ValueError as error:
        raise _invalid(f"{location} must be a lowercase SHA-256 identity") from error
    return text


def _invalid(message: str, *, missing: list[str] | None = None, unknown: list[str] | None = None) -> DocumentParseError:
    details: dict[str, object] = {}
    if missing:
        details["missingFields"] = missing
    if unknown:
        details["unknownFields"] = unknown
    return DocumentParseError(DocumentErrorCode.INVALID_REQUEST, message, details=details)


def _error_envelope(
    request_id: str | None,
    code: DocumentErrorCode,
    message: str,
    *,
    details: Mapping[str, Any],
) -> Mapping[str, Any]:
    return {
        "schemaVersion": DOCUMENT_PARSER_SCHEMA_VERSION,
        "requestId": request_id,
        "ok": False,
        "error": {"code": code.value, "message": message, "details": details},
    }


def _encode_bounded_response(
    envelope: Mapping[str, Any],
    *,
    request_id: str | None,
    maximum_response_bytes: int,
) -> bytes:
    if isinstance(maximum_response_bytes, bool) or maximum_response_bytes <= 0:
        raise ValueError("maximum_response_bytes must be a positive integer")
    encoded = canonical_json_bytes(envelope)
    if len(encoded) <= maximum_response_bytes:
        return encoded

    bounded_error = _error_envelope(
        request_id,
        DocumentErrorCode.OUTPUT_LIMIT_EXCEEDED,
        "document parser response exceeds maximum_response_bytes",
        details={"maximumResponseBytes": maximum_response_bytes},
    )
    encoded_error = canonical_json_bytes(bounded_error)
    if len(encoded_error) <= maximum_response_bytes:
        return encoded_error
    minimal_error = _error_envelope(
        None,
        DocumentErrorCode.OUTPUT_LIMIT_EXCEEDED,
        "document parser response exceeds maximum_response_bytes",
        details={},
    )
    encoded_minimal = canonical_json_bytes(minimal_error)
    if len(encoded_minimal) <= maximum_response_bytes:
        return encoded_minimal
    raise ValueError(
        f"maximum_response_bytes is too small for the canonical error envelope; minimum is {len(encoded_minimal)}"
    )


__all__ = [
    "DOCUMENT_PARSER_SCHEMA_VERSION",
    "CanonicalDocumentResult",
    "CanonicalPageResult",
    "CanonicalParseFailure",
    "CanonicalParseResponse",
    "CanonicalParseSuccess",
    "decode_canonical_request",
    "decode_canonical_response",
    "encode_canonical_request",
    "execute_canonical_request",
]
