"""File/text parsers: normalize supported evidence formats (TXT,
Markdown, DOCX, PDF, VTT) to UTF-8 text plus location metadata,
deterministically and without any LLM call.

See docs/Project_Context_Product_Plan_v1.md Section 8 ("Parsing and
chunking") and FR-006.
"""

from project_context.parsers.kinds import (
    MIME_TYPES,
    EvidenceKind,
    UnsupportedEvidenceError,
    detect_kind,
)
from project_context.parsers.models import ParseResult, TextBlock
from project_context.parsers.registry import parse

__all__ = [
    "MIME_TYPES",
    "EvidenceKind",
    "ParseResult",
    "TextBlock",
    "UnsupportedEvidenceError",
    "detect_kind",
    "parse",
]
