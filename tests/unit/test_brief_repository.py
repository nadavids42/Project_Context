"""Tests for the brief repository: insert/get/list, finalize, supersession
of a project's previous valid brief, and claim CRUD (Prompt 9)."""

from __future__ import annotations

import pytest

from project_context.db import brief_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.briefs import BriefStatus, BriefType, ClaimType, ClaimValidationStatus
from project_context.domain.projects import ProjectCreateInput
from project_context.services.projects import create_project


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def project_id(conn):
    return create_project(
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
    ).id


def test_insert_and_get_brief_round_trips(conn, project_id):
    brief = brief_repository.insert_brief(
        conn,
        project_id,
        brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z",
        input_snapshot={"sections": []},
    )
    assert brief.status is BriefStatus.GENERATING
    assert brief.markdown is None

    fetched = brief_repository.get_brief(conn, project_id, brief.id)
    assert fetched is not None
    assert fetched.input_snapshot == {"sections": []}
    assert fetched.cutoff_at == "2026-08-23T00:00:00Z"


def test_get_brief_is_project_scoped(conn, project_id):
    other_id = create_project(
        conn, ProjectCreateInput(name="Other", objective="Other work")
    ).id
    brief = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={},
    )
    assert brief_repository.get_brief(conn, other_id, brief.id) is None


def test_insert_brief_with_pregenerated_id(conn, project_id):
    brief = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={}, brief_id="fixed-id",
    )
    assert brief.id == "fixed-id"


def test_finalize_brief_sets_markdown_and_telemetry(conn, project_id):
    brief = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={},
    )
    finalized = brief_repository.finalize_brief(
        conn, project_id, brief.id,
        status=BriefStatus.VALID, markdown="# Brief", model_id="gpt-5.6-terra",
        prompt_version="brief_current_v1", schema_version="brief_composition_v1",
        input_tokens=120, output_tokens=40, estimated_cost_usd=0.001, latency_ms=250,
    )
    assert finalized.status is BriefStatus.VALID
    assert finalized.markdown == "# Brief"
    assert finalized.input_tokens == 120
    assert finalized.estimated_cost_usd == pytest.approx(0.001)


def test_finalize_brief_can_mark_failed_with_safe_error(conn, project_id):
    brief = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={},
    )
    finalized = brief_repository.finalize_brief(
        conn, project_id, brief.id, status=BriefStatus.FAILED, safe_error="LLMTimeoutError"
    )
    assert finalized.status is BriefStatus.FAILED
    assert finalized.safe_error == "LLMTimeoutError"
    assert finalized.markdown is None


def test_list_briefs_for_project_orders_newest_first(conn, project_id):
    first = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={},
    )
    second = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T01:00:00Z", input_snapshot={},
    )
    listed = brief_repository.list_briefs_for_project(conn, project_id)
    assert [b.id for b in listed] == [second.id, first.id]


def test_list_briefs_for_project_filters_by_type(conn, project_id):
    brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={},
    )
    meeting_brief = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.MEETING_PREPARATION,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={},
    )
    listed = brief_repository.list_briefs_for_project(
        conn, project_id, brief_type=BriefType.MEETING_PREPARATION
    )
    assert [b.id for b in listed] == [meeting_brief.id]


def test_supersede_previous_valid_briefs_marks_only_valid_ones(conn, project_id):
    old_valid = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={}, status=BriefStatus.VALID,
    )
    old_failed = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={}, status=BriefStatus.FAILED,
    )
    new_brief = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T01:00:00Z", input_snapshot={}, status=BriefStatus.VALID,
    )
    changed = brief_repository.supersede_previous_valid_briefs(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT, except_brief_id=new_brief.id
    )
    assert changed == 1
    refetched_valid = brief_repository.get_brief(conn, project_id, old_valid.id)
    refetched_failed = brief_repository.get_brief(conn, project_id, old_failed.id)
    refetched_new = brief_repository.get_brief(conn, project_id, new_brief.id)
    assert refetched_valid.status is BriefStatus.SUPERSEDED
    assert refetched_failed.status is BriefStatus.FAILED
    assert refetched_new.status is BriefStatus.VALID


def test_supersede_does_not_touch_a_different_brief_type(conn, project_id):
    meeting_brief = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.MEETING_PREPARATION,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={}, status=BriefStatus.VALID,
    )
    current_brief = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={}, status=BriefStatus.VALID,
    )
    brief_repository.supersede_previous_valid_briefs(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT, except_brief_id=current_brief.id
    )
    refetched_meeting = brief_repository.get_brief(conn, project_id, meeting_brief.id)
    assert refetched_meeting.status is BriefStatus.VALID


def test_insert_and_list_claims(conn, project_id):
    brief = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={},
    )
    brief_repository.insert_claim(
        conn, project_id, brief_id=brief.id, section="open_commitments", ordinal=1,
        claim_text="Second.", claim_type=ClaimType.FACT, cited_fact_ids=("f2",),
        validation_status=ClaimValidationStatus.VALID,
    )
    brief_repository.insert_claim(
        conn, project_id, brief_id=brief.id, section="open_commitments", ordinal=0,
        claim_text="First.", claim_type=ClaimType.FACT, cited_fact_ids=("f1", "f1b"),
        validation_status=ClaimValidationStatus.VALID,
    )
    claims = brief_repository.list_claims_for_brief(conn, project_id, brief.id)
    assert [c.claim_text for c in claims] == ["First.", "Second."]
    assert claims[0].cited_fact_ids == ("f1", "f1b")


def test_claim_stores_validation_status_and_ledger_references(conn, project_id):
    brief = brief_repository.insert_brief(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z", input_snapshot={},
    )
    claim = brief_repository.insert_claim(
        conn, project_id, brief_id=brief.id, section="decisions", ordinal=0,
        claim_text="Invalid.", claim_type=ClaimType.FACT, cited_fact_ids=("bogus",),
        validation_status=ClaimValidationStatus.INVALID_REFERENCE,
    )
    assert claim.validation_status is ClaimValidationStatus.INVALID_REFERENCE
    assert claim.ledger_item_id is None
    assert claim.ledger_version_id is None
