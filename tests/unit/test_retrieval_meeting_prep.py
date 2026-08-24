"""Tests for the deterministic Meeting Preparation Brief fact builder
(Section 8 "Retrieval"; Section 5.9; FR-025; Prompt 14) —
`project_context.retrieval.meeting_prep`.

Covers: previous-meeting/cutoff determination and override, changes
before vs. after cutoff, commitment ordering, the decisions-required/
unanswered-questions owner split, participant resolution (exact email,
alias, ambiguous, unknown), the Calendar-optional manual fallback, and
project isolation."""

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
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.evidence_links import EvidenceLinkSupportRole, EvidenceLinkTargetType
from project_context.domain.ledger import LedgerItemKind
from project_context.domain.people import AliasType
from project_context.domain.projects import ProjectCreateInput
from project_context.retrieval.meeting_prep import (
    MeetingArtifactNotFoundError,
    build_meeting_prep_facts,
    compute_cutoff,
    find_previous_meeting_artifact,
    list_meeting_candidates,
    parse_participant_line,
    resolve_participants,
)
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


def make_meeting_artifact(
    conn, project_id, *, title, occurred_at, kind=ArtifactType.CALENDAR_EVENT
):
    n = next(_counter)
    source = sources_repository.ensure_manual_source(conn, project_id)
    return evidence_repository.insert_artifact(
        conn,
        project_id,
        source.id,
        external_id=f"m:{project_id}:{n}",
        artifact_type=kind,
        title=title,
        author=None,
        occurred_at=occurred_at,
        external_url=f"https://example.com/m{n}",
        source_type=None,
    )


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


