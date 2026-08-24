"""Tests for `project_context.domain.fathom_matching`: rule precedence,
the deliberately-never-auto-assigned `scheduled_event` tier, and
`FathomMatchRules.from_boundary` normalization/validation (Section
11.5; Prompt 13)."""

from __future__ import annotations

import pytest

from project_context.domain.fathom_matching import (
    RULE_TIER_CLIENT_DOMAIN,
    RULE_TIER_MANUAL,
    RULE_TIER_PARTICIPANT,
    RULE_TIER_RECORDED_BY,
    RULE_TIER_SCHEDULED_EVENT,
    FathomMatchRules,
    FathomMeetingSummary,
    InvalidFathomRuleError,
    ScheduledWindow,
    evaluate_match,
)


def _meeting(
    recording_id="rec1",
    title="Weekly sync",
    meeting_url=None,
    recorded_by_email=None,
    invitees=(),
    speakers=(),
    scheduled_start=None,
    recording_start=None,
) -> FathomMeetingSummary:
    return FathomMeetingSummary(
        recording_id=recording_id,
        title=title,
        meeting_url=meeting_url,
        recorded_by_email=recorded_by_email,
        invitee_emails=tuple(invitees),
        speaker_emails=tuple(speakers),
        scheduled_start=scheduled_start,
        recording_start=recording_start,
    )


# --- FathomMatchRules.from_boundary -----------------------------------------


def test_from_boundary_normalizes_lists_and_strings():
    rules = FathomMatchRules.from_boundary(
        {
            "included_recording_ids": "rec1",  # bare string, not a list
            "recorded_by_emails": ["Recorder@Acme.com"],
            "client_domain": "@Acme.COM",
            "participant_emails": ["Priya@Acme.com"],
            "meeting_urls": ["https://zoom.us/j/123"],
        }
    )
    assert rules.included_recording_ids == ("rec1",)
    assert rules.recorded_by_emails == ("recorder@acme.com",)
    assert rules.client_domain == "acme.com"
    assert rules.participant_emails == ("priya@acme.com",)
    assert rules.meeting_urls == ("https://zoom.us/j/123",)


def test_from_boundary_accepts_none():
    rules = FathomMatchRules.from_boundary(None)
    assert rules.is_configured() is False


def test_from_boundary_parses_scheduled_windows():
    rules = FathomMatchRules.from_boundary(
        {"scheduled_windows": [{"start": "2026-06-01T00:00:00Z", "end": "2026-06-02T00:00:00Z"}]}
    )
    assert rules.scheduled_windows == (
        ScheduledWindow(start="2026-06-01T00:00:00Z", end="2026-06-02T00:00:00Z"),
    )


def test_from_boundary_rejects_unparseable_window():
    with pytest.raises(InvalidFathomRuleError):
        FathomMatchRules.from_boundary(
            {"scheduled_windows": [{"start": "not-a-date", "end": "2026-06-02T00:00:00Z"}]}
        )


def test_from_boundary_rejects_inverted_window():
    with pytest.raises(InvalidFathomRuleError):
        FathomMatchRules.from_boundary(
            {
                "scheduled_windows": [
                    {"start": "2026-06-02T00:00:00Z", "end": "2026-06-01T00:00:00Z"}
                ]
            }
        )


def test_is_configured_false_for_empty_rules():
    assert FathomMatchRules().is_configured() is False


def test_is_configured_true_with_only_meeting_url():
    assert FathomMatchRules(meeting_urls=("https://zoom.us/j/1",)).is_configured() is True


# --- tier 1: manual ----------------------------------------------------


