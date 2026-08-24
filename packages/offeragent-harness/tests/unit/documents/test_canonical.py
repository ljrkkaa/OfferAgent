from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import cast

import pytest

from offeragent_harness.documents import (
    BackendIdentity,
    CanonicalParseFailure,
    CanonicalParseSuccess,
    DocumentErrorCode,
    DocumentMediaType,
    DocumentParseError,
    DocumentParser,
    DocumentParseRequest,
    DocumentSource,
    ExtractionMethod,
    PageProvenance,
    ParsedDocument,
    ParsedPage,
    decode_canonical_request,
    decode_canonical_response,
    encode_canonical_request,
    execute_canonical_request,
)
from offeragent_harness.foundation.canonical import canonical_json_bytes
from offeragent_harness.ports.cancellation import OperationCancelled


class NeverCancelled:
    def checkpoint(self) -> None:
        return


def request(tmp_path: Path) -> DocumentParseRequest:
    return DocumentParseRequest(
        request_id="request-中文",
        source=DocumentSource(
            source_id="source-1",
            absolute_path=(tmp_path / "scratch.png").resolve(),
            declared_media_type=DocumentMediaType.PNG,
            expected_sha256="sha256:" + "a" * 64,
        ),
    )


def result(tmp_path: Path) -> ParsedDocument:
    source_sha256 = "sha256:" + "a" * 64
    backend = BackendIdentity("fixture", "1")
    pages = (
        ParsedPage(
            provenance=PageProvenance("source-1", source_sha256, 1, backend, backend),
            extraction_method=ExtractionMethod.EMBEDDED_TEXT,
            text="第一问",
            ocr_regions=(),
        ),
        ParsedPage(
            provenance=PageProvenance("source-1", source_sha256, 2, backend, backend),
            extraction_method=ExtractionMethod.EMBEDDED_TEXT,
            text="second",
            ocr_regions=(),
        ),
    )
    return ParsedDocument(
        request_id="request-中文",
        source_id="source-1",
        absolute_path=(tmp_path / "private-scratch.png").resolve(),
        source_sha256=source_sha256,
        media_type=DocumentMediaType.PNG,
        source_byte_size=123,
        parser_config_fingerprint="sha256:" + "b" * 64,
        pages=pages,
    )


class StaticParser:
    def __init__(self, parsed: ParsedDocument) -> None:
        self.parsed = parsed

    def parse(self, parse_request: DocumentParseRequest, cancellation: NeverCancelled) -> ParsedDocument:
        assert parse_request.request_id == self.parsed.request_id
        cancellation.checkpoint()
        return self.parsed


def test_request_round_trip_requires_exact_canonical_json_and_exact_keys(tmp_path: Path) -> None:
    parse_request = request(tmp_path)
    payload = encode_canonical_request(parse_request)

    assert decode_canonical_request(payload) == parse_request
    non_canonical = json.dumps(json.loads(payload), ensure_ascii=False, indent=2).encode()
    with pytest.raises(DocumentParseError) as canonical_error:
        decode_canonical_request(non_canonical)
    assert canonical_error.value.code is DocumentErrorCode.NON_CANONICAL_REQUEST

    value = json.loads(payload)
    value["unknown"] = True
    with pytest.raises(DocumentParseError) as schema_error:
        decode_canonical_request(canonical_json_bytes(value))
    assert schema_error.value.code is DocumentErrorCode.INVALID_REQUEST


def test_success_response_is_typed_strict_and_does_not_leak_scratch_path(tmp_path: Path) -> None:
    parse_request = request(tmp_path)
    parsed = result(tmp_path)
    response_bytes = execute_canonical_request(
        encode_canonical_request(parse_request),
        parser=cast(DocumentParser, StaticParser(parsed)),
        cancellation=NeverCancelled(),
        maximum_response_bytes=1024 * 1024,
    )

    assert response_bytes == canonical_json_bytes(json.loads(response_bytes))
    wire = json.loads(response_bytes)
    assert wire["ok"] is True
    assert "absolutePath" not in wire["result"]["source"]
    assert str(parsed.absolute_path) not in response_bytes.decode()
    assert wire["result"]["text"] == "第一问\n\nsecond"
    assert wire["result"]["pages"][0]["utf8StartByte"] == 0
    assert wire["result"]["pages"][0]["utf8EndByte"] == len("第一问".encode())
    assert wire["result"]["pages"][1]["utf8StartByte"] == len("第一问\n\n".encode())

    decoded = decode_canonical_response(response_bytes)
    assert isinstance(decoded, CanonicalParseSuccess)
    assert decoded.request_id == parse_request.request_id
    assert decoded.result.text == "第一问\n\nsecond"
    assert decoded.result.pages[1].page.provenance.page_number == 2


