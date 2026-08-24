"""Tests for the deterministic Current Project Brief fact builder
(Section 8 "Retrieval"; Prompt 9) — `project_context.retrieval.briefs`.

Covers: every required section is present (even when empty), honest
"empty" sections carry no facts, owner/evidence resolution, the
`previous_summary` diff for transitions, and — critically — that a
second project's facts never appear (FR-022's project isolation applied
to brief retrieval specifically)."""

from __future__ import annotations

import hashlib
import itertools

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import (
    evidence_link_repository,
    evidence_repository,
    people_repository,
    sources_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.briefs import BriefFactType
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.evidence_links import EvidenceLinkSupportRole, EvidenceLinkTargetType
from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus, LedgerTransitionType
from project_context.domain.people import AliasType
from project_context.domain.projects import ProjectCreateInput
from project_context.retrieval.briefs import build_current_project_brief_facts
from project_context.services.ledger import append_ledger_version, create_ledger_item
from project_context.services.projects import ProjectNotFoundError, create_project

_counter = itertools.count()


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def project_id(conn):
    return create_project(
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot", stage="Build")
    ).id


def make_person(conn, *, display_name):
    person = people_repository.create_person(conn, display_name=display_name)
    people_repository.add_alias(
        conn, person.id, alias_type=AliasType.NAME, alias_value=display_name
    )
    return person


def make_content_and_chunk(conn, project_id, *, text):
    n = next(_counter)
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = evidence_repository.insert_artifact(
        conn,
        project_id,
        source.id,
        external_id=f"t:{project_id}:{n}",
        artifact_type=ArtifactType.MANUAL_TEXT,
        title="Notes",
        author=None,
        occurred_at=None,
        external_url=None,
        source_type=None,
    )
    content = evidence_repository.insert_content(
        conn,
        project_id,
        artifact.id,
        sha256=hashlib.sha256(f"{n}:{text}".encode()).hexdigest(),
        raw_storage_path=None,
        mime_type="text/plain",
        byte_size=len(text),
        normalized_text=text,
        parser_name="text",
        parser_version="1",
        parse_status=ParseStatus.PARSED,
        location_map=None,
        original_filename=None,
    )
    spec = ChunkSpec(
        ordinal=0,
        text=text,
        char_start=0,
        char_end=len(text),
        section_path=None,
        sha256=hashlib.sha256(f"{n}:{text}:c".encode()).hexdigest(),
        token_estimate=len(text) // 4,
    )
    (chunk,) = evidence_repository.insert_chunks(conn, project_id, content.id, [spec])
    return content, chunk


def link_evidence(conn, project_id, target_type, target_id, content, chunk):
    return evidence_link_repository.insert_link(
        conn,
        project_id,
        target_type=target_type,
        target_id=target_id,
        content_id=content.id,
        chunk_id=chunk.id,
        char_start=0,
        char_end=len(chunk.text),
        quote=chunk.text,
        support_role=EvidenceLinkSupportRole.SUPPORTS,
    )


def test_every_required_section_is_present_even_when_empty(conn, project_id):
    facts = build_current_project_brief_facts(conn, project_id)
    sections = {s.section: s for s in facts.sections}
    assert set(sections) == {
        "objective_and_scope",
        "current_stage",
        "recent_changes",
        "next_milestone",
        "open_commitments",
        "decisions",
        "risks_and_blockers",
        "unresolved_questions",
    }
    # Nothing has been accepted yet — every section but the project's own
    # metadata is honestly empty.
    assert len(sections["objective_and_scope"].facts) == 1
    assert len(sections["current_stage"].facts) == 1  # project.stage was given
    for key in (
        "recent_changes",
        "next_milestone",
        "open_commitments",
        "decisions",
        "risks_and_blockers",
        "unresolved_questions",
    ):
        assert sections[key].facts == ()


def test_missing_project_raises_not_found(conn):
    with pytest.raises(ProjectNotFoundError):
        build_current_project_brief_facts(conn, "nonexistent-project")


def test_objective_and_scope_fact_carries_objective_and_description(conn):
    connection = conn
    project = create_project(
        connection,
        ProjectCreateInput(name="Acme", objective="Ship the pilot", description="A pilot rollout"),
    )
    facts = build_current_project_brief_facts(connection, project.id)
    scope = facts.sections[0].facts[0]
    assert scope.title == "Ship the pilot"
    assert scope.detail == "A pilot rollout"
    assert scope.fact_type is BriefFactType.PROJECT_META


def test_current_stage_is_honestly_empty_when_not_stated(conn):
    project = create_project(conn, ProjectCreateInput(name="Acme", objective="Ship it"))
    facts = build_current_project_brief_facts(conn, project.id)
    stage_section = next(s for s in facts.sections if s.section == "current_stage")
    assert stage_section.facts == ()


def test_open_commitment_carries_owner_status_due_date_and_evidence(conn, project_id):
    priya = make_person(conn, display_name="Priya")
    content, chunk = make_content_and_chunk(conn, project_id, text="Priya will send the report.")
    item, _v1 = create_ledger_item(
        conn,
        project_id,
        kind=LedgerItemKind.COMMITMENT,
        canonical_title="Send the report",
        canonical_description="Priya will send the report.",
        owner_person_id=priya.id,
        due_date="2026-09-01",
    )
    link_evidence(conn, project_id, EvidenceLinkTargetType.LEDGER_ITEM, item.id, content, chunk)

    facts = build_current_project_brief_facts(conn, project_id)
    section = next(s for s in facts.sections if s.section == "open_commitments")
    assert len(section.facts) == 1
    fact = section.facts[0]
    assert fact.title == "Send the report"
    assert fact.owner_name == "Priya"
    assert fact.due_date == "2026-09-01"
    assert fact.status == "open"
    assert fact.ledger_item_id == item.id
    assert len(fact.evidence_link_ids) == 1


def test_completed_commitment_is_excluded_from_open_commitments(conn, project_id):
    item, _v1 = create_ledger_item(
        conn,
        project_id,
        kind=LedgerItemKind.COMMITMENT,
        canonical_title="Send the report",
    )
    append_ledger_version(
        conn,
        project_id,
        item.id,
        transition_type=LedgerTransitionType.COMPLETE,
        status=LedgerItemStatus.COMPLETED,
    )
    facts = build_current_project_brief_facts(conn, project_id)
    section = next(s for s in facts.sections if s.section == "open_commitments")
    assert section.facts == ()


def test_recent_changes_includes_the_completion_transition_with_previous_summary(conn, project_id):
    item, _v1 = create_ledger_item(
        conn,
        project_id,
        kind=LedgerItemKind.COMMITMENT,
        canonical_title="Send the report",
        due_date="2026-08-28",
    )
    append_ledger_version(
        conn,
        project_id,
        item.id,
        transition_type=LedgerTransitionType.COMPLETE,
        status=LedgerItemStatus.COMPLETED,
    )
    facts = build_current_project_brief_facts(conn, project_id)
    section = next(s for s in facts.sections if s.section == "recent_changes")
    transition_facts = [f for f in section.facts if f.transition_type == "complete"]
    assert len(transition_facts) == 1
    fact = transition_facts[0]
    assert fact.fact_type is BriefFactType.TRANSITION
    assert fact.status == "completed"
    assert fact.previous_summary == "was open"


def test_next_milestone_picks_the_nearest_due_date(conn, project_id):
    create_ledger_item(
        conn,
        project_id,
        kind=LedgerItemKind.MILESTONE,
        canonical_title="Later milestone",
        due_date="2026-12-01",
    )
    create_ledger_item(
        conn,
        project_id,
        kind=LedgerItemKind.MILESTONE,
        canonical_title="Sooner milestone",
        due_date="2026-09-01",
    )
    facts = build_current_project_brief_facts(conn, project_id)
    section = next(s for s in facts.sections if s.section == "next_milestone")
    assert len(section.facts) == 1
    assert section.facts[0].title == "Sooner milestone"


def test_decisions_section_only_includes_active_decisions(conn, project_id):
    active, _ = create_ledger_item(
        conn,
        project_id,
        kind=LedgerItemKind.DECISION,
        canonical_title="Use vendor A",
    )
    canceled_item, _ = create_ledger_item(
        conn,
        project_id,
        kind=LedgerItemKind.DECISION,
        canonical_title="Use vendor B",
    )
    append_ledger_version(
        conn,
        project_id,
        canceled_item.id,
        transition_type=LedgerTransitionType.CANCEL,
        status=LedgerItemStatus.CANCELED,
    )
    facts = build_current_project_brief_facts(conn, project_id)
    section = next(s for s in facts.sections if s.section == "decisions")
    assert [f.title for f in section.facts] == ["Use vendor A"]


def test_risks_and_blockers_combines_both_kinds(conn, project_id):
    create_ledger_item(
        conn, project_id, kind=LedgerItemKind.RISK, canonical_title="Vendor delay risk"
    )
    create_ledger_item(
        conn, project_id, kind=LedgerItemKind.BLOCKER, canonical_title="API access blocker"
    )
    facts = build_current_project_brief_facts(conn, project_id)
    section = next(s for s in facts.sections if s.section == "risks_and_blockers")
    assert {f.title for f in section.facts} == {"Vendor delay risk", "API access blocker"}


def test_unresolved_questions_only_includes_open_ones(conn, project_id):
    open_q, _ = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.OPEN_QUESTION, canonical_title="Which region?"
    )
    resolved_q, _ = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.OPEN_QUESTION, canonical_title="Which vendor?"
    )
    append_ledger_version(
        conn,
        project_id,
        resolved_q.id,
        transition_type=LedgerTransitionType.RESOLVE,
        status=LedgerItemStatus.RESOLVED,
    )
    facts = build_current_project_brief_facts(conn, project_id)
    section = next(s for s in facts.sections if s.section == "unresolved_questions")
    assert [f.title for f in section.facts] == ["Which region?"]


