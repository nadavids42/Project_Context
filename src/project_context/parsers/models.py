"""Shared parser output types.

A `ParseResult` is deliberately parser-agnostic: whatever the source
format, downstream chunking (`project_context.chunking`) only needs the
normalized text plus an ordered sequence of natural-boundary `TextBlock`s
(paragraphs, pages, or transcript turns) to chunk without ever splitting
inside one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from project_context.domain.evidence import ParseStatus


@dataclass(frozen=True)
class TextBlock:
    """One natural-boundary unit of `normalized_text` — a paragraph, a
    page, a table cell, or a merged transcript turn — with its character
    offsets into that text."""

    text: str
    char_start: int
    char_end: int
    section_path: str | None = None
    location_label: str | None = None


@dataclass(frozen=True)
class ParseResult:
    status: ParseStatus
    #: None only for a file whose kind could not be detected at all
    #: (`ParseStatus.UNSUPPORTED` produced outside the parser registry,
    #: before any specific parser could run).
    parser_name: str | None
    parser_version: str | None
    normalized_text: str
    blocks: tuple[TextBlock, ...] = ()
    warnings: tuple[str, ...] = ()

    def location_map(self) -> dict[str, Any]:
        """The JSON-serializable form stored in
        `source_contents.location_map_json`."""
        return {
            "blocks": [
                {
                    "char_start": block.char_start,
                    "char_end": block.char_end,
                    "section_path": block.section_path,
                    "location_label": block.location_label,
                }
                for block in self.blocks
            ],
            "warnings": list(self.warnings),
        }