def test_response_decoder_rejects_unknown_fields_and_inconsistent_ranges(tmp_path: Path) -> None:
    response = execute_canonical_request(
        encode_canonical_request(request(tmp_path)),
        parser=cast(DocumentParser, StaticParser(result(tmp_path))),
        cancellation=NeverCancelled(),
        maximum_response_bytes=1024 * 1024,
    )
    value = json.loads(response)
    value["result"]["source"]["absolutePath"] = "must-not-cross-boundary"
    with pytest.raises(DocumentParseError) as unknown:
        decode_canonical_response(canonical_json_bytes(value))
    assert unknown.value.code is DocumentErrorCode.INVALID_REQUEST

    value = json.loads(response)
    value["result"]["pages"][1]["utf8StartByte"] += 1
    with pytest.raises(DocumentParseError, match="byte ranges"):
        decode_canonical_response(canonical_json_bytes(value))


class CancellationCode(Enum):
    USER = "user"


@dataclass(frozen=True)
class CancellationReason:
    code: CancellationCode
    message: str
    requested_at: datetime


class CancelledParser:
    def parse(self, parse_request: DocumentParseRequest, cancellation: NeverCancelled) -> ParsedDocument:
        raise OperationCancelled(
            CancellationReason(CancellationCode.USER, "cancelled by test", datetime.now(timezone.utc))
        )


class FailingParser:
    def parse(self, parse_request: DocumentParseRequest, cancellation: NeverCancelled) -> ParsedDocument:
        raise DocumentParseError(
            DocumentErrorCode.NO_EXTRACTABLE_TEXT,
            "document contains no extractable text",
            details={"sourceId": parse_request.source.source_id},
        )


@pytest.mark.parametrize(
    ("parser", "expected_code"),
    (
        (CancelledParser(), DocumentErrorCode.CANCELLED),
        (FailingParser(), DocumentErrorCode.NO_EXTRACTABLE_TEXT),
    ),
)
def test_failure_and_cancellation_are_stable_typed_canonical_envelopes(
    tmp_path: Path,
    parser: object,
    expected_code: DocumentErrorCode,
) -> None:
    response = execute_canonical_request(
        encode_canonical_request(request(tmp_path)),
        parser=cast(DocumentParser, parser),
        cancellation=NeverCancelled(),
        maximum_response_bytes=1024 * 1024,
    )

    decoded = decode_canonical_response(response)
    assert isinstance(decoded, CanonicalParseFailure)
    assert decoded.request_id == "request-中文"
    assert decoded.code is expected_code


def test_invalid_request_failure_has_null_request_id(tmp_path: Path) -> None:
    response = execute_canonical_request(
        b'{"not":"the schema"}',
        parser=cast(DocumentParser, StaticParser(result(tmp_path))),
        cancellation=NeverCancelled(),
        maximum_response_bytes=1024 * 1024,
    )
    decoded = decode_canonical_response(response)
    assert isinstance(decoded, CanonicalParseFailure)
    assert decoded.request_id is None
    assert decoded.code is DocumentErrorCode.INVALID_REQUEST


def test_response_limit_returns_a_complete_small_error_never_partial_json(tmp_path: Path) -> None:
    response = execute_canonical_request(
        encode_canonical_request(request(tmp_path)),
        parser=cast(DocumentParser, StaticParser(result(tmp_path))),
        cancellation=NeverCancelled(),
        maximum_response_bytes=300,
    )

    assert len(response) <= 300
    decoded = decode_canonical_response(response)
    assert isinstance(decoded, CanonicalParseFailure)
    assert decoded.code is DocumentErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert b"first" not in response

    with pytest.raises(ValueError, match="too small"):
        execute_canonical_request(
            encode_canonical_request(request(tmp_path)),
            parser=cast(DocumentParser, StaticParser(result(tmp_path))),
            cancellation=NeverCancelled(),
            maximum_response_bytes=10,
        )
