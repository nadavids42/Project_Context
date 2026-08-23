"""Character-span validation, shared by the evidence viewer, extraction
evidence validation, and evidence-link validation (FR-008: "Start/end
offsets resolve to non-empty source text").

Deliberately independent of any one table — it validates a span against
a piece of text it is handed, nothing more. `source_contents.
normalized_text`, a chunk's text, or an evidence-link target can all use
the same rule.
"""

from __future__ import annotations

import re

#: Conservative whitespace normalization (Section 8: "quoted text
#: normalized for whitespace matches the span"): collapse any run of
#: whitespace to a single space and strip the ends. Does not touch
#: punctuation or case — a quote that differs in wording still fails to
#: match. Shared by project_context.services.extraction (evidence-span
#: validation against a chunk) and project_context.db.evidence_link_repository
#: (evidence-link quote validation against a chunk/content) so both use
#: exactly one normalization rule.
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


class InvalidSpanError(ValueError):
    """Raised when a `(char_start, char_end)` pair does not resolve to a
    non-empty slice of the given text."""


def validate_span(text: str, char_start: int, char_end: int) -> str:
    """Validate `(char_start, char_end)` against `text` and return the
    (non-empty) slice it selects.

    Requires `0 <= char_start < char_end <= len(text)`. Does not require
    the slice to be non-whitespace — an all-whitespace span is a
    pathological citation, not an unparseable one, and future evidence
    validation may want to distinguish that case itself.
    """
    if char_start < 0:
        raise InvalidSpanError(f"char_start must be >= 0, got {char_start}")
    if char_end <= char_start:
        raise InvalidSpanError(
            f"char_end ({char_end}) must be greater than char_start ({char_start})"
        )
    if char_end > len(text):
        raise InvalidSpanError(f"char_end ({char_end}) exceeds text length ({len(text)})")
    return text[char_start:char_end]