def make_evidenced_item(conn, project_id, *, kind, title, owner_person_id=None, due_date=None):
    """Attaches evidence to both the item *and* its current version —
    mirroring `project_context.services.review`'s real accept
    transaction (it always links both `LEDGER_ITEM` and
    `LEDGER_VERSION` for an accepted transition)."""
    item, v1 = create_ledger_item(
        conn,
        project_id,
        kind=kind,
        canonical_title=title,
        owner_person_id=owner_person_id,
        due_date=due_date,
    )
    content, chunk = make_content_and_chunk(conn, project_id, text=f"{title} evidence.")
    for target_type, target_id in (
        (EvidenceLinkTargetType.LEDGER_ITEM, item.id),
        (EvidenceLinkTargetType.LEDGER_VERSION, v1.id),
    ):
        evidence_link_repository.insert_link(
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
    return item


def make_person(conn, *, display_name, email=None):
    person = people_repository.create_person(conn, display_name=display_name, primary_email=email)
    if email:
        people_repository.add_alias(conn, person.id, alias_type=AliasType.EMAIL, alias_value=email)
    return person


# --- previous meeting / cutoff ----------------------------------------


def test_no_prior_meeting_falls_back_to_project_created_at(conn, project_id):
    from project_context.services.projects import get_project

    project = get_project(conn, project_id)
    cutoff_at, previous = compute_cutoff(conn, project, reference_at="2026-08-01T00:00:00Z")
    assert cutoff_at == project.created_at
    assert previous is None


def test_previous_meeting_is_the_most_recent_before_reference_time(conn, project_id):
    from project_context.services.projects import get_project

    make_meeting_artifact(conn, project_id, title="Kickoff", occurred_at="2026-06-01T10:00:00Z")
    middle = make_meeting_artifact(
        conn, project_id, title="Mid check-in", occurred_at="2026-07-01T10:00:00Z"
    )
    make_meeting_artifact(conn, project_id, title="Future", occurred_at="2026-09-01T10:00:00Z")

    project = get_project(conn, project_id)
    cutoff_at, previous = compute_cutoff(conn, project, reference_at="2026-08-01T00:00:00Z")
    assert previous is not None
    assert previous.id == middle.id
    assert cutoff_at == "2026-07-01T10:00:00Z"


def test_previous_meeting_excludes_the_selected_artifact_itself(conn, project_id):
    from project_context.services.projects import get_project

    earlier = make_meeting_artifact(
        conn, project_id, title="Kickoff", occurred_at="2026-06-01T10:00:00Z"
    )
    this_one = make_meeting_artifact(
        conn, project_id, title="This meeting", occurred_at="2026-07-01T10:00:00Z"
    )
    project = get_project(conn, project_id)
    _cutoff_at, previous = compute_cutoff(
        conn, project, reference_at="2026-07-01T10:00:00Z", exclude_artifact_id=this_one.id
    )
    assert previous is not None
    assert previous.id == earlier.id


def test_find_previous_meeting_artifact_returns_none_with_no_reference_time(conn, project_id):
    make_meeting_artifact(conn, project_id, title="Kickoff", occurred_at="2026-06-01T10:00:00Z")
    assert find_previous_meeting_artifact(conn, project_id, before_at=None) is None


def test_list_meeting_candidates_orders_most_recent_first(conn, project_id):
    old = make_meeting_artifact(conn, project_id, title="Old", occurred_at="2026-06-01T10:00:00Z")
    new = make_meeting_artifact(conn, project_id, title="New", occurred_at="2026-07-01T10:00:00Z")
    candidates = list_meeting_candidates(conn, project_id)
    assert [c.id for c in candidates] == [new.id, old.id]


# --- changes before vs. after cutoff -----------------------------------


def test_changes_since_previous_excludes_versions_before_cutoff(conn, project_id):
    """A ledger version's `valid_from` is always "when accepted" (real
    wall-clock time — Section 5.9's "accepted changes"), so the before/
    after boundary is exercised directly against `cutoff_override`
    rather than trying to backdate an accepted change."""
    item = make_evidenced_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, title="A commitment"
    )

    future_facts = build_meeting_prep_facts(
        conn, project_id, manual_title="Sync", cutoff_override="2099-01-01T00:00:00Z"
    )
    future_changes = next(s for s in future_facts.sections if s.section == "changes_since_previous")
    assert future_changes.facts == ()

    past_facts = build_meeting_prep_facts(
        conn, project_id, manual_title="Sync", cutoff_override="2000-01-01T00:00:00Z"
    )
    past_changes = next(s for s in past_facts.sections if s.section == "changes_since_previous")
    assert item.canonical_title in {f.title for f in past_changes.facts}


def test_cutoff_override_replaces_the_computed_cutoff(conn, project_id):
    make_meeting_artifact(conn, project_id, title="Kickoff", occurred_at="2026-06-01T10:00:00Z")
    facts = build_meeting_prep_facts(
        conn,
        project_id,
        manual_title="Follow-up",
        manual_scheduled_at="2026-08-01T00:00:00Z",
        cutoff_override="2026-01-01T00:00:00Z",
    )
    assert facts.cutoff_at == "2026-01-01T00:00:00Z"


# --- meeting selection: artifact vs. manual fallback ------------------


def test_selecting_a_meeting_artifact_uses_its_title_and_time(conn, project_id):
    artifact = make_meeting_artifact(
        conn, project_id, title="Acme Kickoff", occurred_at="2026-08-01T10:00:00Z"
    )
    facts = build_meeting_prep_facts(conn, project_id, meeting_artifact_id=artifact.id)
    assert facts.meeting.title == "Acme Kickoff"
    assert facts.meeting.scheduled_at == "2026-08-01T10:00:00Z"
    assert facts.meeting.meeting_artifact_id == artifact.id


