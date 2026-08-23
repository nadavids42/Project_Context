"""Parser dispatch: one entry point from a detected `EvidenceKind` to its
parser. See `project_context.parsers.kinds.detect_kind` for how the kind
is determined from file content.
"""

from __future__ import annotations

from project_context.parsers.docx_parser import parse_docx
from project_context.parsers.kinds import EvidenceKind
from project_context.parsers.models import ParseResult
from project_context.parsers.pdf_parser import parse_pdf
from project_context.parsers.txt_parser import parse_text
from project_context.parsers.vtt_parser import parse_vtt


def parse(kind: EvidenceKind, data: bytes) -> ParseResult:
    if kind is EvidenceKind.TXT:
        return parse_text(data, markdown=False)
    if kind is EvidenceKind.MARKDOWN:
        return parse_text(data, markdown=True)
    if kind is EvidenceKind.DOCX:
        return parse_docx(data)
    if kind is EvidenceKind.PDF:
        return parse_pdf(data)
    if kind is EvidenceKind.VTT:
        return parse_vtt(data)
    raise AssertionError(f"unhandled evidence kind: {kind!r}")  # exhaustive over EvidenceKind
