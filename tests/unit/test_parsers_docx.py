"""Tests for the DOCX parser: paragraphs, lists, and table cells in
deterministic reading order (Section 15)."""

from __future__ import annotations

from fixtures.docx_builder import build_docx_with_paragraphs_and_table
from project_context.domain.evidence import ParseStatus
from project_context.parsers.docx_parser import parse_docx


def test_docx_reading_order_interleaves_paragraphs_and_table():
    data = build_docx_with_paragraphs_and_table()

    result = parse_docx(data)

    assert result.status is ParseStatus.PARSED
    section_paths = [block.section_path for block in result.blocks]
    assert section_paths == [
        "paragraph 1",
        "list item 2",
        "list item 3",
        "table 1 row 1 col 1",
        "table 1 row 1 col 2",
        "table 1 row 2 col 1",
        "table 1 row 2 col 2",
        "paragraph 4",
    ]


def test_docx_paragraph_and_cell_text_is_extracted():
    data = build_docx_with_paragraphs_and_table()

    result = parse_docx(data)
    texts = [block.text for block in result.blocks]

    assert texts == [
        "First paragraph.",
        "Bullet one",
        "Bullet two",
        "A1",
        "B1",
        "A2",
        "B2",
        "Last paragraph.",
    ]


def test_docx_block_char_offsets_resolve_back_into_normalized_text():
    data = build_docx_with_paragraphs_and_table()

    result = parse_docx(data)

    for block in result.blocks:
        assert result.normalized_text[block.char_start : block.char_end] == block.text


def test_docx_empty_document_is_status_empty():
    import io

    from docx import Document

    buf = io.BytesIO()
    Document().save(buf)

    result = parse_docx(buf.getvalue())

    assert result.status is ParseStatus.EMPTY


def test_malformed_docx_fails_visibly_without_raising():
    result = parse_docx(b"this is not a zip file at all")

    assert result.status is ParseStatus.FAILED
    assert result.warnings != ()
    assert result.normalized_text == ""
