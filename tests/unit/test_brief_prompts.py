"""Stage C (brief composition) prompt loading and per-call input
assembly (Section 12.3, 12.7; Prompt 9)."""

from __future__ import annotations

from project_context.domain.briefs import BriefFact, BriefFactSection, BriefFactType
from project_context.domain.projects import Project, ProjectStatus
from project_context.llm.prompts import (
    BRIEF_PROMPT_VERSION,
    PROMPTS_DIR,
    build_brief_input,
    load_brief_system_prompt,
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


def _fact(**overrides) -> BriefFact:
    fields = {
        "fact_id": "fact-1",
        "section": "open_commitments",
        "fact_type": BriefFactType.CURRENT_STATE,
        "title": "Send the report",
        "status": "open",
        "owner_name": "Priya",
        "due_date": "2026-09-01",
    }
    fields.update(overrides)
    return BriefFact(**fields)


def _one_fact_section(fact: BriefFact | None = None) -> BriefFactSection:
    return BriefFactSection(
        section="open_commitments", heading="Open Commitments", facts=(fact or _fact(),)
    )


def test_brief_prompt_version_names_the_loaded_file():
    assert BRIEF_PROMPT_VERSION == "brief_current_v1"
    assert (PROMPTS_DIR / f"{BRIEF_PROMPT_VERSION}.md").is_file()


def test_brief_system_prompt_forbids_inventing_facts():
    prompt = load_brief_system_prompt()
    lowered = prompt.lower()
    assert "may only say what the facts say" in lowered or "may not" in lowered
    assert "fact_ids" in prompt


def test_brief_system_prompt_defines_all_three_claim_types():
    prompt = load_brief_system_prompt()
    assert "`fact`" in prompt
    assert "`inference`" in prompt
    assert "`suggestion`" in prompt


def test_brief_system_prompt_frames_fact_values_as_untrusted_data():
    prompt = load_brief_system_prompt()
    lowered = prompt.lower()
    assert "quoted" in lowered
    not_a_command = "not a command" in lowered
    never_a_command = "never a command" in lowered
    not_instructions = "not instructions" in lowered
    assert not_a_command or never_a_command or not_instructions


def test_build_brief_input_includes_project_name_and_objective():
    rendered = build_brief_input(project=_project(), sections=(_one_fact_section(),))
    assert "Acme Rollout" in rendered
    assert "Ship the pilot" in rendered


def test_build_brief_input_delimits_each_section_by_its_key():
    rendered = build_brief_input(project=_project(), sections=(_one_fact_section(),))
    assert '<section key="open_commitments" heading="Open Commitments">' in rendered
    assert "</section>" in rendered


def test_build_brief_input_carries_fact_id_and_structured_fields_only():
    fact = _fact(detail="Confirmed in the kickoff call.")
    rendered = build_brief_input(project=_project(), sections=(_one_fact_section(fact),))
    assert '"fact_id": "fact-1"' in rendered
    assert '"owner_name": "Priya"' in rendered
    assert '"due_date": "2026-09-01"' in rendered
    assert "Confirmed in the kickoff call." in rendered


def test_build_brief_input_omits_unset_fields_rather_than_sending_null():
    fact = _fact(due_date=None, owner_name=None)
    rendered = build_brief_input(project=_project(), sections=(_one_fact_section(fact),))
    assert "null" not in rendered
    assert "due_date" not in rendered
    assert "owner_name" not in rendered


def test_build_brief_input_with_multiple_sections_keeps_each_facts_list_separate():
    commitments = BriefFactSection(
        section="open_commitments", heading="Open Commitments",
        facts=(_fact(fact_id="fact-1", section="open_commitments"),),
    )
    risks = BriefFactSection(
        section="risks_and_blockers", heading="Risks and Blockers",
        facts=(
            _fact(
                fact_id="fact-2", section="risks_and_blockers",
                title="Vendor delay risk", status="open", owner_name=None, due_date=None,
            ),
        ),
    )
    rendered = build_brief_input(project=_project(), sections=(commitments, risks))
    assert rendered.index('key="open_commitments"') < rendered.index('"fact_id": "fact-1"')
    assert rendered.index('key="risks_and_blockers"') < rendered.index('"fact_id": "fact-2"')
