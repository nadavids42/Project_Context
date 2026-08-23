"""Tests for the review repository: one review per proposal, record
only (Prompt 6: "do not implement the full review transaction until
Prompt 8")."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import (
    evidence_repository,
    observation_repository,
    proposed_mutation_repository,
    review_repository,
    sources_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.review import ProposedMutationAction, ReviewAction
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


@pytest.fixture
def proposal_id(conn, project_id):
    # A proposal only needs a real observation_id per the FK — a minimal
    # observation insert to keep this file focused on reviews.
    text = "Priya will send the report by Friday."
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = evidence_repository.insert_artifact(
        conn,
        project_id,
        source.id,
        external_id="text:1",
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
        sha256=hashlib.sha256(text.encode()).hexdigest(),
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
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        token_estimate=len(text) // 4,
    )
    (chunk,) = evidence_repository.insert_chunks(conn, project_id, content.id, [spec])
    observation, _ = observation_repository.insert_observation(
        conn,
        project_id,
        content_id=content.id,
        chunk_id=chunk.id,
        kind="commitment",
        subject="Priya",
        statement=text,
        evidence_spans=[(chunk.id, 0, len(text))],
        explicitness="explicit",
    )
    proposal = proposed_mutation_repository.insert_proposal(
        conn, project_id, observation_id=observation.id, action=ProposedMutationAction.CREATE
    )
    return proposal.id


def test_insert_review_round_trips(conn, project_id, proposal_id):
    review = review_repository.insert_review(
        conn,
        project_id,
        proposal_id=proposal_id,
        action=ReviewAction.ACCEPT,
        before={"status": "proposed"},
        after={"status": "open"},
        reason_code=None,
        note="looks right",
        duration_ms=1500,
    )
    assert review.action is ReviewAction.ACCEPT
    assert review.before == {"status": "proposed"}
    assert review.after == {"status": "open"}
    assert review.actor == "local-user"
    assert review_repository.get_review_for_proposal(conn, project_id, proposal_id) == review


def test_a_second_review_for_the_same_proposal_is_rejected(conn, project_id, proposal_id):
    review_repository.insert_review(
        conn, project_id, proposal_id=proposal_id, action=ReviewAction.ACCEPT
    )
    with pytest.raises(sqlite3.IntegrityError):
        review_repository.insert_review(
            conn, project_id, proposal_id=proposal_id, action=ReviewAction.REJECT
        )


def test_list_for_project_orders_by_reviewed_at(conn, project_id, proposal_id):
    review_repository.insert_review(
        conn, project_id, proposal_id=proposal_id, action=ReviewAction.ACCEPT
    )
    reviews = review_repository.list_for_project(conn, project_id)
    assert len(reviews) == 1
    assert reviews[0].proposal_id == proposal_id
