"""Tests for the deterministic explicit-language detectors (Section
10.5-10.10). All inputs are already `normalize_form`-normalized, matching
how `project_context.domain.reconciliation.classify_action` calls them."""

from __future__ import annotations

from project_context.domain.reconciliation_language import (
    detect_blocking_language,
    detect_cancellation,
    detect_completion,
    detect_date_change,
    detect_delay,
    detect_local_reference,
    detect_owner_assignment,
    detect_supersession,
)
from project_context.domain.text_normalization import normalize_form


def _norm(text: str) -> str:
    return normalize_form(text)


# --- completion (Section 10.6) ----------------------------------------------


def test_detect_completion_finds_explicit_result_language():
    signal = detect_completion(_norm("The report was sent to the client."))
    assert signal.found is True
    assert signal.matched_phrase == "was sent"


def test_detect_completion_rejects_future_intent():
    signal = detect_completion(_norm("Priya will send the report next week."))
    assert signal.found is False


def test_detect_completion_rejects_future_intent_even_alongside_a_completion_word():
    # "will finish" (future) must win over an incidental "done" elsewhere.
    signal = detect_completion(_norm("She will finish the report and it will be done soon."))
    assert signal.found is False


# --- cancellation vs delay (Section 10.7) -----------------------------------


def test_detect_cancellation_finds_explicit_abandonment():
    signal = detect_cancellation(_norm("This is no longer needed; we won't proceed."))
    assert signal.found is True


def test_detect_delay_is_not_cancellation():
    statement = _norm("The report is delayed and still pending.")
    assert detect_cancellation(statement).found is False
    assert detect_delay(statement).found is True


def test_detect_cancellation_blocked_alone_is_not_cancellation():
    assert detect_cancellation(_norm("Work is blocked on the vendor.")).found is False


# --- changed dates (Section 10.8) -------------------------------------------


def test_detect_date_change_finds_explicit_change_language():
    signal = detect_date_change(_norm("The deadline moved to September 4th."))
    assert signal.found is True
    assert signal.matched_phrase == "moved to"


def test_detect_date_change_absent_for_a_bare_date_mention():
    assert detect_date_change(_norm("The deadline is September 4th.")).found is False


# --- changed owners (Section 10.9) ------------------------------------------


def test_detect_owner_assignment_finds_explicit_reassignment():
    signal = detect_owner_assignment(_norm("The report is now owned by Diego."))
    assert signal.found is True


def test_detect_owner_assignment_rejects_assistance_language():
    signal = detect_owner_assignment(_norm("Diego will help Priya with the report."))
    assert signal.found is False


def test_detect_owner_assignment_rejects_assistance_even_with_an_assignment_word_nearby():
    # "will help" guards the whole statement, even if "assigned" appears too
    # loosely to be the actual claim.
    statement = _norm("Diego will help since he was originally assigned other work.")
    assert detect_owner_assignment(statement).found is False


# --- supersession (Section 10.10) -------------------------------------------


def test_detect_supersession_finds_replacement_language():
    assert detect_supersession(_norm("We will use vendor B instead of vendor A.")).found is True
    assert detect_supersession(_norm("This decision supersedes the prior one.")).found is True


def test_detect_supersession_absent_for_ordinary_wording_cleanup():
    signal = detect_supersession(_norm("Clarifying the vendor decision from last week."))
    assert signal.found is False


# --- risk -> blocker (Section 10.3) -----------------------------------------


def test_detect_blocking_language_finds_cannot_proceed():
    signal = detect_blocking_language(_norm("We cannot proceed until the vendor ships."))
    assert signal.found is True


def test_detect_blocking_language_absent_for_ordinary_risk_language():
    assert detect_blocking_language(_norm("There is a risk the vendor may be late.")).found is False


# --- source-local reference (Section 10.3, tier 4) --------------------------


def test_detect_local_reference_finds_anaphoric_phrase():
    assert detect_local_reference(_norm("That date has now been confirmed.")).found is True


def test_detect_local_reference_absent_without_an_antecedent_phrase():
    assert detect_local_reference(_norm("The report is due Friday.")).found is False
