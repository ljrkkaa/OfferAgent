from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from offeragent_harness.documents import (
    BackendIdentity,
    DocumentErrorCode,
    DocumentMediaType,
    DocumentParseError,
    DocumentParser,
    DocumentParserConfig,
    DocumentParseRequest,
    DocumentSource,
    ExtractionMethod,
    ImageInfo,
    OcrPoint,
    OcrRegion,
    RasterImage,
    detect_media_type,
)

PDF_BYTES = b"%PDF-1.7\nfixture"
PNG_BYTES = b"\x89PNG\r\n\x1a\nfixture"
JPEG_BYTES = b"\xff\xd8\xfffixture"
WEBP_BYTES = b"RIFF\x07\x00\x00\x00WEBPfixture"


def config() -> DocumentParserConfig:
    return DocumentParserConfig(
        max_file_bytes=1024 * 1024,
        max_pdf_pages=10,
        max_image_frames=5,
        max_raster_dimension=4096,
        max_raster_pixels=4_000_000,
        max_page_characters=1000,
        max_total_characters=5000,
        max_page_text_bytes=4000,
        max_total_text_bytes=20_000,
        max_ocr_regions_per_page=10,
        pdf_render_dpi=144,
        pdf_sort_text=True,
    )


class Checkpoints:
    def __init__(self) -> None:
        self.count = 0

    def checkpoint(self) -> None:
        self.count += 1


class StopParsing(BaseException):
    pass


class CancelAt:
    def __init__(self, checkpoint: int) -> None:
        self.checkpoint_number = checkpoint
        self.count = 0

    def checkpoint(self) -> None:
        self.count += 1
        if self.count == self.checkpoint_number:
            raise StopParsing


class FakePdfDocument:
    def __init__(
        self,
        texts: Sequence[str],
        *,
        sizes: Sequence[tuple[float, float]] | None = None,
        rasters: Sequence[RasterImage] | None = None,
        fail_close: bool = False,
    ) -> None:
        self.texts = tuple(texts)
        self.sizes = tuple(sizes or ((100.0, 100.0),) * len(texts))
        self.rasters = tuple(rasters or (RasterImage(2, 2, b"\0" * 12),) * len(texts))
        self.fail_close = fail_close
        self.extracted: list[tuple[int, bool]] = []
        self.rendered: list[tuple[int, int]] = []
        self.closed = False

    @property
    def page_count(self) -> int:
        return len(self.texts)

    def extract_text(self, page_index: int, *, sort: bool) -> str:
        self.extracted.append((page_index, sort))
        return self.texts[page_index]

    def page_size_points(self, page_index: int) -> tuple[float, float]:
        return self.sizes[page_index]

    def render_rgb(self, page_index: int, *, dpi: int) -> RasterImage:
        self.rendered.append((page_index, dpi))
        return self.rasters[page_index]

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close failed")


class FakePdfBackend:
    identity = BackendIdentity("fake-pdf", "1")

    def __init__(self, document: FakePdfDocument) -> None:
        self.document = document
        self.opens = 0

    def open_pdf(self, data: bytes) -> FakePdfDocument:
        assert data.startswith(b"%PDF-")
        self.opens += 1
        return self.document


class FakeImageDecoder:
    identity = BackendIdentity("fake-images", "1")

    def __init__(self, *, info: ImageInfo | None = None, rasters: Sequence[RasterImage] | None = None) -> None:
        self.info = info or ImageInfo(2, 2, 1)
        self.rasters = tuple(rasters or (RasterImage(2, 2, b"\0" * 12),) * self.info.frame_count)
        self.inspected: list[str] = []
        self.decoded: list[tuple[str, int]] = []

    def inspect(self, data: bytes, *, media_type: str) -> ImageInfo:
        self.inspected.append(media_type)
        return self.info

    def decode_frame_rgb(self, data: bytes, *, media_type: str, frame_index: int) -> RasterImage:
        self.decoded.append((media_type, frame_index))
        return self.rasters[frame_index]


class FakeOcr:
    identity = BackendIdentity("fake-ocr", "1", "fixture")

    def __init__(self, outputs: Sequence[Sequence[OcrRegion]]) -> None:
        self.outputs = list(outputs)
        self.images: list[RasterImage] = []

    def recognize(self, image: RasterImage) -> Sequence[OcrRegion]:
        self.images.append(image)
        return self.outputs.pop(0)


def region(text: str, *, confidence: float = 0.9) -> OcrRegion:
    return OcrRegion(
        polygon=(OcrPoint(0, 0), OcrPoint(1, 0), OcrPoint(1, 1), OcrPoint(0, 1)),
        text=text,
        confidence=confidence,
    )


