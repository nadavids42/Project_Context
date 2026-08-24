"""Tests for `project_context.domain.calendar_matching`: rule
precedence, include/exclude behavior, domain/attendee matching, and
ambiguity resolution (Section 11.4; Prompt 12)."""

from __future__ import annotations

import pytest

from project_context.domain.calendar_matching import (
    RULE_TIER_DOMAIN_PARTICIPANT,
    RULE_TIER_EVENT_ID,
    RULE_TIER_INCLUDE_RULE,
    RULE_TIER_PROJECT_NAME_TERM,
    CalendarEventSummary,
    CalendarMatchRules,
    InvalidCalendarRuleError,
    evaluate_match,
)


def _event(
    event_id="evt1", title="Weekly sync", description="", organizer="me@example.com",
    attendees=(),
) -> CalendarEventSummary:
    return CalendarEventSummary(
        event_id=event_id, title=title, description=description,
        organizer_email=organizer, attendee_emails=tuple(attendees),
    )


# --- CalendarMatchRules.from_boundary --------------------------------------


def test_from_boundary_normalizes_lists_and_strings():
    rules = CalendarMatchRules.from_boundary(
        {
            "included_event_ids": "evt1",  # bare string, not a list
            "project_name_terms": ["Acme Rollout", " ", "Acme"],
            "client_domain": "@Acme.COM",
            "participant_emails": ["Priya@Acme.com"],
        }
    )
    assert rules.included_event_ids == ("evt1",)
    assert rules.project_name_terms == ("Acme Rollout", "Acme")
    assert rules.client_domain == "acme.com"
    assert rules.participant_emails == ("priya@acme.com",)


def test_from_boundary_defaults_scan_window():
    rules = CalendarMatchRules.from_boundary({})
    assert rules.scan_days_back == 180
    assert rules.scan_days_forward == 90


def test_from_boundary_accepts_none():
    rules = CalendarMatchRules.from_boundary(None)
    assert rules.is_configured() is False


def test_invalid_include_regex_raises_at_construction():
    with pytest.raises(InvalidCalendarRuleError):
        CalendarMatchRules.from_boundary({"include_regex": "(unclosed"})


def test_invalid_exclude_regex_raises_at_construction():
    with pytest.raises(InvalidCalendarRuleError):
        CalendarMatchRules.from_boundary({"exclude_regex": "["})


def test_is_configured_false_for_empty_rules():
    assert CalendarMatchRules().is_configured() is False


def test_is_configured_true_with_only_include_regex():
    assert CalendarMatchRules(include_regex="kickoff").is_configured() is True


# --- rule precedence matrix: tier 1 (event ID) ------------------------------


