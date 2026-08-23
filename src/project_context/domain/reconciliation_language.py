"""Deterministic explicit-language detectors used by action
classification (Section 10.5-10.10). Phrase-based, not model-based —
"Do not call an LLM from reconciliation" — and deliberately conservative:
each detector looks for a fixed, documented phrase list rather than
inferring intent, so every result is traceable to the exact wording that
triggered it (`LanguageSignal.matched_phrase`).

Every detector takes already-normalized text (`project_context.domain.
text_normalization.normalize_form`), so phrase lists are written in
lowercase with normalized punctuation.

Known weak case (see the reconciliation report): this is a fixed phrase
list, not a grammar or a classifier. Wording the plan's own examples
don't use (Section 10.6/10.7/10.9's quoted phrases are all included) can
fail to match, which fails *closed* — the observation falls through to a
lower-confidence path or CONFLICT rather than an incorrectly-confident
transition. Expanding this list is the cheapest, safest way to improve
recall without touching the scoring or classification logic around it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageSignal:
    """Whether a phrase list matched, and which phrase — kept for the
    audit trail (`candidate_features_json`), never used un-explained."""

    found: bool
    matched_phrase: str | None = None


def _search(normalized_text: str, phrases: Sequence[str]) -> LanguageSignal:
    for phrase in phrases:
        if phrase in normalized_text:
            return LanguageSignal(True, phrase)
    return LanguageSignal(False, None)


# --- completion (Section 10.6) ---------------------------------------------

#: "Future intent ("will finish") is not completion" (Section 10.6).
FUTURE_INTENT_PHRASES: tuple[str, ...] = (
    "will finish",
    "will send",
    "will complete",
    "will deploy",
    "will ship",
    "plans to",
    "planning to",
    "going to",
    "intends to",
    "intend to",
    "expects to",
    "expected to",
    "aims to",
    "hopes to",
    "scheduled to",
    "is set to",
    "due to",
)

#: "An explicit completion verb/result ("sent," "approved," "deployed,"
#: "is complete")" (Section 10.6).
COMPLETION_PHRASES: tuple[str, ...] = (
    "is complete",
    "is now complete",
    "has been sent",
    "was sent",
    "has been approved",
    "was approved",
    "has been deployed",
    "was deployed",
    "has shipped",
    "was shipped",
    "shipped",
    "is done",
    "marked complete",
    "marked as complete",
    "completed on",
    "has finished",
    "finished",
    "delivered",
    "signed off",
    "signed-off",
    "closed out",
    "wrapped up",
)


def detect_completion(normalized_statement: str) -> LanguageSignal:
    """Explicit completion language, guarded against future-intent
    phrasing sharing the same statement (Section 10.6)."""
    if _search(normalized_statement, FUTURE_INTENT_PHRASES).found:
        return LanguageSignal(False, None)
    return _search(normalized_statement, COMPLETION_PHRASES)


# --- cancellation vs delay (Section 10.7) -----------------------------------

#: '"Delayed," "blocked," and "not yet" do not qualify' (Section 10.7).
DELAY_PHRASES: tuple[str, ...] = (
    "delayed",
    "postponed",
    "pushed back",
    "blocked",
    "not yet",
    "still pending",
    "on hold",
    "stalled",
    "slipping",
)

#: "Explicit abandonment/removal ("cancel," "no longer needed," "won't
#: proceed")" (Section 10.7).
CANCELLATION_PHRASES: tuple[str, ...] = (
    "cancel",
    "cancelled",
    "canceled",
    "no longer needed",
    "won't proceed",
    "will not proceed",
    "abandoned",
    "scrapped",
    "called off",
    "dropped entirely",
    "no longer pursuing",
)


def detect_cancellation(normalized_statement: str) -> LanguageSignal:
    return _search(normalized_statement, CANCELLATION_PHRASES)


def detect_delay(normalized_statement: str) -> LanguageSignal:
    """Delay/blockage language — explicitly *not* cancellation (Section
    10.7), kept as its own detector so a delayed item is never silently
    classified as canceled."""
    return _search(normalized_statement, DELAY_PHRASES)


# --- changed dates (Section 10.8) -------------------------------------------

#: '"moved," "pushed," "new target," "now due"' (Section 10.8).
DATE_CHANGE_PHRASES: tuple[str, ...] = (
    "moved to",
    "pushed to",
    "pushed back to",
    "new target",
    "now due",
    "rescheduled to",
    "changed to",
    "updated to",
    "new date",
    "now targeting",
    "moved the date",
)


def detect_date_change(normalized_statement: str) -> LanguageSignal:
    return _search(normalized_statement, DATE_CHANGE_PHRASES)


# --- changed owners (Section 10.9) ------------------------------------------

#: '"Alice will help Bob" is not reassignment without explicit ownership
#: language' (Section 10.9).
OWNER_ASSISTANCE_PHRASES: tuple[str, ...] = (
    "will help",
    "is helping",
    "helping out",
    "assisting",
    "support from",
    "pairing with",
    "lending a hand",
)

OWNER_ASSIGNMENT_PHRASES: tuple[str, ...] = (
    "now owned by",
    "reassigned to",
    "re-assigned to",
    "assigned to",
    "will own",
    "is now responsible for",
    "taking over",
    "took over",
    "handed off to",
    "handed it off to",
    "ownership moves to",
    "new owner",
)


def detect_owner_assignment(normalized_statement: str) -> LanguageSignal:
    """Explicit reassignment language, guarded against assistance
    phrasing sharing the same statement (Section 10.9)."""
    if _search(normalized_statement, OWNER_ASSISTANCE_PHRASES).found:
        return LanguageSignal(False, None)
    return _search(normalized_statement, OWNER_ASSIGNMENT_PHRASES)


# --- supersession (Section 10.10) -------------------------------------------

SUPERSESSION_PHRASES: tuple[str, ...] = (
    "instead of",
    "replaces",
    "replacing",
    "supersedes",
    "superseding",
    "in place of",
    "rather than",
    "overrides",
    "overriding",
)


def detect_supersession(normalized_statement: str) -> LanguageSignal:
    return _search(normalized_statement, SUPERSESSION_PHRASES)


# --- risk -> blocker (Section 10.3's compatibility-matrix note: "blocker
# if language says work cannot proceed") --------------------------------

BLOCKING_PHRASES: tuple[str, ...] = (
    "cannot proceed",
    "can't proceed",
    "unable to proceed",
    "is now blocking",
    "work is blocked",
    "stopped work",
    "blocking progress",
    "halted",
)


def detect_blocking_language(normalized_statement: str) -> LanguageSignal:
    return _search(normalized_statement, BLOCKING_PHRASES)


# --- source-local reference (Section 10.3, tier 4) --------------------------

#: Anaphoric references to something already established earlier in the
#: same source ("that date," "this decision") — Section 10.3's tier-4
#: candidate query is gated on exactly this kind of explicit local
#: antecedent, never resolved silently by an LLM.
LOCAL_REFERENCE_PHRASES: tuple[str, ...] = (
    "that date",
    "this decision",
    "the above",
    "as mentioned",
    "per the previous",
    "that item",
    "this action",
    "the previous",
    "as discussed",
    "aforementioned",
    "same as before",
    "per above",
    "the aforementioned",
)


def detect_local_reference(normalized_statement: str) -> LanguageSignal:
    return _search(normalized_statement, LOCAL_REFERENCE_PHRASES)