def source_request(tmp_path: Path, data: bytes, media_type: DocumentMediaType) -> DocumentParseRequest:
    path = tmp_path / "source.bin"
    path.write_bytes(data)
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    return DocumentParseRequest(
        request_id="parse-1",
        source=DocumentSource(
            source_id="attachment-1",
            absolute_path=path.resolve(),
            declared_media_type=media_type,
            expected_sha256=digest,
        ),
    )


def parser(
    pdf: FakePdfDocument,
    ocr_outputs: Sequence[Sequence[OcrRegion]],
    *,
    parser_config: DocumentParserConfig | None = None,
    images: FakeImageDecoder | None = None,
) -> tuple[DocumentParser, FakePdfBackend, FakeImageDecoder, FakeOcr]:
    pdf_backend = FakePdfBackend(pdf)
    image_decoder = images or FakeImageDecoder()
    ocr = FakeOcr(ocr_outputs)
    return (
        DocumentParser(
            config=parser_config or config(),
            pdf_backend=pdf_backend,
            image_decoder=image_decoder,
            ocr_engine=ocr,
        ),
        pdf_backend,
        image_decoder,
        ocr,
    )


def test_mixed_pdf_uses_embedded_text_and_ocrs_only_missing_text_pages(tmp_path: Path) -> None:
    pdf = FakePdfDocument(("native question", " \n"))
    document_parser, _, _, ocr = parser(pdf, ((region("OCR 问题一"), region("OCR 问题二")),))

    result = document_parser.parse(
        source_request(tmp_path, PDF_BYTES, DocumentMediaType.PDF),
        Checkpoints(),
    )

    assert [page.extraction_method for page in result.pages] == [
        ExtractionMethod.EMBEDDED_TEXT,
        ExtractionMethod.OCR,
    ]
    assert [page.text for page in result.pages] == ["native question", "OCR 问题一\nOCR 问题二"]
    assert pdf.extracted == [(0, True), (1, True)]
    assert pdf.rendered == [(1, 144)]
    assert len(ocr.images) == 1
    assert pdf.closed
    assert result.pages[1].provenance.source_sha256 == result.source_sha256
    assert result.pages[1].provenance.page_number == 2
    assert result.to_json()["text"] == "native question\n\nOCR 问题一\nOCR 问题二"


@pytest.mark.parametrize(
    ("data", "media_type"),
    (
        (PNG_BYTES, DocumentMediaType.PNG),
        (JPEG_BYTES, DocumentMediaType.JPEG),
        (WEBP_BYTES, DocumentMediaType.WEBP),
    ),
)
def test_supported_images_are_decoded_and_ocred_by_explicit_media_type(
    tmp_path: Path,
    data: bytes,
    media_type: DocumentMediaType,
) -> None:
    images = FakeImageDecoder()
    document_parser, pdf, _, _ = parser(FakePdfDocument(("unused",)), ((region("面经"),),), images=images)

    result = document_parser.parse(source_request(tmp_path, data, media_type), Checkpoints())

    assert result.media_type is media_type
    assert result.pages[0].text == "面经"
    assert images.inspected == [media_type.value]
    assert images.decoded == [(media_type.value, 0)]
    assert pdf.opens == 0


def test_magic_detection_and_declared_mime_fail_closed(tmp_path: Path) -> None:
    assert detect_media_type(PDF_BYTES) is DocumentMediaType.PDF
    with pytest.raises(DocumentParseError) as unsupported:
        detect_media_type(b"not a supported file")
    assert unsupported.value.code is DocumentErrorCode.UNSUPPORTED_MEDIA_TYPE

    document_parser, pdf, _, _ = parser(FakePdfDocument(("text",)), ())
    request = source_request(tmp_path, PNG_BYTES, DocumentMediaType.PDF)
    with pytest.raises(DocumentParseError) as mismatch:
        document_parser.parse(request, Checkpoints())
    assert mismatch.value.code is DocumentErrorCode.MIME_MISMATCH
    assert pdf.opens == 0


def test_page_raster_and_frame_limits_reject_instead_of_truncating(tmp_path: Path) -> None:
    page_limited, _, _, _ = parser(
        FakePdfDocument(("a", "b")),
        (),
        parser_config=replace(config(), max_pdf_pages=1),
    )
    with pytest.raises(DocumentParseError) as page_error:
        page_limited.parse(source_request(tmp_path, PDF_BYTES, DocumentMediaType.PDF), Checkpoints())
    assert page_error.value.code is DocumentErrorCode.PDF_PAGE_LIMIT_EXCEEDED

    huge_pdf = FakePdfDocument(("",), sizes=((10_000.0, 10_000.0),))
    raster_limited, _, _, ocr = parser(huge_pdf, ((region("never"),),))
    with pytest.raises(DocumentParseError) as raster_error:
        raster_limited.parse(source_request(tmp_path, PDF_BYTES, DocumentMediaType.PDF), Checkpoints())
    assert raster_error.value.code is DocumentErrorCode.RASTER_LIMIT_EXCEEDED
    assert huge_pdf.rendered == []
    assert ocr.images == []

    images = FakeImageDecoder(info=ImageInfo(2, 2, 2))
    frame_limited, _, _, _ = parser(
        FakePdfDocument(("unused",)),
        ((region("never"),),),
        parser_config=replace(config(), max_image_frames=1),
        images=images,
    )
    with pytest.raises(DocumentParseError) as frame_error:
        frame_limited.parse(source_request(tmp_path, PNG_BYTES, DocumentMediaType.PNG), Checkpoints())
    assert frame_error.value.code is DocumentErrorCode.IMAGE_FRAME_LIMIT_EXCEEDED
    assert images.decoded == []