def test_manual_entry_works_with_no_calendar_artifacts_at_all(conn, project_id):
    """Calendar-disabled manual fallback: no meeting-type artifacts
    exist in this project at all, and the manual path still fully
    resolves a meeting and a cutoff."""
    facts = build_meeting_prep_facts(
        conn,
        project_id,
        manual_title="Manual Sync",
        manual_purpose="Discuss the rollout.",
        manual_scheduled_at="2026-08-01T10:00:00Z",
        participant_lines=("Priya <priya@acme.com>",),
    )
    assert facts.meeting.title == "Manual Sync"
    assert facts.meeting.purpose == "Discuss the rollout."
    assert facts.meeting.meeting_artifact_id is None
    assert facts.previous_meeting_artifact_id is None
    from project_context.services.projects import get_project

    assert facts.cutoff_at == get_project(conn, project_id).created_at


def test_unknown_meeting_artifact_id_raises(conn, project_id):
    with pytest.raises(MeetingArtifactNotFoundError):
        build_meeting_prep_facts(conn, project_id, meeting_artifact_id="does-not-exist")


def test_missing_project_raises_not_found(conn):
    with pytest.raises(ProjectNotFoundError):
        build_meeting_prep_facts(conn, "does-not-exist", manual_title="X")


# --- participant parsing/resolution -------------------------------------


def test_parse_participant_line_handles_name_and_angle_email():
    assert parse_participant_line("Priya Shah <priya@acme.com>") == ("Priya Shah", "priya@acme.com")


def test_parse_participant_line_handles_bare_email():
    assert parse_participant_line("priya@acme.com") == (None, "priya@acme.com")


def test_parse_participant_line_handles_bare_name():
    assert parse_participant_line("Priya Shah") == ("Priya Shah", None)


def test_parse_participant_line_handles_blank():
    assert parse_participant_line("   ") == (None, None)


def test_resolve_participants_exact_email_match(conn, project_id):
    person = make_person(conn, display_name="Priya Shah", email="priya@acme.com")
    (resolved,) = resolve_participants(conn, ("Priya@ACME.com",))
    assert resolved.resolution.outcome == "resolved"
    assert resolved.resolution.person_id == person.id
    assert resolved.display_name == "Priya Shah"


def test_resolve_participants_known_alias_match(conn, project_id):
    person = people_repository.create_person(conn, display_name="Bob Jones")
    people_repository.add_alias(conn, person.id, alias_type=AliasType.NAME, alias_value="Bob")
    (resolved,) = resolve_participants(conn, ("Bob",))
    assert resolved.resolution.outcome == "resolved"
    assert resolved.resolution.person_id == person.id


def test_resolve_participants_ambiguous_name_remains_unresolved(conn, project_id):
    p1 = people_repository.create_person(conn, display_name="Alex Smith")
    p2 = people_repository.create_person(conn, display_name="Alex Nguyen")
    people_repository.add_alias(conn, p1.id, alias_type=AliasType.NAME, alias_value="Alex")
    people_repository.add_alias(conn, p2.id, alias_type=AliasType.NAME, alias_value="Alex")

    (resolved,) = resolve_participants(conn, ("Alex",))
    assert resolved.resolution.outcome == "ambiguous"
    assert resolved.resolution.person_id is None
    assert set(resolved.resolution.candidate_person_ids) == {p1.id, p2.id}
    # Never silently guessed — display text falls back to the raw input.
    assert resolved.display_name == "Alex"


def test_resolve_participants_unknown_name_remains_unresolved(conn, project_id):
    (resolved,) = resolve_participants(conn, ("Nobody Known",))
    assert resolved.resolution.outcome == "unknown"
    assert resolved.display_name == "Nobody Known"


def test_resolve_participants_skips_blank_lines(conn, project_id):
    resolved = resolve_participants(conn, ("", "   ", "Real Person"))
    assert len(resolved) == 1
    assert resolved[0].raw_input == "Real Person"


def test_build_meeting_prep_facts_includes_resolved_participants(conn, project_id):
    person = make_person(conn, display_name="Priya Shah", email="priya@acme.com")
    facts = build_meeting_prep_facts(
        conn,
        project_id,
        manual_title="Sync",
        participant_lines=("Priya Shah <priya@acme.com>",),
    )
    assert len(facts.meeting.participants) == 1
    assert facts.meeting.participants[0].resolution.person_id == person.id