def test_tier1_event_id_wins_over_every_other_tier():
    rules = CalendarMatchRules(
        included_event_ids=("evt1",),
        project_name_terms=("Unrelated Project",),  # would not otherwise match
    )
    result = evaluate_match(_event(event_id="evt1", title="Random"), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_EVENT_ID


def test_tier1_does_not_match_a_different_event_id():
    rules = CalendarMatchRules(included_event_ids=("evt1",))
    result = evaluate_match(_event(event_id="evt2"), rules)
    assert result.matched is False


# --- tier 2: project name terms ---------------------------------------------


def test_tier2_matches_title_containing_project_term():
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    result = evaluate_match(_event(title="Acme Rollout weekly sync"), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_PROJECT_NAME_TERM


def test_tier2_matches_description_containing_project_term():
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    result = evaluate_match(
        _event(title="Weekly sync", description="Discuss Acme Rollout timeline"), rules
    )
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_PROJECT_NAME_TERM


def test_tier2_is_case_insensitive():
    rules = CalendarMatchRules(project_name_terms=("acme rollout",))
    result = evaluate_match(_event(title="ACME ROLLOUT kickoff"), rules)
    assert result.matched is True


# --- tier 3: domain + participant --------------------------------------


def test_tier3_matches_attendee_client_domain():
    rules = CalendarMatchRules(client_domain="acme.com")
    result = evaluate_match(_event(attendees=["bob@acme.com"]), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_DOMAIN_PARTICIPANT


def test_tier3_matches_organizer_client_domain():
    rules = CalendarMatchRules(client_domain="acme.com")
    result = evaluate_match(_event(organizer="alice@acme.com"), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_DOMAIN_PARTICIPANT


def test_tier3_matches_explicit_participant_email():
    rules = CalendarMatchRules(participant_emails=("priya@partner.com",))
    result = evaluate_match(_event(attendees=["priya@partner.com"]), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_DOMAIN_PARTICIPANT


def test_tier3_does_not_match_unrelated_domain():
    rules = CalendarMatchRules(client_domain="acme.com")
    result = evaluate_match(_event(attendees=["bob@other.com"]), rules)
    assert result.matched is False


# --- tier 4: include terms/regex --------------------------------------------


def test_tier4_matches_include_term():
    rules = CalendarMatchRules(include_terms=("kickoff",))
    result = evaluate_match(_event(title="Project kickoff meeting"), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_INCLUDE_RULE


def test_tier4_matches_include_regex():
    rules = CalendarMatchRules(include_regex=r"sprint \d+")
    result = evaluate_match(_event(title="Sprint 12 planning"), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_INCLUDE_RULE


def test_tier4_include_regex_is_case_insensitive():
    rules = CalendarMatchRules(include_regex=r"KICKOFF")
    result = evaluate_match(_event(title="project kickoff"), rules)
    assert result.matched is True


# --- precedence ordering across tiers ---------------------------------------


def test_precedence_tier1_beats_tier2_when_both_would_match():
    rules = CalendarMatchRules(
        included_event_ids=("evt1",), project_name_terms=("Acme Rollout",),
    )
    result = evaluate_match(_event(event_id="evt1", title="Acme Rollout sync"), rules)
    assert result.rule_tier == RULE_TIER_EVENT_ID


def test_precedence_tier2_beats_tier3_when_both_would_match():
    rules = CalendarMatchRules(
        project_name_terms=("Acme Rollout",), client_domain="acme.com",
    )
    result = evaluate_match(
        _event(title="Acme Rollout sync", attendees=["bob@acme.com"]), rules
    )
    assert result.rule_tier == RULE_TIER_PROJECT_NAME_TERM


def test_precedence_tier3_beats_tier4_when_both_would_match():
    rules = CalendarMatchRules(client_domain="acme.com", include_terms=("sync",))
    result = evaluate_match(_event(title="Weekly sync", attendees=["bob@acme.com"]), rules)
    assert result.rule_tier == RULE_TIER_DOMAIN_PARTICIPANT


# --- exclude rules: global override, ambiguity ------------------------------


def test_exclude_term_overrides_a_tier1_match():
    rules = CalendarMatchRules(included_event_ids=("evt1",), exclude_terms=("cancelled",))
    result = evaluate_match(_event(event_id="evt1", title="Cancelled: sync"), rules)
    assert result.matched is False
    assert "excluded" in result.reason
    assert "event_id" in result.reason  # names the overridden tier


def test_exclude_term_with_no_include_match_is_plainly_excluded():
    rules = CalendarMatchRules(project_name_terms=("Acme",), exclude_terms=("personal",))
    result = evaluate_match(_event(title="personal time"), rules)
    assert result.matched is False
    assert result.reason == "excluded by exclude term 'personal'"


def test_exclude_regex_overrides_include():
    rules = CalendarMatchRules(project_name_terms=("Acme",), exclude_regex=r"\bOOO\b")
    result = evaluate_match(_event(title="Acme — OOO"), rules)
    assert result.matched is False


def test_no_rule_matches_is_unmatched_with_no_reason():
    rules = CalendarMatchRules(project_name_terms=("Acme",))
    result = evaluate_match(_event(title="Totally unrelated"), rules)
    assert result.matched is False
    assert result.rule_tier is None
    assert result.reason is None


def test_ambiguous_conflict_is_never_matched_regardless_of_tier():
    """Prompt 12: "Ambiguous matches must remain unassigned/manual" —
    every tier, when it conflicts with an exclude rule, resolves to
    unmatched, never a partial/best-effort inclusion."""
    for rules in (
        CalendarMatchRules(included_event_ids=("evt1",), exclude_terms=("x",)),
        CalendarMatchRules(project_name_terms=("Acme",), exclude_terms=("x",)),
        CalendarMatchRules(client_domain="acme.com", exclude_terms=("x",)),
        CalendarMatchRules(include_terms=("sync",), exclude_terms=("x",)),
    ):
        event = _event(title="Acme sync x", attendees=["bob@acme.com"])
        result = evaluate_match(event, rules)
        assert result.matched is False