def test_two_project_leakage_is_impossible(conn):
    project_a = create_project(conn, ProjectCreateInput(name="Project A", objective="A work"))
    project_b = create_project(conn, ProjectCreateInput(name="Project B", objective="B work"))

    # Identical wording in both projects — a leakage bug would show up as
    # cross-contamination despite the shared text.
    create_ledger_item(
        conn,
        project_a.id,
        kind=LedgerItemKind.RISK,
        canonical_title="Shared risk wording",
    )
    create_ledger_item(
        conn,
        project_b.id,
        kind=LedgerItemKind.RISK,
        canonical_title="Shared risk wording",
    )

    facts_a = build_current_project_brief_facts(conn, project_a.id)
    facts_b = build_current_project_brief_facts(conn, project_b.id)

    risks_a = next(s for s in facts_a.sections if s.section == "risks_and_blockers").facts
    risks_b = next(s for s in facts_b.sections if s.section == "risks_and_blockers").facts
    assert len(risks_a) == 1
    assert len(risks_b) == 1
    assert risks_a[0].ledger_item_id != risks_b[0].ledger_item_id
    assert risks_a[0].fact_id != risks_b[0].fact_id
    # Every fact_id from project_a is absent from project_b's fact set.
    assert facts_a.fact_by_id().keys().isdisjoint(facts_b.fact_by_id().keys())
