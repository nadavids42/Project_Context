"""Tests for the PDF parser: page-by-page text, page numbers, no OCR
(scanned documents flagged `ocr_required` rather than silently returned
as near-empty text), and visible failure on malformed input."""

from __future__ import annotations

from fixtures.pdf_builder import build_minimal_pdf
from project_context.domain.evidence import ParseStatus
from project_context.parsers.pdf_parser import parse_pdf


def test_text_pdf_extracts_pages_with_page_numbers():
    data = build_minimal_pdf(
        [
            "This is page one of a text-bearing PDF fixture with real content.",
            "This is page two, also containing enough real extractable text.",
        ]
    )

    result = parse_pdf(data)

    assert result.status is ParseStatus.PARSED
    assert [block.section_path for block in result.blocks] == ["page 1", "page 2"]
    assert [block.location_label for block in result.blocks] == ["page 1", "page 2"]
    assert "page one" in result.blocks[0].text
    assert "page two" in result.blocks[1].text


def test_pdf_block_char_offsets_resolve_back_into_normalized_text():
    data = build_minimal_pdf(
        ["First page has enough dense text content to pass the threshold check."]
    )

    result = parse_pdf(data)

    for block in result.blocks:
        assert result.normalized_text[block.char_start : block.char_end] == block.text


def test_scanned_or_empty_pdf_is_flagged_ocr_required_not_silently_empty():
    data = build_minimal_pdf(["", ""])

    result = parse_pdf(data)

    assert result.status is ParseStatus.OCR_REQUIRED
    assert result.normalized_text == ""
    assert result.blocks == ()
    assert result.warnings != ()


def test_pdf_with_one_dense_page_and_one_sparse_page_still_parses():
    # Density is judged per-document (>=1 dense page), not per-page — a
    # sparse page's few words are still real content, not noise.
    data = build_minimal_pdf(
        ["This page has plenty of dense, real, extractable text content on it.", "Hi"]
    )

    result = parse_pdf(data)

    assert result.status is ParseStatus.PARSED
    assert len(result.blocks) == 2


def test_malformed_pdf_fails_visibly_without_raising():
    result = parse_pdf(b"%PDF-1.4\nthis is not a valid pdf body at all, no xref table")

    assert result.status is ParseStatus.FAILED
    assert result.warnings != ()