# --- decisions required vs. unanswered questions ------------------------


def test_open_question_with_owner_is_decision_required(conn, project_id):
    owner = make_person(conn, display_name="Priya Shah")
    make_evidenced_item(
        conn,
        project_id,
        kind=LedgerItemKind.OPEN_QUESTION,
        title="Which vendor?",
        owner_person_id=owner.id,
    )
    facts = build_meeting_prep_facts(conn, project_id, manual_title="Sync")
    decisions = next(s for s in facts.sections if s.section == "decisions_required")
    unanswered = next(s for s in facts.sections if s.section == "unanswered_questions")
    assert [f.title for f in decisions.facts] == ["Which vendor?"]
    assert unanswered.facts == ()


def test_open_question_without_owner_is_unanswered(conn, project_id):
    make_evidenced_item(
        conn,
        project_id,
        kind=LedgerItemKind.OPEN_QUESTION,
        title="Is the venue booked?",
    )
    facts = build_meeting_prep_facts(conn, project_id, manual_title="Sync")
    decisions = next(s for s in facts.sections if s.section == "decisions_required")
    unanswered = next(s for s in facts.sections if s.section == "unanswered_questions")
    assert decisions.facts == ()
    assert [f.title for f in unanswered.facts] == ["Is the venue booked?"]


def test_resolved_open_question_is_excluded_from_both_sections(conn, project_id):
    item = make_evidenced_item(
        conn,
        project_id,
        kind=LedgerItemKind.OPEN_QUESTION,
        title="Answered already",
    )
    from project_context.domain.ledger import LedgerItemStatus, LedgerTransitionType

    append_ledger_version(
        conn,
        project_id,
        item.id,
        canonical_title=item.canonical_title,
        canonical_description=None,
        status=LedgerItemStatus.RESOLVED,
        owner_person_id=None,
        due_date=None,
        effective_at=None,
        confidence_band=None,
        transition_type=LedgerTransitionType.RESOLVE,
    )
    facts = build_meeting_prep_facts(conn, project_id, manual_title="Sync")
    decisions = next(s for s in facts.sections if s.section == "decisions_required")
    unanswered = next(s for s in facts.sections if s.section == "unanswered_questions")
    assert decisions.facts == ()
    assert unanswered.facts == ()


# --- outstanding commitments ---------------------------------------------


def test_outstanding_commitments_includes_open_and_active_only(conn, project_id):
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.COMMITMENT, title="Open one")
    completed = make_evidenced_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, title="Completed one"
    )
    from project_context.domain.ledger import LedgerItemStatus, LedgerTransitionType

    append_ledger_version(
        conn,
        project_id,
        completed.id,
        canonical_title=completed.canonical_title,
        canonical_description=None,
        status=LedgerItemStatus.COMPLETED,
        owner_person_id=None,
        due_date=None,
        effective_at=None,
        confidence_band=None,
        transition_type=LedgerTransitionType.COMPLETE,
    )
    facts = build_meeting_prep_facts(conn, project_id, manual_title="Sync")
    section = next(s for s in facts.sections if s.section == "outstanding_commitments")
    titles = {f.title for f in section.facts}
    assert titles == {"Open one"}


# --- project isolation -------------------------------------------------


def test_two_project_leakage_is_impossible(conn):
    project_a = create_project(conn, ProjectCreateInput(name="Project A", objective="A")).id
    project_b = create_project(conn, ProjectCreateInput(name="Project B", objective="B")).id
    make_evidenced_item(
        conn, project_a, kind=LedgerItemKind.COMMITMENT, title="Secret sentinel A commitment"
    )

    facts_b = build_meeting_prep_facts(conn, project_b, manual_title="Sync")
    section = next(s for s in facts_b.sections if s.section == "outstanding_commitments")
    assert section.facts == ()