def test_manual_recording_id_matches_regardless_of_other_signals():
    rules = FathomMatchRules(included_recording_ids=("rec1",))
    result = evaluate_match(_meeting(recording_id="rec1"), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_MANUAL


def test_manual_recording_id_takes_precedence_over_lower_tiers():
    rules = FathomMatchRules(
        included_recording_ids=("rec1",),
        client_domain="other.com",
    )
    result = evaluate_match(_meeting(recording_id="rec1", invitees=["x@other.com"]), rules)
    assert result.rule_tier == RULE_TIER_MANUAL


# --- tier 2: recorded_by ------------------------------------------------


def test_recorded_by_matches_configured_team_email():
    rules = FathomMatchRules(recorded_by_emails=("me@example.com",))
    result = evaluate_match(_meeting(recorded_by_email="me@example.com"), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_RECORDED_BY


def test_recorded_by_does_not_match_unconfigured_recorder():
    rules = FathomMatchRules(recorded_by_emails=("someone-else@example.com",))
    result = evaluate_match(_meeting(recorded_by_email="me@example.com"), rules)
    assert result.matched is False


# --- tier 3: client_domain -----------------------------------------------


def test_client_domain_matches_an_invitee():
    rules = FathomMatchRules(client_domain="acme.com")
    result = evaluate_match(_meeting(invitees=["client@acme.com"]), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_CLIENT_DOMAIN


def test_client_domain_matches_a_transcript_speaker():
    rules = FathomMatchRules(client_domain="acme.com")
    result = evaluate_match(_meeting(speakers=["client@acme.com"]), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_CLIENT_DOMAIN


def test_client_domain_does_not_match_a_different_domain():
    rules = FathomMatchRules(client_domain="acme.com")
    result = evaluate_match(_meeting(invitees=["client@other.com"]), rules)
    assert result.matched is False


# --- tier 4: participant -------------------------------------------------


def test_participant_matches_an_explicit_email():
    rules = FathomMatchRules(participant_emails=("priya@acme.com",))
    result = evaluate_match(_meeting(invitees=["priya@acme.com"]), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_PARTICIPANT


# --- tier 5: scheduled_event — deliberately weak/ambiguous ------------------


def test_scheduled_event_matches_a_configured_meeting_url():
    rules = FathomMatchRules(meeting_urls=("https://zoom.us/j/123",))
    result = evaluate_match(_meeting(meeting_url="https://zoom.us/j/123"), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_SCHEDULED_EVENT


def test_scheduled_event_matches_a_bounded_time_window():
    rules = FathomMatchRules(
        scheduled_windows=(
            ScheduledWindow(start="2026-06-01T00:00:00Z", end="2026-06-02T00:00:00Z"),
        )
    )
    result = evaluate_match(_meeting(recording_start="2026-06-01T14:01:00Z"), rules)
    assert result.matched is True
    assert result.rule_tier == RULE_TIER_SCHEDULED_EVENT


def test_scheduled_event_prefers_recording_start_over_scheduled_start():
    rules = FathomMatchRules(
        scheduled_windows=(
            ScheduledWindow(start="2026-06-01T00:00:00Z", end="2026-06-02T00:00:00Z"),
        )
    )
    # scheduled_start is outside the window; recording_start is inside it.
    result = evaluate_match(
        _meeting(scheduled_start="2026-07-01T00:00:00Z", recording_start="2026-06-01T12:00:00Z"),
        rules,
    )
    assert result.matched is True


def test_time_outside_every_configured_window_does_not_match():
    rules = FathomMatchRules(
        scheduled_windows=(
            ScheduledWindow(start="2026-06-01T00:00:00Z", end="2026-06-02T00:00:00Z"),
        )
    )
    result = evaluate_match(_meeting(recording_start="2026-07-01T00:00:00Z"), rules)
    assert result.matched is False


# --- no match ------------------------------------------------------------


def test_no_configured_tier_matching_yields_no_match():
    rules = FathomMatchRules(client_domain="acme.com")
    result = evaluate_match(_meeting(invitees=["nobody@other.com"]), rules)
    assert result.matched is False
    assert result.rule_tier is None


# --- FathomMeetingSummary.from_raw_meeting ----------------------------------


def test_from_raw_meeting_extracts_speaker_emails_from_transcript():
    meeting = {
        "recording_id": "rec1",
        "title": "Kickoff",
        "recorded_by": {"email": "me@example.com"},
        "calendar_invitees": [{"email": "client@acme.com"}],
        "transcript": [
            {
                "speaker": {
                    "display_name": "Alice",
                    "matched_calendar_invitee_email": "alice@acme.com",
                },
                "text": "Hello.",
                "timestamp": "00:00:01",
            },
            {"speaker": {"display_name": "Unmatched"}, "text": "Hi.", "timestamp": "00:00:03"},
        ],
    }
    summary = FathomMeetingSummary.from_raw_meeting(meeting)
    assert summary.recording_id == "rec1"
    assert summary.recorded_by_email == "me@example.com"
    assert summary.invitee_emails == ("client@acme.com",)
    assert summary.speaker_emails == ("alice@acme.com",)


def test_from_raw_meeting_coerces_integer_recording_id_to_str():
    summary = FathomMeetingSummary.from_raw_meeting({"recording_id": 12345, "title": "X"})
    assert summary.recording_id == "12345"
