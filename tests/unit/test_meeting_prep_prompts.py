"""Stage C (Meeting Preparation Brief composition) prompt loading and
per-call input assembly (Section 12.3, 12.7; FR-025; Prompt 14)."""

from __future__ import annotations

from project_context.domain.briefs import BriefFact, BriefFactSection, BriefFactType
from project_context.domain.meeting_prep import MeetingInfo, ResolvedParticipant
from project_context.domain.people import PersonResolution
from project_context.domain.projects import Project, ProjectStatus
from project_context.llm.prompts import (
    MEETING_PREP_PROMPT_VERSION,
    PROMPTS_DIR,
    build_meeting_prep_input,
    load_meeting_prep_system_prompt,
)


def _project() -> Project:
    return Project(
        id="proj-1",
        name="Acme Rollout",
        objective="Ship the pilot",
        description=None,
        stage="Discovery",
        client_name=None,
        status=ProjectStatus.ACTIVE,
        created_at="2026-08-01T00:00:00Z",
        updated_at="2026-08-01T00:00:00Z",
        archived_at=None,
    )


def _meeting(**overrides) -> MeetingInfo:
    fields = {"title": "Weekly sync", "purpose": "Review the rollout."}
    fields.update(overrides)
    return MeetingInfo(**fields)


def _fact(**overrides) -> BriefFact:
    fields = {
        "fact_id": "fact-1",
        "section": "risks_and_blockers",
        "fact_type": BriefFactType.CURRENT_STATE,
        "title": "Vendor delay risk",
        "status": "open",
    }
    fields.update(overrides)
    return BriefFact(**fields)


def _one_fact_section() -> BriefFactSection:
    return BriefFactSection(
        section="risks_and_blockers", heading="Risks Requiring Discussion", facts=(_fact(),)
    )


def test_meeting_prep_prompt_version_names_the_loaded_file():
    assert MEETING_PREP_PROMPT_VERSION == "brief_meeting_prep_v1"
    assert (PROMPTS_DIR / f"{MEETING_PREP_PROMPT_VERSION}.md").is_file()


def test_meeting_prep_system_prompt_forbids_inventing_facts():
    prompt = load_meeting_prep_system_prompt()
    lowered = prompt.lower()
    assert "may only say what the facts say" in lowered or "may not" in lowered
    assert "fact_ids" in prompt


def test_meeting_prep_system_prompt_defines_all_three_claim_types():
    prompt = load_meeting_prep_system_prompt()
    assert "`fact`" in prompt
    assert "`inference`" in prompt
    assert "`suggestion`" in prompt


def test_meeting_prep_system_prompt_documents_suggested_topics_special_case():
    prompt = load_meeting_prep_system_prompt()
    assert "suggested_topics" in prompt


def test_meeting_prep_system_prompt_frames_values_as_untrusted_data():
    prompt = load_meeting_prep_system_prompt()
    lowered = prompt.lower()
    assert "quoted" in lowered
    assert (
        "not a command" in lowered or "never a command" in lowered or "not instructions" in lowered
    )


def test_build_meeting_prep_input_includes_project_and_meeting_metadata():
    rendered = build_meeting_prep_input(
        project=_project(),
        meeting=_meeting(),
        cutoff_at="2026-07-01T00:00:00Z",
        sections=(_one_fact_section(),),
    )
    assert "Acme Rollout" in rendered
    assert "Weekly sync" in rendered
    assert "Review the rollout." in rendered
    assert "2026-07-01T00:00:00Z" in rendered


def test_build_meeting_prep_input_lists_participants_by_display_name_only():
    participant = ResolvedParticipant(
        raw_input="Priya Shah <priya@acme.com>",
        raw_name="Priya Shah",
        raw_email="priya@acme.com",
        resolution=PersonResolution(outcome="resolved", person_id="person-1"),
        display_name="Priya Shah",
    )
    rendered = build_meeting_prep_input(
        project=_project(),
        meeting=_meeting(participants=(participant,)),
        cutoff_at="2026-07-01T00:00:00Z",
        sections=(_one_fact_section(),),
    )
    assert "Priya Shah" in rendered
    assert "priya@acme.com" not in rendered


def test_build_meeting_prep_input_shows_not_stated_for_unknown_purpose():
    rendered = build_meeting_prep_input(
        project=_project(),
        meeting=_meeting(purpose=None),
        cutoff_at="2026-07-01T00:00:00Z",
        sections=(_one_fact_section(),),
    )
    assert "Purpose: Not stated" in rendered


def test_build_meeting_prep_input_delimits_each_section_by_its_key():
    rendered = build_meeting_prep_input(
        project=_project(),
        meeting=_meeting(),
        cutoff_at="2026-07-01T00:00:00Z",
        sections=(_one_fact_section(),),
    )
    assert '<section key="risks_and_blockers" heading="Risks Requiring Discussion">' in rendered


def test_build_meeting_prep_input_allows_a_section_with_empty_facts():
    empty_section = BriefFactSection(
        section="suggested_topics", heading="Suggested Discussion Topics", facts=()
    )
    rendered = build_meeting_prep_input(
        project=_project(),
        meeting=_meeting(),
        cutoff_at="2026-07-01T00:00:00Z",
        sections=(_one_fact_section(), empty_section),
    )
    assert '<section key="suggested_topics"' in rendered
