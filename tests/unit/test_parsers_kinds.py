"""Tests for content-first evidence kind detection (Section 15: "do not
trust extension alone")."""

from __future__ import annotations

import pytest

from fixtures.docx_builder import build_docx_with_paragraphs_and_table
from fixtures.pdf_builder import build_minimal_pdf
from project_context.parsers.kinds import EvidenceKind, UnsupportedEvidenceError, detect_kind


def test_txt_by_extension():
    assert detect_kind(filename="notes.txt", data=b"hello") == EvidenceKind.TXT


def test_md_by_extension():
    assert detect_kind(filename="readme.md", data=b"# hello") == EvidenceKind.MARKDOWN


def test_no_extension_falls_back_to_txt_if_decodable():
    assert detect_kind(filename="notes", data=b"hello") == EvidenceKind.TXT


def test_pdf_detected_by_content_signature():
    data = build_minimal_pdf(["hello"])
    assert detect_kind(filename="doc.pdf", data=data) == EvidenceKind.PDF


def test_pdf_detected_even_with_no_extension():
    data = build_minimal_pdf(["hello"])
    assert detect_kind(filename="doc", data=data) == EvidenceKind.PDF


def test_docx_detected_by_content_signature():
    data = build_docx_with_paragraphs_and_table()
    assert detect_kind(filename="notes.docx", data=data) == EvidenceKind.DOCX


def test_vtt_detected_by_content_signature():
    data = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHi\n"
    assert detect_kind(filename="meeting.vtt", data=data) == EvidenceKind.VTT


def test_vtt_detected_with_leading_bom():
    data = b"\xef\xbb\xbfWEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHi\n"
    assert detect_kind(filename="meeting.vtt", data=data) == EvidenceKind.VTT


def test_extension_claims_pdf_but_content_is_not_pdf_is_rejected():
    with pytest.raises(UnsupportedEvidenceError, match="filename claims PDF"):
        detect_kind(filename="fake.pdf", data=b"just plain text, not a pdf")


def test_content_is_pdf_but_extension_says_otherwise_is_rejected():
    data = build_minimal_pdf(["hello"])
    with pytest.raises(UnsupportedEvidenceError, match="file content is a PDF"):
        detect_kind(filename="disguised.txt", data=data)


def test_extension_claims_docx_but_content_is_not_a_zip_is_rejected():
    with pytest.raises(UnsupportedEvidenceError, match="filename claims DOCX"):
        detect_kind(filename="fake.docx", data=b"not a zip at all")


def test_zip_that_is_not_docx_is_rejected():
    # A minimal, valid zip local-file-header signature without the
    # word/document.xml member a real DOCX contains.
    fake_zip = b"PK\x03\x04" + b"\x00" * 100
    with pytest.raises(UnsupportedEvidenceError, match="not a supported DOCX"):
        detect_kind(filename="notes.docx", data=fake_zip)


def test_extension_claims_vtt_but_content_lacks_header_is_rejected():
    with pytest.raises(UnsupportedEvidenceError, match="filename claims VTT"):
        detect_kind(filename="fake.vtt", data=b"just plain text")


def test_unknown_extension_and_undecodable_content_is_rejected():
    with pytest.raises(UnsupportedEvidenceError):
        detect_kind(filename="mystery.bin", data=b"\x00\x01\x02\x03\xff\xfe")
