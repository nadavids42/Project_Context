"""PDF parser: page-by-page text extraction with page numbers retained.
No OCR (Section 8/13): a page with too little extractable text is a
signal the PDF is scanned/image-based, not a promise to read it.

If *every* page falls below the density threshold, the whole document
is flagged `ocr_required` rather than returned as sparse, misleading
"parsed" text.
"""

from __future__ import annotations

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from project_context.domain.evidence import ParseStatus
from project_context.parsers.models import ParseResult, TextBlock

PARSER_NAME = "pdf"
PARSER_VERSION = "1"

#: Section 8's example threshold: "<30 non-whitespace characters on most
#: pages" is treated as scanned/OCR-required.
MIN_NON_WHITESPACE_CHARS_PER_PAGE = 30


def parse_pdf(data: bytes) -> ParseResult:
    try:
        reader = PdfReader(io.BytesIO(data))
        page_texts = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, ValueError, KeyError) as exc:
        return ParseResult(
            status=ParseStatus.FAILED,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            normalized_text="",
            warnings=(f"could not read PDF: {exc}",),
        )

    if not page_texts:
        return ParseResult(
            status=ParseStatus.FAILED,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            normalized_text="",
            warnings=("PDF has no pages",),
        )

    non_whitespace_counts = [len("".join(page.split())) for page in page_texts]
    dense_pages = sum(
        1 for count in non_whitespace_counts if count >= MIN_NON_WHITESPACE_CHARS_PER_PAGE
    )
    if dense_pages == 0:
        return ParseResult(
            status=ParseStatus.OCR_REQUIRED,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            normalized_text="",
            warnings=(
                f"extracted text density too low across {len(page_texts)} page(s); this "
                "looks like a scanned document — OCR is not implemented",
            ),
        )

    blocks: list[TextBlock] = []
    text_parts: list[str] = []
    cursor = 0
    for page_number, page_text in enumerate(page_texts, start=1):
        stripped = page_text.strip()
        if not stripped:
            continue
        start = cursor
        text_parts.append(stripped)
        cursor += len(stripped)
        blocks.append(
            TextBlock(
                text=stripped,
                char_start=start,
                char_end=cursor,
                section_path=f"page {page_number}",
                location_label=f"page {page_number}",
            )
        )
        text_parts.append("\n\n")
        cursor += 2

    normalized_text = "".join(text_parts).strip()
    return ParseResult(
        status=ParseStatus.PARSED,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        normalized_text=normalized_text,
        blocks=tuple(blocks),
    )
