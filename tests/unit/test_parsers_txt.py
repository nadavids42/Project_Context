"""Tests for the TXT/Markdown parser: paragraph blocks, line location
info, headings, and alternate-encoding decoding (Section 15)."""

from __future__ import annotations

from project_context.domain.evidence import ParseStatus
from project_context.parsers.txt_parser import parse_text


def test_txt_splits_into_paragraph_blocks():
    data = b"Paragraph one.\nStill paragraph one.\n\nParagraph two.\n"

    result = parse_text(data, markdown=False)

    assert result.status is ParseStatus.PARSED
    assert result.parser_name == "text"
    assert len(result.blocks) == 2
    assert result.blocks[0].text == "Paragraph one.\nStill paragraph one."
    assert result.blocks[1].text == "Paragraph two.\n"


def test_txt_block_char_offsets_resolve_back_into_normalized_text():
    data = b"Paragraph one.\n\nParagraph two.\n\nParagraph three."

    result = parse_text(data, markdown=False)

    for block in result.blocks:
        assert result.normalized_text[block.char_start : block.char_end] == block.text


def test_txt_block_location_label_reports_line_number():
    data = b"Line one.\n\nLine three starts here.\n"

    result = parse_text(data, markdown=False)

    assert result.blocks[0].location_label == "line 1"
    assert result.blocks[1].location_label == "line 3"


def test_txt_empty_content_is_status_empty():
    result = parse_text(b"   \n\n  \n", markdown=False)

    assert result.status is ParseStatus.EMPTY
    assert result.blocks == ()


def test_txt_alternate_encoding_cp1252_decodes_correctly():
    # 0x93/0x94 are curly quotes in cp1252; invalid as UTF-8 continuation
    # bytes, so a naive UTF-8-only decode would mangle or reject this.
    data = "He said “hello”".encode("cp1252")

    result = parse_text(data, markdown=False)

    assert result.status is ParseStatus.PARSED
    assert "hello" in result.normalized_text
    assert "“hello”" in result.normalized_text


def test_txt_undecodable_bytes_fall_back_with_warning():
    # 0x81 is undefined in both UTF-8 (as a lone byte) and cp1252 — the
    # two encodings this parser tries — so this forces the lossy
    # UTF-8-with-replacement fallback path.
    data = b"before \x81 after"

    result = parse_text(data, markdown=False)

    assert result.status is ParseStatus.PARSED
    assert result.warnings != ()


def test_markdown_headings_set_section_path():
    data = b"# Title\n\nIntro paragraph.\n\n## Section A\n\nBody of section A.\n"

    result = parse_text(data, markdown=True)

    assert result.parser_name == "markdown"
    assert result.blocks[0].section_path == "heading: Title"
    assert result.blocks[1].section_path == "under: Title"
    assert result.blocks[2].section_path == "heading: Section A"
    assert result.blocks[3].section_path == "under: Section A"


def test_plain_txt_does_not_treat_hash_lines_as_headings():
    data = b"# not a heading in plain text\n\nSecond paragraph.\n"

    result = parse_text(data, markdown=False)

    assert result.blocks[0].section_path is None
