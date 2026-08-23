"""TXT/Markdown parser: decodes to UTF-8, splits into paragraph blocks,
and retains line/character location information (Section 8, "Parsing
and chunking"; Section 15 fixture requirement: UTF-8 and alternate
encodings).

Paragraphs (text separated by one or more blank lines) are the natural
chunk boundary for prose — a per-line block would be far too granular.
For Markdown, ATX headings (`#` .. `######`) are tracked and attached to
following paragraphs as `section_path`, giving a document outline for
free without a full Markdown parser.
"""

from __future__ import annotations

import re

from project_context.domain.evidence import ParseStatus
from project_context.parsers.models import ParseResult, TextBlock

PARSER_NAME_TEXT = "text"
PARSER_NAME_MARKDOWN = "markdown"
PARSER_VERSION = "1"

#: Tried in order before falling back to lossy UTF-8 replacement.
#: utf-8-sig transparently strips a BOM if present; cp1252 is the
#: common "Windows text editor" alternate encoding (smart quotes, etc.)
#: that would otherwise fail strict UTF-8 decoding.
_ENCODINGS_TO_TRY = ("utf-8-sig", "cp1252")

_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _decode(data: bytes) -> tuple[str, tuple[str, ...]]:
    for encoding in _ENCODINGS_TO_TRY:
        try:
            return data.decode(encoding), ()
        except UnicodeDecodeError:
            continue
    text = data.decode("utf-8", errors="replace")
    return text, ("some bytes could not be decoded as UTF-8 and were replaced",)


def _line_number(text: str, char_offset: int) -> int:
    return text.count("\n", 0, char_offset) + 1


def _split_paragraphs(text: str) -> list[tuple[int, int]]:
    """(start, end) character offsets of each non-blank paragraph."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for match in _BLANK_LINE_RE.finditer(text):
        if match.start() > pos:
            spans.append((pos, match.start()))
        pos = match.end()
    if pos < len(text):
        spans.append((pos, len(text)))
    return [(start, end) for start, end in spans if text[start:end].strip()]


def parse_text(data: bytes, *, markdown: bool) -> ParseResult:
    text, warnings = _decode(data)
    parser_name = PARSER_NAME_MARKDOWN if markdown else PARSER_NAME_TEXT

    if not text.strip():
        return ParseResult(
            status=ParseStatus.EMPTY,
            parser_name=parser_name,
            parser_version=PARSER_VERSION,
            normalized_text=text,
            warnings=warnings,
        )

    blocks: list[TextBlock] = []
    current_heading: str | None = None
    for start, end in _split_paragraphs(text):
        paragraph = text[start:end]
        heading_match = _HEADING_RE.match(paragraph.strip()) if markdown else None
        if heading_match:
            current_heading = heading_match.group(2).strip()
            section_path = f"heading: {current_heading}"
        elif current_heading:
            section_path = f"under: {current_heading}"
        else:
            section_path = None
        blocks.append(
            TextBlock(
                text=paragraph,
                char_start=start,
                char_end=end,
                section_path=section_path,
                location_label=f"line {_line_number(text, start)}",
            )
        )

    return ParseResult(
        status=ParseStatus.PARSED,
        parser_name=parser_name,
        parser_version=PARSER_VERSION,
        normalized_text=text,
        blocks=tuple(blocks),
        warnings=warnings,
    )
