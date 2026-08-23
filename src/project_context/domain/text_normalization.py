"""Deterministic text/date normalization primitives shared by exact
fingerprinting (`project_context.domain.observations`) and fuzzy
reconciliation matching (`project_context.domain.reconciliation`).

Implements Section 10.1's normalization rules exactly:

- "normalized text: Unicode NFKC, lowercase, collapsed whitespace,
  conservative punctuation removal" -> `normalize_form`.
- "normalized dates: ISO value plus original phrase and ambiguity flag"
  -> `NormalizedDate` / `normalize_date`.
- "a subject fingerprint excluding volatile dates/status words" ->
  `subject_tokens`.

"Never discard original text. Normalized values support matching only"
(Section 10.1) — every function here is a pure, side-effect-free
transform; nothing in this module mutates or drops a caller's original
string, it only derives a comparison form from it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

#: A conservative, safe-to-strip punctuation set (Section 10.1:
#: "conservative punctuation removal") — quote/bracket/sentence
#: punctuation that never changes what a proposition means, so two
#: statements differing only in this punctuation still normalize
#: identically. Deliberately does *not* strip characters that can carry
#: meaning (e.g. `-`, `/`, `%`, `$`, `#`, `@`) since a date, ticket
#: reference, or amount could depend on them.
_PUNCTUATION_TABLE = str.maketrans("", "", ".,;:!?\"'`‘’“”()[]{}")

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_form(text: str) -> str:
    """Unicode NFKC, lowercase, conservative punctuation removal, then
    whitespace collapse — the one normalization form Section 10.1
    describes, used everywhere two pieces of text are compared for
    matching rather than shown to a person."""
    nfkc = unicodedata.normalize("NFKC", text)
    lowered = nfkc.lower()
    without_punctuation = lowered.translate(_PUNCTUATION_TABLE)
    return _WHITESPACE_RE.sub(" ", without_punctuation).strip()


# ---------------------------------------------------------------------------
# Subject/token fingerprint (Section 10.1: "a subject fingerprint excluding
# volatile dates/status words").
# ---------------------------------------------------------------------------

#: Ledger status/transition vocabulary (Section 9/10) — volatile because
#: the same proposition can be restated at any status ("the report is
#: open" vs "the report is complete" are about the *same* item).
_STATUS_AND_TRANSITION_WORDS = frozenset(
    {
        "open",
        "active",
        "completed",
        "complete",
        "resolved",
        "resolve",
        "canceled",
        "cancelled",
        "cancel",
        "superseded",
        "supersede",
        "pending",
        "done",
        "blocked",
        "create",
        "created",
        "update",
        "updated",
        "reopen",
        "reopened",
        "correct",
        "corrected",
    }
)

#: Relative/calendar date vocabulary — volatile for the same reason dates
#: themselves are excluded: "due Monday" and "due Friday" can be the same
#: commitment restated with a changed date.
_DATE_WORDS = frozenset(
    {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "today",
        "tomorrow",
        "yesterday",
        "tonight",
        "week",
        "weeks",
        "month",
        "months",
        "day",
        "days",
        "year",
        "years",
        "next",
        "last",
        "this",
        "soon",
        "later",
        "eod",
        "eow",
        "asap",
        "now",
        "currently",
    }
)

#: A deliberately small stopword list — Section 10.1 asks only that
#: volatile date/status words be excluded "without destroying meaning,"
#: not that this become a general-purpose stopword filter.
_MINIMAL_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "to", "for", "on", "in", "at", "by", "is", "was", "will", "be"}
)

VOLATILE_TOKENS = _STATUS_AND_TRANSITION_WORDS | _DATE_WORDS | _MINIMAL_STOPWORDS

_DATE_LIKE_TOKEN_RE = re.compile(
    r"^(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}(/\d{2,4})?|\d{1,2}(st|nd|rd|th)|\d+)$"
)


def subject_tokens(subject: str, statement: str) -> frozenset[str]:
    """The token set used for fuzzy subject-similarity matching (Section
    10.4's `subject_token_similarity` feature): both fields normalized
    and combined, with volatile status/date vocabulary and bare numeric/
    date-shaped tokens removed so a due-date or status change alone does
    not depress the similarity of an otherwise-identical proposition."""
    normalized = normalize_form(f"{subject} {statement}")
    return frozenset(
        token
        for token in normalized.split()
        if token not in VOLATILE_TOKENS and not _DATE_LIKE_TOKEN_RE.match(token)
    )


def token_jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two token sets; `0.0` if both are empty (no
    signal either way, treated as no similarity rather than undefined)."""
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


# ---------------------------------------------------------------------------
# Named-entity heuristic (Section 10.4's `shared_named_entities` feature).
# No NER library is a project dependency (Section 10.1: "lemmatized/
# stemmed keyword set only if a lightweight library is already present;
# otherwise token normalization is sufficient") — this is a conservative
# capitalized-token heuristic, documented as a known weak case.
# ---------------------------------------------------------------------------

_SENTENCE_START_COMMON_WORDS = frozenset(
    {"the", "this", "that", "we", "i", "they", "he", "she", "it", "our", "their", "a", "an"}
)
_CAPITALIZED_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def extract_named_entities(text: str) -> frozenset[str]:
    """A heuristic proper-noun extractor: capitalized tokens (in the
    *original*, un-normalized text) that are not common sentence-initial
    words. Approximate by design — see module docstring."""
    entities = set()
    for token in _CAPITALIZED_TOKEN_RE.findall(text):
        lowered = token.lower()
        if len(token) > 1 and token[0].isupper() and lowered not in _SENTENCE_START_COMMON_WORDS:
            entities.add(lowered)
    return frozenset(entities)


# ---------------------------------------------------------------------------
# Date normalization (Section 10.1: "normalized dates: ISO value plus
# original phrase and ambiguity flag").
# ---------------------------------------------------------------------------

#: Phrases that describe a date without pinning one down. `date_value`
#: being `None` already signals "not resolved to a concrete date," but a
#: model can also emit a `date_text` that is itself inherently vague even
#: when a `date_value` was still (perhaps wrongly) supplied — flagging it
#: here means reconciliation never treats a vague phrase as a confident
#: date match.
_VAGUE_DATE_PHRASES = frozenset(
    {"soon", "eventually", "tbd", "sometime", "later", "at some point", "down the road"}
)


@dataclass(frozen=True)
class NormalizedDate:
    """A date as both a normalized value and the original phrase/
    ambiguity Section 10.1 requires be preserved alongside it."""

    value: date | None
    original_text: str | None
    ambiguous: bool


def normalize_date(date_value: str | None, date_text: str | None) -> NormalizedDate:
    """Build a `NormalizedDate` from an observation's stored
    `date_value` (an ISO date string, already parsed once by extraction)
    and `date_text` (the original phrase). Never re-interprets free text
    into a date itself — that parsing belongs to extraction/Section 12;
    this only tracks whether what extraction produced is usable for
    matching."""
    parsed = date.fromisoformat(date_value) if date_value else None
    ambiguous = parsed is None and date_text is not None
    if not ambiguous and date_text is not None and date_text.strip().lower() in _VAGUE_DATE_PHRASES:
        ambiguous = True
    return NormalizedDate(value=parsed, original_text=date_text, ambiguous=ambiguous)
