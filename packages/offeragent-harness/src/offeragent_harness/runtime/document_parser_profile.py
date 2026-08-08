"""Single production policy for the bundled local document parser host.

The Worker and the process host share this immutable profile so the parser
configuration fingerprint checked by the Worker is exactly the configuration
used to extract a document.  None of these limits are inferred from the input.
"""

from __future__ import annotations

from offeragent_harness.documents import DocumentParserConfig

DOCUMENT_PARSER_EXECUTABLE_ID = "document-extract"
DOCUMENT_PARSER_FIXED_ARGUMENTS = (DOCUMENT_PARSER_EXECUTABLE_ID,)

# Canonical requests contain only identities, a bounded absolute scratch path,
# and the declared media type.  Keeping this separate from the 64 MiB source
# ceiling prevents stdin from becoming a second document transport.
DOCUMENT_PARSER_MAX_REQUEST_BYTES = 256 * 1024

# This is a hard envelope limit, not a truncation target.  The canonical host
# returns a complete OUTPUT_LIMIT_EXCEEDED response when a result will not fit.
DOCUMENT_PARSER_MAX_RESPONSE_BYTES = 12 * 1024 * 1024

BUNDLED_DOCUMENT_PARSER_CONFIG = DocumentParserConfig(
    max_file_bytes=64 * 1024 * 1024,
    max_pdf_pages=256,
    max_image_frames=16,
    max_raster_dimension=16_384,
    max_raster_pixels=40_000_000,
    max_page_characters=256 * 1024,
    max_total_characters=2 * 1024 * 1024,
    max_page_text_bytes=768 * 1024,
    max_total_text_bytes=4 * 1024 * 1024,
    max_ocr_regions_per_page=10_000,
    pdf_render_dpi=200,
    pdf_sort_text=True,
    ocr_execution_provider="cuda",
    ocr_device_id=0,
)


__all__ = [
    "BUNDLED_DOCUMENT_PARSER_CONFIG",
    "DOCUMENT_PARSER_EXECUTABLE_ID",
    "DOCUMENT_PARSER_FIXED_ARGUMENTS",
    "DOCUMENT_PARSER_MAX_REQUEST_BYTES",
    "DOCUMENT_PARSER_MAX_RESPONSE_BYTES",
]
