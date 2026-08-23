"""Tests for the shared normalization primitives (Section 10.1) used by
both exact fingerprinting and fuzzy reconciliation matching."""

from __future__ import annotations

from datetime import date

from project_context.domain.text_normalization import (
    extract_named_entities,
    normalize_date,
    normalize_form,
    subject_tokens,
    token_jaccard,
)


def test_normalize_form_applies_nfkc_lowercase_and_punctuation_removal():
    # "①" (circled digit one) NFKC-normalizes to "1"; full-width "Ａ" to "a".
    assert normalize_form("Report: ①, done!  ") == "report 1 done"
    assert normalize_form("ＡＢＣ") == "abc"


def test_normalize_form_collapses_whitespace():
    assert normalize_form("Send   the\treport\n\nnow") == "send the report now"


def test_normalize_form_does_not_strip_meaningful_punctuation():
    # Hyphens, dates, and reference-like tokens are never in the strip set.
    assert normalize_form("due 2026-09-04, ticket #42") == "due 2026-09-04 ticket #42"


def test_subject_tokens_excludes_status_and_date_vocabulary():
    tokens = subject_tokens("Send the report", "The report is now open, due Friday 2026-09-04.")
    assert "open" not in tokens
    assert "friday" not in tokens
    assert "2026-09-04" not in tokens
    assert "the" not in tokens
    assert "report" in tokens
    assert "send" in tokens


def test_subject_tokens_same_proposition_different_status_and_date_still_overlaps():
    a = subject_tokens("Vendor contract", "The vendor contract is open, next week.")
    b = subject_tokens("Vendor contract", "The vendor contract is completed, Monday.")
    assert token_jaccard(a, b) == 1.0


def test_token_jaccard_empty_sets_are_zero_not_undefined():
    assert token_jaccard(frozenset(), frozenset()) == 0.0
    assert token_jaccard(frozenset({"a"}), frozenset()) == 0.0


def test_token_jaccard_identical_sets_is_one():
    tokens = frozenset({"vendor", "contract"})
    assert token_jaccard(tokens, tokens) == 1.0


def test_extract_named_entities_finds_proper_nouns_not_sentence_starts():
    entities = extract_named_entities("The report was sent to Priya and Diego at Acme.")
    assert "priya" in entities
    assert "diego" in entities
    assert "acme" in entities
    assert "the" not in entities  # sentence-initial common word, filtered


def test_normalize_date_preserves_original_text_alongside_the_parsed_value():
    result = normalize_date("2026-09-04", "September 4th")
    assert result.value == date(2026, 9, 4)
    assert result.original_text == "September 4th"
    assert result.ambiguous is False


def test_normalize_date_flags_ambiguous_when_no_value_was_resolved():
    result = normalize_date(None, "sometime next quarter")
    assert result.value is None
    assert result.ambiguous is True


def test_normalize_date_flags_vague_phrase_even_if_a_value_was_supplied():
    result = normalize_date("2026-09-04", "soon")
    assert result.ambiguous is True


def test_normalize_date_absent_entirely_is_not_ambiguous():
    result = normalize_date(None, None)
    assert result.value is None
    assert result.original_text is None
    assert result.ambiguous is False
