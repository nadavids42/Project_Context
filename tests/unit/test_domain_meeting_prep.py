"""Tests for the Meeting Preparation Brief domain model (Prompt 14):
`MEETING_PREP_SECTIONS`'s fixed seven-section structure (FR-025) and
`MeetingPrepBriefFacts.fact_by_id`."""

from __future__ import annotations

from project_context.domain.briefs import BriefFact, BriefFactSection, BriefFactType
from project_context.domain.meeting_prep import (
    MEETING_PREP_SECTIONS,
    UNASSIGNED_OWNER_LABEL,
    MeetingInfo,
    MeetingPrepBriefFacts,
    ResolvedParticipant,
)
from project_context.domain.people import PersonResolution


def _fact(fact_id: str, section: str) -> BriefFact:
    return BriefFact(
        fact_id=fact_id, section=section, fact_type=BriefFactType.CURRENT_STATE, title="Something"
    )


def test_meeting_prep_sections_has_the_seven_required_sections_in_order():
    expected_keys = (
        "meeting_purpose",
        "changes_since_previous",
        "outstanding_commitments",
        "decisions_required",
        "risks_and_blockers",
        "unanswered_questions",
        "suggested_topics",
    )
    assert tuple(key for key, _heading in MEETING_PREP_SECTIONS) == expected_keys


def test_unassigned_owner_label_is_a_stable_display_string():
    assert UNASSIGNED_OWNER_LABEL == "Unassigned"


def _facts(sections: tuple[BriefFactSection, ...]) -> MeetingPrepBriefFacts:
    return MeetingPrepBriefFacts(
        project_id="p1",
        generated_at="2026-08-23T00:00:00Z",
        meeting=MeetingInfo(title="Weekly sync"),
        cutoff_at="2026-08-01T00:00:00Z",
        previous_meeting_artifact_id=None,
        sections=sections,
    )


def test_fact_by_id_indexes_every_fact_across_every_section():
    facts = _facts(
        (
            BriefFactSection(
                section="outstanding_commitments",
                heading="Outstanding Commitments",
                facts=(
                    _fact("f1", "outstanding_commitments"),
                    _fact("f2", "outstanding_commitments"),
                ),
            ),
            BriefFactSection(
                section="decisions_required",
                heading="Decisions Required",
                facts=(_fact("f3", "decisions_required"),),
            ),
            BriefFactSection(
                section="suggested_topics", heading="Suggested Discussion Topics", facts=()
            ),
        )
    )
    lookup = facts.fact_by_id()
    assert set(lookup) == {"f1", "f2", "f3"}
    assert lookup["f1"].section == "outstanding_commitments"
    assert lookup["f3"].section == "decisions_required"


def test_fact_by_id_empty_when_every_section_is_empty():
    facts = _facts(
        (BriefFactSection(section="decisions_required", heading="Decisions Required", facts=()),)
    )
    assert facts.fact_by_id() == {}


def test_meeting_info_defaults_leave_optional_fields_unset():
    meeting = MeetingInfo(title="Kickoff")
    assert meeting.purpose is None
    assert meeting.scheduled_at is None
    assert meeting.meeting_artifact_id is None
    assert meeting.participants == ()


def test_resolved_participant_carries_its_resolution_outcome():
    participant = ResolvedParticipant(
        raw_input="alice@acme.com",
        raw_name=None,
        raw_email="alice@acme.com",
        resolution=PersonResolution(outcome="unknown"),
        display_name="alice@acme.com",
    )
    assert participant.resolution.outcome == "unknown"
    assert participant.resolution.person_id is None