def test_text_and_ocr_region_limits_never_return_partial_content(tmp_path: Path) -> None:
    page_text_limited, _, _, _ = parser(
        FakePdfDocument(("abcdef",)),
        (),
        parser_config=replace(config(), max_page_characters=5),
    )
    with pytest.raises(DocumentParseError) as page_error:
        page_text_limited.parse(source_request(tmp_path, PDF_BYTES, DocumentMediaType.PDF), Checkpoints())
    assert page_error.value.code is DocumentErrorCode.PAGE_TEXT_LIMIT_EXCEEDED
    assert "not truncated" in str(page_error.value)

    total_limited, _, _, _ = parser(
        FakePdfDocument(("abc", "def")),
        (),
        parser_config=replace(config(), max_total_characters=7, max_page_characters=5),
    )
    with pytest.raises(DocumentParseError) as total_error:
        total_limited.parse(source_request(tmp_path, PDF_BYTES, DocumentMediaType.PDF), Checkpoints())
    assert total_error.value.code is DocumentErrorCode.TOTAL_TEXT_LIMIT_EXCEEDED

    region_limited, _, _, _ = parser(
        FakePdfDocument(("",)),
        ((region("one"), region("two")),),
        parser_config=replace(config(), max_ocr_regions_per_page=1),
    )
    with pytest.raises(DocumentParseError) as region_error:
        region_limited.parse(source_request(tmp_path, PDF_BYTES, DocumentMediaType.PDF), Checkpoints())
    assert region_error.value.code is DocumentErrorCode.OCR_REGION_LIMIT_EXCEEDED


def test_all_empty_document_fails_but_an_empty_page_in_mixed_pdf_is_preserved(tmp_path: Path) -> None:
    empty_parser, _, _, _ = parser(FakePdfDocument(("",)), ((),))
    with pytest.raises(DocumentParseError) as empty_error:
        empty_parser.parse(source_request(tmp_path, PDF_BYTES, DocumentMediaType.PDF), Checkpoints())
    assert empty_error.value.code is DocumentErrorCode.NO_EXTRACTABLE_TEXT

    mixed_parser, _, _, _ = parser(FakePdfDocument(("", "native")), ((),))
    result = mixed_parser.parse(source_request(tmp_path, PDF_BYTES, DocumentMediaType.PDF), Checkpoints())
    assert [page.text for page in result.pages] == ["", "native"]


def test_hash_file_size_close_and_cancellation_failures_are_explicit(tmp_path: Path) -> None:
    too_small, _, _, _ = parser(
        FakePdfDocument(("text",)),
        (),
        parser_config=replace(config(), max_file_bytes=3),
    )
    with pytest.raises(DocumentParseError) as file_error:
        too_small.parse(source_request(tmp_path, PDF_BYTES, DocumentMediaType.PDF), Checkpoints())
    assert file_error.value.code is DocumentErrorCode.FILE_TOO_LARGE

    hash_parser, _, _, _ = parser(FakePdfDocument(("text",)), ())
    hash_request = source_request(tmp_path, PDF_BYTES, DocumentMediaType.PDF)
    hash_request = replace(hash_request, source=replace(hash_request.source, expected_sha256="sha256:" + "0" * 64))
    with pytest.raises(DocumentParseError) as hash_error:
        hash_parser.parse(hash_request, Checkpoints())
    assert hash_error.value.code is DocumentErrorCode.SOURCE_HASH_MISMATCH

    close_parser, _, _, _ = parser(FakePdfDocument(("text",), fail_close=True), ())
    with pytest.raises(DocumentParseError) as close_error:
        close_parser.parse(source_request(tmp_path, PDF_BYTES, DocumentMediaType.PDF), Checkpoints())
    assert close_error.value.code is DocumentErrorCode.BACKEND_FAILED

    cancelled_pdf = FakePdfDocument(("text",))
    cancelled_parser, _, _, _ = parser(cancelled_pdf, ())
    with pytest.raises(StopParsing):
        cancelled_parser.parse(
            source_request(tmp_path, PDF_BYTES, DocumentMediaType.PDF),
            CancelAt(3),
        )
    assert cancelled_pdf.closed
