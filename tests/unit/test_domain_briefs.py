"""Tests for the brief domain model (Prompt 9): `CurrentProjectBriefFacts.
fact_by_id`, and the fixed eight-section structure FR-024 requires."""

from __future__ import annotations

from project_context.domain.briefs import (
    CURRENT_BRIEF_SECTIONS,
    BriefFact,
    BriefFactSection,
    BriefFactType,
    CurrentProjectBriefFacts,
)


def _fact(fact_id: str, section: str) -> BriefFact:
    return BriefFact(
        fact_id=fact_id, section=section, fact_type=BriefFactType.CURRENT_STATE, title="Something"
    )


def test_current_brief_sections_has_the_eight_required_sections_in_order():
    expected_keys = (
        "objective_and_scope",
        "current_stage",
        "recent_changes",
        "next_milestone",
        "open_commitments",
        "decisions",
        "risks_and_blockers",
        "unresolved_questions",
    )
    assert tuple(key for key, _heading in CURRENT_BRIEF_SECTIONS) == expected_keys


def test_fact_by_id_indexes_every_fact_across_every_section():
    facts = CurrentProjectBriefFacts(
        project_id="p1",
        generated_at="2026-08-23T00:00:00Z",
        sections=(
            BriefFactSection(
                section="open_commitments", heading="Open Commitments",
                facts=(_fact("f1", "open_commitments"), _fact("f2", "open_commitments")),
            ),
            BriefFactSection(
                section="decisions", heading="Decisions", facts=(_fact("f3", "decisions"),)
            ),
            BriefFactSection(section="current_stage", heading="Current Stage", facts=()),
        ),
    )
    lookup = facts.fact_by_id()
    assert set(lookup) == {"f1", "f2", "f3"}
    assert lookup["f1"].section == "open_commitments"
    assert lookup["f3"].section == "decisions"


def test_fact_by_id_empty_when_every_section_is_empty():
    facts = CurrentProjectBriefFacts(
        project_id="p1",
        generated_at="2026-08-23T00:00:00Z",
        sections=(BriefFactSection(section="decisions", heading="Decisions", facts=()),),
    )
    assert facts.fact_by_id() == {}


def test_brief_fact_defaults_leave_optional_fields_unset():
    fact = BriefFact(
        fact_id="f1", section="open_commitments", fact_type=BriefFactType.CURRENT_STATE, title="X"
    )
    assert fact.kind is None
    assert fact.ledger_item_id is None
    assert fact.evidence_link_ids == ()
